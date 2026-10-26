"""Actual Windows ACL regression for the 2.0.0 install/launch failure."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from test_v2_upgrade import UpgradeFixture
from install_bundle import BUNDLE_FILES, MANIFEST, install_bundle


@unittest.skipUnless(os.name == 'nt', 'Windows ACL test')
class WindowsPermissionsTests(UpgradeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.target.mkdir(parents=True)
        # Model Program Files: ordinary Users have inherited read/execute,
        # while the test owner can maintain its disposable fake app files.
        executable = Path(os.environ['SystemRoot']) / 'System32/icacls.exe'
        subprocess.run([str(executable), str(self.target), '/grant',
                        '*S-1-5-32-545:(OI)(CI)(RX)', '/Q'], capture_output=True, check=True)

    def assert_users_can_read_execute(self, names):
        quoted = ','.join("'" + str(self.target / name).replace("'", "''") + "'" for name in names)
        script = '''$files = @(__FILES__); @($files | ForEach-Object {
 $acl = Get-Acl -LiteralPath $_
 $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
 $allow = @($rules | Where-Object {
  $_.IdentityReference.Value -eq 'S-1-5-32-545' -and
  $_.AccessControlType -eq 'Allow' -and $_.IsInherited -and
  ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq
   [Security.AccessControl.FileSystemRights]::ReadAndExecute
 })
 @{name=[IO.Path]::GetFileName($_);users_read_execute=($allow.Count -gt 0)}
}) | ConvertTo-Json -Compress'''.replace('__FILES__', quoted)
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, check=True, timeout=15)
        rows = json.loads(result.stdout)
        if isinstance(rows, dict):
            rows = [rows]
        self.assertTrue(all(row['users_read_execute'] for row in rows), rows)

    def test_parent_read_execute_permissions_survive_staging_rename(self):
        install_bundle(self.source, self.target)
        self.assert_users_can_read_execute(BUNDLE_FILES + (MANIFEST,))

    def test_acl_setup_failure_does_not_replace_existing_executable(self):
        old = self.target / 'UFI-Fix.exe'
        old.write_bytes(b'old working app')
        with mock.patch('install_bundle.inherit_staging_permissions', side_effect=PermissionError('ACL denied')):
            with mock.patch('install_bundle.os.replace') as replace, self.assertRaises(OSError):
                install_bundle(self.source, self.target)
        replace.assert_not_called()
        self.assertEqual(old.read_bytes(), b'old working app')

    def test_rollback_keeps_standard_inherited_permissions(self):
        old = self.target / 'UFI-Fix.exe'
        old.write_bytes(b'old working app')
        original_replace = os.replace
        def replace(source, destination):
            if Path(destination).name == 'AI-BRIDGE.md':
                raise PermissionError('simulated replacement failure')
            return original_replace(source, destination)
        with mock.patch('install_bundle.os.replace', side_effect=replace), self.assertRaises(OSError):
            install_bundle(self.source, self.target)
        self.assertEqual(old.read_bytes(), b'old working app')
        self.assert_users_can_read_execute(['UFI-Fix.exe'])
