"""Session-scoped elevated helper over a local, authenticated named pipe.

Only JSON bytes cross this channel (no pickle). The helper pins the connecting
process to its launching Bridge PID and creation time, and dies with that
process/connection. The AI client and log-only bridge remain asInvoker.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from .control import CONTROL_TOOLS, Controller, verified_vendor
from .core import BridgeError

MAX_PACKET = 256 * 1024


def libraries():
    k, a = C.WinDLL('kernel32', use_last_error=True), C.WinDLL('advapi32', use_last_error=True)
    for lib, name, args, result in [
        (k, 'OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        (k, 'CloseHandle', [W.HANDLE], W.BOOL),
        (k, 'GetProcessTimes', [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
        (k, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
        (k, 'LocalFree', [W.HLOCAL], W.HLOCAL),
        (a, 'OpenProcessToken', [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
        (a, 'GetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        (a, 'ConvertSidToStringSidW', [C.c_void_p, C.POINTER(W.LPWSTR)], W.BOOL),
        (a, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)], W.BOOL),
        (k, 'GetNamedPipeClientProcessId', [W.HANDLE, C.POINTER(W.ULONG)], W.BOOL),
    ]:
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    return k, a


def parent_identity(pid):
    k, a = libraries()
    handle = k.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        raise OSError('Cannot inspect the launching bridge process.')
    token = W.HANDLE()
    try:
        times = [W.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *(C.byref(t) for t in times)):
            raise C.WinError(C.get_last_error())
        created = times[0].dwLowDateTime | times[0].dwHighDateTime << 32
        if not a.OpenProcessToken(handle, 8, C.byref(token)):
            raise C.WinError(C.get_last_error())
        length = W.DWORD()
        a.GetTokenInformation(token, 1, None, 0, C.byref(length))
        buffer = C.create_string_buffer(length.value)
        if not a.GetTokenInformation(token, 1, buffer, length, C.byref(length)):
            raise C.WinError(C.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES; its first field is PSID.
        sid_pointer = C.cast(buffer, C.POINTER(C.c_void_p))[0]
        sid_string = W.LPWSTR()
        if not a.ConvertSidToStringSidW(sid_pointer, C.byref(sid_string)):
            raise C.WinError(C.get_last_error())
        try:
            return handle, created, sid_string.value
        finally:
            k.LocalFree(C.cast(sid_string, W.HLOCAL))
    except Exception:
        k.CloseHandle(handle)
        raise
    finally:
        if token.value:
            k.CloseHandle(token)


def elevate(command):
    class EXEC(C.Structure):
        _fields_ = [('cbSize', W.DWORD), ('fMask', W.ULONG), ('hwnd', W.HWND),
                    ('lpVerb', W.LPCWSTR), ('lpFile', W.LPCWSTR), ('lpParameters', W.LPCWSTR),
                    ('lpDirectory', W.LPCWSTR), ('nShow', C.c_int), ('hInstApp', W.HINSTANCE),
                    ('lpIDList', C.c_void_p), ('lpClass', W.LPCWSTR), ('hkeyClass', W.HKEY),
                    ('dwHotKey', W.DWORD), ('hIcon', W.HANDLE), ('hProcess', W.HANDLE)]
    shell = C.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [C.POINTER(EXEC)], W.BOOL
    request = EXEC()
    request.cbSize, request.fMask = C.sizeof(EXEC), 0x40 | 0x100
    request.lpVerb, request.lpFile = 'runas', command[0]
    request.lpParameters, request.lpDirectory = subprocess.list2cmdline(command[1:]), str(Path(command[0]).parent)
    request.nShow = 0
    if not shell.ShellExecuteExW(C.byref(request)):
        raise BridgeError('control_elevation_required', 'Windows did not grant the UFI control helper elevation (UAC). Log tools still work.')
    return request.hProcess


class ControlClient:
    def __init__(self, root):
        self.root = Path(root)
        self.connection = None
        self.helper = None

    def connect(self):
        if os.name != 'nt':
            raise BridgeError('windows_required', 'UFI GUI control requires Windows.')
        verified_vendor(self.root)
        from multiprocessing.connection import Client
        import _winapi
        pipe = r'\\.\pipe\UFI-Control-' + secrets.token_hex(16)
        key = secrets.token_bytes(32)
        k, _ = libraries()
        parent, created, _ = parent_identity(os.getpid())
        k.CloseHandle(parent)
        if getattr(sys, 'frozen', False):
            executable = Path(sys.executable).parent / 'UFI-Control.exe'
            command = [str(executable)]
        else:
            executable = Path(__file__).resolve().parents[1] / 'UFI-Control.py'
            command = [sys.executable, str(executable)]
        if not executable.is_file():
            raise BridgeError('control_helper_missing', 'Install the matching UFI-Control.exe beside UFI-Bridge.exe.')
        command += ['--pipe', pipe, '--key', key.hex(), '--parent', str(os.getpid()),
                    '--created', str(created), '--ufi-dir', str(self.root)]
        self.helper = elevate(command)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if k.WaitForSingleObject(self.helper, 0) == 0:
                break
            try:
                _winapi.WaitNamedPipe(pipe, 200)
            except OSError:
                time.sleep(.05)
                continue
            try:
                self.connection = Client(pipe, family='AF_PIPE', authkey=key)
                return
            except OSError:
                break
        self.close()
        raise BridgeError('control_helper_unavailable', 'The elevated control helper did not connect; log tools remain available.')

    def call(self, name, args):
        if self.connection is None:
            self.connect()
        try:
            self.connection.send_bytes(json.dumps({'name': name, 'arguments': args}).encode('utf-8'))
            if not self.connection.poll(25):
                self.close()
                raise BridgeError('control_timeout', 'Control result was not received; inspect UFI before retrying a change.')
            packet = json.loads(self.connection.recv_bytes(MAX_PACKET))
            if 'error' in packet:
                raise BridgeError(packet['error']['code'], packet['error']['message'])
            return packet['result']
        except (OSError, EOFError, ValueError):
            self.close()
            raise BridgeError('control_disconnected', 'Control connection ended; the last operation is unconfirmed. Read current UFI state before retrying.') from None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.helper and os.name == 'nt':
            libraries()[0].CloseHandle(self.helper)
            self.helper = None


def serve_control(options):
    import _winapi
    from multiprocessing.connection import PipeConnection, deliver_challenge, answer_challenge
    from .native_control import Native
    from .service import validate
    verified_vendor(options.ufi_dir)
    k, a = libraries()
    parent, created, sid = parent_identity(options.parent)
    if created != options.created:
        k.CloseHandle(parent)
        return 2
    class SECURITY(C.Structure):
        _fields_ = [('nLength', W.DWORD), ('lpSecurityDescriptor', C.c_void_p), ('bInheritHandle', W.BOOL)]
    descriptor = C.c_void_p()
    # Protected DACL: only SYSTEM, administrators, and the launching user. No
    # remote clients; kernel client PID additionally pins this exact session.
    sddl = 'D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;' + sid + ')'
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None):
        k.CloseHandle(parent)
        return 2
    security = SECURITY(C.sizeof(SECURITY), descriptor, False)
    k.CreateNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.POINTER(SECURITY)]
    k.CreateNamedPipeW.restype = W.HANDLE
    handle = k.CreateNamedPipeW(options.pipe, 3 | 0x40000000 | 0x80000,
                               4 | 2 | 8, 1, MAX_PACKET, MAX_PACKET, 0, C.byref(security))
    k.LocalFree(descriptor)
    if handle == C.c_void_p(-1).value:
        k.CloseHandle(parent)
        return 2
    connection = None
    try:
        pending = _winapi.ConnectNamedPipe(handle, overlapped=True)
        deadline = time.monotonic() + 30
        while _winapi.WaitForSingleObject(pending.event, 200) == 258:
            if k.WaitForSingleObject(parent, 0) == 0 or time.monotonic() >= deadline:
                pending.cancel()
                pending.GetOverlappedResult(True)
                return 2
        _, error = pending.GetOverlappedResult(True)
        if error:
            return 2
        client = W.ULONG()
        if not k.GetNamedPipeClientProcessId(handle, C.byref(client)) or client.value != options.parent:
            return 2
        connection = PipeConnection(handle)
        handle = None
        key = bytes.fromhex(options.key)
        deliver_challenge(connection, key)
        answer_challenge(connection, key)
        def session_check():
            if k.WaitForSingleObject(parent, 0) == 0 or connection.poll(0):
                raise BridgeError('control_session_ended', 'The bridge disconnected or queued another request; no new action was sent.')
        controller = Controller(Path(options.ufi_dir).resolve(), Native(options.ufi_dir), session_check)
        definitions = {tool[0]: tool[2] for tool in CONTROL_TOOLS}
        while k.WaitForSingleObject(parent, 0) != 0:
            if not connection.poll(.5):
                continue
            message = json.loads(connection.recv_bytes(MAX_PACKET))
            try:
                if not isinstance(message, dict) or set(message) != {'name', 'arguments'} or message['name'] not in definitions:
                    raise BridgeError('unknown_tool', 'Only the fixed UFI control operations are accepted.')
                validate(message['arguments'], definitions[message['name']])
                result = {'result': controller.call(message['name'], message['arguments'])}
            except BridgeError as error:
                result = {'error': {'code': error.code, 'message': str(error)}}
            except Exception:
                result = {'error': {'code': 'control_failed', 'message': 'Control operation failed; inspect UFI before retrying.'}}
            connection.send_bytes(json.dumps(result, ensure_ascii=True).encode('utf-8'))
        return 0
    except (OSError, EOFError, ValueError):
        return 0
    finally:
        if connection:
            connection.close()
        if handle is not None:
            k.CloseHandle(handle)
        k.CloseHandle(parent)


def main():
    parser = argparse.ArgumentParser(description='Private UFI Bridge control helper; not an MCP server.')
    parser.add_argument('--pipe', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--parent', required=True, type=int)
    parser.add_argument('--created', required=True, type=int)
    parser.add_argument('--ufi-dir', required=True, type=Path)
    options = parser.parse_args()
    if not re.fullmatch(r'\\\\\.\\pipe\\UFI-Control-[0-9a-f]{32}', options.pipe) or not re.fullmatch('[0-9a-f]{64}', options.key):
        return 2
    return serve_control(options)
