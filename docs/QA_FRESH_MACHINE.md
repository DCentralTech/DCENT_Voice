# Fresh-machine QA protocol (Windows)

Purpose: prove that DCENT_Voice works on a machine that has never run it, on a
host we do not control, before any build reaches a person outside the project.
This is acceptance criterion **AC8** of
[`FRESH_MACHINE_IMPLEMENTATION_PLAN.md`](FRESH_MACHINE_IMPLEMENTATION_PLAN.md).

CI (`.github/workflows/ci.yml`, job `Windows package smoke`) already proves the
automated half — fresh profile, neutral working directory, silent install,
installed-executable smoke, uninstall — on every push and pull request. This
document covers what CI cannot: a pristine *desktop* with no developer tooling,
no WebView2 guarantee, no Python, and a real user watching the screen.

Run the protocol on three hosts before a release:

1. **Windows Sandbox** (this document, §1–§3) — always, it is free and repeatable.
2. **A physical machine that has never had DCENT_Voice** — a second laptop, a
   fresh Windows account, or a clean VM snapshot.
3. **A Windows 10 host with the WebView2 runtime absent** — proves the app is
   still usable for dictation and that the fallback surfaces explain themselves.

---

## 0. Prerequisites

Windows Sandbox is a Windows 10/11 Pro, Enterprise, or Education feature. Enable
it once, then reboot:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName "Containers-DisposableClientVM" -All
```

Build the artefacts you are going to test:

```powershell
./scripts/build_pyinstaller.ps1 -NoConfirm
./scripts/build_installer.ps1
```

Generate a sandbox configuration pointing at this checkout's `dist` folder. The
committed `scripts/qa/DCENT_Voice.wsb` is a template — Windows Sandbox refuses
to start when `<HostFolder>` does not exist, and that path is machine-specific:

```powershell
# Pass 1 - offline (default). This is the pass that proves AC9.
pwsh -File scripts\qa\new_sandbox.ps1 -Start

# Pass 2 - networking enabled, to observe SmartScreen.
pwsh -File scripts\qa\new_sandbox.ps1 -Networking -Start
```

The generated file lands at `build\qa\DCENT_Voice.wsb`. `dist` is mapped
**read-only** to `C:\Users\WDAGUtilityAccount\Desktop\dist`; a `<LogonCommand>`
opens that folder in Explorer as the sandbox starts.

> **Windows Sandbox usually has no Edge WebView2 runtime.** That is coverage,
> not a defect: it is exactly the S3 host class the friend's report may have hit.
> Settings, the overlay, and the setup wizard must degrade to a native dialog
> that names the problem and offers the runtime download, while hold-to-talk
> dictation keeps working. If the sandbox image *does* have WebView2, note it in
> the results and cover the missing-runtime case on host 3 instead.

---

## 1. Pass 1 — offline install (networking disabled)

Work through the steps in order. Record a screenshot for every step marked 📷.

### 1.1 Run Setup

Copy `DCENT_Voice-Setup.exe` from the mapped `dist` folder to the sandbox
desktop first (the mapping is read-only; the installer does not need to write
there, but copying keeps the run realistic), then double-click it.

Expected, in order:

| # | What you should see | Source |
|---|---|---|
| A | No UAC prompt. The install is per-user, into `%LOCALAPPDATA%\DCENT_Voice`. | `Program.cs` `defaultDest` |
| A2 | **Only on a host missing WebView2 or .NET Framework 4.7.2** — 📷 a warning box titled **DCENT_Voice** whose first line is `Dictation works, but Settings/overlay need Microsoft Edge WebView2.`, then `Missing on this computer:` and a `  • ` bullet per missing runtime (`Microsoft Edge WebView2 runtime — not installed` and/or `.NET Framework 4.7.2 or later — not found`), then `Hold-to-talk dictation is unaffected and works right now. The Settings window, the on-screen overlay and the setup wizard cannot open without these.`, closing with `Open the Microsoft download page in your browser now?` and **Yes** / **No**. Choose **No** for the offline sandbox run; on Host 3 choose **Yes** and confirm the browser opens <https://go.microsoft.com/fwlink/p/?LinkId=2124703> and that Setup itself downloaded nothing. | `Program.cs` `ReportHostDependencies` |
| A3 | A pause of up to a few minutes with no window while Setup runs the installed exe's `doctor` self-check. Nothing else should appear. | `PostInstallCheck.Run` |
| B | 📷 A message box titled **DCENT_Voice** reading:<br>`DCENT_Voice is installed for this user.`<br>`<blank line>`<br>`C:\Users\WDAGUtilityAccount\AppData\Local\DCENT_Voice`<br>`<blank line>`<br>`Launch now?` with **Yes** / **No** buttons and an information icon. | `Program.cs` launch prompt |

If instead of B you get 📷 a **warning** box beginning
`DCENT_Voice is installed, but its post-install self-check found a problem.`
or `DCENT_Voice is installed, but Setup could not run diagnostics.`, the install
is complete and intact but the app was not started. Both texts end with the line
`Setup will report this as a failed install so scripted deployments notice.`, and
Setup does exit **3** (interactive as well as `/S`). Record the full text (it
lists each failing check as `  • <check id> — <detail>` with an
`      Fix: <remediation>` line, and names the report path
`%TEMP%\DCENT_Voice-setup-check-<guid>\doctor.json`) and attach that JSON. That
is a QA **fail** for the host, not a payload failure — do not delete the install.

After a **passing** self-check that `DCENT_Voice-setup-check-<guid>` folder is
deleted, so finding one in `%TEMP%` at all means the self-check did not pass.

If you get 📷 a red error box reading `DCENT_Voice Setup failed:` followed by a
message, **stop** and record the full text — that is a payload validation or
host-floor failure, not a QA pass.

Host-floor failures have exactly these texts (the trailing build number varies):

- `DCENT_Voice Setup requires 64-bit Windows 10 version 1809 (build 17763) or later, or Windows 11.`
- `DCENT_Voice Setup requires Windows 10 version 1809 (build 17763) or later, or Windows 11. This computer reports build <N>. Earlier builds do not ship the .NET Framework 4.7.2 that the Settings window, the overlay and the setup wizard need.`

Choose **Yes** at prompt B.

Then confirm the Start Menu folder **DCENT_Voice** holds **two** entries:
`DCENT_Voice` and `DCENT_Voice Diagnostics` (the latter runs
`dcent-voice.exe doctor --open`). Confirm there is **no** DCENT_Voice entry in
the Startup folder (`shell:startup`) — launch-at-login is the HKCU `Run` value
only, and only once the user turns it on.

### 1.2 First-run experience

Expected within a few seconds of launch (WS3):

| # | What you should see |
|---|---|
| C | 📷 A window titled **Welcome to DCENT_Voice** (the setup wizard) — **or**, on a host without WebView2, a native message box titled **DCENT_Voice is running**. It must appear **exactly once**, ever, and only *after* hold-to-talk is already live. |
| D | Under the heading **DCENT_Voice is running** the first screen lists, verbatim:<br>• `Hold Ctrl+Win and speak. Release and the text lands where you were typing.`<br>• `The tray icon is in the notification area next to the clock. Windows may hide it under the ^ chevron; drag it to the taskbar to pin it.`<br>• `Everything stays on this machine. Your voice is transcribed locally and nothing is uploaded.`<br>followed by `Test your microphone in step 01 below, then choose Finish setup. This welcome screen opens once; the tray icon reopens it any time.` Step **01 Microphone** has a working **Test microphone** button, and the footer has **Finish setup**. |
| E | The tray sentence in D must name the **^** chevron and say to drag the icon to the taskbar to pin it. On a host *without* WebView2 the message box shows the same three bullets plus:<br>`Settings, the overlay, and the setup wizard need the Microsoft Edge WebView2 runtime, which is not installed on this computer. Hold-to-talk dictation works without it. Install it from https://go.microsoft.com/fwlink/p/?LinkId=2124703 — the tray menu also has "Install WebView2 runtime…".`<br>and closes with `This message is shown once. Everything else lives in the tray icon.` |

Press **Finish setup** (or just close the window): either persists
`privacy.first_run_education_shown = true`. It must not reappear on a later
launch (verified in §1.6).

If the wizard fails to open for any reason, a tray balloon appears immediately
("Setup could not open. Use the tray menu to try again.") and a second one
5 seconds later reading `DCENT_Voice is in the notification area (Windows may
hide it under the ^ chevron). Right-click the icon → Advanced → "Setup
wizard..." to finish setup.`

### 1.3 Tray icon

| # | What you should see |
|---|---|
| F | 📷 The DCENT_Voice icon in the notification area. On a default Windows profile it will be **behind the `^` chevron**, not pinned — expand the overflow to find it. The tooltip reads `DCENT_Voice — Hold Ctrl+Win and speak to dictate`. |
| G | The icon appears **before** the speech model has finished loading. Until ASR is ready the fourth (disabled) menu row reads `Model: loading… (dictation queues until ready)`; it becomes `Model: ready` afterwards, and `Model: unloaded — next hold loads` after an idle unload (WS3.4). Confirm the ordering in `logs\dcent_voice.log`: `ASR model load started in the background…` precedes `parakeet ready …`. |
| H | Right-click menu: `Hold Ctrl+Win and speak to dictate` / privacy / hotkeys / model status rows, then **Settings...**, **Advanced** (AI cleanup, Profiles, Setup wizard...), **Troubleshooting** → **Run diagnostics**, **Open log folder**, and **Quit**. On a host without WebView2 the Troubleshooting submenu also contains **Install WebView2 runtime…** (it is hidden when the runtime is present). |

### 1.4 Dictation

1. Open **Notepad** in the sandbox and click into the document.
2. Hold **Ctrl+Win**, say a sentence, release.

| # | What you should see |
|---|---|
| I | 📷 The transcribed sentence appears in Notepad within roughly a second of release. |

If the sandbox has no audio input device, record that and cover dictation on
host 2 instead — do **not** mark this step passed.

### 1.5 Settings and diagnostics

| # | What you should see |
|---|---|
| J | 📷 Tray → Settings opens the settings window (WebView2 present), **or** a native dialog that names the missing WebView2 runtime, gives the log path, and offers the Microsoft download link. A generic "couldn't open" toast is a **failure**. |
| K | 📷 Start Menu → **DCENT_Voice Diagnostics** runs `doctor` and opens the report folder. |
| L | Every doctor check is `pass`, or a `warn` with a stated reason (no audio input device on a sandbox is an acceptable warn). Any `fail` is a QA failure. |
| M | The doctor **egress** check reports zero non-loopback connection attempts. With sandbox networking disabled this is the AC9 proof. |

### 1.6 Second launch

Quit from the tray, relaunch from the Start Menu.

| # | What you should see |
|---|---|
| N | The first-run wizard/dialog does **not** reappear. |
| O | The tray icon returns and dictation works again. |

### 1.7 Uninstall

Settings → Apps → DCENT_Voice → Uninstall (or run
`"%LOCALAPPDATA%\DCENT_Voice\..." /uninstall` from the ARP entry).

| # | What you should see |
|---|---|
| P | 📷 A message box titled **DCENT_Voice** with a question icon:<br>`Remove DCENT_Voice?`<br>`<blank>`<br>`Yes: remove the app and permanently purge settings, personalization, consent/egress records, and saved provider credentials.`<br>`<blank>`<br>`No: remove the app but keep those user records for a future reinstall.`<br>`<blank>`<br>`Cancel: do nothing.` with **Yes** / **No** / **Cancel**. |
| Q | After choosing Yes: 📷 `DCENT_Voice was removed.` |
| R | `%LOCALAPPDATA%\DCENT_Voice`, the Start Menu folder, and the ARP entry are gone. |

Verify R mechanically inside the sandbox (PowerShell is present):

```powershell
Test-Path "$env:LOCALAPPDATA\DCENT_Voice"
Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice"
Test-Path (Join-Path ([Environment]::GetFolderPath('Programs')) 'DCENT_Voice')
```

All three must print `False`.

---

## 2. Pass 2 — networking enabled (SmartScreen observation)

Close the sandbox (this discards everything) and start a fresh one with
networking:

```powershell
pwsh -File scripts\qa\new_sandbox.ps1 -Networking -Start
```

Run only §1.1. The point of this pass is what Windows does *around* the app, not
the app:

| # | What you should see |
|---|---|
| S | 📷 On an unsigned Setup.exe: a blue **Windows protected your PC** SmartScreen dialog, with **More info → Run anyway** needed to proceed. Record whether it appears and what it names as the publisher (expected: *Unknown publisher*). |
| T | Note any Defender scan delay between double-click and dialog B. This — not the app — is the "it does an internet connection" symptom in the original report; the app itself makes no outbound connection (proved by check M in pass 1). |

Once Authenticode signing is in place (WS8), S should show the publisher name
and no "Unknown publisher" warning. Re-run this pass after the first signed
build and record the difference.

---

## 3. Hosts 2 and 3

Repeat §1 on:

- **Host 2 — a physical machine that has never had DCENT_Voice.** This is the
  only host that exercises a real microphone, a real GPU/driver stack, and a
  real user profile with OneDrive possibly redirecting `%LOCALAPPDATA%`. If
  `%LOCALAPPDATA%` is OneDrive-redirected, doctor must say so explicitly (S5).
- **Host 3 — Windows 10 with the WebView2 runtime absent.** Remove it with the
  Evergreen runtime's own uninstaller, or use an LTSC/stripped image. Steps C,
  H, and J are the ones that matter here: dictation must still work, and every
  UI surface that cannot open must say *why* and offer the runtime.

---

## 4. Evidence

Store everything under `docs/release-evidence/<version>/`, e.g.
`docs/release-evidence/v0.2.0-beta.2/`:

```
docs/release-evidence/<version>/
  results.md                       <- the filled-in template below
  sandbox-offline/
    B-install-complete.png
    C-first-run.png
    F-tray-icon.png
    I-dictation-notepad.png
    J-settings.png
    K-diagnostics.png
    P-uninstall-prompt.png
    Q-removed.png
    dcent-voice-diagnostics-<timestamp>.zip
  sandbox-networked/
    S-smartscreen.png
  host2-<short-name>/
    ...
  host3-no-webview2/
    ...
```

The diagnostics zip comes from step K (`doctor` writes it next to its report).
Copy it out of the sandbox **before closing the sandbox window** — the sandbox
discards its disk on close, and the `dist` mapping is read-only, so use
clipboard/drag-out or a second writable mapped folder.

### Results template

Copy this into `docs/release-evidence/<version>/results.md`:

```markdown
# Fresh-machine QA — <version>

Artifacts under test:
- DCENT_Voice-Setup.exe  sha256: <paste from the .sha256 sidecar>
- Signed: yes/no          Signer: <subject or "unsigned">

| Host | OS build | WebView2 | Audio in | Networking |
|---|---|---|---|---|
| 1. Windows Sandbox (offline)  | | | | disabled |
| 2. <machine>                  | | | | enabled  |
| 3. <machine, no WebView2>     | | | | enabled  |

| Step | Host 1 | Host 2 | Host 3 | Notes |
|---|---|---|---|---|
| A no UAC prompt                 | | | | |
| B install-complete dialog       | | | | |
| C first-run surface appears once| | | | |
| D hotkey / local / mic test     | | | | |
| E tray-overflow explained       | | | | |
| F tray icon present             | | | | |
| G icon before model ready       | | | | |
| H tray menu entries             | | | | |
| I dictation into Notepad        | | | | |
| J Settings or explained fallback| | | | |
| K Start Menu diagnostics        | | | | |
| L doctor: no fail               | | | | |
| M doctor: zero egress           | | | | |
| N wizard does not repeat        | | | | |
| O second launch works           | | | | |
| P uninstall prompt text         | | | | |
| Q removal confirmation          | | | | |
| R artefacts gone                | | | | |
| S SmartScreen behaviour         | n/a (offline) | | | |
| T scan delay observed           | | | | |

Use `pass`, `fail`, or `n/a — <reason>`. Any `fail` blocks the release.

Doctor summary (host 1): <paste the summary line>
Doctor summary (host 2): <paste>
Doctor summary (host 3): <paste>

Signed off by: <name>   Date: <YYYY-MM-DD>
```

A release may not be published until this file exists and contains no `fail`.
