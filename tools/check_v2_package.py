"""Check packaged metadata/payload and bridge IPC using synthetic logs only."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import pefile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from app_version import VERSION, VERSION_TUPLE
from install_bundle import BUNDLE_FILES, MANIFEST
from ufi_bridge.service import tool_definitions


def inspect_exe(path, elevation):
    pe = pefile.PE(str(path))
    info = pe.VS_FIXEDFILEINFO[0]
    version = (info.FileVersionMS >> 16, info.FileVersionMS & 65535,
               info.FileVersionLS >> 16, info.FileVersionLS & 65535)
    assert version == VERSION_TUPLE, (path.name, version)
    assert info.FileFlags & 2, 'Prerelease flag missing'
    manifests = [pe.get_data(language.data.struct.OffsetToData, language.data.struct.Size)
                 for resource in pe.DIRECTORY_ENTRY_RESOURCE.entries if resource.id == 24
                 for entry in resource.directory.entries for language in entry.directory.entries]
    levels = [node.attrib['level'] for data in manifests
              for node in ET.fromstring(data).iter()
              if node.tag.endswith('requestedExecutionLevel')]
    assert levels == [elevation], (path.name, levels)
    return {'version': version, 'prerelease_flag': True, 'execution_level': elevation}


def main():
    output = ROOT / 'dist' / ('v' + VERSION)
    setup = output / ('UFI-Fix-v' + VERSION + '-Setup.exe')
    build_status = json.loads((ROOT / 'build' / ('v' + VERSION) / 'build-status.json').read_text(encoding='utf-8'))
    assert build_status['status'] == 'complete', 'Latest build did not finish; existing artifacts may be stale.'
    assert hashlib.sha256(setup.read_bytes()).hexdigest() == build_status['setup_sha256']
    portable = output / ('UFI-Fix-v' + VERSION + '-portable.zip')
    executables = {'main': inspect_exe(output / 'UFI-Fix.exe', 'requireAdministrator'),
                   'bridge': inspect_exe(output / 'UFI-Bridge.exe', 'asInvoker'),
                   'control': inspect_exe(output / 'UFI-Control.exe', 'requireAdministrator'),
                   'setup': inspect_exe(setup, 'requireAdministrator')}
    installer = CArchiveReader(str(setup))
    manifest_bytes = installer.extract(MANIFEST)
    manifest = json.loads(manifest_bytes)
    assert manifest['version'] == VERSION and set(manifest['files']) == set(BUNDLE_FILES)
    with zipfile.ZipFile(portable) as archive:
        assert set(archive.namelist()) == {'UFI-Fix/' + n for n in BUNDLE_FILES + (MANIFEST,)}
        assert archive.read('UFI-Fix/' + MANIFEST) == manifest_bytes
        for name in BUNDLE_FILES:
            embedded = installer.extract(name)
            assert hashlib.sha256(embedded).hexdigest() == manifest['files'][name], name
            assert archive.read('UFI-Fix/' + name) == embedded, name
            if name.endswith('.exe'):
                assert (output / name).read_bytes() == embedded, name
    bridge = output / 'UFI-Bridge.exe'
    version = subprocess.run([str(bridge), '--version'], capture_output=True, check=True, timeout=15)
    assert version.stdout.decode().strip() == 'UFI-Bridge ' + VERSION
    with tempfile.TemporaryDirectory(prefix='ufi-v2-package-') as directory:
        temporary = Path(directory)
        fixture = temporary / 'UFI'
        fixture.mkdir()
        (fixture / 'UFI.exe').touch()  # Never execute this identity fixture.
        (fixture / 'eMMC_ToolBox.log').write_bytes(b'Synthetic version 2 log\n')
        state = temporary / 'support'
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                        'clientInfo': {'name': 'v2-package-check', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'ufi.capabilities', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'ufi.logs.read', 'arguments': {'source': 'emmc'}}},
        ]
        payload = b''.join(json.dumps(message).encode('utf-8') + b'\n' for message in messages)
        process = subprocess.run([str(bridge), '--ufi-dir', str(fixture), '--state-dir', str(state)],
                                 input=payload, capture_output=True, check=True, timeout=15)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        assert [response['id'] for response in responses] == [1, 2, 3, 4]
        assert responses[0]['result']['serverInfo']['version'] == VERSION
        assert responses[1]['result']['tools'] == tool_definitions()
        capabilities = responses[2]['result']['structuredContent']
        assert capabilities['bridge_version'] == VERSION and not capabilities['full_ufi_control']
        assert not responses[3]['result']['isError']
        assert responses[3]['result']['structuredContent']['text'] == 'Synthetic version 2 log\n'
        assert not state.exists()
    tests = (ROOT / 'build' / ('v' + VERSION) / 'automated-checks.log').read_text(encoding='utf-8', errors='replace')
    test_count = int(re.search(r'Ran (\d+) tests', tests).group(1))
    report = {'version': VERSION, 'automated_tests_passed': test_count,
              'executable_metadata': executables, 'payload_checksums': 'passed',
              'portable_matches_installer': True, 'packaged_mcp_synthetic_roundtrip': 'passed',
              'tool_count': len(tool_definitions()), 'live_installation': 'not performed',
              'gui_ufi_hardware_acceptance': 'deferred by user', 'public_release': False}
    (output / 'package-validation.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
