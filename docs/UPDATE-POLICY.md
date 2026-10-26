# Mandatory stable-release policy (2.3.0)

The release authority is fixed to
`https://api.github.com/repos/PoriyaVali/UFI-Fix/releases`. No client parameter,
local configuration, environment version flag or offline switch changes it.
Public API requests need no GitHub token. Numeric versions are compared across
all pages (up to 1,000 releases), so 2.10 is newer than 2.9 and an older release
published later cannot become the required version. Drafts and prereleases are
excluded. Stable tags must be numeric, for example `v2.3.0`.

Before new work, the launcher, Bridge and elevated Controller require a verified
release check. Checks grant an in-process permission for at most 300 monotonic
seconds. The GUI rechecks its status every ten seconds and refreshes GitHub when
the permission expires. Publication can therefore take up to five minutes to
reach an already running process. No positive permission is saved to disk or
accepted after expiration. Offline, rate-limited, malformed, missing or failed
TLS responses lock new work. Failure checks have a ten-second retry cooldown;
the GUI Retry check forces a new request. A newer release observed in a process
cannot be undone by feeding that process an older release listing.

These actions are locked: launch/patch UFI, link shortcuts, Identify, change
settings, create snapshots/drafts and install an outdated UFI-Fix bundle. The
privileged controller checks again immediately before dispatch. Read-only
evidence and current-result observation remain available; guarded idle close,
restore/uninstall and exit are cleanup paths. No hardware command is cancelled,
restarted or force-terminated because an update appears.

The update UI has Update now and Retry check; it has no continue-old action.
Shortcut launches that are locked open this UI. Updates require closing UFI
and other UFI-Fix/Bridge/Control instances. Their processes are never killed.

## Verified installer handoff

The selected stable release must contain one uploaded installer named
`UFI-Fix-vVERSION-Setup.exe` or the legacy `UFI-Fix-Setup.exe`, a bounded size
and GitHub's `sha256:` asset digest. A higher release without the installer
still locks operations; it cannot silently fall back to an old installer.
Download redirects must remain HTTPS on the approved GitHub asset hosts.
Certificate/hostname validation stays enabled; environment proxy and Windows
CA-file overrides do not select another update authority. Changing the Windows
trust store remains outside the application's trust boundary.

The file is staged under Windows' Program Files/UFI-Fix/updates, never a
configuration-selected folder. Its full size, SHA-256 and executable header are
verified, then the release is checked again before publication and execution.
An open read-sharing handle prevents file writes/deletion during final rehash
and native UAC handoff; the protected parent prevents ancestor replacement.
The installer waits for the exact old PID/creation pair to exit and refuses
installation while other UFI components are running. It retains the existing
bundle backup/rollback and data preservation. It reopens the new UI after
success, or the preserved old update UI if installation fails. Uninstaller
behavior remains available. The digest authenticates through the GitHub/TLS
authority; it is not an independent offline signature.

## Publication and limits

Create a draft, upload the complete matching installer, confirm that GitHub
reports its SHA-256 digest, and publish stable only after package checks. Keep
the repository and installer names stable. Portable users may need to repoint
their MCP client to the installed Bridge path; the updater does not rewrite
arbitrary AI-client configuration. There is no automatic downgrade.

This is enforcement by intact clients, not DRM or server-controlled hardware.
Administrator/source/binary modifications, alternative tools and direct vendor
execution can bypass local code. Already published binaries without this gate
cannot be remotely forced to update just by creating a GitHub release. They
must first install a build containing this policy. A source/test candidate ahead
of the published stable is allowed after online verification; that is not a
claim of published release membership or executable attestation. No API accepts
a caller-supplied current version. Stronger enforcement requires an external
trusted service/hardware authority, which this project does not currently have.

## Validation scope

Tests cover stale/offline/rollback checks, numeric pagination, malformed metadata,
URL and version spoof attempts, installer hash/size changes, Windows read-lock
protection, actual owned-child exit/creation checks, both control boundaries and
safe observation. Test transport injection exists only as Python dependencies;
there is no production command-line bypass. A live newer-release installation
cannot be claimed from mock fixtures; it requires a real higher stable release.
