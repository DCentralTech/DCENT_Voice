# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from dcent_voice.service.api import (
    MAX_AUDIO_SECONDS,
    MAX_STREAM_CHUNK_SAMPLES,
    AudioValidationError,
    ServiceEngine,
)
from dcent_voice.service.streaming import StreamingSession, common_word_prefix


class _NeverSpeech:
    def is_speech(self, _audio, _samplerate):
        return SimpleNamespace(speech=False)


def test_common_word_prefix() -> None:
    assert common_word_prefix("hello brave world", "hello brave there") == "hello brave"


def test_streaming_session_silence_does_not_transcribe(fake_asr) -> None:
    session = StreamingSession(ServiceEngine(asr=fake_asr))

    message = session.push(np.zeros(1600, dtype=np.float32))

    assert message.type == "silence"
    assert message.result is None


def test_streaming_session_commits_repeated_prefix(fake_asr) -> None:
    session = StreamingSession(ServiceEngine(asr=fake_asr))

    first = session.push(np.ones(16000, dtype=np.float32) * 0.1)
    second = session.push(np.ones(16000, dtype=np.float32) * 0.1)

    assert first.type == "partial"
    assert second.committed == "hello world"


def test_streaming_rejects_oversized_chunk_before_iteration(fake_asr) -> None:
    class OversizedAudio:
        def __len__(self) -> int:
            return MAX_STREAM_CHUNK_SAMPLES + 1

        def __iter__(self):
            raise AssertionError("oversized input must be rejected before conversion")

    session = StreamingSession(ServiceEngine(asr=fake_asr))
    with pytest.raises(AudioValidationError, match="limit") as excinfo:
        session.push(OversizedAudio(), samplerate=48_000)
    assert excinfo.value.too_large is True


@pytest.mark.parametrize("bad_sample", [float("nan"), float("inf"), 1e100, "0.1", True])
def test_streaming_rejects_nonfinite_or_nonnumeric_audio(fake_asr, bad_sample) -> None:
    session = StreamingSession(ServiceEngine(asr=fake_asr))
    with pytest.raises(AudioValidationError, match="finite"):
        session.push([bad_sample], samplerate=16_000)


def test_streaming_samplerate_is_fixed_until_final(fake_asr) -> None:
    session = StreamingSession(ServiceEngine(asr=fake_asr))
    session.push(np.ones(8_000, dtype=np.float32) * 0.1, samplerate=16_000)

    with pytest.raises(AudioValidationError, match="cannot change"):
        session.push(np.ones(8_000, dtype=np.float32) * 0.1, samplerate=48_000)

    session.push([], final=True)  # omitted rate inherits the active 16 kHz rate
    assert session._active_samplerate is None


def test_streaming_caps_total_utterance_buffer(fake_asr) -> None:
    session = StreamingSession(ServiceEngine(asr=fake_asr))
    session._active_samplerate = 16_000
    session._buffer = np.zeros(16_000 * MAX_AUDIO_SECONDS, dtype=np.float32)

    with pytest.raises(AudioValidationError, match="utterance") as excinfo:
        session.push([0.0], samplerate=16_000)

    assert excinfo.value.too_large is True


def test_streaming_caps_cumulative_utterance_after_rolling_window(fake_asr) -> None:
    session = StreamingSession(ServiceEngine(asr=fake_asr), vad=_NeverSpeech())
    chunk = np.zeros(16_000 * 5, dtype=np.float32)

    for _ in range(MAX_AUDIO_SECONDS // 5):
        session.push(chunk, samplerate=16_000)

    assert session._buffer.size == 16_000 * 8
    assert session._utterance_samples == 16_000 * MAX_AUDIO_SECONDS
    with pytest.raises(AudioValidationError, match="utterance") as excinfo:
        session.push(chunk, samplerate=16_000)

    assert excinfo.value.too_large is True
