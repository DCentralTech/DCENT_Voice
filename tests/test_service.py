# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from dcent_voice.asr.base import Locality, TranscriptResult
from dcent_voice.commands.router import CommandRouter
from dcent_voice.events import EventBus, PrivacyChanged
from dcent_voice.privacy import ConsentLedger, EgressLog, PrivacyMonitor, ProviderPrivacy
from dcent_voice.service.api import (
    MAX_AUDIO_SECONDS,
    ServiceBusyError,
    ServiceEngine,
    TranscribeRequest,
    create_app,
    run_bounded_worker,
)
from dcent_voice.service.ws import (
    WS_CLOSE_BUSY,
    WS_CLOSE_POLICY,
    WS_CLOSE_TOO_LARGE,
    _offer_latest,
    add_event_websocket,
    add_stream_websocket,
)


def test_health_reports_ok(fake_asr) -> None:
    client = TestClient(create_app(ServiceEngine(asr=fake_asr)))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "subsystems" in response.json()
    assert response.json()["subsystems"]["service"]["ok"] is True


def test_health_reports_dead_hotkeys(fake_asr) -> None:
    engine = ServiceEngine(
        asr=fake_asr,
        status_providers={
            "hotkeys": lambda: {
                "enabled": True,
                "ok": False,
                "status": "dead",
                "listener_running": False,
                "critical": True,
            }
        },
    )
    client = TestClient(create_app(engine))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["subsystems"]["hotkeys"]["status"] == "dead"


def test_transcribe_returns_raw_and_cleaned(fake_asr, fake_llm) -> None:
    from dcent_voice.llm.cleanup import CleanupPipeline

    fake_llm.response = "Hello world."
    engine = ServiceEngine(asr=fake_asr, cleanup=CleanupPipeline(fake_llm))
    client = TestClient(create_app(engine))

    response = client.post("/transcribe", json={"audio": [0.1] * 16000, "samplerate": 16000})

    assert response.status_code == 200
    assert response.json()["raw"] == "hello world"
    assert response.json()["cleaned"] == "Hello world."


@pytest.mark.parametrize("ledger_change", ["revoke", "corrupt"])
def test_transcribe_maps_live_cloud_consent_loss_to_structured_403(tmp_path, ledger_change) -> None:
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
    client = TestClient(create_app(ServiceEngine(asr=ConsentCheckingASR(), privacy=monitor)))
    payload = {"audio": [0.1] * 1600, "samplerate": 16000}

    assert client.post("/transcribe", json=payload).status_code == 200
    if ledger_change == "revoke":
        ledger.revoke("asr:openai")
    else:
        ledger.path.write_text("{invalid", encoding="utf-8")

    blocked = client.post("/transcribe", json=payload)

    assert blocked.status_code == 403
    assert blocked.json()["error"] == {
        "code": "consent_required",
        "message": "Cloud consent required for: asr:openai",
        "retryable": False,
    }
    assert "asr:openai" in blocked.json()["detail"]
    assert len(egress.tail()) == 1


def test_transcribe_default_cleaned_uses_local_compose(fake_asr) -> None:
    fake_asr.text = "um hello world"
    engine = ServiceEngine(asr=fake_asr)
    client = TestClient(create_app(engine))
    response = client.post(
        "/transcribe",
        json={"audio": [0.1] * 16000, "samplerate": 16000},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["raw"] == "um hello world"
    assert "um" not in body["cleaned"].lower()
    assert "hello world" in body["cleaned"].lower()
    raw_only = client.post(
        "/transcribe",
        json={"audio": [0.1] * 16000, "samplerate": 16000, "polish": False},
    )
    assert raw_only.json()["cleaned"] == "um hello world"


def test_transcribe_polish_without_style_uses_local_compose(fake_asr) -> None:
    fake_asr.text = "um hello world"
    engine = ServiceEngine(asr=fake_asr)
    client = TestClient(create_app(engine))
    response = client.post(
        "/transcribe",
        json={"audio": [0.1] * 16000, "samplerate": 16000, "polish": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["raw"] == "um hello world"
    assert "um" not in body["cleaned"].lower()
    assert "hello world" in body["cleaned"].lower()


def test_transcribe_cleanup_level_high_drops_hedges(fake_asr) -> None:
    fake_asr.text = "I guess I think we should ship Monday"
    engine = ServiceEngine(asr=fake_asr)
    client = TestClient(create_app(engine))
    high = client.post(
        "/transcribe",
        json={
            "audio": [0.1] * 16000,
            "samplerate": 16000,
            "cleanup_level": "high",
        },
    )
    assert high.status_code == 200
    cleaned = high.json()["cleaned"].lower()
    assert "i guess" not in cleaned
    assert "i think" not in cleaned
    assert "ship monday" in cleaned
    medium = client.post(
        "/transcribe",
        json={
            "audio": [0.1] * 16000,
            "samplerate": 16000,
            "cleanup_level": "medium",
        },
    )
    assert medium.status_code == 200
    kept = medium.json()["cleaned"].lower()
    assert "i think" in kept or "i guess" in kept


def test_transcribe_style_without_llm_uses_local_compose(fake_asr) -> None:
    fake_asr.text = "hey can you send the deck to alice actually bob thanks"
    engine = ServiceEngine(asr=fake_asr)
    client = TestClient(create_app(engine))
    response = client.post(
        "/transcribe",
        json={"audio": [0.1] * 16000, "samplerate": 16000, "style": "email"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["raw"] == fake_asr.text
    assert "alice" not in body["cleaned"].lower()
    assert "bob" in body["cleaned"].lower()
    assert "\n\n" in body["cleaned"]


def test_transcribe_forwards_cleanup_style(fake_asr, fake_llm) -> None:
    from dcent_voice.llm.cleanup import CleanupPipeline

    fake_llm.response = "Hi team,\n\nShipped."
    engine = ServiceEngine(asr=fake_asr, cleanup=CleanupPipeline(fake_llm))
    client = TestClient(create_app(engine))
    response = client.post(
        "/transcribe",
        json={"audio": [0.1] * 16000, "samplerate": 16000, "style": "email"},
    )
    assert response.status_code == 200
    assert "Destination: email" in fake_llm.last_system
    # Cleanup must receive the composed string, not raw ASR.
    fake_asr.text = "hey can you send the deck to alice actually bob thanks"
    fake_llm.response = "Hi,\n\nSend the deck to Bob.\n\nThanks,"
    second = client.post(
        "/transcribe",
        json={"audio": [0.1] * 16000, "samplerate": 16000, "style": "email"},
    )
    assert second.status_code == 200
    assert "alice" not in fake_llm.last_user.lower()
    assert "bob" in fake_llm.last_user.lower()


def test_capabilities_requires_token_and_lists_wav(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr)
    client = TestClient(create_app(engine, token="s3cret"))
    denied = client.get("/capabilities")
    assert denied.status_code == 401
    ok = client.get("/capabilities", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["headless"] is True
    assert "wav_b64" in body["features"]
    assert "prose_context" in body["features"]


def test_concurrent_service_languages_are_serialized_and_restored() -> None:
    entered = threading.Event()
    release = threading.Event()

    class LanguageProvider:
        language: str | None = "auto"
        calls: list[str | None] = []

        def transcribe(self, audio, samplerate=16000, initial_prompt=None):
            del audio, samplerate, initial_prompt
            observed = self.language
            self.calls.append(observed)
            if len(self.calls) == 1:
                entered.set()
                assert release.wait(2)
            return TranscriptResult(
                text=f"text-{observed}",
                language=str(observed),
                duration_s=0.1,
                asr_latency_s=0.01,
            )

    provider = LanguageProvider()
    engine = ServiceEngine(asr=provider)  # type: ignore[arg-type]
    results: dict[str, dict[str, object]] = {}
    french = threading.Thread(
        target=lambda: results.setdefault(
            "fr",
            engine.transcribe(
                TranscribeRequest(audio=[0.1], language="fr", polish=False, cleanup=False)
            ),
        )
    )
    german = threading.Thread(
        target=lambda: results.setdefault(
            "de",
            engine.transcribe(
                TranscribeRequest(audio=[0.1], language="de", polish=False, cleanup=False)
            ),
        )
    )
    french.start()
    assert entered.wait(1)
    german.start()
    release.set()
    french.join(3)
    german.join(3)

    assert not french.is_alive() and not german.is_alive()
    assert provider.calls == ["fr", "de"]
    assert results["fr"]["language"] == "fr"
    assert results["de"]["language"] == "de"
    assert provider.language == "auto"


def test_service_language_restores_after_exception_and_cancelled_waiter() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingLanguageProvider:
        language: str | None = None
        fail = True

        def transcribe(self, audio, samplerate=16000, initial_prompt=None):
            del audio, samplerate, initial_prompt
            entered.set()
            assert release.wait(2)
            try:
                if self.fail:
                    raise RuntimeError("provider failed")
                return TranscriptResult(
                    text="ok",
                    language=str(self.language),
                    duration_s=0.1,
                    asr_latency_s=0.01,
                )
            finally:
                finished.set()

    async def exercise() -> None:
        provider = BlockingLanguageProvider()
        engine = ServiceEngine(asr=provider)  # type: ignore[arg-type]
        request = TranscribeRequest(audio=[0.1], language="fr", polish=False, cleanup=False)
        task = asyncio.create_task(run_bounded_worker(engine, engine.transcribe, request))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert provider.language is None

        provider.fail = False
        entered.clear()
        finished.clear()
        response = engine.transcribe(
            TranscribeRequest(audio=[0.1], language="de", polish=False, cleanup=False)
        )
        assert response["language"] == "de"
        assert provider.language is None

    asyncio.run(exercise())


def test_learn_records_typed_correction_without_audio(fake_asr, tmp_path) -> None:
    from dcent_voice.personalization import PersonalizationStore

    store = PersonalizationStore(tmp_path / "p.json")
    engine = ServiceEngine(asr=fake_asr, personalization=store)
    client = TestClient(create_app(engine, token="tok"))
    denied = client.post("/learn", json={"correction": "Hello DCENT_Voice"})
    assert denied.status_code == 401
    headers = {"Authorization": "Bearer tok"}

    # The reusable service is stateless: no client may claim another client's
    # process-wide "last transcript", and correction-only requests never mutate.
    fake_asr.text = "project alpha"
    assert (
        client.post(
            "/transcribe", json={"audio": [0.1], "app_context": "a.exe"}, headers=headers
        ).status_code
        == 200
    )
    fake_asr.text = "project beta"
    assert (
        client.post(
            "/transcribe", json={"audio": [0.1], "app_context": "b.exe"}, headers=headers
        ).status_code
        == 200
    )
    before = store.snapshot()
    rejected = client.post(
        "/learn",
        json={"correction": "Hello DCENT_Voice"},
        headers=headers,
    )
    assert rejected.status_code == 422
    assert store.snapshot() == before
    assert client.post("/learn", json={"correction": "Again"}, headers=headers).status_code == 422
    assert store.snapshot() == before

    ok = client.post(
        "/learn",
        json={"spoken": "d sent", "written": "DCENT_Voice"},
        headers=headers,
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True
    assert body["snapshot"]["stores_audio"] is False
    listed = client.get("/personalization", headers={"Authorization": "Bearer tok"})
    assert listed.status_code == 200
    assert listed.json()["term_count"] >= 1


def test_http_personalization_is_scoped_and_applied_headlessly(fake_asr, tmp_path) -> None:
    from dcent_voice.personalization import PersonalizationStore

    store = PersonalizationStore(tmp_path / "p.json")
    engine = ServiceEngine(asr=fake_asr, personalization=store)
    client = TestClient(create_app(engine, token="tok"))
    headers = {"Authorization": "Bearer tok"}
    correction = {
        "spoken": "pie test",
        "written": "pytest",
        "style": "code",
        "app_context": "Code.exe",
    }
    for _ in range(2):
        response = client.post("/learn", json=correction, headers=headers)
        assert response.status_code == 200
        assert response.json()["term"]["app"] == "code.exe"

    fake_asr.text = "pie-test"
    matching = client.post(
        "/transcribe",
        json={
            "audio": [0.1],
            "polish": False,
            "style": "code",
            "app_context": "Code.exe",
        },
        headers=headers,
    )
    other_app = client.post(
        "/transcribe",
        json={
            "audio": [0.1],
            "polish": False,
            "style": "code",
            "app_context": "notepad.exe",
        },
        headers=headers,
    )

    assert matching.json()["raw"] == "pie-test"
    assert "pytest" in matching.json()["cleaned"].lower()
    assert "pie-test" in other_app.json()["cleaned"].lower()


def test_http_prose_context_is_explicit_and_defaults_false(fake_asr, tmp_path) -> None:
    from dcent_voice.personalization import PersonalizationStore

    store = PersonalizationStore(tmp_path / "p.json")
    store.record_correction("d central", "D-Central")
    engine = ServiceEngine(asr=fake_asr, personalization=store)
    client = TestClient(create_app(engine, token="tok"))
    headers = {"Authorization": "Bearer tok"}
    fake_asr.text = "Open d central settings."

    conservative = client.post(
        "/transcribe",
        json={"audio": [0.1], "polish": False, "style": "plain"},
        headers=headers,
    )
    trusted = client.post(
        "/transcribe",
        json={
            "audio": [0.1],
            "polish": False,
            "style": "plain",
            "prose_context": True,
        },
        headers=headers,
    )

    assert conservative.json()["cleaned"] == "Open d central settings."
    assert trusted.json()["cleaned"] == "Open D-Central settings."


@pytest.mark.parametrize("invalid", ["false", "true", "yes", "on", 1, 0, [], None])
def test_http_prose_context_rejects_coercive_values(
    fake_asr, tmp_path, monkeypatch, invalid: object
) -> None:
    from dcent_voice.personalization import PersonalizationStore

    calls: list[object] = []
    original_transcribe = fake_asr.transcribe

    def tracked_transcribe(*args, **kwargs):
        calls.append(args[0])
        return original_transcribe(*args, **kwargs)

    monkeypatch.setattr(fake_asr, "transcribe", tracked_transcribe)
    store = PersonalizationStore(tmp_path / "strict-http.json")
    store.record_correction("d central", "D-Central")
    client = TestClient(create_app(ServiceEngine(asr=fake_asr, personalization=store)))

    response = client.post(
        "/transcribe",
        json={"audio": [0.1], "prose_context": invalid},
    )

    assert response.status_code == 422
    assert calls == []


def test_http_transcribe_returns_busy_when_worker_capacity_is_full(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr, worker_limit=1)
    assert engine.try_acquire_worker_slot()
    client = TestClient(create_app(engine))

    try:
        response = client.post("/transcribe", json={"audio": [0.1], "samplerate": 16_000})
    finally:
        engine.release_worker_slot()

    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == "Speech service is busy; retry shortly."
    assert body["error"] == {
        "code": "busy",
        "message": "Speech service is busy; retry shortly.",
        "retryable": True,
    }


def test_command_endpoint_routes_intent(fake_asr) -> None:
    client = TestClient(create_app(ServiceEngine(asr=fake_asr, router=CommandRouter())))

    response = client.post("/command", json={"transcript": "what's 2+2"})

    assert response.status_code == 200
    assert response.json()["action"] == "insert_text"
    assert response.json()["text"] == "4"


def test_stream_websocket_returns_partial(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    client = TestClient(app)

    with client.websocket_connect("/stream") as websocket:
        websocket.send_json({"audio": [0.1] * 16000, "samplerate": 16000})
        message = websocket.receive_json()

    assert message["type"] == "partial"
    assert message["text"] == "hello world"


def test_stream_websocket_prose_context_is_explicit_and_defaults_false(fake_asr, tmp_path) -> None:
    from dcent_voice.personalization import PersonalizationStore

    store = PersonalizationStore(tmp_path / "stream-prose.json")
    store.record_correction("d central", "D-Central")
    fake_asr.text = "Open d central settings."
    engine = ServiceEngine(asr=fake_asr, personalization=store)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    client = TestClient(app)

    def final_text(**context) -> str:
        with client.websocket_connect("/stream") as websocket:
            websocket.send_json(
                {
                    "audio": [0.1] * 1600,
                    "samplerate": 16000,
                    "final": True,
                    "polish": False,
                    "style": "plain",
                    **context,
                }
            )
            return str(websocket.receive_json()["text"])

    assert final_text() == "Open d central settings."
    assert final_text(prose_context=False) == "Open d central settings."
    assert final_text(prose_context=True) == "Open D-Central settings."


def test_stream_websocket_does_not_leak_trusted_prose_to_next_utterance(fake_asr, tmp_path) -> None:
    from dcent_voice.personalization import PersonalizationStore

    store = PersonalizationStore(tmp_path / "stream-prose-reset.json")
    store.record_correction("d central", "D-Central")
    fake_asr.text = "Open d central settings."
    engine = ServiceEngine(asr=fake_asr, personalization=store)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    client = TestClient(app)

    base = {
        "audio": [0.1] * 1600,
        "samplerate": 16000,
        "final": True,
        "polish": False,
        "style": "plain",
    }
    with client.websocket_connect("/stream") as websocket:
        websocket.send_json({**base, "prose_context": True})
        trusted = websocket.receive_json()["text"]
        websocket.send_json(base)
        next_utterance = websocket.receive_json()["text"]

    assert trusted == "Open D-Central settings."
    assert next_utterance == "Open d central settings."


@pytest.mark.parametrize("invalid", ["true", 1, [], {}, None])
def test_stream_websocket_rejects_non_boolean_prose_context(fake_asr, invalid) -> None:
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/stream") as websocket,
    ):
        websocket.send_json(
            {
                "audio": [0.1],
                "samplerate": 16000,
                "prose_context": invalid,
            }
        )
        websocket.receive_json()

    assert excinfo.value.code == WS_CLOSE_POLICY


def test_http_transcribe_rejects_nonfinite_and_overlong_audio(fake_asr) -> None:
    client = TestClient(create_app(ServiceEngine(asr=fake_asr)))

    nonfinite = client.post(
        "/transcribe",
        content='{"audio":[NaN],"samplerate":16000}',
        headers={"content-type": "application/json"},
    )
    assert nonfinite.status_code == 422

    float32_overflow = client.post("/transcribe", json={"audio": [1e100], "samplerate": 16000})
    assert float32_overflow.status_code == 422

    with pytest.raises(ValidationError, match="duration"):
        TranscribeRequest(
            audio=[0.0] * (8_000 * MAX_AUDIO_SECONDS + 1),
            samplerate=8_000,
        )


def test_http_body_limit_rejects_declared_payload_before_json_parse(fake_asr) -> None:
    client = TestClient(create_app(ServiceEngine(asr=fake_asr), max_body_bytes=32))

    response = client.post(
        "/transcribe",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": "33"},
    )

    assert response.status_code == 413
    body = response.json()
    assert body["detail"] == "Request body exceeds the service limit."
    assert body["error"] == {
        "code": "payload_too_large",
        "message": "Request body exceeds the service limit.",
        "retryable": False,
    }


def test_http_body_limit_rejects_chunked_payload_before_json_parse(fake_asr) -> None:
    app = create_app(ServiceEngine(asr=fake_asr), max_body_bytes=20)

    async def exercise() -> list[dict]:
        incoming = iter(
            [
                {"type": "http.request", "body": b'{"audio":[0.1,', "more_body": True},
                {"type": "http.request", "body": b"0.2,0.3]}", "more_body": False},
            ]
        )
        sent: list[dict] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/transcribe",
            "raw_path": b"/transcribe",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        async def receive() -> dict:
            return next(incoming)

        async def send(message: dict) -> None:
            sent.append(message)

        await app(scope, receive, send)
        return sent

    sent = asyncio.run(exercise())

    assert (
        next(message for message in sent if message["type"] == "http.response.start")["status"]
        == 413
    )


def test_command_endpoint_bounds_text_fields(fake_asr) -> None:
    client = TestClient(create_app(ServiceEngine(asr=fake_asr)))

    response = client.post("/command", json={"transcript": "x" * 20_001})

    assert response.status_code == 422


def test_stream_websocket_rejects_oversized_chunk_before_asr(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/stream") as websocket,
    ):
        websocket.send_json({"audio": [0.1] * (16_000 * 5 + 1), "samplerate": 16_000})
        websocket.receive_json()

    assert excinfo.value.code == WS_CLOSE_TOO_LARGE


def test_stream_websocket_rejects_invalid_samplerate(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/stream") as websocket,
    ):
        websocket.send_json({"audio": [0.1], "samplerate": 192_000})
        websocket.receive_json()

    assert excinfo.value.code == WS_CLOSE_POLICY


def test_stream_websocket_enforces_connection_cap(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_stream_websocket(app, engine, max_connections=1)
    client = TestClient(app)

    with client.websocket_connect("/stream") as first:
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/stream") as second,
        ):
            second.receive_json()
        assert excinfo.value.code == WS_CLOSE_BUSY

        first.send_json({"audio": [0.1] * 16_000, "samplerate": 16_000})
        assert first.receive_json()["type"] == "partial"


def test_bounded_queue_latest_wins() -> None:
    async def exercise() -> list[int]:
        target: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        _offer_latest(target, 1)
        _offer_latest(target, 2)
        _offer_latest(target, 3)
        return [target.get_nowait(), target.get_nowait()]

    assert asyncio.run(exercise()) == [2, 3]


def test_worker_slot_survives_cancellation_until_thread_finishes(fake_asr) -> None:
    async def exercise() -> None:
        engine = ServiceEngine(asr=fake_asr, worker_limit=1)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_work() -> None:
            started.set()
            release.wait(1.0)
            finished.set()

        task = asyncio.create_task(run_bounded_worker(engine, blocking_work))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(ServiceBusyError):
            await run_bounded_worker(engine, lambda: None)

        release.set()
        assert await asyncio.to_thread(finished.wait, 1.0)
        assert engine.try_acquire_worker_slot()
        engine.release_worker_slot()

    asyncio.run(exercise())


def test_worker_slot_releases_after_queued_work_is_cancelled(fake_asr) -> None:
    async def wait_until(predicate) -> None:
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting for background work")

    async def exercise() -> None:
        engine = ServiceEngine(asr=fake_asr, worker_limit=1)
        blocker_started = threading.Event()
        unblock = threading.Event()
        worker_finished = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)

        def blocker() -> None:
            blocker_started.set()
            unblock.wait(1.0)

        def queued_work() -> None:
            worker_finished.set()

        try:
            blocker_future = loop.run_in_executor(None, blocker)
            await wait_until(blocker_started.is_set)
            task = asyncio.create_task(run_bounded_worker(engine, queued_work))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            unblock.set()
            await asyncio.wait_for(asyncio.shield(blocker_future), 1.0)
            await wait_until(worker_finished.is_set)

            def slot_free() -> bool:
                if not engine.try_acquire_worker_slot():
                    return False
                engine.release_worker_slot()
                return True

            await wait_until(slot_free)
            assert engine.try_acquire_worker_slot()
            engine.release_worker_slot()
        finally:
            executor.shutdown(wait=True)

    asyncio.run(exercise())


def test_event_websocket_quiet_disconnect_releases_connection_slot(fake_asr) -> None:
    bus = EventBus()
    bus.start()
    app = create_app(ServiceEngine(asr=fake_asr))
    add_event_websocket(app, bus, max_connections=1, queue_size=2)
    client = TestClient(app)

    try:
        with client.websocket_connect("/events"):
            pass

        with client.websocket_connect("/events") as websocket:
            bus.publish(PrivacyChanged(status="sovereign"))
            message = websocket.receive_json()
    finally:
        bus.stop()

    assert message == {
        "type": "PrivacyChanged",
        "payload": {
            "status": "sovereign",
            "detail": "",
            "consent_state": "",
            "reason": "",
            "missing_providers": [],
        },
    }


def test_transcribe_requires_token_when_configured(fake_asr) -> None:
    client = TestClient(create_app(ServiceEngine(asr=fake_asr), token="s3cret"))
    payload = {"audio": [0.1] * 16000, "samplerate": 16000}

    unauth = client.post("/transcribe", json=payload)
    assert unauth.status_code == 401

    authed = client.post("/transcribe", json=payload, headers={"Authorization": "Bearer s3cret"})
    assert authed.status_code == 200


def test_transcribe_rejects_wrong_length_authorization_without_500(fake_asr) -> None:
    client = TestClient(create_app(ServiceEngine(asr=fake_asr), token="s3cret"))
    payload = {"audio": [0.1] * 16000, "samplerate": 16000}

    short = client.post("/transcribe", json=payload, headers={"Authorization": "Bearer x"})
    assert short.status_code == 401

    long_header = client.post(
        "/transcribe",
        json=payload,
        headers={"Authorization": "Bearer " + ("x" * 200)},
    )
    assert long_header.status_code == 401


def test_stream_websocket_rejects_browser_origin(fake_asr) -> None:
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/stream", headers={"origin": "https://evil.example"}
        ) as websocket,
    ):
        websocket.receive_json()


def test_liveness_probe_helpers_stay_deleted() -> None:
    """WS2: ``_can_connect``/``_is_our_service`` were dead and must not return.

    Neither had a caller. ``_is_our_service`` was already hard-wired to
    ``False`` with a docstring explaining why (a public HTTP response is not
    proof of process identity), and ``_can_connect`` invited exactly the
    "someone is listening, so it must be us" reasoning the single-instance
    mutex exists to replace. Reintroducing either is a regression.
    """
    from dcent_voice.service import server

    assert not hasattr(server, "_can_connect")
    assert not hasattr(server, "_is_our_service")


def test_service_wait_ready_false_when_error_set() -> None:
    from dcent_voice.service.server import ServiceThread

    thread = ServiceThread(app=object(), host="127.0.0.1", port=1)
    thread.error = RuntimeError("bind failed")
    assert thread.wait_ready(timeout=0.2) is False


def test_service_thread_disables_uvicorn_access_log(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from dcent_voice.service.server import ServiceThread

    configured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            configured.update(kwargs)

    class FakeServer:
        def __init__(self, _config) -> None:
            self.started = False
            self.should_exit = False

        def run(self) -> None:
            return

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )

    ServiceThread(app=object())._run()

    assert configured["access_log"] is False


def test_format_http_base_brackets_ipv6_literals() -> None:
    from dcent_voice.service.server import format_http_base

    assert format_http_base("127.0.0.1", 8765) == "http://127.0.0.1:8765"
    assert format_http_base("::1", 8765) == "http://[::1]:8765"
