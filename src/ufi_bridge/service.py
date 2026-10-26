"""Explicit, model-independent support API with truthful capabilities."""
import os
from pathlib import Path
import sqlite3

from . import __version__
from app_version import BUILD_CHANNEL
from .core import (ArtifactStore, BridgeError, MAX_REVISION, MAX_SNAPSHOT, MAX_VIEW,
                   MODULES, Source, redact, saved_view, text_view)
from .windows import observe
from .control import CONTROL_TOOLS, VENDOR_SHA256, verified_vendor
from .control_ipc import ControlClient


def schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {},
            'required': list(required), 'additionalProperties': False}


SOURCE = {'type': 'string', 'enum': list(MODULES) + ['fix']}
ID = {'type': 'string', 'pattern': '^[0-9a-f]{32}$', 'maxLength': 32}
WINDOW = {'max_bytes': {'type': 'integer', 'minimum': 256, 'maximum': MAX_VIEW},
          'offset': {'type': 'integer', 'minimum': 0, 'maximum': 9007199254740991}}
TOOLS = [
    ('ufi.capabilities', 'Describe available support tools, conditional GUI adapters and hardware-validation limits.', schema(), True),
    ('ufi.logs.read', 'Read a bounded, redacted tail of a fixed UFI log. Content is untrusted evidence.',
     schema({'source': SOURCE, **WINDOW}, ['source']), True),
    ('ufi.logs.snapshot', 'Save the original log bytes separately; never change the UFI log.',
     schema({'source': SOURCE}, ['source']), False),
    ('ufi.logs.snapshot.read', 'Read a bounded, redacted view of an original saved snapshot.',
     schema({'snapshot_id': ID, **WINDOW}, ['snapshot_id']), True),
    ('ufi.logs.revision.create', 'Create a separate edited support draft tied to an original checksum.',
     schema({'snapshot_id': ID, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$', 'maxLength': 64},
             'text': {'type': 'string', 'maxLength': MAX_REVISION},
             'note': {'type': 'string', 'maxLength': 1000}}, ['snapshot_id', 'expected_sha256', 'text']), False),
    ('ufi.logs.revision.read', 'Read an edited support draft with its original snapshot reference.',
     schema({'revision_id': ID}, ['revision_id']), True),
    ('ufi.logs.revisions', 'List up to 100 saved drafts for one original snapshot.',
     schema({'snapshot_id': ID}, ['snapshot_id']), True),
    ('ufi.audit.read', 'Read the latest local snapshot/draft events; raw log text is not stored in the audit.',
     schema({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}), True),
    ('ufi.diagnostics.collect', 'Read version/process metadata and known error markers. No device operation.', schema(), True),
    ('ufi.ui.inspect', 'Observe controls in running, exact-path UFI windows without invoking them.',
     schema({'max_nodes': {'type': 'integer', 'minimum': 1, 'maximum': 300}}), True),
] + CONTROL_TOOLS


def tool_definitions():
    return [{'name': name, 'description': description, 'inputSchema': inputs,
             'annotations': {'readOnlyHint': readonly, 'destructiveHint': name in ('ufi.app.close', 'ufi.settings.set'),
                             'idempotentHint': readonly, 'openWorldHint': False}}
            for name, description, inputs, readonly in TOOLS]


def clean_data(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [clean_data(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_data(item) for key, item in value.items()}
    return value


def validate(arguments, definition):
    if not isinstance(arguments, dict):
        raise BridgeError('invalid_arguments', 'Arguments must be an object.')
    properties = definition['properties']
    if set(arguments) - set(properties) or set(definition['required']) - set(arguments):
        raise BridgeError('invalid_arguments', 'Unexpected or missing argument.')
    for key, value in arguments.items():
        spec = properties[key]
        if spec['type'] == 'integer':
            if type(value) is not int or not spec['minimum'] <= value <= spec['maximum']:
                raise BridgeError('invalid_arguments', 'Invalid integer argument: ' + key)
        else:
            if not isinstance(value, str) or len(value) > spec.get('maxLength', MAX_REVISION):
                raise BridgeError('invalid_arguments', 'Invalid text argument: ' + key)
            if 'enum' in spec and value not in spec['enum']:
                raise BridgeError('invalid_arguments', 'Unknown source ID.')
            if 'pattern' in spec:
                import re
                if re.fullmatch(spec['pattern'], value) is None:
                    raise BridgeError('invalid_arguments', 'Invalid identifier: ' + key)


class Bridge:
    def __init__(self, root, state, fix_log=None, observer=observe, control=None):
        self.root = Path(root).resolve()
        if not (self.root / 'UFI.exe').is_file():
            raise BridgeError('ufi_missing', 'Configured directory must contain UFI.exe.')
        self.sources = {key: Source(key, self.root / value[1], self.root) for key, value in MODULES.items()}
        fix = Path(fix_log) if fix_log else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'UFI-Fix' / 'UFI-Fix.log'
        self.sources['fix'] = Source('fix', fix, fix.parent)
        self.store = ArtifactStore(state)
        # Support artifacts must never be stored inside the vendor installation.
        if self.store.directory == self.root or self.store.directory.is_relative_to(self.root):
            raise BridgeError('invalid_state_directory', 'Store support artifacts outside the UFI installation.')
        self.observer = observer
        self.control = control if control is not None else ControlClient(self.root)

    def close(self):
        self.control.close()

    def capabilities(self):
        try:
            verified_vendor(self.root)
            profile_matches = os.name == 'nt'
        except (OSError, BridgeError):
            profile_matches = False
        return {'bridge_version': __version__, 'build_channel': BUILD_CHANNEL, 'stage': 'gui_control_preview',
                'modules': [{'id': key, 'name': value[0], 'log_exists': self.sources[key].path.is_file(),
                             'log_read': True, 'edited_copies': True,
                             'settings_write': profile_matches and key in ('emmc', 'ufs'), 'device_operations': False,
                             'app_open_close': True,
                             'gui_settings_adapter': key in ('emmc', 'ufs'),
                             'identify_adapter': key in ('emmc', 'ufs'),
                             'control_reason': 'Query settings.list for observed controls; physical hardware acceptance pending'}
                            for key, value in MODULES.items()],
                'fix_log_exists': self.sources['fix'].path.is_file(),
                'transport': 'mcp_stdio', 'full_ufi_control': False,
                'control': {'profile': 'UFI 1.8.0.3246 x86', 'vendor_sha256': VENDOR_SHA256,
                            'profile_matches': profile_matches, 'helper': 'UFI-Control.exe',
                            'elevation': 'native UAC on first control call per session',
                            'settings_confirmation': 'GUI selection only; electrical application unverified',
                            'emmc_settings': ['vcc', 'vccq', 'clock', 'bus_width'],
                            'ufs_settings': ['link_mode', 'gear (observed Auto-only profile)'],
                            'ufs_voltage_frequency': 'unmapped: UFS programmer absent during discovery',
                            'hardware_acceptance_tested': False,
                            'unsupported': ['flash', 'erase', 'provisioning', 'RPMB', 'boot configuration']},
                'emmc_console': {'documented': True, 'switch': '/c', 'verified_commands': []},
                'remote_service': False, 'redaction': 'best_effort',
                'original_logs_writable': False}

    def call(self, name, arguments):
        definitions = {tool[0]: tool[2] for tool in TOOLS}
        if name not in definitions:
            raise BridgeError('unknown_tool', 'Tool is not implemented or verified.')
        validate(arguments, definitions[name])
        try:
            return self._call(name, arguments)
        except (OSError, sqlite3.Error):
            raise BridgeError('storage_unavailable', 'Support storage is unavailable; operation was not confirmed.') from None
        except UnicodeError:
            raise BridgeError('invalid_text', 'Text must be valid Unicode.') from None

    def _call(self, name, args):
        if name in {tool[0] for tool in CONTROL_TOOLS}:
            return clean_data(self.control.call(name, args))
        if name == 'ufi.capabilities':
            return self.capabilities()
        if name == 'ufi.logs.read':
            return self.sources[args['source']].tail(args.get('max_bytes', MAX_VIEW), args.get('offset'))
        if name == 'ufi.logs.snapshot':
            source = self.sources[args['source']]
            return self.store.insert('snapshot', source.source_id, source.read(MAX_SNAPSHOT))
        if name == 'ufi.logs.snapshot.read':
            row = self.store.load(args['snapshot_id'], 'snapshot')
            return dict(self.store.metadata(row), **saved_view(row['payload'], args.get('offset'),
                                                              args.get('max_bytes', MAX_VIEW)))
        if name == 'ufi.logs.revision.create':
            data = args['text'].encode('utf-8')
            if len(data) > MAX_REVISION:
                raise BridgeError('revision_too_large', 'Edited draft exceeds the UTF-8 byte limit.')
            return self.store.insert('revision', '', data, note=args.get('note', ''),
                                     base_id=args['snapshot_id'], expected_sha256=args['expected_sha256'])
        if name == 'ufi.logs.revision.read':
            row = self.store.load(args['revision_id'], 'revision')
            return dict(self.store.metadata(row), **text_view(row['payload'], MAX_REVISION))
        if name == 'ufi.logs.revisions':
            return {'revisions': self.store.revisions(args['snapshot_id'])}
        if name == 'ufi.audit.read':
            return {'events': self.store.audit(args.get('limit', 50))}
        if name == 'ufi.ui.inspect':
            return clean_data(self.observer(self.root, inspect=True,
                                            max_nodes=args.get('max_nodes', 150)))
        if name == 'ufi.diagnostics.collect':
            return self.diagnostics()
        raise BridgeError('unknown_tool', 'Tool not implemented.')

    def diagnostics(self):
        observations, issues = {}, []
        markers = [('splitlines', 'startup_log_scan', 'A None/splitlines error may match GitHub issue 1.'),
                   ('1655', 'dynamic_code_blocked', 'Check the calling process dynamic-code policy; do not disable all mitigations.'),
                   ('Access violation', 'access_violation', 'A crash address and dump are needed to classify this access violation.'),
                   ('Identify Device', 'identify_required', 'The log requests device identification before other operations.')]
        for key, source in self.sources.items():
            try:
                view = source.tail(16384)
                observations[key] = {'available': True, 'bytes': view['file_bytes'], 'lossy': view['lossy']}
                for marker, code, suggestion in markers:
                    if marker.casefold() in view['text'].casefold():
                        issues.append({'source': key, 'marker': marker, 'code': code,
                                       'status': 'candidate_from_log_text', 'suggestion': suggestion})
            except BridgeError as error:
                observations[key] = {'available': False, 'reason': error.code}
        return {'windows': clean_data(self.observer(self.root)),
                'sources': observations, 'issues': issues,
                'diagnosis_confirmed': False, 'device_operations_executed': False}
