"""Build a versioned local test candidate without installing or launching UFI."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from app_version import BUILD_CHANNEL, VERSION, VERSION_TUPLE
from install_bundle import BUNDLE_FILES, MANIFEST, validate_bundle

BUILD = ROOT / 'build' / ('v' + VERSION)
OUTPUT = ROOT / 'dist' / ('v' + VERSION)
BIN = BUILD / 'bin'


def run(arguments, log):
    with log.open('wb') as stream:
        result = subprocess.run(arguments, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError('Build/check failed; see ' + str(log))


def resource(name):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)
    info = VSVersionInfo(ffi=FixedFileInfo(filevers=VERSION_TUPLE, prodvers=VERSION_TUPLE,
                                         flags=2, OS=0x40004, fileType=1), kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'UFI-Fix community project'),
            StringStruct('FileDescription', name + ' - test candidate'),
            StringStruct('FileVersion', VERSION), StringStruct('ProductName', 'UFI-Fix'),
            StringStruct('ProductVersion', VERSION), StringStruct('OriginalFilename', name + '.exe'),
            StringStruct('Comments', 'Local candidate; hardware acceptance testing pending.')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
    path = BUILD / (name + '-version.txt')
    path.write_text(str(info), encoding='utf-8')
    return path


def build(name, entry, extra, console=False, admin=False):
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile',
               '--console' if console else '--noconsole', '--name', name,
               '--distpath', str(BIN), '--workpath', str(BUILD / 'work' / name),
               '--specpath', str(BUILD / 'spec'), '--version-file', str(resource(name)),
               '--icon', str(ROOT / 'assets/icon.ico'),
               '--exclude-module', 'PIL', '--exclude-module', 'numpy', '--exclude-module', 'cv2']
    if admin:
        command.append('--uac-admin')
    command += extra + [str(ROOT / entry)]
    run(command, BUILD / (name + '-build.log'))


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / 'build-status.json').write_text(json.dumps({'status': 'building', 'version': VERSION}), encoding='utf-8')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], BUILD / 'automated-checks.log')
    run([sys.executable, '-m', 'compileall', '-q', 'src', 'tests', 'tools'], BUILD / 'compile.log')
    print('Automated checks passed; building four executables.', flush=True)
    build('UFI-Fix', 'src/UFI-Fix.py', [
        '--add-data', str(ROOT / 'assets/icon.ico') + ';.',
        '--add-data', str(ROOT / 'assets/tron.png') + ';.', '--hidden-import', 'qrcode'], admin=True)
    build('UFI-Bridge', 'src/UFI-Bridge.py', [], console=True)
    build('UFI-Control', 'src/UFI-Control.py', [], admin=True)
    payload = BUILD / 'payload'
    payload.mkdir(parents=True, exist_ok=True)
    inputs = {'UFI-Fix.exe': BIN / 'UFI-Fix.exe', 'UFI-Bridge.exe': BIN / 'UFI-Bridge.exe',
              'UFI-Control.exe': BIN / 'UFI-Control.exe',
              'README.txt': ROOT / 'README.txt', 'AI-BRIDGE.md': ROOT / 'docs/AI-BRIDGE.md',
              'ufi-bridge-tools.json': ROOT / 'docs/ufi-bridge-tools.json',
              'icon.ico': ROOT / 'assets/icon.ico', 'LICENSE': ROOT / 'LICENSE',
              'CONTRIBUTORS.md': ROOT / 'CONTRIBUTORS.md'}
    assert set(inputs) == set(BUNDLE_FILES)
    for name, path in inputs.items():
        shutil.copy2(path, payload / name)
    manifest = {'version': VERSION, 'build_channel': BUILD_CHANNEL,
                'files': {name: checksum(payload / name) for name in BUNDLE_FILES}}
    (payload / MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    validate_bundle(payload)
    build('UFI-Fix-Setup', 'src/UFI-Fix-Setup.py', ['--add-data', str(payload) + ';.'], admin=True)
    for name in ('UFI-Fix.exe', 'UFI-Bridge.exe', 'UFI-Control.exe'):
        shutil.copy2(BIN / name, OUTPUT / name)
    installer = OUTPUT / ('UFI-Fix-v' + VERSION + '-Setup.exe')
    shutil.copy2(BIN / 'UFI-Fix-Setup.exe', installer)
    portable = OUTPUT / ('UFI-Fix-v' + VERSION + '-portable.zip')
    with zipfile.ZipFile(portable, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in BUNDLE_FILES + (MANIFEST,):
            archive.write(payload / name, 'UFI-Fix/' + name)
    artifacts = [installer, portable, OUTPUT / 'UFI-Fix.exe', OUTPUT / 'UFI-Bridge.exe', OUTPUT / 'UFI-Control.exe']
    source_status = subprocess.check_output(
        ['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT).decode('utf-8')
    report = {'version': VERSION, 'build_channel': BUILD_CHANNEL,
              'git_base': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
              'source_state': 'modified worktree' if source_status.strip() else 'clean checkout',
              'hardware_acceptance_test': 'not performed by the build script; see docs/V2-VALIDATION.md',
              'live_installation': 'not performed', 'full_ufi_control': False,
              'build_environment': {name: importlib.metadata.version(name)
                                    for name in ('pyinstaller', 'capstone', 'qrcode', 'pillow')},
              'artifacts': [{'name': p.name, 'bytes': p.stat().st_size, 'sha256': checksum(p)} for p in artifacts]}
    import capstone
    report['build_environment']['capstone_runtime_bindings'] = capstone.__version__
    report['build_environment']['capstone_runtime_api'] = list(capstone.cs_version())
    (OUTPUT / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    (OUTPUT / 'SHA256SUMS.txt').write_text(''.join(checksum(p) + '  ' + p.name + '\n' for p in artifacts), encoding='utf-8')
    (BUILD / 'build-status.json').write_text(json.dumps({'status': 'complete', 'version': VERSION,
        'setup_sha256': checksum(installer)}), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
