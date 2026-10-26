"""Verified Windows installer handoff. Never terminate a device application."""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path

from release_updates import GATE, UpdateError, secure_update_directory


def _kernel():
    if os.name != 'nt':
        raise UpdateError('windows_required', 'Installer handoff requires Windows.')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    for name, args, result in (
        ('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        ('CloseHandle', [W.HANDLE], W.BOOL),
        ('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
        ('GetProcessTimes', [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
        ('CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE),
        ('CreateFileW', [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def _creation(kernel, handle):
    values = [W.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(C.byref(value) for value in values)):
        raise UpdateError('update_handoff_failed', 'Cannot verify the old launcher process identity.')
    return values[0].dwLowDateTime | values[0].dwHighDateTime << 32


def own_creation():
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000, False, os.getpid())
    if not handle:
        raise UpdateError('update_handoff_failed', 'Cannot inspect the launcher process.')
    try:
        return _creation(kernel, handle)
    finally:
        kernel.CloseHandle(handle)


def wait_for_launcher(pid, created, timeout_ms=30000):
    if type(pid) is not int or not 0 < pid <= 0xffffffff or type(created) is not int or created <= 0 or pid == os.getpid():
        raise UpdateError('invalid_update_handoff', 'Invalid old-launcher process identity.')
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if C.get_last_error() == 87:  # Process already exited, before PID reuse.
            return
        raise UpdateError('update_handoff_failed', 'Cannot wait safely for the old launcher.')
    try:
        if _creation(kernel, handle) != created:
            raise UpdateError('invalid_update_handoff', 'Old launcher PID was reused; installation stopped.')
        if kernel.WaitForSingleObject(handle, timeout_ms) != 0:
            raise UpdateError('update_handoff_failed', 'Close the old launcher before retrying installation.')
    finally:
        kernel.CloseHandle(handle)


def busy_processes():
    kernel = _kernel()
    class Entry(C.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                    ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', W.DWORD),
                    ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
                    ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(kernel, name)
        function.argtypes, function.restype = [W.HANDLE, C.POINTER(Entry)], W.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == C.c_void_p(-1).value:
        raise UpdateError('update_handoff_failed', 'Cannot verify that UFI applications are closed.')
    try:
        entry = Entry()
        entry.dwSize = C.sizeof(entry)
        success, found = kernel.Process32FirstW(snapshot, C.byref(entry)), []
        if not success:
            raise UpdateError('update_handoff_failed', 'Process enumeration was not available.')
        while success:
            if entry.th32ProcessID != os.getpid() and entry.szExeFile.casefold() in (
                    'ufi.exe', 'ufi-fix.exe', 'ufi-bridge.exe', 'ufi-control.exe'):
                found.append({'pid': entry.th32ProcessID, 'name': entry.szExeFile})
            success = kernel.Process32NextW(snapshot, C.byref(entry))
        if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise UpdateError('update_handoff_failed', 'Process enumeration ended unexpectedly.')
        return found
    finally:
        kernel.CloseHandle(snapshot)


def launch_installer(download, gate=GATE):
    if busy_processes():
        raise UpdateError('update_apps_running', 'Close UFI and other UFI-Fix/Bridge/Control instances, then retry. No process was terminated.')
    release, asset = gate.update_target(force=True)
    if (release['id'] != download['release_id'] or release['tag_name'].lstrip('vV') != download['version']
            or asset['digest'] != 'sha256:' + download['sha256']
            or asset['size'] != download['bytes']):
        raise UpdateError('release_changed', 'The downloaded update is no longer the current verified release.')
    path = Path(download['path'])
    if path.parent != secure_update_directory() or path.is_symlink():
        raise UpdateError('update_integrity_failed', 'Installer must use the protected update staging directory.')
    kernel = _kernel()
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    if handle == C.c_void_p(-1).value:
        raise UpdateError('update_integrity_failed', 'Cannot lock the verified installer against replacement.')
    import msvcrt
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(handle)
        raise UpdateError('update_integrity_failed', 'Cannot read the locked installer.') from None
    # FILE_SHARE_READ denies writers/deletion until the executable is mapped by
    # Windows. The protected parent also prevents ancestor-directory replacement.
    with os.fdopen(descriptor, 'rb') as source:
        if os.fstat(source.fileno()).st_size != download['bytes'] or hashlib.file_digest(source, 'sha256').hexdigest() != download['sha256']:
            raise UpdateError('update_integrity_failed', 'Installer changed after download; execution was blocked.')
        from ufi_bridge.control_ipc import elevate
        from ufi_bridge.core import BridgeError
        try:
            process = elevate([str(path), '--silent', '--wait-for-pid', str(os.getpid()),
                               '--wait-for-created', str(own_creation()), '--reopen'])
        except BridgeError:
            raise UpdateError('update_elevation_required', 'Windows did not start the update installer. Allow its Administrator request or retry the update.') from None
        kernel.CloseHandle(process)
