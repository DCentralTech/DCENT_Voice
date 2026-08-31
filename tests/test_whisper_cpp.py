# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import numpy as np
import pytest

from dcent_voice.asr.factory import build_asr_provider
from dcent_voice.asr.whisper_cpp_provider import WhisperCppASRProvider, whisper_cpp_available
from dcent_voice.config import ASRSpec, load_config


def test_factory_builds_whisper_cpp_provider() -> None:
    provider = build_asr_provider(ASRSpec.parse("whisper-cpp:base.en"), language="en")
    assert isinstance(provider, WhisperCppASRProvider)
    assert provider.locality.value == "local"


def test_example_config_keeps_faster_whisper_default() -> None:
    from pathlib import Path

    config = load_config(Path("config.example.toml"), create=False)
    assert config.current_profile.asr.provider == "parakeet"
    assert config.profiles["whispercpp"].asr.provider == "whisper-cpp"


def test_whisper_cpp_availability_matches_import() -> None:
    assert whisper_cpp_available() is True or whisper_cpp_available() is False


@pytest.mark.parametrize("automatic", ["", "auto", "detect"])
def test_english_only_whisper_cpp_rejects_auto_before_load(automatic: str) -> None:
    provider = WhisperCppASRProvider(ASRSpec.parse("whisper-cpp:base.en"), language="en")
    with pytest.raises(ValueError, match="automatic language detection"):
        provider.transcribe(np.zeros(16, dtype=np.float32), language=automatic)
    assert provider._model is None
