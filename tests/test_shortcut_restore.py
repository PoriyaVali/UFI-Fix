"""Real Windows shortcut regressions confined to owned temporary fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


@unittest.skipUnless(os.name == 'nt', 'Windows shell links')
class ShortcutRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'src'
        sys.path.insert(0, str(source))
        spec = importlib.util.spec_from_file_location('shortcut_restore_regression', source / 'UFI-Fix.py')
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ufi-undo-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = (Path(self.temporary.name) / "UFI EXPECTED_ARGS ' [case]").resolve()
        self.root.mkdir()
        self.vendor = self.root / 'UFI.exe'
        self.tool = self.root / 'UFI-Fix.exe'
        self.vendor.touch()  # Identity fixture; never execute either file/link.
        self.tool.touch()
        self.messages = []
        for patch in (
            mock.patch.object(self.app, 'load_config', return_value={'tool':str(self.tool)}),
            mock.patch.object(self.app, 'log', side_effect=lambda text: '2000-01-01 ' + text),
            mock.patch.object(self.app, 'PS_SCAN', r'''
$sh=New-Object -ComObject WScript.Shell
foreach($d in @('UFIDIR','UFIDIR')) {
 Get-ChildItem -LiteralPath $d -Filter *.lnk -File | ForEach-Object {
  $l=$sh.CreateShortcut($_.FullName)
  "$($_.FullName)|$($l.TargetPath)|$($l.Arguments)"
 }
}
'__UFI_SHORTCUT_SCAN_COMPLETE__'
'''),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def shortcut(self, filename, target=None, args='/emmc'):
        path = self.root / filename
        script = ('$sh=New-Object -ComObject WScript.Shell;$l=$sh.CreateShortcut(%s);'
                  '$l.TargetPath=%s;$l.Arguments=%s;$l.WorkingDirectory=%s;$l.Save();"ok"' % (
                      self.app._q(str(path)), self.app._q(str(target or self.tool)),
                      self.app._q(args), self.app._q(str(self.root))))
        self.assertEqual(self.app._ps(script), 'ok')
        return path

    def target(self, path):
        data = self.app._ps('$sh=New-Object -ComObject WScript.Shell;$l=$sh.CreateShortcut(%s);'
                            '@{target=$l.TargetPath;arguments=$l.Arguments}|ConvertTo-Json -Compress' % self.app._q(str(path)))
        return json.loads(data)

    def undo(self):
        return self.app.restore_shortcuts(str(self.root), str(self.tool), self.messages.append)

    def test_copied_shortcut_without_backup_returns_to_vendor_and_is_idempotent(self):
        copied = self.shortcut('Copied toolbox.lnk')
        report = self.undo()
        self.assertTrue(report['success'], self.messages)
        self.assertEqual((report['repaired'], report['remaining']), (1, 0))
        self.assertEqual(self.target(copied), {'target':str(self.vendor),'arguments':'/emmc'})
        second = self.undo()
        self.assertTrue(second['success'])
        self.assertEqual((second['restored'],second['repaired'],second['already_original']),(0,0,1))

    def test_short_path_alias_for_vendor_and_launcher_is_supported(self):
        import ctypes
        current=self.shortcut('Short alias.lnk')
        def short(path):
            output=ctypes.create_unicode_buffer(32768)
            self.assertTrue(ctypes.windll.kernel32.GetShortPathNameW(str(path),output,len(output)))
            return output.value
        result=self.app.restore_shortcuts(short(self.root),short(self.tool),self.messages.append)
        self.assertTrue(result['success'],self.messages)
        self.assertEqual(result['repaired'],1)
        self.assertEqual(self.target(current)['target'],str(self.vendor))

    def test_verified_original_backup_is_restored_and_preserved(self):
        current = self.shortcut('Original toolbox.lnk', self.vendor, '/ufs')
        backup = Path(str(current)+'.orig')
        shutil.copy2(current, backup)
        original = backup.read_bytes()
        self.shortcut(current.name, self.tool, '/ufs')
        report = self.undo()
        self.assertTrue(report['success'], self.messages)
        self.assertEqual((report['restored'],report['repaired']),(1,0))
        self.assertEqual(current.read_bytes(),original)
        self.assertEqual(backup.read_bytes(),original)

    def test_wrapper_backup_is_not_mistaken_for_original(self):
        current = self.shortcut('Wrong backup.lnk', self.tool, '/chip')
        backup = Path(str(current)+'.orig')
        shutil.copy2(current, backup)
        original = backup.read_bytes()
        report = self.undo()
        self.assertTrue(report['success'],self.messages)
        self.assertEqual(report['repaired'],1)
        self.assertEqual(self.target(current)['target'],str(self.vendor))
        self.assertEqual(backup.read_bytes(),original)

    def test_backup_for_another_toolbox_does_not_change_module(self):
        current = self.shortcut('Mixed modules.lnk', self.vendor, '/android')
        backup = Path(str(current)+'.orig')
        shutil.copy2(current, backup)
        self.shortcut(current.name,self.tool,'/emmc')
        report=self.undo()
        self.assertTrue(report['success'],self.messages)
        self.assertEqual(report['repaired'],1)
        self.assertEqual(self.target(current)['arguments'],'/emmc')

    def test_manager_and_unrelated_links_and_their_backups_are_unchanged(self):
        manager = self.shortcut('UFI-Fix manager.lnk', self.tool, '')
        unrelated = self.shortcut('Unrelated.lnk', self.root/'other'/'UFI-Fix.exe', '/ufs')
        backup=Path(str(unrelated)+'.orig');shutil.copy2(unrelated,backup)
        before={p:p.read_bytes() for p in (manager,unrelated,backup)}
        report=self.undo()
        self.assertTrue(report['success'],self.messages)
        self.assertEqual(report['restored']+report['repaired'],0)
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_failed_mutation_and_remaining_wrapper_never_report_success(self):
        current=self.shortcut('Blocked.lnk')
        original=self.app._ps
        def failure(script):
            return '' if 'UFI_RESTORE_LINK_V1' in script else original(script)
        with mock.patch.object(self.app,'_ps',side_effect=failure):report=self.undo()
        self.assertFalse(report['success'])
        self.assertEqual((report['failed'],report['remaining']),(1,1))
        self.assertEqual(self.target(current)['target'],str(self.tool))

    def test_missing_scan_completion_cannot_be_treated_as_no_links(self):
        with mock.patch.object(self.app,'run_powershell',return_value=''):
            report=self.undo()
        self.assertFalse(report['success'])
        self.assertIsNone(report['remaining'])

    def test_gui_and_cli_do_not_claim_success_after_partial_undo(self):
        failed={'success':False,'remaining':1,'failed':1}
        gui=self.app.Gui.__new__(self.app.Gui)
        gui.ufi_dir=str(self.root);gui._tool_path=lambda:str(self.tool)
        gui.messagebox=mock.Mock();gui.messagebox.askyesno.return_value=True
        gui.say=self.messages.append
        with mock.patch.object(self.app,'restore_shortcuts',return_value=failed):
            gui.do_restore()
            gui.messagebox.showwarning.assert_called_once()
            self.assertFalse(any('Verified:' in line for line in self.messages))
            with mock.patch.object(sys,'argv',['UFI-Fix.exe','--restore']),mock.patch.object(self.app,'find_ufi_dir',return_value=(str(self.root),'fixture')):
                self.assertEqual(self.app.main(),1)

    def test_uninstall_aborts_before_removing_files_if_undo_failed(self):
        with (mock.patch.object(self.app,'find_ufi_dir',return_value=(str(self.root),'fixture')),
              mock.patch.object(self.app,'restore_shortcuts',return_value={'success':False}),
              mock.patch.object(self.app.os,'remove') as remove,
              mock.patch.object(self.app.subprocess,'Popen') as launch,
              mock.patch.object(self.app.ctypes.windll.user32,'MessageBoxW',return_value=0)):
            self.assertEqual(self.app.remove_everything(),1)
            remove.assert_not_called();launch.assert_not_called()
