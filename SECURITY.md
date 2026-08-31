# Security Policy

## Reporting a vulnerability

Please report security issues privately to **security@d-central.tech** rather
than opening a public issue. Include reproduction steps and the affected version.

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x beta | Yes |
| 0.1.x and earlier | No |

We target acknowledgement within three business days, initial triage within seven
days, and a status update at least every 14 days while remediation is active.
Disclosure is coordinated with the reporter after a fix is available; complex
issues may take longer, and we will communicate material timeline changes.

## Security posture

DCENT_Voice is local-first by design:

- **Voice stays on device.** Audio is transcribed locally by default. Cloud
  providers are opt-in, require an explicit consent record, and every egress is
  logged (metadata only — never content).
- **Offline is enforced in-process, not merely intended.** Before any other module
  is imported, `dcent_voice.util.bootlog` sets `HF_HUB_OFFLINE`,
  `HF_HUB_DISABLE_TELEMETRY`, `HF_HUB_DISABLE_IMPLICIT_TOKEN`,
  `TRANSFORMERS_OFFLINE` and `DO_NOT_TRACK` to `1`, overriding a `0` inherited
  from the parent environment. `huggingface_hub` reads those exactly once, at
  import time, so setting them anywhere later would be decorative. Only
  `scripts/download_models.py` and `scripts/qa/warm_model_cache.py` — the explicit
  release-build model fetches — opt out, with `DCENT_VOICE_ALLOW_HUB=1`.
  `ParakeetASRProvider.load()` additionally asserts the resolved snapshot
  directory exists immediately before calling `onnx_asr.load_model(path=…)`,
  because `onnx_asr` chooses between "load these files" and "download this repo"
  purely on whether that path exists: a snapshot that vanished between
  verification and load (unmounted drive, OneDrive dehydration, antivirus
  quarantine) would otherwise turn an offline-by-design app into one that reaches
  for the network.
- **You do not have to take that on faith.** `dcent-voice doctor` runs the real
  model load and an idle period under an in-process socket monitor and reports
  every non-loopback connection or name-resolution attempt made through
  Python's socket API as the `egress.connections` check. Native libraries
  (ONNX Runtime, CTranslate2) sit below that monitor; they are vouched for by
  the closed-world payload verification and the absence of network code paths,
  not by the monitor. The
  expected result is none, and CI asserts it on Windows, Linux and macOS. The
  fresh-machine QA protocol (`docs/QA_FRESH_MACHINE.md`) repeats it in a Windows
  Sandbox with networking switched off entirely.
- **What network activity people do sometimes observe** on a first run is
  Microsoft Defender SmartScreen performing a reputation lookup on a large,
  freshly downloaded, unsigned executable. That is the operating system, not this
  application, and it is attributed to the process that Windows is inspecting.
  Code signing is the only real fix; until the certificate exists, releases say so
  and publish SHA-256 sidecars to verify against.
- **Transcripts are not logged to disk** unless `DCENT_VOICE_LOG_TRANSCRIPTS=1`
  is set for debugging. Failed-dictation recovery is a separate, explicit
  Settings opt-in: it retains only usable text that could not be inserted,
  never successful dictation or audio. Its owner-only local vault is bounded by
  item count and age, uses atomic private-file publication, and is purged when
  the feature is disabled.
- **The local ADE API** is enabled by default on `127.0.0.1:8765`, is restricted
  to loopback hosts unless you explicitly set `[service] allow_lan = true`,
  and requires a per-session bearer token for every operation or detail beyond
  the minimal unauthenticated liveness response from `GET /health`. WebSocket
  upgrades reject browser origins outside the ADE webview allowlist. HTTP
  endpoints rely on bearer authentication and do not grant cross-origin access;
  browser preflights are rejected and no `Access-Control-Allow-Origin` header is
  emitted. Disable the service with `[service] enabled = false`.
- **Voice-to-ADE tool dispatch stays local and authenticated.** Command Mode
  reads `dcent-ade.json` and its `tokenRef` from the local DVAP module registry,
  requires an HTTP(S) loopback endpoint, and sends the token as a bearer header.
  `DCENT_VOICE_ADE_ENDPOINT` and proxy environment variables cannot redirect
  transcript-derived tool calls. Only `open`, `search`, `summarize`, and the
  existing bounded-content `create` command pass strict argument schemas;
  destructive/unknown names, extra arguments, unsafe open targets, and
  automation while text is selected fail before discovery or network access.
  Side-effecting POSTs are single-shot because a timeout may follow an ADE
  commit; failures expose neither bearer material nor provider exception
  context.
- **Credentials** (API keys, OAuth tokens) are stored in the OS keychain via
  `keyring`, never in plain text or in the config file. The per-session ADE
  bearer token is written to a local registry file under the user profile
  (`tokenRef` in the module registry) with verified owner-only ACL restriction
  — not the keychain — so ADE can discover it on the same machine. If that
  restriction cannot be applied and verified, token/registry publication fails
  closed and the local service is stopped. Atomic secret writers restrict and
  verify the still-empty temporary before writing any bearer or transcript
  bytes; Windows ACL helpers run without opening a console window.
- **Attach credentials stay out of proxy and access logs.** The bundled attach
  client rejects untrusted registry/token paths and ACLs, ignores proxy
  environment variables and redirects, and sends WebSocket credentials in an
  authenticated bearer subprotocol rather than a query string. Local-service
  access logging is disabled.
- **Local LLM traffic cannot be proxy-redirected.** Ollama and LM Studio require
  an explicit-port loopback HTTP(S) endpoint; their clients and Settings probes
  ignore proxy environment variables and redirects. Cloud LLM and ASR clients
  use the same transport isolation in addition to live consent and metadata-only
  pre-wire auditing.
- **Provider sign-in is separately consented.** API-key validation and OAuth
  device-code requests use an `auth:<provider>` / `credentials` consent record,
  independent of later audio/text consent. A zero-byte metadata attempt is
  written before every request or poll; keys, tokens, device codes, credential
  lengths, transcripts, and audio are never written to that log. Revocation or
  an unavailable audit log blocks the request before network access.
- **Update checks are manual.** GitHub is contacted only when the user selects
  the update-check action in Settings; there is no background update polling.
  The request requires HTTPS, ignores ambient proxy settings, and does not
  follow redirects.
- **Model downloads are explicit and auditable.** Optional TTS model retrieval
  requires model-specific consent and a writable metadata-only egress log. An
  attempt record is persisted before the request, ambient proxy settings are
  ignored, redirects are followed manually only to another HTTPS destination,
  and the downloaded bytes must match the shipped SHA-256 pin before install.
- **ASR models survive application replacement.** On Windows, explicitly
  installed/offline-bundle ASR models are isolated in
  `%LOCALAPPDATA%\DCENT_Voice.Models`, outside the Setup payload. Ordinary
  upgrades and uninstall retain that tree; only an explicit purge deletes it.
  Legacy migration is registry-bound, path/file allowlisted, reparse-resistant,
  handle-locked, hash-verified, and transactionally merged. An unsafe,
  conflicting, or ambiguous model tree fails the upgrade before replacement.
  The executable-free historical default is accepted only as a models-only
  closed world at the exact per-user path; lookalike or mixed-content
  directories are never adopted as application installs.

## Known limitations

- Injected keystrokes/paste cannot reach elevated (admin/UAC) windows on Windows
  by design (integrity isolation).
- Dictated text may appear in the OS clipboard history (e.g. Win+V) when the
  clipboard injector is used.
- If failed-dictation recovery is explicitly enabled, failed transcript text is
  readable to the signed-in OS user until its configured retention limit,
  successful manual deletion, or successful feature-disable purge. A sharing or
  permission failure is shown explicitly instead of claiming deletion; close
  the program holding the vault and retry Clear. The vault is not encrypted
  beyond the operating system's owner-only file protection; use full-disk
  encryption when protection against offline disk access is required.
- Binding the ADE API off loopback requires an explicit `allow_lan = true`
  config choice; treat that as intentional LAN exposure of the local service.
  Off-loopback HTTP/WebSocket transport is plaintext and the registry advertises
  `sovereigntyClass: LAN` (while local STT processing remains declared `LOCAL`).
  Use it only on a trusted network or behind a separately managed TLS boundary.
