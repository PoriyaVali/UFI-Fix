import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from app_version import VERSION
from install_bundle import (BUNDLE_FILES, MANIFEST, connection_config, install_bundle,
                            save_config, should_relink, validate_bundle)


class UpgradeFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'bundle'
        self.source.mkdir()
        self.target = self.root / 'installed' / 'UFI-Fix'
        checksums = {}
        for name in BUNDLE_FILES:
            payload = ('version2-' + name).encode()
            (self.source / name).write_bytes(payload)
            checksums[name] = hashlib.sha256(payload).hexdigest()
        (self.source / MANIFEST).write_text(json.dumps({'version': VERSION, 'files': checksums}), encoding='utf-8')


class UpgradeTests(UpgradeFixture, unittest.TestCase):
    def test_clean_install_copies_both_executables_and_exact_bundle(self):
        self.assertIsNone(install_bundle(self.source, self.target))
        validate_bundle(self.target)
        for name in BUNDLE_FILES + (MANIFEST,):
            self.assertEqual((self.source / name).read_bytes(), (self.target / name).read_bytes())

    def test_upgrade_backs_up_previous_files_and_preserves_unrelated_content(self):
        self.target.mkdir(parents=True)
        (self.target / 'UFI-Fix.exe').write_bytes(b'version1')
        (self.target / 'my-notes.txt').write_text('keep', encoding='utf-8')
        history = self.root / 'user-data' / 'support.sqlite3'
        history.parent.mkdir()
        history.write_bytes(b'support history')
        backup = install_bundle(self.source, self.target)
        self.assertEqual((backup / 'UFI-Fix.exe').read_bytes(), b'version1')
        self.assertEqual((self.target / 'my-notes.txt').read_text(), 'keep')
        self.assertEqual(history.read_bytes(), b'support history')
        validate_bundle(self.target)

    def test_tampered_package_aborts_before_installation(self):
        (self.source / 'UFI-Bridge.exe').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            install_bundle(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_failure_partway_through_upgrade_restores_old_bundle(self):
        self.target.mkdir(parents=True)
        (self.target / 'UFI-Fix.exe').write_bytes(b'version1')
        (self.target / 'README.txt').write_bytes(b'old readme')
        original_replace = os.replace
        def replace(source, target):
            if Path(target).name == 'AI-BRIDGE.md':
                raise PermissionError('simulated locked file')
            return original_replace(source, target)
        with mock.patch('install_bundle.os.replace', side_effect=replace), self.assertRaises(OSError) as caught:
            install_bundle(self.source, self.target)
        self.assertIn('rolled back', str(caught.exception))
        self.assertEqual((self.target / 'UFI-Fix.exe').read_bytes(), b'version1')
        self.assertEqual((self.target / 'README.txt').read_bytes(), b'old readme')
        self.assertFalse((self.target / 'UFI-Bridge.exe').exists())
        self.assertFalse((self.target / MANIFEST).exists())

    def test_locked_first_file_leaves_previous_executable_unchanged(self):
        self.target.mkdir(parents=True)
        (self.target / 'UFI-Fix.exe').write_bytes(b'version1')
        with mock.patch('install_bundle.os.replace', side_effect=PermissionError('locked')), self.assertRaises(OSError):
            install_bundle(self.source, self.target)
        self.assertEqual((self.target / 'UFI-Fix.exe').read_bytes(), b'version1')

    def test_config_upgrade_keeps_existing_preferences_and_original_bytes(self):
        directory = self.root / 'user-data'
        directory.mkdir()
        original = b'{"ufi_dir":"C:\\\\UFI", "custom_setting":42}'
        path = directory / 'config.json'
        path.write_bytes(original)
        history = directory / 'support.sqlite3'
        history.write_bytes(b'history')
        save_config(directory, r'D:\UFI', r'C:\Program Files\UFI-Fix\UFI-Fix.exe')
        config = json.loads(path.read_text())
        self.assertEqual(config['custom_setting'], 42)
        self.assertEqual(config['ufi_dir'], r'D:\UFI')
        self.assertEqual(next(directory.glob('config.before-*.json')).read_bytes(), original)
        self.assertEqual(history.read_bytes(), b'history')

    def test_malformed_old_config_is_preserved_and_replaced_with_valid_json(self):
        directory = self.root / 'user-data'
        directory.mkdir()
        path = directory / 'config.json'
        path.write_bytes(b'invalid version1 config')
        save_config(directory, r'C:\UFI', r'C:\UFI-Fix\UFI-Fix.exe')
        self.assertEqual(json.loads(path.read_text())['ufi_dir'], r'C:\UFI')
        self.assertEqual(next(directory.glob('config.before-*.json')).read_bytes(), b'invalid version1 config')

    def test_shortcuts_upgrade_only_known_application_targets(self):
        args = (r'C:\UFI', r'C:\Program Files\UFI-Fix\UFI-Fix.exe')
        self.assertTrue(should_relink(args[0], r'C:\UFI\UFI.exe', '/emmc', args[1]))
        self.assertTrue(should_relink(args[0], r'C:\UFI\fix\UFI-Fix.exe', '/ufs', args[1]))
        self.assertTrue(should_relink(args[0], r'D:\old\UFI-Fix.exe', '/android', args[1], [r'D:\old\UFI-Fix.exe']))
        self.assertFalse(should_relink(args[0], r'D:\unrelated\UFI-Fix.exe', '/ufs', args[1]))
        self.assertFalse(should_relink(args[0], r'C:\UFI\fix\UFI-Fix.exe', '--uninstall', args[1]))
        self.assertFalse(should_relink(args[0], r'D:\OtherUFI\UFI.exe', '/emmc', args[1]))

    def test_connection_config_preserves_unicode_and_spaces(self):
        executable = 'C:\\Users\\\u067e\u0648\u0631\u06cc\u0627\\UFI Fix\\UFI-Bridge.exe'
        result = json.loads(json.dumps(connection_config(executable, r'D:\UFI')))
        self.assertEqual(result['mcpServers']['ufi-support']['command'], executable)

    def test_version_is_shared_by_bridge(self):
        from ufi_bridge import __version__
        from app_version import VERSION_TUPLE
        self.assertEqual(tuple(map(int, VERSION.split('.'))), VERSION_TUPLE[:3])
        self.assertEqual(__version__, VERSION)


if __name__ == '__main__':
    unittest.main()
