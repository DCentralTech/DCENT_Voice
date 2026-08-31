# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Loopback HTTP client for the versioned ADE attach contract."""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from dcent_voice.attach.contract import LOOPBACK_HOSTS
from dcent_voice.attach.registry import (
    default_registry_dir,
    read_registry_entry,
    verify_private_file,
)

ATTACH_MODULE_ID = "dcent-voice"
BEARER_SUBPROTOCOL_PREFIX = "dcent.bearer."
MAX_ATTACH_TOKEN_BYTES = 512


class AttachError(RuntimeError):
    """Structured attach failure. ``code`` is stable; ``message`` is human."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)

    @classmethod
    def from_response(cls, response: httpx.Response) -> AttachError:
        try:
            body = response.json()
        except Exception:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("code"):
            return cls(
                str(error["code"]),
                str(error.get("message") or response.text or response.reason_phrase),
                retryable=bool(error.get("retryable")),
            )
        detail = body.get("detail") if isinstance(body, dict) else None
        message = detail if isinstance(detail, str) else (response.text or response.reason_phrase)
        return cls("http_error", str(message), retryable=response.status_code == 503)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def assert_loopback_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise AttachError("refused", "Attach endpoint has an invalid port.") from exc
    if (
        parsed.scheme != "http"
        or host not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AttachError(
            "refused",
            "Attach client only talks to loopback (127.0.0.1 / localhost).",
            retryable=False,
        )


class VoiceAttachClient:
    """Discover a running DCENT Voice module and transcribe or compose without the tray."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        assert_loopback_endpoint(endpoint)
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.endpoint,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    @classmethod
    def discover(
        cls,
        registry_dir: Path | str | None = None,
        **kwargs: Any,
    ) -> VoiceAttachClient:
        root = (
            Path(registry_dir) if registry_dir is not None else default_registry_dir()
        ).resolve()
        path = root / "dcent-voice.json"
        if not path.is_file():
            raise AttachError(
                "not_running",
                "DCENT Voice is not running (no dcent-voice.json registry entry).",
                retryable=False,
            )
        try:
            if path.is_symlink():
                raise PermissionError("registry entry must not be a symlink")
            verify_private_file(path)
            entry = read_registry_entry(path)
            if entry.moduleId != ATTACH_MODULE_ID:
                raise ValueError("registry module identity did not match")
            assert_loopback_endpoint(entry.endpoint)
            token = _read_registry_token(root, entry.tokenRef)
        except AttachError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AttachError(
                "unauthorized",
                "Attach registry or token failed local security validation.",
                retryable=False,
            ) from exc
        return cls(entry.endpoint, token, **kwargs)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> VoiceAttachClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def capabilities(self) -> dict[str, Any]:
        return self._json("GET", "/capabilities")

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/health")

    def ready(self) -> dict[str, Any]:
        return self._json("GET", "/ready")

    def cancel(self) -> dict[str, Any]:
        return self._json("POST", "/cancel")

    def transcribe(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit JSON audio to one-shot ADE transcription."""
        return self._json("POST", "/transcribe", json=payload)

    def transcribe_file(
        self,
        path: Path | str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Read a WAV and submit it for ADE transcription."""
        wav_b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        payload = {"wav_b64": wav_b64, **kwargs}
        return self.transcribe(payload)

    def stream(self, audio: Any, **kwargs: Any) -> dict[str, Any]:
        """Submit audio to the ADE streaming WebSocket."""
        payload: dict[str, Any] = {
            "audio": [float(sample) for sample in audio],
            "samplerate": int(kwargs.pop("samplerate", 16000)),
            "final": bool(kwargs.pop("final", True)),
            **kwargs,
        }
        path = "/stream"
        subprotocol = _bearer_subprotocol(self.token)
        connect = getattr(self._client, "websocket_connect", None)
        if callable(connect):
            try:
                with connect(path, subprotocols=[subprotocol]) as websocket:
                    websocket.send_json(payload)
                    body = websocket.receive_json()
            except Exception:
                raise AttachError("http_error", "Attach stream request failed.") from None
            if not isinstance(body, dict):
                raise AttachError(
                    "invalid_response",
                    "Attach stream response was not a JSON object.",
                )
            return body
        return _loopback_ws_json(
            self.endpoint,
            path,
            payload,
            timeout=self.timeout,
            subprotocol=subprotocol,
        )

    def stream_session(self, frames: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Send sequential WS /stream frames on one connection."""
        samplerate = int(kwargs.pop("samplerate", 16000))
        frame_list = [frame for frame in frames]
        if not frame_list:
            raise AttachError("invalid_request", "Attach stream session had no frames.")
        path = "/stream"
        subprotocol = _bearer_subprotocol(self.token)
        payloads: list[dict[str, Any]] = []
        last = len(frame_list) - 1
        for index, frame in enumerate(frame_list):
            payload: dict[str, Any] = {
                "audio": [float(sample) for sample in frame],
                "samplerate": samplerate,
                "final": index == last,
                **kwargs,
            }
            payloads.append(payload)
        connect = getattr(self._client, "websocket_connect", None)
        if callable(connect):
            events: list[dict[str, Any]] = []
            try:
                with connect(path, subprotocols=[subprotocol]) as websocket:
                    for payload in payloads:
                        websocket.send_json(payload)
                        body = websocket.receive_json()
                        if not isinstance(body, dict):
                            raise AttachError(
                                "invalid_response",
                                "Attach stream response was not a JSON object.",
                            )
                        events.append(body)
            except AttachError:
                raise
            except Exception:
                raise AttachError("http_error", "Attach stream request failed.") from None
            return events
        return _loopback_ws_session(
            self.endpoint,
            path,
            payloads,
            timeout=self.timeout,
            subprotocol=subprotocol,
        )

    def compose(self, text: str, **kwargs: Any) -> dict[str, Any]:
        """Compose text through the ADE service."""
        payload = {"text": text, **kwargs}
        return self._json("POST", "/compose", json=payload)

    def learn(self, spoken: str, written: str, **kwargs: Any) -> dict[str, Any]:
        """Store a typed correction without sending audio."""
        payload = {"spoken": spoken, "written": written, **kwargs}
        return self._json("POST", "/learn", json=payload)

    def personalization(self) -> dict[str, Any]:
        """Return the ADE personalization snapshot."""
        return self._json("GET", "/personalization")

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self.token}"
        response = self._client.request(
            method,
            path,
            headers=headers,
            follow_redirects=False,
            **kwargs,
        )
        if response.status_code >= 400:
            raise AttachError.from_response(response)
        body = response.json()
        if not isinstance(body, dict):
            raise AttachError("invalid_response", "Attach response was not a JSON object.")
        return body


def _recvexact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise AttachError("invalid_response", "Attach stream closed.")
        buf.extend(chunk)
    return bytes(buf)


def _recv_http_headers(sock: socket.socket) -> bytes:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AttachError("http_error", "Attach stream WebSocket upgrade failed.")
        buf.extend(chunk)
        if len(buf) > 16_384:
            raise AttachError("http_error", "Attach stream WebSocket upgrade failed.")
    return bytes(buf)


def _ws_send_text(sock: socket.socket, text: str) -> None:
    data = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _ws_recv_text(sock: socket.socket) -> str:
    first, second = _recvexact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recvexact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recvexact(sock, 8))[0]
    mask = _recvexact(sock, 4) if masked else b""
    payload = _recvexact(sock, length)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    if opcode == 0x8:
        raise AttachError("http_error", "Attach stream closed.")
    if opcode != 0x1:
        raise AttachError("invalid_response", "Attach stream sent a non-text frame.")
    return payload.decode("utf-8")


def _loopback_ws_json(
    endpoint: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    subprotocol: str,
) -> dict[str, Any]:
    bodies = _loopback_ws_session(
        endpoint,
        path,
        [payload],
        timeout=timeout,
        subprotocol=subprotocol,
    )
    return bodies[0]


def _loopback_ws_session(
    endpoint: str,
    path: str,
    payloads: list[dict[str, Any]],
    *,
    timeout: float,
    subprotocol: str,
) -> list[dict[str, Any]]:
    if not payloads:
        raise AttachError("invalid_request", "Attach stream session had no frames.")
    parsed = urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    host_header = f"[{host}]" if ":" in host else host
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Protocol: {subprotocol}\r\n"
        "\r\n"
    )
    bodies: list[dict[str, Any]] = []
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request.encode("ascii"))
        header = _recv_http_headers(sock)
        status = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise AttachError("http_error", "Attach stream WebSocket upgrade failed.")
        leftover = header.split(b"\r\n\r\n", 1)[1]
        if leftover:
            raise AttachError("invalid_response", "Attach stream sent early data.")
        for payload in payloads:
            _ws_send_text(sock, json.dumps(payload))
            body = json.loads(_ws_recv_text(sock))
            if not isinstance(body, dict):
                raise AttachError(
                    "invalid_response",
                    "Attach stream response was not a JSON object.",
                )
            bodies.append(body)
    return bodies


def _bearer_subprotocol(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{BEARER_SUBPROTOCOL_PREFIX}{encoded}"


def _read_registry_token(registry_root: Path, token_ref: str) -> str:
    token_path = Path(token_ref)
    if not token_path.is_absolute():
        token_path = registry_root / token_path
    if token_path.is_symlink():
        raise PermissionError("attach token must not be a symlink")
    resolved = token_path.resolve(strict=True)
    resolved.relative_to(registry_root)
    if not resolved.is_file() or resolved.stat().st_size > MAX_ATTACH_TOKEN_BYTES:
        raise PermissionError("attach token file is invalid")
    verify_private_file(resolved)
    token = resolved.read_text(encoding="ascii").strip()
    encoded = token.encode("ascii")
    if (
        not token
        or len(encoded) > MAX_ATTACH_TOKEN_BYTES
        or any(character.isspace() or not character.isprintable() for character in token)
    ):
        raise PermissionError("attach token is invalid")
    return token
