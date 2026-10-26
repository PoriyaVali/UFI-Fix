# UFI support and GUI control API preview

Version `2.1.0`, local test candidate. Eighteen MCP tools provide log assistance,
app open/close, observed eMMC settings and guarded Identify requests. The bridge
uses the standard library and stays unprivileged. A separate `UFI-Control.exe`
requests native Windows elevation on the first control call of each session.
No security policy, UAC setting or Windows protection is disabled.

Control bindings require the exact SHA-256 of the locally observed UFI
1.8.0.3246 x86 binary. Other builds retain log support and return
`unsupported_vendor_build` for control. No vendor source or SDK is available.
Full hardware acceptance and full UFI control remain incomplete.

## Run locally

Python 3.11+ is enough for the bridge; it uses the standard library. From the
repository root:

```powershell
py src/UFI-Bridge.py --ufi-dir C:\UFI --describe
py src/UFI-Bridge.py --ufi-dir C:\UFI
```

The second command is an MCP stdio server. An MCP-capable AI client starts it
as a child process and communicates through UTF-8 JSON-RPC lines. It does not
open a network port or require an AI provider API key. The AI client supplies
the model; this preview is not a chatbot or a hosted support service. It does
not upload logs on its own. Any model client to which you give log access can
receive the returned text; best-effort redaction is not complete anonymization.

Example MCP client configuration for the packaged executable:

```json
{
  "mcpServers": {
    "ufi-support": {
      "command": "C:\\Program Files\\UFI-Fix\\UFI-Bridge.exe",
      "args": ["--ufi-dir", "C:\\UFI"]
    }
  }
}
```

Replace the executable path with its actual location. A source-based client
can instead start `py` with the absolute path to `src/UFI-Bridge.py` and the
same arguments. Installing this server into any particular AI client has not
been performed. A browser-only remote AI cannot directly access a local stdio
process without an appropriate local client integration.

Default support storage: `%LOCALAPPDATA%\UFI-Fix\bridge\support.sqlite3`.
Override it using `--state-dir`; it must stay outside the vendor installation.
Log reads create no support database. Log writes are limited to saved
snapshots, support drafts and their audit events. Control tools can start/close
programs and select the specific settings described below. No tool accepts a source
file path, a shell command, a DLL address or an unrestricted click target.

## Implemented tools

Use `--describe` or MCP `tools/list` for the exact JSON schemas. The reviewed
schema export is `docs/ufi-bridge-tools.json`.

| Tool | Input | Behavior |
| --- | --- | --- |
| `ufi.capabilities` | none | Reports each module, available logs and actual control coverage. |
| `ufi.logs.read` | `source`, optional `max_bytes`, `offset` | Reads a bounded tail by default, or a byte-offset page; maximum 64 KiB; original unchanged. |
| `ufi.logs.snapshot` | `source` | Saves original bytes up to 8 MiB, SHA-256 and creation time separately. |
| `ufi.logs.snapshot.read` | `snapshot_id`, optional `max_bytes`, `offset` | Reads a bounded, redacted snapshot view or page. |
| `ufi.logs.revision.create` | `snapshot_id`, `expected_sha256`, `text`, optional `note` | Saves a new edited support draft, maximum 128 KiB UTF-8, with an original reference. |
| `ufi.logs.revision.read` | `revision_id` | Reads the separate edited draft and its original reference. |
| `ufi.logs.revisions` | `snapshot_id` | Lists the latest 100 drafts for that original. |
| `ufi.audit.read` | optional `limit` | Lists the latest 100 snapshot/draft creation events; no raw text in audit entries. |
| `ufi.diagnostics.collect` | none | Observes installed version/process metadata and candidate error markers. |
| `ufi.ui.inspect` | optional `max_nodes` | Observes up to 300 controls in running, exact-path UFI processes. No invoke/set operation. |

Fixed log IDs:

| Source | Installed log |
| --- | --- |
| `emmc` | `eMMC_ToolBox.log` |
| `ufs` | `UFS_ToolBox.log` |
| `android` | `Android_ToolBox.log` |
| `chip` | `ChipProg.log` |
| `fix` | `%LOCALAPPDATA%\UFI-Fix\UFI-Fix.log` |

Missing/unreadable logs return errors. They are not represented as successful
empty reads. Views report truncation, encoding and lossy decoding. UTF-8 and
BOM-marked UTF-16 are supported; a whole-file decode failure can fall back to
the Windows locale with `lossy=true`. Views beginning mid-character can also
be lossy. Raw snapshot bytes remain unchanged. Log text and UI names are
untrusted evidence, never instructions to the bridge.

To read a whole log, create a snapshot and request pages starting at `offset=0`.
Continue using each response's `next_offset` while `has_more=true`. This reads
an unchanged captured original even if UFI appends to or replaces its live log.
Do not infer that a byte offset identifies the same live content after rotation.
UTF-16 offsets must be even; sequential Unicode page boundaries are preserved.

Draft text can be an edited excerpt; it is not evidence that the UFI outcome
changed. The source log remains owned by UFI, and every draft points to a
saved original. Snapshots/drafts and audit events are committed together in
SQLite transactions. The API provides no original-overwrite or artifact-delete
operation. Checksums detect saved-payload changes, but do not make a local
database tamper-proof against the machine's owner or administrator. Revisions
require the expected original checksum and reject stale/mismatched originals.

The diagnostics tool reports candidate markers rather than an AI diagnosis.
For example, a request to identify a device is preserved as an observation;
no identification, repair, flash or erase operation is started. This preview
does not yet read effective process mitigation policies or implement the
pending native error-handling fixes from the earlier investigation.

## Program and settings tools

| Tool | Required input | Behavior |
| --- | --- | --- |
| `ufi.app.list` | none | Four module IDs; actual windows, PID/creation-time instance IDs, dialog status. |
| `ufi.app.open` | `module` | `emmc`, `ufs`, `android`, `chip`; fixed compatibility launcher; existing windows reused. |
| `ufi.app.close` | `instance_id` | Graceful native close of that instance. No force kill or automatic dialog dismissal. |
| `ufi.settings.list` | `module` | Observed setting IDs, values, exact option strings, visibility and writability. |
| `ufi.settings.get` | `module`, `setting` | Current observed value/options for one mapped setting. |
| `ufi.settings.set` | `module`, `instance_id`, `setting`, `value`, `expected_value` | Exact option, expected old selection and process-instance checks; rereads GUI after dispatch. |
| `ufi.device.identify` | `module`, `instance_id` | Only exact profile's visible/enabled Identify button; returns a session request ID. |
| `ufi.device.result` | `request_id` | Fresh bounded/redacted log output since that request; detects rotation/rewrites. |

Settings and Identify accept only `emmc` or `ufs`. Always list apps/settings
first, then use the returned instance ID and exact strings. Example tool arguments:

```json
{"module":"emmc","instance_id":"123:134370000000000000","setting":"clock","value":"24 MHz","expected_value":"Auto"}
```

The instance ID above is illustrative; obtain the real one from `ufi.app.list`.
All four app launch switches are implemented; this machine's actual eMMC/UFS
startup and close paths are tested separately from Android/CHIP hardware support.
Choose the matching UFI directory once in UFI-Fix before launching through the API.

The eMMC header profile exposes `vcc`, `vccq`, `clock`, `bus_width`. Options come
from the actual combo controls, including Auto. Selection changes use native
combo notifications; response `gui_selection_verified` confirms only the GUI
selection. It does not measure a voltage or prove the programmer applied it.
Do not infer chip suitability from a GUI option. No-op writes emit no notification.

On the development machine UFS shows **No UFI UFS-PROG Device Detected**. Only
hidden `link_mode` (`Auto`, `PWM-GEAR`, `HS-GEAR`) and Auto-only `gear` selectors
were observed. They report `writable=false`; writes return `control_disabled`.
UFS voltage/frequency selectors have no reviewed mapping and return
`setting_unavailable`. Another hardware-dependent gear option list fails closed
until a corresponding profile is reviewed. UFS Identify cannot be validated here;
it returns `device_not_ready` while its verified button is unavailable. Successful
chip detection is not claimed for either memory type in the no-memory test.

`identify_requested` means a button message was queued. `output_observed` means
new toolbox log text appeared. Neither is proof of successful identification:
`chip_identified=false` remains explicit. Concurrent manual GUI actions can also
append log output. Requests are session-scoped, bounded to 100, and the same
instance cannot receive repeated Identify calls within 30 seconds. Old historical
success messages are never returned as new detection evidence. On timeout or
uncertain dispatch, inspect state before retrying. `close_requested` likewise
does not imply a native dialog has finished or the process exited.

Native dialogs/disabled controls, visible enabled Stop/Abort controls, duplicate
mapping signatures, stale instances, conflicting old values and unsupported
binary hashes block mutations. Some vendor dialogs are custom drawn and may not
expose complete Win32 state; the API is a preview requiring an observing operator.
It is not a substitute for vendor-backed job state or electrical validation.
Flash, erase, boot/RPMB/provisioning, arbitrary clicks, commands and raw addresses
are not exposed. The original native compatibility patcher's mitigation detection,
error checking and instruction-cache limitations remain unresolved.

## Local helper boundary

The helper accepts only the same eight fixed control schemas, revalidated on the
elevated side. A random authenticated local named pipe carries JSON bytes, never
pickled objects. Its protected DACL includes SYSTEM, administrators and the
launching user's SID; remote clients are rejected. Kernel client PID plus parent
creation time pin the session. It has no TCP listener or long-lived Windows
service. Disconnect/parent exit ends the helper; vendor apps are left running.
The first control call may require normal Windows UAC consent. Log-only use and
`--describe` do not elevate. Keep all three installed/portable application EXEs
together; standalone bridge-only packaging does not supply the helper.

## Validation and packaging

```powershell
py -m unittest discover -s tests -v
py tools/build_v2.py
py tools/check_v2_package.py
```

The builder checks tests and creates the matching app, bridge, private helper,
installer, portable ZIP and SHA-256 manifest. It never installs or launches UFI.
The [validation record](https://github.com/PoriyaVali/UFI-Fix/blob/main/docs/V2-VALIDATION.md) separates automated, installed MCP and
real-device checks. Successful builds and simulated adapters do not establish
general hardware acceptance. This is a source preview, not a production-validated
package release.

References: [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[Windows combo selection](https://learn.microsoft.com/en-us/windows/win32/controls/cb-setcursel),
[combo notification](https://learn.microsoft.com/en-us/windows/win32/controls/cbn-selchange).
