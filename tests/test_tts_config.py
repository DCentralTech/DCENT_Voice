# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path

import pytest

from dcent_voice.config import ConfigError, TtsConfig, load_config, parse_config


def _base() -> dict:
    return {"active_profile": "d", "profile": {"d": {"asr": "faster-whisper:tiny", "llm": "none"}}}


def test_example_config_tts_defaults_off() -> None:
    config = load_config(Path("config.example.toml"), create=False)
    assert config.tts.enabled is False
    assert config.tts.backend == "kokoro"
    assert config.tts.mic_policy == "pause"
    assert config.tts.skip_code is True


def test_missing_tts_section_uses_defaults() -> None:
    config = parse_config(_base())
    assert config.tts == TtsConfig()


def test_tts_section_parsed() -> None:
    raw = _base()
    raw["tts"] = {
        "enabled": True,
        "backend": "piper",
        "voice": "legacy-voice",
        "mic_policy": "duck",
        "duck_gain": 0.3,
        "skip_code": False,
    }
    config = parse_config(raw)
    assert config.tts.enabled is True
    assert config.tts.backend == "piper"
    assert config.tts.mic_policy == "duck"
    assert config.tts.duck_gain == pytest.approx(0.3)
    assert config.tts.skip_code is False


@pytest.mark.parametrize(
    "tts",
    [
        {"backend": "xtts"},  # excluded engine
        {"mic_policy": "shout"},
        {"duck_gain": 2.0},
        {"duck_gain": "loud"},
    ],
)
def test_invalid_tts_values_rejected(tts: dict) -> None:
    raw = _base()
    raw["tts"] = tts
    with pytest.raises(ConfigError):
        parse_config(raw)
