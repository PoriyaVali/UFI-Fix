"""Probe a configured installed MCP server; preserve vendor logs and originals.

The two write calls create an explicitly labeled snapshot/support draft using
the server's actual default storage. No device or settings operation is called.
Only metadata/check outcomes are written into the report, never raw log text.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import threading
import time


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Client:
    def __init__(self, configuration):
        self.process = subprocess.Popen(
            [configuration['command'], *configuration.get('args', [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.responses = queue.Queue()
        self.stderr = bytearray()
        self.calls = []
        self.tools = set()
        self.sequence = 0
        def stdout():
            for line in iter(self.process.stdout.readline, b''):
                self.responses.put(line)
            self.responses.put(None)
        def stderr():
            for line in iter(self.process.stderr.readline, b''):
                if len(self.stderr) < 8192:
                    self.stderr.extend(line[:8192 - len(self.stderr)])
        for reader in (stdout, stderr):
            threading.Thread(target=reader, daemon=True).start()

    def notify(self, method):
        self.send({'jsonrpc': '2.0', 'method': method})

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=True).encode('utf-8') + b'\n')
        self.process.stdin.flush()

    def rpc(self, method, params=None, error_code=None):
        self.sequence += 1
        message = {'jsonrpc': '2.0', 'id': self.sequence, 'method': method}
        if params is not None:
            message['params'] = params
        started = time.monotonic()
        self.send(message)
        response = self.responses.get(timeout=30)
        assert response is not None, 'Server closed stdout unexpectedly.'
        response = json.loads(response.decode('utf-8'))
        assert response.get('jsonrpc') == '2.0' and response.get('id') == self.sequence, 'Response identity mismatch.'
        self.calls.append({'id': self.sequence, 'method': method,
                           'tool': params.get('name') if params else None,
                           'milliseconds': round((time.monotonic() - started) * 1000),
                           'protocol_error': response.get('error', {}).get('code')})
        if error_code is not None:
            assert response.get('error', {}).get('code') == error_code, response
            return response['error']
        assert 'error' not in response, response.get('error')
        return response['result']

    def tool(self, name, arguments=None, error=None):
        result = self.rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        assert result['isError'] == bool(error), name + ': unexpected tool error status: ' + json.dumps(result.get('structuredContent', {}).get('error', {}))
        structured = result['structuredContent']
        assert json.loads(result['content'][0]['text']) == structured, 'Text/structured result mismatch.'
        if error:
            assert structured['error']['code'] == error, structured
        self.tools.add(name)
        return structured

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Only the probe-owned support server; never touch a vendor process.
                self.process.terminate()
                self.process.wait(timeout=5)
        return self.process.returncode


def probe(configuration):
    executable = Path(configuration['command'])
    args = configuration.get('args', [])
    root = Path(args[args.index('--ufi-dir') + 1])
    sources = {'emmc': root / 'eMMC_ToolBox.log', 'ufs': root / 'UFS_ToolBox.log',
               'android': root / 'Android_ToolBox.log', 'chip': root / 'ChipProg.log'}
    before = {name: checksum(path) for name, path in sources.items()}
    client = Client(configuration)
    try:
        initialized = client.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'installed-ufi-mcp-probe', 'version': '1.0'}})
        client.notify('notifications/initialized')
        assert client.rpc('ping') == {}
        tools = client.rpc('tools/list')['tools']
        expected_tools = json.loads((executable.parent / 'ufi-bridge-tools.json').read_text(encoding='utf-8'))['tools']
        assert tools == expected_tools, 'Runtime tools differ from installed schema.'
        assert len(tools) >= 10
        capabilities = client.tool('ufi.capabilities')
        release_status = client.tool('ufi.updates.status')
        assert release_status['operations_allowed'], 'Installed version cannot start new operations: ' + release_status['code']
        views = {}
        for source in [*sources, 'fix']:
            view = client.tool('ufi.logs.read', {'source': source, 'max_bytes': 8192})
            assert not view['lossy'], source + ': unexpected lossy decoding'
            views[source] = {'file_bytes': view['file_bytes'], 'encoding': view['encoding'],
                             'redaction': view['redaction'], 'truncated': view['truncated']}
        diagnostics = client.tool('ufi.diagnostics.collect')
        assert diagnostics['windows']['available'], 'Windows metadata probe unavailable.'
        ui = client.tool('ufi.ui.inspect', {'max_nodes': 50})
        assert ui['available'], 'UI observation unavailable.'
        snapshot = client.tool('ufi.logs.snapshot', {'source': 'emmc'})
        assert snapshot['sha256'] == before['emmc'], 'Snapshot does not match original log bytes.'
        offset, pages = 0, 0
        while True:
            page = client.tool('ufi.logs.snapshot.read', {'snapshot_id': snapshot['id'],
                                                       'offset': offset, 'max_bytes': 16384})
            assert page['sha256'] == snapshot['sha256'] and not page['lossy']
            pages += 1
            assert pages < 1024, 'Snapshot page loop did not terminate.'
            if not page['has_more']:
                assert page['next_offset'] == snapshot['bytes']
                break
            assert page['next_offset'] > offset
            offset = page['next_offset']
        # Verify the conflict guard without changing any existing draft.
        client.tool('ufi.logs.revision.create', {'snapshot_id': snapshot['id'],
                    'expected_sha256': '0' * 64, 'text': 'Rejected test draft'}, error='snapshot_conflict')
        draft = '\u0622\u0632\u0645\u0627\u06cc\u0634 \u0627\u062a\u0635\u0627\u0644 API\n'
        draft += '\u0627\u06cc\u0646 \u06cc\u06a9 \u06a9\u067e\u06cc \u0622\u0632\u0645\u0627\u06cc\u0634\u06cc \u0627\u0633\u062a\u061b \u0644\u0627\u06af \u0627\u0635\u0644\u06cc \u062a\u063a\u06cc\u06cc\u0631 \u0646\u06a9\u0631\u062f\u0647 \u0627\u0633\u062a.\n'
        revision = client.tool('ufi.logs.revision.create', {'snapshot_id': snapshot['id'],
            'expected_sha256': snapshot['sha256'], 'text': draft, 'note': 'Installed MCP connection test; not a repair outcome.'})
        edited = client.tool('ufi.logs.revision.read', {'revision_id': revision['id']})
        assert edited['text'] == draft and edited['base_id'] == snapshot['id']
        revisions = client.tool('ufi.logs.revisions', {'snapshot_id': snapshot['id']})['revisions']
        assert any(row['id'] == revision['id'] for row in revisions)
        audit = client.tool('ufi.audit.read', {'limit': 100})['events']
        assert {snapshot['id'], revision['id']} <= {row['object_id'] for row in audit}
        client.rpc('tools/call', {'name': 'ufi.logs.read', 'arguments': {'source': '../unapproved'}}, error_code=-32602)
        assert client.rpc('ping') == {}
        support_tools = {tool['name'] for tool in tools if not tool['name'].startswith(('ufi.app.', 'ufi.settings.', 'ufi.device.'))}
        assert support_tools == client.tools
        after = {name: checksum(path) for name, path in sources.items()}
        assert before == after, 'Vendor logs changed during the probe; check for a concurrent vendor operation.'
        report = {'status': 'passed', 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
                  'configuration': configuration, 'executable_sha256': checksum(executable),
                  'server_info': initialized['serverInfo'], 'protocol_version': initialized['protocolVersion'],
                  'update_status': release_status,
                  'tool_count': len(tools), 'tools_exercised': sorted(client.tools), 'log_sources': views,
                  'snapshot_pages': pages, 'snapshot_original_sha_verified': True,
                  'persian_draft_roundtrip': True, 'revision_history_and_audit': True,
                  'checksum_conflict_rejected': True, 'arbitrary_source_rejected': True,
                  'vendor_logs_unchanged': True, 'created_snapshot_id': snapshot['id'],
                  'created_test_revision_id': revision['id'],
                  'windows_version': diagnostics['windows']['windows'],
                  'ufi_version': diagnostics['windows']['executable_version'],
                  'observed_log_markers': [{'source': row['source'], 'code': row['code'], 'status': row['status']}
                                           for row in diagnostics['issues']],
                  'ui_state': ui['ui_state'], 'ui_nodes': len(ui.get('ui', [])),
                  'inaccessible_ufi_processes': ui.get('inaccessible_process_count', 0),
                  'full_ufi_control': capabilities['full_ufi_control'],
                  'device_operations_executed': False, 'client_application_configuration_changed': False,
                  'rpc_calls': client.calls}
    finally:
        exit_code = client.close()
    assert exit_code == 0, 'Support server did not exit cleanly.'
    report['server_exit_code'] = exit_code
    report['stderr_bytes'] = len(client.stderr)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    configuration = json.loads(options.config.read_text(encoding='utf-8-sig'))['mcpServers']['ufi-support']
    report = probe(configuration)
    options.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    summary = {key: report[key] for key in ('status', 'server_info', 'protocol_version', 'tool_count',
               'tools_exercised', 'log_sources', 'snapshot_pages', 'persian_draft_roundtrip',
               'vendor_logs_unchanged', 'ui_state', 'ui_nodes', 'full_ufi_control', 'server_exit_code')}
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
