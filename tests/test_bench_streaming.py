# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dcent_voice.pipeline import PipelineConfig
from scripts import bench_streaming


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


def test_require_speech_rejects_silence(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    path.write_bytes(b"RIFF")
    with pytest.raises(ValueError, match="silence"):
        bench_streaming.require_speech(np.zeros(16000, dtype=np.float32), path)


def test_require_shipped_asr_rejects_tiny_standin() -> None:
    with pytest.raises(ValueError, match="tiny"):
        bench_streaming.require_shipped_asr("faster-whisper:tiny.en:int8")
    with pytest.raises(ValueError, match="base.en"):
        bench_streaming.require_shipped_asr("faster-whisper:base.en:cpu-int8")
    bench_streaming.require_shipped_asr("parakeet:tdt-0.6b-v3:int8")


def test_simulate_cadence_records_first_partial_before_stable() -> None:
    clock = _Clock()
    audio = np.full(16000 * 2, 0.1, dtype=np.float32)
    texts = ["hello", "hello", "hello there"]

    def transcribe(window: np.ndarray) -> str:
        del window
        return texts[min(transcribe.calls, len(texts) - 1)]

    transcribe.calls = 0

    def counting(window: np.ndarray) -> str:
        text = transcribe(window)
        transcribe.calls += 1
        clock.t += 0.05  # simulated ASR
        return text

    result = bench_streaming.simulate_streaming_cadence(
        audio=audio,
        samplerate=16000,
        transcribe=counting,
        config=PipelineConfig(
            stream_first_peek_s=0.32,
            stream_interval_s=0.45,
            stream_min_audio_s=0.32,
            stream_agreement_passes=3,
            stream_first_agreement_passes=2,
        ),
        sleep=clock.sleep,
        clock=clock,
    )
    assert result["first_partial_s"] is not None
    assert result["first_stable_s"] is not None
    assert result["first_partial_s"] <= result["first_stable_s"]
    assert result["committed"].startswith("hello")
    assert result["rewrite_count"] == 0


def test_word_rewrite_detects_abandoned_prefix() -> None:
    assert bench_streaming.word_rewrite("i scream", "ice cream") is True
    assert bench_streaming.word_rewrite("hello", "hello there") is False


def test_parser_accepts_atomic_output_path(tmp_path: Path) -> None:
    output = tmp_path / "streaming.json"
    args = bench_streaming.build_parser().parse_args(["--output-json", str(output)])
    assert args.output_json == output
