import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
from ufi_bridge.core import BridgeError, MAX_REVISION, MAX_SNAPSHOT, MODULES, redact
from ufi_bridge.protocol import MAX_MESSAGE, Session, serve
from ufi_bridge.service import Bridge, tool_definitions


class BridgeFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / 'UFI'
        self.root.mkdir()
        (self.root / 'UFI.exe').touch()  # File identity fixture; never executed.
        for key, (_, filename, _) in MODULES.items():
            (self.root / filename).write_bytes((key + '\r\nSerial: secret-123\r\n').encode())
        self.bridge = Bridge(self.root, self.directory / 'support',
                             fix_log=self.directory / 'missing-fix.log',
                             observer=lambda *args, **kwargs: {'available': True, 'processes': [], 'ui': []})

    def call(self, name, **kwargs):
        return self.bridge.call(name, kwargs)

    def snapshot(self, source='emmc'):
        return self.call('ufi.logs.snapshot', source=source)


class BridgeTests(BridgeFixture, unittest.TestCase):
    def test_four_real_source_names_have_bounded_redacted_views(self):
        for source in MODULES:
            view = self.call('ufi.logs.read', source=source)
            self.assertIn(source, view['text'])
            self.assertNotIn('secret-123', view['text'])
            self.assertTrue(view['untrusted_content'])
            self.assertFalse(view['truncated'])

    def test_snapshot_is_exact_and_source_survives_revision(self):
        path = self.root / 'eMMC_ToolBox.log'
        original = path.read_bytes()
        snapshot = self.snapshot()
        self.assertEqual(snapshot['sha256'], hashlib.sha256(original).hexdigest())
        stored = self.bridge.store.load(snapshot['id'], 'snapshot')
        self.assertEqual(stored['payload'], original)
        revision = self.call('ufi.logs.revision.create', snapshot_id=snapshot['id'],
                             expected_sha256=snapshot['sha256'], text='Edited support draft', note='AI suggestion')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.bridge.store.load(snapshot['id'], 'snapshot')['payload'], original)
        view = self.call('ufi.logs.revision.read', revision_id=revision['id'])
        self.assertEqual(view['text'], 'Edited support draft')
        self.assertEqual(view['base_id'], snapshot['id'])
        self.assertEqual(len(self.call('ufi.logs.revisions', snapshot_id=snapshot['id'])['revisions']), 1)
        self.assertEqual(len(self.call('ufi.audit.read')['events']), 2)

    def test_snapshot_survives_vendor_log_rotation(self):
        original = self.snapshot()
        (self.root / 'eMMC_ToolBox.log').write_text('new session', encoding='utf-8')
        view = self.call('ufi.logs.snapshot.read', snapshot_id=original['id'])
        self.assertIn('emmc', view['text'])

    def test_mismatched_checksum_creates_no_revision_or_audit_event(self):
        snapshot = self.snapshot()
        with self.assertRaises(BridgeError) as caught:
            self.call('ufi.logs.revision.create', snapshot_id=snapshot['id'], expected_sha256='0' * 64, text='x')
        self.assertEqual(caught.exception.code, 'snapshot_conflict')
        self.assertEqual(self.call('ufi.logs.revisions', snapshot_id=snapshot['id'])['revisions'], [])
        self.assertEqual(len(self.call('ufi.audit.read')['events']), 1)

    def test_corrupt_snapshot_is_not_treated_as_original(self):
        snapshot = self.snapshot()
        db = sqlite3.connect(self.bridge.store.path)
        try:
            with db:
                db.execute('UPDATE objects SET payload=? WHERE id=?', (b'changed externally', snapshot['id']))
        finally:
            db.close()
        with self.assertRaises(BridgeError) as caught:
            self.call('ufi.logs.snapshot.read', snapshot_id=snapshot['id'])
        self.assertEqual(caught.exception.code, 'artifact_corrupt')

    def test_no_arbitrary_paths_or_unknown_controls(self):
        for name, args in [('ufi.logs.read', {'source': '../secret'}),
                           ('ufi.logs.read', {'source': 'emmc', 'path': 'secret'}),
                           ('ufi.logs.snapshot.read', {'snapshot_id': '../secret'}),
                           ('ufi.settings.set', {'name': 'Erase', 'value': True}),
                           ('ufi.shell', {'command': 'anything'})]:
            with self.subTest(name=name, args=args), self.assertRaises(BridgeError):
                self.bridge.call(name, args)

    def test_state_inside_vendor_installation_is_rejected(self):
        with self.assertRaises(BridgeError):
            Bridge(self.root, self.root / 'support')

    def test_missing_source_is_an_error_not_empty_success(self):
        with self.assertRaises(BridgeError) as caught:
            self.call('ufi.logs.read', source='fix')
        self.assertEqual(caught.exception.code, 'source_missing')

    def test_large_log_tail_is_bounded_and_original_not_truncated(self):
        path = self.root / 'UFS_ToolBox.log'
        data = b'x' * 100000 + b'\nlast line\n'
        path.write_bytes(data)
        view = self.call('ufi.logs.read', source='ufs', max_bytes=256)
        self.assertTrue(view['truncated'])
        self.assertTrue(view['text'].endswith('last line\n'))
        self.assertLessEqual(len(view['text'].encode()), 256)
        self.assertEqual(path.read_bytes(), data)

    def test_oversize_snapshot_does_not_create_artifact_database(self):
        path = self.root / 'UFS_ToolBox.log'
        with path.open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT + 1)
        with self.assertRaises(BridgeError) as caught:
            self.snapshot('ufs')
        self.assertEqual(caught.exception.code, 'source_too_large')
        self.assertFalse(self.bridge.store.path.exists())

    def test_unicode_revision_byte_limit_and_invalid_surrogates(self):
        snapshot = self.snapshot()
        for text in ('\u062a' * MAX_REVISION, '\ud800'):
            with self.subTest(length=len(text)), self.assertRaises(BridgeError):
                self.call('ufi.logs.revision.create', snapshot_id=snapshot['id'],
                          expected_sha256=snapshot['sha256'], text=text)

    def test_utf16_and_utf8_unicode_tails(self):
        path = self.root / 'UFS_ToolBox.log'
        for encoding in ('utf-8', 'utf-16', 'utf-16-be'):
            text = ('\u067e\u0648\u0631\u06cc\u0627 \u0423\u0424\u0418\n' * 100)
            data = text.encode(encoding)
            if encoding == 'utf-16-be':
                data = b'\xfe\xff' + data
            path.write_bytes(data)
            view = self.call('ufi.logs.read', source='ufs', max_bytes=257)
            self.assertTrue(view['truncated'])
            self.assertIn('\u067e\u0648\u0631\u06cc\u0627', view['text'])
            self.assertIn('\u0423\u0424\u0418', view['text'])

    def test_paginated_snapshot_reads_complete_unicode_without_loss(self):
        path = self.root / 'UFS_ToolBox.log'
        expected = ('\u067e\u0648\u0631\u06cc\u0627 \u0423\u0424\u0418\n' * 100)
        for encoding in ('utf-8', 'utf-8-sig', 'utf-16', 'utf-16-be'):
            data = expected.encode(encoding)
            path.write_bytes(b'\xfe\xff' + data if encoding == 'utf-16-be' else data)
            snapshot = self.snapshot('ufs')
            offset, chunks = 0, []
            while True:
                view = self.call('ufi.logs.snapshot.read', snapshot_id=snapshot['id'], offset=offset, max_bytes=257)
                self.assertFalse(view['lossy'])
                chunks.append(view['text'])
                if not view['has_more']:
                    break
                self.assertGreater(view['next_offset'], offset)
                offset = view['next_offset']
            self.assertEqual(''.join(chunks), expected)

    def test_log_pages_and_out_of_range_offsets(self):
        path = self.root / 'UFS_ToolBox.log'
        path.write_bytes(b'a' * 300 + b'b' * 300)
        first = self.call('ufi.logs.read', source='ufs', offset=0, max_bytes=256)
        second = self.call('ufi.logs.read', source='ufs', offset=first['next_offset'], max_bytes=256)
        self.assertEqual(first['text'], 'a' * 256)
        self.assertTrue(second['has_more'])
        with self.assertRaises(BridgeError):
            self.call('ufi.logs.read', source='ufs', offset=10000)

    def test_redaction_does_not_damage_normal_diagnostic_words(self):
        self.assertEqual(redact('Android Identify Device\nBus width: 8'), 'Android Identify Device\nBus width: 8')
        text = redact('IMEI: 123456789012345\nSerial number: abc\nuser@example.com\nC:\\Users\\Person\\log')
        self.assertNotIn('123456789012345', text)
        self.assertNotIn('abc', text)
        self.assertNotIn('user@example.com', text)
        self.assertNotIn('Person', text)

    def test_redaction_of_ui_names_preserves_structured_data(self):
        self.bridge.observer = lambda *a, **k: {'ui': [{'name': 'Serial: secret', 'enabled': True}], 'processes': []}
        result = self.call('ufi.ui.inspect')
        self.assertEqual(result['ui'][0]['name'], 'Serial: [REDACTED]')
        self.assertTrue(result['ui'][0]['enabled'])

    def test_diagnostics_are_candidates_and_no_device_action_is_claimed(self):
        (self.root / 'Android_ToolBox.log').write_text('Please Identify Device first. Access violation.', encoding='utf-8')
        result = self.call('ufi.diagnostics.collect')
        self.assertFalse(result['diagnosis_confirmed'])
        self.assertFalse(result['device_operations_executed'])
        self.assertIn('identify_required', [x['code'] for x in result['issues']])

    def test_readonly_capabilities_create_no_state_and_never_claim_full_control(self):
        result = self.call('ufi.capabilities')
        self.assertEqual(len(result['modules']), 4)
        self.assertFalse(result['full_ufi_control'])
        self.assertTrue(all(not x['settings_write'] and not x['device_operations'] for x in result['modules']))
        self.assertFalse(self.bridge.store.directory.exists())

    def test_symlink_source_cannot_read_outside_root(self):
        source = self.root / 'eMMC_ToolBox.log'
        outside = self.directory / 'private.txt'
        outside.write_text('secret', encoding='utf-8')
        source.unlink()
        try:
            source.symlink_to(outside)
        except OSError:
            self.skipTest('Creating a test symlink requires Windows privilege/developer mode.')
        with self.assertRaises(BridgeError) as caught:
            self.call('ufi.logs.read', source='emmc')
        self.assertEqual(caught.exception.code, 'source_outside_root')


class ProtocolTests(BridgeFixture, unittest.TestCase):
    def new_session(self):
        session = Session(self.bridge)
        response = session.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                     'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                                                'clientInfo': {'name': 'test', 'version': '1'}}})
        self.assertEqual(response['result']['protocolVersion'], '2025-11-25')
        session.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return session

    def test_handshake_required_and_notification_has_no_response(self):
        session = Session(self.bridge)
        response = session.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(response['error']['code'], -32002)
        self.assertIsNone(session.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))

    def test_tool_discovery_and_known_capability_call(self):
        session = self.new_session()
        response = session.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self.assertEqual(response['result']['tools'], tool_definitions())
        response = session.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                     'params': {'name': 'ufi.capabilities', 'arguments': {}}})
        self.assertFalse(response['result']['isError'])
        self.assertFalse(response['result']['structuredContent']['full_ufi_control'])

    def test_bad_arguments_and_unknown_tools_are_protocol_errors(self):
        session = self.new_session()
        for name, args in [('ufi.logs.read', {'source': 'emmc', 'max_bytes': True}),
                           ('ufi.logs.read', {'source': '../etc'}), ('ufi.actions.execute', {})]:
            response = session.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                         'params': {'name': name, 'arguments': args}})
            self.assertEqual(response['error']['code'], -32602)

    def test_missing_log_is_a_tool_error(self):
        session = self.new_session()
        response = session.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                     'params': {'name': 'ufi.logs.read', 'arguments': {'source': 'fix'}}})
        self.assertTrue(response['result']['isError'])
        self.assertEqual(response['result']['structuredContent']['error']['code'], 'source_missing')

    def test_invalid_json_nonstandard_number_and_oversized_message(self):
        for data, expected in [(b'not-json\n', -32700),
                               (b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n', -32700),
                               (b'x' * (MAX_MESSAGE + 1), -32600)]:
            output = io.BytesIO()
            serve(self.bridge, io.BytesIO(data), output)
            self.assertEqual(json.loads(output.getvalue())['error']['code'], expected)

    def test_full_subprocess_client_roundtrip_preserves_original(self):
        path = self.root / 'eMMC_ToolBox.log'
        before = path.read_bytes()
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                        'clientInfo': {'name': 'integration-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': 'ufi.logs.read', 'arguments': {'source': 'emmc'}}}]
        payload = b''.join(json.dumps(item).encode() + b'\n' for item in messages)
        run = subprocess.run([sys.executable, str(SRC / 'UFI-Bridge.py'), '--ufi-dir', str(self.root),
                              '--state-dir', str(self.directory / 'stdio-state')], input=payload,
                             capture_output=True, timeout=10, check=True)
        responses = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual([item['id'] for item in responses], [1, 2])
        self.assertFalse(responses[1]['result']['isError'])
        self.assertNotIn('secret-123', str(responses))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.directory / 'stdio-state').exists())


if __name__ == '__main__':
    unittest.main()
