# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from dcent_voice.asr.base import Locality
from dcent_voice.asr.cloud import (
    XAI_LANGUAGE_CODES,
    DeepgramASRProvider,
    GroqTranscriptionProvider,
    OpenAITranscriptionProvider,
    XaiTranscriptionProvider,
    build_cloud_asr_provider,
    float_audio_to_wav_bytes,
)
from dcent_voice.asr.factory import build_asr_provider
from dcent_voice.config import ASRSpec, load_config
from dcent_voice.engine import VoiceEngine
from dcent_voice.personalization import PersonalizationStore
from dcent_voice.privacy import (
    ConsentLedger,
    ConsentRequired,
    EgressLog,
    PrivacyMonitor,
    ProviderPrivacy,
)
from dcent_voice.service.api import ServiceEngine, TranscribeRequest, create_app


def _allow_egress(_provider_key: str, _payload_type: str, _byte_count: int) -> None:
    """Explicit metadata-only gate for tests whose focus is not privacy."""


def test_runtime_factory_rejects_cloud_asr_without_egress_logger() -> None:
    with pytest.raises(RuntimeError, match="egress logger"):
        build_asr_provider(
            ASRSpec.parse("deepgram:nova-3"),
            api_key="test",
        )


@pytest.mark.parametrize(
    ("provider_type", "spec"),
    (
        (DeepgramASRProvider, "deepgram:nova-3"),
        (OpenAITranscriptionProvider, "openai:gpt-4o-mini-transcribe"),
        (GroqTranscriptionProvider, "groq:whisper-large-v3"),
        (XaiTranscriptionProvider, "xai:grok-stt"),
    ),
)
def test_direct_cloud_provider_without_egress_logger_fails_before_wire(
    provider_type, spec: str
) -> None:
    wire_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal wire_calls
        wire_calls += 1
        return httpx.Response(200, json={})

    provider = provider_type(
        ASRSpec.parse(spec),
        api_key="test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="consent-enforcing metadata egress logger"):
        provider.transcribe(np.zeros(160, dtype=np.float32))

    assert wire_calls == 0


@pytest.mark.parametrize(
    ("provider_type", "spec"),
    (
        (DeepgramASRProvider, "deepgram:nova-3"),
        (OpenAITranscriptionProvider, "openai:gpt-4o-mini-transcribe"),
        (GroqTranscriptionProvider, "groq:whisper-large-v3"),
        (XaiTranscriptionProvider, "xai:grok-stt"),
    ),
)
def test_cloud_egress_gate_runs_immediately_before_each_wire(provider_type, spec: str) -> None:
    events: list[str] = []

    def record(provider_key: str, payload_type: str, byte_count: int) -> None:
        assert provider_key == f"asr:{ASRSpec.parse(spec).provider}"
        assert payload_type == "audio"
        assert byte_count > 44
        events.append("audit")

    def handler(_request: httpx.Request) -> httpx.Response:
        assert events == ["audit"]
        events.append("wire")
        if provider_type is DeepgramASRProvider:
            return httpx.Response(
                200,
                json={"results": {"channels": [{"alternatives": [{"transcript": "ok"}]}]}},
            )
        return httpx.Response(200, json={"text": "ok", "language": "en"})

    provider = provider_type(
        ASRSpec.parse(spec),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=record,
    )

    assert provider.transcribe(np.zeros(160, dtype=np.float32)).text == "ok"
    assert events == ["audit", "wire"]


@pytest.mark.parametrize("ledger_state", ["revoked", "corrupt"])
def test_direct_cloud_provider_rechecks_live_consent_on_every_operation(
    tmp_path: Path, ledger_state: str
) -> None:
    consent_path = tmp_path / "consent.json"
    egress_path = tmp_path / "egress.jsonl"
    ledger = ConsentLedger(consent_path)
    ledger.grant("asr:openai", payload_type="audio")
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
        egress_log=EgressLog(egress_path),
    )
    wire_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal wire_calls
        wire_calls += 1
        return httpx.Response(200, json={"text": "private spoken transcript"})

    def record(provider_key: str, payload_type: str, byte_count: int) -> None:
        monitor.record_egress(provider_key, payload_type=payload_type, byte_count=byte_count)

    provider = OpenAITranscriptionProvider(
        ASRSpec.parse("openai:gpt-4o-mini-transcribe"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=record,
    )

    assert provider.transcribe(np.zeros(160, dtype=np.float32)).text == (
        "private spoken transcript"
    )
    if ledger_state == "revoked":
        ledger.revoke("asr:openai")
    else:
        consent_path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(ConsentRequired, match="asr:openai"):
        provider.transcribe(np.zeros(160, dtype=np.float32))

    assert wire_calls == 1
    lines = egress_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "private spoken transcript" not in lines[0]


def test_cloud_network_failure_still_has_pre_wire_metadata_audit() -> None:
    recorded: list[tuple[str, str, int]] = []

    def record(provider_key: str, payload_type: str, byte_count: int) -> None:
        recorded.append((provider_key, payload_type, byte_count))

    def handler(request: httpx.Request) -> httpx.Response:
        assert len(recorded) == 1
        raise httpx.ConnectError("offline", request=request)

    provider = OpenAITranscriptionProvider(
        ASRSpec.parse("openai:gpt-4o-mini-transcribe"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=record,
    )

    with pytest.raises(httpx.ConnectError, match="offline"):
        provider.transcribe(np.zeros(160, dtype=np.float32))

    assert len(recorded) == 1
    assert recorded[0][0:2] == ("asr:openai", "audio")
    assert recorded[0][2] > 44


def test_cloud_asr_owned_client_ignores_proxy_environment_and_redirects(monkeypatch) -> None:
    from dcent_voice.asr import cloud as cloud_module

    options: list[dict[str, object]] = []
    real_client = httpx.Client

    def guarded_client(*args, **kwargs):
        options.append(dict(kwargs))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(cloud_module.httpx, "Client", guarded_client)
    provider = OpenAITranscriptionProvider(
        ASRSpec.parse("openai:gpt-4o-mini-transcribe"),
        api_key="test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"text": "ok"})),
        egress_logger=_allow_egress,
    )

    assert provider.transcribe(np.zeros(160, dtype=np.float32)).text == "ok"
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_float_audio_to_wav_bytes_has_riff_header() -> None:
    wav = float_audio_to_wav_bytes(np.zeros(160, dtype=np.float32), 16000)

    assert wav.startswith(b"RIFF")
    assert b"WAVE" in wav[:16]


def test_deepgram_provider_records_egress_and_parses_response() -> None:
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Token test"
        assert request.content.startswith(b"RIFF")
        return httpx.Response(
            200,
            json={"results": {"channels": [{"alternatives": [{"transcript": "hello cloud"}]}]}},
        )

    provider = DeepgramASRProvider(
        ASRSpec.parse("deepgram:nova-3"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda provider_key, payload_type, byte_count: recorded.append(
            (provider_key, payload_type, byte_count)
        ),
    )

    result = provider.transcribe(np.zeros(160, dtype=np.float32), 16000)

    assert result.text == "hello cloud"
    assert recorded[0][0] == "asr:deepgram"
    assert recorded[0][1] == "audio"
    assert recorded[0][2] > 0


@pytest.mark.parametrize("automatic", ["", "auto", "detect"])
def test_deepgram_explicit_auto_uses_documented_detection_query(
    automatic: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": {"channels": [{"alternatives": [{"transcript": "bonjour"}]}]}},
        )

    provider = DeepgramASRProvider(
        ASRSpec.parse("deepgram:nova-3"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=_allow_egress,
    )
    provider.transcribe(np.zeros(160, dtype=np.float32), language=automatic)
    assert requests[0].url.params["detect_language"] == "true"
    assert "language" not in requests[0].url.params


def test_deepgram_omitted_language_keeps_documented_english_default() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(
            200,
            json={"results": {"channels": [{"alternatives": [{"transcript": "hello"}]}]}},
        )

    provider = DeepgramASRProvider(
        ASRSpec.parse("deepgram:nova-3"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=_allow_egress,
    )
    provider.transcribe(np.zeros(160, dtype=np.float32), language=None)
    assert "detect_language" not in urls[0]
    assert "language=" not in urls[0]


def test_build_cloud_asr_provider_supports_groq() -> None:
    provider = build_cloud_asr_provider(
        ASRSpec.parse("groq:whisper-large-v3"),
        api_key="test",
        egress_logger=_allow_egress,
    )

    assert provider.spec.provider == "groq"


def test_xai_provider_posts_wav_and_keyterms() -> None:
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.x.ai"
        assert "keyterm" in str(request.url)
        assert request.headers["authorization"] == "Bearer test"
        body = request.content
        assert b"audio.wav" in body or request.content.startswith(b"RIFF") or b"RIFF" in body
        return httpx.Response(200, json={"text": "hello grok", "language": "en"})

    provider = XaiTranscriptionProvider(
        ASRSpec.parse("xai:grok-stt"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda provider_key, payload_type, byte_count: recorded.append(
            (provider_key, payload_type, byte_count)
        ),
    )
    result = provider.transcribe(
        np.zeros(160, dtype=np.float32),
        16000,
        hotwords="DCENT_Voice bitcoin",
    )
    assert result.text == "hello grok"
    assert recorded[0][0] == "asr:xai"


def test_build_cloud_asr_provider_supports_xai() -> None:
    provider = build_cloud_asr_provider(
        ASRSpec.parse("xai:grok-stt"), api_key="test", egress_logger=_allow_egress
    )
    assert provider.spec.provider == "xai"


@pytest.mark.parametrize(
    ("provider_type", "spec"),
    (
        (XaiTranscriptionProvider, "xai:grok-stt"),
        (OpenAITranscriptionProvider, "openai:gpt-4o-mini-transcribe"),
        (GroqTranscriptionProvider, "groq:whisper-large-v3"),
    ),
)
def test_service_language_is_normalized_and_sent_on_cloud_multipart_wire(
    provider_type, spec: str
) -> None:
    wires: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wires.append(request.content)
        return httpx.Response(200, json={"text": "bonjour", "language": "fr"})

    provider = provider_type(
        ASRSpec.parse(spec),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=_allow_egress,
    )
    result = ServiceEngine(asr=provider).transcribe(
        TranscribeRequest(audio=[0.1], language=" FR ", polish=False, cleanup=False)
    )

    assert result["language"] == "fr"
    assert len(wires) == 1
    assert b'name="language"' in wires[0]
    assert b"\r\n\r\nfr\r\n" in wires[0]
    if provider_type is XaiTranscriptionProvider:
        assert b'name="format"' in wires[0]
        assert b"\r\n\r\ntrue\r\n" in wires[0]


@pytest.mark.parametrize("invalid", ["eng", "fr-CA", "f", "123", "f_", " français "])
def test_cloud_language_rejects_non_iso_639_1_before_egress(invalid: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"text": "unexpected"})

    provider = OpenAITranscriptionProvider(
        ASRSpec.parse("openai:gpt-4o-mini-transcribe"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *_args: pytest.fail(
            "invalid language must be rejected before the egress gate"
        ),
    )
    with pytest.raises(ValueError, match="ISO-639-1"):
        provider.transcribe(np.zeros(160, dtype=np.float32), language=invalid)
    assert calls == 0


@pytest.mark.parametrize(
    ("provider_type", "spec", "invalid"),
    (
        (DeepgramASRProvider, "deepgram:nova-3", "zz"),
        (OpenAITranscriptionProvider, "openai:gpt-4o-mini-transcribe", "zz"),
        (GroqTranscriptionProvider, "groq:whisper-large-v3", "zz"),
        (XaiTranscriptionProvider, "xai:grok-stt", "zz"),
        (XaiTranscriptionProvider, "xai:grok-stt", "zh"),
    ),
)
def test_provider_language_set_rejects_before_any_cloud_egress(
    provider_type, spec: str, invalid: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"text": "unexpected"})

    provider = provider_type(
        ASRSpec.parse(spec),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *_args: pytest.fail(
            "unsupported language must be rejected before the egress gate"
        ),
    )
    with pytest.raises(ValueError):
        provider.transcribe(np.zeros(160, dtype=np.float32), language=invalid)
    assert calls == 0


@pytest.mark.parametrize(
    ("provider_type", "spec"),
    (
        (DeepgramASRProvider, "deepgram:nova-3"),
        (OpenAITranscriptionProvider, "openai:gpt-4o-mini-transcribe"),
        (GroqTranscriptionProvider, "groq:whisper-large-v3"),
        (XaiTranscriptionProvider, "xai:grok-stt"),
    ),
)
def test_service_rejects_invalid_language_before_four_cloud_wires(provider_type, spec: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"text": "unexpected"})

    provider = provider_type(
        ASRSpec.parse(spec),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *_args: pytest.fail(
            "invalid service input must be rejected before the egress gate"
        ),
    )
    response = TestClient(create_app(ServiceEngine(asr=provider))).post(
        "/transcribe",
        json={"audio": [0.1], "language": "zz", "polish": False},
    )
    assert response.status_code == 422
    assert calls == 0


def test_voice_engine_rejects_invalid_language_before_cloud_wire(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"text": "unexpected"})

    provider = XaiTranscriptionProvider(
        ASRSpec.parse("xai:grok-stt"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *_args: pytest.fail(
            "invalid engine input must be rejected before the egress gate"
        ),
    )
    engine = VoiceEngine(
        load_config(Path("config.example.toml"), create=False),
        asr=provider,
        personalization=PersonalizationStore(tmp_path / "p.json"),
    )
    with pytest.raises(ValueError):
        engine.transcribe(np.zeros(160, dtype=np.float32), language="zz")
    assert calls == 0


def test_xai_exact_documented_language_set_accepts_fil_and_excludes_zh() -> None:
    assert "fil" in XAI_LANGUAGE_CODES
    assert "zh" not in XAI_LANGUAGE_CODES
    assert len(XAI_LANGUAGE_CODES) == 25
    wires: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wires.append(request.content)
        return httpx.Response(200, json={"text": "kumusta", "language": "fil"})

    provider = XaiTranscriptionProvider(
        ASRSpec.parse("xai:grok-stt"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=_allow_egress,
    )
    result = provider.transcribe(np.zeros(160, dtype=np.float32), language="FIL")
    assert result.language == "fil"
    assert b"\r\n\r\nfil\r\n" in wires[0]


def test_voice_engine_capabilities_describe_injected_cloud_provider(
    tmp_path: Path,
) -> None:
    provider = XaiTranscriptionProvider(
        ASRSpec.parse("xai:grok-stt"),
        api_key="test",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
        egress_logger=_allow_egress,
    )
    engine = VoiceEngine(
        load_config(Path("config.example.toml"), create=False),
        asr=provider,
        personalization=PersonalizationStore(tmp_path / "p.json"),
    )

    capabilities = engine.capabilities()
    assert capabilities["local_default"] is False
    assert capabilities["asr"]["provider"] == "xai"
    assert capabilities["asr"]["resolved"] == "xai:grok-stt"
    assert capabilities["asr"]["local"] is False
    assert capabilities["asr"]["language_hint"] == {
        "supported": True,
        "codes": sorted(XAI_LANGUAGE_CODES),
        "auto": True,
    }


@pytest.mark.parametrize("automatic", [None, "", "auto", "detect"])
def test_cloud_auto_language_omits_multipart_language(automatic: str | None) -> None:
    wires: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wires.append(request.content)
        return httpx.Response(200, json={"text": "detected", "language": "de"})

    provider = GroqTranscriptionProvider(
        ASRSpec.parse("groq:whisper-large-v3"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=_allow_egress,
    )
    provider.transcribe(np.zeros(160, dtype=np.float32), language=automatic)
    assert b'name="language"' not in wires[0]


def test_headless_voice_engine_forwards_cloud_language_per_call(tmp_path: Path) -> None:
    wires: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wires.append(request.content)
        return httpx.Response(200, json={"text": "bonjour", "language": "fr"})

    provider = OpenAITranscriptionProvider(
        ASRSpec.parse("openai:gpt-4o-mini-transcribe"),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=_allow_egress,
    )
    engine = VoiceEngine(
        load_config(Path("config.example.toml"), create=False),
        asr=provider,
        personalization=PersonalizationStore(tmp_path / "personalization.json"),
    )
    result = engine.transcribe(np.zeros(160, dtype=np.float32), language="fr", polish=False)

    assert result.language == "fr"
    assert b'name="language"' in wires[0]
    assert b"\r\n\r\nfr\r\n" in wires[0]


@pytest.mark.parametrize(
    ("provider_type", "spec"),
    (
        (XaiTranscriptionProvider, "xai:grok-stt"),
        (OpenAITranscriptionProvider, "openai:gpt-4o-mini-transcribe"),
        (GroqTranscriptionProvider, "groq:whisper-large-v3"),
    ),
)
def test_learned_terms_never_enter_cloud_hints_but_correct_local_output(
    provider_type, spec: str, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        wire = unquote(str(request.url)).encode() + request.content
        assert b"PrivateClientCodename" not in wire
        assert b"private client code name" not in wire.lower()
        # Static config vocabulary remains the intentional ASR-hint surface.
        assert b"D-Central" in wire
        return httpx.Response(
            200,
            json={"text": "private client code name", "language": "en"},
        )

    provider = provider_type(
        ASRSpec.parse(spec),
        api_key="test",
        transport=httpx.MockTransport(handler),
        egress_logger=_allow_egress,
    )
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("private client code name", "PrivateClientCodename")
    engine = VoiceEngine(
        load_config(Path("config.example.toml"), create=False),
        asr=provider,
        personalization=store,
    )

    result = engine.transcribe(np.zeros(160, dtype=np.float32), polish=False)

    assert requests
    assert result.raw == "private client code name"
    assert result.text == "PrivateClientCodename"
