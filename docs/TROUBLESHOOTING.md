# Troubleshooting DCENT_Voice

**If nothing happens after you launch DCENT_Voice, open the Start Menu, run
"DCENT_Voice Diagnostics", and send us the zip file it points at.** That one
step replaces every question we would otherwise have to ask you.

The same thing is available from the tray menu (**Run diagnostics**) and from
the command line:

```powershell
# Windows, installed build
& "$env:LOCALAPPDATA\DCENT_Voice\dcent-voice.exe" doctor --open

# from a source checkout
uv run python -m dcent_voice doctor --open
```

Diagnostics writes three files to
`%APPDATA%\DCENT_Voice\diagnostics\` (or `<DCENT_VOICE_PROFILE_ROOT>\diagnostics`
when that override is set):

| File | What it is |
|---|---|
| `doctor-<timestamp>.json` | machine-readable report (schema: `docs/schemas/doctor.schema.json`) |
| `doctor-<timestamp>.txt` | the same report, readable in any text editor |
| `dcent-voice-diagnostics-<timestamp>.zip` | **send us this**: report + logs + your config with any credential-shaped values redacted |

## Options

| Flag | Effect |
|---|---|
| `--open` | Open the diagnostics folder when the run finishes. |
| `--json PATH` | Also write the JSON report to `PATH`. |
| `--no-launch-checks` | Skip the trial launch (the slow check). Everything else still runs. |
| `--no-zip` | Do not build the zip bundle. |

`dcent-voice diagnose` is an alias for `dcent-voice doctor`.

For scripts and CI, two environment variables stop diagnostics from putting
anything on the screen: `DCENT_VOICE_NO_DIALOGS=1` suppresses the summary
message box, and `DCENT_VOICE_NO_OPEN=1` makes `--open` a no-op instead of
launching a file manager. The report files are still written either way.

**Exit codes:** `0` everything passed or only warned, `1` at least one check
failed, `2` diagnostics itself could not run (that is a bug — send the output).

Diagnostics runs **before** the configuration is loaded, so it still works when
`config.toml` is missing or corrupt, and it imports every native library in a
**child process**, so a library that crashes the process is reported as a finding
instead of taking diagnostics down with it.

## Reading the report

Each line is one check with a stable id, a status, what was seen, and what to do:

```
[warn] ui.webview2   the Microsoft Edge WebView2 runtime is not installed, so Settings …
```

A **warning** never means the app is broken — it means something is degraded
(no microphone plugged in, no WebView2 so Settings cannot open, the app is
already running). A **failure** means dictation will not work until you fix it.

## Check reference

### Environment — where the app is and whether it can write

| id | Fails / warns when | What to do |
|---|---|---|
| `env.os` | Windows build is older than 17763 (Windows 10 1809) — warn | Update Windows, or install .NET Framework 4.8. Dictation may still work; Settings and the overlay will not. |
| `env.architecture` | The process is 32-bit — fail | Install the x64 build. |
| `env.install` | The bundled `config.example.toml` is missing from the payload — fail | Reinstall from Setup.exe. **This is the classic "the app flashes and disappears" cause**: without this file a machine that has never run DCENT_Voice cannot create a configuration. |
| `env.profile` | Never fails; reports which config, log, data and state directories are in use | Use it to confirm which profile you are actually looking at. |
| `env.disk_space` | Under 2 GB free — warn; under 500 MB — fail | Free up space. |
| `env.write_access` | A profile directory cannot be written — fail | Fix folder permissions, or set `DCENT_VOICE_PROFILE_ROOT` to a writable directory. |
| `env.redirected_paths` | `%APPDATA%`, `%LOCALAPPDATA%` or the profile is a junction/reparse point or OneDrive-redirected — warn | Model verification rejects reparse points and OneDrive Files-On-Demand placeholders. Install to a non-synced folder (`Setup.exe /D=C:\DCENT_Voice`), or mark the folders "Always keep on this device". |

### Payload — is the install intact (frozen builds only)

| id | Fails when | What to do |
|---|---|---|
| `payload.runtime_files` | A file the installer requires is missing, empty or a reparse point | Reinstall. If antivirus quarantined a file, restore it and exclude the install folder. |
| `payload.models` | A shipped model snapshot does not match its pinned size + SHA-256 | Reinstall. DCENT_Voice never downloads speech models at runtime, so a damaged model stays damaged. |
| `payload.alternate_data_streams` | Payload files carry NTFS alternate data streams — `Zone.Identifier` (Mark-of-the-Web) is a warning, anything else is a failure | Explorer stamps `Zone.Identifier` on files extracted from a downloaded ZIP. Run `Get-ChildItem -Recurse '<install folder>' \| Unblock-File`. |

In a source checkout these three report "not applicable".

### Config — does your configuration exist, parse and point at real models

| id | Fails / warns when | What to do |
|---|---|---|
| `config.file` | No `config.toml` yet — warn | Launch the app once; it seeds one. If it still does not appear, look at `env.install` and `env.write_access`. |
| `config.profile` | The file exists but does not parse, or the active profile is unknown — fail | Rename or delete `config.toml`; the app reseeds a default from the bundled example. The report names the exact path. |
| `config.asr_model` | The model the **active** profile needs is not installed — fail | Reinstall, or switch to a profile whose model ships with the app. |
| `config.unbundled_models` | Some **other** profile references a model that is not installed — warn | Only matters if you switch to that profile. |

#### "My settings were reset" — `config.toml.broken-<timestamp>`

If DCENT_Voice cannot use your `config.toml`, it does **not** refuse to start.
It renames the file to `config.toml.broken-<timestamp>`, writes a fresh default
in its place, and says so in the log, on stderr and in a tray notification. A
startup that stops dead with no window is the failure this replaced.

**Your old settings are not gone.** They are in the `.broken-` file next to the
new one, in `%APPDATA%\DCENT_Voice\` (macOS:
`~/Library/Application Support/DCENT_Voice/`; Linux:
`~/.config/DCENT_Voice/`). Nothing ever deletes it — one is kept per reset.

To get your settings back, open both files in a text editor and copy the parts
you had changed into the new `config.toml`, then restart. Copying the whole old
file back will simply quarantine it again on the next launch, because whatever
made it unusable is still in it. The log line and the tray notification name the
specific reason, and it is worth reading before you copy anything:

```
%APPDATA%\DCENT_Voice\logs\dcent_voice.log      # search for "invalid configuration reset"
```

Reasons are not limited to a typo or broken TOML syntax. **Any** configuration
this build cannot parse triggers the same reset — including an `active_profile`
naming a profile that no longer exists, and a file written by a *newer* version
of DCENT_Voice whose keys this build does not understand. So if you downgraded,
or you copied a `config.toml` from another machine running a newer build, expect
one reset on the first launch; recover the settings you want from the `.broken-`
file rather than restoring it wholesale.

Two things are deliberately never quarantined: a file you pass explicitly with
`--config` (that path belongs to you, and silently renaming it would be data
loss), and a config that is merely *unusual* rather than unparsable. If the
reseed itself also fails, that is a reported fatal error with a dialog, not a
silent exit.

### Native libraries — can the binaries load

Each of these imports one library in a separate process. `native.ctranslate2`,
`native.onnxruntime`, `native.sounddevice` and `native.pynput` are **failures**
when they cannot load: transcription, the microphone, or the hotkey would not
work. `native.pystray`, `native.pillow`, `native.webview`, `native.pythonnet`,
`native.win32gui` and `native.uiautomation` are **warnings**: hold-to-talk
dictation survives without them.

| id | Notes |
|---|---|
| `native.audio_input` | **Warn** when no microphone is present — never a failure, so CI machines without audio hardware do not report a broken install. Check Windows Settings → Privacy & security → Microphone. |
| `native.onnx_providers` | Lists the ONNX Runtime execution providers. CPU is the shipped default; CUDA is a bonus, and its absence is not a problem. |

A library that hard-crashes the child process is reported as
"the probe process exited -1073741795 without a verdict", which is itself the
answer: that library cannot run on this CPU or is missing a system DLL.

### UI runtime — why Settings will not open

| id | Warns when | What to do |
|---|---|---|
| `ui.webview2` | The Edge WebView2 Evergreen runtime is absent | Install it from <https://go.microsoft.com/fwlink/p/?LinkId=2124703>. Settings, the overlay and the setup wizard need it; **hold-to-talk dictation does not.** |
| `ui.dotnet_framework` | .NET Framework older than 4.7.2 (release 461808) | Install .NET Framework 4.8. It is preinstalled from Windows 10 1809. |
| `ui.edge` | Never fails; informational | — |

The three ids above are Windows-only and always pass elsewhere. Linux and macOS
have their own host dependencies, described next.

### Desktop host — Linux and macOS (always passes on Windows)

On Windows, "why can't I open Settings" is WebView2. On the other two platforms
the equivalent questions are completely different, and the answers are things
the user has to install or permit by hand.

**Linux.** Text insertion is not an API — it is a set of external programs, and
which ones you need depends on whether you are on X11 or Wayland.

| id | Warns / fails when | What to do |
|---|---|---|
| `desktop.session` | No graphical session is attached — warn | Expected over SSH (only the headless API works). Otherwise launch DCENT_Voice from inside your desktop session. Reports the session type and desktop environment, which every other check below depends on. |
| `desktop.portaudio` | `libportaudio2` is not installed — **fail** | `sudo apt install libportaudio2` (Debian/Ubuntu), `sudo dnf install portaudio` (Fedora), `sudo pacman -S portaudio` (Arch). Without it there is no microphone at all. |
| `desktop.injection_tools` | The text-insertion helpers for **your** session type are missing — fail | X11: `sudo apt install xclip xdotool`. Wayland: `sudo apt install wl-clipboard wtype` (or `ydotool` with its daemon running). Having the X11 pair installed does not help a Wayland session, and the check says so explicitly. |
| `desktop.uinput` | Wayland session and `/dev/uinput` is missing or not writable — warn | `sudo usermod -aG input $USER`, then log out and back in; `sudo modprobe uinput` if the device does not exist. Wayland compositors do not hand global hotkeys to ordinary clients, so without this hold-to-talk may only fire inside DCENT_Voice's own window. X11 needs none of this and the check passes there. |
| `desktop.webkitgtk` | The GTK WebKit2 typelib is missing or unusable — warn | `sudo apt install gir1.2-webkit2-4.1 python3-gi gir1.2-gtk-3.0` (use `gir1.2-webkit2-4.0` on Ubuntu 22.04). This is Linux's `ui.webview2`: Settings, the overlay and the setup wizard need it, **hold-to-talk dictation does not.** |

**macOS.** Both things dictation needs are TCC permissions that only you can
grant. Neither can be enabled programmatically — the report gives you the exact
System Settings pane.

| id | Warns / fails when | What to do |
|---|---|---|
| `desktop.accessibility` | Accessibility is not granted — **fail** | System Settings → Privacy & Security → Accessibility → enable DCENT Voice, then relaunch. Without it macOS delivers no global hotkey and the injector cannot type. |
| `desktop.microphone` | Microphone access is denied or restricted — **fail**; not yet requested — warn | System Settings → Privacy & Security → Microphone → enable DCENT Voice. On a first launch "not determined" is normal: hold the hotkey once and choose Allow. |
| `desktop.dependencies` | Never fails; informational | Confirms the `.app` is self-contained — Homebrew and a system Python are never required. |

### Instance — is it already running

| id | Warns / fails when | What to do |
|---|---|---|
| `instance.mutex` | Another DCENT_Voice already holds the single-instance lock — warn | This is the second most common "nothing happens": it *is* running. Windows hides new tray icons under the `^` chevron next to the clock — click it and drag the icon out. |
| `instance.lock_file` | A lock file remains from a process that is gone — warn | Harmless; the app clears it on the next launch. |
| `instance.ade_registry` | Registry entries are stale or unparseable — warn | Harmless; cleaned at startup. |
| `instance.service_port` | The configured loopback port is taken by something that is not us — warn | Close the other program, or change `[service] port` in `config.toml`. |
| `instance.autostart` | The login item points at an executable that no longer exists — warn (it repairs itself) | Windows reads the HKCU `Run` value; macOS reads `~/Library/LaunchAgents/tech.d-central.dcent-voice.plist`; Linux reads `$XDG_CONFIG_HOME/autostart/dcent-voice.desktop`. DCENT_Voice rewrites its login item to the current executable on **every** launch, so simply starting it from its new location repairs a moved `.app`, a renamed AppImage or a reinstall. You can also delete the file the report names. |

### Egress — proving it stays local

`egress.connections` wraps the socket layer, loads the configured speech model,
idles for two seconds, and lists every non-loopback connection attempt. **The
expected result is none.** A failure here is a bug in DCENT_Voice, not a
configuration problem — please send the zip.

It observes TCP connections (`connect`, `connect_ex`, `create_connection`) and
DNS lookups (`getaddrinfo`) made through Python's socket layer. A name lookup
counts as an attempt: resolving a remote host is already a network question, and
it happens before any connection, so a request that fails at DNS still leaves a
trace. What it does **not** see: UDP `sendto`, raw sockets, and traffic from a
native library that bypasses Python's socket layer. Those are outside what an
in-process monitor can honestly claim, which is why the check reports what it
observed rather than asserting that nothing whatsoever happened.

Running diagnostics from the **tray** skips this one check and says so: it would
run inside the live application, where loading a second copy of the model and
patching the running app's sockets would disturb the thing being diagnosed.
Start Menu → **DCENT_Voice Diagnostics** runs in its own process and gives you
the full egress proof.

If your machine reported network activity when you first ran the installer, that
is almost certainly Microsoft Defender SmartScreen looking up the reputation of a
large, unsigned, freshly downloaded executable. That happens outside this app;
this check exists so you do not have to take our word for the rest.

### History — what the last failure already said

| id | Meaning |
|---|---|
| `history.last_startup_failure` | A previous startup died and recorded why. **Fail** — and usually the single most specific line in the report. |
| `history.logs` | Lists `startup.log`, `dcent_voice.log` and `dcent_voice_fault.log` with their last 60 lines. **Warn** when no log exists at all: the app either never started or died before it could open one. |

### Launch — the actual experiment

`launch.fresh_profile` starts the app on a throwaway profile root, from a
neutral working directory, on a free port, with an isolated single-instance
mutex, waits for the loopback `/health` endpoint, reports how long it took and
which ASR provider came up, then shuts it down. Your real profile, tray icon,
autostart entry and any running instance are untouched.

Skip it with `--no-launch-checks` (the installer's post-install self-check and
the tray's "Run diagnostics" both do, because they already know the app runs).

## Common situations

**"I double-click it and nothing happens."** Run diagnostics. In order of
likelihood: `instance.mutex` warns (it is already running — find the tray icon
under the `^` chevron), `history.last_startup_failure` fails (it names the
cause), `env.install` or `payload.runtime_files` fails (damaged install).

**"I can't open the settings."** Check `ui.webview2`. Dictation works without
it; the Settings window does not.

**"It says my model is missing."** Check `payload.models` and
`config.asr_model`. If `payload.alternate_data_streams` also warns, run
`Unblock-File` over the install folder first — a Mark-of-the-Web on a model file
is enough to fail verification.

**"Dictation records but nothing is typed."** That is injection, not startup:
`native.win32gui` / `native.uiautomation` in the report, and the
`dcent_voice.log` tail, show which target was chosen.

## Privacy

The zip contains: the report, your log files, and `config.toml` with anything
that looks like an API key, token, secret, password or bearer credential
replaced by `***REDACTED***`. It contains **no audio and no transcripts**.

The diagnostics folder and every file in it are created owner-only: the bundle
holds your logs and your configuration, and you may be on a shared or roaming
profile. If those permissions cannot be applied, the report is still written —
a diagnostics file you cannot obtain would defeat the purpose.
Read the `.txt` file before sending it if you want to know exactly what you are
sharing.
