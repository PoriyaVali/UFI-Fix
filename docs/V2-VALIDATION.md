# Version 2.1.0 validation record

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
