# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Provide authenticated WebSocket streaming endpoints."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import secrets
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from dcent_voice.dictation.style import STYLE_NAMES
from dcent_voice.events import AppEvent, EventBus
from dcent_voice.service.api import (
    AudioValidationError,
    ConnectionLimiter,
    ServiceBusyError,
    ServiceEngine,
    run_bounded_worker,
)
from dcent_voice.service.streaming import StreamingSession

EVENT_QUEUE_MAX = 64
MAX_EVENT_CONNECTIONS = 4
MAX_STREAM_CONNECTIONS = 4
WS_CLOSE_POLICY = 1008
WS_CLOSE_TOO_LARGE = 1009
WS_CLOSE_BUSY = 1013
MAX_STREAM_JSON_BYTES = 8 * 1024 * 1024
MAX_STREAM_STYLE_CHARS = 32
MAX_STREAM_APP_CONTEXT_CHARS = 128
BEARER_SUBPROTOCOL_PREFIX = "dcent.bearer."

# The ADE desktop shell is a Tauri webview whose WebSocket handshake always
# carries an Origin header (a browser transport cannot suppress it). These are
# the only origins that legitimately reach a local DCENT module:
#   - http://127.0.0.1:1420 / http://localhost:1420 — ADE's Vite dev server
#     (tauri.conf.json devUrl).
#   - http://tauri.localhost — the packaged Windows webview scheme.
#   - tauri://localhost — the packaged macOS/Linux webview scheme.
# Any other Origin is an untrusted web page and is refused. A connection with no
# Origin at all is a native client (curl, a Python client, a test) and is
# allowed through to the token check, preserving the prior native-only contract.
ALLOWED_WEBVIEW_ORIGINS = frozenset(
    {
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "http://tauri.localhost",
        "tauri://localhost",
    }
)


class _StreamIngressError(ValueError):
    """A client-controlled ``/stream`` frame violates the wire contract."""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


@dataclass(frozen=True)
class _StreamOptions:
    final: bool
    style: str | None
    polish: bool | None
    app_context: str | None
    prose_context: bool | None


def origin_allowed(origin: str | None) -> bool:
    """Return whether a connection's ``Origin`` header is admissible.

    Absent/empty Origin ⇒ a native client (allowed). A present Origin must be one
    of :data:`ALLOWED_WEBVIEW_ORIGINS`; any other browser origin is rejected.
    """

    if not origin:
        return True
    return origin in ALLOWED_WEBVIEW_ORIGINS


def _ws_authorized(websocket: WebSocket, token: str | None) -> bool:
    return _ws_handshake(websocket, token)[0]


def _ws_handshake(websocket: WebSocket, token: str | None) -> tuple[bool, str | None]:
    """Authenticate WS clients, preferring a log-safe bearer subprotocol."""

    # A browser page opening ws://127.0.0.1:8765 always sends an Origin header;
    # native ADE clients do not. Allow the ADE webview origins and native
    # (origin-less) clients; reject any other cross-origin (browser) connection so
    # a web page can't read the voice-event stream, and require the session token
    # when one is configured.
    if not origin_allowed(websocket.headers.get("origin")):
        return False, None
    if token is None:
        return True, None
    offered = websocket.scope.get("subprotocols") or []
    if not offered:
        raw = websocket.headers.get("sec-websocket-protocol") or ""
        offered = [part.strip() for part in raw.split(",") if part.strip()]
    bearer = next(
        (protocol for protocol in offered if protocol.startswith(BEARER_SUBPROTOCOL_PREFIX)),
        None,
    )
    if bearer is not None:
        encoded = bearer[len(BEARER_SUBPROTOCOL_PREFIX) :]
        try:
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError):
            return False, None
        return secrets.compare_digest(decoded, token.encode("utf-8")), bearer
    # Query credentials remain a legacy external-client fallback. Repository
    # clients use the subprotocol so request targets and access logs stay clean.
    supplied = websocket.query_params.get("token") or ""
    if len(supplied) != len(token):
        return False, None
    return secrets.compare_digest(supplied, token), None


def add_event_websocket(
    app: FastAPI,
    bus: EventBus,
    token: str | None = None,
    *,
    max_connections: int = MAX_EVENT_CONNECTIONS,
    queue_size: int = EVENT_QUEUE_MAX,
) -> None:
    limiter = ConnectionLimiter(max_connections)
    if queue_size <= 0:
        raise ValueError("queue_size must be positive")

    @app.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        authorized, subprotocol = _ws_handshake(websocket, token)
        if not authorized:
            await websocket.close(code=WS_CLOSE_POLICY)
            return
        if not limiter.try_acquire():
            await websocket.close(code=WS_CLOSE_BUSY)
            return
        try:
            await websocket.accept(subprotocol=subprotocol)
            loop = asyncio.get_running_loop()
            events_queue: asyncio.Queue[AppEvent] = asyncio.Queue(maxsize=queue_size)

            def enqueue(ev: AppEvent) -> None:
                # Latest-wins telemetry policy: a slow observer loses its oldest
                # queued event instead of growing memory without bound.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_offer_latest, events_queue, ev)

            unsubscribe = bus.subscribe(enqueue)
            sender = asyncio.create_task(_pump_events(websocket, events_queue))
            disconnect = asyncio.create_task(_watch_disconnect(websocket))
            try:
                await asyncio.wait({sender, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                unsubscribe()
                for task in (sender, disconnect):
                    task.cancel()
                await asyncio.gather(sender, disconnect, return_exceptions=True)
        finally:
            limiter.release()


def add_stream_websocket(
    app: FastAPI,
    engine: ServiceEngine,
    token: str | None = None,
    *,
    max_connections: int = MAX_STREAM_CONNECTIONS,
) -> None:
    """Attach the authenticated ADE JSON streaming WebSocket."""

    limiter = ConnectionLimiter(max_connections)

    @app.websocket("/stream")
    async def stream(websocket: WebSocket) -> None:
        authorized, subprotocol = _ws_handshake(websocket, token)
        if not authorized:
            await websocket.close(code=WS_CLOSE_POLICY)
            return
        if not limiter.try_acquire():
            await websocket.close(code=WS_CLOSE_BUSY)
            return
        try:
            await websocket.accept(subprotocol=subprotocol)
            session = StreamingSession(engine)
            try:
                while True:
                    try:
                        message = await _receive_stream_message(websocket)
                        options = _validate_stream_options(message)
                    except _StreamIngressError as exc:
                        await websocket.close(
                            code=WS_CLOSE_TOO_LARGE if exc.too_large else WS_CLOSE_POLICY
                        )
                        return
                    try:
                        stream_message = await run_bounded_worker(
                            engine,
                            session.push,
                            message.get("audio", []),
                            final=options.final,
                            samplerate=message.get("samplerate"),
                            style=options.style,
                            polish=options.polish,
                            app_context=options.app_context,
                            prose_context=options.prose_context,
                        )
                    except AudioValidationError as exc:
                        await websocket.close(
                            code=WS_CLOSE_TOO_LARGE if exc.too_large else WS_CLOSE_POLICY
                        )
                        return
                    except ServiceBusyError:
                        await websocket.close(code=WS_CLOSE_BUSY)
                        return
                    await websocket.send_json(
                        {
                            "type": stream_message.type,
                            "text": stream_message.text,
                            "committed": stream_message.committed,
                            "partial": stream_message.partial,
                            "speech": stream_message.speech,
                            "result": stream_message.result,
                        }
                    )
            except WebSocketDisconnect:
                return
        finally:
            limiter.release()


async def _receive_stream_message(websocket: WebSocket) -> dict[str, Any]:
    """Receive one bounded text frame and decode its JSON object."""

    frame = await websocket.receive()
    if frame.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(int(frame.get("code", 1000)))
    text = frame.get("text")
    if not isinstance(text, str):
        raise _StreamIngressError("stream messages must be JSON text")
    if len(text) > MAX_STREAM_JSON_BYTES:
        raise _StreamIngressError("stream message exceeds the size limit", too_large=True)
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _StreamIngressError("stream message must be valid UTF-8") from exc
    if encoded_size > MAX_STREAM_JSON_BYTES:
        raise _StreamIngressError("stream message exceeds the size limit", too_large=True)
    try:
        message = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _StreamIngressError("stream message must be valid JSON") from exc
    if not isinstance(message, dict):
        raise _StreamIngressError("stream message must be an object")
    return message


def _validate_stream_options(message: dict[str, Any]) -> _StreamOptions:
    """Validate the metadata that ``StreamingSession.push`` accepts."""

    final = _optional_literal_bool(message, "final", default=False)
    polish = _optional_literal_bool(message, "polish")
    prose_context = _optional_literal_bool(message, "prose_context")

    style: str | None = None
    if "style" in message:
        value = message["style"]
        if not isinstance(value, str):
            raise _StreamIngressError("style must be a string")
        if len(value) > MAX_STREAM_STYLE_CHARS:
            raise _StreamIngressError("style exceeds the size limit", too_large=True)
        if value not in STYLE_NAMES:
            raise _StreamIngressError("style is not supported")
        style = value

    app_context: str | None = None
    if "app_context" in message:
        value = message["app_context"]
        if not isinstance(value, str):
            raise _StreamIngressError("app_context must be a string")
        if len(value) > MAX_STREAM_APP_CONTEXT_CHARS:
            raise _StreamIngressError("app_context exceeds the size limit", too_large=True)
        if not value:
            raise _StreamIngressError("app_context must not be empty")
        app_context = value

    return _StreamOptions(
        final=bool(final),
        style=style,
        polish=polish,
        app_context=app_context,
        prose_context=prose_context,
    )


def _optional_literal_bool(
    message: dict[str, Any], field: str, *, default: bool | None = None
) -> bool | None:
    if field not in message:
        return default
    value = message[field]
    if type(value) is not bool:
        raise _StreamIngressError(f"{field} must be a boolean")
    return value


def _offer_latest(target: asyncio.Queue, item: Any) -> None:
    """Put without blocking; when full, explicitly discard the oldest item."""

    if target.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            target.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        target.put_nowait(item)


async def _pump_events(websocket: WebSocket, events_queue: asyncio.Queue[AppEvent]) -> None:
    while True:
        ev = await events_queue.get()
        await websocket.send_json(event_to_json(ev))


async def _watch_disconnect(websocket: WebSocket) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return


def event_to_json(ev: AppEvent) -> dict[str, Any]:
    payload = asdict(ev) if is_dataclass(ev) else {}
    return {"type": type(ev).__name__, "payload": _jsonable(payload)}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
