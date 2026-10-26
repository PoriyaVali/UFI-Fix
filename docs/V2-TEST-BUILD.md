# UFI-Fix 2.1.0 local test candidate

Version 2.1.0 adds eight scoped program/settings/Identify tools, for eighteen
MCP tools total. All binaries and Windows file metadata share this version.
This is a source preview, not a production-validated package release.
The 2.0.1 staging ACL fix and app-file backup/rollback behavior are retained.

Run `py tools/build_v2.py`, then `py tools/check_v2_package.py`. The builder runs
the suite, compiles four EXEs (app, bridge, helper, setup) and creates a complete
portable ZIP, SHA-256 manifest and build report under `dist/v2.1.0`. The bridge
is asInvoker; the private control helper, original launcher and setup request
Administrator. Build/check commands do not install or launch vendor software.

Close UFI-Fix/Bridge/Control before setup. It reuses the registered application
directory/key, backs up existing app files under `backups/before-2.1.0-*`, merges
configuration and preserves source logs/AI history and original shortcut backups.
App-file rollback does not cover subsequent shortcut/registry steps. Keep all
three app EXEs and documents together for portable use. Old artifacts remain
available; upgrading does not publish or create a GitHub release.

The exact UFI1.8.0.3246 binary and observed controls are required for GUI control.
See [AI-BRIDGE.md](AI-BRIDGE.md) for API coverage, errors and known limits.
No-memory testing may verify GUI selection and disabled-button guards. It cannot
verify applied voltages/clocks, chip detection or data operations. UFS voltage/
frequency remain unavailable pending a connected-programmer control profile.
Native compatibility mitigation/error/cache improvements are still pending.
See [V2-VALIDATION.md](V2-VALIDATION.md) for the completed checks, including one
real eMMC Identify, and the remaining limitations.
