# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""DVAP (DCENT Voice Attachment Protocol) envelope for the local service.

This adds the `/dvap` WebSocket endpoint that ADE speaks to negotiate a session
and receive the STT message family and `module.sovereignty` observations. The
normative protocol is `DCENT_ADE/docs/attachment-protocol.md` (DVAP v1.0 + the
v1.1 sovereignty extension); messages conform to `message.schema.json`.

Roles on this endpoint: ADE connects as the WebSocket client and sends `hello`;
this module (the DCENT Voice service) replies with `welcome`, then streams STT
and sovereignty messages. The existing `/events` and `/stream` endpoints are
left untouched — `/dvap` is additive.

Close codes:
- ``1008`` — an `Origin` header was present (a browser); matches `/events` and
  `/stream`.
- ``4401`` — bearer-token authentication failed (missing/wrong token).
- ``4400`` — capability negotiation failed (see :class:`NegotiationError`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import secrets
from collections.abc import Callable
from typing import Any

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from dcent_voice.dictation.style import STYLE_NAMES
from dcent_voice.events import (
    AppEvent,
    EventBus,
    HotkeyPressed,
    PrivacyChanged,
    WakeWordDetected,
)
from dcent_voice.privacy import ConsentRequired
from dcent_voice.service.api import (
    MAX_COMMAND_CHARS,
    AudioValidationError,
    ComposeRequest,
    ConnectionLimiter,
    ServiceBusyError,
    ServiceEngine,
    run_bounded_worker,
)
from dcent_voice.service.streaming import StreamingSession, StreamMessage
from dcent_voice.service.voice_control import VoiceControlError, VoiceRuntimeControl
from dcent_voice.service.ws import _offer_latest, origin_allowed
from dcent_voice.sovereignty import (
    DVAP_PROTOCOL,
    DVAP_SUPPORTED_VERSIONS,
    DVAP_VERSION,
    KNOWN_CAPABILITIES,
    STT_CAPABILITIES,
    SovereigntyClass,
    default_capability_sovereignty,
    served_capabilities,
    sovereignty_class_for_status,
)
from dcent_voice.tts import (
    AudioSink,
    CodePolicy,
    MicGate,
    PlaybackEngine,
    RefCountMicGate,
    SoundDeviceSink,
    TtsBackend,
    TtsPlayer,
)

# Valid `barge_in.source` values per message.schema.json.
BARGE_IN_SOURCES = ("ptt", "wake_word", "vad")

# A browser WebSocket cannot set an Authorization header, so ADE carries its
# bearer credential as a WebSocket subprotocol: ``dcent.bearer.<base64url(token)>``
# (base64url, no padding), offered alongside the plain ``dvap.v1`` protocol. Per
# RFC 6455 the server selects exactly one offered subprotocol on accept; we echo
# back the validated ``dcent.bearer.*`` value so the negotiated `socket.protocol`
# reflects the credential channel ADE opened on.
DVAP_SUBPROTOCOL = "dvap.v1"
BEARER_SUBPROTOCOL_PREFIX = "dcent.bearer."

# WebSocket close code used when capability negotiation fails. DVAP has no
# error message type, so the server closes the socket instead of replying.
DVAP_CLOSE_NEGOTIATION = 4400
DVAP_CLOSE_AUTH = 4401
DVAP_CLOSE_ORIGIN = 1008
DVAP_CLOSE_POLICY = 1008
DVAP_CLOSE_TOO_LARGE = 1009
DVAP_CLOSE_BUSY = 1013

MAX_DVAP_CONNECTIONS = 4
DVAP_OUTBOUND_QUEUE_MAX = 32
MAX_TTS_APPEND_CHARS = 4_096
MAX_TTS_PENDING_CHARS = 32_768
MAX_TTS_PENDING_MESSAGES = 64
MAX_STREAMED_AUDIO_BYTES = 16_000 * 2 * 60
MAX_SYNTH_AUDIO_BYTES = 24_000 * 2 * 60
MAX_APP_CONTEXT_CHARS = 128

# The module's declared data-flow class (registry + hello). Observed egress may
# differ and, per the extension, wins in ADE's UI/enforcement model.
MODULE_SOVEREIGNTY_CLASS = SovereigntyClass.LOCAL.value


class NegotiationError(Exception):
    """Raised when a `hello` requests an unknown required capability (or is malformed)."""


class DVAPMessageError(ValueError):
    """An admitted session sent a policy-invalid or oversized message."""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


# --- Message builders -----------------------------------------------------------


def build_hello(
    module_id: str = "dcent-voice",
    *,
    version: str = DVAP_VERSION,
    capabilities: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a `hello` describing this module (used in tests and for symmetry)."""

    from dcent_voice.sovereignty import ADVERTISED_CAPABILITIES

    return {
        "type": "hello",
        "protocol": DVAP_PROTOCOL,
        "version": version,
        "moduleId": module_id,
        "capabilities": list(capabilities or ADVERTISED_CAPABILITIES),
        "sovereigntyClass": MODULE_SOVEREIGNTY_CLASS,
        "capabilitySovereignty": default_capability_sovereignty(),
    }


def build_welcome(
    session_id: str, accepted_version: str, capabilities: list[str]
) -> dict[str, Any]:
    return {
        "type": "welcome",
        "sessionId": session_id,
        "acceptedVersion": accepted_version,
        "capabilities": list(capabilities),
    }


def build_module_sovereignty(
    *,
    sovereignty_class: str = MODULE_SOVEREIGNTY_CLASS,
    observed_class: str | None = None,
    capability: str | None = None,
    capability_sovereignty: list[dict[str, str]] | None = None,
    consent_state: str | None = None,
    reason: str | None = None,
    missing_providers: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Build a `module.sovereignty` message, omitting unset optional fields.

    ``message.schema.json`` forbids additional properties, so ``None`` fields are
    dropped rather than emitted as ``null``.
    """

    message: dict[str, Any] = {
        "type": "module.sovereignty",
        "sovereigntyClass": sovereignty_class,
    }
    if observed_class is not None:
        message["observedClass"] = observed_class
    if capability is not None:
        message["capability"] = capability
    if capability_sovereignty is not None:
        message["capabilitySovereignty"] = capability_sovereignty
    if consent_state:
        message["consentState"] = consent_state
    if reason:
        message["reason"] = reason
    if missing_providers:
        message["missingProviders"] = list(dict.fromkeys(missing_providers))
    return message


def model_download_sovereignty() -> dict[str, Any]:
    """`module.sovereignty` for an observed speech-model download (SERVER_EGRESS)."""

    return build_module_sovereignty(
        observed_class=SovereigntyClass.SERVER_EGRESS.value,
        capability="voice.model.download",
        capability_sovereignty=[
            {
                "capability": "voice.model.download",
                "sovereigntyClass": SovereigntyClass.SERVER_EGRESS.value,
                "reason": "Observed model asset download egress.",
            }
        ],
    )


def stream_message_to_dvap(message: StreamMessage) -> dict[str, Any] | None:
    """Translate a `/stream` :class:`StreamMessage` into a DVAP STT message.

    Reuses the exact transcription flow `/stream` drives. ``silence`` blocks
    carry no transcript and map to nothing. A `partial` is marked ``stable`` only
    once its committed prefix covers the whole hypothesis (nothing volatile left);
    otherwise it is ghost text (``stable=false``).
    """

    if message.type == "final":
        return {"type": "stt.final", "text": message.text}
    if message.type == "partial":
        text = message.partial or message.committed
        stable = bool(message.committed) and message.committed == message.partial
        return {"type": "stt.partial", "text": text, "stable": stable}
    return None


def build_barge_in(source: str) -> dict[str, Any]:
    """Build a `barge_in` message. ``source`` must be a valid schema enum value."""

    if source not in BARGE_IN_SOURCES:
        raise ValueError(f"invalid barge_in source: {source!r}")
    return {"type": "barge_in", "source": source}


def barge_in_for_event(event: AppEvent) -> dict[str, Any] | None:
    """Map a local activation event to a `barge_in`, or ``None`` to skip.

    A push-to-talk press (``HotkeyPressed``) that lands while TTS is speaking is a
    barge-in: the caller cancels playback and the module tells ADE with `barge_in`
    so it can stop streaming reply tokens. Wake-word events are emitted only by
    a loaded local model, never by a timer or simulated state.
    """

    if isinstance(event, HotkeyPressed):
        return build_barge_in("ptt")
    if isinstance(event, WakeWordDetected):
        return build_barge_in("wake_word")
    return None


def module_sovereignty_for_event(event: AppEvent) -> dict[str, Any] | None:
    """Map an app event to a `module.sovereignty` message, or ``None`` to skip.

    A `PrivacyChanged` (which fires on consent grant/revoke and provider changes)
    reports the module's declared class against the observed data-flow class the
    new privacy state implies. When observed matches declared (still fully local)
    no observation is added; ADE only needs to act on a divergence.
    """

    if not isinstance(event, PrivacyChanged):
        return None
    observed = sovereignty_class_for_status(event.status).value
    if event.consent_state in {"revoked", "required", "blocked"}:
        # A configured cloud provider is not an observed cloud flow while the
        # explicit consent gate is closed.
        observed = SovereigntyClass.LOCAL.value
    consent_state = event.consent_state or None
    reason = event.reason or event.detail or None
    missing_providers = event.missing_providers or None
    if observed == MODULE_SOVEREIGNTY_CLASS:
        return build_module_sovereignty(
            consent_state=consent_state,
            reason=reason,
            missing_providers=missing_providers,
        )
    return build_module_sovereignty(
        observed_class=observed,
        consent_state=consent_state,
        reason=reason,
        missing_providers=missing_providers,
    )


# --- Negotiation ----------------------------------------------------------------


def negotiate(
    hello: dict[str, Any],
    *,
    supported: tuple[str, ...] = STT_CAPABILITIES,
    known: frozenset[str] = KNOWN_CAPABILITIES,
    server_versions: tuple[str, ...] = DVAP_SUPPORTED_VERSIONS,
) -> tuple[list[str], str]:
    """Negotiate a session from a client `hello`.

    Returns ``(accepted_capabilities, accepted_version)``.

    - A requested capability the server serves is accepted.
    - A requested capability the server recognizes but does not currently serve
      is optional and ignored (dropped from the accepted set).
    - A requested capability outside the DVAP vocabulary is an unknown REQUIRED
      capability and raises :class:`NegotiationError`.

    Raises :class:`NegotiationError` if the message is not a well-formed `hello`.
    """

    if not isinstance(hello, dict) or hello.get("type") != "hello":
        raise NegotiationError("expected a hello message")
    if hello.get("protocol") not in (None, DVAP_PROTOCOL):
        raise NegotiationError(f"unsupported protocol: {hello.get('protocol')!r}")

    requested = hello.get("capabilities", [])
    if not isinstance(requested, list):
        raise NegotiationError("hello.capabilities must be an array")

    accepted: list[str] = []
    for capability in requested:
        if capability in supported:
            accepted.append(capability)
        elif capability in known:
            continue  # recognized but not served now → optional, ignored
        else:
            raise NegotiationError(f"unknown required capability: {capability!r}")

    return accepted, _negotiate_version(hello.get("version"), server_versions)


def _negotiate_version(client_version: Any, server_versions: tuple[str, ...]) -> str:
    if isinstance(client_version, str) and client_version in server_versions:
        return client_version
    # Fall back to the highest version this build implements; the extension is a
    # superset, so an older or unspecified client still gets a valid session.
    return server_versions[0]


def new_session_id() -> str:
    return f"session_{secrets.token_hex(8)}"


def compose_text(engine: ServiceEngine, message: dict[str, Any]) -> dict[str, Any]:
    """Compose DVAP text through the desktop's local dictation path."""
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        raise DVAPMessageError("text.compose.text is empty")
    if len(text) > MAX_COMMAND_CHARS:
        raise DVAPMessageError("text.compose.text is too large", too_large=True)
    polish = True
    if "polish" in message:
        if not isinstance(message["polish"], bool):
            raise DVAPMessageError("text.compose.polish must be a boolean")
        polish = bool(message["polish"])
    style = _optional_enum(
        message.get("style"),
        field="text.compose.style",
        values=STYLE_NAMES,
    )
    cleanup_level = _optional_enum(
        message.get("cleanup_level"),
        field="text.compose.cleanup_level",
        values=("none", "light", "medium", "high"),
    )
    app_context = _bounded_optional_text(
        message.get("app_context"),
        field="text.compose.app_context",
        max_length=MAX_APP_CONTEXT_CHARS,
    )
    try:
        result = engine.compose(
            ComposeRequest(
                text=text,
                style=style,
                polish=polish,
                cleanup_level=cleanup_level,
                app_context=app_context,
            )
        )
    except ValueError as exc:
        raise DVAPMessageError(str(exc)) from exc
    composed: dict[str, Any] = {"type": "text.composed", "text": result["text"]}
    if result.get("style"):
        composed["style"] = result["style"]
    return composed


def consent_required_sovereignty(missing: tuple[str, ...]) -> dict[str, Any]:
    """Report a live STT operation blocked before cloud egress."""

    return build_module_sovereignty(
        observed_class=SovereigntyClass.LOCAL.value,
        capability="audio.in.stream",
        capability_sovereignty=[
            {
                "capability": "audio.in.stream",
                "sovereigntyClass": SovereigntyClass.LOCAL.value,
                "reason": (
                    "Cloud STT egress blocked because explicit consent is missing or invalid."
                ),
            }
        ],
        consent_state="required",
        reason="cloud_consent_missing_or_invalid",
        missing_providers=missing,
    )


# --- Endpoint -------------------------------------------------------------------


def _offered_subprotocols(websocket: WebSocket) -> list[str]:
    """The subprotocols the client offered in ``Sec-WebSocket-Protocol``.

    Starlette parses the header into ``scope["subprotocols"]``; fall back to
    parsing the raw header so the helper is robust to either shape.
    """

    offered = websocket.scope.get("subprotocols")
    if offered:
        return list(offered)
    raw = websocket.headers.get("sec-websocket-protocol")
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _find_bearer_subprotocol(subprotocols: list[str]) -> str | None:
    """Return the first ``dcent.bearer.*`` subprotocol offered, or ``None``."""

    for proto in subprotocols:
        if proto.startswith(BEARER_SUBPROTOCOL_PREFIX):
            return proto
    return None


def _decode_bearer_token(subprotocol: str) -> bytes | None:
    """Decode the base64url (unpadded) token from a ``dcent.bearer.*`` value.

    Returns the raw token bytes, or ``None`` if the payload is empty or not valid
    base64url — a malformed credential is an auth failure, never an admit.
    """

    encoded = subprotocol[len(BEARER_SUBPROTOCOL_PREFIX) :]
    if not encoded:
        return None
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding)
    except (binascii.Error, ValueError):
        return None


def _bearer_token_matches(decoded: bytes, token: str) -> bool:
    """Constant-time compare a decoded bearer credential to the session token."""

    return secrets.compare_digest(decoded, token.encode("utf-8"))


def dvap_handshake_decision(
    websocket: WebSocket, token: str | None
) -> tuple[int | None, str | None]:
    """Decide whether to admit this DVAP connection and which subprotocol to echo.

    Returns ``(close_code, selected_subprotocol)``:

    - ``close_code`` is ``None`` to admit, else the WS close code to reject with
      (``1008`` origin, ``4401`` auth).
    - ``selected_subprotocol`` is the validated ``dcent.bearer.*`` value to echo
      on accept (RFC 6455 requires the server to pick one of the offered
      subprotocols), or ``None`` when auth came via the query fallback / no auth.

    Auth precedence: if the client offered a ``dcent.bearer.*`` subprotocol we
    validate it (a wrong or malformed one is ``4401`` — we never silently fall
    through to the query string). Only when no bearer subprotocol is offered do we
    honour the legacy ``?token=`` query parameter.

    Subprotocol selection: on admit via a bearer we echo that bearer. In every
    other case (query-token admit, or a rejection) we echo plain ``dvap.v1`` when
    the client offered it. Echoing an offered subprotocol matters even for a
    rejection: a strict WebSocket client (undici / some browsers) fails a ``101``
    that selects none of its offered subprotocols, and would never read the close
    frame that carries the real ``1008``/``4401`` — so it must be admitted to the
    open state first, then closed with the honest code.
    """

    offered = _offered_subprotocols(websocket)
    fallback: str | None = DVAP_SUBPROTOCOL if DVAP_SUBPROTOCOL in offered else None
    if fallback is None and offered:
        fallback = offered[0]

    if not origin_allowed(websocket.headers.get("origin")):
        return DVAP_CLOSE_ORIGIN, fallback
    if token is None:
        return None, fallback

    bearer = _find_bearer_subprotocol(offered)
    if bearer is not None:
        decoded = _decode_bearer_token(bearer)
        if decoded is not None and _bearer_token_matches(decoded, token):
            return None, bearer
        return DVAP_CLOSE_AUTH, fallback

    supplied = websocket.query_params.get("token") or ""
    if len(supplied) != len(token) or not secrets.compare_digest(supplied, token):
        return DVAP_CLOSE_AUTH, fallback
    return None, fallback


def add_dvap_websocket(
    app: FastAPI,
    engine: ServiceEngine,
    bus: EventBus | None = None,
    token: str | None = None,
    *,
    supported: tuple[str, ...] | None = None,
    tts_backend_factory: Callable[[], TtsBackend | None] | None = None,
    sink_factory: Callable[[], AudioSink] | None = None,
    mic_gate: MicGate | None = None,
    voice_control: VoiceRuntimeControl | None = None,
    code_policy: CodePolicy = CodePolicy.SKIP,
    max_connections: int = MAX_DVAP_CONNECTIONS,
    outbound_queue_size: int = DVAP_OUTBOUND_QUEUE_MAX,
) -> None:
    """Attach the DVAP endpoint with isolated TTS backend state per connection."""

    make_sink = sink_factory or (lambda: SoundDeviceSink())
    # Each DVAP connection owns its own PlaybackEngine, but they all affect the
    # same capture device. Reference-count the supplied policy once at endpoint
    # setup so one connection cannot restore the microphone during another
    # connection's active speech.
    shared_mic_gate = RefCountMicGate(mic_gate) if mic_gate is not None else None
    limiter = ConnectionLimiter(max_connections)
    if outbound_queue_size <= 0:
        raise ValueError("outbound_queue_size must be positive")

    async def serve_dvap(websocket: WebSocket) -> None:
        # The handshake was already accepted by the `/dvap` route (accept-then-
        # reject surfaces real close codes; see the endpoint below), so this pump
        # never calls `accept()` itself.

        # TTS backends carry mutable cancellation and inference state. Create a
        # fresh backend for each WebSocket so one client's barge-in cannot cancel
        # another client's synthesis or race its inference session.
        connection_backend = tts_backend_factory() if tts_backend_factory is not None else None
        tts_available = connection_backend is not None and connection_backend.available()
        connection_supported = supported or served_capabilities(
            tts_available=tts_available,
            voice_control_available=voice_control is not None,
        )

        # Handshake: client sends hello, server replies welcome.
        try:
            hello = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        try:
            accepted, version = negotiate(hello, supported=connection_supported)
        except NegotiationError:
            await websocket.close(code=DVAP_CLOSE_NEGOTIATION)
            return
        accepted_caps = frozenset(accepted)
        await websocket.send_json(build_welcome(new_session_id(), version, accepted))

        # A TTS player exists for the connection only when a backend is available;
        # it owns the sentence chunker, synthesis, and playback (cancellable in
        # <100 ms). Inbound tts.append/tts.cancel drive it; a local PTT/wake/VAD
        # interrupt cancels it and emits barge_in.
        player: TtsPlayer | None = None
        engine_playback: PlaybackEngine | None = None
        if (
            tts_available
            and connection_backend is not None
            and accepted_caps.intersection({"tts.append", "tts.cancel"})
        ):
            sink = make_sink()
            engine_playback = PlaybackEngine(sink, mic_gate=shared_mic_gate)
            player = TtsPlayer(connection_backend, engine_playback, code_policy=code_policy)

        # After the handshake the endpoint runs two concurrent pumps: inbound
        # messages (audio → STT, tts.* → playback) and an outbound queue carrying
        # module.sovereignty and barge_in. Whichever finishes first (usually a
        # disconnect) tears the other down.
        loop = asyncio.get_running_loop()
        outbound_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=outbound_queue_size)

        def enqueue(message: dict[str, Any]) -> None:
            # Latest-wins policy bounds observations for a slow ADE connection.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_offer_latest, outbound_queue, message)

        def on_event(event: AppEvent) -> None:
            sovereignty = module_sovereignty_for_event(event)
            if sovereignty is not None:
                enqueue(sovereignty)
            # A local activation while TTS is speaking is a barge-in: cancel
            # playback and tell ADE so it stops streaming the reply.
            if player is not None and player.is_playing:
                barge_in = barge_in_for_event(event)
                if barge_in is not None:
                    player.cancel()
                    if "barge_in" in accepted_caps:
                        enqueue(barge_in)

        unsubscribe = bus.subscribe(on_event) if bus is not None else (lambda: None)
        session = StreamingSession(engine)
        inbound_task = asyncio.create_task(
            _pump_inbound(
                websocket,
                session,
                player,
                connection_backend,
                accepted_caps,
                voice_control,
            )
        )
        outbound_task = asyncio.create_task(_pump_outbound(websocket, outbound_queue))
        try:
            await asyncio.wait({inbound_task, outbound_task}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # Starlette may cancel the endpoint concurrently with a client-side
            # close. The connection is already going away, so finish the child
            # task cleanup below rather than leaking that expected close through
            # TestClient (or the ASGI server).
            return
        finally:
            unsubscribe()
            for task in (inbound_task, outbound_task):
                task.cancel()
            if player is not None:
                player.close()
            if engine_playback is not None:
                engine_playback.close()

    @app.websocket("/dvap")
    async def dvap(websocket: WebSocket) -> None:
        code, subprotocol = dvap_handshake_decision(websocket, token)
        # Accept the WebSocket BEFORE any rejection so the close frame carries the
        # real DVAP close code. A pre-accept close is delivered to browser/undici
        # clients as an opaque 1006 (the app code — 1008 origin, 4401 auth — is
        # lost with the failed upgrade); accepting first lets ADE surface the
        # honest reason. We echo the validated bearer subprotocol per RFC 6455.
        await websocket.accept(subprotocol=subprotocol)
        if code is not None:
            await websocket.close(code=code)
            return
        if not limiter.try_acquire():
            await websocket.close(code=DVAP_CLOSE_BUSY)
            return
        try:
            await serve_dvap(websocket)
        finally:
            limiter.release()


async def _pump_inbound(
    websocket: WebSocket,
    session: StreamingSession,
    player: TtsPlayer | None,
    tts_backend: TtsBackend | None,
    accepted: frozenset[str],
    voice_control: VoiceRuntimeControl | None = None,
) -> None:
    pending_tts_chars = 0
    pending_tts_messages = 0
    active_audio: dict[str, Any] | None = None
    try:
        while True:
            frame = await websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(int(frame.get("code", 1000)))
            binary = frame.get("bytes")
            if binary is not None:
                if active_audio is None:
                    raise DVAPMessageError("binary audio requires audio.in.begin")
                active_audio["bytes"].extend(binary)
                if len(active_audio["bytes"]) > MAX_STREAMED_AUDIO_BYTES:
                    raise DVAPMessageError("streamed audio is too large", too_large=True)
                continue
            text = frame.get("text")
            if not isinstance(text, str):
                raise DVAPMessageError("message must be JSON text or PCM binary")
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DVAPMessageError("message must be valid JSON") from exc
            if not isinstance(message, dict):
                raise DVAPMessageError("message must be an object")
            kind = message.get("type")
            if not isinstance(kind, str):
                raise DVAPMessageError("message type is missing or invalid")
            if kind == "audio.in.begin":
                _require_allowed_fields(
                    message,
                    field="audio.in.begin",
                    allowed={
                        "type",
                        "requestId",
                        "sampleRate",
                        "channels",
                        "encoding",
                        "style",
                        "polish",
                        "app_context",
                    },
                )
                if "audio.in.stream" not in accepted:
                    raise DVAPMessageError("audio.in.stream was not negotiated")
                if active_audio is not None:
                    raise DVAPMessageError("an audio stream is already active")
                request_id = _bounded_request_id(message.get("requestId"))
                if (
                    message.get("sampleRate") != 16_000
                    or message.get("channels") != 1
                    or message.get("encoding") != "pcm_s16le"
                ):
                    raise DVAPMessageError("audio.in.stream requires 16 kHz mono pcm_s16le")
                style = _optional_enum(
                    message.get("style"),
                    field="audio.in.begin.style",
                    values=STYLE_NAMES,
                )
                polish_kw: bool | None = None
                if "polish" in message:
                    if not isinstance(message["polish"], bool):
                        raise DVAPMessageError("audio.in.begin.polish must be a boolean")
                    polish_kw = message["polish"]
                app_context = _bounded_optional_text(
                    message.get("app_context"),
                    field="audio.in.begin.app_context",
                    max_length=MAX_APP_CONTEXT_CHARS,
                )
                active_audio = {
                    "requestId": request_id,
                    "bytes": bytearray(),
                    "style": style,
                    "polish": polish_kw,
                    "app_context": app_context,
                }
                continue
            if kind == "audio.in.end":
                _require_allowed_fields(
                    message,
                    field="audio.in.end",
                    allowed={"type", "requestId"},
                )
                request_id = _bounded_request_id(message.get("requestId"))
                if active_audio is None or active_audio["requestId"] != request_id:
                    raise DVAPMessageError("audio.in.end does not match an active stream")
                raw = bytes(active_audio["bytes"])
                style = active_audio.get("style")
                polish = active_audio.get("polish")
                app_context = active_audio.get("app_context")
                active_audio = None
                if len(raw) % 2:
                    raise DVAPMessageError("pcm_s16le audio must contain complete samples")
                samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                stream_message = await run_bounded_worker(
                    session.engine,
                    session.push,
                    samples,
                    final=True,
                    samplerate=16_000,
                    style=style,
                    polish=polish,
                    app_context=app_context,
                )
                response = stream_message_to_dvap(stream_message)
                if response is not None and response["type"] in accepted:
                    response["requestId"] = request_id
                    await websocket.send_json(response)
                continue
            if kind == "text.compose":
                _require_allowed_fields(
                    message,
                    field="text.compose",
                    allowed={
                        "type",
                        "text",
                        "style",
                        "polish",
                        "cleanup_level",
                        "app_context",
                    },
                )
                if "text.compose" not in accepted:
                    raise DVAPMessageError("text.compose was not negotiated")
                await websocket.send_json(compose_text(session.engine, message))
                continue
            if kind == "tts.synth.begin":
                _require_allowed_fields(
                    message,
                    field="tts.synth.begin",
                    allowed={"type", "requestId", "text"},
                )
                if "tts.synth.stream" not in accepted or tts_backend is None:
                    raise DVAPMessageError("tts.synth.stream was not negotiated")
                request_id = _bounded_request_id(message.get("requestId"))
                text = message.get("text")
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or len(text) > MAX_TTS_APPEND_CHARS
                ):
                    raise DVAPMessageError("tts.synth.begin.text is empty or too large")
                sample_rate, chunks = await asyncio.get_running_loop().run_in_executor(
                    None, _synthesize_pcm, tts_backend, text
                )
                await websocket.send_json(
                    {
                        "type": "tts.audio.begin",
                        "requestId": request_id,
                        "sampleRate": sample_rate,
                        "channels": 1,
                        "encoding": "pcm_s16le",
                    }
                )
                for chunk in chunks:
                    await websocket.send_bytes(chunk)
                await websocket.send_json({"type": "tts.audio.end", "requestId": request_id})
                continue
            if kind in {"voice.mode.set", "voice.devices.get", "voice.device.set"}:
                allowed_fields = {
                    "voice.mode.set": {"type", "mode"},
                    "voice.devices.get": {"type"},
                    "voice.device.set": {"type", "kind", "deviceId"},
                }
                _require_allowed_fields(message, field=kind, allowed=allowed_fields[kind])
                if voice_control is None:
                    raise DVAPMessageError("voice control is unavailable")
                required = "voice.mode" if kind == "voice.mode.set" else "voice.devices"
                if required not in accepted:
                    raise DVAPMessageError(f"{required} was not negotiated")
                try:
                    if kind == "voice.mode.set":
                        mode = message.get("mode")
                        if not isinstance(mode, str):
                            raise VoiceControlError("voice.mode.set.mode must be a string")
                        response = voice_control.set_mode(mode)
                    elif kind == "voice.devices.get":
                        response = voice_control.devices_snapshot()
                    else:
                        device_id = message.get("deviceId")
                        if device_id is not None and not isinstance(device_id, str):
                            raise VoiceControlError(
                                "voice.device.set.deviceId must be a string or null"
                            )
                        response = voice_control.set_device(str(message.get("kind", "")), device_id)
                except VoiceControlError as exc:
                    raise DVAPMessageError(str(exc)) from exc
                await websocket.send_json(response)
                continue
            if kind in {"tts.append", "tts.cancel"}:
                allowed_fields = {
                    "tts.append": {"type", "text", "final"},
                    "tts.cancel": {"type", "reason"},
                }
                _require_allowed_fields(message, field=kind, allowed=allowed_fields[kind])
                if kind not in accepted or player is None:
                    raise DVAPMessageError(f"{kind} was not negotiated")
                if kind == "tts.append":
                    text = message.get("text", "")
                    if not isinstance(text, str):
                        raise DVAPMessageError("tts.append.text must be a string")
                    if "final" in message and not isinstance(message["final"], bool):
                        raise DVAPMessageError("tts.append.final must be a boolean")
                    pending_tts_chars += len(text)
                    pending_tts_messages += int(bool(text))
                    if (
                        pending_tts_chars > MAX_TTS_PENDING_CHARS
                        or pending_tts_messages > MAX_TTS_PENDING_MESSAGES
                    ):
                        raise DVAPMessageError("TTS pending utterance is too large", too_large=True)
                _apply_tts_message(message, player)
                if kind == "tts.cancel" or message.get("final"):
                    pending_tts_chars = 0
                    pending_tts_messages = 0
                continue
            raise DVAPMessageError("message type is missing or unsupported")
    except WebSocketDisconnect:
        return
    except (AudioValidationError, DVAPMessageError) as exc:
        too_large = getattr(exc, "too_large", False)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=DVAP_CLOSE_TOO_LARGE if too_large else DVAP_CLOSE_POLICY)
    except ServiceBusyError:
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=DVAP_CLOSE_BUSY)
    except ConsentRequired as exc:
        # Consent is re-checked by an already-built cloud provider immediately
        # before wire egress. Give ADE a schema-defined sovereignty result, then
        # close with the policy code instead of an opaque ASGI 1011.
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json(consent_required_sovereignty(exc.missing))
            await websocket.close(code=DVAP_CLOSE_POLICY)


def _bounded_request_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not all(character.isalnum() or character in "-_" for character in value)
    ):
        raise DVAPMessageError("requestId is invalid")
    return value


def _optional_enum(value: Any, *, field: str, values: tuple[str, ...]) -> str | None:
    """Validate a schema-enumerated optional DVAP string without coercion."""

    if value is None:
        return None
    if not isinstance(value, str) or value not in values:
        raise DVAPMessageError(f"{field} must be one of {', '.join(values)}")
    return value


def _bounded_optional_text(value: Any, *, field: str, max_length: int) -> str | None:
    """Validate optional schema strings without accepting empty or oversized metadata."""

    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DVAPMessageError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise DVAPMessageError(f"{field} is too large", too_large=True)
    return value


def _require_allowed_fields(message: dict[str, Any], *, field: str, allowed: set[str]) -> None:
    """Keep accepted client messages aligned with schema ``additionalProperties: false``."""

    unexpected = set(message).difference(allowed)
    if unexpected:
        raise DVAPMessageError(f"{field} contains unsupported fields")


def _synthesize_pcm(backend: TtsBackend, text: str) -> tuple[int, list[bytes]]:
    chunks: list[bytes] = []
    sample_rate = 0
    total = 0
    for chunk in backend.synthesize(text):
        if sample_rate and chunk.sample_rate != sample_rate:
            raise DVAPMessageError("TTS sample rate changed during synthesis")
        sample_rate = chunk.sample_rate
        pcm = (np.clip(chunk.samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        total += len(pcm)
        if total > MAX_SYNTH_AUDIO_BYTES:
            backend.cancel()
            raise DVAPMessageError("synthesized audio is too large", too_large=True)
        chunks.append(pcm)
    if sample_rate <= 0:
        raise DVAPMessageError("TTS produced no audio")
    return sample_rate, chunks


def _apply_tts_message(message: dict[str, Any], player: TtsPlayer) -> None:
    """Drive TTS with per-message and currently-pending memory budgets."""

    kind = message.get("type")
    if kind == "tts.append":
        text = message.get("text", "")
        if not isinstance(text, str):
            raise DVAPMessageError("tts.append.text must be a string")
        if len(text) > MAX_TTS_APPEND_CHARS:
            raise DVAPMessageError("tts.append text is too large", too_large=True)
        pending_messages, pending_chars = _tts_pending_usage(player)
        additions = int(bool(text)) + int(bool(message.get("final")))
        if (
            pending_messages + additions > MAX_TTS_PENDING_MESSAGES
            or pending_chars + len(text) > MAX_TTS_PENDING_CHARS
        ):
            raise DVAPMessageError("TTS pending queue is full", too_large=True)
        if text:
            player.append(text)
        if message.get("final"):
            player.flush()
    elif kind == "tts.cancel":
        player.cancel()


def _tts_pending_usage(player: TtsPlayer) -> tuple[int, int]:
    """Snapshot TtsPlayer's bounded ingress queue under its own lock."""

    lock = getattr(player, "_lock", None)
    pending = getattr(player, "_queue", None)
    if lock is None or pending is None:  # pragma: no cover - defensive protocol adapter
        return (0, 0)
    with lock:
        items = tuple(pending)
    return len(items), sum(len(item) for item in items if isinstance(item, str))


async def _pump_outbound(
    websocket: WebSocket, outbound_queue: asyncio.Queue[dict[str, Any]]
) -> None:
    try:
        while True:
            message = await outbound_queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        return
