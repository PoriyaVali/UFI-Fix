"""Verified app-file upgrades; never overwrite vendor logs or support history."""
from datetime import datetime, timezone
import hashlib
import json
import ntpath
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import uuid

from app_version import VERSION

BUNDLE_FILES = ('UFI-Fix.exe', 'UFI-Bridge.exe', 'UFI-Control.exe', 'README.txt', 'AI-BRIDGE.md', 'UPDATE-POLICY.md',
                'ufi-bridge-tools.json', 'icon.ico', 'LICENSE', 'CONTRIBUTORS.md')
MANIFEST = 'package-manifest.json'


def inherit_staging_permissions(directory):
    """Use the install parent's ACL before creating files that will be moved.

    Windows tempfile directories are intentionally owner/admin-only. A rename
    retains their files' ACLs, so without this reset an elevated installation
    can produce files unreadable even to the same user's filtered desktop.
    Inherit the actual app-directory policy; never grant Everyone/write rights.
    """
    if os.name != 'nt':
        return
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'icacls.exe'
    try:
        result = subprocess.run([str(executable), str(directory), '/reset', '/Q'],
                                capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError('Cannot prepare inherited installation permissions: %s' % error) from error
    if result.returncode:
        details = (result.stderr or result.stdout or b'').decode(errors='replace').strip()
        raise OSError('Cannot prepare inherited installation permissions: ' + details)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_bundle(source):
    source = Path(source).resolve()
    data = json.loads((source / MANIFEST).read_text(encoding='utf-8'))
    if data.get('version') != VERSION or set(data.get('files', {})) != set(BUNDLE_FILES):
        raise ValueError('Installer payload version or file list is invalid.')
    for name in BUNDLE_FILES:
        path = source / name
        if path.resolve().parent != source or not path.is_file() or sha256(path) != data['files'][name]:
            raise ValueError('Installer payload checksum failed: ' + name)
    return data


def install_bundle(source, target):
    """Stage, back up and replace known app files; roll back a failed replace."""
    source, target = Path(source).resolve(), Path(target).resolve()
    validate_bundle(source)
    if target == Path(target.anchor) or target == source:
        raise ValueError('Choose a dedicated installation directory.')
    target.mkdir(parents=True, exist_ok=True)
    names = BUNDLE_FILES + (MANIFEST,)
    for name in names:
        path = target / name
        if path.is_symlink() or path.resolve().parent != target:
            raise ValueError('Installation target redirects outside its directory: ' + name)
        if path.exists() and not path.is_file():
            raise ValueError('Installation target is not a file: ' + name)
    prior = [name for name in names if (target / name).is_file()]
    backup = None
    if prior:
        label = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        backup = target / 'backups' / ('before-' + VERSION + '-' + label)
        if (target / 'backups').is_symlink() or not backup.resolve().is_relative_to(target):
            raise ValueError('Backup directory redirects outside the installation.')
        backup.mkdir(parents=True)
        for name in prior:
            shutil.copy2(target / name, backup / name)
    # TemporaryDirectory owns only this newly created staging directory.
    with tempfile.TemporaryDirectory(prefix='.ufi-upgrade-', dir=target) as directory:
        staging = Path(directory)
        if staging.resolve().parent != target:
            raise ValueError('Staging directory redirects outside the installation.')
        inherit_staging_permissions(staging)
        for name in names:
            shutil.copy2(source / name, staging / name)
        validate_bundle(staging)
        replaced = []
        try:
            for name in names:
                os.replace(staging / name, target / name)
                replaced.append(name)
        except OSError as original_error:
            rollback_errors = []
            for name in reversed(replaced):
                try:
                    if name in prior:
                        shutil.copy2(backup / name, staging / name)
                        os.replace(staging / name, target / name)
                    else:
                        (target / name).unlink()
                except OSError as error:
                    rollback_errors.append(name + ': ' + str(error))
            if rollback_errors:
                raise OSError('Upgrade failed and rollback needs attention. Backup: %s; %s'
                              % (backup, '; '.join(rollback_errors))) from original_error
            raise OSError('Upgrade was rolled back. Close UFI-Fix and UFI-Bridge before retrying. %s'
                          % original_error) from original_error
    return backup


def save_config(config_directory, ufi_directory, executable):
    directory = Path(config_directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'config.json'
    previous = {}
    if path.is_file():
        backup = directory / ('config.before-' + VERSION + '-' + uuid.uuid4().hex[:8] + '.json')
        shutil.copy2(path, backup)
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            previous = {}
    if not isinstance(previous, dict):
        previous = {}
    previous.update(ufi_dir=str(ufi_directory or ''), tool=str(executable))
    temporary = directory / ('.config-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(previous, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_shortcut_path(value):
    value = ntpath.normpath(str(value or ''))
    if os.name == 'nt' and ntpath.isabs(value) and not value.startswith('\\\\'):
        # Shell links expose long paths even when TEMP/config uses an 8.3 alias.
        value = os.path.realpath(value)
    return ntpath.normcase(value)


def should_relink(ufi_directory, target, arguments, tool, previous_tools=()):
    normalize = normalize_shortcut_path
    if normalize(target or '') == normalize(ntpath.join(ufi_directory, 'UFI.exe')):
        return True
    known = {normalize(tool), normalize(ntpath.join(ufi_directory, 'fix', 'UFI-Fix.exe'))}
    known.update(normalize(path) for path in previous_tools if path)
    return (normalize(target or '') in known
            and (arguments or '').strip().lower() in ('', '/emmc', '/ufs', '/android', '/chip'))


def connection_config(executable, ufi_directory):
    return {'mcpServers': {'ufi-support': {'command': str(executable),
                                          'args': ['--ufi-dir', str(ufi_directory)]}}}
