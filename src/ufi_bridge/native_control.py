"""Win32 bindings for the reviewed UFI window/control profile (helper only)."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess
import sys
import time

from .control import CLOCKS, UFS_MODES, verified_vendor
from .core import BridgeError, MODULES


class Native:
    def __init__(self, root):
        if os.name != 'nt':
            raise BridgeError('windows_required', 'UFI GUI control requires Windows.')
        self.root = Path(root).resolve()
        self.module_cache = {}
        self.u = C.WinDLL('user32', use_last_error=True)
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        signatures = [
            (self.u, 'EnumWindows', [self.callback, W.LPARAM], W.BOOL),
            (self.u, 'EnumChildWindows', [W.HWND, self.callback, W.LPARAM], W.BOOL),
            (self.u, 'GetWindowThreadProcessId', [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            (self.u, 'GetClassNameW', [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            (self.u, 'GetWindowTextW', [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            (self.u, 'IsWindowVisible', [W.HWND], W.BOOL),
            (self.u, 'IsWindowEnabled', [W.HWND], W.BOOL),
            (self.u, 'GetParent', [W.HWND], W.HWND),
            (self.u, 'GetDlgCtrlID', [W.HWND], C.c_int),
            (self.u, 'GetWindowRect', [W.HWND, C.POINTER(W.RECT)], W.BOOL),
            (self.u, 'PostMessageW', [W.HWND, W.UINT, C.c_size_t, C.c_ssize_t], W.BOOL),
            (self.u, 'SendMessageTimeoutW', [W.HWND, W.UINT, C.c_size_t, C.c_ssize_t, W.UINT, W.UINT, C.POINTER(C.c_size_t)], C.c_ssize_t),
            (self.k, 'OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.k, 'CloseHandle', [W.HANDLE], W.BOOL),
            (self.k, 'QueryFullProcessImageNameW', [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            (self.k, 'GetProcessTimes', [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            (self.k, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
        ]
        for library, name, args, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def text(self, hwnd, class_name=False):
        buffer = C.create_unicode_buffer(512)
        function = self.u.GetClassNameW if class_name else self.u.GetWindowTextW
        function(hwnd, buffer, len(buffer))
        return buffer.value

    def message(self, hwnd, msg, wparam=0, lparam=0):
        if getattr(self, 'read_deadline', None) and time.monotonic() > self.read_deadline:
            self.read_deadline = None
            raise BridgeError('ui_observation_timeout', 'The bounded UI inspection expired; no setting change was dispatched by inspection.')
        value = C.c_size_t()
        C.set_last_error(0)
        if not self.u.SendMessageTimeoutW(hwnd, msg, wparam, lparam, 0x22, 1500, C.byref(value)):
            raise BridgeError('ui_timeout', 'UFI did not confirm a window message; the operation result is unknown.')
        return -1 if value.value in (0xffffffff, 0xffffffffffffffff) else value.value

    def children(self, hwnd):
        self.read_deadline = time.monotonic() + 8
        handles = []
        @self.callback
        def visit(child, parameter):
            handles.append(child)
            return len(handles) < 1000
        self.u.EnumChildWindows(hwnd, visit, 0)
        if len(handles) >= 1000:
            raise BridgeError('control_mapping_unavailable', 'UFI control tree exceeds the reviewed bound.')
        nodes = []
        for handle in handles:
            rect = W.RECT()
            self.u.GetWindowRect(handle, C.byref(rect))
            node = {'hwnd': handle, 'parent': self.u.GetParent(handle),
                    'class': self.text(handle, True), 'name': self.text(handle),
                    'visible': bool(self.u.IsWindowVisible(handle)),
                    'enabled': bool(self.u.IsWindowEnabled(handle)),
                    'rect': [rect.left, rect.top, rect.right, rect.bottom]}
            nodes.append(node)
        # The reviewed selectors are sibling rows of two (UFS) or four (eMMC)
        # combo boxes. Restrict expensive cross-process string reads to those
        # structures, then still require exact option signatures in bindings().
        groups = {}
        for node in nodes:
            if node['class'] == 'TComboBox':
                groups.setdefault(node['parent'], []).append(node)
        candidates = []
        title = self.text(hwnd).casefold()
        reviewed_count = 4 if 'emmc toolbox' in title else 2 if 'ufs toolbox' in title else None
        for group in groups.values():
            if len(group) in ((reviewed_count,) if reviewed_count else (2, 4)) and max(n['rect'][1] for n in group) - min(n['rect'][1] for n in group) <= 2:
                candidates.extend(group)
        # The known eMMC title identifies a unique four-selector header; do
        # not spend the bounded budget on its unrelated hidden voltage panels.
        if reviewed_count == 4:
            visible_groups = [g for g in groups.values() if len(g) == 4 and all(n['visible'] for n in g)
                              and max(n['rect'][1] for n in g) - min(n['rect'][1] for n in g) <= 2]
            if len(visible_groups) == 1:
                candidates = visible_groups[0]
            elif len(visible_groups) > 1:
                raise BridgeError('control_mapping_unavailable', 'Multiple possible eMMC headers were observed.')
        for node in candidates:
            handle = node['hwnd']
            count = self.message(handle, 0x146)
            # Only lengths used by reviewed signatures. Enumerating every
            # unrelated vendor model/provisioning combo is slow and adds
            # no evidence to the allowlisted controls.
            if count not in (1, 3, 4, 5, 9):
                continue
            options = []
            for index in range(count):
                length = self.message(handle, 0x149, index)
                if not 0 <= length <= 511:
                    raise BridgeError('control_mapping_unavailable', 'Combo option text exceeds its bound.')
                buffer = C.create_unicode_buffer(length + 1)
                self.message(handle, 0x148, index, C.addressof(buffer))
                options.append(buffer.value)
            node.update(options=options, selection=self.message(handle, 0x147))
        self.read_deadline = None
        return nodes

    def process(self, pid):
        handle = self.k.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            path, length = C.create_unicode_buffer(32768), W.DWORD(32768)
            if not self.k.QueryFullProcessImageNameW(handle, 0, path, C.byref(length)):
                return None
            if Path(path.value).resolve() != (self.root / 'UFI.exe').resolve():
                return None
            times = [W.FILETIME() for _ in range(4)]
            if not self.k.GetProcessTimes(handle, *(C.byref(t) for t in times)):
                return None
            created = times[0].dwLowDateTime | times[0].dwHighDateTime << 32
            return str(pid) + ':' + str(created)
        finally:
            self.k.CloseHandle(handle)

    def instances(self):
        roots = []
        visible = []
        @self.callback
        def visit(handle, parameter):
            if self.u.IsWindowVisible(handle):
                pid = W.DWORD()
                self.u.GetWindowThreadProcessId(handle, C.byref(pid))
                kind = self.text(handle, True)
                visible.append((handle, pid.value, kind))
                if kind in ('UFI::WinUIClass', '#32770'):
                    roots.append(handle)
            return True
        self.u.EnumWindows(visit, 0)
        instances = []
        for handle in roots:
            pid = W.DWORD()
            self.u.GetWindowThreadProcessId(handle, C.byref(pid))
            identity = self.process(pid.value)
            if identity is None:
                continue
            title = self.text(handle)
            if self.text(handle, True) == '#32770':
                # Report vendor-owned native error dialogs as failures, never
                # as an opened toolbox. Do not automatically acknowledge them.
                texts = [n['name'] for n in self.children(handle) if n['name']]
                instances.append({'instance_id': identity, 'pid': pid.value, 'hwnd': handle,
                                  'module': None, 'title': title, 'enabled': False,
                                  'dialogs': texts, 'status': 'native_error_dialog'})
                continue
            module = next((key for key, value in MODULES.items() if value[0].casefold() in title.casefold()), self.module_cache.get(identity))
            if module is None:
                # UFS deliberately has a blank title/no visible child controls
                # on its "No UFI UFS-PROG Device Detected" screen.
                nodes = self.children(handle)
                if any(n.get('options') == UFS_MODES for n in nodes):
                    module = 'ufs'
                elif any(n.get('options') == CLOCKS for n in nodes):
                    module = 'emmc'
            if module:
                self.module_cache[identity] = module
            if len(self.module_cache) > 100:
                self.module_cache = {identity: module}
            instances.append({'instance_id': identity, 'pid': pid.value, 'hwnd': handle,
                              'module': module, 'title': title,
                              'enabled': bool(self.u.IsWindowEnabled(handle)),
                              'dialogs': [self.text(h) or kind for h, p, kind in visible
                                          if p == pid.value and h != handle and kind != 'TApplication'],
                              'status': 'window_observed' if title.strip() else 'ui_unavailable'})
        return instances

    def launch(self, module):
        verified_vendor(self.root)
        launcher = (Path(sys.executable).parent / 'UFI-Fix.exe' if getattr(sys, 'frozen', False)
                    else Path(__file__).resolve().parents[1] / 'UFI-Fix.py')
        if not launcher.is_file():
            raise BridgeError('launcher_missing', 'Install the matching UFI-Fix compatibility launcher beside the helper.')
        command = [str(launcher), MODULES[module][2]]
        if launcher.suffix == '.py':
            command.insert(0, sys.executable)
        # Launcher normally reads its saved UFI path. Refuse a different root
        # instead of silently opening another vendor installation.
        import json
        config = Path(os.environ.get('LOCALAPPDATA', '')) / 'UFI-Fix' / 'config.json'
        try:
            settings = json.loads(config.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = {}
        # The exact config key is kept in sync with the existing launcher.
        configured = settings.get('ufi_dir')
        if not configured or Path(configured).resolve() != self.root:
            raise BridgeError('launcher_root_mismatch', 'Choose this --ufi-dir in UFI-Fix first; its saved directory must match exactly.')
        subprocess.Popen(command, cwd=launcher.parent, creationflags=subprocess.CREATE_NO_WINDOW)

    def guard(self, instance, node=None):
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(instance['hwnd'], C.byref(pid))
        if self.process(pid.value) != instance['instance_id'] or self.text(instance['hwnd'], True) != 'UFI::WinUIClass':
            raise BridgeError('stale_instance', 'The target window/process changed before dispatch.')
        if not self.u.IsWindowEnabled(instance['hwnd']):
            raise BridgeError('ui_blocked', 'UFI main window became disabled before dispatch.')
        if node:
            self.u.GetWindowThreadProcessId(node['hwnd'], C.byref(pid))
            if self.process(pid.value) != instance['instance_id'] or self.text(node['hwnd'], True) != node['class'] or self.u.GetParent(node['hwnd']) != node['parent']:
                raise BridgeError('stale_control', 'The target control changed before dispatch.')
            if not self.u.IsWindowVisible(node['hwnd']) or not self.u.IsWindowEnabled(node['hwnd']):
                raise BridgeError('control_disabled', 'The target control became unavailable before dispatch.')

    def close_window(self, instance):
        self.guard(instance)
        if not self.u.PostMessageW(instance['hwnd'], 0x10, 0, 0):
            raise BridgeError('close_failed', 'Graceful close could not be requested.')

    def instance_alive(self, instance):
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, instance['pid'])
        if not handle:
            if C.get_last_error() == 87:  # PID no longer exists.
                return False
            raise BridgeError('process_status_unavailable', 'Cannot confirm whether the toolbox process exited.')
        try:
            # Query identity through this held handle. Reopening by PID after
            # WaitForSingleObject races ordinary teardown and can falsely
            # report stale_instance while the original process is exiting.
            times = [W.FILETIME() for _ in range(4)]
            if not self.k.GetProcessTimes(handle, *(C.byref(t) for t in times)):
                if self.k.WaitForSingleObject(handle, 0) == 0:
                    return False
                raise BridgeError('process_status_unavailable', 'Cannot read the held process identity.')
            created = times[0].dwLowDateTime | times[0].dwHighDateTime << 32
            if str(instance['pid']) + ':' + str(created) != instance['instance_id']:
                return False  # The old instance exited and its PID was reused.
            state = self.k.WaitForSingleObject(handle, 0)
            if state == 0:
                return False
            if state != 258:
                raise BridgeError('process_status_unavailable', 'Windows did not confirm the toolbox process state.')
            return True
        finally:
            self.k.CloseHandle(handle)

    def select(self, node, index, instance):
        self.guard(instance, node)
        if self.message(node['hwnd'], 0x147) != node['selection']:
            raise BridgeError('setting_conflict', 'GUI selection changed immediately before dispatch.')
        if self.message(node['hwnd'], 0x14e, index) != index:
            raise BridgeError('setting_unconfirmed', 'UFI did not accept the GUI selection.')
        # CB_SETCURSEL does not emit CBN_SELCHANGE; notify the actual parent.
        identifier = self.u.GetDlgCtrlID(node['hwnd']) & 0xffff
        self.message(node['parent'], 0x111, identifier | (1 << 16), node['hwnd'])

    def identify(self, node, instance):
        # Native BM_CLICK on this specific verified TButton only. No arbitrary
        # command ID, HWND, text or screen coordinate can come from the client.
        self.guard(instance, node)
        if not self.u.PostMessageW(node['hwnd'], 0xf5, 0, 0):
            raise BridgeError('identify_dispatch_failed', 'The Identify button message could not be queued.')
