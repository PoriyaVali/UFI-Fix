"""UFI-Fix - run the UFI Box software on Windows 11 24H2 / 25H2.

Unofficial compatibility fix.  Not made by, or affiliated with, UFI / zegobit.

WHY IT IS NEEDED
    The UFI loader rewrites the program's import table so that a large part of
    the Windows API calls go through stubs of its copy protection.  Each stub
    parses the first bytes of the real API with a small built-in instruction
    decoder.  That decoder cannot read `movups <mem>, xmm` (opcode 0F 11 ..),
    an SSE instruction Microsoft's compiler started emitting in the prologue of
    several APIs in Windows 11 24H2 - user32!GetClientRect among them.  The
    decoder walks into the middle of an instruction, hands the program a
    garbage pointer, and UFI dies with
        "Access violation at address 01D31877 ... Read of address 1D54378B".

WHAT THIS TOOL DOES
    It starts UFI, finds the protected child process the loader creates, and in
    that process only moves the offending instruction out of the region the
    stub parses: the original instructions are copied to a trampoline and
    replaced by a jump.  The API behaves exactly as before.
    Nothing on disk is modified, no Windows file is touched, nothing outside
    the UFI process is changed, and the UFI protection and licence are left
    completely alone.

USAGE
    UFI-Fix.exe                 open the window
    UFI-Fix.exe /emmc           start the eMMC ToolBox with the fix (no window)
    UFI-Fix.exe /ufs            start the UFS ToolBox with the fix
    UFI-Fix.exe /android        start the Android ToolBox with the fix
    UFI-Fix.exe /chip           start the CHIP Programmer with the fix
    UFI-Fix.exe --link          link the UFI shortcuts to this tool
    UFI-Fix.exe --restore       put the shortcuts back the way they were
"""
import ctypes
import ctypes.wintypes as wt
import json
import re
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import winreg

from capstone import CS_ARCH_X86, CS_MODE_32, Cs
from powershell_io import run_powershell
from app_version import VERSION, BUILD_CHANNEL
from release_updates import GATE as UPDATE_GATE, UpdateError, RELEASES_URL, download_installer
from install_bundle import connection_config, should_relink, normalize_shortcut_path

APP = 'UFI-Fix'
DONATE_ADDRESS = 'TPWxvw8juAByLMjHsXpnEnQmq2N9UT5CUg'
DONATE_COIN = 'TRX  (TRON network)'
CONFIG_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), APP)
CONFIG = os.path.join(CONFIG_DIR, 'config.json')
SYSWOW64 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'SysWOW64')
TOOLBOXES = [('/emmc', 'eMMC ToolBox'), ('/ufs', 'UFS ToolBox'),
             ('/android', 'Android ToolBox'), ('/chip', 'CHIP Prog')]
CREATE_NO_WINDOW = 0x08000000

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
md = Cs(CS_ARCH_X86, CS_MODE_32)

k32.OpenProcess.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
ntdll.NtSuspendProcess.argtypes = [wt.HANDLE]
ntdll.NtSuspendProcess.restype = wt.LONG
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.restype = wt.LONG


# ----------------------------------------------------------------- settings
def res(name):
    """a file that was bundled with the exe (or sits next to the script)"""
    base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def load_config():
    try:
        with open(CONFIG, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def log_path():
    return os.path.join(CONFIG_DIR, 'UFI-Fix.log')


def log(msg):
    line = '%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(log_path(), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    return line


# --------------------------------------------------- finding UFI on this PC
def _reg_install_dir():
    """HKCU\\SOFTWARE\\ZEGOBIT\\UFI\\Install -> Directory (ASCII, NUL terminated)"""
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r'SOFTWARE\ZEGOBIT\UFI\Install') as k:
                val, _ = winreg.QueryValueEx(k, 'Directory')
            if isinstance(val, bytes):
                val = val.split(b'\0')[0].decode('latin1')
            if val:
                return val.rstrip('\\')
        except OSError:
            continue
    return None


def _running_ufi_dir():
    for pid in ufi_processes():
        h = k32.OpenProcess(0x1000, False, pid)         # QUERY_LIMITED_INFORMATION
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(600)
        size = wt.DWORD(600)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        if ok and buf.value:
            return os.path.dirname(buf.value)
    return None


def _shortcut_ufi_dir():
    for path, target, _args in scan_shortcuts(None):
        name = os.path.basename(target).lower()
        if name == 'ufi.exe':
            return os.path.dirname(target)
    return None


def find_ufi_dir(preferred=None):
    """return (directory, how it was found) or (None, None)"""
    candidates = []
    if preferred:
        candidates.append((preferred, 'chosen by you'))
    cfg = load_config()
    if cfg.get('ufi_dir'):
        candidates.append((cfg['ufi_dir'], 'saved setting'))
    d = _reg_install_dir()
    if d:
        candidates.append((d, 'registry (ZEGOBIT\\UFI\\Install)'))
    d = _running_ufi_dir()
    if d:
        candidates.append((d, 'running UFI process'))
    try:
        d = _shortcut_ufi_dir()
        if d:
            candidates.append((d, 'existing shortcut'))
    except Exception:
        pass
    for drive in 'CDEFGH':
        candidates.append(('%s:\\UFI' % drive, 'common location'))
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')):
        if base:
            candidates.append((os.path.join(base, 'UFI'), 'Program Files'))
    for path, how in candidates:
        if path and os.path.exists(os.path.join(path, 'UFI.exe')):
            return os.path.normpath(path), how
    return None, None


def tool_home(ufi_dir):
    """where the installed copy of this tool lives (falls back if UFI dir is read-only)"""
    candidate = os.path.join(ufi_dir, 'fix')
    try:
        os.makedirs(candidate, exist_ok=True)
        probe = os.path.join(candidate, '.w')
        with open(probe, 'w') as f:
            f.write('x')
        os.remove(probe)
        return candidate
    except OSError:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        return CONFIG_DIR


# ---------------------------------------------------------------- processes
class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)), ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD), ('szExeFile', ctypes.c_char * 260)]


def ufi_processes():
    out = {}
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == wt.HANDLE(-1).value:
        return out
    pe = PE32()
    pe.dwSize = ctypes.sizeof(pe)
    ok = k32.Process32First(snap, ctypes.byref(pe))
    while ok:
        if pe.szExeFile.lower() == b'ufi.exe':
            out[pe.th32ProcessID] = pe.th32ParentProcessID
        ok = k32.Process32Next(snap, ctypes.byref(pe))
    k32.CloseHandle(wt.HANDLE(snap))
    return out


class Target(object):
    def __init__(self, pid):
        self.pid = pid
        # NtSuspendProcess/NtResumeProcess require PROCESS_SUSPEND_RESUME
        # (0x0800). The old 0x438 handle silently failed to suspend anything.
        self.h = k32.OpenProcess(0xC38, False, pid)
        if not self.h:
            raise OSError('Cannot open UFI for compatibility patching: %s' % ctypes.get_last_error())
        p = ctypes.c_void_p()
        ntdll.NtQueryInformationProcess(wt.HANDLE(self.h), 26, ctypes.byref(p), ctypes.sizeof(p), None)
        self.peb32 = p.value or 0

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]

    def write(self, addr, data):
        old = wt.DWORD()
        k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(data), 0x40, ctypes.byref(old))
        got = ctypes.c_size_t()
        ok = k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), data, len(data), ctypes.byref(got))
        k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(data), old.value, ctypes.byref(old))
        return bool(ok) and got.value == len(data)

    def alloc(self, size):
        return k32.VirtualAllocEx(self.h, None, size, 0x3000, 0x40)

    def alive(self):
        c = wt.DWORD()
        k32.GetExitCodeProcess(self.h, ctypes.byref(c))
        return c.value == 259

    def suspend(self):
        status = ntdll.NtSuspendProcess(wt.HANDLE(self.h))
        if status < 0:
            raise OSError('NtSuspendProcess failed: NTSTATUS 0x%08X; no patch dispatched' % (status & 0xffffffff))

    def resume(self):
        status = ntdll.NtResumeProcess(wt.HANDLE(self.h))
        if status < 0:
            raise OSError('NtResumeProcess failed: NTSTATUS 0x%08X; inspect the UFI process' % (status & 0xffffffff))

    def modules(self):
        out = {}
        if not self.peb32:
            return out
        raw = self.read(self.peb32 + 0x0c, 4)
        if not raw:
            return out
        ldr = struct.unpack('<I', raw)[0]
        if not ldr:
            return out
        head = ldr + 0x0c
        raw = self.read(head, 4)
        if not raw:
            return out
        cur = struct.unpack('<I', raw)[0]
        for _ in range(500):
            if not cur or cur == head:
                break
            ent = self.read(cur, 0x40)
            if not ent:
                break
            nxt = struct.unpack_from('<I', ent, 0)[0]
            base = struct.unpack_from('<I', ent, 0x18)[0]
            ln, _mx, buf = struct.unpack_from('<HHI', ent, 0x2c)
            if base and buf and ln:
                nm = self.read(buf, ln)
                if nm:
                    out[nm.decode('utf-16-le', 'ignore').lower()] = base
            cur = nxt
        return out


# ------------------------------------------------------------------ the fix
def exports_from_disk(path):
    try:
        with open(path, 'rb') as f:
            b = f.read()
    except OSError:
        return []
    try:
        pe = struct.unpack_from('<I', b, 0x3c)[0]
        if b[pe:pe + 4] != b'PE\0\0':
            return []
        opt = pe + 24
        optsz = struct.unpack_from('<H', b, pe + 20)[0]
        if struct.unpack_from('<H', b, opt)[0] != 0x10b:
            return []
        sections = []
        s = opt + optsz
        for _ in range(struct.unpack_from('<H', b, pe + 6)[0]):
            vs, va, rs, ra = struct.unpack_from('<IIII', b, s + 8)
            sections.append((va, max(vs, rs), ra))
            s += 40

        def off(rva):
            for va, size, ra in sections:
                if va <= rva < va + size:
                    return rva - va + ra
            return None

        edir = struct.unpack_from('<I', b, opt + 96)[0]
        if not edir:
            return []
        eo = off(edir)
        if eo is None:
            return []
        count = struct.unpack_from('<I', b, eo + 24)[0]
        fa, na, oa = (off(struct.unpack_from('<I', b, eo + k)[0]) for k in (28, 32, 36))
        if None in (fa, na, oa):
            return []
        out, seen = [], set()
        for i in range(count):
            no = off(struct.unpack_from('<I', b, na + 4 * i)[0])
            if no is None:
                continue
            name = b[no:b.index(b'\0', no)].decode('latin1')
            rva = struct.unpack_from('<I', b, fa + 4 * struct.unpack_from('<H', b, oa + 2 * i)[0])[0]
            if rva in seen:
                continue
            seen.add(rva)
            o = off(rva)
            if o:
                out.append((name, rva, b[o:o + 0x60]))
        return out
    except (struct.error, ValueError, IndexError):
        return []


RELATIVE = {'jmp', 'call', 'je', 'jne', 'jz', 'jnz', 'ja', 'jae', 'jb', 'jbe', 'jg', 'jge',
            'jl', 'jle', 'js', 'jns', 'jo', 'jno', 'jp', 'jnp', 'jcxz', 'jecxz', 'loop',
            'loope', 'loopne'}
BROKEN = (b'\x0f\x11', b'\x0f\x10')


def plan(code):
    ins = [i for i in md.disasm(code, 0) if i.address < 0x60]
    for idx, i in enumerate(ins):
        if i.address >= 0x50:
            return None
        if i.bytes[:2] in BROKEN:
            start, end, j = i.address, i.address + i.size, idx
            while end - start < 5 and j + 1 < len(ins):
                j += 1
                end = ins[j].address + ins[j].size
            if end - start < 5:
                return None
            for k in range(idx, j + 1):
                if ins[k].mnemonic in RELATIVE:
                    return None
            return start, end
    return None


def patch_module(t, name, base, path, report):
    fixed = 0
    for export, rva, disk in exports_from_disk(path):
        if plan(disk) is None:
            continue
        live = t.read(base + rva, 0x60)
        if not live:
            continue
        p = plan(live)
        if p is None:
            continue
        start, end = p
        addr = base + rva + start
        orig = live[start:end]
        tramp = t.alloc(len(orig) + 5)
        if not tramp:
            continue
        back = base + rva + end
        if not t.write(tramp, orig + b'\xe9' + struct.pack('<i', back - (tramp + len(orig) + 5))):
            continue
        jump = b'\xe9' + struct.pack('<i', tramp - (addr + 5)) + b'\x90' * (len(orig) - 5)
        if t.write(addr, jump):
            fixed += 1
            report.append('%s!%s +0x%x' % (name, export, start))
    return fixed


def run_ufi(ufi_dir, arg, emit=None):
    """launch UFI and keep fixing its process until it closes"""
    def say(msg):
        line = log(msg)
        if emit:
            emit(line[11:])          # drop the date, keep the time
    exe = os.path.join(ufi_dir, 'UFI.exe')
    if not os.path.exists(exe):
        say('UFI.exe not found in %s' % ufi_dir)
        return 1
    try:
        UPDATE_GATE.require_current()
    except UpdateError as error:
        say('%s: %s' % (error.code, error))
        say('Open UFI-Fix to check/install the required update: ' + RELEASES_URL)
        return 3
    say('starting %s %s' % (exe, arg))
    try:
        proc = subprocess.Popen([exe, arg], cwd=ufi_dir)
    except OSError as e:
        say('cannot start UFI: %s' % e)
        return 1

    child, t0 = None, time.time()
    while time.time() - t0 < 25 and child is None:
        for pid, parent in ufi_processes().items():
            if parent == proc.pid and pid != proc.pid:
                child = pid
                break
    if child is None:
        say('no protected process appeared - UFI may already be running')
        return 0

    try:
        t = Target(child)
    except OSError as error:
        say('compatibility error: %s' % error)
        return 1
    say('protected process %d found in %.1fs - applying the fix' % (child, time.time() - t0))
    done, total = set(), 0
    while t.alive():
        try:
            # Suspend before the module snapshot as well as before writes.
            # Do not resume after a failed suspend, or write into a live thread.
            t.suspend()
            try:
                todo = []
                for name, base in t.modules().items():
                    if name in done or not name.endswith('.dll'):
                        continue
                    done.add(name)
                    p = os.path.join(SYSWOW64, name)
                    if os.path.exists(p):
                        todo.append((name, base, p))
                for name, base, p in todo:
                    report = []
                    n = patch_module(t, name, base, p, report)
                    if n:
                        total += n
                        for line in report:
                            log('    ' + line)
                        say('  fixed %d in %s' % (n, name))
            finally:
                t.resume()
        except OSError as error:
            say('compatibility error: %s' % error)
            k32.CloseHandle(wt.HANDLE(t.h))
            return 1
        time.sleep(0.005 if time.time() - t0 < 10 else 1.0)
    say('UFI closed - %d function(s) were fixed in %d module(s)' % (total, len(done)))
    k32.CloseHandle(wt.HANDLE(t.h))
    return 0


# ---------------------------------------------------------------- shortcuts
PS_SCAN = r'''
$sh = New-Object -ComObject WScript.Shell
$dirs = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Shortcuts'),
  [Environment]::GetFolderPath('CommonDesktopDirectory'),
  [Environment]::GetFolderPath('Programs'),
  [Environment]::GetFolderPath('CommonPrograms'),
  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch'),
  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'),
  'UFIDIR'
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
foreach ($d in $dirs) {
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $l = $sh.CreateShortcut($_.FullName)
      "$($_.FullName)|$($l.TargetPath)|$($l.Arguments)"
    } catch {}
  }
}
'__UFI_SHORTCUT_SCAN_COMPLETE__'
'''


def scan_shortcuts(ufi_dir, strict=False):
    """[(lnk path, target, arguments)] for every shortcut we can see"""
    script = PS_SCAN.replace('UFIDIR', ufi_dir.replace("'", "''") if ufi_dir else 'C:\\UFI')
    out = run_powershell(script, timeout=90, report=log)
    if strict and '__UFI_SHORTCUT_SCAN_COMPLETE__' not in out.splitlines():
        raise RuntimeError('Shortcut scan could not be verified; Undo is incomplete.')
    rows = []
    seen = set()
    for line in out.splitlines():
        parts = line.strip().split('|')
        if len(parts) >= 3 and parts[0].lower().endswith('.lnk'):
            key = normalize_shortcut_path(parts[0])
            if key not in seen:
                seen.add(key)
                rows.append((parts[0], parts[1], '|'.join(parts[2:])))
    return rows


def ufi_shortcuts(ufi_dir, tool=None):
    """(linked, unlinked) shortcut lists for this UFI installation"""
    ufi_exe = normalize_shortcut_path(os.path.join(ufi_dir, 'UFI.exe'))
    linked, unlinked = [], []
    toolbox_args = {a for a, _n in TOOLBOXES}
    tool = tool or load_config().get('tool') or (sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
    for path, target, args in scan_shortcuts(ufi_dir):
        t = normalize_shortcut_path(target)
        if t == ufi_exe:
            unlinked.append((path, args))
        elif ((args or '').strip().lower() in toolbox_args
              and should_relink(ufi_dir, target, args, tool, [load_config().get('tool')])):
            # our own UFI-Fix shortcut has no toolbox argument - don't count it
            linked.append((path, args))
    return linked, unlinked


def link_shortcuts(ufi_dir, tool, emit=print):
    UPDATE_GATE.require_current()
    ufi_exe = os.path.join(ufi_dir, 'UFI.exe')
    changed = 0
    for path, target, args in scan_shortcuts(ufi_dir):
        if not should_relink(ufi_dir, target, args, tool, [load_config().get('tool')]):
            continue
        script = (
            '$sh=New-Object -ComObject WScript.Shell;'
            '$l=$sh.CreateShortcut(%s);'
            'if(-not (Test-Path %s)){Copy-Item -LiteralPath %s -Destination %s};'
            '$a=$l.Arguments;$l.TargetPath=%s;$l.Arguments=$a;$l.WorkingDirectory=%s;$l.Save();"ok"'
            % (_q(path), _q(path + '.orig'), _q(path), _q(path + '.orig'), _q(tool), _q(ufi_dir + '\\')))
        if _ps(script) == 'ok':
            changed += 1
            emit('  linked: %s  (%s)' % (os.path.basename(path), args or 'no argument'))
    save_config(dict(load_config(), ufi_dir=ufi_dir, tool=tool))
    emit('%d shortcut(s) now start UFI through the fix.' % changed)
    return changed


def restore_shortcuts(ufi_dir, tool, emit=print):
    """Restore owned toolbox links, including copied links without .orig files.

    Manager shortcuts and unrelated executables are excluded. A backup must
    identify the same vendor and toolbox; all backups are retained. Success
    requires both per-link readback and a second complete shortcut scan.
    """
    report = dict(restored=0, repaired=0, already_original=0, failed=0, remaining=None, success=False)
    ufi_dir = os.path.realpath(os.path.abspath(ufi_dir))
    vendor = os.path.join(ufi_dir, 'UFI.exe')
    toolbox_args = {arg for arg, _ in TOOLBOXES}
    previous = load_config().get('tool')
    tool = tool or previous or (sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
    same = lambda left, right: normalize_shortcut_path(left) == normalize_shortcut_path(right)
    owned = lambda target, args: ((args or '').strip().lower() in toolbox_args
                                 and not same(target, vendor)
                                 and should_relink(ufi_dir, target, args, tool, [previous]))
    try:
        if not os.path.isfile(vendor):
            raise RuntimeError('Original UFI.exe was not found; no shortcut was changed.')
        rows = scan_shortcuts(ufi_dir, strict=True)
        for path, target, args in rows:
            if same(target, vendor):
                report['already_original'] += 1
                continue
            if not owned(target, args):
                continue
            # WScript cannot load a .lnk.orig directly. Inspect a temporary .lnk
            # copy, then restore only an original for this exact toolbox.
            script = r'''# UFI_RESTORE_LINK_V1
$ErrorActionPreference='Stop'
$ufiPath=LINK_PATH
$ufiVendor=VENDOR_PATH
$ufiExpectedTarget=EXPECTED_TARGET
$ufiArgs=EXPECTED_ARGS
$ufiWork=WORK_DIRECTORY
$ufiTemporary=$null
$ufiResult=@{success=$false;method=$null;warning=$null}
try {
 $ufiShell=New-Object -ComObject WScript.Shell
 $ufiLink=$ufiShell.CreateShortcut($ufiPath)
 if($ufiLink.TargetPath -ine $ufiExpectedTarget -or $ufiLink.Arguments -cne $ufiArgs){throw 'Shortcut changed during Undo; retry.'}
 $ufiBackup=$ufiPath+'.orig'
 $ufiValid=$false
 if(Test-Path -LiteralPath $ufiBackup -PathType Leaf){
  try {
   $ufiTemporary=Join-Path ([IO.Path]::GetTempPath()) ('UFI-restore-'+[Guid]::NewGuid().ToString('N')+'.lnk')
   Copy-Item -LiteralPath $ufiBackup -Destination $ufiTemporary
   $ufiOriginal=$ufiShell.CreateShortcut($ufiTemporary)
   $ufiValid=($ufiOriginal.TargetPath -ieq $ufiVendor -and $ufiOriginal.Arguments.Trim().ToLowerInvariant() -eq $ufiArgs.Trim().ToLowerInvariant())
   if(-not $ufiValid){$ufiResult.warning='Backup is not the original for this toolbox; it was kept.'}
  } catch {$ufiResult.warning='Backup could not be validated; it was kept.'}
 }
 if($ufiValid){
  Copy-Item -LiteralPath $ufiBackup -Destination $ufiPath -Force
  $ufiResult.method='restored'
 } else {
  $ufiLink.TargetPath=$ufiVendor
  $ufiLink.Arguments=$ufiArgs
  $ufiLink.WorkingDirectory=$ufiWork
  $ufiLink.IconLocation=$ufiVendor+',0'
  $ufiLink.Save()
  $ufiResult.method='repaired'
 }
 $ufiCheck=$ufiShell.CreateShortcut($ufiPath)
 if($ufiCheck.TargetPath -ine $ufiVendor -or $ufiCheck.Arguments.Trim().ToLowerInvariant() -ne $ufiArgs.Trim().ToLowerInvariant()){
  throw 'Shortcut readback did not match the original UFI and toolbox.'
 }
 $ufiResult.success=$true
} catch {$ufiResult.error=$_.Exception.Message}
finally {if($ufiTemporary -and (Test-Path -LiteralPath $ufiTemporary)){Remove-Item -LiteralPath $ufiTemporary -Force}}
$ufiResult | ConvertTo-Json -Compress
'''
            # Substitute whole placeholder tokens, never shell-interpolated data.
            values = {key: _q(value) for key, value in (
                ('LINK_PATH', path), ('VENDOR_PATH', vendor), ('EXPECTED_TARGET', target),
                ('EXPECTED_ARGS', args or ''), ('WORK_DIRECTORY', ufi_dir))}
            script = re.sub(r'\b(?:LINK_PATH|VENDOR_PATH|EXPECTED_TARGET|EXPECTED_ARGS|WORK_DIRECTORY)\b',
                            lambda match: values[match[0]], script)
            try:
                result = json.loads(_ps(script))
                if not isinstance(result, dict) or not result.get('success') or result.get('method') not in ('restored', 'repaired'):
                    raise RuntimeError(result.get('error', 'Shortcut restoration was not confirmed.') if isinstance(result, dict) else 'Invalid restoration response.')
                report[result['method']] += 1
                emit('  %s: %s' % (result['method'], os.path.basename(path)))
                if result.get('warning'):
                    emit('  ' + result['warning'])
            except (OSError, ValueError, RuntimeError) as error:
                report['failed'] += 1
                emit('  could not restore %s: %s' % (path, error))
        remaining = scan_shortcuts(ufi_dir, strict=True)
        report['remaining'] = sum(owned(target, args) for _, target, args in remaining)
        report['success'] = report['failed'] == 0 and report['remaining'] == 0
    except (OSError, RuntimeError) as error:
        report['failed'] += 1
        emit('Undo verification failed: %s' % error)
    emit('Undo %s: %d restored, %d repaired, %d already original, %d failed, %s still linked.' % (
        'verified' if report['success'] else 'incomplete', report['restored'], report['repaired'],
        report['already_original'], report['failed'], report['remaining'] if report['remaining'] is not None else 'unknown'))
    return report


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def _ps(script):
    return run_powershell(script, timeout=60, report=log).strip()


UNINSTALL_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\UFI-Fix'


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(args):
    """re-run this exe elevated; returns True if the request was made"""
    exe = sys.executable if getattr(sys, 'frozen', False) else sys.executable
    params = ' '.join('"%s"' % a for a in ([] if getattr(sys, 'frozen', False) else [os.path.abspath(__file__)]) + list(args))
    rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', exe, params, None, 1)
    return rc > 32


def remove_everything():
    """restore the UFI shortcuts and delete everything this tool installed"""
    lines = []
    d, _ = find_ufi_dir()
    if d:
        result = restore_shortcuts(d, '', lines.append)
        if not result['success']:
            ctypes.windll.user32.MessageBoxW(None, 'Shortcut restoration is incomplete. UFI-Fix was kept.\r\n\r\n' + '\r\n'.join(lines[-12:]), APP, 0x10)
            return 1
    for folder in (os.path.join(os.environ.get('PUBLIC', ''), 'Desktop'),
                   os.path.join(os.path.expanduser('~'), 'Desktop'),
                   os.path.join(os.environ.get('ProgramData', ''),
                                r'Microsoft\Windows\Start Menu\Programs')):
        for name in ('UFI-Fix.lnk',):
            p = os.path.join(folder, name)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    lines.append('removed %s' % p)
                except OSError:
                    pass
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY)
        lines.append('removed the Apps & features entry')
    except OSError:
        pass
    # Keep user settings, source snapshots, edited drafts and audit history.
    lines.append('kept your settings, logs and AI support history at %s' % CONFIG_DIR)
    here = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
    # a running exe cannot delete itself - let cmd do it a moment later
    if os.path.basename(here).lower() == 'ufi-fix':
        subprocess.Popen('cmd /c ping 127.0.0.1 -n 4 >nul & rmdir /s /q "%s"' % here,
                         shell=True, creationflags=CREATE_NO_WINDOW)
        lines.append('removing %s' % here)
    ctypes.windll.user32.MessageBoxW(
        None, 'UFI-Fix has been removed.\r\n\r\n' + '\r\n'.join(lines[-12:]) +
              '\r\n\r\nYour UFI shortcuts start UFI.exe directly again.', APP, 0x40)
    return 0


def windows_build():
    v = _ps('(Get-CimInstance Win32_OperatingSystem).Version + "." + '
            '(Get-ItemProperty "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion").UBR + " " + '
            '(Get-ItemProperty "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion").DisplayVersion')
    return v or 'unknown'


# ---------------------------------------------------------------------- GUI
BG = '#12161d'
CARD = '#1b2029'
FG = '#e7ecf3'
MUTED = '#8c97a8'
ACCENT = '#3b82f6'
OK = '#22c55e'
WARN = '#f59e0b'
BAD = '#ef4444'


def make_dpi_aware():
    """crisp text on scaled displays; returns the scale factor (1.0 = 100%)"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)     # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return 1.0
    try:
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)    # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, dc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


class Gui(object):
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox

        self.scale = make_dpi_aware()
        self.root = tk.Tk()
        self.root.title('%s %s - test build' % (APP, VERSION))
        self.root.configure(bg=BG)
        self.root.tk.call('tk', 'scaling', self.scale * 96.0 / 72.0)
        width = min(int(780 * self.scale), self.root.winfo_screenwidth() - 40)
        height = min(int(900 * self.scale), self.root.winfo_screenheight() - 80)
        self.root.geometry('%dx%d' % (width, height))
        self.root.minsize(min(int(720 * self.scale), width), min(int(620 * self.scale), height))
        try:
            self.root.iconbitmap(default=self._icon())
        except Exception:
            pass

        self.ufi_dir, self.how = find_ufi_dir()
        self.tool = self._tool_path()
        self.busy = False
        self.updating = False
        self.checking_update = False
        self.update_state = UPDATE_GATE.peek()
        self.launch_buttons = []

        self._build()
        self.refresh()

    # ---------------------------------------------------------- appearance
    def _icon(self):
        base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(base, 'icon.ico')
        return p if os.path.exists(p) else ''

    def _card(self, parent, pad=14):
        f = self.tk.Frame(parent, bg=CARD, highlightbackground='#262d3a', highlightthickness=1)
        f.pack(fill='x', padx=16, pady=(0, 12))
        inner = self.tk.Frame(f, bg=CARD)
        inner.pack(fill='x', padx=pad, pady=pad)
        return inner

    def _label(self, parent, text, color=FG, size=10, bold=False, **kw):
        return self.tk.Label(parent, text=text, bg=kw.pop('bg', CARD), fg=color,
                             font=('Segoe UI', size, 'bold' if bold else 'normal'),
                             anchor='w', justify='left', **kw)

    def _button(self, parent, text, cmd, kind='normal', width=None):
        colors = {'normal': ('#242c3a', FG), 'primary': (ACCENT, '#ffffff'),
                  'quiet': (CARD, MUTED), 'danger': ('#3a2430', '#ffb4b4')}
        bg, fg = colors[kind]
        b = self.tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                           activebackground=bg, activeforeground=fg,
                           relief='flat', bd=0, cursor='hand2',
                           font=('Segoe UI', 10, 'bold' if kind == 'primary' else 'normal'),
                           padx=14, pady=9, highlightthickness=0)
        if width:
            b.configure(width=width)
        return b

    # -------------------------------------------------------------- layout
    def _build(self):
        tk = self.tk
        # anchored to the bottom and packed first: never pushed off the window
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(side='bottom', fill='x', padx=16, pady=(8, 12))
        tk.Label(foot, text='Unofficial fix - not made by UFI / zegobit. Nothing on disk is '
                            'modified; the fix lives only in the running UFI process.',
                 bg=BG, fg=MUTED, font=('Segoe UI', 8), wraplength=int(660 * self.scale),
                 justify='left').pack(fill='x')
        self._donate_card()

        # Keep the added AI card accessible on smaller/high-DPI desktops.
        scroll_area = tk.Frame(self.root, bg=BG)
        scroll_area.pack(fill='both', expand=True)
        canvas = tk.Canvas(scroll_area, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(scroll_area, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        content = tk.Frame(canvas, bg=BG)
        content_window = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(content_window, width=event.width))

        head = tk.Frame(content, bg=BG)
        head.pack(fill='x', padx=16, pady=(16, 12))
        tk.Label(head, text=APP, bg=BG, fg=FG, font=('Segoe UI', 20, 'bold')).pack(side='left')
        tk.Label(head, text='  v%s - compatibility fix + AI support' % VERSION,
                 bg=BG, fg=MUTED, font=('Segoe UI', 10)).pack(side='left', pady=(10, 0))
        self.dot = tk.Label(head, text='\u25cf', bg=BG, fg=MUTED, font=('Segoe UI', 16))
        self.dot.pack(side='right', pady=(4, 0))
        self.dot_text = tk.Label(head, text='', bg=BG, fg=MUTED, font=('Segoe UI', 10))
        self.dot_text.pack(side='right', padx=(0, 8), pady=(6, 0))

        c = self._card(content)
        self._label(c, 'Required updates', MUTED, 9).pack(fill='x')
        self.update_label = self._label(c, 'Checking the official release before enabling operations...', WARN, 9)
        self.update_label.configure(wraplength=int(620 * self.scale), justify='left')
        self.update_label.pack(fill='x', pady=(4, 6))
        row = tk.Frame(c, bg=CARD)
        row.pack(fill='x')
        self.update_button = self._button(row, 'Update now', self.update_now, 'primary')
        self.update_button.pack(side='left')
        self._button(row, 'Retry check', lambda: self.check_updates(force=True)).pack(side='left', padx=(8, 0))

        # --- where UFI is
        c = self._card(content)
        self._label(c, 'UFI installation folder', MUTED, 9).pack(fill='x')
        row = tk.Frame(c, bg=CARD)
        row.pack(fill='x', pady=(4, 0))
        self.path_var = tk.StringVar(value=self.ufi_dir or 'not found')
        e = tk.Entry(row, textvariable=self.path_var, width=1, bg='#0e1219', fg=FG, relief='flat',
                     insertbackground=FG, font=('Consolas', 10), highlightthickness=1,
                     highlightbackground='#2b3342', highlightcolor=ACCENT)
        e.pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 8))
        self._button(row, 'Browse...', self.browse).pack(side='left')
        self.how_label = self._label(c, '', MUTED, 9)
        self.how_label.pack(fill='x', pady=(6, 0))

        # --- toolboxes
        c = self._card(content)
        self._label(c, 'Start a programmer  (the fix is applied automatically)', MUTED, 9).pack(fill='x')
        grid = tk.Frame(c, bg=CARD)
        grid.pack(fill='x', pady=(8, 0))
        for i, (arg, name) in enumerate(TOOLBOXES):
            b = self._button(grid, name, lambda a=arg: self.launch(a), 'primary')
            self.launch_buttons.append(b)
            b.grid(row=i // 2, column=i % 2, sticky='ew', padx=(0, 8) if i % 2 == 0 else (0, 0), pady=4)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # --- shortcuts
        c = self._card(content)
        self._label(c, 'Desktop and Start menu shortcuts', MUTED, 9).pack(fill='x')
        self.sc_label = self._label(c, '', FG, 10)
        self.sc_label.pack(fill='x', pady=(4, 8))
        row = tk.Frame(c, bg=CARD)
        row.pack(fill='x')
        self.link_btn = self._button(row, 'Link shortcuts to the fix', lambda: self.job(self.do_link))
        self.link_btn.pack(side='left', padx=(0, 8))
        self._button(row, 'Undo - restore originals', lambda: self.job(self.do_restore), 'danger').pack(side='left', padx=(0, 8))
        self._button(row, 'Open log', self.open_log, 'quiet').pack(side='right')

        # --- AI support
        c = self._card(content)
        self._label(c, 'AI support', MUTED, 9).pack(fill='x')
        self._label(c, 'Read logs and create edited copies while keeping the originals.', FG, 10).pack(fill='x', pady=(3, 2))
        self._label(c, 'API: open/close apps, inspect settings and request Identify on supported controls.', MUTED, 9).pack(fill='x')
        self._button(c, 'AI connection settings...', self.ai_connection).pack(anchor='w', pady=(7, 0))

        # --- activity
        c = self._card(content)
        self._label(c, 'Activity', MUTED, 9).pack(fill='x')
        box = tk.Frame(c, bg=CARD)
        box.pack(fill='both', expand=True, pady=(6, 0))
        # width=1: let the widget take the width it is given instead of asking
        # for 80 columns, which would push the whole window wider than the screen
        self.text = tk.Text(box, width=1, height=9, bg='#0e1219', fg='#b9c4d4', relief='flat',
                            font=('Consolas', 9), wrap='word', highlightthickness=1,
                            highlightbackground='#2b3342', padx=10, pady=8)
        sb = tk.Scrollbar(box, command=self.text.yview, bg=CARD, relief='flat',
                          troughcolor=CARD, activebackground='#2b3342')
        self.text.configure(yscrollcommand=sb.set, state='disabled')
        self.text.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')


    # -------------------------------------------------------------- donate
    def _donate_card(self):
        """support line: TRON logo + address that copies itself and shows a QR"""
        tk = self.tk
        f = tk.Frame(self.root, bg=CARD, highlightbackground='#262d3a', highlightthickness=1)
        f.pack(side='bottom', fill='x', padx=16, pady=(0, 10))
        inner = tk.Frame(f, bg=CARD)
        inner.pack(fill='x', padx=14, pady=12)

        tk.Label(inner, text='If this helped you, a small donation is welcome - entirely optional',
                 bg=CARD, fg=MUTED, font=('Segoe UI', 9), anchor='w').pack(fill='x')

        row = tk.Frame(inner, bg=CARD)
        row.pack(fill='x', pady=(6, 0))

        self.tron_img = None
        try:
            self.tron_img = tk.PhotoImage(file=res('tron.png'))
            tk.Label(row, image=self.tron_img, bg=CARD).pack(side='left', padx=(0, 8))
        except Exception:
            c = tk.Canvas(row, width=20, height=20, bg=CARD, highlightthickness=0)
            c.create_oval(1, 1, 19, 19, fill='#ec0928', outline='')
            c.create_polygon(4, 6, 17, 8, 9, 17, fill='white', outline='')
            c.pack(side='left', padx=(0, 8))

        tk.Label(row, text=DONATE_COIN, bg=CARD, fg='#ff6b7a',
                 font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(0, 10))

        self.addr_lbl = tk.Label(row, text=DONATE_ADDRESS, bg=CARD, fg=ACCENT,
                                 font=('Consolas', 10, 'underline'), cursor='hand2')
        self.addr_lbl.pack(side='left')
        self.addr_lbl.bind('<Button-1>', lambda _e: self.donate_click())

        self.copy_hint = tk.Label(row, text='click to copy + show QR', bg=CARD, fg=MUTED,
                                  font=('Segoe UI', 8))
        self.copy_hint.pack(side='left', padx=(10, 0))

    def donate_click(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(DONATE_ADDRESS)
            self.root.update_idletasks()
            self.copy_hint.configure(text='copied to clipboard', fg=OK)
            self.root.after(4000, lambda: self.copy_hint.configure(
                text='click to copy + show QR', fg=MUTED))
        except Exception:
            pass
        self.show_qr()

    def show_qr(self):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title('Donate - %s' % DONATE_COIN)
        win.configure(bg=BG)
        win.resizable(False, False)
        try:
            win.iconbitmap(self._icon())
        except Exception:
            pass

        try:
            import qrcode
            q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
            q.add_data(DONATE_ADDRESS)
            q.make(fit=True)
            matrix = q.get_matrix()
        except Exception as e:
            tk.Label(win, text='could not build the QR code: %s' % e, bg=BG, fg=BAD,
                     font=('Segoe UI', 10), padx=20, pady=20).pack()
            matrix = None

        if matrix:
            n = len(matrix)
            box = max(4, int(260 * self.scale) // n)
            size = n * box
            cv = tk.Canvas(win, width=size, height=size, bg='white', highlightthickness=0)
            cv.pack(padx=int(24 * self.scale), pady=(int(24 * self.scale), int(12 * self.scale)))
            for y, rowv in enumerate(matrix):
                for x, on in enumerate(rowv):
                    if on:
                        cv.create_rectangle(x * box, y * box, (x + 1) * box, (y + 1) * box,
                                            fill='black', outline='')

        head = tk.Frame(win, bg=BG)
        head.pack(pady=(0, 2))
        if self.tron_img is not None:
            tk.Label(head, image=self.tron_img, bg=BG).pack(side='left', padx=(0, 8))
        tk.Label(head, text=DONATE_COIN, bg=BG, fg='#ff6b7a',
                 font=('Segoe UI', 10, 'bold')).pack(side='left')

        tk.Label(win, text=DONATE_ADDRESS, bg=BG, fg=FG, font=('Consolas', 10)).pack(pady=(4, 2))
        note = tk.Label(win, text='send any amount you like - TRON (TRC20) network only',
                        bg=BG, fg=MUTED, font=('Segoe UI', 8))
        note.pack(pady=(0, 10))

        btns = tk.Frame(win, bg=BG)
        btns.pack(pady=(0, 16))

        def copy_again():
            self.root.clipboard_clear()
            self.root.clipboard_append(DONATE_ADDRESS)
            self.root.update_idletasks()
            note.configure(text='address copied to the clipboard', fg=OK)

        self._button(btns, 'Copy address', copy_again, 'primary').pack(side='left', padx=(0, 8))
        self._button(btns, 'Close', win.destroy, 'quiet').pack(side='left')
        win.transient(self.root)
        win.grab_set()

    # ------------------------------------------------------------- helpers
    def say(self, msg):
        self.text.configure(state='normal')
        self.text.insert('end', msg + '\n')
        self.text.see('end')
        self.text.configure(state='disabled')
        self.root.update_idletasks()

    def _tool_path(self):
        if getattr(sys, 'frozen', False):
            return sys.executable
        return os.path.abspath(__file__)

    def browse(self):
        d = self.filedialog.askdirectory(title='Select the folder that contains UFI.exe',
                                         initialdir=self.ufi_dir or 'C:\\')
        if not d:
            return
        d = os.path.normpath(d)
        if not os.path.exists(os.path.join(d, 'UFI.exe')):
            self.messagebox.showerror(APP, 'UFI.exe is not in that folder.')
            return
        self.ufi_dir, self.how = d, 'chosen by you'
        save_config(dict(load_config(), ufi_dir=d))
        self.say('UFI folder set to %s' % d)
        self.refresh()

    def _apply_update_lock(self):
        allowed = self.update_state.get('operations_allowed', False) and not self.busy and not self.updating
        for button in self.launch_buttons + [self.link_btn]:
            button.configure(state='normal' if allowed else 'disabled')
        self.update_button.configure(state='normal' if self.update_state.get('update_available')
                                     and not self.busy and not self.updating else 'disabled')

    def _show_update_state(self, state):
        self.update_state = state
        self.checking_update = False
        text = state['message']
        if state.get('latest_version'):
            text += ' Current: %s; latest stable: %s.' % (VERSION, state['latest_version'])
        self.update_label.configure(text=text, fg=OK if state['operations_allowed'] else WARN)
        self._apply_update_lock()

    def check_updates(self, force=False):
        if self.checking_update:
            return
        self.checking_update = True
        def worker():
            state = UPDATE_GATE.status(force=force)
            try:
                self.root.after(0, lambda: self._show_update_state(state))
            except RuntimeError:
                pass  # Window was closed during a network request.
        threading.Thread(target=worker, daemon=True).start()

    def update_tick(self):
        self.check_updates()
        self.root.after(10000, self.update_tick)

    def update_now(self):
        if self.updating or self.busy:
            return
        self.updating = True
        self._apply_update_lock()
        def worker():
            try:
                from update_runtime import busy_processes, launch_installer
                if busy_processes():
                    raise UpdateError('update_apps_running', 'Close UFI and other UFI-Fix/Bridge/Control instances, then retry.')
                self.say('Downloading the required update from the official release...')
                last_percent = [-1]
                def progress(done, total):
                    percent = done * 100 // total
                    if percent != last_percent[0]:
                        last_percent[0] = percent
                        self.root.after(0, lambda p=percent: self.update_label.configure(text='Downloading update: %d%%' % p))
                downloaded = download_installer(progress=progress)
                self.say('Verified update %s; starting Windows installer.' % downloaded['version'])
                launch_installer(downloaded)
                self.root.after(0, self.root.destroy)
            except Exception as error:
                self.say('Update stopped: %s' % error)
                self.updating = False
                self.root.after(0, lambda: self._show_update_state(UPDATE_GATE.peek()))
        threading.Thread(target=worker, daemon=True).start()

    def refresh(self):
        self._apply_update_lock()
        self.path_var.set(self.ufi_dir or 'UFI.exe not found - use Browse')
        self.how_label.configure(text=('found via %s   |   Windows %s' % (self.how, windows_build()))
                                 if self.ufi_dir else 'Windows %s' % windows_build())
        if not self.ufi_dir:
            self.dot.configure(fg=BAD)
            self.dot_text.configure(text='UFI not found')
            self.sc_label.configure(text='-')
            self.link_btn.configure(bg='#242c3a', fg=FG)
            return
        linked, unlinked = ufi_shortcuts(self.ufi_dir)
        self.sc_label.configure(
            text='%d shortcut(s) go through the fix,  %d still start UFI directly'
                 % (len(linked), len(unlinked)))
        if linked and not unlinked:
            self.dot.configure(fg=OK)
            self.dot_text.configure(text='ready')
            self.link_btn.configure(bg='#242c3a', fg=FG)
        else:
            self.dot.configure(fg=WARN)
            self.dot_text.configure(text='partly linked' if linked else 'not linked yet')
            self.link_btn.configure(bg=ACCENT, fg='#ffffff')   # nudge: this is the next step

    def job(self, fn):
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                fn()
            except Exception as e:
                self.say('error: %s' % e)
            finally:
                self.busy = False
                self.root.after(0, self.refresh)
        threading.Thread(target=run, daemon=True).start()

    def do_link(self):
        if not self.ufi_dir:
            self.say('set the UFI folder first')
            return
        # Keep the main EXE and its bridge companion together in the chosen
        # installation/portable folder instead of creating an outdated copy.
        target = self._tool_path()
        self.say('linking shortcuts ...')
        link_shortcuts(self.ufi_dir, target, self.say)
        self.say('done - a backup of every shortcut was kept as <name>.lnk.orig')

    def ai_connection(self):
        if not self.ufi_dir:
            self.messagebox.showerror(APP, 'Select the folder containing UFI.exe first.')
            return
        bridge = os.path.join(os.path.dirname(self._tool_path()), 'UFI-Bridge.exe')
        if not os.path.isfile(bridge):
            self.messagebox.showerror(APP, 'UFI-Bridge.exe is missing. Install version 2 or extract the complete portable ZIP.')
            return
        config = json.dumps(connection_config(bridge, self.ufi_dir), indent=2)
        window = self.tk.Toplevel(self.root)
        window.title('UFI AI connection settings')
        window.configure(bg=BG)
        window.transient(self.root)
        self.tk.Label(window, text='Add this configuration to an MCP-capable AI client.\n'
                      'The client can read logs and save edited support copies.',
                      bg=BG, fg=FG, justify='left', padx=14, pady=12).pack(anchor='w')
        text = self.tk.Text(window, width=76, height=14, bg=CARD, fg=FG, font=('Consolas', 10))
        text.insert('1.0', config)
        text.configure(state='disabled')
        text.pack(fill='both', expand=True, padx=14)
        def copy():
            window.clipboard_clear()
            window.clipboard_append(config)
        self._button(window, 'Copy configuration', copy).pack(anchor='e', padx=14, pady=12)

    def do_restore(self):
        if not self.messagebox.askyesno(APP, 'Put every UFI shortcut back to the original UFI.exe?'):
            return
        self.say('restoring shortcuts ...')
        result = restore_shortcuts(self.ufi_dir, self._tool_path(), self.say)
        if result['success']:
            checked = result['restored'] + result['repaired'] + result['already_original']
            self.say(('Verified: %d toolbox shortcut(s) point directly to UFI.exe.' % checked)
                     if checked else 'No toolbox shortcuts were found to restore.')
            self.say('UFI-Fix remains available from its own manager icon.')
            self.say('Already running UFI processes are unchanged; close them normally before checking a fresh launch.')
        else:
            self.say('Undo is incomplete. Some shortcuts could not be restored or verified; retry after resolving the reported errors.')
            self.messagebox.showwarning(APP, 'Undo is incomplete. Check the Activity log; success was not confirmed.')

    def launch(self, arg):
        if not self.ufi_dir:
            self.say('set the UFI folder first')
            return
        self.say('')
        self.job(lambda: run_ufi(self.ufi_dir, arg, self.say))

    def open_log(self):
        p = log_path()
        if os.path.exists(p):
            os.startfile(p)
        else:
            self.say('nothing logged yet')

    def run(self):
        self.say('%s %s (%s) started; checking required updates.' % (APP, VERSION, BUILD_CHANNEL))
        if self.ufi_dir:
            self.say('UFI found at %s (%s)' % (self.ufi_dir, self.how))
        else:
            self.say('UFI.exe was not found automatically - use Browse to point at its folder.')
        self.root.after(100, self.update_tick)
        self.root.mainloop()


# --------------------------------------------------------------------- main
def headless(arg):
    """started from a shortcut: no window, just fix and run"""
    try:
        UPDATE_GATE.require_current()
    except UpdateError:
        # A locked shortcut opens the update UI instead of silently starting UFI.
        Gui().run()
        return 3
    cfg = load_config()
    ufi_dir, _how = find_ufi_dir(cfg.get('ufi_dir'))
    if not ufi_dir:
        ctypes.WinDLL('user32').MessageBoxW(
            None, 'UFI.exe was not found.\r\n\r\nOpen UFI-Fix and choose the folder that '
                  'contains UFI.exe.', APP, 0x10)
        return 1
    return run_ufi(ufi_dir, arg)


def main():
    args = sys.argv[1:]
    if args:
        a = args[0].lower()
        if a in [t[0] for t in TOOLBOXES]:
            return headless(a)
        if a in ('--link', '/link', '--install'):
            d, _ = find_ufi_dir()
            if d:
                me = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                return 0 if link_shortcuts(d, me) else 1
            return 1
        if a in ('--restore', '/restore'):
            d, _ = find_ufi_dir()
            return 0 if d and restore_shortcuts(d, '')['success'] else 1
        if a in ('--uninstall', '/uninstall'):
            if not is_admin():
                return 0 if relaunch_as_admin(['--uninstall']) else 1
            return remove_everything()
        if a.startswith('/'):
            return headless(a)
    Gui().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
