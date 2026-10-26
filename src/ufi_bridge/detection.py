"""Bounded evidence parsing and UI readiness; never execute log instructions."""
from decimal import Decimal, InvalidOperation
import hashlib
import re

from .core import redact

STEPS = ('programmer', 'insertion', 'bus', 'identity', 'capacity', 'health', 'completion')


def caption(node):
    return node.get('name', '').replace('&', '').strip()


def ui_observation(instance, nodes):
    """Recognize the observed VCL embedded dialog structure, not screen clicks.

    A root-owned inset panel with an enabled dismiss button is conservatively
    treated as an overlay. Full-width layout panels are excluded. Handles never
    become caller-selectable targets. No automatic acknowledgement is provided.
    """
    root, bounds = instance['hwnd'], instance.get('rect')
    by_handle = {n['hwnd']: n for n in nodes}
    overlays = []
    if bounds:
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        for panel in nodes:
            rect = panel.get('rect')
            if panel['class'] != 'TPanel' or panel['parent'] != root or not panel['visible'] or not rect:
                continue
            if not (0 < rect[2] - rect[0] < width * .95 and 0 < rect[3] - rect[1] < height * .95
                    and rect[0] >= bounds[0] and rect[1] >= bounds[1]
                    and rect[2] <= bounds[2] and rect[3] <= bounds[3]):
                continue
            dismiss = []
            for button in nodes:
                if button['class'] != 'TButton' or not button['visible'] or not button['enabled']:
                    continue
                label = caption(button)
                if label.casefold() not in ('ok', 'close', 'yes', 'no', 'cancel'):
                    continue
                parent, seen = button['parent'], set()
                while parent in by_handle and parent not in seen:
                    if parent == panel['hwnd']:
                        dismiss.append(label)
                        break
                    seen.add(parent)
                    parent = by_handle[parent]['parent']
            if dismiss:
                overlays.append({'kind': 'embedded_panel', 'buttons': sorted(set(dismiss)),
                                 'reason': 'Resolve the visible dialog in UFI; no input was sent.'})
    busy = ' - running' in instance.get('title', '').casefold() or any(
        n['visible'] and n['enabled'] and caption(n).casefold() in ('abort', 'stop', 'cancel operation')
        for n in nodes)
    label = 'Identify eMMC' if instance.get('module') == 'emmc' else 'Identify UFS'
    identify = [n for n in nodes if n['class'] == 'TButton' and caption(n) == label]
    blocked = not instance['enabled'] or bool(instance.get('dialogs')) or bool(overlays)
    return {'busy': busy, 'blocked': blocked, 'embedded_dialogs': overlays,
            'identify_available': len(identify) == 1 and identify[0]['visible'] and identify[0]['enabled'],
            'observation_complete': True}


def decode_fresh(data, header):
    marker = header if header else data[:4]
    encoding = ('utf-16-le' if marker.startswith(b'\xff\xfe') else
                'utf-16-be' if marker.startswith(b'\xfe\xff') else 'utf-8-sig')
    try:
        return data.decode(encoding).lstrip('\ufeff'), False
    except UnicodeDecodeError:
        return data.decode(encoding, errors='replace').lstrip('\ufeff'), True


def parse_detection(text):
    """Extract typed observations from this request's fresh window only.

    Do not round a displayed GiB value into an exact byte count. CID is hashed,
    never returned raw. Multiple identities/attempts are ambiguous, even if an
    older block contains a complete and apparently successful result.
    """
    device, capacity, bus = {}, {}, {}
    health = {'status': 'not_reported', 'severity': 'unknown'}
    normalized = text.replace('\r', '')
    ids = list(re.finditer(r'(?im)^\s*CID:\s*([0-9a-f][0-9a-f \t]*)$', normalized))
    valid_ids = [(m, re.sub(r'\s', '', m.group(1))) for m in ids
                 if len(re.sub(r'\s', '', m.group(1))) == 32]
    ambiguous = len(ids) > 1
    if len(valid_ids) == 1:
        identity_position, cid = valid_ids[0]
        device['cid_sha256'] = hashlib.sha256(bytes.fromhex(cid)).hexdigest()
        if re.search(r'(?im)^Connection status:', normalized[identity_position.end():]):
            ambiguous = True
    manufacturer = re.search(r'(?im)^\s*Manufacturer ID:\s*0x([0-9a-f]{1,2})\s*\(([^\r\n)]{1,64})\)', normalized)
    if manufacturer:
        device.update(manufacturer_id=int(manufacturer[1], 16), manufacturer=redact(manufacturer[2]))
    product = re.search(r'(?im)^\s*Product name:\s*([A-Za-z0-9_.-]{1,32})(?:\s|,|\()', normalized)
    if product:
        device['product_name'] = product[1]
    revision = re.search(r'(?im)^\s*EXT_CSD revision:\s*([^\n]{1,100})', normalized)
    if revision:
        device['ext_csd_report'] = revision[1].strip()
    area = re.search(r'(?im)^\s*User area:\s*[^\n(]*\(([0-9][0-9,]{0,24})\s+bytes\)', normalized)
    if area:
        value = int(area[1].replace(',', ''))
        if 0 < value < 2**64:
            capacity.update(user_area_bytes=value, user_area_gib=round(value / 2**30, 2))
    for match in re.finditer(r'(?im)^\s*(Boot1|Boot2|RPMB):\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|MiB|GiB|bytes)\s*$', normalized):
        try:
            value = Decimal(match[2]) * {'kib': 2**10, 'mib': 2**20, 'gib': 2**30, 'bytes': 1}[match[3].lower()]
            if value == int(value) and 0 <= value < 2**64:
                capacity[match[1].lower() + '_bytes'] = int(value)
        except (InvalidOperation, ValueError):
            pass
    volts = re.search(r'(?im)^\s*VCC:\s*([0-9]+(?:\.[0-9]+)?)\s*V,\s*VCCQ:\s*([0-9]+(?:\.[0-9]+)?)\s*V', normalized)
    if volts and all(0 < Decimal(v) <= 5.5 for v in volts.groups()):
        bus.update(vcc_volts=float(volts[1]), vccq_volts=float(volts[2]))
    speed = re.search(r'(?im)^\s*Bus:\s*(1|4|8)\s+bit\s*\(([^\n)]{1,64})\)', normalized)
    if speed:
        bus.update(width_bits=int(speed[1]), mode_report=speed[2].strip())
        clock = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*MHz', speed[2], re.I)
        if clock and 0 < Decimal(clock[1]) <= 1000:
            bus['clock_hz'] = int(Decimal(clock[1]) * 1000000)
    bus['electrically_measured'] = False
    health_block = re.search(r'(?im)^\s*eMMC 5\.x Health report:', normalized)
    if health_block:
        block = normalized[health_block.end():]
        life = re.search(r'(?im)^\s*Device lifetime:\s*([^\n]{1,160})', block)
        if life:
            report = life[1].strip()
            status = next((s for s in ('urgent', 'warning', 'normal') if report.casefold().startswith(s)), 'unknown')
            health.update(status=status, report=redact(report),
                          severity={'urgent': 'critical', 'warning': 'warning', 'normal': 'normal'}.get(status, 'unknown'))
        for key, label in (('slc', 'SLC'), ('mlc', 'MLC')):
            match = re.search(r'(?im)^\s*Estimation of the device lifetime\s*\(' + label + r'\):\s*([^\n]{1,180})', block)
            if match:
                report = match[1].strip()
                state = ('exceeded' if 'exceeded' in report.casefold() else
                         'not_defined' if 'not defined' in report.casefold() else 'reported')
                health[key] = {'status': state, 'report': redact(report)}
        if health['severity'] == 'critical' or health.get('slc', {}).get('status') == 'exceeded':
            health['next_action'] = 'preserve_data_before_further_operations'
    errors = []
    patterns = [('access_violation', r'Access violation|UncaughtException'),
                ('cmd1_timeout', r'CMD1 error|SEND_OP_COND command failed'),
                ('initialization_failed', r'Failed to initialize eMMC'),
                ('insertion_failed', r'Insertion test[^\n]*(?:fail|error)'),
                ('programmer_missing', r'No UFI[^\n]*Device Detected')]
    cid_position = valid_ids[0][0].start() if len(valid_ids) == 1 else -1
    for code, pattern in patterns:
        for match in list(re.finditer(pattern, normalized, re.I))[:8]:
            recovered = code in ('cmd1_timeout', 'initialization_failed') and match.start() < cid_position
            errors.append({'code': code, 'recovered_before_identity': recovered})
    identity_complete = all(k in device for k in ('cid_sha256', 'manufacturer_id', 'product_name')) and not ambiguous
    stages = [
        ('programmer', bool(re.search(r'(?im)^Interface:\s*UFI', normalized))),
        ('insertion', bool(re.search(r'Insertion test\.\.\.\s*Done', normalized, re.I))),
        ('bus', bool(speed)), ('identity', identity_complete),
        ('capacity', 'user_area_bytes' in capacity), ('health', health['status'] != 'not_reported')]
    return {'device': device, 'capacity': capacity, 'bus': bus, 'health': health,
            'identity_observed': identity_complete, 'ambiguous_output': ambiguous,
            'errors': errors, 'steps': [{'id': key, 'status': 'observed' if ok else 'pending'} for key, ok in stages],
            'evidence_source': 'fresh_ufi_log', 'untrusted_content': True}
