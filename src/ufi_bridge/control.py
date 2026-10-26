"""Narrow, version-specific UFI GUI contracts; no raw message/shell API."""
import hashlib
import copy
import os
from pathlib import Path
import time
import uuid
from release_updates import GATE, UpdateError

from .core import BridgeError, MODULES, window_view
from .detection import decode_fresh, parse_detection, ui_observation

MAX_IDENTIFY_OUTPUT = 32768  # JSON-escaped UTF-16/UTF-8 text fits the private pipe.

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
    ('ufi.device.preflight', 'Read Detect readiness, blocking dialogs, current settings and log availability. Sends no device command.',
     obj({'module': MEMORY}, ['module']), True),
    ('ufi.device.identify', 'Invoke only the verified Identify button when enabled. Returns a request ID, never a fabricated detected chip.',
     obj({'module': MEMORY, 'instance_id': INSTANCE}, ['module', 'instance_id']), False),
    ('ufi.device.result', 'Read structured Detect progress/identity/capacity/health from fresh output. Confirmation also requires stable output and an idle exact-instance GUI.',
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
    def __init__(self, root, native, session_check=lambda: None, updates=None):
        self.root, self.native = Path(root), native
        self.requests = {}
        self.launch_pending = {}
        self.session_check = session_check
        self.updates = updates if updates is not None else GATE

    def require_current(self):
        try:
            self.updates.require_current()
        except UpdateError as error:
            raise BridgeError(error.code, str(error)) from None

    def before_dispatch(self, require_current=True):
        if require_current:
            self.require_current()
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
        activity = ui_observation(instance, nodes)
        if activity['blocked']:
            raise BridgeError('ui_blocked', 'UFI has a modal dialog or disabled main window; resolve it in UFI first.')
        # Fail closed whenever an enabled Stop/Abort operation control is visible.
        if activity['busy']:
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

    def preflight(self, module):
        checks, instance, settings = [], None, None
        reason = None
        activity = {'observation_complete': False}
        try:
            instance = self.target(module)
            checks.append({'id': 'application', 'status': 'ready'})
            activity = self.native.activity(instance)
            if activity['blocked']:
                raise BridgeError('ui_blocked', 'Resolve the visible UFI dialog before Identify.')
            if activity['busy']:
                raise BridgeError('device_busy', 'Wait for the current UFI operation; no retry was sent.')
            if not instance.get('title', '').strip() and not activity['identify_available']:
                raise BridgeError('ui_unavailable', 'UFI has not exposed a usable toolbox interface; inspect its startup screen and box connection.')
            checks.append({'id': 'ui', 'status': 'ready'})
            settings = self.list_settings(module)
            if settings['instance']['instance_id'] != instance['instance_id']:
                raise BridgeError('stale_instance', 'Toolbox restarted while readiness was read.')
            checks.append({'id': 'control_profile', 'status': 'ready'})
            if not activity['identify_available']:
                raise BridgeError('device_not_ready', 'The real Identify button is unavailable; check the programmer/device.')
            checks.append({'id': 'identify_button', 'status': 'ready'})
            log = self.root / MODULES[module][1]
            if not log.is_file() or log.is_symlink() or log.resolve().parent != self.root.resolve():
                raise BridgeError('log_unavailable', 'A regular toolbox log is required to track fresh output.')
            with log.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
            if size > 8 * 1024 * 1024:
                raise BridgeError('log_too_large', 'Archive the large toolbox log before tracking Identify.')
            checks.append({'id': 'log', 'status': 'ready'})
        except BridgeError as error:
            reason = error.code
            checks.append({'id': 'readiness', 'status': 'blocked', 'reason_code': reason, 'message': str(error)})
        return {'module': module, 'can_identify': reason is None, 'reason_code': reason,
                'instance': self.public(instance) if instance else None,
                'checks': checks, 'ui': activity, 'settings': settings,
                'device_command_sent': False, 'settings_changed': False,
                'readiness_basis': 'ufi_ui_and_fixed_log; chip identity requires Identify output',
                'steps_after_dispatch': ['programmer', 'insertion', 'bus', 'identity', 'capacity', 'health', 'completion']}

    def read_result(self, request_id):
        job = self.requests.get(request_id)
        if job is None:
            raise BridgeError('unknown_request', 'Identify request belongs to another session or does not exist.')
        if job.get('final_result'):
            return copy.deepcopy(job['final_result'])
        path = job['path']
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise BridgeError('log_changed', 'Toolbox log path changed; output cannot be attributed.')
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            prefix = stream.read(job['offset'])
            if stat.st_ino != job['inode'] or hashlib.sha256(prefix).hexdigest() != job['baseline']:
                raise BridgeError('log_changed', 'Toolbox log was rotated/rewritten; no detection result is confirmed.')
            data = stream.read(MAX_IDENTIFY_OUTPUT + 1)
        truncated = stat.st_size - job['offset'] > MAX_IDENTIFY_OUTPUT
        chunk = data[:MAX_IDENTIFY_OUTPUT]
        raw, lossy = decode_fresh(chunk, job['header'])
        parsed = parse_detection(raw)
        view = window_view(chunk, job['header'], job['offset'], stat.st_size)
        now = time.monotonic()
        fingerprint = hashlib.sha256(chunk).hexdigest()
        if fingerprint != job.get('last_fingerprint'):
            job.update(last_fingerprint=fingerprint, last_change=now, stable_since=now, stable_reads=1)
        else:
            job['stable_reads'] += 1
        ui, ui_reason = {'observation_complete': False}, None
        try:
            target = self.target(job['module'], job['instance_id'])
            ui = self.native.activity(target)
        except BridgeError as error:
            ui_reason = error.code
        idle = bool(ui.get('observation_complete') and not ui['busy'] and not ui['blocked'] and ui['identify_available'])
        stable = job['stable_reads'] >= 2 and now - job['stable_since'] >= .25
        failure = any(not e['recovered_before_identity'] for e in parsed['errors'])
        complete_metadata = parsed['identity_observed'] and 'user_area_bytes' in parsed['capacity']
        health = parsed['health']
        needs_health = 'MMC v5' in parsed['device'].get('ext_csd_report', '')
        health_complete = health['status'] in ('urgent', 'warning', 'normal') and all(k in health for k in ('slc', 'mlc'))
        phase, finished, confirmed = 'pending', False, False
        if ui_reason in ('stale_instance', 'app_not_running'):
            phase, finished = 'interrupted', True
        elif parsed['ambiguous_output']:
            phase, finished = 'ambiguous', idle and stable
        elif truncated or lossy or job['module'] != 'emmc':
            phase = 'unconfirmed'
        elif failure:
            phase, finished = 'failed', idle and stable
        elif ui.get('blocked'):
            phase = 'blocked'
        elif idle and stable and data:
            confirmed = complete_metadata
            phase = 'completed' if complete_metadata and (not needs_health or health_complete) else 'partial'
            finished = True
        elif data:
            phase = 'verifying_completion' if idle else 'running'
        if not finished and phase in ('pending', 'running') and now - job['last_change'] > 30:
            phase = 'stalled'  # Observation only: never cancel or retry a hardware command.
        steps = parsed['steps']
        if finished:
            steps = [dict(s, status='not_reported') if s['status'] == 'pending' else s for s in steps]
        completion = ('failed' if phase in ('failed', 'interrupted') else
                      'unconfirmed' if phase == 'ambiguous' else 'observed') if finished else 'pending'
        steps = steps + [{'id': 'completion', 'status': completion}]
        next_step = None if finished else next((s['id'] for s in steps if s['status'] == 'pending'), None)
        result = dict(view, **{k: v for k, v in parsed.items() if k != 'steps'},
                      status='output_observed' if data else 'pending', phase=phase, finished=finished,
                      chip_identified=confirmed and phase in ('completed', 'partial'), steps=steps,
                      next_expected_step=next_step, request_id=request_id, instance_id=job['instance_id'],
                      elapsed_seconds=round(now - job['started'], 2),
                      seconds_since_progress=round(now - job['last_change'], 2),
                      ui=ui, ui_reason_code=ui_reason, output_truncated=truncated,
                      decoding_unconfirmed=lossy, next_poll_after_ms=1000 if not finished else None,
                      bytes_since_request=max(0, stat.st_size - job['offset']), has_more=truncated,
                      attribution='fresh_toolbox_log_and_exact_instance_ui; concurrent manual actions can be ambiguous')
        if finished:
            job['final_result'] = copy.deepcopy(result)
        return result

    def call(self, name, args):
        if name in ('ufi.app.open', 'ufi.settings.set', 'ufi.device.identify'):
            self.require_current()
        self.deadline = time.monotonic() + 18
        return self._call(name, args)

    def _call(self, name, args):
        if name == 'ufi.device.preflight':
            return self.preflight(args['module'])
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
            self.before_dispatch(require_current=False)
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
            current_activity = self.native.activity(instance)
            if current_activity['blocked']:
                raise BridgeError('ui_blocked', 'Resolve the visible UFI panel before Identify.')
            if current_activity['busy']:
                raise BridgeError('device_busy', 'Another UFI operation is running; no new Identify was sent.')
            label = 'Identify eMMC' if args['module'] == 'emmc' else 'Identify UFS'
            buttons = [n for n in nodes if n['class'] == 'TButton' and n['name'].replace('&', '').strip() == label]
            if len(buttons) != 1 or not buttons[0]['visible'] or not buttons[0]['enabled']:
                raise BridgeError('device_not_ready', 'The verified Identify button is absent, hidden or disabled. Check the programmer/device in UFI.')
            if len(self.requests) >= 100:
                raise BridgeError('request_limit', 'This session reached its Identify request limit.')
            if any(j['instance_id'] == args['instance_id'] and
                   (not j.get('final_result') or time.monotonic() - j['started'] < 30)
                   for j in self.requests.values()):
                raise BridgeError('device_busy', 'A tracked Identify is still active or was recently sent; inspect its result first.')
            log = self.root / MODULES[args['module']][1]
            if not log.is_file() or log.is_symlink() or log.resolve().parent != self.root.resolve():
                raise BridgeError('log_unavailable', 'A regular toolbox log is required to track fresh Identify output.')
            # Store a full bounded prefix digest to detect rewrites as well as truncation.
            with log.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                if stat.st_size > 8 * 1024 * 1024:
                    raise BridgeError('log_too_large', 'Snapshot/archive the large toolbox log before Identify tracking.')
                baseline = stream.read(8 * 1024 * 1024 + 1)
                if len(baseline) > 8 * 1024 * 1024:
                    raise BridgeError('log_too_large', 'Toolbox log grew beyond its tracking limit.')
            request_id = uuid.uuid4().hex
            self.requests[request_id] = {'path': log, 'baseline': hashlib.sha256(baseline).hexdigest(),
                'offset': len(baseline), 'inode': stat.st_ino, 'instance_id': args['instance_id'],
                'header': baseline[:4], 'started': time.monotonic(), 'module': args['module']}
            try:
                self.before_dispatch()
                self.native.identify(buttons[0], instance)
            except Exception:
                del self.requests[request_id]
                raise
            return {'status': 'identify_requested', 'request_id': request_id, 'chip_identified': False,
                    'instance_id': args['instance_id']}
        if name == 'ufi.device.result':
            return self.read_result(args['request_id'])
        raise BridgeError('unknown_tool', 'The control helper does not implement this operation.')
