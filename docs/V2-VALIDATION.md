# Version 2 validation record

## Undo shortcut hotfix: 2.3.1

130 Windows tests pass without skips. Ten real shell-link regressions use owned
temporary fixtures only: copied links without backup, validated originals,
wrong wrapper/module backups, retained backup bytes, idempotence, 8.3/long paths,
quoted/bracketed names, manager/unrelated preservation, failed scans/writes and
truthful GUI/CLI failure. Uninstall stops before removal if Undo is incomplete.
The first focused run caught a real short/long-path mismatch; canonical path
comparison and an explicit short-path regression resolve it. Tests now isolate
synthetic launcher logging instead of appending fake errors to the user's log.

Live investigation found four installation links already original, while four
desktop toolbox links still targeted UFI-Fix and had no .orig backup. The old
implementation skipped them and reported success. The final package check passes
for all four EXE versions/UAC manifests, payload hashes, portable agreement and
twenty-tool MCP roundtrip. Setup SHA-256:
`61fc16ad9a6bb755655630e0fa2f87d55a9ff2a13d5d0c33139f9a0965616d26`.

Installed over 2.3.0 with exit zero and matching manifest hashes. The single GUI
and its PyInstaller bootstrap exited gracefully. Installed `--restore` exited
zero: COM readback verifies all eight toolbox links target `C:\UFI\UFI.exe` with
unchanged module arguments. Both manager links remain unchanged during Undo.
A second installed Undo exits zero without changing any shortcut bytes; ten
backups remain byte-identical. Four vendor logs and the support database stayed
unchanged throughout install/restore. The exact user's installed MCP configuration
passes initialize/tools-list/capabilities with version 2.3.1, twenty tools, empty
stderr and exit zero. No vendor launch, Identify or firmware command was used.

The first local backup command failed due to PowerShell path-expression
precedence; it changed no original shortcuts. Ten backups were then hash verified,
and only the five accidentally created workspace copies were removed after
path/time/hash checks. Private evidence is retained outside publication. These
checks cover a locally installed candidate. Source publication is separate from
the installer release; the maintainer uploads the application assets separately.

## Mandatory update preview: 2.3.0

120 Windows tests pass without skips, including numeric/paginated release
selection, unknown/offline/expired/rollback locks, malicious URLs and version
parameters, altered/truncated downloads, Windows file sharing against installer
replacement, actual owned-child PID/creation waiting and both control gates.
The source GUI smoke check confirms all five launch/link buttons are disabled
when required/offline and do not invoke operations; verified state enables them.
Live public GitHub verification passes with current candidate 2.3.0 and stable
v1.0. Four EXE versions/elevation manifests, payload hashes, portable/installer
agreement and packaged MCP roundtrip passed. Final setup SHA-256 is
`e4186db68b33b224e2f6881f4de1127f3c2e4c16a7bf1df78e22c59e23440d2f`.

The final installer replaced local 2.2.0 successfully (exit zero), with matching
installed payload hashes and unchanged four vendor logs/support database during
installation. All eleven support tools passed through the user's installed MCP
configuration (twenty tools advertised), including live update status, snapshot
paging and a separate Persian draft. A real legacy v1.0 asset download verified
its size and GitHub SHA-256 as a transport fixture; it was never executed.
The protected staging directory resolved from Windows Program Files despite an
environment override; its owner was Administrators and a real medium-integrity
write probe was denied. Installed helper app-list/stale-target rejection passed
without a hardware command; a caller's claimed version did not change policy.
MCP sessions exited zero with empty stderr. Raw device/support evidence is private.

The new policy applies to intact builds containing the gate. Administrator
tampering, already shipped gate-free binaries and direct vendor execution remain
outside local enforcement. Permission lasts at most five monotonic minutes;
publication is not instantly pushed to every client. Read-only observation and
safe cleanup remain available. Newer-release self-install is not established by
mock test fixtures; there is no live higher stable release yet. No chip command
or firmware update is used to test this feature. See UPDATE-POLICY.md.

## Detect stage 1 preview: 2.2.0

The local Detect update adds a nineteenth tool, read-only preflight, typed
progress/identity/capacity/health, embedded-panel guards and conservative
completion/frozen results. Eighty-nine Windows tests pass, including fresh vs
historical output, mixed identities, partial health, UTF-16, stalls and UI guards.
The prior captured real result and embedded-dialog geometry replay correctly;
read-only preflight also passed against the already running toolbox. The four
executables, payload checksums, portable/installer agreement and packaged MCP
roundtrip passed. A cold vendor startup then showed "Ping to device timed out,
please reconnect the BOX and retry", although Windows still listed the USB box.
The installed preflight blocked with zero Identify dispatches. A fresh startup
access violation was also logged; its cause is undetermined. After the user
confirmed physical USB reconnect, the toolbox opened. Preflight correctly
blocked the embedded backup-path warning. An observing operator acknowledged
that exact warning and closed Path settings using verified native buttons,
without changing options. Preflight then passed. No settings, driver or security
policy changes were made to bypass the startup failure.

The final local 2.2.0 installer then exited successfully. Installed payload
checksums matched the manifest; four vendor logs and the existing support
database stayed unchanged during installation. All ten support tools passed
through the user's installed stdio configuration, including snapshot paging,
a separate Persian draft and history/audit checks; vendor logs stayed unchanged.
Installed control preflight with the vendor app closed correctly returned
`app_not_running` and zero Identify dispatches. These are IPC/install checks.

### Fresh 2.2.0 eMMC check after reconnect

Exactly one Identify was sent through the installed MCP bridge. All seven
stages reached `observed`; final `phase=completed`, `finished=true` and
`chip_identified=true` required stable fresh output and the same GUI instance
returning idle with Identify enabled. Elapsed time was 155.56 seconds.

The fresh result was Toshiba 400071, MMC v5.0/v5.01, 15,758,000,128 user bytes
and 4 MiB each for Boot1, Boot2 and RPMB. Health was Urgent (90% reserved-block
consumption), SLC estimated lifetime exceeded, MLC not defined. No access
violation or CMD1 failure appeared in this request's fresh output.

Exactly 1,760 bytes were appended. Original before/after snapshots had verified
checksums and an identical prefix. Their new window matched the result hash,
the snapshot API page and the original vendor file. All four GUI settings
remained Auto before/after; displayed negotiated bus/voltage values are vendor
reports, not electrical measurements. No second Identify, manual setting change,
Write, Erase or Format was sent. The MCP server exited zero with empty stderr.

First observations occurred at 1.02s programmer, 3.05s insertion, 4.06s identity/
bus, 35.50s capacity, 125.05s health and 155.56s completion. Gaps over 30s produced
`stalled` observations and resumed with new output without retrying. A private
running screenshot showed "Scanning user partition". These are observation
times and output gaps; their underlying cause is undetermined. They do not
establish a physical timeout or chip failure cause. No further hardware command
was used to investigate the timing.

The startup backup warning was cleared for this session; a persistent fix is
still unverified. Physical UFS and broader hardware acceptance remain untested.

## Baseline 2.1.0 checks

Date: 2026-10-26. This is a source preview with limited device coverage, not a
production-validated release or a complete UFI SDK. No GitHub package release is
created by committing these sources.

| Area | Checked behavior |
| --- | --- |
| Automated tests | 69 Windows tests covering UTF-8 startup scanning, log preservation/drafts, protocol guards, process identity, local JSON pipe, app-file upgrades/rollback and inherited NTFS permissions. |
| Native suspend regression | An owned Python child confirms the old process rights reject suspension and the corrected rights permit suspend/resume; failure paths stop before patching. |
| Packaged files | Four EXEs share version 2.1.0; elevation manifests, payload checksums, portable/installer consistency and packaged stdio roundtrip checked. |
| Installed support tools | All ten support tools checked through MCP: original snapshot/page reads, a separate Persian test draft, history/audit and error guards; source logs preserved. |
| Installed control tools | All eight control tools exercised. All four toolboxes open/reuse/close; close checks actual process exit. eMMC clock GUI selection changed and restored. Hidden UFS controls, stale instances and invalid requests rejected. |
| Real eMMC | One authorized Identify returned fresh device identity, capacity, partition metadata and health output. The log window was checked against a separately saved original snapshot. UI returned to idle with Identify enabled. |

The real-device check did not issue a manual voltage/clock change, Write, Erase,
Format or provisioning command. It did not create a full memory image or measure
electrical signals. The generic result API deliberately reports observed output
rather than asserting successful identification automatically; the fresh text and
UI were inspected during this check.

Remaining limitations:

- UFS voltage/frequency and hardware-dependent gear lists have no reviewed
  control mapping; UFS physical Identify has not been validated.
- Embedded UFI error/settings panels can be absent from the API dialog list.
  Native combo writability alone does not prove that the entire UI is available.
  An observing operator remains necessary for this preview.
- A startup backup-path warning required acknowledging the native error and
  closing the Path settings for the tested session. A persistent fix is unverified.
- High idle CPU usage was observed in the vendor application; its cause remains
  undetermined.
- Native mitigation detection, complete memory-write/protection checks and
  instruction-cache handling remain incomplete. This is not an ACG fix.
- Hardware behavior beyond the one eMMC case, broad electrical acceptance,
  flash/erase/provisioning and unattended operation are not validated.

Private test logs, serial/CID values, device screenshots, user settings, local
support databases and work journals are excluded from this change.
Generated builds stay under ignored `build/` and `dist/` directories.
