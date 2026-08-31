# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
from dcent_voice.config import load_config
from dcent_voice.engine import VoiceEngine
from dcent_voice.personalization import PersonalizationStore
from dcent_voice.privacy import ConsentLedger, ConsentRequired


class _FakeASR(ASRProvider):
    locality = Locality.LOCAL

    def __init__(self, text: str = "hello d sent no I meant DCENT_Voice") -> None:
        self.text = text
        self.loaded = False
        self.calls = 0

    def load(self) -> None:
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False

    def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
        self.calls += 1
        return TranscriptResult(
            text=self.text,
            language="en",
            duration_s=len(np.asarray(audio).reshape(-1)) / float(samplerate),
            asr_latency_s=0.01,
        )


def _write_headless_config(tmp_path: Path, *, asr: str) -> tuple[Path, Path, Path]:
    consent = tmp_path / "consent.json"
    egress = tmp_path / "egress.jsonl"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''\
active_profile = "headless"

[privacy]
consent_ledger_path = "{consent.as_posix()}"
egress_log_path = "{egress.as_posix()}"

[profile.headless]
asr = "{asr}"
llm = "none"
cleanup_enabled = false
''',
        encoding="utf-8",
    )
    return config_path, consent, egress


def test_headless_cloud_engine_requires_consent_before_provider_construction(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, _consent, egress = _write_headless_config(tmp_path, asr="deepgram:nova-3")
    provider_builds = 0

    def forbidden_builder(*_args, **_kwargs):
        nonlocal provider_builds
        provider_builds += 1
        raise AssertionError("cloud provider construction must not be reached")

    monkeypatch.setenv("DEEPGRAM_API_KEY", "environment-key-must-not-bypass-consent")
    monkeypatch.setattr("dcent_voice.engine.build_asr_provider", forbidden_builder)

    with pytest.raises(ConsentRequired, match="asr:deepgram"):
        VoiceEngine.from_config(
            config_path,
            personalization=PersonalizationStore(tmp_path / "personalization.json"),
        )

    assert provider_builds == 0
    assert not egress.exists()


def test_headless_cloud_engine_logs_metadata_and_revoke_stops_existing_provider(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, consent, egress = _write_headless_config(tmp_path, asr="deepgram:nova-3")
    ledger = ConsentLedger(consent)
    ledger.grant("asr:deepgram", payload_type="audio")
    wire_calls = 0

    class LoggedCloudASR(ASRProvider):
        locality = Locality.CLOUD

        def __init__(self, spec, logger) -> None:
            self.spec = spec
            self._logger = logger

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def transcribe(self, audio, **_kwargs):
            nonlocal wire_calls
            self._logger("asr:deepgram", "audio", len(np.asarray(audio)))
            wire_calls += 1
            return TranscriptResult(
                text="private spoken transcript",
                language="en",
                duration_s=0.01,
                asr_latency_s=0.01,
            )

    def build_logged(spec, **kwargs):
        assert callable(kwargs.get("egress_logger"))
        return LoggedCloudASR(spec, kwargs["egress_logger"])

    monkeypatch.setattr("dcent_voice.engine.build_asr_provider", build_logged)
    engine = VoiceEngine.from_config(
        config_path,
        personalization=PersonalizationStore(tmp_path / "personalization.json"),
    )

    result = engine.transcribe(np.zeros(160, dtype=np.float32), polish=False)
    assert result.raw == "private spoken transcript"
    assert wire_calls == 1
    line = egress.read_text(encoding="utf-8").strip()
    metadata = json.loads(line)
    assert set(metadata) == {"timestamp", "provider_key", "payload_type", "byte_count"}
    assert metadata["provider_key"] == "asr:deepgram"
    assert metadata["payload_type"] == "audio"
    assert "private spoken transcript" not in line

    ledger.revoke("asr:deepgram")
    with pytest.raises(ConsentRequired, match="asr:deepgram"):
        engine.transcribe(np.zeros(160, dtype=np.float32), polish=False)
    assert wire_calls == 1
    assert egress.read_text(encoding="utf-8").splitlines() == [line]


def test_main_cloud_asr_override_cannot_bypass_consent(tmp_path: Path, monkeypatch, capsys) -> None:
    from dcent_voice.app import main

    config_path, _consent, egress = _write_headless_config(
        tmp_path, asr="faster-whisper:tiny.en:cpu-int8"
    )
    store = PersonalizationStore(tmp_path / "personalization.json")
    provider_builds = 0

    def forbidden_builder(*_args, **_kwargs):
        nonlocal provider_builds
        provider_builds += 1
        raise AssertionError("cloud provider construction must not be reached")

    monkeypatch.setenv("DEEPGRAM_API_KEY", "environment-key-must-not-bypass-consent")
    monkeypatch.setattr("dcent_voice.engine.build_asr_provider", forbidden_builder)
    monkeypatch.setattr("dcent_voice.app.PersonalizationStore", lambda **_kwargs: store)

    rc = main(
        [
            "--config",
            str(config_path),
            "transcribe",
            str(tmp_path / "not-read.wav"),
            "--asr",
            "deepgram:nova-3",
        ]
    )

    assert rc == 1
    assert provider_builds == 0
    assert not egress.exists()
    assert "ConsentRequired" in capsys.readouterr().err


def test_engine_transcribes_without_tray(tmp_path: Path) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    store = PersonalizationStore(tmp_path / "p.json")
    engine = VoiceEngine(config, asr=_FakeASR(), personalization=store)
    result = engine.transcribe(np.zeros(1600, dtype=np.float32), samplerate=16000)
    assert "DCENT_Voice" in result.text
    assert result.timings["asr"] >= 0
    assert "postprocess" in result.timings
    assert store.as_vocab(style="plain")
    caps = engine.capabilities()
    assert caps["headless"] is True
    assert "oneshot" in caps["modes"]


def test_engine_cancel_returns_rejected() -> None:
    config = load_config(Path("config.example.toml"), create=False)
    engine = VoiceEngine(config, asr=_FakeASR())
    engine.cancel()
    result = engine.transcribe(np.zeros(16, dtype=np.float32))
    assert result.rejected_reason == "cancelled"


def test_stream_session_coalesces_redundant_partials_but_never_final(
    tmp_path: Path,
) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    asr = _FakeASR("hello world")
    engine = VoiceEngine(
        config,
        asr=asr,
        personalization=PersonalizationStore(tmp_path / "p.json"),
    )
    session = engine.open_stream(partial_interval_s=0.5)
    quarter_second = np.full(4_000, 0.2, dtype=np.float32)

    first = session.push(quarter_second)
    repeated = session.push(quarter_second)
    refreshed = session.push(quarter_second)
    final = session.push(quarter_second, final=True)

    assert first.type == "partial"
    assert repeated.type == "partial"
    assert repeated.raw == first.raw
    assert refreshed.type == "partial"
    assert final.type == "final"
    assert final.text == "Hello world."
    assert asr.calls == 3
    assert session._buffer.size == 0
    assert session._last_partial_samples == 0


def test_transcribe_cli_can_write_frozen_safe_json_with_load_timing(
    tmp_path: Path, monkeypatch
) -> None:
    from dcent_voice.app import build_parser, run_transcribe_command

    config = load_config(Path("config.example.toml"), create=False)
    output = tmp_path / "result.json"
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture")
    events: list[str] = []
    result = SimpleNamespace(
        rejected_reason="",
        text="hello",
        to_dict=lambda: {
            "text": "hello",
            "timings": {"asr": 0.1, "postprocess": 0.01},
            "rejected_reason": "",
        },
    )

    class FakeEngine:
        def __init__(self, _config, *, polish: bool, asr=None, personalization=None) -> None:
            assert polish is True

        def load(self) -> None:
            events.append("load")

        def transcribe(
            self,
            samples,
            *,
            samplerate: int,
            language: str | None,
            prose_context: bool | None,
            style: str | None = None,
        ):
            assert samples == [0.1]
            assert samplerate == 16000
            assert language is None
            assert prose_context is None
            assert style is None
            events.append("transcribe")
            return result

        def unload(self) -> None:
            events.append("unload")

    monkeypatch.setattr("dcent_voice.engine.VoiceEngine", FakeEngine)
    monkeypatch.setattr("dcent_voice.engine.load_wav_mono", lambda _path: ([0.1], 16000))
    args = build_parser().parse_args(["transcribe", str(audio), "--output-json", str(output)])

    assert run_transcribe_command(config, args) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert events == ["load", "transcribe", "unload"]
    assert payload["text"] == "hello"
    assert payload["cli_measurement"]["scope"] == "headless_transcribe_process"
    assert payload["cli_measurement"]["model_load_s"] >= 0
    assert payload["cli_measurement"]["transcribe_s"] >= 0


def test_transcribe_cli_exposes_explicit_prose_context_control() -> None:
    from dcent_voice.app import build_parser

    parser = build_parser()
    trusted = parser.parse_args(["transcribe", "speech.wav", "--prose-context"])
    conservative = parser.parse_args(["transcribe", "speech.wav", "--no-prose-context"])
    default = parser.parse_args(["transcribe", "speech.wav"])

    assert trusted.prose_context is True
    assert conservative.prose_context is False
    assert default.prose_context is None


def test_compose_cli_uses_shipped_writer(capsys) -> None:
    from dcent_voice.app import main

    spoken = [
        "hey",
        "can",
        "you",
        "send",
        "the",
        "deck",
        "to",
        "alice",
        "actually",
        "bob",
        "thanks",
    ]
    code = main(["compose", "--style", "email", *spoken])
    captured = capsys.readouterr()
    assert code == 0
    assert "Could you" in captured.out
    assert "Bob" in captured.out
    assert "alice" not in captured.out.lower()


def test_compose_cli_high_drops_stacked_hedges(capsys) -> None:
    from dcent_voice.app import main

    code = main(
        [
            "compose",
            "--cleanup-level",
            "high",
            "I",
            "guess",
            "I",
            "think",
            "we",
            "should",
            "ship",
            "Monday",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    out = captured.out.lower()
    assert "i guess" not in out
    assert "i think" not in out
    assert "ship monday" in out


def test_compose_cli_high_drops_uncommaed_mid_clause(capsys) -> None:
    from dcent_voice.app import main

    code = main(
        [
            "compose",
            "--cleanup-level",
            "high",
            "We",
            "should",
            "I",
            "think",
            "ship",
            "Monday",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    out = captured.out.lower()
    assert "i think" not in out
    assert "ship monday" in out


def test_engine_default_path_rewrites_and_styles() -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    spoken = "so um hey can you send the deck to alice actually bob thanks"
    engine = VoiceEngine(config, asr=_FakeASR(spoken))
    plain = engine.transcribe(np.zeros(1600, dtype=np.float32), samplerate=16000)
    email = engine.transcribe(np.zeros(1600, dtype=np.float32), samplerate=16000, style="email")
    assert "alice" not in plain.text.lower()
    assert "bob" in plain.text.lower()
    assert "um" not in plain.text.lower()
    assert email.text != plain.text
    assert "\n\n" in email.text
    assert "bob" in email.text.lower()


def test_engine_applies_learned_variants_only_in_declared_app_context(
    tmp_path: Path,
) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    store = PersonalizationStore(tmp_path / "p.json")
    for _ in range(2):
        store.record_correction("d central", "D-Central", style="code", app="code.exe")
    engine = VoiceEngine(
        config,
        asr=_FakeASR("d-central"),
        personalization=store,
    )

    code = engine.transcribe(
        np.zeros(1600, dtype=np.float32),
        style="code",
        app_context="Code.exe",
        polish=False,
    )
    email = engine.transcribe(
        np.zeros(1600, dtype=np.float32),
        style="email",
        app_context="outlook.exe",
        polish=False,
    )

    assert "D-Central" in code.text
    assert "d-central" in email.text
    assert store.last_utterance() == {"raw": "", "cleaned": ""}


def test_scoped_personalization_overrides_shipped_global_dictionary(tmp_path: Path) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    store = PersonalizationStore(tmp_path / "p.json")
    store.record_correction("d central", "DCENT", style="code", app="code.exe")
    engine = VoiceEngine(
        config,
        asr=_FakeASR("d central"),
        personalization=store,
    )

    result = engine.transcribe(
        np.zeros(1600, dtype=np.float32),
        style="code",
        app_context="code.exe",
        polish=False,
    )

    assert result.text == "DCENT"


def test_engine_uses_learned_destination_style_without_explicit_style(
    tmp_path: Path,
) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    store = PersonalizationStore(tmp_path / "p.json")
    store.remember_app_style("notepad.exe", "email", immediate=True)
    engine = VoiceEngine(
        config,
        asr=_FakeASR("tell alex the invoice is ready"),
        personalization=store,
    )

    notepad = engine.transcribe(
        np.zeros(1600, dtype=np.float32),
        app_context="notepad.exe",
        polish=False,
    )
    browser = engine.transcribe(
        np.zeros(1600, dtype=np.float32),
        app_context="msedge.exe",
        polish=False,
    )

    assert notepad.text.startswith("Hi Alex,")
    assert notepad.text.rstrip().endswith("Thanks,")
    assert not browser.text.startswith("Hi Alex,")
    snap = store.snapshot()
    assert snap["stores_audio"] is False
    assert snap["app_styles"][0]["app"] == "notepad.exe"


def test_engine_requires_explicit_prose_context_for_longer_learning(
    tmp_path: Path,
) -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "p.json")
    store.record_correction("d central", "D-Central")
    engine = VoiceEngine(
        config,
        asr=_FakeASR("Open d central settings."),
        personalization=store,
    )

    conservative = engine.transcribe(
        np.zeros(1600, dtype=np.float32),
        style="plain",
        polish=False,
    )
    trusted = engine.transcribe(
        np.zeros(1600, dtype=np.float32),
        style="plain",
        polish=False,
        prose_context=True,
    )

    assert conservative.text == "Open d central settings."
    assert trusted.text == "Open D-Central settings."


def test_engine_stream_propagates_only_explicit_trusted_prose_context(
    tmp_path: Path,
) -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "stream-prose.json")
    store.record_correction("d central", "D-Central")
    engine = VoiceEngine(
        config,
        asr=_FakeASR("Open d central settings."),
        personalization=store,
    )
    audio = np.full(1600, 0.1, dtype=np.float32)

    omitted = engine.open_stream(style="plain", polish=False).push(audio, final=True)
    refused = engine.open_stream(style="plain", polish=False, prose_context=False).push(
        audio, final=True
    )
    trusted = engine.open_stream(style="plain", polish=False, prose_context=True).push(
        audio, final=True
    )

    assert omitted.text == "Open d central settings."
    assert refused.text == "Open d central settings."
    assert trusted.text == "Open D-Central settings."


@pytest.mark.parametrize("invalid", ["false", "true", 1, 0, [], {}])
def test_engine_rejects_non_boolean_prose_context_before_asr(
    tmp_path: Path, invalid: object
) -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    asr = _FakeASR("Open d central settings.")
    engine = VoiceEngine(
        config,
        asr=asr,
        personalization=PersonalizationStore(tmp_path / "strict-engine.json"),
    )

    with pytest.raises(TypeError, match="boolean or None"):
        engine.transcribe(
            np.zeros(1600, dtype=np.float32),
            prose_context=invalid,  # type: ignore[arg-type]
        )

    assert asr.calls == 0


@pytest.mark.parametrize("field", ["enabled", "learn", "prose_context"])
def test_engine_rejects_malformed_personalization_policy_before_asr(
    tmp_path: Path,
    field: str,
) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    malformed_policy = replace(config.personalization)
    object.__setattr__(malformed_policy, field, "false")
    malformed_config = replace(config, personalization=malformed_policy)
    asr = _FakeASR()
    store = PersonalizationStore(tmp_path / f"injected-{field}.json")

    with pytest.raises(TypeError, match=rf"configured personalization.{field} must be a boolean"):
        VoiceEngine(malformed_config, asr=asr, personalization=store)

    assert asr.calls == 0


def test_engine_config_governs_injected_store_apply_and_learning(tmp_path: Path) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    disabled_policy = replace(config.personalization, enabled=False, learn=False)
    config = replace(config, personalization=disabled_policy, dictionary=())
    path = tmp_path / "injected-disabled.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "PRIVATE")
    store.note_utterance("private client", "private client")
    before = path.read_bytes()
    asr = _FakeASR("d central")

    engine = VoiceEngine(config, asr=asr, personalization=store)
    result = engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    learned = engine.learn("private client", "SecretName")
    learned_last = engine.learn_last("SecretName")

    assert result.text == "d central"
    assert learned["ok"] is False
    assert learned_last["ok"] is False
    assert learned["term_count"] == 1
    assert (store.enabled, store.learn) == (True, True)
    assert path.read_bytes() == before
    assert asr.calls == 1


def test_engine_revalidates_config_without_mutating_injected_store(tmp_path: Path) -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "live-engine-policy.json")
    store.record_correction("d central", "D-Central")
    engine = VoiceEngine(config, asr=_FakeASR("d central"), personalization=store)
    engine.config = replace(
        config,
        personalization=replace(config.personalization, enabled=False, learn=False),
    )

    result = engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)

    assert result.text == "d central"
    assert (store.enabled, store.learn) == (True, True)


@pytest.mark.parametrize(
    ("enabled", "learn", "applies", "records"),
    [
        (False, False, False, False),
        (False, True, False, False),
        (True, False, True, False),
        (True, True, True, True),
    ],
)
def test_engine_call_policy_covers_all_combinations_without_store_mutation(
    tmp_path: Path, enabled: bool, learn: bool, applies: bool, records: bool
) -> None:
    base = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    config = replace(
        base,
        personalization=replace(base.personalization, enabled=enabled, learn=learn),
    )
    path = tmp_path / f"policy-{enabled}-{learn}.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "D-Central")
    store.update_policy(enabled=False, learn=False)
    store.save()
    engine = VoiceEngine(config, asr=_FakeASR("d central"), personalization=store)

    result = engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    learned = engine.learn("private client", "SecretName")

    assert (result.text == "D-Central") is applies
    assert learned["ok"] is records
    assert (store.enabled, store.learn) == (False, False)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["enabled"] is False
    assert payload["learn"] is False


def test_two_engines_with_opposite_policy_are_deterministic_during_asr(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingASR(_FakeASR):
        def transcribe(self, *args, **kwargs):
            entered.set()
            assert release.wait(2.0)
            return super().transcribe(*args, **kwargs)

    base = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    enabled_config = replace(
        base,
        personalization=replace(base.personalization, enabled=True, learn=True),
    )
    disabled_config = replace(
        base,
        personalization=replace(base.personalization, enabled=False, learn=False),
    )
    path = tmp_path / "shared-engine-policy.json"
    store = PersonalizationStore(path)
    store.record_correction("d central", "D-Central")
    store.update_policy(enabled=True, learn=False)
    store.save()
    enabled_engine = VoiceEngine(
        enabled_config, asr=BlockingASR("d central"), personalization=store
    )
    disabled_engine = VoiceEngine(disabled_config, asr=_FakeASR("d central"), personalization=store)
    enabled_results: list[str] = []
    worker = threading.Thread(
        target=lambda: enabled_results.append(
            enabled_engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False).text
        )
    )
    worker.start()
    assert entered.wait(1.0)

    disabled_result = disabled_engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    store.save()
    release.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert disabled_result.text == "d central"
    assert enabled_results == ["D-Central"]
    assert (store.enabled, store.learn) == (True, False)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert (payload["enabled"], payload["learn"]) == (True, False)


def test_opposite_engine_learning_policy_is_concurrent_and_isolated(
    tmp_path: Path,
) -> None:
    base = load_config(Path("config.example.toml"), create=False)
    learning_config = replace(
        base,
        personalization=replace(base.personalization, enabled=True, learn=True),
    )
    blocked_config = replace(
        base,
        personalization=replace(base.personalization, enabled=True, learn=False),
    )
    path = tmp_path / "concurrent-learning.json"
    store = PersonalizationStore(path, enabled=False, learn=False)
    store.save()
    learning_engine = VoiceEngine(learning_config, personalization=store)
    blocked_engine = VoiceEngine(blocked_config, personalization=store)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []

    def learn(engine: VoiceEngine, spoken: str, written: str) -> None:
        barrier.wait()
        outcomes.append(engine.learn(spoken, written)["ok"])

    threads = [
        threading.Thread(target=learn, args=(learning_engine, "private client", "SecretName")),
        threading.Thread(target=learn, args=(blocked_engine, "other client", "OtherName")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == [False, True]
    assert store.snapshot()["term_count"] == 1
    assert (store.enabled, store.learn) == (False, False)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert (payload["enabled"], payload["learn"]) == (False, False)


def test_learn_last_uses_engine_local_utterance_not_shared_store_slot(
    tmp_path: Path,
) -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "engine-provenance.json")
    engine_a = VoiceEngine(config, asr=_FakeASR("project alpha"), personalization=store)
    engine_b = VoiceEngine(config, asr=_FakeASR("project beta"), personalization=store)

    engine_a.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    engine_b.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    learned = engine_a.learn_last("Project AlphaCorrect")
    repeated = engine_a.learn_last("Project WrongRepeat")

    assert learned["ok"] is True
    assert learned["spoken"] == "project alpha"
    assert repeated["ok"] is False
    terms = store.snapshot()["terms"]
    assert any(term["spoken"] == "project alpha" for term in terms)
    assert all(term["spoken"] != "project beta" for term in terms)
    assert store.last_utterance() == {"raw": "", "cleaned": ""}


def test_blocked_spoken_last_correction_cannot_use_other_engine_utterance(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class CorrectionASR(_FakeASR):
        def __init__(self) -> None:
            super().__init__("project alpha")
            self.sequence = 0

        def transcribe(self, *args, **kwargs):
            self.sequence += 1
            if self.sequence == 1:
                self.text = "project alpha"
            else:
                entered.set()
                assert release.wait(2.0)
                self.text = "correct that to Project AlphaCorrect"
            return super().transcribe(*args, **kwargs)

    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "blocked-provenance.json")
    engine_a = VoiceEngine(config, asr=CorrectionASR(), personalization=store)
    engine_b = VoiceEngine(config, asr=_FakeASR("project beta"), personalization=store)
    engine_a.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    corrected: list[str] = []
    worker = threading.Thread(
        target=lambda: corrected.append(
            engine_a.transcribe(np.zeros(1600, dtype=np.float32), polish=False).text
        )
    )
    worker.start()
    assert entered.wait(1.0)

    engine_b.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    release.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert corrected == ["Project AlphaCorrect"]
    spoken_last = [term for term in store.snapshot()["terms"] if term["source"] == "spoken_last"]
    assert [term["spoken"] for term in spoken_last] == ["project alpha"]


def test_same_engine_concurrent_transcribes_keep_newest_generation_for_learning(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class OrderedASR(_FakeASR):
        def __init__(self) -> None:
            super().__init__("base phrase")
            self.sequence = 0
            self.sequence_lock = threading.Lock()

        def transcribe(self, *args, **kwargs):
            with self.sequence_lock:
                self.sequence += 1
                sequence = self.sequence
            if sequence == 1:
                self.text = "base phrase"
            elif sequence == 2:
                entered.set()
                assert release.wait(2.0)
                self.text = "older phrase"
            else:
                self.text = "newer phrase"
            return super().transcribe(*args, **kwargs)

    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "same-engine-order.json")
    engine = VoiceEngine(config, asr=OrderedASR(), personalization=store)
    engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    older = threading.Thread(
        target=lambda: engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    )
    older.start()
    assert entered.wait(1.0)

    newer = engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    release.set()
    older.join(2.0)
    learned = engine.learn_last("Newer Correct")

    assert not older.is_alive()
    assert newer.text == "newer phrase"
    assert learned["ok"] is True
    assert learned["spoken"] == "newer phrase"


def test_empty_success_does_not_erase_prior_engine_correction_source(
    tmp_path: Path,
) -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    asr = _FakeASR("project alpha")
    store = PersonalizationStore(tmp_path / "empty-provenance.json")
    engine = VoiceEngine(config, asr=asr, personalization=store)

    assert engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False).text
    asr.text = "   "
    empty = engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    learned = engine.learn_last("Project AlphaCorrect")

    assert not empty.text.strip()
    assert learned["ok"] is True
    assert learned["spoken"] == "project alpha"


def test_local_asr_hints_include_learned_terms_cloud_does_not(
    tmp_path: Path,
) -> None:
    from dcent_voice.asr.base import Locality

    class _CapturingASR(_FakeASR):
        def __init__(self, text: str, locality: Locality) -> None:
            super().__init__(text)
            self.locality = locality
            self.hotwords: str | None = None
            self.initial_prompt: str | None = None

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            self.hotwords = hotwords
            self.initial_prompt = initial_prompt
            return super().transcribe(
                audio,
                samplerate=samplerate,
                initial_prompt=initial_prompt,
                hotwords=hotwords,
            )

    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "hints.json")
    store.record_correction("private client code name", "PrivateClientCodename")

    local = _CapturingASR("hello", Locality.LOCAL)
    VoiceEngine(config, asr=local, personalization=store).transcribe(
        np.zeros(1600, dtype=np.float32), polish=False
    )
    blob = f"{local.hotwords or ''} {local.initial_prompt or ''}"
    assert "PrivateClientCodename" in blob
    assert "Lightning Network" in blob

    cloud = _CapturingASR("hello", Locality.CLOUD)
    VoiceEngine(config, asr=cloud, personalization=store).transcribe(
        np.zeros(1600, dtype=np.float32), polish=False
    )
    cloud_blob = f"{cloud.hotwords or ''} {cloud.initial_prompt or ''}"
    assert "PrivateClientCodename" not in cloud_blob
    assert "Lightning Network" not in cloud_blob


def test_engine_save_failure_releases_claim_for_honest_retry(tmp_path: Path, monkeypatch) -> None:
    config = replace(load_config(Path("config.example.toml"), create=False), dictionary=())
    store = PersonalizationStore(tmp_path / "engine-retry.json")
    engine = VoiceEngine(config, asr=_FakeASR("project alpha"), personalization=store)
    engine.transcribe(np.zeros(1600, dtype=np.float32), polish=False)
    before = store.snapshot()
    original_replace = Path.replace

    def fail_replace(self: Path, target: Path) -> Path:
        del self, target
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        engine.learn_last("Project AlphaCorrect")
    assert store.snapshot() == before

    monkeypatch.setattr(Path, "replace", original_replace)
    retried = engine.learn_last("Project AlphaCorrect")
    assert retried["ok"] is True
    assert retried["spoken"] == "project alpha"
