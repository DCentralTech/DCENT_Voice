# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import bench_e2e


def test_benchmark_requires_an_explicit_measurement_scope() -> None:
    with pytest.raises(SystemExit):
        bench_e2e.build_parser().parse_args([])

    warm = bench_e2e.build_parser().parse_args(["--source-warm"])
    assert warm.source_warm is True
    assert warm.executable is None

    output = Path("result.json")
    parsed = bench_e2e.build_parser().parse_args(["--source-warm", "--output-json", str(output)])
    assert parsed.output_json == output


def test_frozen_cold_launches_fresh_processes_and_reports_scope(
    tmp_path: Path, monkeypatch
) -> None:
    payload = tmp_path / "DCENT_Voice"
    internal = payload / "_internal"
    internal.mkdir(parents=True)
    executable = payload / "dcent-voice.exe"
    executable.write_bytes(b"MZ")
    (internal / "config.example.toml").write_text("active_profile='desktop'\n", encoding="utf-8")
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF-real-speech-fixture")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs):
        commands.append(command)
        output = Path(command[command.index("--output-json") + 1])
        output.write_text(
            json.dumps(
                {
                    "text": "hello DCENT",
                    "provider": "parakeet",
                    "model": "tdt-0.6b-v3",
                    "rejected_reason": "",
                    "asr_latency_s": 0.5,
                    "timings": {"asr": 0.51, "postprocess": 0.02},
                    "cli_measurement": {
                        "frozen": True,
                        "audio_load_s": 0.01,
                        "model_load_s": 0.4,
                        "transcribe_s": 0.54,
                        "unload_s": 0.03,
                    },
                }
            ),
            encoding="utf-8",
        )
        assert kwargs["cwd"] == executable.parent.resolve()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bench_e2e.subprocess, "run", fake_run)
    report = bench_e2e.benchmark_frozen_cold(
        executable=executable,
        audio=audio,
        repeat=2,
        timeout_s=10,
    )

    assert len(commands) == 2
    assert commands[0] != commands[1]
    assert all("--output-json" in command for command in commands)
    assert report["label"] == "frozen_cold_headless_transcription"
    assert report["shipped_artifact_measured"] is True
    assert report["frozen_verified"] is True
    assert report["cold_process"] is True
    assert report["user_perceived_e2e"] is False
    assert report["bundled_default_config"] is True
    assert report["executable_bytes"] == 2
    assert report["executable_sha256"] == sha256(b"MZ").hexdigest()
    assert report["model_load"]["p50_s"] == pytest.approx(0.4)
    assert report["asr"]["p50_s"] == pytest.approx(0.51)
    assert "foreground-app text injection" in report["excludes"]


def test_frozen_cold_rejects_source_process_masquerading_as_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "dcent-voice.exe"
    executable.write_bytes(b"MZ")
    config = tmp_path / "config.toml"
    config.write_text("active_profile='desktop'\n", encoding="utf-8")
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF")

    def fake_run(command: list[str], **_kwargs):
        output = Path(command[command.index("--output-json") + 1])
        output.write_text(json.dumps({"cli_measurement": {"frozen": False}}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bench_e2e.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="did not identify itself as a frozen executable"):
        bench_e2e.benchmark_frozen_cold(
            executable=executable,
            audio=audio,
            repeat=1,
            config_path=config,
        )


def test_frozen_cold_uses_appimage_internal_default_without_host_config(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "DCENT_Voice.AppImage"
    executable.write_bytes(b"\x7fELF")
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        commands.append(command)
        output = Path(command[command.index("--output-json") + 1])
        output.write_text(
            json.dumps(
                {
                    "text": "hello",
                    "provider": "parakeet",
                    "model": "tdt-0.6b-v3",
                    "rejected_reason": "",
                    "timings": {"asr": 0.2, "postprocess": 0.01},
                    "cli_measurement": {"frozen": True},
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bench_e2e.subprocess, "run", fake_run)
    report = bench_e2e.benchmark_frozen_cold(
        executable=executable,
        audio=audio,
        repeat=1,
    )

    assert "--config" not in commands[0]
    assert report["config"] == "artifact-internal"
    assert report["bundled_default_config"] is True


def test_source_warm_report_disclaims_shipped_and_user_e2e(monkeypatch, tmp_path: Path) -> None:
    config = SimpleNamespace(
        active_profile="desktop",
        current_profile=SimpleNamespace(asr=SimpleNamespace(raw="fake:model:int8")),
    )
    result = SimpleNamespace(
        text="hello",
        asr_latency_s=0.01,
        timings={"asr": 0.01, "postprocess": 0.001},
    )

    class FakeEngine:
        def __init__(self, _config) -> None:
            pass

        def load(self) -> None:
            pass

        def transcribe(self, _audio, samplerate=16000):
            return result

        def unload(self) -> None:
            pass

    class FakeInjector:
        def inject(self, _text: str) -> None:
            pass

    monkeypatch.setattr(bench_e2e, "load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(bench_e2e, "load_wav_mono", lambda _path: ([0.1] * 160, 16000))
    monkeypatch.setattr(bench_e2e, "VoiceEngine", FakeEngine)
    monkeypatch.setattr(bench_e2e, "_bench_injector", lambda _config: FakeInjector())

    report = bench_e2e.benchmark_source_warm(audio_path=tmp_path / "speech.wav", repeat=2)

    assert report["label"] == "source_warm_engine_simulated_injection"
    assert report["shipped_artifact_measured"] is False
    assert report["cold_process"] is False
    assert report["user_perceived_e2e"] is False
    assert "model load" in report["excludes"]
    assert "real OS clipboard or keystrokes" in report["excludes"]
