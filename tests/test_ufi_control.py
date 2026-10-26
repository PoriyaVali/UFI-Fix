"""Control guards and result attribution; no physical device operations."""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ufi_bridge.control import (Controller, CLOCKS, VOLTAGES, WIDTHS, UFS_MODES,
                                bindings, verified_vendor)
from ufi_bridge.core import BridgeError
from ufi_bridge.service import Bridge, tool_definitions


def combo(handle, options, left, parent=10):
    return dict(hwnd=handle, parent=parent, options=list(options), selection=0,
                visible=True, enabled=True, rect=[left, 10, left + 10, 30],
                name='', **{'class': 'TComboBox'})


class FakeNative:
    def __init__(self):
        self.apps = [dict(instance_id='123:456', pid=123, module='emmc', hwnd=1,
                          enabled=True, title='eMMC ToolBox', dialogs=[], status='window_observed')]
        self.nodes = [combo(20, VOLTAGES, 10), combo(21, VOLTAGES, 30),
                      combo(22, CLOCKS, 50), combo(23, WIDTHS, 70),
                      combo(99, VOLTAGES, 90, parent=999),
                      dict(hwnd=30, parent=10, name='Identify eMMC', visible=True,
                           enabled=False, **{'class': 'TButton'})]
        self.dispatched = []

    def instances(self):
        return copy.deepcopy(self.apps)

    def children(self, hwnd):
        return copy.deepcopy(self.nodes)

    def launch(self, module):
        self.dispatched.append(('launch', module))
        self.apps = [dict(self.apps[0], module=module)]

    def close_window(self, instance):
        self.dispatched.append(('close', instance['instance_id']))
        self.apps = []

    def instance_alive(self, instance):
        return any(a['instance_id'] == instance['instance_id'] for a in self.apps)

    def select(self, node, index, instance):
        self.dispatched.append(('select', node['hwnd'], index))
        next(n for n in self.nodes if n['hwnd'] == node['hwnd'])['selection'] = index

    def identify(self, node, instance):
        self.dispatched.append(('identify', node['hwnd']))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'UFI.exe').write_bytes(b'not a real executable')
        self.native = FakeNative()
        self.controller = Controller(self.root, self.native)
        patcher = mock.patch('ufi_bridge.control.verified_vendor', return_value=self.root / 'UFI.exe')
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, name, **args):
        return self.controller.call(name, args)

    def setting(self, **overrides):
        args = dict(module='emmc', instance_id='123:456', setting='clock', value='24 MHz', expected_value='Auto')
        return self.call('ufi.settings.set', **dict(args, **overrides))

    def error(self, code, callback):
        with self.assertRaises(BridgeError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_exact_binary_required_without_changing_a_file(self):
        self.error('unsupported_vendor_build', lambda: verified_vendor(self.root))

    def test_mapping_uses_signatures_siblings_and_excludes_other_panels(self):
        self.native.nodes.reverse()
        mapped = bindings('emmc', self.native.nodes)
        self.assertEqual({k: n['hwnd'] for k, n in mapped.items()},
                         dict(vcc=20, vccq=21, clock=22, bus_width=23))
        self.native.nodes.append(combo(24, CLOCKS, 200, parent=20))
        self.error('control_mapping_unavailable', lambda: bindings('emmc', self.native.nodes))

    def test_read_options_and_change_confirm_gui_only(self):
        read = self.call('ufi.settings.list', module='emmc')
        self.assertEqual([s['id'] for s in read['settings']], ['vcc', 'vccq', 'clock', 'bus_width'])
        result = self.setting()
        self.assertEqual(result['status'], 'gui_selection_verified')
        self.assertFalse(result['hardware_application_verified'])
        self.assertEqual(self.native.dispatched, [('select', 22, 7)])
        self.assertEqual(self.call('ufi.settings.get', module='emmc', setting='clock')['setting']['value'], '24 MHz')

    def test_noop_does_not_dispatch_voltage_or_notification(self):
        self.assertEqual(self.setting(setting='vcc', value='Auto')['status'], 'unchanged')
        self.assertEqual(self.native.dispatched, [])

    def test_expired_or_disconnected_inspection_cannot_dispatch(self):
        def expired():
            self.controller.deadline = time.monotonic() - 1
        self.controller.session_check = expired
        self.error('control_deadline', self.setting)
        def disconnected():
            raise BridgeError('control_session_ended', 'test disconnect')
        self.controller.session_check = disconnected
        self.error('control_session_ended', self.setting)
        self.assertEqual(self.native.dispatched, [])

    def test_conflict_invalid_value_and_stale_instance_never_dispatch(self):
        self.error('setting_conflict', lambda: self.setting(expected_value='12 MHz'))
        self.error('invalid_setting_value', lambda: self.setting(value='999 MHz'))
        self.error('stale_instance', lambda: self.setting(instance_id='123:457'))
        self.assertEqual(self.native.dispatched, [])

    def test_hidden_disabled_modal_busy_and_duplicate_windows_fail_closed(self):
        for flag in ('visible', 'enabled'):
            self.native.nodes[2][flag] = False
            self.error('control_disabled', self.setting)
            self.native.nodes[2][flag] = True
        self.native.apps[0]['dialogs'] = ['Native warning']
        self.error('ui_blocked', self.setting)
        self.assertFalse(self.call('ufi.settings.list', module='emmc')['settings'][0]['writable'])
        self.native.apps[0]['dialogs'] = []
        self.native.nodes.append(dict(self.native.nodes[-1], name='Stop', enabled=True))
        self.error('device_busy', self.setting)
        self.native.nodes.pop()
        self.native.apps.append(dict(self.native.apps[0], instance_id='124:456'))
        self.error('ambiguous_instance', lambda: self.call('ufi.settings.list', module='emmc'))
        self.assertEqual(self.native.dispatched, [])

    def test_setting_result_must_be_observed_and_same_instance(self):
        with mock.patch.object(self.native, 'select'):
            self.error('setting_unconfirmed', self.setting)
        def restart(*args):
            self.native.apps[0]['instance_id'] = '123:999'
        with mock.patch.object(self.native, 'select', side_effect=restart):
            self.error('stale_instance', self.setting)

    def test_ufs_hidden_profile_reports_no_voltage_or_frequency(self):
        self.native.apps[0]['module'] = 'ufs'
        self.native.nodes = [combo(20, UFS_MODES, 10), combo(21, ['Auto'], 30)]
        for node in self.native.nodes:
            node['visible'] = False
        result = self.call('ufi.settings.list', module='ufs')
        self.assertEqual(result['unmapped'], ['vcc', 'vccq', 'clock'])
        self.assertTrue(all(not s['writable'] for s in result['settings']))
        self.error('setting_unavailable', lambda: self.setting(module='ufs', setting='vcc'))
        self.error('control_disabled', lambda: self.setting(module='ufs', setting='link_mode', value='PWM-GEAR'))

    def test_open_is_idempotent_and_close_targets_one_instance(self):
        self.assertEqual(self.call('ufi.app.open', module='emmc')['status'], 'already_running')
        self.assertEqual(self.native.dispatched, [])
        self.assertEqual(self.call('ufi.app.close', instance_id='123:456')['status'], 'closed')
        self.error('stale_instance', lambda: self.call('ufi.app.close', instance_id='123:456'))
        self.assertEqual(self.native.dispatched, [('close', '123:456')])

    def test_disabled_identify_does_not_click_or_create_job(self):
        self.error('device_not_ready', lambda: self.call('ufi.device.identify', module='emmc', instance_id='123:456'))
        self.assertEqual(self.native.dispatched, [])
        self.assertEqual(self.controller.requests, {})

    def test_hidden_window_is_not_proof_that_process_exited(self):
        with mock.patch.object(self.native, 'instance_alive', return_value=True), mock.patch('ufi_bridge.control.time.sleep'):
            result = self.call('ufi.app.close', instance_id='123:456')
        self.assertEqual(result['status'], 'close_requested')

    def start_identify(self):
        self.native.nodes[-1]['enabled'] = True
        self.log = self.root / 'eMMC_ToolBox.log'
        self.log.write_bytes(b'Old historical successful Identify\n')
        return self.call('ufi.device.identify', module='emmc', instance_id='123:456')['request_id']

    def test_identify_never_reuses_historical_success_and_prevents_rapid_repeats(self):
        request = self.start_identify()
        result = self.call('ufi.device.result', request_id=request)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['text'], '')
        self.assertFalse(result['chip_identified'])
        self.error('device_busy', lambda: self.call('ufi.device.identify', module='emmc', instance_id='123:456'))
        with self.log.open('ab') as stream:
            stream.write(b'New attempt: device disconnected\nSerial: secret\n')
        result = self.call('ufi.device.result', request_id=request)
        self.assertEqual(result['status'], 'output_observed')
        self.assertNotIn('historical', result['text'])
        self.assertNotIn('secret', result['text'])
        self.assertFalse(result['chip_identified'])

    def test_identify_rotation_and_unknown_job_not_a_success(self):
        request = self.start_identify()
        self.log.write_bytes(b'Other session\n' * 100)
        self.error('log_changed', lambda: self.call('ufi.device.result', request_id=request))
        self.error('unknown_request', lambda: self.call('ufi.device.result', request_id='0' * 32))

    def test_mcp_validates_again_and_marks_mutations(self):
        transport = mock.Mock()
        bridge = Bridge(self.root, self.root.parent / 'unused-state', control=transport)
        for args in (dict(module='emmc', setting='vcc', value='3.3 V'),
                     dict(module='emmc', instance_id='123:456', setting='erase', value='yes', expected_value='no')):
            self.error('invalid_arguments', lambda: bridge.call('ufi.settings.set', args))
        transport.call.assert_not_called()
        definitions = {t['name']: t for t in tool_definitions()}
        self.assertEqual(len(definitions), 18)
        self.assertTrue(definitions['ufi.settings.set']['annotations']['destructiveHint'])
        self.assertFalse(definitions['ufi.app.close']['annotations']['readOnlyHint'])


@unittest.skipUnless(os.name == 'nt', 'Windows named-pipe transport')
class PipeTests(unittest.TestCase):
    def test_exit_check_uses_held_handle_and_does_not_reopen_disappearing_pid(self):
        from ufi_bridge.native_control import Native
        native = Native.__new__(Native)
        native.k = mock.Mock()
        native.k.OpenProcess.return_value = 100
        def times(handle, created, exited, kernel, user):
            created._obj.dwLowDateTime = 456
            created._obj.dwHighDateTime = 0
            return True
        native.k.GetProcessTimes.side_effect = times
        native.process = mock.Mock(side_effect=AssertionError('Do not reopen PID during teardown'))
        instance = dict(pid=123, instance_id='123:456')
        native.k.WaitForSingleObject.return_value = 0
        self.assertFalse(native.instance_alive(instance))
        native.k.WaitForSingleObject.return_value = 258
        self.assertTrue(native.instance_alive(instance))
        self.assertFalse(native.instance_alive(dict(instance, instance_id='123:455')))
        native.process.assert_not_called()

    def test_real_json_pipe_checks_arguments_rejects_shell_and_exits_on_disconnect(self):
        from multiprocessing.connection import Client
        from ufi_bridge.control_ipc import parent_identity, libraries
        import _winapi
        pipe = r'\\.\pipe\UFI-Control-' + secrets.token_hex(16)
        key = secrets.token_bytes(32)
        handle, created, _ = parent_identity(os.getpid())
        libraries()[0].CloseHandle(handle)
        # Test-only process patches native observation; no alternate production
        # command, bypass flag, executable fixture or UAC prompt is provided.
        source = Path(__file__).resolve().parents[1] / 'src'
        code = """import sys,types
sys.path.insert(0,sys.argv[1])
from ufi_bridge import control,control_ipc,native_control
control.verified_vendor=lambda root: None
control_ipc.verified_vendor=lambda root: None
native_control.Native.instances=lambda self: []
options=types.SimpleNamespace(pipe=sys.argv[2],key=sys.argv[3],parent=int(sys.argv[4]),created=int(sys.argv[5]),ufi_dir=sys.argv[6])
raise SystemExit(control_ipc.serve_control(options))
"""
        process = subprocess.Popen([sys.executable, '-c', code, str(source), pipe, key.hex(),
                                    str(os.getpid()), str(created), tempfile.gettempdir()],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        connection = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    _winapi.WaitNamedPipe(pipe, 100)
                    break
                except OSError:
                    time.sleep(.02)
            connection = Client(pipe, family='AF_PIPE', authkey=key)
            for name, args, expected in [('ufi.app.list', {}, None),
                                         ('ufi.settings.set', {'command': 'whoami'}, 'invalid_arguments'),
                                         ('ufi.shell', {'command': 'whoami'}, 'unknown_tool')]:
                connection.send_bytes(json.dumps({'name': name, 'arguments': args}).encode())
                self.assertTrue(connection.poll(5))
                result = json.loads(connection.recv_bytes())
                if expected:
                    self.assertEqual(result['error']['code'], expected)
                else:
                    self.assertEqual(result['result']['apps'], [])
            connection.close()
            connection = None
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual((process.returncode, stdout, stderr), (0, b'', b''))
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
