"""Regression coverage for issue #1 without launching or changing UFI."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock


@unittest.skipUnless(os.name == 'nt', 'UFI-Fix imports Windows APIs')
class PowerShellOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = Path(__file__).resolve().parents[1] / 'src'
        sys.path.insert(0, str(src))
        for attr, filename in (('app', 'UFI-Fix.py'), ('setup', 'UFI-Fix-Setup.py')):
            spec = importlib.util.spec_from_file_location('ufi_' + attr, src / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setattr(cls, attr, module)

    def setUp(self):
        # Diagnostics must stay in memory rather than change the user's log.
        self.log = mock.patch.object(self.app, 'log').start()
        self.addCleanup(mock.patch.stopall)

    def test_startup_survives_missing_shortcut_output(self):
        result = subprocess.CompletedProcess([], 0, stdout=None, stderr=None)
        with mock.patch.object(subprocess, 'run', return_value=result):
            self.assertEqual(self.app.ufi_shortcuts(r'C:\UFI'), ([], []))

    def test_both_wrappers_survive_missing_output(self):
        result = subprocess.CompletedProcess([], 0, stdout=None, stderr=None)
        with mock.patch.object(subprocess, 'run', return_value=result):
            for run in (self.app._ps, self.setup.ps):
                with self.subTest(run=run.__name__):
                    self.assertEqual(run('unused'), '')

    def test_empty_scan_is_safe(self):
        result = subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'')
        with mock.patch.object(subprocess, 'run', return_value=result):
            self.assertEqual(self.app.scan_shortcuts(None), [])
            self.assertEqual(self.setup.scan_ufi_shortcuts(r'C:\UFI'), [])

    def test_invalid_path_bytes_are_rejected(self):
        bad_row = b'C:\\bad\xff.lnk|C:\\UFI\\UFI.exe|/emmc\r\n'
        result = subprocess.CompletedProcess([], 0, stdout=bad_row, stderr=b'')
        with mock.patch.object(subprocess, 'run', return_value=result):
            self.assertEqual(self.app.scan_shortcuts(r'C:\UFI'), [])
            self.assertEqual(self.setup.scan_ufi_shortcuts(r'C:\UFI'), [])
        self.log.assert_called()

    def test_both_scanners_preserve_unicode_rows_and_pipe_in_arguments(self):
        path = 'C:\\Users\\\u067e\u0648\u0631\u06cc\u0627\\\u0423\u0424\u0418.lnk'
        target = 'D:\\\u062a\u0639\u0645\u06cc\u0631\\UFI.exe'
        args = '/emmc "a|b"'
        output = (path + '|' + target + '|' + args + '\r\n').encode('utf-8')
        result = subprocess.CompletedProcess([], 0, stdout=output, stderr=b'')
        with mock.patch.object(subprocess, 'run', return_value=result):
            expected = [(path, target, args)]
            self.assertEqual(self.app.scan_shortcuts(r'C:\UFI'), expected)
            self.assertEqual(self.setup.scan_ufi_shortcuts(r'C:\UFI'), expected)

    def test_failed_command_cannot_report_success_or_shortcuts(self):
        for output in (b'ok', b'C:\\UFI.lnk|C:\\UFI\\UFI.exe|/emmc'):
            result = subprocess.CompletedProcess([], 1, stdout=output, stderr=b'failed')
            with mock.patch.object(subprocess, 'run', return_value=result):
                self.assertEqual(self.app._ps('unused'), '')
                self.assertEqual(self.setup.ps('unused'), '')
                self.assertEqual(self.app.scan_shortcuts(r'C:\UFI'), [])
                self.assertEqual(self.setup.scan_ufi_shortcuts(r'C:\UFI'), [])

    def test_unavailable_powershell_is_safe(self):
        with mock.patch.object(subprocess, 'run', side_effect=FileNotFoundError('powershell')):
            self.assertEqual(self.app.scan_shortcuts(r'C:\UFI'), [])
            self.assertEqual(self.app._ps('unused'), '')
            self.assertEqual(self.setup.ps('unused'), '')

    def test_timeout_is_safe(self):
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('powershell', 1)):
            self.assertEqual(self.app.scan_shortcuts(r'C:\UFI'), [])
            self.assertEqual(self.app._ps('unused'), '')
            self.assertEqual(self.setup.ps('unused'), '')

    @unittest.skipUnless(shutil.which('powershell'), 'Windows PowerShell unavailable')
    def test_live_unicode_paths_survive_scan_and_installer_wrapper(self):
        row = ('C:\\Users\\' + '\u067e\u0648\u0631\u06cc\u0627' + '\\Desktop\\'
               + '\u0423\u0424\u0418' + '.lnk|D:\\' + '\u062a\u0639\u0645\u06cc\u0631'
               + r'\UFI.exe|/emmc "a|b"')
        # Only emit a synthetic row; no real shortcut is read or written.
        script = "[Console]::WriteLine('" + row.replace("'", "''") + "')"
        with mock.patch.object(self.app, 'PS_SCAN', script):
            expected = [tuple(row.split('|', 2))]
            self.assertEqual(self.app.scan_shortcuts(r"C:\O'Brien\UFI"), expected)
        # The installer shares the same output handling through its wrapper.
        self.assertEqual(self.setup.ps(script), row)

    @unittest.skipUnless(shutil.which('powershell'), 'Windows PowerShell unavailable')
    def test_live_invalid_bytes_do_not_crash_a_reader_thread(self):
        script = '$s = [Console]::OpenStandardOutput(); $s.WriteByte(255); $s.Flush()'
        self.assertEqual(self.app._ps(script), '')
        self.assertEqual(self.setup.ps(script), '')
        self.log.assert_called()

    @unittest.skipUnless(shutil.which('powershell'), 'Windows PowerShell unavailable')
    def test_live_confirmation_and_empty_output(self):
        for run in (self.app._ps, self.setup.ps):
            with self.subTest(run=run.__name__):
                self.assertEqual(run("'ok'"), 'ok')
                self.assertEqual(run('$null'), '')


if __name__ == '__main__':
    unittest.main()
