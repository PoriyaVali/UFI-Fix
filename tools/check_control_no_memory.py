"""Exercise packaged MCP controls without a chip; never alter voltage/flash.

Only windows created by this probe are closed. Clock GUI selection is restored
in finally. The operator must actually disconnect all memory before running.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from check_installed_mcp import Client, checksum


def probe(configuration):
    client = Client(configuration)
    created = []
    results = []
    def tool(name, args=None, error=None):
        result = client.tool(name, args, error=error)
        results.append({'tool': name, 'arguments': args or {}, 'result': result})
        print(name + ': ' + (result.get('status') or result.get('error', {}).get('code', 'ok')), flush=True)
        return result
    try:
        initialized = client.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                               'clientInfo': {'name': 'ufi-no-memory-check', 'version': '1'}})
        client.notify('notifications/initialized')
        definitions = client.rpc('tools/list')['tools']
        assert len(definitions) == 18
        initial = tool('ufi.app.list')['apps']
        initial_ids = {a['instance_id'] for a in initial}
        for module in ('emmc', 'ufs', 'android', 'chip'):
            opened = tool('ufi.app.open', {'module': module})
            assert opened['status'] != 'launch_failed', opened
            deadline = time.monotonic() + 20
            instance = None
            while time.monotonic() < deadline:
                apps = client.tool('ufi.app.list')['apps']
                fresh = [a for a in apps if a['instance_id'] not in initial_ids]
                for a in fresh:
                    if a['instance_id'] not in created:
                        created.append(a['instance_id'])
                matches = [a for a in apps if a['module'] == module]
                if matches and (matches[0]['title'].strip() or module == 'ufs'):
                    instance = matches[0]
                    break
                time.sleep(.2)
            assert instance is not None, 'Toolbox did not become identifiable: ' + module
            assert tool('ufi.app.open', {'module': module})['status'] == 'already_running'
            iid = instance['instance_id']
            if module in ('emmc', 'ufs'):
                listing = tool('ufi.settings.list', {'module': module})
                setting = 'clock' if module == 'emmc' else 'link_mode'
                current = tool('ufi.settings.get', {'module': module, 'setting': setting})
                if module == 'emmc':
                    ready_deadline = time.monotonic() + 15
                    while not current['setting']['writable'] and time.monotonic() < ready_deadline:
                        time.sleep(.5)
                        current = tool('ufi.settings.get', {'module': module, 'setting': setting})
                    assert current['setting']['writable'], current
                    old = current['setting']['value']
                    alternate = '24 MHz' if old != '24 MHz' else '12 MHz'
                    arguments = {'module': module, 'instance_id': iid, 'setting': setting}
                    try:
                        changed = tool('ufi.settings.set', dict(arguments, value=alternate, expected_value=old))
                        assert changed['status'] == 'gui_selection_verified'
                    finally:
                        observed = tool('ufi.settings.get', {'module': module, 'setting': setting})
                        value = observed['setting']['value']
                        if value != old:
                            restored = tool('ufi.settings.set', dict(arguments, value=old, expected_value=value))
                            assert restored['status'] == 'gui_selection_verified'
                    voltage = tool('ufi.settings.get', {'module': module, 'setting': 'vcc'})['setting']['value']
                    noop = tool('ufi.settings.set', {'module': module, 'instance_id': iid, 'setting': 'vcc',
                                                   'value': voltage, 'expected_value': voltage})
                    assert noop['status'] == 'unchanged'
                else:
                    assert all(not s['writable'] for s in listing['settings']), 'This probe expects no UFS programmer.'
                    tool('ufi.settings.set', {'module': module, 'instance_id': iid, 'setting': 'link_mode',
                                             'value': 'PWM-GEAR', 'expected_value': current['setting']['value']},
                         error='control_disabled')
                    tool('ufi.settings.get', {'module': module, 'setting': 'vcc'}, error='setting_unavailable')
                tool('ufi.device.identify', {'module': module, 'instance_id': iid}, error='device_not_ready')
            if iid in created:
                assert tool('ufi.app.close', {'instance_id': iid})['status'] == 'closed'
                created.remove(iid)
                tool('ufi.app.close', {'instance_id': iid}, error='stale_instance')
        tool('ufi.device.result', {'request_id': '0' * 32}, error='unknown_request')
        assert client.rpc('ping') == {}
        final = tool('ufi.app.list')['apps']
        assert {a['instance_id'] for a in final} == initial_ids
        report = {'status': 'passed', 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
                  'configuration': configuration, 'executable_sha256': checksum(Path(configuration['command'])),
                  'server': initialized['serverInfo'], 'advertised_tools': len(definitions),
                  'control_tools_exercised': sorted(client.tools), 'clock_gui_restored': True,
                  'voltage_change_dispatched': False, 'identify_dispatched': False,
                  'physical_chip_detected': False, 'hardware_acceptance': False,
                  'events': results, 'rpc_calls': client.calls}
    finally:
        for iid in reversed(created):
            try:
                tool('ufi.app.close', {'instance_id': iid})
            except Exception:
                pass  # Never force-terminate UFI after a failed probe.
        exit_code = client.close()
    assert exit_code == 0
    report['server_exit_code'] = exit_code
    report['stderr_bytes'] = len(client.stderr)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--without-memory', action='store_true', required=True)
    options = parser.parse_args()
    configuration = json.loads(options.config.read_text(encoding='utf-8-sig'))['mcpServers']['ufi-support']
    try:
        report = probe(configuration)
    except Exception as error:
        options.output.write_text(json.dumps({'status': 'failed', 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
            'error': type(error).__name__ + ': ' + str(error), 'configuration': configuration}, indent=2) + '\n', encoding='utf-8')
        raise
    options.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'server', 'advertised_tools',
                       'control_tools_exercised', 'clock_gui_restored', 'hardware_acceptance', 'server_exit_code')}, indent=2))


if __name__ == '__main__':
    main()
