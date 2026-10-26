"""Read Detect readiness, or explicitly run ONE Identify and follow its stages.

Never changes settings, dismisses dialogs, retries, writes/erases or cancels UFI.
Private output may contain a device fingerprint; do not publish the report.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from check_installed_mcp import Client, checksum


def probe(configuration, output, identify=False, open_if_needed=False, timeout=600):
    client = Client(configuration)
    report = {'status': 'in_progress', 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'configuration': configuration, 'identify_dispatches': 0, 'settings_changes': 0,
              'events': []}
    def save():
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    def tool(name, args=None):
        result = client.tool(name, args)
        report['events'].append({'tool': name, 'result': result})
        save()
        return result
    try:
        info = client.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                        'clientInfo': {'name': 'detect-stage-check', 'version': '1'}})
        report['server_info'] = info['serverInfo']
        report['binary_sha256'] = checksum(Path(configuration['command']))
        client.notify('notifications/initialized')
        definitions = client.rpc('tools/list')['tools']
        assert {'ufi.device.preflight', 'ufi.device.identify', 'ufi.device.result'} <= {t['name'] for t in definitions}
        if open_if_needed:
            opened = tool('ufi.app.open', {'module': 'emmc'})
            if opened['status'] == 'launch_failed':
                raise RuntimeError('UFI reported a native launch error.')
        ready = tool('ufi.device.preflight', {'module': 'emmc'})
        deadline = time.monotonic() + 15
        while ready['reason_code'] in ('device_not_ready', 'control_mapping_unavailable', 'ui_unavailable') and time.monotonic() < deadline:
            time.sleep(.5)
            ready = tool('ufi.device.preflight', {'module': 'emmc'})
        print(json.dumps({'preflight': ready['can_identify'], 'reason': ready['reason_code']}), flush=True)
        if not ready['can_identify']:
            report['status'] = 'blocked'
            return report
        if not identify:
            report['status'] = 'preflight_passed'
            return report
        before = tool('ufi.logs.snapshot', {'source': 'emmc'})
        report['original_before'] = before['id']
        requested = tool('ufi.device.identify', {'module': 'emmc', 'instance_id': ready['instance']['instance_id']})
        report['identify_dispatches'] = 1
        report['request_id'] = requested['request_id']
        print(json.dumps({'identify_requested': requested['request_id']}), flush=True)
        deadline = time.monotonic() + timeout
        last_summary = None
        while time.monotonic() < deadline:
            result = tool('ufi.device.result', {'request_id': requested['request_id']})
            summary = {'phase': result['phase'], 'steps': result['steps'],
                       'product': result['device'].get('product_name'),
                       'bytes': result['capacity'].get('user_area_bytes'),
                       'health': result['health']['status']}
            if summary != last_summary:
                print(json.dumps(summary), flush=True)
                last_summary = summary
            if result['finished']:
                report['status'] = 'passed' if result['phase'] == 'completed' and result['chip_identified'] else result['phase']
                report['final_result'] = result
                after = tool('ufi.logs.snapshot', {'source': 'emmc'})
                report['original_after'] = after['id']
                report['settings_after'] = tool('ufi.settings.list', {'module': 'emmc'})
                return report
            time.sleep(1)
        report['status'] = 'observation_timeout'
        report['device_command_not_retried_or_cancelled'] = True
        return report
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        report['server_exit_code'] = client.close()
        report['stderr_bytes'] = len(client.stderr)
        if report['server_exit_code'] != 0 or report['stderr_bytes']:
            report['status'] = 'failed'
            report['error'] = 'MCP server did not exit cleanly.'
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--identify', action='store_true', help='Explicitly send one Identify after successful preflight.')
    parser.add_argument('--open-if-needed', action='store_true')
    parser.add_argument('--timeout-seconds', type=int, default=600)
    args = parser.parse_args()
    if not 10 <= args.timeout_seconds <= 900:
        parser.error('Observation timeout must be between 10 and 900 seconds.')
    configuration = json.loads(args.config.read_text(encoding='utf-8-sig'))['mcpServers']['ufi-support']
    report = probe(configuration, args.output, args.identify, args.open_if_needed, args.timeout_seconds)
    print(json.dumps({'status': report['status'], 'identify_dispatches': report['identify_dispatches'],
                      'server_exit_code': report['server_exit_code']}))
    return 0 if report['status'] in ('passed', 'preflight_passed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
