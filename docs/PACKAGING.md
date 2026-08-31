# DCENT Voice Packaging

DCENT Voice's primary supported path for contributors is a uv-based source
install. End users on Windows should use `DCENT_Voice-Setup.exe` — the *only*
supported Windows installer (see "One installer" below). That artifact is
win-x64 only (Windows 10 version 1809, build 17763, or later; or Windows 11).
It embeds CPython,
VC++/UCRT, PortAudio, ONNX Runtime, CTranslate2, and both shipped speech
models so a friend machine does not need Python, a .NET SDK, a Visual C++
redistributable installer, CUDA, or a model download. Settings/overlay still
use the host Edge WebView2 runtime.

The public-beta installer is **unsigned** (Authenticode certificate is an
environment blocker). The ZIP remains a fallback. It is not yet a signed
production installer.

## One installer

There is exactly one supported Windows installer: the self-contained .NET 8 SFX
stub in `packaging/windows/setup-stub/`, built by `scripts/build_installer.ps1`
and published by `.github/workflows/release.yml`.

The former Inno Setup pipeline (`dcent-voice.iss`, `build_inno_installer.ps1`,
`verify-installed.ps1`) is **retired**. It now lives, unsupported and untested,
under [`packaging/legacy/inno/`](../packaging/legacy/inno/README.md). Two
pipelines with different post-install verification meant nobody could say which
artifact a given user had. The stub won because it needs no third-party
toolchain, already carried the recovery/migration/uninstall logic, and is what
the release workflow builds. Inno's one advantage — running the installed exe as
a post-install check under a kill-on-close Job Object — was ported into the stub
(`OwnedJob.cs`, a C# port of Inno's `DcentOwnedJob`; `PostInstallCheck.cs`).

`scripts/install_windows.ps1` is not a second installer: it is the developer
directory-tree installer for a local `dist\DCENT_Voice`, and it is not what users
get.

### Supported Windows floor: build 17763

Setup refuses anything below **Windows 10 version 1809 (build 17763)**
(`Program.cs`, `MinimumWindowsBuild`). The floor is not the installer's own — the
SFX would run on far older Windows. It is the app's windows: Settings, the
overlay and the setup wizard are pywebview on pythonnet, which requires .NET
Framework 4.7.2, preinstalled from 1803/1809 onward. Advertising 1607 (the
previous claim) shipped a build whose UI could not load on 1607–1803 hosts. Keep
this floor in step across `Program.cs`, `README.md`, this file,
`packaging/windows/dcent-voice.exe.manifest` and `docs/QA_FRESH_MACHINE.md`.

### Post-install self-check

After the tree is in place and before the "Launch now?" prompt, Setup runs the
executable it just installed:

```
<dest>\dcent-voice.exe doctor --json <tmp>\doctor.json --no-launch-checks --no-zip
```

inside a private kill-on-close Job Object (`CreateProcess(CREATE_SUSPENDED)` →
`AssignProcessToJobObject` → `ResumeThread`, so no child instruction runs before
the ownership boundary exists), 300 s timeout, with `DCENT_VOICE_PROFILE_ROOT`
pointed at a throwaway temp directory so the user's real
`%APPDATA%\DCENT_Voice` is untouched, plus `DCENT_VOICE_NO_DIALOGS=1` and
`DCENT_VOICE_DISABLE_AUTOSTART=1`.

| doctor exit | Setup does | Setup exits |
|---|---|---|
| 0 (all pass/warn) | offers "Launch now?" as before | 0 |
| 1 (a check failed) | keeps the install, lists the failing check ids and their remediation, does **not** auto-launch | **3** |
| 2 / timeout / could not start | keeps the install, says diagnostics could not run, does **not** auto-launch | **3** |

The install is never rolled back for a self-check failure. The payload was
already hash-verified by `ValidatePayload` before it moved into place; what
`doctor` finds is a *host* problem (missing runtime, locked folder, no audio
device), and deleting the app would remove the thing that can explain it. The
files and the Add/Remove Programs registration both stay.

But Setup must not *declare success* either (AC5), so the exit code is **3 in
both interactive and silent mode** — an unattended deployment has no dialog to
read, and an interactive exit code is still what a wrapper script sees. Under
`/S` the same detail goes to stderr; interactively both dialogs close with
`Setup will report this as a failed install so scripted deployments notice.`

On failure the report is kept at
`%TEMP%\DCENT_Voice-setup-check-<guid>\doctor.json` as evidence and its path is
named in the dialog. On success that whole throwaway profile root is deleted
(best-effort, bounded) — so one of those folders existing at all means the
self-check did not pass.

### Host runtimes: detected, never bundled

Setup checks the Edge WebView2 Evergreen runtime (`EdgeUpdate` client
`{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` in HKLM and HKCU, both the
`WOW6432Node` and native views; a `0.0.0.0` `pv` stub counts as absent) and
.NET Framework `Release >= 461808` (4.7.2). Missing either is a **warning, never
fatal** — hold-to-talk dictation does not use them.

**Decision: Setup does not bundle the WebView2 runtime.** The standalone
Evergreen redistributable is ~150 MB on top of an already ~900 MB Setup, it needs
its own elevation path, and it is already present on typical Windows 11 and on
Windows 10 with Edge. Instead the non-silent installer shows a Yes/No dialog
naming what is missing and, only on an explicit **Yes**, opens
<https://go.microsoft.com/fwlink/p/?LinkId=2124703> in the browser. That click is
the single sanctioned network step in the whole install. Under `/S` the same
information goes to stderr and nothing is opened. Revisit bundling only if
pristine-host QA shows the download step is where people actually give up.

### Shortcuts and autostart

Setup writes two Start Menu entries under `DCENT_Voice`:

| Shortcut | Target |
|---|---|
| `DCENT_Voice.lnk` | `dcent-voice.exe` |
| `DCENT_Voice Diagnostics.lnk` | `dcent-voice.exe doctor --open` |

The second is what a stuck user can still find when the app itself will not
start; its arguments must match `dcent_voice.doctor.start_menu_shortcut_args()`.
The uninstaller removes the whole `DCENT_Voice` Programs folder, so both go.

There is exactly **one** autostart mechanism: the
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run\DCENT_Voice` value written by
`src/dcent_voice/autostart.py` when the user enables launch-at-login, and removed
by `uninstall.ps1`. **The installer never creates a Startup-folder shortcut** —
the retired Inno script did, and the app's own autostart code did not manage it,
so the two could disagree about whether launch-at-login was on.

### Mark-of-the-Web on the portable ZIP

The SFX extracts with `ZipFile`, which writes no alternate data streams. Windows
Explorer, extracting the **portable ZIP**, stamps every extracted file with a
`Zone.Identifier` NTFS stream (Mark-of-the-Web). Model verification used to
reject any named stream outright, so portable-ZIP users failed verification at
install and at every model load. `model_registry._safe_regular_file` now treats a
stream named exactly `Zone.Identifier` as non-fatal: once the file's own SHA-256
verifies, the stream is deleted and the removal is logged at info. Any *other*
named stream still fails. Or run `Unblock-File` over the extracted tree first.

Reparse points are still rejected — but the message now says why you would
realistically hit one (OneDrive Files-On-Demand or a redirected/synced profile
turning files into placeholders) and what to do: install somewhere that is not
synced, e.g. `DCENT_Voice-Setup.exe /D=C:\DCENT_Voice`.

### Size

`build_installer.ps1` prints the finished Setup.exe size and warns above a
900 MB budget. The warning is informational; it does not fail the build.

## Windows Setup.exe

```powershell
# After scripts/build_pyinstaller.ps1 has produced dist\DCENT_Voice\
.\scripts\build_installer.ps1
```

That publishes a self-contained WinExe stub, zips the PyInstaller tree, and
appends a `DCENTSFX` trailer:

```
[stub.exe][payload.zip][SHA-256(payload)][u64 zip length][DCENTSFX][0-7 zero pad]
```

Output:

- `dist\DCENT_Voice-Setup.exe` — double-click / `/S` silent / `/uninstall`
- `dist\DCENT_Voice-Setup.exe.sha256`

`scripts/build_portable_zip.ps1` likewise emits the portable ZIP and a
sha256sum-compatible `.sha256` sidecar. The frozen tree includes `LICENSE`,
`README.md`, `THIRD-PARTY-LICENSES.md`, an artifact-derived CycloneDX SBOM, and
the full license/notice tree for embedded Python packages, the PyInstaller
bootloader/hooks, CPython, OpenSSL, SQLite, libffi, and both shipped speech
models. Windows builds also inventory and hash PortAudio plus the Microsoft
VC/UCRT DLLs, exclude the unused ASIO and PyAV/FFmpeg codec stacks, and bundle
the applicable notices. The self-contained Setup staging pass adds the exact
.NET SDK `LICENSE.txt` and `ThirdPartyNotices.txt` and records the resolved .NET
and Windows Desktop runtime versions without misrepresenting those components
as part of the portable ZIP. `global.json`, `RuntimeFrameworkVersion`, and
`packages.lock.json` pin the Setup build to .NET SDK 8.0.424/runtime 8.0.30;
the build performs a locked restore before publishing. The native installer
streams the trailer-declared ZIP through a bounded
1 MiB buffer and refuses a SHA-256 mismatch before extraction. Because signing changes executable
bytes, the signing and tagged release-packaging steps regenerate checksums after
signing and after the artifact is renamed.

Install location is `%LOCALAPPDATA%\DCENT_Voice`. Add/Remove Programs is
registered under HKCU. The installer copies itself next to the app so uninstall
still works after the downloaded Setup.exe is deleted.

Durable ASR models installed from an offline bundle are stored in the separate
`%LOCALAPPDATA%\DCENT_Voice.Models` tree, never inside the replaceable
executable directory. On the first upgrade from the legacy layout, Setup reads
the bounded model registry, accepts only canonical Faster Whisper model paths
and an allowlist of non-executable CTranslate2 assets, holds source handles
against replacement, hashes before and after copy, and publishes the merged
registry transactionally. Existing durable and legacy-only models are merged;
byte conflicts, corrupt registries, reparse points, undeclared files, or unsafe
paths abort before the old install tree moves. This preserves offline model
sovereignty without copying arbitrary material from an install directory.
Older source/offline installs may have created a models-only
`%LOCALAPPDATA%\DCENT_Voice` before Setup existed. Setup recognizes that exact
default path only when `models` is its sole plain child and the registry,
provider paths, complete model inventories, pinned manifests, and hashes all
verify. It transactionally publishes the durable tree, revalidates the legacy
closed world, retires the now-empty legacy parent, and only then performs normal
destination validation. Any extra file, executable, malformed registry,
unregistered model, link/reparse point, byte conflict, or concurrent mutation
preserves the legacy directory and refuses installation.

Windows uninstall always removes the exact `DCENT_Voice` login `Run` value and
the app's ADE discovery JSON/token/install record. User-created roaming state
under `%APPDATA%\DCENT_Voice` (configuration, personalization, consent/egress
records, and logs), durable ASR models under
`%LOCALAPPDATA%\DCENT_Voice.Models`, and OS-keyring provider credentials are
retained by default so reinstall is non-destructive. Running Setup with
`/uninstall /purge-user-data` (add `/S` for explicit silent purge), or choosing
**Yes** in the interactive uninstall choice, permanently removes those records
and durable models;
**No** removes the app while retaining them. Credential purge enumerates only
the exact `DCENT_Voice` keyring namespace and never reads or logs secret values.
Custom `/D=` installs are deliberately unregistered and their generated
uninstaller removes only that custom payload; it cannot delete a coexisting
normal install's shortcuts, registration, autostart, ADE records, configuration,
credentials, or durable models. Setup refuses malformed `/D` forms, protected
profile/system roots, reparse paths, and non-empty directories that cannot be
verified as an owned DCENT_Voice installation.

Signing (when the certificate exists):

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 dist\DCENT_Voice-Setup.exe
```

## macOS

On a native macOS host, the following command builds the PyInstaller payload,
explicitly downloads and embeds the shipped-default Parakeet model at pinned
revision `8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce`, assembles `DCENT
Voice.app`, and emits a versioned `.dmg`, `.zip`, and SHA-256 files:

```bash
bash scripts/build_macos_app.sh
```

Unsigned CI/local builds need no Apple credentials. Set
`MACOS_SIGNING_IDENTITY` to codesign with the hardened runtime. Notarization can
use `MACOS_NOTARY_KEYCHAIN_PROFILE`, App Store Connect API key variables
(`MACOS_NOTARY_KEY`, `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER`), or Apple ID
variables (`MACOS_APPLE_ID`, `MACOS_APP_PASSWORD`, `MACOS_TEAM_ID`). Partial
credentials fail closed instead of silently publishing an unnotarized artifact.

The unsigned `.app` / `.dmg` / `.zip` recipe is complete in-tree. Validate it
on any host (including Windows) with:

```bash
uv run python scripts/check_macos_pipeline.py
# or: bash scripts/build_macos_app.sh --check
```

A Mac binary still requires a Darwin runner; PyInstaller cannot cross-compile.
Signing and notarization require Apple credentials. Missing notarization is an
environment blocker, not a product win. The Darwin builder writes
`dist/macos-pipeline-status.json` stating `signed` / `notarized` honestly.
The `.app` is a drag-install bundle and therefore has no privileged uninstaller.
Turn off **Launch at startup** before deleting it, or remove only
`~/Library/LaunchAgents/tech.d-central.dcent-voice.plist`; deleting the `.app`
does not silently purge `~/Library/Application Support/DCENT_Voice` or Keychain
credentials.

Because a drag-install bundle can be anywhere, the LaunchAgent is rewritten in
full to the current `sys.executable` on **every** launch, so moving the `.app`
from the mounted `.dmg` to `/Applications` repairs its own login item on the
next start rather than leaving a plist pointing at an unmounted volume.
`dcent-voice doctor`'s `instance.autostart` check reports a login item whose
target no longer exists. Deleting the `.app` without first turning the setting
off leaves the plist behind — launchd will simply fail to start it.

## Linux

On a native Linux host, download a current `appimagetool`, set `APPIMAGETOOL` to
its executable path, and separately download a type-2 AppImage runtime. Supply
the runtime explicitly so `appimagetool` cannot fetch mutable `continuous`
bytes during a release build:

```bash
sudo apt-get install appstream build-essential python3-dev libgtk-3-dev libwebkit2gtk-4.0-dev \
  gir1.2-webkit2-4.0 \
  libgirepository1.0-dev libcairo2-dev pkg-config portaudio19-dev
APPIMAGETOOL=/path/to/appimagetool \
APPIMAGE_RUNTIME_FILE=/path/to/runtime-x86_64 \
  bash scripts/build_linux_appimage.sh
```

The command builds the native PyInstaller payload, embeds the shipped-default
Parakeet model, verifies its exact declared file set and hashes, then emits a
versioned AppImage, `.deb`, and SHA-256 files. The
Debian package installs the private payload under `/opt/dcent-voice` and a
launcher under `/usr/bin`; neither artifact requires a system Python.
Both layouts install the reverse-DNS AppStream metadata under `usr/share/metainfo`.
The Debian package recommends complete Wayland (`wl-clipboard` plus `wtype` or
`ydotool`) and X11 (`xclip` or `xsel`, plus `xdotool`) helper sets. Its
post-install hook and launcher report a non-fatal readiness warning if neither
complete path is present. AppImage cannot install host packages, so `AppRun`
performs the same explicit check at startup and names the required helpers.
The build version is rendered into its release entry,
so AppStream-aware software centers and AppImage tooling receive real product,
license, launchable, and privacy-oriented description metadata.
Every wrapper creates a private sealed payload with model bytes copied from
verified open handles, verifies the copied model tree immediately before its
archive/package command, and refuses wrapper shortcut inputs that cannot pass
the same check. Windows Setup similarly verifies extracted model hashes before
publishing or registering the install. These checks defend ordinary corruption,
links/reparse points, alternate streams, and same-user replacement races; they
do not claim to withstand an administrator who can mutate the running verifier
or its trusted code.
The script validates that metadata directly, then disables appimagetool's redundant
legacy filename lookup with `--no-appstream`.
The builder executes the frozen `dcent-voice platform-check` before wrapping
the payload. This imports GTK, WebKit2, and pywebview from the frozen archive,
so a missing `gi` bridge fails the build instead of reaching users as a broken
Settings window. The tagged CI job verifies the pinned runtime's byte length
and SHA-256 before passing it to `appimagetool --runtime-file`.

Tagged Linux artifacts are built on Ubuntu 22.04 (glibc 2.35), not Ubuntu
24.04 (glibc 2.39), to preserve compatibility with Jammy-era and newer
distributions. The build uses Jammy's WebKit2GTK 4.0 development ABI; the
runtime probe prefers WebKit2GTK 4.1/Soup 3 when present and deterministically
falls back to WebKit2GTK 4.0/Soup 2.4. Debian metadata accepts either runtime.
The frozen launcher restores PyInstaller's saved host library search path before
WebKit starts its renderer processes, preventing bundled Jammy libraries from
being injected into a newer distribution's system WebKit ABI. Validate the real
packaged settings lifecycle under a virtual X server with:

```bash
bash scripts/smoke_linux_settings.sh \
  dist/DCENT_Voice-linux-x86_64-0.2.0b1.AppImage 10
```

The smoke passes only when the settings process remains alive until the expected
timeout; startup crashes and early exits fail the command.

AppImage is portable and Debian package removal cannot safely mutate arbitrary
users' home directories. Turn off **Launch at startup** before removal, or
remove only `${XDG_CONFIG_HOME:-$HOME/.config}/autostart/dcent-voice.desktop`.
Package removal retains `${XDG_CONFIG_HOME:-$HOME/.config}/DCENT_Voice` and the
desktop keyring by default; purge those explicitly only when their loss is
intended.

The autostart entry is rewritten in full to the current `sys.executable` on
**every** launch. That matters most for AppImages, whose filename carries the
version: upgrading `DCENT_Voice-linux-x86_64-0.2.0.AppImage` to `…-0.3.0.AppImage`
would otherwise leave an entry pointing at a file the user deleted, and the
session would silently start nothing. Running the new AppImage once repairs it.
`dcent-voice doctor`'s `instance.autostart` check reports an entry whose `Exec=`
target no longer exists, and `desktop.*` reports the session type and the
clipboard/keystroke helpers that session actually needs.

## Online Install

```powershell
.\scripts\install.ps1
```

This creates `.venv` and installs the project with its runtime dependencies. End
users can alternatively download and unpack the Windows release ZIP. Contributors
should use an editable development install instead: `python -m pip install -e ".[dev]"`.

Published source distributions intentionally exclude `eval/`. That directory
contains development-only recordings, generated screenshots, and local corpus
manifests; it is not imported by the product and is not needed to rebuild the
wheel. Its checked-in attribution files retain canonical CC BY 4.0/CC0 links,
full governing texts under `packaging/licenses/`, and extraction/resampling or
noise-modification labels. Release tests inspect the built sdist to keep those
assets out rather than silently redistributing an incomplete evaluation corpus.

## Offline Bundle

Create bundle directories and a manifest without network access:

```powershell
python .\scripts\download_models.py --dry-run --bundle-dir .\build\offline-bundle
```

Build a real bundle on an online machine:

```powershell
python .\scripts\download_models.py --include-wheels --accept-model-license --bundle-dir .\build\offline-bundle
```

The live model fetch path requires `huggingface_hub` and explicit
`--accept-model-license`. It accepts only models with an immutable bundled
revision/hash manifest, resumes through the Hugging Face cache, and verifies
every staged file. The generated `dcent-voice-offline-bundle.json` records the
revision and SHA-256 maps for both models and every wheel, and must keep
`remoteUrls` empty. Offline install verifies a closed wheel directory against
those hashes before invoking `uv --no-index`; a missing, extra, linked, or
modified wheel fails closed. Runtime transcription never invokes this downloader
and passes `local_files_only=True`.

Wheel acquisition exports `uv.lock` with `uv export --locked`, resolves every
artifact against its exported hash using pinned pip 26.2.1, and builds the
DCENT_Voice wheel locally with `uv build --no-sources`. The legacy pure-Python
`proxy-tools` sdist is hash-verified and converted to a wheel. The upstream
PyAV wheel is intentionally not redistributed: DCENT_Voice always supplies
decoded float32 PCM, so the builder substitutes the source-visible, MIT-licensed
`av` compatibility wheel under `packaging/av-shim`. It satisfies Faster
Whisper's eager import and fails closed if file/codec decoding is attempted;
tests prove that it contains no FFmpeg/x264/x265/native codec payload. It does not
use the nonexistent `uv pip download` command. Tagged Windows releases repeat
this flow, verify the closed manifest, then install it with uv's `--offline`
and `--no-index` switches before building public artifacts.

Install from a completed bundle:

```powershell
.\scripts\install.ps1 -Offline -BundlePath .\build\offline-bundle
```

Use `-NoModels` only for script-level testing; a release offline bundle should include the recorded
faster-whisper model snapshots.

## CI Release Builds

Pushing a `v*` tag runs `.github/workflows/release.yml` on native Windows,
Ubuntu, and macOS runners. The jobs attach Setup.exe/ZIP, AppImage/`.deb`, and
`.dmg`/`.zip` artifacts plus checksums to one **draft** GitHub Release. Linux and
unsigned macOS artifacts require no signing secrets. macOS signing and
notarization activate when the documented certificate and API-key secrets are
configured.

## Hub Spawn

`packaging/launch.json` is the ADE hub spawn descriptor. It launches:

```powershell
.venv\Scripts\python.exe -m dcent_voice --no-tray --no-overlay
```

The fake-audio entry uses `DCENT_VOICE_FAKE_AUDIO=1` and disables hotkeys for ADE attach smoke runs.
Its static capability list is the public-beta baseline (STT plus sovereignty);
the runtime handshake is authoritative and adds TTS only when a compatible
source-preview backend is actually available.
