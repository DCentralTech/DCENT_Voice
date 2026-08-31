# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from dcent_voice.asr.faster_whisper_provider import FasterWhisperASRProvider
from dcent_voice.config import ASRSpec


@pytest.mark.hw
def test_faster_whisper_fixture_contains_expected_words() -> None:
    pytest.importorskip("faster_whisper")
    fixture = Path("tests/fixtures/audio/hello.wav")
    assert fixture.exists()
    audio, samplerate = _read_wav_mono_float32(fixture)
    provider = FasterWhisperASRProvider(ASRSpec.parse("faster-whisper:tiny:int8"), language="en")

    result = provider.transcribe(audio, samplerate=samplerate)

    assert "hello" in result.text.lower()
    assert result.asr_latency_s >= 0


def _read_wav_mono_float32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        samplerate = wav.getframerate()
        sample_width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if sample_width != 2:
        raise ValueError("fixture must be 16-bit PCM")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, samplerate
