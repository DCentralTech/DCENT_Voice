# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from starlette.websockets import WebSocketDisconnect

from dcent_voice.asr.base import Locality, TranscriptResult
from dcent_voice.attach.registry import create_registry_entry
from dcent_voice.audio.capture import AudioCapture
from dcent_voice.events import AppMode, EventBus, HotkeyPressed, PrivacyChanged, WakeWordDetected
from dcent_voice.privacy import ConsentLedger, EgressLog, PrivacyMonitor, ProviderPrivacy
from dcent_voice.service.api import ServiceEngine, create_app
from dcent_voice.service.dvap import (
    BEARER_SUBPROTOCOL_PREFIX,
    DVAP_CLOSE_AUTH,
    DVAP_CLOSE_BUSY,
    DVAP_CLOSE_NEGOTIATION,
    DVAP_CLOSE_ORIGIN,
    DVAP_CLOSE_POLICY,
    DVAP_CLOSE_TOO_LARGE,
    MAX_TTS_APPEND_CHARS,
    MAX_TTS_PENDING_MESSAGES,
    DVAPMessageError,
    NegotiationError,
    _apply_tts_message,
    add_dvap_websocket,
    barge_in_for_event,
    build_barge_in,
    build_hello,
    consent_required_sovereignty,
    model_download_sovereignty,
    module_sovereignty_for_event,
    negotiate,
    stream_message_to_dvap,
)
from dcent_voice.service.streaming import StreamMessage
from dcent_voice.service.voice_control import VoiceRuntimeControl
from dcent_voice.sovereignty import advertised_capabilities, served_capabilities
from dcent_voice.tts import FakeAudioSink, FakeTtsBackend

DVAP_SCHEMAS = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "dvap"


# --- Schema fixtures ------------------------------------------------------------


def _load(name: str) -> dict:
    return json.loads((DVAP_SCHEMAS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def message_validator() -> Draft202012Validator:
    return Draft202012Validator(_load("message.schema.json"))


@pytest.fixture(scope="module")
def registry_validator() -> Draft202012Validator:
    return Draft202012Validator(_load("registry-entry.schema.json"))


@pytest.fixture(scope="module")
def vectors() -> dict:
    return _load("test-vectors.json")


# --- Handshake ------------------------------------------------------------------


def _dvap_app(
    fake_asr,
    *,
    token: str | None = None,
    bus: EventBus | None = None,
    **dvap_options,
):
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine, token=token)
    add_dvap_websocket(app, engine, bus, token=token, **dvap_options)
    return app


def test_negotiation_happy_path(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr))

    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello())
        welcome = websocket.receive_json()

    assert welcome["type"] == "welcome"
    assert welcome["sessionId"].startswith("session_")
    assert welcome["acceptedVersion"] == "1.2"
    # STT and text compose are served; the model-download capability is
    # recognized but optional and is dropped from the accepted session
    # capabilities.
    assert welcome["capabilities"] == [
        "stt.partial",
        "stt.final",
        "audio.in.stream",
        "text.compose",
    ]


def test_unknown_required_capability_fails_negotiation(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr))
    hello = build_hello(capabilities=("stt.partial", "telepathy.stream"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.send_json(hello)
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_NEGOTIATION


def test_negotiate_ignores_known_optional_capability() -> None:
    accepted, version = negotiate(build_hello())

    assert accepted == ["stt.partial", "stt.final", "audio.in.stream"]
    assert version == "1.2"


def test_negotiate_raises_on_non_hello() -> None:
    with pytest.raises(NegotiationError):
        negotiate({"type": "welcome"})


# --- Auth / close codes ---------------------------------------------------------


def test_bad_token_closes_4401(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap?token=wrong") as websocket,
    ):
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_AUTH


def test_missing_token_closes_4401(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_AUTH


def test_good_token_admits_and_negotiates(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with client.websocket_connect("/dvap?token=s3cret") as websocket:
        websocket.send_json(build_hello())
        welcome = websocket.receive_json()

    assert welcome["type"] == "welcome"


def test_browser_origin_closes_1008(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/dvap?token=s3cret", headers={"origin": "https://evil.example"}
        ) as websocket,
    ):
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_ORIGIN


# --- Origin allowlist (Wave 12.V) ----------------------------------------------


def _bearer_subprotocol(token: str) -> str:
    """Encode a token the way ADE's browser transport does: base64url, no pad."""

    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{BEARER_SUBPROTOCOL_PREFIX}{encoded}"


def test_allowed_webview_origin_admits(fake_asr) -> None:
    # ADE's Tauri webview always sends an Origin header; the dev-server origin is
    # on the allowlist, so the handshake must complete rather than be rejected.
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with client.websocket_connect(
        "/dvap",
        subprotocols=["dvap.v1", _bearer_subprotocol("s3cret")],
        headers={"origin": "http://127.0.0.1:1420"},
    ) as websocket:
        websocket.send_json(build_hello())
        welcome = websocket.receive_json()

    assert welcome["type"] == "welcome"


def test_packaged_tauri_origin_admits(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with client.websocket_connect(
        "/dvap",
        subprotocols=["dvap.v1", _bearer_subprotocol("s3cret")],
        headers={"origin": "tauri://localhost"},
    ) as websocket:
        websocket.send_json(build_hello())
        assert websocket.receive_json()["type"] == "welcome"


def test_absent_origin_admits_native_client(fake_asr) -> None:
    # No Origin header ⇒ a native client (not a browser); it stays allowed.
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with client.websocket_connect(
        "/dvap", subprotocols=["dvap.v1", _bearer_subprotocol("s3cret")]
    ) as websocket:
        websocket.send_json(build_hello())
        assert websocket.receive_json()["type"] == "welcome"


def test_non_allowlisted_browser_origin_closes_1008(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/dvap",
            subprotocols=["dvap.v1", _bearer_subprotocol("s3cret")],
            headers={"origin": "http://localhost:3000"},
        ) as websocket,
    ):
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_ORIGIN


# --- Subprotocol bearer token (Wave 12.V) --------------------------------------


def test_subprotocol_bearer_token_admits_and_is_echoed(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))
    bearer = _bearer_subprotocol("s3cret")

    with client.websocket_connect("/dvap", subprotocols=["dvap.v1", bearer]) as websocket:
        # Per RFC 6455 the server selects one offered subprotocol; DVAP echoes the
        # validated bearer so ADE's negotiated socket.protocol reflects it.
        assert websocket.accepted_subprotocol == bearer
        websocket.send_json(build_hello())
        assert websocket.receive_json()["type"] == "welcome"


def test_subprotocol_wrong_token_closes_4401(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/dvap", subprotocols=["dvap.v1", _bearer_subprotocol("wrong-token")]
        ) as websocket,
    ):
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_AUTH


def test_subprotocol_malformed_base64_closes_4401(fake_asr) -> None:
    # "abcde" is 5 base64 chars — an impossible group length — so decoding raises
    # and the credential is treated as a failed auth, never a silent admit.
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/dvap", subprotocols=["dvap.v1", f"{BEARER_SUBPROTOCOL_PREFIX}abcde"]
        ) as websocket,
    ):
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_AUTH


def test_subprotocol_bearer_preferred_over_query_fallback(fake_asr) -> None:
    # A present (but wrong) bearer subprotocol must not fall through to a correct
    # ?token= — the offered credential channel is authoritative.
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/dvap?token=s3cret", subprotocols=["dvap.v1", _bearer_subprotocol("wrong-token")]
        ) as websocket,
    ):
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_AUTH


def test_rejection_echoes_offered_subprotocol_so_close_code_reaches_client(fake_asr) -> None:
    # A strict WS client (undici / some browsers) fails a 101 that selects none of
    # its offered subprotocols, and would never read the close frame. So even a
    # rejection echoes plain dvap.v1: the socket opens, then closes with the honest
    # 4401. This asserts the echo; the coded close itself is proven live on-wire.
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with client.websocket_connect(
        "/dvap", subprotocols=["dvap.v1", _bearer_subprotocol("wrong-token")]
    ) as websocket:
        assert websocket.accepted_subprotocol == "dvap.v1"
        with pytest.raises(WebSocketDisconnect) as excinfo:
            websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_AUTH


def test_query_token_still_admits_without_subprotocol(fake_asr) -> None:
    # Legacy fallback: a native client with no bearer subprotocol may still auth
    # via ?token=.
    client = TestClient(_dvap_app(fake_asr, token="s3cret"))

    with client.websocket_connect("/dvap?token=s3cret") as websocket:
        assert websocket.accepted_subprotocol is None
        websocket.send_json(build_hello())
        assert websocket.receive_json()["type"] == "welcome"


# --- STT bridge -----------------------------------------------------------------


def _send_pcm_utterance(websocket, request_id: str, *, sample_count: int = 1600) -> None:
    websocket.send_json(
        {
            "type": "audio.in.begin",
            "requestId": request_id,
            "sampleRate": 16_000,
            "channels": 1,
            "encoding": "pcm_s16le",
        }
    )
    websocket.send_bytes((np.ones(sample_count, dtype="<i2") * 1000).tobytes())
    websocket.send_json({"type": "audio.in.end", "requestId": request_id})


def test_normative_pcm_stream_bridges_final(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr))

    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello())
        websocket.receive_json()  # welcome

        _send_pcm_utterance(websocket, "utterance_1", sample_count=16_000)
        final = websocket.receive_json()

    assert final == {
        "type": "stt.final",
        "text": "Hello world.",
        "requestId": "utterance_1",
    }


@pytest.mark.parametrize("ledger_change", ["revoke", "corrupt"])
def test_negotiated_dvap_session_reports_live_cloud_consent_loss_then_policy_closes(
    tmp_path, message_validator, ledger_change
) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    egress = EgressLog(tmp_path / "egress.jsonl")
    monitor = PrivacyMonitor(
        (
            ProviderPrivacy(
                key="asr:openai",
                role="asr",
                provider="openai",
                locality=Locality.CLOUD,
                payload_type="audio",
            ),
        ),
        ledger=ledger,
        egress_log=egress,
    )

    class ConsentCheckingASR:
        def transcribe(self, audio, samplerate=16000, initial_prompt=None):
            del initial_prompt
            monitor.record_egress(
                "asr:openai",
                payload_type="audio",
                byte_count=len(audio) * 4,
            )
            return TranscriptResult(
                text="consented cloud result",
                language="en",
                duration_s=len(audio) / samplerate,
                asr_latency_s=0.001,
            )

    ledger.grant("asr:openai", payload_type="audio")
    engine = ServiceEngine(asr=ConsentCheckingASR(), privacy=monitor)
    app = create_app(engine)
    add_dvap_websocket(app, engine)
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.send_json(build_hello())
        websocket.receive_json()  # welcome
        _send_pcm_utterance(websocket, "before_revoke")
        assert websocket.receive_json()["type"] == "stt.final"

        if ledger_change == "revoke":
            ledger.revoke("asr:openai")
        else:
            ledger.path.write_text("{invalid", encoding="utf-8")

        _send_pcm_utterance(websocket, "after_revoke")
        blocked = websocket.receive_json()
        message_validator.validate(blocked)
        assert blocked["type"] == "module.sovereignty"
        assert blocked["observedClass"] == "LOCAL"
        assert blocked["consentState"] == "required"
        assert blocked["reason"] == "cloud_consent_missing_or_invalid"
        assert blocked["missingProviders"] == ["asr:openai"]
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_POLICY
    assert len(egress.tail()) == 1


def test_audio_rejected_when_stt_family_was_not_negotiated(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        # barge_in is known but unavailable without a TTS backend, leaving this
        # session with no accepted STT family.
        websocket.send_json(build_hello(capabilities=("barge_in",)))
        assert websocket.receive_json()["capabilities"] == []
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "not_negotiated",
                "sampleRate": 16_000,
                "channels": 1,
                "encoding": "pcm_s16le",
            }
        )
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_POLICY


def test_dvap_rejects_oversized_audio_chunk(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.send_json(build_hello())
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "too_large",
                "sampleRate": 16_000,
                "channels": 1,
                "encoding": "pcm_s16le",
            }
        )
        websocket.send_bytes(b"\x00" * (16_000 * 2 * 60 + 1))
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_TOO_LARGE


@pytest.mark.parametrize(
    ("message", "expected_code"),
    (
        ({"audio": [0.1], "samplerate": 16_000, "final": "yes"}, DVAP_CLOSE_POLICY),
        ({"type": []}, DVAP_CLOSE_POLICY),
        (
            {
                "type": "audio.in.begin",
                "requestId": "extra_metadata",
                "sampleRate": 16_000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "metadata": {"unbounded": True},
            },
            DVAP_CLOSE_POLICY,
        ),
        (
            {
                "type": "audio.in.begin",
                "requestId": "large_context",
                "sampleRate": 16_000,
                "channels": 1,
                "encoding": "pcm_s16le",
                "app_context": "a" * 129,
            },
            DVAP_CLOSE_TOO_LARGE,
        ),
    ),
)
def test_schema_invalid_audio_frames_close_deterministically(
    fake_asr, message_validator, message, expected_code
) -> None:
    assert not message_validator.is_valid(message)
    client = TestClient(_dvap_app(fake_asr))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.send_json(build_hello())
        websocket.receive_json()
        websocket.send_json(message)
        websocket.receive_json()

    assert excinfo.value.code == expected_code


def test_dvap_enforces_connection_cap(fake_asr) -> None:
    client = TestClient(_dvap_app(fake_asr, max_connections=1))

    with client.websocket_connect("/dvap") as first:
        first.send_json(build_hello())
        first.receive_json()
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/dvap") as second,
        ):
            second.receive_json()
        assert excinfo.value.code == DVAP_CLOSE_BUSY


def test_stream_message_to_dvap_maps_types() -> None:
    silence = StreamMessage(type="silence", text="", committed="", partial="", speech=False)
    stable = StreamMessage(
        type="partial", text="run the", committed="run the", partial="run the", speech=True
    )
    volatile = StreamMessage(
        type="partial", text="run", committed="", partial="run the", speech=True
    )

    assert stream_message_to_dvap(silence) is None
    assert stream_message_to_dvap(stable) == {
        "type": "stt.partial",
        "text": "run the",
        "stable": True,
    }
    assert stream_message_to_dvap(volatile)["stable"] is False


# --- Sovereignty ----------------------------------------------------------------


def test_module_sovereignty_emitted_on_consent_change(
    fake_asr, tmp_path, message_validator
) -> None:
    from dcent_voice.privacy import ConsentLedger

    bus = EventBus()
    bus.start()
    client = TestClient(_dvap_app(fake_asr, bus=bus))
    ledger = ConsentLedger(tmp_path / "consent.json")

    try:
        with client.websocket_connect("/dvap") as websocket:
            websocket.send_json(build_hello())
            websocket.receive_json()  # welcome

            # A consent grant flips the observed data-flow class; the app signals
            # that with PrivacyChanged, which /dvap turns into module.sovereignty.
            ledger.grant("asr:openai", payload_type="audio")
            bus.publish(
                PrivacyChanged(
                    "cloud",
                    consent_state="granted",
                    reason="user_granted_cloud_consent",
                )
            )

            message = websocket.receive_json()
    finally:
        bus.stop()

    assert message["type"] == "module.sovereignty"
    assert message["sovereigntyClass"] == "LOCAL"
    assert message["observedClass"] == "CLOUD"
    assert message["consentState"] == "granted"
    assert message["reason"] == "user_granted_cloud_consent"

    revoked = module_sovereignty_for_event(
        PrivacyChanged(
            "cloud",
            consent_state="revoked",
            reason="user_revoked_cloud_consent",
            missing_providers=("asr:openai",),
        )
    )
    message_validator.validate(revoked)
    assert revoked == {
        "type": "module.sovereignty",
        "sovereigntyClass": "LOCAL",
        "consentState": "revoked",
        "reason": "user_revoked_cloud_consent",
        "missingProviders": ["asr:openai"],
    }


def test_model_download_sovereignty_matches_vector(vectors, message_validator) -> None:
    message = model_download_sovereignty()
    message_validator.validate(message)
    assert message["capability"] == "voice.model.download"
    assert message["observedClass"] == "SERVER_EGRESS"

    blocked = consent_required_sovereignty(("asr:openai",))
    message_validator.validate(blocked)
    assert blocked["consentState"] == "required"


# --- Schema conformance + vectors ----------------------------------------------


def test_emitted_messages_validate_against_schema(fake_asr, message_validator) -> None:
    emitted = [
        build_hello(),
        {"type": "welcome", "sessionId": "s", "acceptedVersion": "1.1", "capabilities": []},
        stream_message_to_dvap(
            StreamMessage(type="partial", text="hi", committed="", partial="hi", speech=True)
        ),
        stream_message_to_dvap(
            StreamMessage(
                type="final",
                text="hello world",
                committed="hello world",
                partial="hello world",
                speech=True,
            )
        ),
        model_download_sovereignty(),
    ]
    for message in emitted:
        message_validator.validate(message)


def test_shared_vectors_pass_schema(vectors, message_validator, registry_validator) -> None:
    for entry in vectors["registryEntries"]:
        registry_validator.validate(entry)
    for message in vectors["messages"]:
        message_validator.validate(message)


def test_invalid_shared_vectors_fail_schema(vectors, message_validator) -> None:
    for vector in vectors["invalidMessages"]:
        assert not message_validator.is_valid(vector["message"]), vector


@pytest.mark.parametrize("vector_index", (0, 1, 2))
def test_invalid_shared_vectors_match_runtime_policy_close(fake_asr, vectors, vector_index) -> None:
    vector = vectors["invalidMessages"][vector_index]
    assert vector["runtime"] == "policy-close"
    client = TestClient(_dvap_app(fake_asr))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.send_json(build_hello())
        websocket.receive_json()
        websocket.send_json(vector["message"])
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_POLICY


def test_registry_entry_validates_against_schema(registry_validator, tmp_path) -> None:
    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        token="tok",
    )
    registry_validator.validate(entry.to_json())


def test_lan_registry_entry_validates_against_schema(registry_validator, tmp_path) -> None:
    entry = create_registry_entry(
        endpoint="http://0.0.0.0:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        token="tok",
    )
    assert entry.sovereigntyClass == "LAN"
    registry_validator.validate(entry.to_json())


# --- Vector-file drift guard ----------------------------------------------------

_VECTOR_FILES = ("message.schema.json", "registry-entry.schema.json", "test-vectors.json")


def _ade_dvap_dir() -> Path:
    override = os.environ.get("DCENT_ADE_REPO")
    if override:
        return Path(override) / "docs" / "schemas" / "dvap"
    # Default: the sibling ADE checkout next to this repo.
    return Path(__file__).resolve().parents[2] / "DCENT_ADE" / "docs" / "schemas" / "dvap"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", _VECTOR_FILES)
def test_vector_fixtures_match_ade_source(name: str) -> None:
    ade_dir = _ade_dvap_dir()
    ade_file = ade_dir / name
    if not ade_file.exists():
        pytest.skip(
            f"ADE DVAP schema not found at {ade_file}. Set DCENT_ADE_REPO to the "
            "ADE repo root to run the DVAP vector drift check."
        )
    fixture = DVAP_SCHEMAS / name
    assert _sha256(fixture) == _sha256(ade_file), (
        f"{name} drifted from the ADE source. Re-copy "
        f"{ade_file} into {fixture} (the two repos must share byte-identical DVAP "
        "schemas/vectors)."
    )


# --- Wave E1-2: DVAP TTS family -------------------------------------------------

_TTS_HELLO_CAPS = (
    "stt.partial",
    "stt.final",
    "audio.in.stream",
    "text.compose",
    "tts.append",
    "tts.cancel",
    "barge_in",
    "tts.synth.stream",
)


def _dvap_tts_app(
    fake_asr,
    *,
    backend: FakeTtsBackend | None = None,
    sinks: list | None = None,
    bus: EventBus | None = None,
    token: str | None = None,
    **dvap_options,
):
    """A /dvap app with a fake TTS backend and a captured fake playback sink."""
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine, token=token)

    def sink_factory() -> FakeAudioSink:
        sink = FakeAudioSink()
        if sinks is not None:
            sinks.append(sink)
        return sink

    add_dvap_websocket(
        app,
        engine,
        bus,
        token=token,
        tts_backend_factory=lambda: backend or FakeTtsBackend(),
        sink_factory=sink_factory,
        **dvap_options,
    )
    return app


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# Capability advertisement ------------------------------------------------------


def test_capability_helpers_gate_on_availability() -> None:
    assert served_capabilities(tts_available=False) == (
        "stt.partial",
        "stt.final",
        "audio.in.stream",
        "text.compose",
    )
    assert served_capabilities(tts_available=True) == _TTS_HELLO_CAPS
    assert "tts.append" not in advertised_capabilities(tts_available=False)
    assert "voice.model.download" in advertised_capabilities(tts_available=True)


def test_voice_control_frames_are_negotiated_and_round_trip(fake_asr) -> None:
    control = VoiceRuntimeControl(AudioCapture())
    client = TestClient(_dvap_tts_app(fake_asr, voice_control=control))
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=("voice.mode", "voice.devices")))
        welcome = websocket.receive_json()
        assert welcome["capabilities"] == ["voice.mode", "voice.devices"]
        websocket.send_json({"type": "voice.mode.set", "mode": "wake_word"})
        mode = websocket.receive_json()
        assert mode["type"] == "voice.mode"
        assert mode["mode"] == "push_to_talk"
        assert mode["wakeWordAvailable"] is False
        websocket.send_json({"type": "voice.devices.get"})
        assert websocket.receive_json()["type"] == "voice.devices"


def test_wake_word_event_maps_to_real_barge_in_source() -> None:
    assert barge_in_for_event(WakeWordDetected()) == {
        "type": "barge_in",
        "source": "wake_word",
    }


def test_tts_capabilities_advertised_when_backend_available(fake_asr) -> None:
    client = TestClient(_dvap_tts_app(fake_asr))
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
        welcome = websocket.receive_json()
    assert welcome["capabilities"] == list(_TTS_HELLO_CAPS)


def test_tts_backend_factory_isolates_concurrent_dvap_connections(fake_asr) -> None:
    created: list[FakeTtsBackend] = []

    def backend_factory() -> FakeTtsBackend:
        backend = FakeTtsBackend()
        created.append(backend)
        return backend

    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_dvap_websocket(app, engine, tts_backend_factory=backend_factory)
    client = TestClient(app)

    with client.websocket_connect("/dvap") as first:
        first.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
        assert first.receive_json()["capabilities"] == list(_TTS_HELLO_CAPS)
        with client.websocket_connect("/dvap") as second:
            second.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
            assert second.receive_json()["capabilities"] == list(_TTS_HELLO_CAPS)

    assert len(created) == 2
    assert created[0] is not created[1]


def test_tts_capabilities_not_advertised_without_backend(fake_asr) -> None:
    # No TTS backend → tts.* are recognized-but-optional and dropped.
    client = TestClient(_dvap_app(fake_asr))
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
        welcome = websocket.receive_json()
    assert welcome["capabilities"] == [
        "stt.partial",
        "stt.final",
        "audio.in.stream",
        "text.compose",
    ]


def test_unavailable_backend_does_not_advertise_tts(fake_asr) -> None:
    client = TestClient(_dvap_tts_app(fake_asr, backend=FakeTtsBackend(available=False)))
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
        welcome = websocket.receive_json()
    assert welcome["capabilities"] == [
        "stt.partial",
        "stt.final",
        "audio.in.stream",
        "text.compose",
    ]


def test_phone_stream_audio_and_tts_are_request_scoped(fake_asr) -> None:
    client = TestClient(_dvap_tts_app(fake_asr))
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(
            build_hello(capabilities=("audio.in.stream", "tts.synth.stream", "stt.final"))
        )
        assert websocket.receive_json()["capabilities"] == [
            "audio.in.stream",
            "tts.synth.stream",
            "stt.final",
        ]
        websocket.send_json(
            {
                "type": "audio.in.begin",
                "requestId": "phone_1",
                "sampleRate": 16000,
                "channels": 1,
                "encoding": "pcm_s16le",
            }
        )
        websocket.send_bytes((np.ones(3200, dtype="<i2") * 1000).tobytes())
        websocket.send_json({"type": "audio.in.end", "requestId": "phone_1"})
        assert websocket.receive_json() == {
            "type": "stt.final",
            "text": "Hello world.",
            "requestId": "phone_1",
        }

        websocket.send_json({"type": "tts.synth.begin", "requestId": "phone_1", "text": "Done."})
        begin = websocket.receive_json()
        assert begin == {
            "type": "tts.audio.begin",
            "requestId": "phone_1",
            "sampleRate": 24000,
            "channels": 1,
            "encoding": "pcm_s16le",
        }
        assert websocket.receive_bytes()
        while True:
            frame = websocket.receive()
            if frame.get("text"):
                assert json.loads(frame["text"]) == {
                    "type": "tts.audio.end",
                    "requestId": "phone_1",
                }
                break


# tts.append / tts.cancel -------------------------------------------------------


def test_tts_message_rejected_when_capability_not_negotiated(fake_asr) -> None:
    client = TestClient(_dvap_tts_app(fake_asr))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.send_json(build_hello(capabilities=("stt.partial", "stt.final")))
        websocket.receive_json()
        websocket.send_json({"type": "tts.append", "text": "not negotiated"})
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_POLICY


def test_tts_append_text_limit_closes_1009(fake_asr) -> None:
    client = TestClient(_dvap_tts_app(fake_asr))

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/dvap") as websocket,
    ):
        websocket.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
        websocket.receive_json()
        websocket.send_json({"type": "tts.append", "text": "x" * (MAX_TTS_APPEND_CHARS + 1)})
        websocket.receive_json()

    assert excinfo.value.code == DVAP_CLOSE_TOO_LARGE


def test_tts_pending_message_budget_is_enforced() -> None:
    class SaturatedPlayer:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._queue = deque(["pending"] * MAX_TTS_PENDING_MESSAGES)

        def append(self, text: str) -> None:  # pragma: no cover - must be rejected first
            raise AssertionError(text)

        def flush(self) -> None:  # pragma: no cover - must be rejected first
            raise AssertionError

    with pytest.raises(DVAPMessageError, match="queue") as excinfo:
        _apply_tts_message(
            {"type": "tts.append", "text": "one more"},
            SaturatedPlayer(),  # type: ignore[arg-type]
        )
    assert excinfo.value.too_large is True


def test_tts_append_produces_first_audio_under_800ms(fake_asr) -> None:
    sinks: list[FakeAudioSink] = []
    client = TestClient(_dvap_tts_app(fake_asr, sinks=sinks))
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
        websocket.receive_json()  # welcome

        start = time.monotonic()
        websocket.send_json({"type": "tts.append", "text": "Hello there.", "final": True})
        assert _wait(lambda: bool(sinks) and bool(sinks[0].chunks))
        first_audio_ms = (sinks[0].write_times[0] - start) * 1000
    assert first_audio_ms < 800, f"first audio took {first_audio_ms:.0f} ms"


def test_tts_cancel_stops_playback(fake_asr) -> None:
    sinks: list[FakeAudioSink] = []
    client = TestClient(_dvap_tts_app(fake_asr, sinks=sinks))
    with client.websocket_connect("/dvap") as websocket:
        websocket.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
        websocket.receive_json()

        websocket.send_json(
            {
                "type": "tts.append",
                "text": "A long spoken reply that keeps going for a while.",
                "final": True,
            }
        )
        assert _wait(lambda: bool(sinks) and bool(sinks[0].chunks))
        t0 = time.monotonic()
        websocket.send_json({"type": "tts.cancel"})
        assert _wait(lambda: sinks[0].stopped, timeout=1.0)
        stop_ms = (sinks[0].stop_time - t0) * 1000
    assert stop_ms < 100, f"audible stop took {stop_ms:.0f} ms"


# barge_in ----------------------------------------------------------------------


def test_barge_in_emitted_on_hotkey_while_playing(fake_asr) -> None:
    bus = EventBus()
    bus.start()
    sinks: list[FakeAudioSink] = []
    client = TestClient(_dvap_tts_app(fake_asr, sinks=sinks, bus=bus))
    try:
        with client.websocket_connect("/dvap") as websocket:
            websocket.send_json(build_hello(capabilities=_TTS_HELLO_CAPS))
            websocket.receive_json()

            websocket.send_json(
                {
                    "type": "tts.append",
                    "text": "A long reply so playback is still going.",
                    "final": True,
                }
            )
            assert _wait(lambda: bool(sinks) and sinks[0].chunks and not sinks[0].stopped)
            # A PTT press while TTS speaks interrupts it.
            bus.publish(HotkeyPressed(AppMode.DICTATION))
            message = websocket.receive_json()
    finally:
        bus.stop()

    assert message == {"type": "barge_in", "source": "ptt"}
    assert sinks[0].stopped


def test_build_barge_in_and_event_mapping() -> None:
    assert build_barge_in("ptt") == {"type": "barge_in", "source": "ptt"}
    with pytest.raises(ValueError):
        build_barge_in("telepathy")
    assert barge_in_for_event(HotkeyPressed(AppMode.COMMAND)) == {
        "type": "barge_in",
        "source": "ptt",
    }
    assert barge_in_for_event(PrivacyChanged("cloud")) is None


# Schema conformance for the new message family ---------------------------------


def test_tts_family_messages_validate(message_validator) -> None:
    for message in (
        {"type": "tts.append", "text": "hello", "stable": True},
        {"type": "tts.cancel"},
        {"type": "tts.cancel", "reason": "barge-in"},
        build_barge_in("ptt"),
        build_barge_in("wake_word"),
        build_barge_in("vad"),
    ):
        message_validator.validate(message)


def test_registry_entry_advertises_tts_when_available(tmp_path) -> None:
    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        token="tok",
        tts_available=True,
    )
    assert "tts.append" in entry.capabilities
    assert "barge_in" in entry.capabilities
    assert "voice.model.download" in entry.capabilities
