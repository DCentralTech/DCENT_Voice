# DCENT_ADE Local API

Default base URL: `http://127.0.0.1:8765`

**Attach contract version:** `api_version = "1"` (stable embedder fields; independent of the app release). Headless: no tray, Settings UI, or global hotkeys.

## Attach in 5 minutes

A running DCENT Voice writes `%LOCALAPPDATA%\DCENT\modules\dcent-voice.json` plus a bearer token. It is loopback-only by default. An explicit `[service] allow_lan = true` with a wildcard or non-loopback bind publishes `sovereigntyClass: LAN`; the transport is plaintext HTTP/WebSocket, while local STT capability processing remains `LOCAL`. No silent egress.

```python
from dcent_voice.attach import VoiceAttachClient

with VoiceAttachClient.discover() as client:
    print(client.capabilities()["api_version"])  # "1"
    print("compose" in client.capabilities()["features"])
    print(client.ready()["ready"])               # True even if hotkeys are down
    print(client.transcribe_file("recording.wav", polish=False)["raw"])
    print(client.learn("vip", "I'm Ada", app_context="Code.exe")["ok"])  # ADE attach learn records app-scoped learned written forms.
    print(client.personalization()["stores_audio"])  # False
    print(client.transcribe_file("recording.wav", style="formal", app_context="Code.exe")["cleaned"])
    print(client.transcribe({"audio": [0.0] * 1600, "samplerate": 16000, "style": "formal", "app_context": "Code.exe"})["cleaned"])
    print(client.compose("vip", style="formal", app_context="Code.exe")["text"])
    print(client.stream([0.1] * 16000, style="formal", app_context="Code.exe")["text"])
    client.cancel()  # next one-shot returns rejected_reason=cancelled
```

Copy-paste references:

- HTTP / ADE: `examples/attach_ade.py`
- In-process (no service): `examples/attach_engine.py` and `VoiceEngine.from_config()`
- CLI: `dcent-voice transcribe recording.wav --json` and `dcent-voice engine-info`

Errors are `{ "detail": ..., "error": { "code", "message", "retryable" } }`. Codes include `unauthorized`, `consent_required` (`403` when a live cloud grant was revoked or became invalid), `invalid_audio`, `unsupported_language`, `payload_too_large`, `busy`, `invalid_request`.

Streaming: in-process `engine.transcribe_stream(chunks)`, copy-paste `VoiceAttachClient.stream`, or `WS /stream` (token query). ADE attach stream writing style keeps app-scoped learned written forms. ADE attach transcribe oneshot writing style keeps app-scoped learned written forms. Headless transcribe_stream writing style keeps app-scoped learned written forms. Headless transcribe writing style keeps app-scoped learned written forms. Headless transcribe_file writing style keeps app-scoped learned written forms. REST `POST /cancel` and `GET /ready` do not require the desktop UI.

## Outbound Command Mode dispatch to DCENT ADE

When Command Mode produces a `tool_call`, DCENT Voice looks for
`dcent-ade.json` in the same per-user DVAP module registry and reads the current
bearer token from its `tokenRef`. The registry `endpoint` is the exact HTTP
receiver for the tool-call JSON. It must name `127.0.0.1`, `localhost`, or `::1`
with an explicit port; remote, LAN, credential-bearing, non-HTTP, malformed,
and symlinked entries fail closed before a request. The token reference must be
a bounded regular file inside the registry directory. Both the entry and token
must pass the platform's read-only owner-access verification before either is
trusted.

The request uses `Authorization: Bearer <token>`, does not follow redirects,
and does not trust `HTTP_PROXY` / `HTTPS_PROXY`. Tool POSTs are deliberately
single-shot: a connect/read timeout cannot prove ADE did not already commit a
side effect, and the current protocol has no negotiated idempotency key. The
obsolete `DCENT_VOICE_ADE_ENDPOINT` environment value is ignored:
transcript-derived automation is not a cloud-consent path and may not be
redirected off-machine. Bearer values and underlying request exceptions are
not included in exposed failure messages. The registry and token are read again
for each tool call so an ADE restart can rotate its session credential.

The outbound grammar is intentionally narrow:

- `open` requires exactly one string `target`, and the target must be a known
  non-destructive ADE surface such as Settings, Board, Files, Terminal, or
  Orion.
- `search` and `summarize` require exactly one non-empty, control-free string
  `target` of at most 256 characters.
- `create` preserves the existing Command Mode content-creation action but uses
  the same exact bounded, control-free `{ "target": "..." }` schema. Extra
  operation/path/command fields are rejected.
- Shell/process/file operations, unknown or case-mutated names,
  missing/extra/nested arguments, and malformed targets are rejected before
  registry access.
- Any selected text disables tool automation for that utterance. Selection
  transforms stay in their dedicated local rewrite path; untrusted selected
  content cannot become an ADE command argument.

## Authentication

Each run generates a per-session token and writes it to the module registry
(`%LOCALAPPDATA%\DCENT\modules\dcent-voice.token`; the `tokenRef` field of
`dcent-voice.json` gives the exact path). Token and live registry publication
require a verified owner-only mode/ACL. If the platform cannot apply or verify
that boundary, DCENT_Voice removes partial artifacts and stops the local API
instead of exposing an attach service with an insecure credential. Read the
token from that file, then:

`VoiceAttachClient.discover()` verifies that both registry and token are
owner-only regular files, requires `moduleId: dcent-voice`, and rejects symlinks,
out-of-registry token references, or non-loopback endpoints. Its owned HTTP
client ignores proxy environment variables and redirects. Streaming carries the
bearer in the `dcent.bearer.<base64url>` WebSocket subprotocol, never in the
request query; the server retains query-token admission only for legacy external
clients. Uvicorn access logging is disabled so request targets are not retained.

- `POST /transcribe` and `POST /command` require `Authorization: Bearer <token>`
  (missing or wrong token → `401`).
- `WS /events` and `WS /stream` prefer the token as a
  `dcent.bearer.<base64url(token)>` WebSocket subprotocol. A query token remains
  accepted only for legacy external clients. Connections that send an `Origin`
  header are rejected with close code `1008` unless the origin is on the ADE
  webview allowlist (local ADE UI hosts only — not arbitrary browsers).
- `WS /dvap` (the DVAP envelope) accepts the token as a query parameter **or**
  as a WebSocket subprotocol `dcent.bearer.<base64url(token)>` (required for
  browser webviews that cannot set `Authorization`). Origin and auth failures
  use distinct close codes — see the table below.
- `GET /health` is reachable without a token but returns liveness only
  (`{"ok": ..., "subsystems": {"service": {"ok": true}}}`). Send
  `Authorization: Bearer <token>` to get privacy posture and per-subsystem
  detail (hotkeys, pipeline, capture).

### WebSocket close codes

| Code | Endpoint(s) | Meaning |
|---|---|---|
| `1008` | `/events`, `/stream`, `/dvap` | Origin/auth policy rejection; on an admitted DVAP session, also a message-policy violation or live cloud-consent block. A consent block first sends `module.sovereignty` with `consentState: "required"`. |
| `4401` | `/dvap` | Bearer-token authentication failed (missing or wrong token). Per the DVAP spec, auth failure is a distinct `4401` rather than the generic `1008`. |
| `4400` | `/dvap` | Capability negotiation failed — the first message was not a valid `hello`, or it required a capability this module does not implement (see below). |

`/events` and `/stream` close a bad/missing token with `1008` as well (they do
not separate origin from token). Only `/dvap` reports the DVAP-specified `4401`.

## Health

```http
GET /health
```

Without a token, returns liveness only:

```json
{
  "ok": true,
  "subsystems": { "service": { "ok": true } }
}
```

With `Authorization: Bearer <token>`, the response also includes privacy
posture and per-subsystem status:

```json
{
  "ok": true,
  "privacy": {
    "status": "sovereign",
    "providers": [],
    "missing_consents": []
  },
  "subsystems": {
    "service": { "ok": true },
    "hotkeys": { "ok": true, "status": "ok" },
    "pipeline": { "ok": true, "state": "idle" },
    "capture": { "ok": true }
  }
}
```

Authenticated health also includes `api_version`, `ready` (headless STT accepts
work even when desktop hotkeys are down), `hardware`, and `requires_tray` /
`requires_hotkeys` (both false).

```http
GET /ready
Authorization: Bearer <token>
```

Same headless readiness snapshot without desktop subsystem detail.
`desktop_ok` reports whether tray/hotkeys/pipeline are healthy; it does not
gate attach.

```http
POST /cancel
Authorization: Bearer <token>
```

Sets a cancel flag. The next `POST /transcribe` returns
`rejected_reason: "cancelled"` and does not run ASR. In-process
`VoiceEngine.cancel()` is the same contract.

## Capabilities

```http
GET /capabilities
Authorization: Bearer <token>
```

Headless discovery for ADE and other embedders. Does not require the Settings
UI, tray, or hotkeys. Returns `api_version` (`"1"`), `requires_tray` /
`requires_hotkeys` / `requires_settings_ui` (all false), engine version,
provider/model, `hardware` (local auto device/compute, never egress), and
feature flags including `wav_b64`, provider-specific `language_hint`,
`streaming`, `cancel`, and `ready`.
`language_hint.codes` is the exact list accepted by the active provider. This
is derived from the actual injected/resolved provider, not merely the selected
configuration profile, so an embedder can make a reliable locality and
language decision before submitting audio.

## Compose

```http
POST /compose
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "text": "vip",
  "style": "formal",
  "polish": true,
  "cleanup_level": "medium",
  "app_context": "Code.exe"
}
```

Text-only. No audio, tray, or hotkeys. Same local `compose_dictation` path as desktop / CLI / `VoiceEngine.compose`. ADE text compose writing style keeps dictionary written forms. ADE text compose writing style keeps snippet expansions. ADE attach compose writing style keeps dictionary written forms. ADE attach compose writing style keeps snippet expansions. ADE attach compose writing style keeps learned written forms. ADE attach compose writing style keeps app-scoped learned written forms. Learned terms come from `POST /learn` / `VoiceAttachClient.learn` (no audio). Omit `app_context` to apply global terms only; scoped terms fail closed.

```python
print(client.compose("vip", style="formal")["text"])
print(client.compose("sig", style="formal")["text"])
print(client.compose("vip", style="formal", app_context="Code.exe")["text"])
```

## Transcribe

```http
POST /transcribe
Content-Type: application/json
```

```json
{
  "audio": [0.0, 0.1, -0.1],
  "samplerate": 16000,
  "cleanup": true,
  "polish": true,
  "style": "email",
  "app_context": "outlook.exe",
  "cleanup_level": "medium"
}
```

Prefer a WAV when attaching from another process — JSON float arrays are still
accepted for compatibility:

```json
{
  "wav_b64": "<base64 16-bit PCM WAV>",
  "language": "en"
}
```

An explicit `language` must be in the active provider's advertised recognized
code set (normally ISO-639-1). xAI additionally documents Filipino as `fil`.
Invented codes such as `zz` are rejected before audio conversion or egress.
`auto`, `detect`,
or an empty string requests provider auto-detection only when
`language_hint.auto` is true; omitted/null retains the configured local-provider
default and omits the cloud hint. OpenAI and Groq
receive the code in their documented transcription multipart `language`
field. xAI receives multipart `language` plus `format=true`, which applies the
hint to inverse text normalization; keyterms remain independent. The shipped
The shipped Parakeet v3 model automatically decodes the exact 25 languages in
its pinned upstream model card. Supported explicit language requests and Auto
therefore remain on the faster verified Parakeet path. The current `onnx-asr`
adapter has no language-bias control and returns no detected-language field:
capabilities disclose `effect: metadata_only` and
`reports_detected_language: false`; Auto results use an empty/unknown language
rather than inventing English or a detection. An unsupported explicit language
routes to pinned local Faster Whisper `base`. A directly selected `.en` Whisper
model rejects auto/non-English requests before model load.
Faster Whisper and whisper.cpp honor the hint per call when using multilingual
models; an explicitly selected `.en` model similarly rejects non-English.

Provider references: [OpenAI create transcription](https://platform.openai.com/docs/api-reference/audio/createTranscription),
[Groq speech-to-text](https://console.groq.com/docs/speech-to-text), and
[Deepgram model languages](https://developers.deepgram.com/docs/models-languages-overview),
and [xAI speech-to-text](https://docs.x.ai/developers/model-capabilities/audio/speech-to-text).

Returns raw and cleaned text plus stage timings. `raw` is ASR as-decoded.
`cleaned` defaults to the same local `compose_dictation` path the desktop
uses (ramble rewrite + destination tone, no network). `raw` is always ASR
as-decoded. Pass `polish=false` for a raw STT client. Sticky unpolished ADE writing style keeps dictionary written forms. Sticky unpolished ADE writing style keeps snippet expansions. Optional
`cleanup_level` (`none` | `light` | `medium` | `high`, default `medium`)
selects the same local Auto Cleanup analog as the desktop. High is local
hedge brevity (lead, mid-clause with or without commas, stacked, trailing),
not a cloud LLM.
Optional LLM cleanup, when configured, runs on the composed string.
`style` still selects email / chat / code / formal. The shared service
engine lock serializes ASR/cleanup access so the API path does not load
duplicate models.

When `app_context` is supplied, the local personalization store applies only
corrections whose style/app scope matches this request (plus global terms).
`raw` remains the untouched ASR result. Omit context when the client does not
know its destination; scoped terms then fail closed instead of leaking into an
unrelated app.

Headless CLI (no tray or hotkeys):

`dcent-voice transcribe --style formal --app Code.exe` applies only matching
app-scoped learned terms (plus global terms). Omit `--app` for global terms
only; other apps fail closed. CLI compose writing style keeps app-scoped
learned written forms: `dcent-voice compose --style formal --app Code.exe vip`.
CLI learn records app-scoped learned written forms:
`dcent-voice learn --from vip --to "I'm Ada" --app Code.exe`.

```powershell
dcent-voice transcribe recording.wav --json
dcent-voice transcribe recording.wav --style formal --app Code.exe
dcent-voice compose --style email hey can you send the deck to alice actually bob thanks
dcent-voice compose --cleanup-level high I guess I think we should ship Monday
dcent-voice learn --from "vip" --to "I'm Ada"
dcent-voice learn --from "vip" --to "I'm Ada" --app Code.exe
dcent-voice compose --style formal vip
dcent-voice compose --style formal --app Code.exe vip
dcent-voice engine-info
```

Python attach:

```python
from dcent_voice.engine import VoiceEngine
engine = VoiceEngine.from_config()
print(engine.transcribe_file("recording.wav", style="formal", polish=False).text)
# Shipped default real speech keeps accuracy latency reliability.
# Shipped default real speech corpus keeps WER CER.
# Shipped default hold-release injection keeps reliability.
# Shipped default long-form real speech keeps WER CER.
# Shipped default hold-release injects real apps.
# Shipped default acoustic hold-release injects real apps.
# Shipped default first dictation stays warm.
# Shipped default idle CPU stays near zero.
# Shipped default product dictation keeps WER CER.
# Shipped default streaming dictation stays responsive.
# Shipped default multilingual dictation keeps WER CER.
# Shipped default model load stays bounded.
# Shipped default dictation CPU stays bounded.
# Shipped default accented dictation keeps WER CER.
# Shipped default silent install lands onedir.
# Shipped default noisy dictation keeps WER CER.
# Shipped default named dictation keeps WER CER.
# Shipped default loaded RAM stays bounded.
# Shipped default transcribe tail stays bounded.
# Shipped default silent uninstall removes onedir.
# Shipped default learned vocabulary keeps product names.
# Shipped default streaming tail stays bounded.
# Sticky unpolished headless transcribe_file writing style keeps dictionary written forms.
# Sticky unpolished headless transcribe_file writing style keeps snippet expansions.
print(engine.compose("vip", style="formal"))
# Headless compose writing style keeps dictionary written forms.
print(engine.compose("sig", style="formal"))
# Headless compose writing style keeps snippet expansions.
print(engine.learn("vip", "I'm Ada", app_context="Code.exe")["ok"])
# Headless learn records app-scoped learned written forms.
print(engine.compose("vip", style="formal", app_context="Code.exe"))
# Headless compose writing style keeps app-scoped learned written forms.
```

The learned-vocabulary score helpers keep the shipped model and conservative
configuration unchanged. They explicitly pass `prose_context=True` only for the
repository-owned fixed prose WAV so the measurement demonstrates contextual
learning without representing trusted prose as the product default.

## Learn (typed correction)

```http
POST /learn
Authorization: Bearer <token>
```

```json
{
  "spoken": "d sent",
  "written": "DCENT_Voice",
  "style": "code",
  "app_context": "Code.exe"
}
```

ADE attach learn records app-scoped learned written forms.
ADE learn records app-scoped learned written forms.
`style` and `app_context` are optional scopes. Exact mappings apply after the
first explicit correction. Conservative separator and inflection variants are
enabled only after the same mapping is confirmed again; ambiguous scoped rules
fail closed. Longer-utterance learned rewrites require the caller to set
`prose_context: true`; the default is `false`, and no destination/app heuristic
enables it. REST accepts only the literal JSON booleans `true` and `false`;
strings, numbers, containers, and `null` are rejected. Code style always refuses
longer learned rewrites. Without explicit trusted-prose context, only
whole-utterance corrections run. `/learn` is deliberately stateless and always
requires both `spoken` and `written`; correction-only requests are rejected, so
one authenticated client can never claim another client's recent transcript.
Clear URLs, email
addresses, paths, filenames, and code-like literals remain unchanged even when
the prose flag is set. Never accepts audio, never learns from passive transcript
history, and never sends terms over the network. Last utterance lives in process
memory only for direct desktop/engine clients, never as ADE `/learn` provenance.
The JSON store is inspectable/resettable and bounded to 400 terms
of at most 256 characters per side.

For direct `PersonalizationStore` consumers, omitted `enabled` / `learn`
arguments restore valid saved policy; explicit boolean constructor arguments
override valid saved policy. Legacy v1/v2 stores without policy retain their enabled
defaults. Current v3 stores require exact version, policy, term-array, and term
schemas; unsupported versions, non-finite JSON, or any malformed member reject
the complete payload without rewriting it. Safe legacy migration validates its
complete term list before publishing any term. Loading is capped at 6 MiB and
16 JSON nesting levels before decode; this covers the bounded 400-term schema,
including worst-case legacy Unicode escaping, while corrupted resource bombs
remain inert. V3 timestamps use the writer's canonical UTC-seconds RFC 3339
form and supported 1970-2199 range; valid legacy offsets/fractions are migrated
to that form. Same-path writers use a bounded advisory lock and a three-way
reload/merge transaction, so a stale policy save cannot erase a correction or
resurrect an explicit reset. Successful writes flush and sync the temporary
file before atomic replacement and sync the containing directory where the
platform supports it; timeout and durability failures are reported instead of
being acknowledged as successful.
An unchanged stale policy preserves the newer disk policy. An explicit local
`update_policy` or constructor override wins when the disk still matches its
loaded base; independently divergent local and remote policy edits conflict
and leave the disk unchanged.
When a store is injected into `VoiceEngine`, the engine configuration governs
that engine's operations through validated call-scoped policy. It never mutates
the store's direct-caller policy, so multiple engines can safely share a store.
Each engine also owns and generation-orders its last-utterance context;
`learn_last` never reads another engine's shared-store slot, and consumes a
source generation at most once.

```http
GET /personalization
Authorization: Bearer <token>
```

Returns learned terms, learned destination styles (`app_styles`), `stores_audio: false`, and whether a last utterance is in memory. Destination styles are local only: they never leave the machine and are not sent to cloud STT.

CLI:

```powershell
dcent-voice learn --from "d sent" --to "DCENT_Voice" --style code --app Code.exe
dcent-voice learn --style email --app notepad.exe
```

## Command

```http
POST /command
Content-Type: application/json
```

```json
{
  "transcript": "what's 2+2",
  "selection": ""
}
```

Returns a `CommandIntent`:

```json
{
  "action": "insert_text",
  "text": "4",
  "tool_call": null,
  "confidence": 0.95,
  "reason": "arithmetic"
}
```

## Events WebSocket

```text
WS /events
```

Mirrors typed app events from the internal event bus:

```json
{
  "type": "PrivacyChanged",
  "payload": {
    "status": "sovereign",
    "detail": "",
    "consent_state": "",
    "reason": "",
    "missing_providers": []
  }
}
```

## Stream WebSocket

```text
WS /stream
```

P4 endpoint with per-connection rolling audio and VAD gating. Send JSON audio blocks:

```json
{
  "audio": [0.0, 0.1, -0.1],
  "samplerate": 16000,
  "final": false,
  "style": "formal",
  "polish": true,
  "app_context": "Code.exe",
  "prose_context": true
}
```

Optional `style` / `polish` / `app_context` / `prose_context` stick for the
utterance (same as REST `/transcribe`). `prose_context` accepts only a literal
JSON boolean and defaults to `false`; set it to `true` only when the caller knows
the audio is trusted prose and wants longer learned rewrites. ADE stream writing
style keeps app-scoped learned written forms. Omit `app_context` or send a
different app to fail closed for scoped terms. DVAP `audio.in.begin` may carry
the style, polish, and app-context fields; they apply when `audio.in.end`
finalizes the PCM stream. ADE stream / DVAP stream writing style keeps dictionary
written forms. DVAP stream writing style keeps app-scoped learned written forms.

The server returns `silence` when the VAD gate rejects a non-final block,
`partial` for active speech blocks, and `final` when `final` is true. Responses
include the latest `partial` text and the stable `committed` prefix.

## DVAP WebSocket

```text
WS /dvap
```

The DCENT Voice Attachment Protocol (DVAP) envelope ADE negotiates against. The
normative protocol is `DCENT_ADE/docs/attachment-protocol.md` (DVAP v1.0 + the
v1.1 sovereignty extension); every message conforms to
`docs/schemas/dvap/message.schema.json`. Authentication and origin rejection
follow the token/close-code rules above (`4401` for a bad token, `1008` for a
browser origin).

### Handshake

ADE connects as the client and sends `hello`; the module replies `welcome`:

```json
{
  "type": "hello",
  "protocol": "dvap",
  "version": "1.1",
  "moduleId": "dcent-voice",
  "capabilities": ["stt.partial", "stt.final", "voice.model.download"],
  "sovereigntyClass": "LOCAL",
  "capabilitySovereignty": [
    { "capability": "stt.final", "sovereigntyClass": "LOCAL",
      "reason": "Audio transcription remains on this machine." },
    { "capability": "voice.model.download", "sovereigntyClass": "SERVER_EGRESS",
      "reason": "Model asset download can contact upstream registries only after explicit user action." }
  ]
}
```

```json
{
  "type": "welcome",
  "sessionId": "session_1a2b3c4d5e6f7080",
  "acceptedVersion": "1.1",
  "capabilities": ["stt.partial", "stt.final"]
}
```

Negotiation rule: a requested capability the module serves is accepted; a
capability the module recognizes but does not serve today (e.g.
`voice.model.download`, a discovery/egress descriptor rather than a session
capability) is optional and silently dropped from the accepted set; a capability
outside the DVAP vocabulary is an unknown **required** capability and fails
negotiation (close `4400`). The accepted protocol version is the client's if
this build implements it, otherwise the highest version this build implements.

### Capabilities advertised

Always: `stt.partial`, `stt.final`, `audio.in.stream`, `text.compose`, and
`voice.model.download`.

### Text compose

After `welcome` includes `text.compose`, ADE may send a text-only rewrite with
no audio, tray, or hotkeys. Same local `compose_dictation` path as desktop /
CLI / `POST /compose`. DVAP compose writing style keeps dictionary written
forms. DVAP compose writing style keeps snippet expansions.
DVAP compose writing style keeps learned written forms.
DVAP compose writing style keeps app-scoped learned written forms.

```json
{ "type": "text.compose", "text": "vip", "style": "formal", "app_context": "Code.exe" }
```

```json
{ "type": "text.composed", "text": "I'm Ada", "style": "formal" }
```

DVAP PCM stream (`audio.in.begin` → binary → `audio.in.end`) may carry
`app_context` the same way. DVAP stream writing style keeps app-scoped learned written forms.

```json
{
  "type": "audio.in.begin",
  "requestId": "phone_1",
  "sampleRate": 16000,
  "channels": 1,
  "encoding": "pcm_s16le",
  "style": "formal",
  "app_context": "Code.exe"
}
```

When a **TTS backend is available** (Wave E1 — Kokoro model assets are present
on disk), the module additionally advertises and accepts `tts.append`,
`tts.cancel`, and `barge_in`. On a fresh install with no TTS models downloaded,
these stay **unadvertised**: there is no playback for an interrupt to cancel and
no meaningfully emittable `barge_in`. The capability list always reflects real
service state, so ADE can rely on `welcome.capabilities` to decide whether to
send `tts.append`.

In a source environment with a compatible local Kokoro runtime, install it from
**Settings -> Models**. The user must confirm the pinned model download;
DCENT_Voice records a metadata-only `voice.model.download` attempt before any
network request, ignores ambient proxy settings, permits only HTTPS destinations
and manually validated HTTPS redirects, verifies every asset SHA-256, enables
the backend only after the complete install, and requires an app restart before
`tts.*` is advertised. Failed transfers and checksum mismatches retain the
zero-byte attempt record without recording model content. Piper is deferred
pending compatible voice licensing. The Windows
public beta intentionally omits the optional TTS runtimes while license
compatibility is reviewed.

### STT messages

After `welcome`, ADE sends a schema-defined `audio.in.begin`, one or more binary
16 kHz mono PCM frames, and the matching `audio.in.end`. Inline `/stream` JSON
audio blocks are not accepted on DVAP. The module returns DVAP-shaped STT
messages bridged from the identical transcription flow:

```json
{ "type": "stt.partial", "text": "run the", "stable": false }
{ "type": "stt.final", "text": "run the tests and fix the failure" }
```

`stable: false` is ghost text; a partial is `stable: true` only once its
committed prefix covers the whole hypothesis.

### TTS messages (Wave E1)

Advertised only when a TTS backend is available (see above). ADE streams reply
text to be spoken as it is generated; the module synthesizes it sentence-by-
sentence and plays it locally on the speakers.

```json
{ "type": "tts.append", "text": "Running the tests now." }
{ "type": "tts.append", "text": " All checks passed.", "final": true }
{ "type": "tts.cancel" }
```

- `tts.append` — incremental text to speak. The module buffers it into whole
  sentences (never speaks a half-word; skips code by default) and begins playback
  on the first complete sentence. `final: true` flushes any trailing partial
  sentence at end-of-utterance. First audio starts **< 800 ms** after the first
  `tts.append` (CPU).
- `tts.cancel` — stop speaking immediately. Audible output ceases in **< 100 ms**
  and any queued/in-flight synthesis is abandoned.

While TTS is playing, capture is half-duplex: the microphone is paused or ducked
(config `[tts].mic_policy`) so it does not hear the speakers. `duck` scales
captured samples and the level meter to `[tts].duck_gain`, then restores normal
gain when playback ends or is cancelled.

### Barge-in (Wave E1)

When a local activation interrupts playback — today a push-to-talk press — the
module cancels playback and tells ADE so it can stop streaming the reply:

```json
{ "type": "barge_in", "source": "ptt" }
```

`source` is one of `ptt`, `wake_word`, `vad` (wake-word/VAD arrive with Wave E2).

### Sovereignty messages (v1.1)

When the privacy ledger's data-flow class changes (a consent grant/revoke, or an
observed egress differing from the declared `LOCAL` class), the module pushes:

```json
{ "type": "module.sovereignty", "sovereigntyClass": "LOCAL", "observedClass": "CLOUD" }
```

Consent transitions add optional `consentState`, `reason`, and
`missingProviders` fields. If a grant is revoked or the ledger becomes invalid
during an already-negotiated cloud STT session, the attempted egress is blocked,
the module reports `observedClass: "LOCAL"` with `consentState: "required"`, and
then closes the session with policy code `1008`.

Model-asset downloads report `observedClass: "SERVER_EGRESS"` with a
`voice.model.download` capability block. ADE treats the observed class as
authoritative over the declared class.
