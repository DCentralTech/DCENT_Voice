# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from dataclasses import replace

from dcent_voice.app import main
from dcent_voice.audio.capture import AudioCapture
from dcent_voice.config import TtsConfig, load_config


def test_autostart_sync_can_be_disabled_for_isolated_automation(monkeypatch) -> None:
    from dcent_voice import app

    # conftest's autouse profile isolation sets this for every test; drop it so
    # the enabled branch is exercised on its real default.
    monkeypatch.delenv("DCENT_VOICE_DISABLE_AUTOSTART", raising=False)
    assert app._autostart_sync_enabled() is True
    monkeypatch.setenv("DCENT_VOICE_DISABLE_AUTOSTART", "true")
    assert app._autostart_sync_enabled() is False


def test_devices_bench_cli_prints_profile_suggestion_json(capsys) -> None:
    exit_code = main(
        [
            "--config",
            "config.example.toml",
            "devices",
            "--bench",
            "--device-class",
            "gpu",
            "--samples-ms",
            "420,455,480",
            "--assume-input-devices",
            "1",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "meets-target"
    assert payload["p50_finalize_ms"] == 455
    assert payload["threshold_ms"] == 500
    assert payload["suggested_profile"] == "gpu"


def test_benchmark_cli_delegates_to_packaged_benchmark_module(monkeypatch) -> None:
    from dcent_voice import app

    calls: list[list[str]] = []
    monkeypatch.setattr(app, "benchmark_main", lambda argv: calls.append(argv) or 17)

    exit_code = app.main(["--config", "config.example.toml", "benchmark"])

    assert exit_code == 17
    assert calls == [["--config", "config.example.toml"]]


def test_tts_mic_policy_builds_pause_duck_and_off_gates() -> None:
    from dcent_voice import app

    capture = AudioCapture()
    config = load_config("config.example.toml", create=False)

    paused: list[bool] = []
    capture.stop = lambda: paused.append(True)  # type: ignore[method-assign]
    pause_gate = app._build_tts_mic_gate(
        capture,
        replace(config, tts=TtsConfig(enabled=True, mic_policy="pause")),
        tts_available=True,
    )
    assert pause_gate is not None
    pause_gate.on_tts_start()
    assert paused == [True]

    duck_gate = app._build_tts_mic_gate(
        capture,
        replace(config, tts=TtsConfig(enabled=True, mic_policy="duck", duck_gain=0.3)),
        tts_available=True,
    )
    assert duck_gate is not None
    duck_gate.on_tts_start()
    assert capture.input_gain == 0.3
    duck_gate.on_tts_stop()
    assert capture.input_gain == 1.0

    off_gate = app._build_tts_mic_gate(
        capture,
        replace(config, tts=TtsConfig(enabled=True, mic_policy="off")),
        tts_available=True,
    )
    unavailable_gate = app._build_tts_mic_gate(
        capture,
        replace(config, tts=TtsConfig(enabled=True, mic_policy="duck")),
        tts_available=False,
    )
    assert off_gate is None
    assert unavailable_gate is None
