# Changelog

All notable changes to DCENT_Voice are documented here. This project follows
[Semantic Versioning](https://semver.org).

## [Unreleased]

Reliability, offline depth, and cross-platform work on top of the public beta.

### Fixed
- **DCENT_Voice did not start on any machine that had never run it before, and
  it failed silently.** The frozen build ships `config.example.toml` inside
  `_internal\`, but the first-run config seeder never looked there. On an
  account with no `config.toml` the seed raised, `main()` turned that into a
  parser error and exited — and because the executable is windowed with logging
  not yet configured, there was no message, no log line, and no dialog. The app
  appeared for a second and vanished. It worked on development machines only
  because a `config.toml` already existed. The seeder now finds the bundled
  example in a frozen layout, and first-run failures surface a dialog and a log
  line instead of exiting silently.
- **Windows Setup reinstalling over an existing folder** no longer leaves a
  half-replaced install; reinstall and upgrade paths are explicit about what
  they keep, migrate, and refuse.
- **Streaming retract on macOS and Linux** no longer silently no-ops, so a
  retracted word is actually removed on every platform rather than only on
  Windows.
- **Offline bundle defaults** resolve correctly for the shipped desktop ASR
  model, so a machine that never downloads anything still transcribes.
- Spoken corrections keep their lead-in words instead of eating them, and
  "I meant" rewrites only the intended span.

### Added
- **`doctor` self-check subcommand** — reports microphone, model, injection,
  hotkey, and config health against a throwaway profile, with secrets redacted.
  Windows Setup runs it after installing and, if anything fails, keeps the
  install and says what to fix rather than launching an app that will not work.
  A **DCENT_Voice Diagnostics** Start Menu entry runs the same check any time.
- **Deeper offline dictation** with no LLM and no network: spoken structure cues
  (`new line`, `new paragraph`, `press enter`, bullet lists), spoken corrections
  (`scratch that`, `delete last word/sentence/line`, `replace X with Y`,
  `no I meant …`), starter snippets, and local Auto Cleanup levels.
- **Opt-in failed-dictation recovery** — when injection fails, the transcript is
  recoverable instead of lost.
- **DVAP admits DCENT_ADE webview clients** through an explicit origin allowlist
  and a bearer subprotocol, so an embedded ADE surface can attach without
  loosening cross-origin rules for everyone else.
- Explicit code-switch evidence in multilingual routing, and host-safe routine
  validation with owned child-process launch.

### Changed
- **CPU is the default path.** DCENT_Voice no longer assumes a high-end GPU:
  the shipped desktop model runs on CPU ONNX by default, with GPU used when it
  is actually available. Machines without an NVIDIA card get a working install
  rather than a fallback that fails at first transcription.
- Streaming dictation commits on three-pass agreement instead of two, trading a
  little latency for materially fewer typed mis-hears.
- Agent and CI workspaces are isolated from the developer's live profile, so a
  test run can never write to a real user's configuration.

## [0.2.0-beta.1] — 2026-07-10 — final public beta

Round 2: modernize, optimize, accounts, cross-platform — plus a final
release-hardening pass.

### Release hardening (final pass)
- **Clipboard safety and compatibility**: clipboard injection snapshots common
  text, rich-text, image, locale, and copied-file formats before dictation,
  restores only after a configurable target-app delay, and skips restoration if
  another application writes newer clipboard data. Formats backed only by owner-
  rendered or GDI handles cannot be restored faithfully; dictated text remains
  on the clipboard in that case instead of destroying data.
- **Streaming injection serialization**: incremental and final streaming deltas
  share an injection gate, and routing adds a defense-in-depth lock, preventing
  interleaved pastes and post-stop stragglers.
- **Release readiness**: startup failures now tear down acquired locks and worker
  threads, self-test verifies that the configured Whisper model loads, cloud LLM
  health checks use a short read timeout, public DVAP schemas are canonical and
  test-validated, and scheduled Windows packaging smoke coverage guards spec drift.
- **Settings save no longer wipes config** (RT-UX-1): saving from Settings now
  merges onto the on-disk TOML instead of re-serializing a fixed section list,
  so `config_version`, `[tts]`, `audio.max_seconds`/`auto_stop_seconds`, and
  any future/unknown sections survive every save. The merged config is
  validated **before** the file is written (a bad patch raises and leaves disk
  untouched), and the write is atomic (temp file + replace).
- **Settings/wizard can no longer boot on placeholder values** (RT-UX-2): the
  pages wait for the pywebview bridge (up to 15 s, then browser-preview mode)
  instead of falling back to a stub config after 1.5 s — and every
  config/credential mutation is refused when the bridge is absent, so stub
  values can never be saved over real settings. Save errors now surface as
  toasts instead of failing silently.
- **Config reload off the event bus** (RT-REL-1): applying a settings change
  (which can load a multi-GB speech model) now runs on a worker thread instead
  of the bus dispatcher, so hotkeys stay live during a reload; a failed reload
  keeps the previous settings and toasts instead of half-applying.
- **Streaming dictation**: a straggling streaming pass can no longer swallow
  the tail of the utterance at finalize (the committer is not advanced once
  stop is signalled), and shutdown joins the streaming thread so an in-flight
  decode is never torn down mid-inference (RT-ASR-1/4).
- **Local API hardening**: `/transcribe` payloads are bounded (audio length,
  samplerate range, prompt length — RT-SEC-1); interactive OpenAPI docs are
  disabled (RT-SEC-14); unauthenticated `/health` returns liveness only, with
  privacy posture and subsystem detail requiring the session token (RT-SEC-4);
  service readiness uses uvicorn's in-process flag instead of trusting a
  spoofable HTTP shape (RT-SEC-2).
- **Packaging**: the Windows build is now windowed (no console box) and
  UPX-free (a classic antivirus false-positive trigger) — RT-UX-8.
- **Update check understands pre-releases**: 0.2.0 final counts as newer than
  0.2.0-beta.1, and beta.2 as newer than beta.1.
- **UX comprehensibility**: the tray now leads with "Hold Ctrl+Win and speak to
  dictate" (and uses it as the tooltip), "Run setup" is "Setup wizard",
  "Cleanup enabled" is "AI cleanup (optional)", and Settings drops jargon
  ("PTT mode" → "Push-to-talk: Hold to talk / Press to toggle", "Injector
  default" → "Insert text via", clearer switch labels).

### Fixed
- **Whisper “crazy loop” transcripts** (e.g. endless “should be able to…”):
  faster-whisper now forces `condition_on_previous_text=False`, `temperature=0`,
  `no_repeat_ngram_size=3`, segment no-speech/compression filters, and whole-
  transcript density/repetition rejection before inject. Dictionary prompts use
  natural written forms (not `spoken -> written` meta syntax). Cleanup rejects
  invented essays / preambles when LM Studio is online.
- **Hard crash during long push-to-talk on Windows**: the session-resume
  monitor used a custom ctypes Win32 message window that triggered native
  `access violation` faults (process died mid-hold with no `Shutting down`
  log). It now uses **tick-gap polling only**; hotkey listener rebind still
  covers sleep/resume. Long holds no longer kill the process via that path.
- **Silent hotkey death on Windows**: global push-to-talk hooks are supervised.
  If pynput's keyboard listener dies (sleep/resume, session lock, hook-chain
  breakage), a watchdog rebinds it with backoff and publishes
  `HotkeyHealthChanged`. Tray shows Hotkeys OK / reconnecting / FAILED and
  toasts on permanent failure or recovery. `/health` now reports subsystem
  status (`hotkeys`, `pipeline`, `capture`) and sets `ok: false` when hotkeys
  are enabled but dead — so a green service is no longer a false "voice works"
  signal.
- **Silence / too-short felt like a dead app**: discarded utterances now flash
  an overlay "No speech detected" / "Too short" state (and optional tray toast)
  instead of vanishing with no feedback. Focus-steal paths toast
  "Copied to clipboard".
- **Config mic device ignored after reload**: `AudioCapture.set_device` applies
  `audio.input_device` changes; PortAudio status flags are logged.
- **pythonw / headless crash invisibility**: `sys.excepthook`,
  `threading.excepthook`, atexit flush, and faulthandler dump to the user log
  directory. Event-bus subscriber errors are logged instead of swallowed.
- **Session unlock / power resume**: Windows session monitor force-rebinds
  hotkeys on unlock and APM resume.

### Added
- **Long-speech limits**: `[audio] max_seconds` (ring capacity, default 90) and
  `auto_stop_seconds` (soft PTT stop, default 60). At auto-stop the utterance
  finalizes with overlay/tray feedback instead of recording forever or
  silently wrapping the ring buffer.
- Cleanup **circuit breaker** + quieter failure logs when LM Studio/Ollama is
  down (raw transcript still used; no multi-page traceback spam).
- Overlay shows **Transcribing Ns…** during finalize; re-asserts no-activate
  styles on show; amplitude pump at 15 Hz.
- `dcent-voice --self-test` probes hotkey listener liveness and exits.
- `scripts/check_env.py` starts a brief pynput Listener to verify hooks.
- **DVAP TTS family** (`WS /dvap`): `tts.append` (sentence-streamed synthesis of
  incremental reply text through the active backend, first audio < 800 ms on
  CPU), `tts.cancel` (audible stop < 100 ms), and `barge_in` emission when a
  push-to-talk press interrupts playback (`source: ptt`; wake-word/VAD in Wave
  E2). `tts.append`/`tts.cancel`/`barge_in` are advertised in `hello`/`welcome`
  and the module registry entry **only when a TTS backend is actually available**
  (model assets present) — a fresh install still negotiates STT only. Closes the
  Wave E0 deviation that deferred TTS/barge_in. All messages conform to the
  shared `docs/schemas/dvap` schemas.
- **Local text-to-speech** (`dcent_voice.tts`): a `TtsBackend` protocol with
  **Kokoro-82M** as its source-preview default (Apache-2.0, via `kokoro-onnx` on
  the onnxruntime that already ships with faster-whisper — no torch). Model assets
  are not bundled; they are consent-gated downloads (`voice.model.download`,
  declared `SERVER_EGRESS`) with SHA-256 verification and a license note written
  beside each asset. A `SentenceStream` chunker turns incremental text into
  whole-sentence synthesis units (never splits mid-word, respects
  abbreviations/decimals, skips fenced/inline code per a configurable policy).
  Playback cancels in **under 100 ms** and enforces a half-duplex barge-in policy
  (`[tts].mic_policy` — pause or duck the mic while speaking; a push-to-talk press
  cancels playback). TTS is **off by default** and advertises nothing to ADE until
  a runtime and assets are present. It is source-preview only in this public beta;
  the Windows bundle omits optional TTS runtimes while license compatibility is
  reviewed. Piper is deferred pending compatible voice licensing; XTTS remains
  excluded (ADR V003). New `[tts]` config section; CI never downloads models
  (deterministic `FakeTtsBackend`; real synthesis is opt-in via
  `--run-tts-models`).
- **DVAP attachment envelope** (`WS /dvap`): `hello`/`welcome` capability
  negotiation per `DCENT_ADE/docs/attachment-protocol.md`, the STT message family
  (`stt.partial` with a `stable` ghost-text flag, `stt.final`) bridged from the
  same transcription flow as `/stream`, and `module.sovereignty` (v1.1) pushed
  from the privacy ledger on consent change and observed model-download egress
  (`SERVER_EGRESS`). Auth failure closes `4401` and a browser origin closes
  `1008` (both documented in `docs/ADE_API.md`); messages conform to the shared
  `docs/schemas/dvap` schemas, with a test that guards against vector drift
  between the two repos. The module registry entry now carries DVAP 1.1
  `capabilitySovereignty` blocks. Wave E1 TTS capabilities (`tts.*`) and
  `barge_in` are advertised only when a compatible backend and its model assets
  are available.
- **First-run setup wizard** (`--setup`, tray "Run setup"): guided microphone
  test/selection, hotkey overview, and speech-model status.
- **In-app account connection with live validation**: API keys are verified
  against the provider before saving; per-provider "get your key" and privacy
  links. New **Grok (xAI)** provider (OpenAI-compatible).
- **"Sign in with Grok"** OAuth 2.0 device-code flow (activates once an xAI
  OAuth client is registered; API-key connect works today).
- **Streaming dictation hotkey** — live incremental transcription that types
  stable words as you speak.
- **Cross-platform text injection** — macOS (NSPasteboard + CGEvent) and Linux
  (X11 / Wayland via xclip/wl-clipboard + xdotool/ydotool) clipboard-paste
  injectors, selected automatically per OS.
- **Launch-at-startup** now actually registers with the OS (Windows Run key;
  macOS LaunchAgent / Linux XDG autostart scaffolding).

### Changed
- **Brand alignment** to D-Central's design system: ember `#FF6E00` accent,
  Barlow Condensed / Inter / JetBrains Mono (bundled, no CDN), refined grounds.
- **Resource use**: the speech model unloads after idle and reloads on demand;
  the microphone closes between utterances; the overlay animation loop stops
  when hidden; the cleanup worker pool is reused.
- Config location now resolves via `platformdirs` (unchanged on Windows).

### Security
- The local ADE API now requires a per-session bearer token and rejects
  cross-origin WebSocket connections. Transcript text is no longer written to
  disk logs unless `DCENT_VOICE_LOG_TRANSCRIPTS=1` is set.
- A transcript is never lost: on any injection/ASR/cleanup failure it is left on
  the clipboard. Tray profile and cleanup toggles now work. Dictation into
  terminals/consoles types instead of a no-op paste.

### Known limitations (beta)
- Streaming dictation types words as soon as two consecutive passes agree; a
  rare mis-hear that both passes agree on cannot be retracted after it is
  typed. Push-to-talk dictation (the default) is not affected.
- Changing the Local API host/port applies after an app restart.

## [0.1.2]
- Internal pre-public iteration; no public release artifact.

## [0.1.1]
- Internal pre-public iteration; no public release artifact.

## [0.1.0] — 2026-07-06
- Initial local-first runtime (Waves W0–W5), followed by first-launch fixes:
  launch-blocking bugs, CPU fallback when CUDA fails at inference time,
  per-utterance logging, and microphone device selection.
