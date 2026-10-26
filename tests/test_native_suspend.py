"""Real Windows suspend-right regression plus fail-before-write checks."""
import ctypes
from ctypes import wintypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


@unittest.skipUnless(os.name == 'nt', 'Windows process access rights')
class SuspendTests(unittest.TestCase):
    def setUp(self):
        permission = mock.patch.object(self.app.UPDATE_GATE, 'require_current')
        permission.start()
        self.addCleanup(permission.stop)
        logging = mock.patch.object(self.app, 'log', side_effect=lambda text: '2000-01-01 ' + text)
        logging.start()
        self.addCleanup(logging.stop)

    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'src'
        sys.path.insert(0, str(source))
        spec = importlib.util.spec_from_file_location('ufi_suspend_regression', source / 'UFI-Fix.py')
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)

    def test_real_old_handle_denied_and_new_handle_suspends_and_resumes_own_child(self):
        child = subprocess.Popen([sys.executable, '-c',
                                  "import sys; print('ready',flush=True); sys.stdin.readline(); print('resumed',flush=True)"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        target, suspended = None, False
        try:
            self.assertEqual(child.stdout.readline().strip(), b'ready')
            old = self.app.k32.OpenProcess(0x438, False, child.pid)
            self.assertTrue(old)
            try:
                status = self.app.ntdll.NtSuspendProcess(wintypes.HANDLE(old))
                self.assertEqual(status & 0xffffffff, 0xc0000022)  # ACCESS_DENIED
            finally:
                self.app.k32.CloseHandle(wintypes.HANDLE(old))
            target = self.app.Target(child.pid)
            target.suspend()
            suspended = True
            target.resume()
            suspended = False
            stdout, stderr = child.communicate(b'go\n', timeout=5)
            self.assertEqual((child.returncode, stdout.strip(), stderr), (0, b'resumed', b''))
        finally:
            if suspended:
                target.resume()
            if target:
                self.app.k32.CloseHandle(wintypes.HANDLE(target.h))
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)

    def test_suspend_failure_stops_before_module_reads_and_never_calls_resume(self):
        target = mock.Mock()
        target.h = 123
        target.alive.return_value = True
        target.suspend.side_effect = OSError('ACCESS_DENIED')
        with mock.patch.object(self.app.os.path, 'exists', return_value=True), \
             mock.patch.object(self.app.subprocess, 'Popen', return_value=mock.Mock(pid=42)), \
             mock.patch.object(self.app, 'ufi_processes', return_value={43: 42}), \
             mock.patch.object(self.app, 'Target', return_value=target), \
             mock.patch.object(self.app, 'patch_module') as patch, \
             mock.patch.object(self.app, 'log', return_value='test'), \
             mock.patch.object(self.app.k32, 'CloseHandle'):
            self.assertEqual(self.app.run_ufi('test-only', '/emmc'), 1)
        target.modules.assert_not_called()
        target.resume.assert_not_called()
        patch.assert_not_called()

    def test_snapshot_and_patch_failure_still_resume(self):
        target = mock.Mock()
        target.h = 123
        target.alive.return_value = True
        target.modules.side_effect = OSError('read failed')
        with mock.patch.object(self.app.os.path, 'exists', return_value=True), \
             mock.patch.object(self.app.subprocess, 'Popen', return_value=mock.Mock(pid=42)), \
             mock.patch.object(self.app, 'ufi_processes', return_value={43: 42}), \
             mock.patch.object(self.app, 'Target', return_value=target), \
             mock.patch.object(self.app, 'log', return_value='test'), \
             mock.patch.object(self.app.k32, 'CloseHandle'):
            self.assertEqual(self.app.run_ufi('test-only', '/emmc'), 1)
        target.suspend.assert_called_once()
        target.resume.assert_called_once()


if __name__ == '__main__':
    unittest.main()
