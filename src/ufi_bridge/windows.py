"""Observe the installed executable and its UI; never invoke a control."""
import json
import os
from pathlib import Path

from powershell_io import run_powershell


def discover_root():
    candidates = []
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\ZEGOBIT\UFI\Install') as key:
                    value, _ = winreg.QueryValueEx(key, 'Directory')
                if isinstance(value, bytes):
                    value = value.split(b'\0', 1)[0].decode('latin1')
                if isinstance(value, str):
                    candidates.append(Path(value))
            except OSError:
                pass
    candidates.extend(Path(drive + ':/UFI') for drive in 'CDEFGH')
    return next((p.resolve() for p in candidates if (p / 'UFI.exe').is_file()), None)


SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$exe = __EXE__
$item = Get-Item -LiteralPath $exe
$ufiProcessesFound = @(Get-CimInstance Win32_Process -Filter "Name = 'UFI.exe'")
$procs = @($ufiProcessesFound | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -ieq $exe })
$report = [ordered]@{
 available = $true
 executable_version = $item.VersionInfo.FileVersion
 windows = [Environment]::OSVersion.Version.ToString()
 processes = @($procs | ForEach-Object { @{pid=$_.ProcessId; parent_pid=$_.ParentProcessId} })
 inaccessible_process_count = @($ufiProcessesFound | Where-Object { -not $_.ExecutablePath }).Count
 ui = @()
}
if (__INSPECT__ -and $procs.Count -gt 0) {
 Add-Type -AssemblyName UIAutomationClient
 Add-Type -AssemblyName UIAutomationTypes
 $tree = [System.Windows.Automation.TreeWalker]::ControlViewWalker
 $desktop = [System.Windows.Automation.AutomationElement]::RootElement
 $nodes = New-Object System.Collections.Generic.List[object]
 $timer = [Diagnostics.Stopwatch]::StartNew()
 foreach ($proc in $procs) {
  $condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, [int]$proc.ProcessId)
  $windows = $desktop.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
  foreach ($window in $windows) {
   $queue = New-Object System.Collections.Queue
   $queue.Enqueue(@($window, 0, -1))
   while ($queue.Count -gt 0 -and $nodes.Count -lt __LIMIT__ -and $timer.ElapsedMilliseconds -lt 4000) {
    $entry = $queue.Dequeue(); $element = $entry[0]; $depth = [int]$entry[1]
    try {
     $current = $element.Current
     if ($current.IsPassword) { continue }
     $patterns = @($element.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })
     $nodeId = $nodes.Count
     $nodes.Add(@{id=$nodeId;parent_id=$entry[2];pid=$proc.ProcessId;depth=$depth;
      name=([string]$current.Name).Substring(0,[Math]::Min(300,([string]$current.Name).Length));
      automation_id=$current.AutomationId;class_name=$current.ClassName;
      control_type=$current.ControlType.ProgrammaticName;enabled=$current.IsEnabled;
      patterns=$patterns})
     if ($depth -lt 6) {
      $child = $tree.GetFirstChild($element)
      while ($null -ne $child -and $queue.Count -lt __LIMIT__) {
       $queue.Enqueue(@($child, $depth+1, $nodeId)); $child = $tree.GetNextSibling($child)
      }
     }
    } catch { $report['ui_partial'] = $true }
   }
  }
 }
 $report.ui = @($nodes.ToArray())
 $report['ui_partial'] = [bool]$report['ui_partial'] -or $nodes.Count -ge __LIMIT__ -or $timer.ElapsedMilliseconds -ge 4000
}
$report | ConvertTo-Json -Depth 10 -Compress
'''


def observe(root, inspect=False, max_nodes=150):
    if os.name != 'nt':
        return {'available': False, 'reason': 'windows_required'}
    exe = (Path(root) / 'UFI.exe').resolve()
    if not exe.is_file():
        return {'available': False, 'reason': 'ufi_executable_missing'}
    quoted = "'" + str(exe).replace("'", "''") + "'"
    script = SCRIPT.replace('__EXE__', quoted).replace('__INSPECT__', '$true' if inspect else '$false')
    script = script.replace('__LIMIT__', str(max_nodes))
    failures = []
    output = run_powershell(script, timeout=20, report=failures.append)
    if not output:
        return {'available': False, 'reason': 'observation_failed', 'detail': failures[:1]}
    try:
        result = json.loads(output)
    except (ValueError, TypeError):
        return {'available': False, 'reason': 'invalid_observation'}
    if not isinstance(result, dict):
        return {'available': False, 'reason': 'invalid_observation'}
    result['ui_state'] = ('observed' if result.get('ui') else
                          'not_running' if not result.get('processes') else
                          'no_controls_observed' if inspect else 'not_requested')
    result['ui_control_verified'] = False
    return result
