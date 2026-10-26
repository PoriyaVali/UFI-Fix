# Issue 1: crash while scanning shortcuts

The screenshot in [issue 1](https://github.com/PoriyaVali/UFI-Fix/issues/1)
shows `AttributeError: 'NoneType' object has no attribute 'splitlines'` in
`scan_shortcuts`, reached from the GUI's initial refresh. This exception occurs
in UFI-Fix before the user launches a toolbox.

The old scanner assumed `subprocess.run(..., capture_output=True, text=True)`
always returned a string. On Windows a decoding failure in the subprocess
reader thread can instead leave `stdout=None`. A controlled invalid-code-page
test reproduced this locally; [CPython issue 158006](https://github.com/python/cpython/issues/158006)
also describes this behavior. The reporter's raw output and locale have not
been collected, so an encoding mismatch on that particular machine remains
an explanation supported by reproduction, not a confirmed machine diagnosis.

The program and installer now share `src/powershell_io.py`. It makes PowerShell
emit UTF-8, captures bytes, and decodes in the calling thread. Missing output,
invalid UTF-8, a timeout, a missing executable, and a failed command return an
empty string. App callers record these failures in the existing log. Invalid
path bytes are rejected rather than replaced with characters that could point
shortcut operations at a different path. A failed command cannot report `ok`
to a caller even if it emitted that text before failing.

On Windows with Python and Capstone installed, run:

```powershell
py -m unittest discover -s tests -v
```

The tests import the app and installer without running their entry points.
They cover the exact missing-output failure, both scanners/wrappers, Unicode
paths and arguments, and command failures. Live PowerShell checks emit only
synthetic output; they do not launch UFI, modify registry settings, install
anything, or read or change real shortcuts.

This is the first scoped correction for the requested version 2. The version
strings remain 1.0 while the owner supplies the remaining changes. Any locally
built EXEs are uncommitted review candidates, not the published v1.0 binaries
or a completed v2 release. Reporter-machine and genuine UFI hardware validation
remain outstanding.
