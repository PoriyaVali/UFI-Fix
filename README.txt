UFI-Fix 2.3.1 - compatibility launcher and local AI API
====================================================
Unofficial community project, not affiliated with UFI / zegobit.
LOCAL TEST CANDIDATE; full hardware acceptance pending.

Undo also restores copied toolbox shortcuts without backups. Their next launch
goes directly to UFI.exe. The UFI-Fix manager icon remains available. A running
UFI process is unchanged; close it normally before checking a fresh shortcut.
Undo reports verification failures instead of claiming everything was restored.

Online stable-release verification is required before new operations. If a newer
official release exists, use Update now in UFI-Fix; there is no continue-old or
offline override. Valid online checks last at most five minutes. Existing job
observation and safe cleanup remain available. Updates preserve logs/history.
Older distributed builds without this gate cannot be remotely disabled.

Close UFI-Fix and active bridge clients before upgrading with
UFI-Fix-v2.3.1-Setup.exe. Setup reuses the previous installation and registration,
backs up replaced app files and keeps vendor logs, settings and AI history.
The installer includes the 2.0.1 inherited-ACL fix. No uninstall is needed.

Keep UFI-Fix.exe, UFI-Bridge.exe, UFI-Control.exe and companion files together.
Use AI connection settings in UFI-Fix to copy your MCP configuration. Existing
C:\Program Files\UFI-Fix\UFI-Bridge.exe configurations keep working after upgrade.

20 tools: logs, snapshots, separate edited drafts/history, diagnostics, UI read,
app open/close, settings, read-only Detect preflight and structured result tracking.
Detect confirms fresh identity/capacity only after stable output and idle GUI.
Embedded error/settings panels block changes; no automatic retry/dismissal.
The bridge stays unprivileged. Its private helper requests native Windows UAC
on the first control call of a session. No cloud account/API key is installed.

eMMC GUI profile: VCC, VCCQ, clock, bus width. Selection is verified in the GUI,
not electrically measured. Without a UFS programmer the UFS selectors are hidden;
UFS voltage/frequency have no reviewed mapping. Identify returns device_not_ready
when its real button is unavailable. Successful chip identification is not claimed
by no-memory tests. See AI-BRIDGE.md and ufi-bridge-tools.json for exact contracts.

No flash/erase/provisioning or unrestricted click/shell API. Original native
patcher mitigation detection, memory error handling and instruction-cache issues
remain unresolved. ACG compatibility is not fixed by this package.

Version 1 background follows; historical v1 claims do not establish v2 acceptance.

THE PROBLEM
-----------
When UFI starts, its copy protection rewrites the program's import table:
428 of the 965 Windows API entries are routed through stubs of its own.  Each
stub reads the first bytes of the real Windows function with a small built-in
instruction decoder before jumping into it.

In Windows 11 24H2 Microsoft recompiled parts of the system DLLs, and the
prologue of several APIs now contains this SSE instruction:

        movups [ebp-0x34], xmm0          machine code:  0F 11 45 CC

UFI's decoder counts that instruction as 2 bytes; it is 4.  The decoder lands
in the middle of the next instruction, follows a meaningless pointer, and the
program dies with the error every UFI user on 24H2 knows:

        Access violation at address 01D31877 in module 'UFI.exe'.
        Read of address 1D54378B.

The first API UFI hits is user32!GetClientRect.

This has nothing to do with your box, your dongle, your drivers or your
licence.  It is a compatibility problem between the UFI software and the newer
Windows build, and it affects every UFI installation on 24H2 / 25H2.


WHAT THIS TOOL DOES
-------------------
It starts UFI, finds the protected child process that the UFI loader creates,
and - inside that process only - moves the offending instruction out of the
range the stub parses.  The original instructions are copied to a trampoline
and replaced with a jump, so the Windows function still does exactly what it
did before.  About 38 functions are treated on each run.

  * No file on disk is modified.
  * No Windows file is touched.
  * Nothing outside the running UFI process is changed.
  * UFI's protection and licence are left completely alone.

The fix exists only in memory and disappears when UFI closes.


HOW TO USE IT
-------------
Normal use: just start UFI from your usual icons.  All four programmers work:

        UFI - eMMC ToolBox
        UFI - UFS ToolBox
        UFI - Android ToolBox
        UFI - CHIP Prog

Or open UFI-Fix from the desktop.  The window gives you:

        * the detected UFI installation folder (with Browse to change it)
        * four buttons to start each programmer directly
        * "Link shortcuts to the fix"   - route the UFI icons through the fix
        * "Undo - restore originals"    - put every shortcut back
        * "Open log"                    - what was patched, and when
        * the dot in the top right: green means everything is ready

Command line:

        UFI-Fix.exe                open the window
        UFI-Fix.exe /emmc          start the eMMC ToolBox with the fix
        UFI-Fix.exe /ufs           start the UFS ToolBox
        UFI-Fix.exe /android       start the Android ToolBox
        UFI-Fix.exe /chip          start the CHIP Programmer
        UFI-Fix.exe --link         link the UFI shortcuts
        UFI-Fix.exe --restore      put the shortcuts back
        UFI-Fix.exe --uninstall    remove the tool completely


IF UFI IS INSTALLED SOMEWHERE ELSE
----------------------------------
The tool finds the folder by itself, in this order:

        1. the setting you chose before
        2. the registry:  HKCU\SOFTWARE\ZEGOBIT\UFI\Install -> Directory
        3. a running UFI process
        4. the existing UFI shortcuts
        5. the usual places:  C:\UFI, D:\UFI, ... and Program Files

If none of that works, use Browse and point it at the folder that contains
UFI.exe; your choice is remembered.  (Tested with UFI on a different drive.)


IS EVERYTHING REVERSIBLE?  YES
------------------------------
1. The actual fix lives only in the memory of the running UFI process.  Close
   UFI and it is gone.  Nothing is left behind on disk.

2. The only permanent change is the shortcuts.  Before a shortcut is changed,
   a copy of it is saved next to it as   <name>.lnk.orig
   "Undo - restore originals" (or UFI-Fix.exe --restore) puts them all back.

3. To remove the tool completely: Apps & features -> UFI-Fix -> Uninstall,
   or run  UFI-Fix.exe --uninstall.  That restores the shortcuts, deletes the
   program folder, the settings in %LOCALAPPDATA%\UFI-Fix and the registry
   entry.


NOTES
-----
1. After a UFI update the UFI installer recreates its shortcuts, so open
   UFI-Fix once and press "Link shortcuts to the fix" again.

2. If you start UFI.exe directly - not through a shortcut and not from the
   UFI-Fix window - the fix is not applied and the old crash comes back.

3. Windows updates are fine.  The tool locates the affected functions live
   every time it runs; no fixed addresses are stored anywhere.

4. Log file:  %LOCALAPPDATA%\UFI-Fix\UFI-Fix.log


SUPPORTING THE WORK
-------------------
The tool is free and always will be.  If it saved you a reinstall of Windows
and you would like to say thanks, there is a donation address at the bottom of
the UFI-Fix window:

        TRX (TRON network):  TPWxvw8juAByLMjHsXpnEnQmq2N9UT5CUg

Clicking the address copies it to the clipboard and opens a QR code you can
scan with your wallet.  Any amount is welcome, and nothing in the tool changes
whether you donate or not.  Send TRX on the TRON (TRC20) network only.


TECHNICAL SUMMARY
-----------------
  * UFI.exe is a small 32-bit Delphi loader.  It unpacks the real 43 MB
    application and runs it in a second process (process hollowing), so the
    image is private memory rather than a mapped image.
  * The protection redirects 428 of 965 IAT entries to its own stubs.
  * Those stubs parse the target API's prologue; the parser mis-reads
    0F 10 / 0F 11 (movups) with a memory operand.
  * On this Windows build about 30 exported functions across the DLLs UFI
    loads contain that instruction in their first 0x50 bytes.
  * A second, harmless issue also exists: the loader corrupts its own SEH
    chain during an int3 anti-debug check, and Windows kills it with
    c0000409 (SEHOP).  That happens after it has already created the real
    process, so it does not affect anything.
