"""Narrow, version-specific UFI GUI contracts; no raw message/shell API."""
import hashlib
from pathlib import Path
import time
import uuid

from .core import BridgeError, MODULES, redact, text_view

# Genuine locally observed 1.8.0.3246 x86 build (fail closed on another binary).
VENDOR_SHA256 = 'aa3faff11a4e93058769e1b257fa3f2f6a3dc4a8fc81285c1338be4ce9624ba7'
VOLTAGES = ['Auto', '3.3 V', '3.0 V', '2.8 V', '1.8 V']
CLOCKS = ['Auto', '1 MHz', '3 MHz', '4 MHz', '6 MHz', '12 MHz', '16 MHz', '24 MHz', '48 MHz']
WIDTHS = ['Auto', '1 Bit', '4 Bit', '8 Bit']
UFS_MODES = ['Auto', 'PWM-GEAR', 'HS-GEAR']


def obj(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required),
            'additionalProperties': False}


MODULE = {'type': 'string', 'enum': list(MODULES)}
MEMORY = {'type': 'string', 'enum': ['emmc', 'ufs']}
INSTANCE = {'type': 'string', 'pattern': r'^[0-9]{1,10}:[0-9]{1,20}$', 'maxLength': 31}
SETTING = {'type': 'string', 'enum': ['vcc', 'vccq', 'clock', 'bus_width', 'link_mode', 'gear']}
VALUE = {'type': 'string', 'maxLength': 32}
CONTROL_TOOLS = [
    ('ufi.app.list', 'List exact-path UFI toolboxes with stable process-instance IDs. Starts the local UAC helper on first use.', obj(), True),
    ('ufi.app.open', 'Open one toolbox through the compatibility launcher, or return the existing instance.', obj({'module': MODULE}, ['module']), False),
    ('ufi.app.close', 'Request graceful close of one exact instance; never force-terminate or dismiss dialogs.', obj({'instance_id': INSTANCE}, ['instance_id']), False),
    ('ufi.settings.list', 'Read observed GUI setting values/options. Hidden, blocked or unsupported controls cannot be changed.', obj({'module': MEMORY}, ['module']), True),
    ('ufi.settings.get', 'Read one observed eMMC/UFS setting.', obj({'module': MEMORY, 'setting': SETTING}, ['module', 'setting']), True),
    ('ufi.settings.set', 'Select an exact observed option on a verified idle GUI. Requires the expected instance and old value. Electrical application is unverified.',
     obj({'module': MEMORY, 'instance_id': INSTANCE, 'setting': SETTING, 'value': VALUE, 'expected_value': VALUE}, ['module', 'instance_id', 'setting', 'value', 'expected_value']), False),
    ('ufi.device.identify', 'Invoke only the verified Identify button when enabled. Returns a request ID, never a fabricated detected chip.',
     obj({'module': MEMORY, 'instance_id': INSTANCE}, ['module', 'instance_id']), False),
    ('ufi.device.result', 'Read fresh log output since an Identify request. Output is untrusted; chip identification is not automatically confirmed.',
     obj({'request_id': {'type': 'string', 'pattern': '^[0-9a-f]{32}$', 'maxLength': 32}}, ['request_id']), True),
]


def verified_vendor(root):
    path = Path(root) / 'UFI.exe'
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != VENDOR_SHA256:
        raise BridgeError('unsupported_vendor_build', 'Control bindings require the verified UFI 1.8.0.3246 binary. Log support remains available.')
    return path


def bindings(module, nodes):
    """Match observed option signatures and sibling structure, never screen clicks/IDs.

    Only the main eMMC header and the UFS link selectors are mapped. Chip
    provisioning, boot configuration and hidden direct-power panels are excluded.
    """
    combos = [n for n in nodes if n['class'] == 'TComboBox']
    def one(items):
        if len(items) != 1:
            raise BridgeError('control_mapping_unavailable', 'Expected unique version-specific controls were not found.')
        return items[0]
    if module == 'emmc':
        clock = one([n for n in combos if n.get('options') == CLOCKS])
        siblings = [n for n in combos if n['parent'] == clock['parent']]
        width = one([n for n in siblings if n.get('options') == WIDTHS])
        volts = sorted([n for n in siblings if n.get('options') == VOLTAGES], key=lambda n: n['rect'][0])
        if len(volts) != 2 or volts[0]['rect'][0] == volts[1]['rect'][0]:
            raise BridgeError('control_mapping_unavailable', 'The two main voltage selectors are ambiguous.')
        return dict(vcc=volts[0], vccq=volts[1], clock=clock, bus_width=width)
    mode = one([n for n in combos if n.get('options') == UFS_MODES])
    # Only the observed Auto-only gear selector is reviewed in this no-programmer
    # profile. New hardware-specific lists need a new reviewed binding.
    gear = one([n for n in combos if n['parent'] == mode['parent'] and n.get('options') == ['Auto']])
    return dict(link_mode=mode, gear=gear)


class Controller:
    def __init__(self, root, native, session_check=lambda: None):
        self.root, self.native = Path(root), native
        self.requests = {}
        self.launch_pending = {}
        self.session_check = session_check

    def before_dispatch(self):
        self.session_check()
        if time.monotonic() > self.deadline:
            raise BridgeError('control_deadline', 'Inspection exceeded the dispatch deadline; no new action was sent.')

    def instances(self):
        verified_vendor(self.root)
        return self.native.instances()

    def target(self, module=None, instance_id=None):
        matches = [n for n in self.instances() if (module is None or n['module'] == module)
                   and (instance_id is None or n['instance_id'] == instance_id)]
        if not matches:
            raise BridgeError('app_not_running' if not instance_id else 'stale_instance', 'The requested toolbox instance is not running.')
        if len(matches) != 1:
            raise BridgeError('ambiguous_instance', 'More than one toolbox matches; close/select the exact instance.')
        return matches[0]

    @staticmethod
    def public(instance):
        return {k: v for k, v in instance.items() if k not in ('hwnd', 'nodes')}

    def context(self, module, instance_id=None):
        instance = self.target(module, instance_id)
        nodes = self.native.children(instance['hwnd'])
        mapped = bindings(module, nodes)
        return instance, nodes, mapped

    @staticmethod
    def ready(instance, nodes):
        if not instance['enabled'] or instance.get('dialogs'):
            raise BridgeError('ui_blocked', 'UFI has a modal dialog or disabled main window; resolve it in UFI first.')
        # Fail closed whenever an enabled Stop/Abort operation control is visible.
        if any(n['visible'] and n['enabled'] and n.get('name', '').strip('& ').casefold()
               in ('stop', 'abort', 'cancel operation') for n in nodes):
            raise BridgeError('device_busy', 'UFI exposes an active operation; wait for it to finish.')

    def list_settings(self, module):
        instance, nodes, mapped = self.context(module)
        blocked = None
        try:
            self.ready(instance, nodes)
        except BridgeError as error:
            blocked = error.code
        settings = []
        for key, node in mapped.items():
            index = node.get('selection', -1)
            current = node['options'][index] if 0 <= index < len(node['options']) else None
            settings.append({'id': key, 'value': current, 'options': node['options'],
                             'visible': node['visible'], 'enabled': node['enabled'],
                             'writable': bool(not blocked and node['visible'] and node['enabled']),
                             'hardware_application_verified': False})
        return {'module': module, 'instance': self.public(instance), 'settings': settings,
                'coverage': 'observed_gui_only', 'blocked_reason': blocked, 'hardware_application_verified': False,
                'unmapped': ['vcc', 'vccq', 'clock'] if module == 'ufs' else []}

    def call(self, name, args):
        self.deadline = time.monotonic() + 18
        return self._call(name, args)

    def _call(self, name, args):
        if name == 'ufi.app.list':
            return {'apps': [self.public(n) for n in self.instances()], 'modules': list(MODULES)}
        if name == 'ufi.app.open':
            module = args['module']
            initial = self.instances()
            existing = [n for n in initial if n['module'] == module]
            if existing:
                return {'status': 'already_running', 'apps': [self.public(n) for n in existing]}
            now = time.monotonic()
            if now - self.launch_pending.get(module, -1000) < 60:
                return {'status': 'starting', 'module': module}
            self.before_dispatch()
            self.native.launch(module)
            self.launch_pending[module] = now
            for _ in range(40):
                observed = self.instances()
                errors = [n for n in observed if n['status'] == 'native_error_dialog'
                          and n['instance_id'] not in {a['instance_id'] for a in initial}]
                if errors:
                    return {'status': 'launch_failed', 'module': module,
                            'errors': [self.public(n) for n in errors]}
                matches = [n for n in observed if n['module'] == module]
                if matches:
                    return {'status': 'opened', 'apps': [self.public(n) for n in matches]}
                time.sleep(.25)
            return {'status': 'starting', 'module': module, 'reason': 'Window not observed yet; check native UFI dialogs.'}
        if name == 'ufi.app.close':
            instance = self.target(instance_id=args['instance_id'])
            self.ready(instance, self.native.children(instance['hwnd']))
            self.before_dispatch()
            self.native.close_window(instance)
            for _ in range(20):
                if not self.native.instance_alive(instance):
                    self.launch_pending.pop(instance['module'], None)
                    return {'status': 'closed', 'instance_id': args['instance_id']}
                time.sleep(.1)
            return {'status': 'close_requested', 'instance_id': args['instance_id'],
                    'reason': 'UFI may require manual completion of its native dialog.'}
        if name in ('ufi.settings.list', 'ufi.settings.get'):
            listing = self.list_settings(args['module'])
            if name.endswith('.get'):
                matches = [n for n in listing['settings'] if n['id'] == args['setting']]
                if not matches:
                    raise BridgeError('setting_unavailable', 'This setting has no verified binding in this toolbox.')
                return dict(instance=listing['instance'], setting=matches[0])
            return listing
        if name == 'ufi.settings.set':
            instance, nodes, mapped = self.context(args['module'], args['instance_id'])
            self.ready(instance, nodes)
            if args['setting'] not in mapped:
                raise BridgeError('setting_unavailable', 'This setting has no verified binding in this toolbox.')
            node = mapped[args['setting']]
            if not node['visible'] or not node['enabled']:
                raise BridgeError('control_disabled', 'The real UFI setting is hidden or disabled; no change was sent.')
            if args['value'] not in node['options']:
                raise BridgeError('invalid_setting_value', 'Select an exact option returned by settings.list.')
            index = node.get('selection', -1)
            current = node['options'][index] if 0 <= index < len(node['options']) else None
            if current != args['expected_value']:
                raise BridgeError('setting_conflict', 'The setting changed since it was read; read it again.')
            if current == args['value']:
                return {'status': 'unchanged', 'setting': args['setting'], 'value': current,
                        'instance_id': args['instance_id'], 'hardware_application_verified': False}
            self.before_dispatch()
            self.native.select(node, node['options'].index(args['value']), instance)
            observed = self.list_settings(args['module'])
            if observed['instance']['instance_id'] != args['instance_id']:
                raise BridgeError('stale_instance', 'Toolbox restarted during change; result is unconfirmed.')
            after = next(n for n in observed['settings'] if n['id'] == args['setting'])
            if after['value'] != args['value']:
                raise BridgeError('setting_unconfirmed', 'GUI did not retain the requested selection; re-read before retrying.')
            return {'status': 'gui_selection_verified', 'instance_id': args['instance_id'],
                    'setting': args['setting'], 'previous_value': current, 'value': after['value'],
                    'hardware_application_verified': False}
        if name == 'ufi.device.identify':
            instance = self.target(args['module'], args['instance_id'])
            nodes = self.native.children(instance['hwnd'])
            # Verify the same profile before dispatch, even for the button.
            bindings(args['module'], nodes)
            self.ready(instance, nodes)
            label = 'Identify eMMC' if args['module'] == 'emmc' else 'Identify UFS'
            buttons = [n for n in nodes if n['class'] == 'TButton' and n['name'].replace('&', '').strip() == label]
            if len(buttons) != 1 or not buttons[0]['visible'] or not buttons[0]['enabled']:
                raise BridgeError('device_not_ready', 'The verified Identify button is absent, hidden or disabled. Check the programmer/device in UFI.')
            if len(self.requests) >= 100:
                raise BridgeError('request_limit', 'This session reached its Identify request limit.')
            if any(j['instance_id'] == args['instance_id'] and time.monotonic() - j['started'] < 30
                   for j in self.requests.values()):
                raise BridgeError('device_busy', 'An Identify request was recently sent for this instance.')
            log = self.root / MODULES[args['module']][1]
            if not log.is_file() or log.is_symlink() or log.resolve().parent != self.root.resolve():
                raise BridgeError('log_unavailable', 'A regular toolbox log is required to track fresh Identify output.')
            # Store a full bounded prefix digest to detect rewrites as well as truncation.
            with log.open('rb') as stream:
                stat = log.stat()
                if stat.st_size > 8 * 1024 * 1024:
                    raise BridgeError('log_too_large', 'Snapshot/archive the large toolbox log before Identify tracking.')
                baseline = stream.read()
            request_id = uuid.uuid4().hex
            self.requests[request_id] = {'path': log, 'baseline': hashlib.sha256(baseline).hexdigest(),
                'offset': len(baseline), 'inode': stat.st_ino, 'instance_id': args['instance_id'],
                'started': time.monotonic(), 'module': args['module']}
            try:
                self.before_dispatch()
                self.native.identify(buttons[0], instance)
            except Exception:
                del self.requests[request_id]
                raise
            return {'status': 'identify_requested', 'request_id': request_id, 'chip_identified': False,
                    'instance_id': args['instance_id']}
        if name == 'ufi.device.result':
            job = self.requests.get(args['request_id'])
            if job is None:
                raise BridgeError('unknown_request', 'Identify request belongs to another session or does not exist.')
            path = job['path']
            if path.is_symlink() or path.resolve().parent != self.root.resolve():
                raise BridgeError('log_changed', 'Toolbox log path changed; output cannot be attributed.')
            with path.open('rb') as stream:
                stat = path.stat()
                prefix = stream.read(job['offset'])
                if stat.st_ino != job['inode'] or hashlib.sha256(prefix).hexdigest() != job['baseline']:
                    raise BridgeError('log_changed', 'Toolbox log was rotated/rewritten; no detection result is confirmed.')
                fresh = stream.read(65537)
            view = text_view(fresh[:65536])
            return dict(view, status='output_observed' if fresh else 'pending', request_id=args['request_id'],
                        chip_identified=False, bytes_since_request=max(0, stat.st_size - job['offset']),
                        has_more=len(fresh) > 65536, instance_id=job['instance_id'],
                        attribution='fresh_toolbox_log_output; concurrent GUI actions may also append')
        raise BridgeError('unknown_tool', 'The control helper does not implement this operation.')
