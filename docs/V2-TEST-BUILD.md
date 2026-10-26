# UFI-Fix 2.3.1 local test candidate

2.3.1 fixes copied-shortcut Undo, validates/restores original backups or repairs
missing/invalid ones, retains backups and verifies remaining launcher targets.
It preserves the manager's empty-argument shortcut and unrelated applications.

Version 2.3.1 adds mandatory stable-release checks and a verified installer
handoff. Twenty MCP tools include update status, Detect preflight, structured
stages and embedded dialog guards. All binaries share this Windows file version.
This is a source preview, not a production-validated package release.
The 2.0.1 staging ACL fix and app-file backup/rollback behavior are retained.

Run `py tools/build_v2.py`, then `py tools/check_v2_package.py`. The builder runs
the suite, compiles four EXEs (app, bridge, helper, setup) and creates a complete
portable ZIP, SHA-256 manifest and build report under `dist/v2.3.1`. The bridge
is asInvoker; the private control helper, original launcher and setup request
Administrator. Build/check commands do not install or launch vendor software.

Close UFI-Fix/Bridge/Control before setup. It reuses the registered application
directory/key, backs up existing app files under `backups/before-2.3.1-*`, merges
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
Online GitHub verification is now required for new work and installation.
Publish a draft release, upload the complete installer with its GitHub SHA-256
digest, then publish stable; see [UPDATE-POLICY.md](UPDATE-POLICY.md). Builds
older than the latest stable refuse installation. Original snapshots and active
result observation remain readable when operations are update-locked.
See [V2-VALIDATION.md](V2-VALIDATION.md) for the completed checks, including one
real eMMC Identify, and the remaining limitations.
