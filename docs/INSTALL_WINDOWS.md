# Installing DCENT_Voice on Windows

This page shows you what you should see at each step, so you can tell the
difference between "it is working" and "nothing happened". If you get to the end
and something is still wrong, jump to
[If it does not work](#if-it-does-not-work) — there is one command that answers
almost every question.

**What you need:** Windows 10 version 1809 (build 17763) or newer, 64-bit, about
2.5 GB free. No Python, no Visual Studio, no internet connection.

On anything older, Setup refuses with

> DCENT_Voice Setup requires 64-bit Windows 10 version 1809 (build 17763) or
> later, or Windows 11.

That floor is not arbitrary: Settings, the overlay and the setup wizard are
pywebview on pythonnet, which needs .NET Framework 4.7.2 — preinstalled from
Windows 10 1803/1809 onward.

---

## Step 1 — Download and verify

Download `DCENT_Voice-Setup.exe` and its `DCENT_Voice-Setup.exe.sha256` from the
[Releases page](https://github.com/D-Central-Tech/DCENT_Voice/releases).

The installer is large (around 850 MB) because the speech model ships inside it.
That is deliberate: DCENT_Voice never downloads a model, so it works with no
network at all.

**Check that you got the file we published.** In PowerShell, in your Downloads
folder:

```powershell
Get-FileHash .\DCENT_Voice-Setup.exe -Algorithm SHA256 | Format-List
Get-Content .\DCENT_Voice-Setup.exe.sha256
```

The two hashes must match, ignoring case. If they do not, delete the file and
download it again — do not run it.

> **If you downloaded the portable ZIP instead**, unblock it before extracting,
> or Windows marks every extracted file with a "downloaded from the internet"
> tag that model verification will report:
>
> ```powershell
> Unblock-File .\DCENT_Voice-portable.zip
> ```

## Step 2 — SmartScreen

**What you will see:** a blue full-screen dialog, *"Windows protected your PC"*,
saying the publisher is unknown.

This is expected. The build is not yet code-signed with an Authenticode
certificate, and SmartScreen warns about any executable it has not seen before.
It is also the most likely explanation for "it looks like it made an internet
connection" — that is Windows itself asking Microsoft about the file's
reputation, not DCENT_Voice. The app makes no outbound connection at any point
(see [SECURITY.md](../SECURITY.md)).

**What to do:** click **More info**, then **Run anyway**.

You verified the SHA-256 in step 1, so you already know more about this file
than SmartScreen does. If you did not do step 1, go back and do it.

## Step 3 — Install

Setup extracts into `%LOCALAPPDATA%\DCENT_Voice` for your user account only. It
never asks for administrator rights and never touches other users.

**What you will see:**

1. A progress window while roughly 1 GB is extracted and the model files are
   verified against their pinned SHA-256 hashes. On a mechanical drive, or with
   an antivirus scanning as it goes, this can take a few minutes.
2. Setup then launches the installed app once, in a throwaway profile, purely to
   check that it really starts on this machine. This is the step that catches a
   broken install *before* you are the one who discovers it.
3. **If everything passed:** a dialog reading

   > DCENT_Voice is installed for this user.
   >
   > `C:\Users\<you>\AppData\Local\DCENT_Voice`
   >
   > Launch now?

   Click **Yes**.

4. **If a host runtime is missing:** a Yes/No dialog beginning

   > Dictation works, but Settings/overlay need Microsoft Edge WebView2.

   The Microsoft Edge **WebView2** runtime (or .NET Framework 4.7.2) is not on
   this machine. **Yes** opens
   <https://go.microsoft.com/fwlink/p/?LinkId=2124703> in your browser — that page
   is the only network request anything here makes, and only because you clicked.
   **No** is a perfectly good answer: dictation works immediately, and the tray
   menu keeps an **Install WebView2 runtime…** entry for as long as the runtime is
   absent.

5. **If the self-check found a problem:** a dialog beginning

   > DCENT_Voice is installed, but its post-install self-check found a problem.

   or, if diagnostics could not run at all,

   > DCENT_Voice is installed, but Setup could not run diagnostics.

   Both end with

   > Setup will report this as a failed install so scripted deployments notice.

   Either way the install is **kept** and the automatic launch is skipped so you
   read the message. Setup does not roll back for a host problem: the payload was
   already hash-verified, and deleting the app would remove the very thing that
   can explain what is wrong. Start it from the Start Menu, and run
   **DCENT_Voice Diagnostics** if it does not appear.

   Setup nevertheless **exits with code 3**, in both the interactive and the
   silent (`/S`) case. The files and the Add/Remove Programs entry stay exactly
   where they are; the non-zero code exists so an unattended deployment — which
   has no dialog to read — does not record a broken host as a clean install. The
   dialog also names the full report, kept at
   `%TEMP%\DCENT_Voice-setup-check-<guid>\doctor.json`; attach it if you ask for
   help. After a *successful* self-check that folder is deleted, so its presence
   always means something went wrong.

## Step 4 — First launch

**What you will see, in this order:**

1. **The tray icon appears** in the notification area next to the clock.
   Hold-to-talk is live from this moment, before the speech model has finished
   loading.

   > **Windows hides new tray icons.** If you do not see it, click the **`^`**
   > chevron to the left of the clock — it will be in that overflow panel. Drag
   > it down onto the taskbar to pin it there permanently. This is the single
   > most common reason people think nothing happened.

2. **A setup window** explaining three things:

   - *Hold Ctrl+Win and speak. Release and the text lands where you were typing.*
   - Where the tray icon is, and the chevron note above.
   - *Everything stays on this machine. Your voice is transcribed locally and
     nothing is uploaded.*

   It has a **Test microphone** button. Use it.

   This window appears **once, ever**. It will not come back on later launches.

3. **Without WebView2** the setup window cannot render at all. Instead you get a
   plain Windows dialog titled **"DCENT_Voice is running"** with the same three
   facts, plus the WebView2 download link and the line *"This message is shown
   once. Everything else lives in the tray icon."*

**Try it:** open Notepad, click into it, hold **Ctrl+Win**, say a sentence,
release. The text appears where your cursor was.

The first dictation may pause for a few seconds while the speech model finishes
loading — the overlay shows *Loading model…* while that happens. After that it
is fast.

## Step 5 — Know where the safety net is

Two things worth finding now, while everything works, so you are not hunting for
them later:

- **Start Menu → DCENT_Voice** — starts the app.
- **Start Menu → DCENT_Voice Diagnostics** — runs every self-check and writes a
  report. You will want this exactly once, and it will be at a bad moment.

Settings, the microphone picker, and the hotkey are all reachable by
right-clicking the tray icon.

---

## If it does not work

**Run the diagnostics.** Start Menu → **DCENT_Voice Diagnostics**. It opens a
folder containing

`dcent-voice-diagnostics-<timestamp>.zip`

**Send us that zip.** It contains a report of about forty checks — payload
integrity, model hashes, native libraries, WebView2 and .NET Framework versions,
audio devices, whether another copy is already running, whether anything tried
to reach the network, and the tail of every log. Credential-shaped values are
redacted. It is the difference between us guessing and us knowing.

The report is also readable on its own: open the `.txt` next to the zip. Each
finding has an id (`env.install`, `ui.webview2`, `native.onnxruntime`, …) that is
explained in [TROUBLESHOOTING.md](TROUBLESHOOTING.md), and each one that is not a
pass tells you what to do about it.

Diagnostics work even when the app itself will not start — that is the whole
point of them.

### Two things to check first

**"I double-clicked it and nothing happened."** It is probably already running.
Look under the `^` chevron next to the clock. `instance.mutex` in the
diagnostics report says so explicitly.

**"Settings won't open."** You are missing the WebView2 runtime; see step 3.
Dictation is unaffected. `ui.webview2` in the report confirms it and gives you
the link.

---

## Uninstalling

Settings → Apps → **DCENT_Voice** → Uninstall, or run `Setup.exe /uninstall`.

You will be asked whether to keep your settings and the downloaded speech
models. Choosing **No** (keep them) makes a later reinstall instant and
non-destructive. Choosing **Yes** permanently deletes your configuration,
consent records, logs, the models under `%LOCALAPPDATA%\DCENT_Voice.Models`, and
any credentials stored in the Windows keyring.

Uninstalling always removes the start-at-login entry and the Start Menu
shortcuts either way.
