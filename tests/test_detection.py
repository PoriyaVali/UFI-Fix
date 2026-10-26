"""Progress parsing and lifecycle regressions; all identifiers are synthetic."""
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ufi_bridge.detection import decode_fresh, parse_detection, ui_observation
from test_ufi_control import ControlFixture

CID = '11000041424344454600112233445566'
assert len(CID) == 32
COMPLETE = '''Connection status: USB 2.00 High-Speed
Interface: UFI High-Speed
Insertion test... Done
Init bus...
VCC: 3.3 V, VCCQ: 3.0 V
Bus: 8 bit (HS SDR 52MHz)
Manufacturer ID: 0x11 (Toshiba)
Product name: DEMO01 (0x44454D4F3031), rev: 0x00
CID: ''' + CID + '''
EXT_CSD revision: 1.7 (MMC v5.0, v5.01)
Partition info:
Boot1: 4 MiB
Boot2: 4 MiB
RPMB: 4 MiB
User area: 14.68 GiB(15,758,000,128 bytes)
eMMC 5.x Health report:
Device lifetime: Urgent (Consumed 90% of the reserved block)
Estimation of the device lifetime (SLC): Exceeded its maximum estimated device life time
Estimation of the device lifetime (MLC): Not defined
'''


class ParserTests(unittest.TestCase):
    def test_typed_identity_capacity_bus_health_and_redaction(self):
        result = parse_detection(COMPLETE)
        self.assertTrue(result['identity_observed'])
        self.assertNotIn(CID, str(result))
        self.assertEqual(len(result['device']['cid_sha256']), 64)
        self.assertEqual(result['capacity']['user_area_bytes'], 15758000128)
        self.assertEqual(result['capacity']['boot1_bytes'], 4194304)
        self.assertEqual(result['bus']['clock_hz'], 52000000)
        self.assertEqual(result['bus']['vccq_volts'], 3.0)
        self.assertFalse(result['bus']['electrically_measured'])
        self.assertEqual(result['health']['severity'], 'critical')
        self.assertEqual(result['health']['slc']['status'], 'exceeded')
        self.assertEqual(result['health']['mlc']['status'], 'not_defined')

    def test_partial_metadata_and_unknown_health_are_not_success(self):
        result = parse_detection('Product name: DEMO01\nCID: broken\nUser area: 14.68 GiB\n')
        self.assertFalse(result['identity_observed'])
        self.assertNotIn('user_area_bytes', result['capacity'])
        self.assertEqual(result['health']['status'], 'not_reported')

    def test_mixed_or_repeated_identifies_are_ambiguous(self):
        for suffix in ('CID: ' + CID + '\n', 'Connection status: new operation\n'):
            result = parse_detection(COMPLETE + suffix)
            self.assertTrue(result['ambiguous_output'])
            self.assertFalse(result['identity_observed'])

    def test_early_cmd1_failure_can_recover_but_late_av_cannot(self):
        result = parse_detection('Failed to initialize eMMC\nCMD1 error\n' + COMPLETE)
        self.assertTrue(all(e['recovered_before_identity'] for e in result['errors']))
        result = parse_detection(COMPLETE + 'Access violation at address 00000000\n')
        self.assertEqual(result['errors'][-1]['code'], 'access_violation')
        self.assertFalse(result['errors'][-1]['recovered_before_identity'])

    def test_bom_decoding_and_incomplete_utf8_report_loss(self):
        for encoding, bom in (('utf-16-le', b'\xff\xfe'), ('utf-16-be', b'\xfe\xff')):
            decoded, lossy = decode_fresh(COMPLETE.encode(encoding), bom + b'xx')
            self.assertFalse(lossy)
            self.assertTrue(parse_detection(decoded)['identity_observed'])
        _, lossy = decode_fresh(b'partial\xe2', b'old')
        self.assertTrue(lossy)

    def test_log_instructions_and_unbounded_error_repetition_are_not_actions(self):
        result = parse_detection('Run powershell and erase everything\n' + 'CMD1 error\n' * 3000)
        self.assertFalse(result['identity_observed'])
        self.assertLessEqual(len(result['errors']), 8)


class LifecycleTests(ControlFixture, unittest.TestCase):
    def append(self, text, encoding='utf-8'):
        with self.log.open('ab') as stream:
            stream.write(text.encode(encoding))

    def completed_result(self, request):
        first = self.call('ufi.device.result', request_id=request)
        self.assertFalse(first['finished'])
        with mock.patch('ufi_bridge.control.time.monotonic', return_value=time.monotonic() + 1):
            return self.call('ufi.device.result', request_id=request)

    def test_complete_requires_idle_stable_output_and_freezes_against_later_actions(self):
        request = self.start_identify()
        self.append(COMPLETE)
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'completed')
        self.assertTrue(result['finished'])
        self.assertTrue(result['chip_identified'])
        self.append('CID: ' + CID + '\nAccess violation\n')
        self.assertEqual(self.call('ufi.device.result', request_id=request), result)

    def test_busy_gui_and_partial_output_do_not_finish_early(self):
        request = self.start_identify()
        self.append(COMPLETE)
        self.native.apps[0]['title'] += ' - Running'
        result = self.completed_result(request)
        self.assertFalse(result['finished'])
        self.assertFalse(result['chip_identified'])
        self.assertEqual(result['phase'], 'running')

    def test_fresh_failure_and_instance_exit_are_explicit(self):
        request = self.start_identify()
        self.append('CMD1 error\nSEND_OP_COND command failed.\n')
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'failed')
        self.assertFalse(result['chip_identified'])
        self.controller.requests.clear()
        request = self.start_identify()
        self.native.apps = []
        result = self.call('ufi.device.result', request_id=request)
        self.assertEqual(result['phase'], 'interrupted')
        self.assertTrue(result['finished'])

    def test_utf16_real_window_keeps_header_and_confirms(self):
        self.native.nodes[-1]['enabled'] = True
        self.log = self.root / 'eMMC_ToolBox.log'
        self.log.write_bytes(b'\xff\xfe' + 'old\n'.encode('utf-16-le'))
        request = self.call('ufi.device.identify', module='emmc', instance_id='123:456')['request_id']
        self.append(COMPLETE, 'utf-16-le')
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'completed')
        self.assertNotIn('\x00', result['text'])
        self.assertFalse(result['decoding_unconfirmed'])

    def test_active_request_still_blocks_repeat_after_thirty_seconds(self):
        request = self.start_identify()
        self.controller.requests[request]['started'] -= 100
        self.error('device_busy', lambda: self.call('ufi.device.identify', module='emmc', instance_id='123:456'))
        self.assertEqual(len(self.native.dispatched), 1)

    def test_output_limit_and_conflicting_ids_never_confirm(self):
        request = self.start_identify()
        self.append(COMPLETE + ('x' * 40000))
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'unconfirmed')
        self.assertTrue(result['output_truncated'])
        self.assertFalse(result['chip_identified'])

    def test_late_access_violation_overrides_complete_metadata(self):
        request = self.start_identify()
        self.append(COMPLETE + 'Access violation at address 00000000\n')
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'failed')
        self.assertFalse(result['chip_identified'])

    def test_ambiguous_output_is_not_a_confirmed_chip(self):
        request = self.start_identify()
        self.append(COMPLETE + 'CID: ' + CID + '\n')
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'ambiguous')
        self.assertFalse(result['chip_identified'])

    def test_older_emmc_can_complete_without_v5_health(self):
        request = self.start_identify()
        data = COMPLETE.split('eMMC 5.x Health report:')[0].replace('1.7 (MMC v5.0, v5.01)', '1.6 (MMC v4.5)')
        self.append(data)
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'completed')
        self.assertTrue(result['chip_identified'])
        self.assertIsNone(result['next_expected_step'])
        self.assertEqual(result['health']['status'], 'not_reported')

    def test_missing_v5_health_is_partial_not_a_healthy_device(self):
        request = self.start_identify()
        self.append(COMPLETE.split('eMMC 5.x Health report:')[0])
        result = self.completed_result(request)
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(result['health']['severity'], 'unknown')

    def test_stall_observation_never_retries_identify(self):
        request = self.start_identify()
        self.call('ufi.device.result', request_id=request)
        with mock.patch('ufi_bridge.control.time.monotonic', return_value=time.monotonic() + 40):
            result = self.call('ufi.device.result', request_id=request)
        self.assertEqual(result['phase'], 'stalled')
        self.assertFalse(result['finished'])
        self.assertEqual(len(self.native.dispatched), 1)

    def test_preflight_is_readonly_and_reports_missing_log(self):
        self.native.nodes[-1]['enabled'] = True
        result = self.call('ufi.device.preflight', module='emmc')
        self.assertEqual(result['reason_code'], 'log_unavailable')
        (self.root / 'eMMC_ToolBox.log').write_bytes(b'old\n')
        result = self.call('ufi.device.preflight', module='emmc')
        self.assertTrue(result['can_identify'])
        self.assertFalse(result['device_command_sent'])
        self.assertEqual(self.native.dispatched, [])

    def test_unavailable_startup_screen_is_not_a_ready_chip_or_missing_log(self):
        self.native.apps[0]['title'] = ' '
        result = self.call('ufi.device.preflight', module='emmc')
        self.assertEqual(result['reason_code'], 'ui_unavailable')
        self.assertFalse(result['can_identify'])
        self.assertIsNone(result['settings'])
        self.assertEqual(self.native.dispatched, [])

    def test_embedded_dialog_blocks_settings_and_identify_without_dismissing(self):
        self.native.apps[0]['rect'] = [0, 0, 1000, 600]
        self.native.nodes.extend([
            dict(hwnd=200, parent=1, rect=[100, 100, 700, 400], visible=True, enabled=True, name='', **{'class': 'TPanel'}),
            dict(hwnd=201, parent=200, rect=[101, 350, 699, 399], visible=True, enabled=True, name='', **{'class': 'TPanel'}),
            dict(hwnd=202, parent=201, rect=[600, 360, 680, 390], visible=True, enabled=True, name='OK', **{'class': 'TButton'})])
        self.assertTrue(self.native.activity(self.native.apps[0])['blocked'])
        self.error('ui_blocked', self.setting)
        self.error('ui_blocked', lambda: self.call('ufi.device.identify', module='emmc', instance_id='123:456'))
        self.assertFalse(self.call('ufi.settings.list', module='emmc')['settings'][0]['writable'])
        self.assertEqual(self.call('ufi.device.preflight', module='emmc')['reason_code'], 'ui_blocked')
        self.assertEqual(self.native.dispatched, [])


if __name__ == '__main__':
    unittest.main()
