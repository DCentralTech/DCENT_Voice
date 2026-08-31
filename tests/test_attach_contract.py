# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from dcent_voice.attach import API_VERSION, AttachError, VoiceAttachClient
from dcent_voice.attach.contract import error_payload, headless_surface
from dcent_voice.attach.registry import (
    create_registry_entry,
    verify_private_file,
    write_registry_entry,
    write_text_atomic,
)
from dcent_voice.config import load_config
from dcent_voice.engine import VoiceEngine
from dcent_voice.personalization import PersonalizationStore
from dcent_voice.service.api import ServiceEngine, create_app
from dcent_voice.service.ws import add_stream_websocket


class _FakeASR:
    locality = None

    def __init__(self, text: str = "hello world") -> None:
        self.text = text
        self.loaded = True
        self.calls = 0

    def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
        from dcent_voice.asr.base import TranscriptResult

        self.calls += 1
        return TranscriptResult(
            text=self.text,
            language="en",
            duration_s=len(np.asarray(audio).reshape(-1)) / float(samplerate),
            asr_latency_s=0.01,
        )


def _wav_bytes() -> bytes:
    return Path("tests/fixtures/audio/hello.wav").read_bytes()


def _client(fake_asr, token: str = "s3cret") -> TestClient:
    return TestClient(create_app(ServiceEngine(asr=fake_asr), token=token))


def test_capabilities_advertise_versioned_headless_surface(fake_asr) -> None:
    client = _client(fake_asr)
    denied = client.get("/capabilities")
    assert denied.status_code == 401
    body = denied.json()
    assert body["detail"] == "Unauthorized"
    assert body["error"] == {
        "code": "unauthorized",
        "message": "Unauthorized",
        "retryable": False,
    }

    ok = client.get("/capabilities", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    caps = ok.json()
    for key, value in headless_surface().items():
        assert caps[key] == value
    assert caps["api_version"] == API_VERSION
    assert "wav_b64" in caps["features"]
    assert "cancel" in caps["features"]
    assert "ready" in caps["features"]
    assert "hardware_auto" in caps["features"]
    assert caps["hardware"]["auto"] is True
    assert "active_device" in caps["hardware"]


def test_ready_does_not_require_tray_or_hotkeys(fake_asr) -> None:
    engine = ServiceEngine(
        asr=fake_asr,
        status_providers={
            "hotkeys": lambda: {
                "enabled": True,
                "ok": False,
                "status": "dead",
                "critical": True,
            }
        },
    )
    client = TestClient(create_app(engine, token="s3cret"))
    health = client.get("/health")
    assert health.json()["ok"] is False
    ready = client.get("/ready", headers={"Authorization": "Bearer s3cret"})
    assert ready.status_code == 200
    body = ready.json()
    assert body["ready"] is True
    assert body["ok"] is True
    assert body["requires_tray"] is False
    assert body["requires_hotkeys"] is False
    assert body["desktop_ok"] is False
    assert body["api_version"] == API_VERSION


def test_cancel_aborts_next_transcribe() -> None:
    asr = _FakeASR()
    client = _client(asr)
    cancelled = client.post("/cancel", headers={"Authorization": "Bearer s3cret"})
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True
    response = client.post(
        "/transcribe",
        json={"audio": [0.1] * 1600, "samplerate": 16000},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert response.status_code == 200
    assert response.json()["rejected_reason"] == "cancelled"
    assert asr.calls == 0


def test_wav_transcribe_and_structured_busy_error(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr, worker_limit=1)
    assert engine.try_acquire_worker_slot()
    client = TestClient(create_app(engine, token="s3cret"))
    try:
        busy = client.post(
            "/transcribe",
            json={"wav_b64": base64.b64encode(_wav_bytes()).decode("ascii")},
            headers={"Authorization": "Bearer s3cret"},
        )
    finally:
        engine.release_worker_slot()
    assert busy.status_code == 503
    body = busy.json()
    assert body["detail"] == "Speech service is busy; retry shortly."
    assert body["error"] == {
        "code": "busy",
        "message": "Speech service is busy; retry shortly.",
        "retryable": True,
    }

    ok = client.post(
        "/transcribe",
        json={"wav_b64": base64.b64encode(_wav_bytes()).decode("ascii"), "polish": False},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert ok.status_code == 200
    assert ok.json()["raw"] == "hello world"


def test_attach_client_discovers_registry_and_transcribes(fake_asr, tmp_path: Path) -> None:
    app = create_app(ServiceEngine(asr=fake_asr), token="s3cret")
    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.2.0b1",
        registry_dir=tmp_path,
        token="s3cret",
    )
    write_registry_entry(entry, registry_dir=tmp_path)
    http = TestClient(app)
    with VoiceAttachClient.discover(tmp_path, client=http) as client:
        caps = client.capabilities()
        assert caps["api_version"] == API_VERSION
        assert caps["requires_tray"] is False
        ready = client.ready()
        assert ready["ready"] is True
        result = client.transcribe_file(
            Path("tests/fixtures/audio/hello.wav"),
            polish=False,
        )
        assert result["raw"] == "hello world"
        client.cancel()
        aborted = client.transcribe({"audio": [0.1] * 16, "samplerate": 16000})
        assert aborted["rejected_reason"] == "cancelled"


def test_attach_client_default_ignores_hostile_proxy_and_keeps_secrets_out_of_target(
    monkeypatch,
) -> None:
    captured: list[dict[str, str]] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            captured.append(
                {
                    "target": self.path,
                    "authorization": self.headers.get("authorization", ""),
                    "body": body,
                }
            )
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"raw":"proxy"}')

        def log_message(self, _format: str, *_args: object) -> None:
            return

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("http_proxy", proxy_url)
    monkeypatch.setenv("ALL_PROXY", proxy_url)
    monkeypatch.setenv("all_proxy", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    secret = "attach-secret-must-not-leak"
    client = VoiceAttachClient("http://127.0.0.1:1", secret, timeout=0.2)
    try:
        with pytest.raises(httpx.TransportError):
            client.transcribe({"audio": [0.125], "samplerate": 16_000})
    finally:
        client.close()
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)

    assert captured == []
    assert secret not in repr(client)


def test_attach_stream_uses_bearer_subprotocol_not_query_token(fake_asr, monkeypatch) -> None:
    token = "log-safe-secret"
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine, token=token)
    add_stream_websocket(app, engine, token=token)
    http = TestClient(app)
    opened: list[tuple[str, list[str]]] = []
    original_connect = http.websocket_connect

    def recording_connect(url: str, *args, **kwargs):
        opened.append((url, list(kwargs.get("subprotocols") or [])))
        return original_connect(url, *args, **kwargs)

    monkeypatch.setattr(http, "websocket_connect", recording_connect)
    client = VoiceAttachClient("http://127.0.0.1:8765", token, client=http)

    result = client.stream([0.1] * 1600)

    assert result["type"] == "final"
    assert opened[0][0] == "/stream"
    assert "?" not in opened[0][0]
    assert token not in opened[0][0]
    assert opened[0][1][0].startswith("dcent.bearer.")

    rejected = VoiceAttachClient(
        "http://127.0.0.1:8765",
        "different-sensitive-token",
        client=http,
    )
    with pytest.raises(AttachError) as excinfo:
        rejected.stream([0.1])
    assert "different-sensitive-token" not in str(excinfo.value)
    assert "different-sensitive-token" not in repr(excinfo.value)


def test_attach_raw_websocket_handshake_keeps_bearer_out_of_request_target(
    monkeypatch,
) -> None:
    from dcent_voice.attach import client as client_module

    body = b'{"type":"final"}'
    handshake = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
    frame = bytes((0x81, len(body))) + body

    class FakeSocket:
        def __init__(self) -> None:
            self.chunks = [bytearray(handshake), bytearray(frame)]
            self.sent: list[bytes] = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def sendall(self, payload: bytes) -> None:
            self.sent.append(payload)

        def recv(self, size: int) -> bytes:
            if not self.chunks:
                return b""
            chunk = self.chunks[0]
            result = bytes(chunk[:size])
            del chunk[:size]
            if not chunk:
                self.chunks.pop(0)
            return result

    fake_socket = FakeSocket()
    monkeypatch.setattr(
        client_module.socket,
        "create_connection",
        lambda *_args, **_kwargs: fake_socket,
    )
    secret = "raw-websocket-secret"
    client = VoiceAttachClient("http://127.0.0.1:8765", secret)
    try:
        result = client.stream([0.1])
    finally:
        client.close()

    request = fake_socket.sent[0].decode("ascii")
    assert result == {"type": "final"}
    assert request.startswith("GET /stream HTTP/1.1\r\n")
    assert "GET /stream?" not in request
    assert secret not in request
    assert "Sec-WebSocket-Protocol: dcent.bearer." in request


def test_attach_discovery_verifies_registry_and_token_owner_only(
    tmp_path: Path, monkeypatch
) -> None:
    from dcent_voice.attach import client as client_module

    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.2.0b1",
        registry_dir=tmp_path,
        token="s3cret",
    )
    write_registry_entry(entry, registry_dir=tmp_path)
    verified: list[Path] = []

    def verifier(path: Path) -> None:
        verify_private_file(path)
        verified.append(path)

    monkeypatch.setattr(client_module, "verify_private_file", verifier)

    with VoiceAttachClient.discover(
        tmp_path,
        client=TestClient(create_app(ServiceEngine(asr=_FakeASR()))),
    ):
        pass

    assert verified == [tmp_path / "dcent-voice.json", tmp_path / "dcent-voice.token"]


@pytest.mark.parametrize(
    "mutation",
    ["module", "endpoint", "outside_token", "registry_symlink", "token_symlink"],
)
def test_attach_discovery_rejects_untrusted_registry_targets(
    tmp_path: Path, mutation: str, monkeypatch
) -> None:
    from dcent_voice.attach import client as client_module

    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.2.0b1",
        registry_dir=tmp_path,
        token="s3cret",
    )
    write_registry_entry(entry, registry_dir=tmp_path)
    registry_path = tmp_path / "dcent-voice.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if mutation == "module":
        payload["moduleId"] = "dcent-ade"
    elif mutation == "endpoint":
        payload["endpoint"] = "https://attacker.example/collect"
    elif mutation == "outside_token":
        outside = tmp_path.parent / f"{tmp_path.name}-outside.token"
        write_text_atomic(outside, "outside-secret")
        payload["tokenRef"] = str(outside)
    elif mutation in {"registry_symlink", "token_symlink"}:
        token_path = tmp_path / "dcent-voice.token"
        original_is_symlink = Path.is_symlink
        simulated_symlink = registry_path if mutation == "registry_symlink" else token_path

        def is_symlink(path: Path) -> bool:
            return path == simulated_symlink or original_is_symlink(path)

        monkeypatch.setattr(client_module.Path, "is_symlink", is_symlink)
    write_text_atomic(registry_path, json.dumps(payload))

    with pytest.raises(AttachError) as excinfo:
        VoiceAttachClient.discover(tmp_path)

    assert excinfo.value.code in {"refused", "unauthorized"}


def test_attach_client_maps_401_and_refuses_non_loopback(fake_asr) -> None:
    app = create_app(ServiceEngine(asr=fake_asr), token="s3cret")
    client = VoiceAttachClient("http://127.0.0.1:8765", "wrong", client=TestClient(app))
    with pytest.raises(AttachError) as excinfo:
        client.capabilities()
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.retryable is False
    with pytest.raises(AttachError) as refused:
        VoiceAttachClient("http://example.com:8765", "s3cret")
    assert refused.value.code == "refused"


def test_voice_engine_stream_and_cancel_are_headless(tmp_path: Path) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    engine = VoiceEngine(
        config,
        asr=_FakeASR(),
        personalization=PersonalizationStore(tmp_path / "p.json"),
    )
    caps = engine.capabilities()
    assert caps["api_version"] == API_VERSION
    assert caps["requires_tray"] is False
    assert caps["requires_hotkeys"] is False
    assert caps["hardware"]["auto"] is True
    speech = np.full(1600, 0.2, dtype=np.float32)
    events = list(engine.transcribe_stream([speech, speech]))
    assert events[-1]["type"] == "final"
    assert "hello" in events[-1]["text"].lower()
    engine.cancel()
    cancelled = engine.open_stream().push(speech, final=True)
    assert cancelled.type == "cancelled"
    assert engine.ready()["requires_hotkeys"] is False


def test_error_payload_keeps_detail() -> None:
    body = error_payload(401, "Unauthorized")
    assert body["detail"] == "Unauthorized"
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["retryable"] is False
