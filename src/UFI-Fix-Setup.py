"""UFI-Fix Setup - installs the UFI Box compatibility fix for Windows 11 24H2.

Runs elevated (the manifest asks for administrator), copies UFI-Fix.exe into
Program Files, creates a desktop shortcut, points the UFI shortcuts at the fix
and registers an entry in Apps & features so it can be removed again.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import winreg

APP = 'UFI-Fix'
VERSION = '1.0'
PUBLISHER = 'UFI-Fix (unofficial community fix)'
UNINSTALL_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\UFI-Fix'
CREATE_NO_WINDOW = 0x08000000

BG = '#12161d'
CARD = '#1b2029'
FG = '#e7ecf3'
MUTED = '#8c97a8'
ACCENT = '#3b82f6'
OK = '#22c55e'
BAD = '#ef4444'


def res(name):
    base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ps(script):
    try:
        return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                              capture_output=True, text=True, timeout=120,
                              creationflags=CREATE_NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def make_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return 1.0
    try:
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)
        ctypes.windll.user32.ReleaseDC(0, dc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


# ------------------------------------------------------------ UFI detection
def reg_install_dir():
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


def find_ufi_dir():
    seen = []
    d = reg_install_dir()
    if d:
        seen.append((d, 'registry'))
    for drive in 'CDEFGH':
        seen.append(('%s:\\UFI' % drive, 'common location'))
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')):
        if base:
            seen.append((os.path.join(base, 'UFI'), 'Program Files'))
    for path, how in seen:
        if os.path.exists(os.path.join(path, 'UFI.exe')):
            return os.path.normpath(path), how
    return None, None


# ------------------------------------------------------------------ actions
def set_run_as_admin(lnk):
    """flip the 'run as administrator' bit inside a .lnk file"""
    try:
        with open(lnk, 'r+b') as f:
            f.seek(0x15)
            b = f.read(1)
            f.seek(0x15)
            f.write(bytes([b[0] | 0x20]))
        return True
    except OSError:
        return False


def make_shortcut(path, target, args='', icon=None, workdir=None, desc='', admin=False):
    script = ('$sh=New-Object -ComObject WScript.Shell;$l=$sh.CreateShortcut(%s);'
              '$l.TargetPath=%s;$l.Arguments=%s;$l.WorkingDirectory=%s;$l.IconLocation=%s;'
              '$l.Description=%s;$l.Save();"ok"'
              % (q(path), q(target), q(args), q(workdir or os.path.dirname(target)),
                 q(icon or (target + ',0')), q(desc)))
    if ps(script) != 'ok':
        return False
    if admin:
        set_run_as_admin(path)
    return True


def scan_ufi_shortcuts(ufi_dir):
    script = r'''
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
    try { $l = $sh.CreateShortcut($_.FullName); "$($_.FullName)|$($l.TargetPath)|$($l.Arguments)" } catch {}
  }
}
'''.replace('UFIDIR', ufi_dir.replace("'", "''"))
    rows = []
    for line in ps(script).splitlines():
        parts = line.strip().split('|')
        if len(parts) >= 3 and parts[0].lower().endswith('.lnk'):
            rows.append((parts[0], parts[1], '|'.join(parts[2:])))
    return rows


def link_ufi_shortcuts(ufi_dir, tool, as_admin, emit):
    ufi_exe = os.path.join(ufi_dir, 'UFI.exe').lower()
    n = 0
    for path, target, args in scan_ufi_shortcuts(ufi_dir):
        if (target or '').lower() != ufi_exe:
            continue
        bak = path + '.orig'
        script = ('$sh=New-Object -ComObject WScript.Shell;$l=$sh.CreateShortcut(%s);'
                  'if(-not (Test-Path %s)){Copy-Item -LiteralPath %s -Destination %s};'
                  '$a=$l.Arguments;$l.TargetPath=%s;$l.Arguments=$a;$l.WorkingDirectory=%s;$l.Save();"ok"'
                  % (q(path), q(bak), q(path), q(bak), q(tool), q(ufi_dir + '\\')))
        if ps(script) == 'ok':
            if as_admin:
                set_run_as_admin(path)
            n += 1
            emit('   linked  %s  (%s)' % (os.path.basename(path), args or '-'))
    return n


def register_uninstall(install_dir, exe):
    try:
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as k:
            size = 0
            for root, _d, files in os.walk(install_dir):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            for name, val in (('DisplayName', '%s %s - UFI Box fix for Windows 11 24H2' % (APP, VERSION)),
                              ('DisplayVersion', VERSION),
                              ('Publisher', PUBLISHER),
                              ('InstallLocation', install_dir),
                              ('DisplayIcon', exe),
                              ('UninstallString', '"%s" --uninstall' % exe)):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, 'NoModify', 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, 'NoRepair', 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, 'EstimatedSize', 0, winreg.REG_DWORD, max(1, size // 1024))
        return True
    except OSError:
        return False


def perform_install(target_dir, ufi_dir, want_desktop, want_link, want_admin, say):
    """the whole installation, shared by the window and by --silent"""
    say('installing to %s' % target_dir)
    os.makedirs(target_dir, exist_ok=True)
    exe = os.path.join(target_dir, 'UFI-Fix.exe')
    src = res('UFI-Fix.exe')
    if not os.path.exists(src):
        say('ERROR: the packaged UFI-Fix.exe is missing')
        return None
    shutil.copy2(src, exe)
    say('   copied UFI-Fix.exe')
    for extra in ('README.txt', 'icon.ico'):
        if os.path.exists(res(extra)):
            shutil.copy2(res(extra), os.path.join(target_dir, extra))
            say('   copied %s' % extra)

    if want_desktop:
        desk = os.environ.get('PUBLIC', '')
        desk = os.path.join(desk, 'Desktop') if desk else os.path.join(os.path.expanduser('~'), 'Desktop')
        if make_shortcut(os.path.join(desk, 'UFI-Fix.lnk'), exe, workdir=target_dir,
                         desc='UFI-Fix - Windows 11 24H2 compatibility fix for UFI Box',
                         admin=want_admin):
            say('   desktop shortcut created%s' % (' (runs as administrator)' if want_admin else ''))
    start = os.path.join(os.environ.get('ProgramData', ''), r'Microsoft\Windows\Start Menu\Programs')
    if os.path.isdir(start):
        make_shortcut(os.path.join(start, 'UFI-Fix.lnk'), exe, workdir=target_dir,
                      desc='UFI-Fix', admin=want_admin)
        say('   start menu entry created')

    if want_link and ufi_dir:
        say('linking the UFI shortcuts ...')
        n = link_ufi_shortcuts(ufi_dir, exe, want_admin, say)
        say('   %d shortcut(s) now go through the fix' % n)
        if want_admin:
            say('   they will ask for administrator rights')

    if register_uninstall(target_dir, exe):
        say('   registered in Apps & features')

    try:
        cfgdir = os.path.join(os.environ.get('LOCALAPPDATA', ''), APP)
        os.makedirs(cfgdir, exist_ok=True)
        with open(os.path.join(cfgdir, 'config.json'), 'w', encoding='utf-8') as f:
            f.write('{"ufi_dir": %s, "tool": %s}'
                    % (repr(ufi_dir or '').replace("'", '"'), repr(exe).replace("'", '"')))
    except OSError:
        pass
    return exe


# ---------------------------------------------------------------------- GUI
class Setup(object):
    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox
        self.tk, self.filedialog, self.messagebox = tk, filedialog, messagebox
        self.scale = make_dpi_aware()
        self.root = tk.Tk()
        self.root.title('%s %s Setup' % (APP, VERSION))
        self.root.configure(bg=BG)
        self.root.tk.call('tk', 'scaling', self.scale * 96.0 / 72.0)
        self.root.geometry('%dx%d' % (int(680 * self.scale), int(600 * self.scale)))
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(default=res('icon.ico'))
        except Exception:
            pass

        self.ufi_dir, self.how = find_ufi_dir()
        self.install_dir = os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), APP)
        self.busy = False
        self.done = False
        self._build()

    def _card(self, pad=14):
        f = self.tk.Frame(self.root, bg=CARD, highlightbackground='#262d3a', highlightthickness=1)
        f.pack(fill='x', padx=18, pady=(0, 12))
        inner = self.tk.Frame(f, bg=CARD)
        inner.pack(fill='x', padx=pad, pady=pad)
        return inner

    def _btn(self, parent, text, cmd, kind='normal'):
        colors = {'normal': ('#242c3a', FG), 'primary': (ACCENT, '#ffffff'), 'quiet': (CARD, MUTED)}
        bg, fg = colors[kind]
        return self.tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=bg,
                              activeforeground=fg, relief='flat', bd=0, cursor='hand2',
                              font=('Segoe UI', 10, 'bold' if kind == 'primary' else 'normal'),
                              padx=16, pady=9, highlightthickness=0)

    def _build(self):
        tk = self.tk
        # the button bar is packed first, anchored to the bottom, so it can
        # never be pushed off the window by the cards above it
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(side='bottom', fill='x', padx=18, pady=(8, 16))
        self.install_btn = self._btn(foot, 'Install', self.install, 'primary')
        self.install_btn.pack(side='right')
        self._btn(foot, 'Close', self.root.destroy, 'quiet').pack(side='right', padx=(0, 8))
        tk.Label(foot, text='Unofficial fix. Not made by UFI / zegobit.', bg=BG, fg=MUTED,
                 font=('Segoe UI', 8)).pack(side='left', pady=(10, 0))

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill='x', padx=18, pady=(16, 12))
        tk.Label(head, text='%s Setup' % APP, bg=BG, fg=FG,
                 font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        tk.Label(head, text='Makes the UFI Box software run on Windows 11 24H2 / 25H2',
                 bg=BG, fg=MUTED, font=('Segoe UI', 10)).pack(anchor='w')

        c = self._card()
        tk.Label(c, text='Install to', bg=CARD, fg=MUTED, font=('Segoe UI', 9), anchor='w').pack(fill='x')
        row = tk.Frame(c, bg=CARD)
        row.pack(fill='x', pady=(4, 10))
        self.dir_var = tk.StringVar(value=self.install_dir)
        tk.Entry(row, textvariable=self.dir_var, width=1, bg='#0e1219', fg=FG, relief='flat',
                 insertbackground=FG, font=('Consolas', 10), highlightthickness=1,
                 highlightbackground='#2b3342').pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 8))
        self._btn(row, 'Change...', self.pick_install).pack(side='left')

        tk.Label(c, text='UFI installation folder', bg=CARD, fg=MUTED,
                 font=('Segoe UI', 9), anchor='w').pack(fill='x')
        row = tk.Frame(c, bg=CARD)
        row.pack(fill='x', pady=(4, 0))
        self.ufi_var = tk.StringVar(value=self.ufi_dir or 'not found - please choose')
        tk.Entry(row, textvariable=self.ufi_var, width=1, bg='#0e1219', fg=FG, relief='flat',
                 insertbackground=FG, font=('Consolas', 10), highlightthickness=1,
                 highlightbackground='#2b3342').pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 8))
        self._btn(row, 'Browse...', self.pick_ufi).pack(side='left')
        self.found_lbl = tk.Label(c, text=('found via %s' % self.how) if self.ufi_dir else
                                  'UFI.exe was not found automatically', bg=CARD,
                                  fg=MUTED if self.ufi_dir else BAD, font=('Segoe UI', 9), anchor='w')
        self.found_lbl.pack(fill='x', pady=(6, 0))

        c = self._card()
        tk.Label(c, text='Options', bg=CARD, fg=MUTED, font=('Segoe UI', 9), anchor='w').pack(fill='x')
        self.opt_desktop = tk.BooleanVar(value=True)
        self.opt_link = tk.BooleanVar(value=True)
        self.opt_admin = tk.BooleanVar(value=True)
        for var, text in ((self.opt_desktop, 'Create a desktop shortcut for UFI-Fix'),
                          (self.opt_link, 'Point the existing UFI shortcuts at the fix (a backup of each is kept)'),
                          (self.opt_admin, 'Run as administrator  (UFI-Fix and UFI start elevated)')):
            tk.Checkbutton(c, text=text, variable=var, bg=CARD, fg=FG, selectcolor='#0e1219',
                           activebackground=CARD, activeforeground=FG, font=('Segoe UI', 10),
                           anchor='w', highlightthickness=0, bd=0).pack(fill='x', pady=2)

        c = self._card()
        tk.Label(c, text='Progress', bg=CARD, fg=MUTED, font=('Segoe UI', 9), anchor='w').pack(fill='x')
        self.text = tk.Text(c, width=1, height=8, bg='#0e1219', fg='#b9c4d4', relief='flat',
                            font=('Consolas', 9), wrap='word', highlightthickness=1,
                            highlightbackground='#2b3342', padx=10, pady=8, state='disabled')
        self.text.pack(fill='both', expand=True, pady=(6, 0))


    def say(self, msg):
        self.text.configure(state='normal')
        self.text.insert('end', msg + '\n')
        self.text.see('end')
        self.text.configure(state='disabled')
        self.root.update_idletasks()

    def pick_install(self):
        d = self.filedialog.askdirectory(title='Install UFI-Fix into', initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(os.path.normpath(d))

    def pick_ufi(self):
        d = self.filedialog.askdirectory(title='Select the folder that contains UFI.exe',
                                         initialdir=self.ufi_var.get() if self.ufi_dir else 'C:\\')
        if not d:
            return
        d = os.path.normpath(d)
        if not os.path.exists(os.path.join(d, 'UFI.exe')):
            self.messagebox.showerror(APP, 'UFI.exe is not in that folder.')
            return
        self.ufi_dir = d
        self.ufi_var.set(d)
        self.found_lbl.configure(text='chosen by you', fg=MUTED)

    def install(self):
        if self.done:
            self.root.destroy()
            return
        if self.busy:
            return
        if not self.ufi_dir or not os.path.exists(os.path.join(self.ufi_dir, 'UFI.exe')):
            self.messagebox.showerror(APP, 'Please select the folder that contains UFI.exe.')
            return
        self.busy = True
        self.install_btn.configure(text='Installing...')
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self):
        try:
            exe = perform_install(self.dir_var.get(), self.ufi_dir, self.opt_desktop.get(),
                                  self.opt_link.get(), self.opt_admin.get(), self.say)
            if not exe:
                self.install_btn.configure(text='Install')
                return
            self.say('')
            self.say('Done. Start UFI from your usual icons, or from UFI-Fix on the desktop.')
            self.done = True
            self.install_btn.configure(text='Finish', bg=OK)
        except Exception as e:
            self.say('ERROR: %s' % e)
            self.install_btn.configure(text='Install')
        finally:
            self.busy = False

    def run(self):
        self.say('%s %s' % (APP, VERSION))
        self.say('administrator: %s' % ('yes' if is_admin() else 'NO - some steps may fail'))
        if self.ufi_dir:
            self.say('UFI found at %s (%s)' % (self.ufi_dir, self.how))
        self.root.mainloop()


def main():
    args = [a.lower() for a in sys.argv[1:]]
    if args and args[0] in ('--uninstall', '/uninstall'):
        exe = os.path.join(os.path.dirname(sys.executable), 'UFI-Fix.exe')
        if os.path.exists(exe):
            subprocess.Popen([exe, '--uninstall'])
        return 0
    if '--silent' in args or '/silent' in args:
        if not is_admin():
            ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, '--silent', None, 0)
            return 0
        ufi, _how = find_ufi_dir()
        target = os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), APP)
        out = []
        exe = perform_install(target, ufi, True, True, True, out.append)
        try:
            with open(os.path.join(os.environ.get('TEMP', '.'), 'UFI-Fix-setup.log'), 'w',
                      encoding='utf-8') as f:
                f.write('\n'.join(out))
        except OSError:
            pass
        return 0 if exe else 1
    if not is_admin() and getattr(sys, 'frozen', False) and '--noelevate' not in args:
        # the manifest normally handles this; ask again just in case
        params = ' '.join('"%s"' % a for a in sys.argv[1:])
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, params, None, 1)
        return 0
    Setup().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
