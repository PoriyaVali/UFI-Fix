"""Read Windows PowerShell output without depending on the Windows locale."""
import subprocess


def run_powershell(script, timeout=60, report=None):
    """Return UTF-8 output, or an empty string when the command cannot be read.

    On Windows, a decoding error in subprocess's text-mode reader thread can
    leave stdout as None. Capture bytes instead, make PowerShell emit UTF-8,
    and decode in this thread. Never replace undecodable characters in paths:
    a damaged path must not be used to link or restore a shortcut.
    """
    prefix = ('[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); '
              '$OutputEncoding = [Console]::OutputEncoding;\n')

    def failed(reason):
        if report is not None:
            report('PowerShell output unavailable: %s' % reason)
        return ''

    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', prefix + script],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as error:
        return failed(str(error))

    if result.returncode != 0:
        # Replacement is safe for diagnostics only, never for shortcut paths.
        details = (result.stderr or b'').decode('utf-8', errors='replace').strip()
        return failed('exit %s: %s' % (result.returncode, details[:1200]))
    if result.stdout is None:
        return failed('no stdout captured')
    try:
        return result.stdout.decode('utf-8-sig')
    except UnicodeError as error:
        return failed(str(error))
