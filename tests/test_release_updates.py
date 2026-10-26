"""Mandatory-update boundaries and malicious/stale metadata; no firmware actions."""
import copy
import ctypes
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import release_updates as updates
from release_updates import ReleaseGate, UpdateError, download_installer, installer_asset, latest_release, version_tuple
from ufi_bridge.core import BridgeError
from test_ufi_control import ControlFixture
from test_ufi_bridge import BridgeFixture

PAYLOAD = b'MZsynthetic installer fixture; never execute this data'


def release(tag='v2.4.0', **values):
    name = 'UFI-Fix-v' + tag.lstrip('vV') + '-Setup.exe'
    result = {'id': 100, 'tag_name': tag, 'draft': False, 'prerelease': False,
              'published_at': '2026-10-26T10:00:00Z',
              'html_url': updates.RELEASES_URL + '/tag/' + tag,
              'assets': [{'name': name, 'state': 'uploaded', 'size': len(PAYLOAD),
                          'digest': 'sha256:' + hashlib.sha256(PAYLOAD).hexdigest(),
                          'browser_download_url': 'https://github.com/' + updates.REPOSITORY + '/releases/download/' + tag + '/' + name}]}
    result.update(values)
    return result


class Response(io.BytesIO):
    status = 200
    def geturl(self):
        return 'https://release-assets.githubusercontent.com/fixture/setup.exe?test=1'


class PolicyTests(unittest.TestCase):
    def error(self, code, callback):
        with self.assertRaises(UpdateError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_numeric_versions_not_strings_or_release_order(self):
        self.assertGreater(version_tuple('v2.10.0'), version_tuple('2.9.9'))
        self.assertEqual(version_tuple('v2'), version_tuple('2.0.0.0'))
        self.assertEqual(latest_release(lambda page: [release('v2.9.9'), release('v2.10.0')])[1]['tag_name'], 'v2.10.0')
        for invalid in ('2.01', '../2.4.0', '2.4.0-beta', '2.4.0+modified', 'latest', '2.999999', 3, ''):
            self.error('invalid_release', lambda v=invalid: version_tuple(v))

    def test_drafts_and_prereleases_never_force_stable_upgrade(self):
        gate = ReleaseGate('2.3.0', fetch=lambda page: [release('v2.3.0'), release('v999', draft=True), release('v888-beta', prerelease=True)])
        self.assertTrue(gate.status()['operations_allowed'])

    def test_all_pages_checked_for_highest_numeric_release(self):
        pages = {1: [release('v2.3.0')] * 100, 2: [release('v2.10.0')]}
        self.assertEqual(latest_release(pages.__getitem__)[1]['tag_name'], 'v2.10.0')
        self.error('invalid_release', lambda: latest_release(lambda page: [release()] * 100))

    def test_newer_version_blocks_even_without_installer_assets(self):
        gate = ReleaseGate('2.3.0', fetch=lambda page: [release(assets=[])])
        self.error('update_required', gate.require_current)
        self.error('update_asset_missing', gate.update_target)

    def test_current_and_ahead_candidate_require_online_release_verification(self):
        for version in ('2.4.0', '2.5.0'):
            fetch = mock.Mock(return_value=[release()])
            gate = ReleaseGate(version, fetch=fetch)
            self.assertFalse(gate.peek()['operations_allowed'])
            gate.require_current()
            self.assertEqual(fetch.call_count, 1)

    def test_no_offline_grace_after_lease_or_local_clock_change(self):
        tick = [1.0]
        fetch = mock.Mock(return_value=[release('v2.3.0')])
        gate = ReleaseGate('2.3.0', fetch=fetch, clock=lambda: tick[0])
        gate.require_current()
        tick[0] += updates.LEASE_SECONDS + 1
        self.assertFalse(gate.peek()['operations_allowed'])
        fetch.side_effect = UpdateError('update_check_failed', 'offline')
        with mock.patch('time.time', return_value=-999999):
            self.error('update_check_failed', gate.require_current)
        self.assertFalse(gate.peek()['operations_allowed'])

    def test_monotonic_regression_cannot_extend_permission(self):
        tick = [10]
        fetch = mock.Mock(return_value=[release('v2.3.0')])
        gate = ReleaseGate('2.3.0', fetch=fetch, clock=lambda: tick[0])
        gate.require_current()
        tick[0] = 9
        fetch.side_effect = UpdateError('update_check_failed', 'offline')
        self.error('update_check_failed', gate.require_current)

    def test_seen_newer_release_cannot_be_undone_with_old_metadata(self):
        source = [[release()], [release('v2.3.0')]]
        gate = ReleaseGate('2.3.0', fetch=lambda page: source.pop(0))
        self.assertEqual(gate.status()['code'], 'update_required')
        self.assertEqual(gate.status(force=True)['code'], 'release_rollback')
        self.assertFalse(gate.peek()['operations_allowed'])

    def test_empty_malformed_unpublished_or_foreign_releases_fail_closed(self):
        for rows in ([], {}, [{'tag_name': 'v9'}], [release(html_url='https://evil.test/tag/v9')],
                     [release(published_at=None)], [release(draft='false')]):
            gate = ReleaseGate('2.3.0', fetch=lambda page, data=rows: data)
            self.assertFalse(gate.status()['operations_allowed'])

    def test_caller_cannot_mutate_cached_permission(self):
        gate = ReleaseGate('2.3.0', fetch=lambda page: [release()])
        state = gate.status()
        state.update(operations_allowed=True, latest_version='1.0')
        self.error('update_required', gate.require_current)

    def test_installer_requires_exact_project_url_sha_size_and_upload(self):
        changes = ({'digest': None}, {'digest': 'sha256:' + 'x'*64}, {'size': True},
                   {'size': updates.MAX_INSTALLER + 1}, {'state': 'new'},
                   {'browser_download_url': 'http://github.com/' + updates.REPOSITORY},
                   {'browser_download_url': 'https://evil.test/setup.exe'})
        for change in changes:
            row = release()
            row['assets'][0].update(change)
            self.error('invalid_update_asset', lambda data=row: installer_asset(data))

    def test_redirect_rules_reject_api_redirects_http_credentials_and_other_hosts(self):
        self.assertTrue(updates.valid_download_url('https://release-assets.githubusercontent.com/asset?sig=sample'))
        for url in ('http://github.com/a', 'https://github.com.evil.test/a', 'https://user@github.com/a',
                    'https://github.com:444/a', 'file:///tmp/update.exe', 'https://github.com/a#fragment'):
            self.assertFalse(updates.valid_download_url(url))
        redirect = updates._Redirects(False)
        self.error('untrusted_update_url', lambda: redirect.redirect_request(None, None, 302, '', {}, 'https://github.com/a'))

    def test_no_environment_or_local_json_version_override(self):
        with mock.patch.dict(os.environ, {'UFI_SKIP_UPDATE': '1', 'UFI_UPDATE_URL': 'https://evil.test',
                                          'UFI_VERSION': '999.0', 'UFI_OFFLINE': '1'}):
            gate = ReleaseGate('2.3.0', fetch=lambda page: [release()])
            self.error('update_required', gate.require_current)

    def test_metadata_transport_rejects_html_duplicate_keys_and_oversized_body(self):
        class Metadata(Response):
            def geturl(self):
                return updates.API_URL+'?per_page=100&page=1'
        bodies=(b'<html>offline cache</html>', b'[{"draft":false,"draft":true}]',b' '* (updates.MAX_METADATA+1))
        for body in bodies:
            opener=mock.Mock(open=lambda *args,data=body,**kwargs:Metadata(data))
            with mock.patch.object(updates,'_opener',return_value=opener):
                with self.assertRaises(UpdateError):updates.fetch_release_page(1)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.gate = ReleaseGate('2.3.0', fetch=lambda page: [release()])

    def opener(self, payload=PAYLOAD):
        return mock.Mock(open=lambda *args, **kwargs: Response(payload))

    def test_verified_complete_download_and_fresh_metadata_before_publication(self):
        result = download_installer(self.gate, self.folder, self.opener())
        self.assertEqual(Path(result['path']).read_bytes(), PAYLOAD)
        self.assertEqual(result['sha256'], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertFalse(list(self.folder.glob('*.partial')))

    def test_hash_mismatch_short_long_and_non_executable_never_publish(self):
        for data in (PAYLOAD[:-1], PAYLOAD+b'extra', b'MZ'+b'x'*(len(PAYLOAD)-2)):
            with self.assertRaises(UpdateError) as caught:
                download_installer(self.gate, self.folder, self.opener(data))
            self.assertEqual(caught.exception.code, 'update_integrity_failed')
            self.assertFalse(list(self.folder.iterdir()))
        row = release()
        data = b'not an executable'
        row['assets'][0].update(size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest())
        gate = ReleaseGate('2.3.0', fetch=lambda page: [row])
        with self.assertRaises(UpdateError):
            download_installer(gate, self.folder, self.opener(data))
        self.assertFalse(list(self.folder.iterdir()))

    def test_published_release_changes_during_download_requires_restart(self):
        rows = [[release()], [release('v2.5.0', id=101)]]
        gate = ReleaseGate('2.3.0', fetch=lambda page: rows.pop(0))
        with self.assertRaises(UpdateError) as caught:
            download_installer(gate, self.folder, self.opener())
        self.assertEqual(caught.exception.code, 'release_changed')
        self.assertFalse(list(self.folder.iterdir()))

    def test_network_error_never_uses_a_previous_download(self):
        existing = self.folder/'previous.exe'
        existing.write_bytes(PAYLOAD)
        opener = mock.Mock()
        opener.open.side_effect = OSError('offline')
        with self.assertRaises(UpdateError):
            download_installer(self.gate, self.folder, opener)
        self.assertEqual([p.name for p in self.folder.iterdir()], ['previous.exe'])

    def test_retagged_release_with_same_id_and_digest_is_not_accepted(self):
        before=release()
        after=release('v2.5.0')
        # Both records intentionally keep the same release ID and payload hash.
        pages=[[before],[after]]
        gate=ReleaseGate('2.3.0',fetch=lambda page:pages.pop(0))
        with self.assertRaises(UpdateError) as caught:
            download_installer(gate,self.folder,self.opener())
        self.assertEqual(caught.exception.code,'release_changed')
        self.assertFalse(list(self.folder.iterdir()))


class ControlBoundaryTests(ControlFixture, unittest.TestCase):
    def deny(self):
        self.updates.require_current.side_effect = UpdateError('update_required', 'Update first')

    def test_privileged_controller_stops_before_open_select_or_identify(self):
        self.deny()
        for callback in (lambda: self.call('ufi.app.open', module='emmc'), self.setting,
                         lambda: self.call('ufi.device.identify', module='emmc', instance_id='123:456')):
            self.error('update_required', callback)
        self.assertEqual(self.native.dispatched, [])

    def test_second_check_before_dispatch_cannot_be_bypassed_by_old_lease(self):
        self.updates.require_current.side_effect = [None, UpdateError('update_required', 'new release')]
        self.error('update_required', self.setting)
        self.assertEqual(self.native.dispatched, [])

    def test_existing_result_and_safe_idle_close_remain_available(self):
        request = self.start_identify()
        self.deny()
        self.assertFalse(self.call('ufi.device.result', request_id=request)['finished'])
        self.assertEqual(self.call('ufi.app.close', instance_id='123:456')['status'], 'closed')


class BridgeBoundaryTests(BridgeFixture, unittest.TestCase):
    def test_old_bridge_cannot_start_control_helper_or_write_support_artifacts(self):
        self.bridge.updates.require_current.side_effect = UpdateError('update_required', 'new release')
        transport = mock.Mock()
        self.bridge.control = transport
        for name, args in (('ufi.app.open', {'module':'emmc'}), ('ufi.logs.snapshot', {'source':'emmc'})):
            with self.assertRaises(BridgeError) as caught:
                self.bridge.call(name,args)
            self.assertEqual(caught.exception.code, 'update_required')
        transport.call.assert_not_called()
        self.assertFalse(self.bridge.store.path.exists())

    def test_update_tool_rejects_client_version_url_and_skip_options(self):
        for args in ({'current_version':'999'}, {'skip':True}, {'url':'https://evil.test'}):
            with self.assertRaises(BridgeError) as caught:
                self.bridge.call('ufi.updates.status',args)
            self.assertEqual(caught.exception.code, 'invalid_arguments')


@unittest.skipUnless(os.name=='nt', 'Windows launcher and installer boundaries')
class WindowsBoundaryTests(unittest.TestCase):
    def setUp(self):
        logging = mock.patch.object(self.app, 'log', side_effect=lambda text: '2000-01-01 ' + text)
        logging.start()
        self.addCleanup(logging.stop)

    @classmethod
    def setUpClass(cls):
        source=Path(__file__).resolve().parents[1]/'src'
        for attr,filename in (('app','UFI-Fix.py'),('setup','UFI-Fix-Setup.py')):
            spec=importlib.util.spec_from_file_location('mandatory_'+attr,source/filename)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            setattr(cls,attr,module)

    def test_direct_run_never_launches_when_update_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'UFI.exe').touch()
            with (mock.patch.object(self.app.UPDATE_GATE,'require_current',side_effect=UpdateError('update_required','new release')),
                  mock.patch.object(self.app.subprocess,'Popen') as launch):
                self.assertEqual(self.app.run_ufi(directory,'/emmc'),3)
                launch.assert_not_called()

    def test_shortcut_lock_opens_update_gui_and_does_not_launch_vendor(self):
        with (mock.patch.object(self.app.UPDATE_GATE,'require_current',side_effect=UpdateError('update_required','new release')),
              mock.patch.object(self.app,'Gui') as gui, mock.patch.object(self.app,'run_ufi') as launch):
            self.assertEqual(self.app.headless('/emmc'),3)
            gui.return_value.run.assert_called_once();launch.assert_not_called()

    def test_outdated_installer_stops_before_bundle_or_config_mutation(self):
        with (mock.patch.object(self.setup.UPDATE_GATE,'require_current',side_effect=UpdateError('update_required','new release')),
              mock.patch.object(self.setup,'install_bundle') as install):
            self.assertIsNone(self.setup.perform_install('unused','unused',False,False,False,lambda _:None))
            install.assert_not_called()

    def test_wait_checks_actual_child_creation_and_never_terminates_it(self):
        import update_runtime
        child=subprocess.Popen([sys.executable,'-c',"import sys; print('ready',flush=True); sys.stdin.readline()"],
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertEqual(child.stdout.readline().strip(),b'ready')
            kernel=update_runtime._kernel();handle=kernel.OpenProcess(0x1000,False,child.pid)
            try:created=update_runtime._creation(kernel,handle)
            finally:kernel.CloseHandle(handle)
            with self.assertRaises(UpdateError) as caught:
                update_runtime.wait_for_launcher(child.pid,created+1,10)
            self.assertEqual(caught.exception.code,'invalid_update_handoff')
            self.assertIsNone(child.poll())
            with self.assertRaises(UpdateError):update_runtime.wait_for_launcher(child.pid,created,10)
            self.assertIsNone(child.poll())
            child.stdin.write(b'exit\n');child.stdin.flush()
            update_runtime.wait_for_launcher(child.pid,created,5000)
            child.communicate(timeout=5)
        finally:
            if child.poll() is None:child.terminate();child.communicate(timeout=5)

    def test_windows_tls_does_not_accept_environment_ca_file(self):
        import ssl
        with mock.patch.dict(os.environ,{'SSL_CERT_FILE':r'C:\untrusted\ca.pem','SSL_CERT_DIR':r'C:\untrusted'}):
            context=updates._tls_context()
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(len(context.get_ca_certs()),0)

    def test_changed_download_never_elevates_and_valid_file_is_locked_at_handoff(self):
        import update_runtime
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'setup.exe';path.write_bytes(PAYLOAD)
            row=release();asset=row['assets'][0]
            downloaded={'path':str(path),'sha256':hashlib.sha256(PAYLOAD).hexdigest(),
                        'bytes':len(PAYLOAD),'release_id':row['id'],'version':'2.4.0'}
            gate=mock.Mock(update_target=mock.Mock(return_value=(row,asset)))
            with (mock.patch.object(update_runtime,'busy_processes',return_value=[]),
                  mock.patch.object(update_runtime,'secure_update_directory',return_value=path.parent),
                  mock.patch('ufi_bridge.control_ipc.elevate') as elevate):
                path.write_bytes(PAYLOAD[:-1]+b'X')
                with self.assertRaises(UpdateError):update_runtime.launch_installer(downloaded,gate)
                elevate.assert_not_called()
                path.write_bytes(PAYLOAD)
                def held(command):
                    self.assertEqual(command[0],str(path))
                    self.assertIn('--wait-for-created',command)
                    with self.assertRaises(PermissionError):path.write_bytes(b'changed')
                    with self.assertRaises(PermissionError):path.rename(path.with_suffix('.moved'))
                    kernel=update_runtime._kernel()
                    return kernel.OpenProcess(0x1000,False,os.getpid())
                elevate.side_effect=held
                update_runtime.launch_installer(downloaded,gate)
                elevate.assert_called_once()

    def test_running_programmer_blocks_installer_without_closing_or_elevating(self):
        import update_runtime
        with (mock.patch.object(update_runtime,'busy_processes',return_value=[{'pid':123,'name':'UFI.exe'}]),
              mock.patch('ufi_bridge.control_ipc.elevate') as elevate):
            with self.assertRaises(UpdateError) as caught:update_runtime.launch_installer({})
            self.assertEqual(caught.exception.code,'update_apps_running')
            elevate.assert_not_called()
