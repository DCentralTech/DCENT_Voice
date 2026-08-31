# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dcent_voice.asr.base import PARAKEET_V3_LANGUAGE_CODES
from dcent_voice.asr.factory import build_asr_provider, describe_asr
from dcent_voice.asr.language import resolve_language_policy
from dcent_voice.asr.model_registry import ModelUnavailableError
from dcent_voice.asr.parakeet_provider import (
    ParakeetASRProvider,
    parakeet_available,
    resolve_parakeet_model_name,
)
from dcent_voice.config import ASRSpec, load_config
from dcent_voice.service.api import ServiceEngine, create_app


def test_factory_builds_parakeet_provider() -> None:
    provider = build_asr_provider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), language="en")
    assert isinstance(provider, ParakeetASRProvider)
    assert provider.locality.value == "local"
    assert provider.model_name == "nemo-parakeet-tdt-0.6b-v3"


def test_example_config_ships_parakeet_desktop_default() -> None:
    config = load_config(Path("config.example.toml"), create=False)
    assert config.current_profile.asr.provider == "parakeet"
    assert config.current_profile.asr.model == "tdt-0.6b-v3"
    assert config.profiles["tiny"].asr.provider == "faster-whisper"


def test_parakeet_aliases_and_availability() -> None:
    assert resolve_parakeet_model_name("v3") == "nemo-parakeet-tdt-0.6b-v3"
    assert resolve_parakeet_model_name("v2") == "nemo-parakeet-tdt-0.6b-v2"
    assert parakeet_available() is True or parakeet_available() is False


def test_describe_asr_keeps_multilingual_parakeet_and_discloses_hint_semantics() -> None:
    spec = ASRSpec.parse("parakeet:tdt-0.6b-v3:int8")
    info = describe_asr(spec, resolve_language_policy("multilingual", "auto"))
    assert info["requested"] == spec.raw
    assert info["provider"] == "parakeet"
    assert info["model"] == "tdt-0.6b-v3"
    assert info["local"] is True
    assert info["language_hint"] == {
        "supported": True,
        "codes": [
            "bg",
            "cs",
            "da",
            "de",
            "el",
            "en",
            "es",
            "et",
            "fi",
            "fr",
            "hr",
            "hu",
            "it",
            "lt",
            "lv",
            "mt",
            "nl",
            "pl",
            "pt",
            "ro",
            "ru",
            "sk",
            "sl",
            "sv",
            "uk",
        ],
        "auto": True,
        "effect": "metadata_only",
        "reports_detected_language": False,
    }


def test_parakeet_accepts_documented_languages_and_rejects_unsupported_before_load() -> None:
    provider = ParakeetASRProvider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), language="en")
    client = TestClient(create_app(ServiceEngine(asr=provider)))

    response = client.post(
        "/transcribe",
        json={"audio": [0.1], "language": "ja", "polish": False},
    )

    assert response.status_code == 422
    assert "supports only:" in response.json()["detail"]
    assert provider._model is None
    assert provider.validate_language_hint("fr") == "fr"
    assert provider.validate_language_hint("auto") == ""
    with pytest.raises(ValueError, match="supports only:"):
        provider.transcribe([0.1], language="ja")
    assert provider._model is None


def test_parakeet_capability_lists_exact_upstream_languages_and_auto() -> None:
    provider = ParakeetASRProvider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), language="en")
    capability = ServiceEngine(asr=provider).capabilities()["language_hint"]
    assert capability["supported"] is True
    assert capability["auto"] is True
    assert set(capability["codes"]) == PARAKEET_V3_LANGUAGE_CODES


def test_factory_falls_back_when_parakeet_missing(monkeypatch) -> None:
    import dcent_voice.asr.parakeet_provider as parakeet_mod
    from dcent_voice.asr.faster_whisper_provider import FasterWhisperASRProvider

    monkeypatch.setattr(parakeet_mod, "parakeet_available", lambda: False)
    provider = build_asr_provider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"))
    assert isinstance(provider, FasterWhisperASRProvider)
    assert provider.spec.model == "base"

    multilingual = build_asr_provider(
        ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"),
        language="fr",
        language_mode="multilingual",
    )
    assert isinstance(multilingual, FasterWhisperASRProvider)
    assert multilingual.spec.model == "base"
    assert multilingual.language == "fr"


@pytest.mark.parametrize(
    ("mode", "language", "configured"),
    (
        ("multilingual", "fr", "fr"),
        ("multilingual", "auto", "auto"),
        ("auto", "en", "auto"),
        ("auto", "auto", "auto"),
    ),
)
def test_installed_parakeet_handles_supported_multilingual_policy(
    monkeypatch, mode: str, language: str, configured: str
) -> None:
    import dcent_voice.asr.parakeet_provider as parakeet_mod

    availability_calls = 0

    def available() -> bool:
        nonlocal availability_calls
        availability_calls += 1
        return True

    monkeypatch.setattr(parakeet_mod, "parakeet_available", available)
    provider = build_asr_provider(
        ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"),
        language=language,
        language_mode=mode,
    )

    assert isinstance(provider, ParakeetASRProvider)
    assert provider.spec.raw == "parakeet:tdt-0.6b-v3:int8"
    assert provider.language == configured
    assert availability_calls == 1


def test_installed_parakeet_remains_shipped_english_default(monkeypatch) -> None:
    import dcent_voice.asr.parakeet_provider as parakeet_mod

    monkeypatch.setattr(parakeet_mod, "parakeet_available", lambda: True)
    provider = build_asr_provider(
        ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"),
        language="en",
        language_mode="english",
    )
    assert isinstance(provider, ParakeetASRProvider)


@pytest.mark.parametrize("language", ["fr", "auto"])
def test_factory_routes_language_without_separate_mode_selection(
    monkeypatch, language: str
) -> None:
    import dcent_voice.asr.parakeet_provider as parakeet_mod

    monkeypatch.setattr(parakeet_mod, "parakeet_available", lambda: True)
    provider = build_asr_provider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), language=language)
    assert isinstance(provider, ParakeetASRProvider)
    assert provider.spec.model == "tdt-0.6b-v3"


def test_factory_routes_language_outside_parakeet_set_to_pinned_whisper() -> None:
    from dcent_voice.asr.faster_whisper_provider import FasterWhisperASRProvider

    provider = build_asr_provider(
        ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"),
        language="ja",
        language_mode="multilingual",
    )
    assert isinstance(provider, FasterWhisperASRProvider)
    assert provider.spec.raw == "faster-whisper:base:cpu-int8"
    assert provider.language == "ja"


def test_looks_like_parakeet_dir_and_env_resolver(tmp_path, monkeypatch) -> None:
    import dcent_voice.asr.parakeet_provider as parakeet_mod
    from dcent_voice.asr.parakeet_provider import (
        looks_like_parakeet_dir,
        resolve_parakeet_model_dir,
        stage_parakeet_bundle,
    )

    empty = tmp_path / "empty"
    empty.mkdir()
    assert looks_like_parakeet_dir(empty) is False
    fake = tmp_path / "weights"
    fake.mkdir()
    (fake / "encoder-model.int8.onnx").write_bytes(b"weights")
    (fake / "vocab.txt").write_text("a", encoding="utf-8")
    assert looks_like_parakeet_dir(fake) is False

    names = {
        "config.json",
        "decoder_joint-model.int8.onnx",
        "encoder-model.int8.onnx",
        "vocab.txt",
    }
    exact = tmp_path / "exact"
    exact.mkdir()
    for name in names:
        (exact / name).write_bytes(name.encode())

    def small_verify(path: Path, _model_id: str) -> tuple[bool, str]:
        actual = {entry.name for entry in path.iterdir()} if path.is_dir() else set()
        valid = actual == names and all(
            not entry.is_symlink() and entry.is_file() for entry in path.iterdir()
        )
        return valid, "test snapshot"

    monkeypatch.setattr(parakeet_mod, "verify_pinned_snapshot", small_verify)

    def small_stage(source: Path, destination: Path, _model_id: str) -> Path:
        destination.mkdir(parents=True)
        for name in names:
            (destination / name).write_bytes((source / name).read_bytes())
        return destination

    monkeypatch.setattr(parakeet_mod, "stage_verified_snapshot", small_stage)
    with pytest.raises(FileNotFoundError, match="Verified Parakeet"):
        stage_parakeet_bundle(tmp_path / "payload", source=fake)
    dest = stage_parakeet_bundle(tmp_path / "payload", source=exact)
    assert looks_like_parakeet_dir(dest) is True
    monkeypatch.setenv("DCENT_VOICE_PARAKEET_DIR", str(dest))
    assert resolve_parakeet_model_dir() == dest
    env_dest = stage_parakeet_bundle(tmp_path / "env-payload")
    assert looks_like_parakeet_dir(env_dest) is True
    assert (env_dest / "encoder-model.int8.onnx").read_bytes() == b"encoder-model.int8.onnx"


def test_pad_parakeet_audio_extends_short_clips() -> None:
    import numpy as np

    from dcent_voice.asr.parakeet_provider import pad_parakeet_audio

    short = np.ones(16000, dtype=np.float32)  # 1 s
    padded = pad_parakeet_audio(short, 16000)
    assert len(padded) == 48000
    lead = 3200
    assert float(padded[:lead].max()) == 0.0
    assert float(padded[lead : lead + 16000].min()) == 1.0
    assert float(padded[lead + 16000 :].max()) == 0.0
    long = np.ones(64000, dtype=np.float32)
    assert len(pad_parakeet_audio(long, 16000)) == 64000


def test_parakeet_spec_is_local_not_cloud() -> None:
    spec = ASRSpec.parse("parakeet:tdt-0.6b-v3:int8")
    assert spec.locality.value == "local"


def test_missing_parakeet_falls_back_to_pinned_local_whisper_not_online_alias(
    monkeypatch, tmp_path
) -> None:
    import dcent_voice.asr.faster_whisper_provider as whisper_mod
    import dcent_voice.asr.parakeet_provider as parakeet_mod

    calls: list[dict[str, object]] = []
    monkeypatch.setitem(
        sys.modules,
        "onnx_asr",
        SimpleNamespace(load_model=lambda *_args, **kwargs: calls.append(kwargs)),
    )
    monkeypatch.setattr(
        parakeet_mod,
        "require_parakeet_model_dir",
        lambda **_kwargs: (_ for _ in ()).throw(ModelUnavailableError("offline model missing")),
    )

    class FakeFallback:
        def __init__(self, spec, *, language):
            self.spec = spec
            self.language = language
            self.loaded = False

        def load(self):
            self.loaded = True

        def unload(self):
            self.loaded = False

    monkeypatch.setattr(whisper_mod, "FasterWhisperASRProvider", FakeFallback)
    provider = ParakeetASRProvider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), language="en")
    provider.load()
    assert calls == []
    assert provider._fallback is not None
    assert provider._fallback.spec.raw == "faster-whisper:base:cpu-int8"


def test_parakeet_load_always_passes_verified_local_path(monkeypatch, tmp_path) -> None:
    import dcent_voice.asr.parakeet_provider as parakeet_mod

    local = tmp_path / "verified"
    local.mkdir()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    model = object()

    def load_model(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return model

    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=load_model))
    monkeypatch.setattr(parakeet_mod, "require_parakeet_model_dir", lambda **_kwargs: local)
    monkeypatch.setattr(parakeet_mod, "verified_snapshot_lock", lambda path, _id: nullcontext(path))
    provider = ParakeetASRProvider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), language="en")
    provider.load()
    assert provider._model is model
    assert calls[0][1]["path"] == local
    options = calls[0][1]["sess_options"]
    assert options.get_session_config_entry("session.disable_prepacking") == "1"
    assert options.intra_op_num_threads == min(4, max(1, os.cpu_count() or 1))
    assert options.inter_op_num_threads == 1


def test_clean_parakeet_resolution_is_offline_and_actionable(monkeypatch, tmp_path) -> None:
    import socket

    import dcent_voice.asr.parakeet_provider as parakeet_mod

    calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(parakeet_mod, "model_root", lambda: tmp_path / "registry")
    monkeypatch.setattr(parakeet_mod, "bundled_model_root", lambda: tmp_path / "bundle")
    monkeypatch.setattr(parakeet_mod, "application_root", lambda: tmp_path / "app")
    monkeypatch.setattr(parakeet_mod, "pinned_huggingface_snapshot", lambda _id: None)
    monkeypatch.delenv("DCENT_VOICE_PARAKEET_DIR", raising=False)
    assert parakeet_mod.resolve_parakeet_model_dir() is None
    with pytest.raises(ModelUnavailableError, match="never downloads.*during dictation"):
        parakeet_mod.require_parakeet_model_dir()
    assert calls == 0


def test_parakeet_description_reports_actual_verified_readiness(monkeypatch) -> None:
    import dcent_voice.asr.parakeet_provider as parakeet_mod

    readiness = {
        "ready": False,
        "model_id": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "revision": "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
        "path": None,
        "detail": "offline model missing",
    }
    monkeypatch.setattr(parakeet_mod, "parakeet_model_status", lambda: readiness)
    info = describe_asr(
        ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"),
        resolve_language_policy("english", "en"),
    )
    assert info["model_readiness"] == readiness


def test_parakeet_explicit_language_is_metadata_and_auto_is_not_relabelled() -> None:
    class FakeModel:
        def recognize(self, *_args, **_kwargs):
            return "Je m'appelle"

    provider = ParakeetASRProvider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), language="fr")
    provider._model = FakeModel()
    explicit = provider.transcribe([0.1] * 16000, language="fr")
    automatic = provider.transcribe([0.1] * 16000, language="auto")
    assert explicit.text == "Je m'appelle"
    assert explicit.language == "fr"
    assert automatic.text == "Je m'appelle"
    assert automatic.language == ""
