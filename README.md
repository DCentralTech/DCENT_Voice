# DCENT_Voice

**v0.2.0-beta.1 — public beta.** Local-first dictation with offline polish, snippets,
wake/toggle modes, and ADE/DVAP. Expect rough edges; please file issues.

DCENT_Voice is original D-Central Technologies software: an open-source,
local-first voice dictation app for digital sovereigns, and part of the **DCENT
stack** alongside [DCENT_ADE](#local-api). Hold a hotkey, speak, release — your
words are transcribed **on this machine** and typed into whatever app has focus.
No account, no cloud, and no Python or model download for the Windows Setup.

Dictation is a sovereignty problem. Every mainstream voice tool ships your voice
to someone else's server, where it becomes training data, a retention policy,
and a subpoena target. DCENT_Voice removes that dependency entirely: the speech
model runs on your hardware, the text never leaves it, and nothing works better
because you signed in. Cloud providers exist as an explicit, per-provider,
consent-gated option — off by default, logged when used, never a silent
fallback.

- **Push-to-talk dictation** (default `Ctrl+Win`), **command mode**, and
  **streaming dictation** that types stable words while you speak.
- **Local Parakeet transcription** (NVIDIA Parakeet TDT 0.6B v3 on **CPU ONNX
  by default**, with a faster-whisper fallback) — audio never leaves the machine.
- **Local writing on by default** — fillers, false starts, mid-utterance
  corrections (`5 actually 6`), and destination style (email / chat / code /
  formal) run on-device with no LLM. Optional local-LLM cleanup stays off
  unless you enable it.
- **Explicit-consent cloud providers** (OpenAI, Groq, Grok/xAI, Anthropic,
  Deepgram) with live key validation, a consent ledger, an egress log, and a
  clear privacy signal whenever anything would leave the machine.
- **System tray + voice-reactive overlay**, first-run setup wizard, per-app
  injection overrides, and a token-secured local API for DCENT_ADE attachment
  (including source-preview local TTS over DVAP when a compatible runtime and
  model assets are installed).

## Download or Install

> **[docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md) walks through the Windows
> install step by step, showing what you should see at each one** — the SmartScreen
> warning and how to verify the download, every Setup dialog, where the tray icon
> hides, and what to do when nothing appears. Read that if you are installing for
> the first time; the summary below is for people who already know the shape of it.

**Windows (normal path):** double-click `DCENT_Voice-Setup.exe`. It installs
per-user under `%LOCALAPPDATA%\DCENT_Voice`, adds a Start Menu shortcut, and
registers Add/Remove Programs. No Python, .NET SDK, Visual C++ redistributable,
admin prompt, model download, or terminal step. The Setup is **64-bit Windows 10
version 1809 (build 17763)+ or Windows 11** only — earlier builds do not ship the
.NET Framework 4.7.2 the app's windows need, and Setup refuses them with that
reason rather than installing something whose UI cannot open. Hold-to-talk
dictation uses the bundled CPU speech models. Settings, the overlay, and the
setup wizard need the Edge **WebView2** runtime (already present on typical
Windows 11 and on Windows 10 with Edge); Setup detects a missing runtime, says
so, and offers to open Microsoft's download page — it never installs it for you.
Before it offers to launch, Setup runs the app's own `doctor` self-check against
a throwaway profile and, if anything fails, keeps the install and tells you what
to fix instead of starting an app that will not work. A **DCENT_Voice
Diagnostics** Start Menu entry runs the same check any time.
Downloaded or offline-bundle ASR models live separately under
`%LOCALAPPDATA%\DCENT_Voice.Models`: upgrades and ordinary uninstall retain
them, while the explicit **purge user data** uninstall choice deletes them.
The first upgrade from an older Setup safely migrates only registry-declared,
allowlisted model assets after locking and hashing their bytes; unsafe or
ambiguous legacy trees stop the upgrade before the old app is replaced. A
historical `%LOCALAPPDATA%\DCENT_Voice` directory is adopted without an
executable only when its sole child is the plain `models` registry tree and
that complete tree passes the same closed-world verification.
The public-beta installer is **unsigned**, so SmartScreen may warn; verify the
SHA-256 next to the download against the GitHub release. Signing is an
environment blocker (Authenticode certificate), not a missing installer.

The ZIP (`DCENT_Voice-windows-*.zip`) remains available if you prefer to unpack
and run `dcent-voice.exe` yourself.

For a local source install:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install .
```

Contributors who will run tests and linters should use
`python -m pip install -e ".[dev]"` instead.

The base install includes onnx-asr (Parakeet) and faster-whisper on Windows,
macOS, and Linux. The **default profile is CPU ONNX Parakeet TDT 0.6B v3**.
Windows Setup.exe stages those weights next to the app so first dictation does
not need a network fetch. GPU acceleration remains optional: use the `auto` or
`gpu` profile after `python scripts/check_env.py` reports CUDA/cuDNN ready.

Wave 1 push-to-talk runtime dependencies are in the base install. If you are
using an environment that has not been installed from `pyproject.toml`, rerun
the install so `sounddevice`, `pynput`, `pystray`, and `Pillow` are
available.

Optional local TTS is a **source-preview feature**. The Windows public-beta
bundle intentionally omits its runtime engines while their license compatibility
is reviewed. Its public-beta model path currently exposes only Kokoro; Piper is
deferred pending compatible voice licensing. Settings reports whether a locally
installed Kokoro runtime is available; only then can it download checksum-pinned
model assets after an explicit egress confirmation. Model downloads are stored
locally and never upload microphone audio.

To enable that optional source preview, install its extra explicitly:

```powershell
python -m pip install -e ".[dev,tts]"
```

Review [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md) first; this extra is
not bundled with the Windows public beta.

### CPU-only machines (no high-end GPU)

You do **not** need an NVIDIA GPU. Use the default `desktop` profile or
`tiny` on older CPUs:

```powershell
# Built desktop payload, one fresh frozen process per run (includes model load)
python scripts/bench_e2e.py --executable dist/DCENT_Voice/dcent-voice.exe --repeat 3
# Whisper CPU fallback
python scripts/bench_latency.py --asr faster-whisper:base.en:cpu-int8 --repeat 3
# Lightest / older laptops
python scripts/bench_latency.py --asr faster-whisper:tiny.en:cpu-int8 --repeat 3
# Higher-quality CPU path (may exceed 800 ms short-utterance p50 — tradeoff is intentional)
python scripts/bench_latency.py --asr faster-whisper:distil-small.en:cpu-int8 --repeat 3
```

Profiles in Settings → Models:

| Profile | Hardware | When to use |
|---------|----------|-------------|
| **desktop** (default) | CPU ONNX Parakeet TDT 0.6B v3 | Fast automatic decoding for its documented 25 languages. Exact CC-BY-4.0 weights from pinned mirror revision `8f23f0c…` ship in native installers and are verified before loading. Supported explicit languages and Auto stay on Parakeet; unsupported languages or an unavailable Parakeet use shipped Whisper `base`. |
| **multilingual** | CPU int8 `base` | Explicit Faster Whisper alternative for corpus comparisons and languages outside Parakeet's documented set. |
| **tiny** | CPU int8 `tiny.en` | Older laptops / lightest footprint |
| **quality** | CPU int8 `distil-small.en` | Better accuracy on CPU; accepts slower finalize |
| **auto** | CUDA if ready, else CPU | GPU present *and* CUDA/cuDNN installed |
| **gpu** | Force CUDA float16 | Known-good NVIDIA stack |
| **accurate** | CPU int8 large-v3 | Best quality; slower on CPU |
| **whispercpp** | whisper.cpp `base.en` | Optional tournament backend. Slower than faster-whisper on this Windows CPU. |

### Optional GPU acceleration

On Windows CUDA systems, CTranslate2 needs compatible CUDA runtime DLLs and
cuDNN 8 DLLs on `PATH`. Run `python scripts/check_env.py` first. If CUDA is
incomplete, DCENT_Voice stays on CPU automatically (no account, no cloud).

On Linux, `pynput` builds the `evdev` extension from source, and audio capture
and injection need system packages:

```bash
# Debian/Ubuntu
sudo apt install gcc linux-libc-dev libportaudio2   # build + audio
sudo apt install xclip xdotool                      # X11 clipboard-paste injection
sudo apt install wl-clipboard ydotool               # Wayland equivalents
```

## First Run Config

The app reads `%APPDATA%\DCENT_Voice\config.toml` (via `platformdirs` on
macOS/Linux). If the file is missing, it is created from `config.example.toml`.
Defaults are local-only.

For a guided first run (pick and test your microphone, see your hotkeys, check
the speech model), launch the setup wizard:

```powershell
dcent-voice --setup
```

It is also available from the tray menu as "Setup wizard...".

## Useful Commands

```powershell
python scripts/check_env.py
python scripts/bench_latency.py --no-asr
python scripts/smoke_app.py
python -m pytest
uv run mypy src/dcent_voice --ignore-missing-imports --no-error-summary --show-error-codes --check-untyped-defs
python -m dcent_voice --print-config
python -m dcent_voice --self-test
python -m dcent_voice transcribe tests/fixtures/audio/hello.wav
python -m dcent_voice engine-info
# Shipped-artifact cold headless path; explicitly excludes capture and real injection
python scripts/bench_e2e.py --executable dist/DCENT_Voice/dcent-voice.exe --repeat 5
# Source-only warm diagnostic with simulated injection (not shipped/user-perceived E2E)
python scripts/bench_e2e.py --source-warm --repeat 5
python scripts/eval_dictation.py --skip-asr
python -m dcent_voice devices --bench --device-class gpu --samples-ms 420,455,480
```

## Troubleshooting (Windows)

**Start here: run diagnostics.** Start Menu → **DCENT_Voice Diagnostics** (or
`dcent-voice doctor --open`) checks the payload, the models, the native libraries,
WebView2, audio devices, whether a copy is already running and whether anything
reached the network, then writes a zip you can send us. It works even when the app
itself will not start — which is exactly when you need it.
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) explains every check id,
including the Linux and macOS `desktop.*` checks.

| Symptom | What to check |
|---------|----------------|
| Nothing happens at all after launching | Usually it *is* running: Windows hides new tray icons under the `^` chevron next to the clock — click it and drag the icon out. Otherwise run Diagnostics; `instance.mutex`, `env.install` and `history.last_startup_failure` name the cause, and a genuine startup failure now always shows a dialog and writes `%APPDATA%\DCENT_Voice\logs\startup.log`. |
| Settings / overlay / wizard will not open | The Edge **WebView2** runtime is missing (`ui.webview2` in Diagnostics). Install it from <https://go.microsoft.com/fwlink/p/?LinkId=2124703>, or use the tray's **Install WebView2 runtime…** entry. Hold-to-talk dictation is unaffected. |
| Hotkey does nothing | Tray menu **Hotkeys:** line. If FAILED, wait a few seconds for auto-reconnect or restart. Logs: `%APPDATA%\DCENT_Voice\logs\dcent_voice.log`. |
| Overlay flashes then nothing | Mic muted / wrong device — overlay should say **No speech detected**. Run setup or set `[audio].input_device`. |
| Long speech stops at ~60s | By design: `[audio].auto_stop_seconds` (default 60) finalizes the hold. Raise it (and `max_seconds`) in config if you need longer. |
| Repetitive nonsense (“should be able to…”) | Fixed by anti-hallucination ASR decode + density filters. Update and restart. Optional: stronger model (`large-v3`) on GPU. |
| Process died mid-hold | Update past the session-monitor fix; check `%APPDATA%\DCENT_Voice\logs\dcent_voice_fault.log`. |
| Slow after speaking | ASR time grows with length; overlay shows **Transcribing…**. If LM Studio is off, cleanup falls back to raw (optional: `cleanup_enabled = false`). |
| App says already running | Another instance owns the lock. Use the existing tray icon, or end `dcent-voice` / python processes. |
| After sleep/lock | Hooks auto-rebind (listener watchdog + sleep tick poll); no restart required. |
| Health check | `GET http://127.0.0.1:8765/health` returns overall `ok`. With the session bearer token it also includes `subsystems.hotkeys.status` (`ok` / `recovering` / `dead`) and privacy posture. |

Self-test without starting the full UI:

```powershell
dcent-voice --self-test
```

Hardware/model smoke tests are opt-in:

```powershell
python -m pytest --run-hw
```

Tests that touch real hotkeys, the live clipboard, foreground windows, input
injection, or physical audio devices are collected in the interactive suite and
skip unless the exact opt-in is present:

```powershell
$env:DCENT_VOICE_ALLOW_INTERACTIVE_TESTS = "1"
python -m pytest -m interactive
```

Routine pytest and CI runs never set that variable.

The ASR smoke test uses [tests/fixtures/audio/hello.wav](tests/fixtures/audio/hello.wav)
and skips cleanly if the selected optional runtime or verified model is absent.

Run the real-audio accuracy corpus with an atomic report (file-only: no
microphone, hotkeys, clipboard, or injection):

```powershell
python scripts/eval_dictation.py --language-mode auto --output-json artifacts/dictation-eval.json
```

When faster-whisper is installed, run a local ASR timing pass:

```powershell
python scripts/bench_latency.py --asr faster-whisper:base:cpu-int8 --repeat 2
```

To turn real finalize-latency samples into a profile recommendation, run:

```powershell
python -m dcent_voice devices --bench --device-class gpu --samples-ms 420,455,480
```

Use `--device-class cpu` for CPU-only benches, `--bench-url http://127.0.0.1:.../bench/latency`
for a local service returning `{ "finalizeMs": [...] }`, and `--json` when ADE or CI needs
machine-readable evidence. The GPU target is p50 finalize under 500 ms; the CPU target is under
800 ms.

See [docs/ASR_BACKENDS.md](docs/ASR_BACKENDS.md) for the backend abstraction,
current controlled tournament, model/license status, and admission protocol for
Parakeet, faster-whisper, whisper.cpp, Nemotron, and community parakeet.cpp ports.

## Privacy Model

Transcription is local by default: the desktop profile uses the bundled,
hash-verified Parakeet TDT 0.6B v3 ONNX model, with the bundled pinned Faster
Whisper `base` model as its offline fallback. Neither path needs an account or
external service. The
optional AI cleanup pass is off in the shipped default profile; when you enable
it, it uses a local LLM (Ollama or LM Studio) unless you explicitly select a
cloud provider. Cloud providers must be selected explicitly and will require a
consent record before runtime requests are allowed. Egress logs record
timestamp, provider key, payload type, and byte count only; content is never
written to the egress log.

Current local/cloud status is derived from the active providers:

- `sovereign`: all active providers are local.
- `hybrid`: at least one active provider is cloud and at least one is local.
- `cloud`: all active providers are cloud.

Cloud ASR or LLM startup is blocked until consent exists in the consent ledger.

## Quick Start

After dependencies are installed:

```powershell
dcent-voice
```

Hold `Ctrl+Win`, speak, and release. The utterance is recorded, transcribed by
the local Parakeet default (or its local Faster Whisper fallback), and pasted
into the focused app; your previous clipboard
is restored after a short target-app delay and the app returns to idle. Clipboard
restoration preserves every byte-materializable pasteboard item/MIME target,
including text, HTML/RTF, images, and copied files. If any advertised format
cannot be cloned losslessly, injection fails before clipboard mutation (an
eligible native Windows edit control may use its non-clipboard fallback).
`Ctrl+C` in the terminal exits cleanly. The tray menu always shows the current
dictation hotkey.

## Settings

```powershell
dcent-voice --settings
```

The settings window uses pywebview with a dark dashboard UI in the D-Central
design system (ember `#FF6E00` accent; bundled Barlow Condensed / Inter /
JetBrains Mono — no CDN). It reads and writes the same TOML config, lists local
Ollama/LM Studio/faster-whisper models when reachable, connects cloud accounts
with live key validation (keys are stored through the OS keychain via
`keyring`, never in the config), shows privacy state, and displays the egress
log. It also exposes service settings, overlay behavior, hotkey capture,
injector defaults, per-app injector overrides, and an optional failed-dictation
recovery vault. Recovery is off by default. When enabled, it keeps only usable
text that could not be inserted—never successful dictation or microphone
audio—with configurable item/age limits, per-item copy/delete, and immediate
purge when disabled. If Windows refuses deletion because the vault is open or
permissions changed, Settings reports that retained bytes may remain and offers
Clear for an explicit retry.

Slow or remote applications can opt out of clipboard timing entirely. For example:

```toml
[injector]
paste_delay_s = 0.3
# restore_clipboard = false

[injector.per_app]
"mstsc.exe" = "keystroke"
```

The **Models** page reports optional local TTS runtime availability. In a source
environment that has a compatible Kokoro runtime installed, it can install
verified assets after a clear egress confirmation. Piper is deferred pending
compatible voice licensing. The Windows public beta leaves optional TTS engines
out pending license review, so it does not advertise `tts.*`.

The overlay signals state visually: a springy entrance, an ember ring pulse that
tracks loudness while listening, particles orbiting a voice-reactive waveform,
a spinning arc while transcribing, a distinct tint in Command Mode, and a
confirmation pop when text is injected.

## Local API

The app starts a local FastAPI service when `[service].enabled = true`.

- `GET /health`
- `POST /transcribe`
- `POST /command`
- `WS /events`
- `WS /stream`

`/transcribe` and `/command` require the per-session bearer token. WebSocket
clients should send it in the log-safe `dcent.bearer.<base64url>` subprotocol;
query-token authentication remains only for legacy external clients. Browser
cross-origin connections are rejected. The token is written to the protected
local registry entry so DCENT_ADE can discover it. See
[docs/ADE_API.md](docs/ADE_API.md).

## Manual Checklist

- Paste target: Notepad.
- Paste target: VS Code.
- Paste target: Chrome or Edge text field.
- Paste target: Windows Terminal.
- Elevated Notepad: injection is expected to fail because normal user input
  cannot reach elevated windows.
- Rapid double PTT: no stuck recording state.
- Clipboard restore: every original clipboard format returns after paste.
- Overlay: click-through, no focus steal, waveform tracks loudness.
- Multi-monitor: overlay appears on the active foreground-window monitor.
- Ollama killed during cleanup: raw ASR text is still injected.
- Win+V clipboard history: users should understand dictated text may appear in
  Windows clipboard history during clipboard-paste injection.
- Per-app override: Windows Terminal should use keystroke injection when
  configured in `[injector.per_app]`.
- UIA selection: Command Mode should prefer UI Automation selected text when
  available and fall back to clipboard probing.

## Packaging

DCENT_Voice supports uv-based source installs and an optional offline bundle.
Tagged releases build native artifacts on all three desktop platforms:
`DCENT_Voice-Setup.exe` and ZIP on Windows, AppImage and `.deb` on Linux, and
`.dmg` and `.zip` on macOS. Every native payload embeds the shipped-default
Parakeet model and the immutable, hash-verified Faster Whisper `base` fallback,
so both English and Multilingual/Auto work without a first-use download. See
[docs/PACKAGING.md](docs/PACKAGING.md),
[scripts/build_installer.ps1](scripts/build_installer.ps1),
[scripts/install.ps1](scripts/install.ps1),
[scripts/download_models.py](scripts/download_models.py), and
[packaging/launch.json](packaging/launch.json).

[packaging/DCENT_Voice.spec](packaging/DCENT_Voice.spec) is platform-aware.
The native builders are [scripts/build_pyinstaller.ps1](scripts/build_pyinstaller.ps1),
[scripts/build_linux_appimage.sh](scripts/build_linux_appimage.sh), and
[scripts/build_macos_app.sh](scripts/build_macos_app.sh).

## About D-Central Technologies

[D-Central Technologies](https://d-central.tech) builds decentralized technology
for digital sovereignty — hardware, firmware, and software that keeps control on
the user's side of the wire. DCENT_Voice is D-Central original work, written in
the open, and is part of the DCENT stack: tools that assume your machine is
yours, that treat the network as optional, and that never require an account to
do their job.

If that is the kind of software you want to exist, the most useful things you
can do are file issues, report what breaks on your hardware, and tell someone
else it exists.

## License

DCENT_Voice is open source by D-Central Technologies — decentralized
technologies for digital sovereignty — and is released under the
[MIT License](LICENSE). Every source file carries an
`SPDX-License-Identifier: MIT` header; bundled third-party components and their
licenses are itemized in [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

Source, issues, and release notes: [DCentralTech/DCENT_Voice](https://github.com/DCentralTech/DCENT_Voice).
