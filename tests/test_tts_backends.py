# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import numpy as np
import pytest

from dcent_voice.config import TtsConfig
from dcent_voice.tts import AudioChunk, FakeTtsBackend, build_tts_backend
from dcent_voice.tts.base import TtsUnavailable
from dcent_voice.tts.kokoro import KokoroTtsBackend
from dcent_voice.tts.piper import PiperTtsBackend

# --- AudioChunk conventions -----------------------------------------------------


def test_audio_chunk_is_mono_float32() -> None:
    chunk = AudioChunk(samples=[0.0, 0.5, -0.5], sample_rate=24000)
    assert chunk.samples.dtype == np.float32
    assert chunk.samples.ndim == 1
    assert chunk.frames == 3
    assert chunk.duration_s == pytest.approx(3 / 24000)


def test_audio_chunk_rejects_bad_rate() -> None:
    with pytest.raises(ValueError):
        AudioChunk(samples=[0.0], sample_rate=0)


# --- FakeTtsBackend -------------------------------------------------------------


def test_fake_backend_is_deterministic() -> None:
    backend = FakeTtsBackend()
    first = np.concatenate([c.samples for c in backend.synthesize("hello world")])
    second = np.concatenate([c.samples for c in backend.synthesize("hello world")])
    assert np.array_equal(first, second)
    # Different text yields different audio.
    other = np.concatenate([c.samples for c in backend.synthesize("goodbye")])
    assert not np.array_equal(first[: other.size], other[: first.size])


def test_fake_backend_capability() -> None:
    cap = FakeTtsBackend().capability()
    assert cap.available is True
    assert cap.sample_rate == 24000


def test_fake_backend_cancel_stops_stream() -> None:
    backend = FakeTtsBackend(sample_rate=24000, chunk_ms=20)
    it = backend.synthesize("a fairly long sentence to synthesize into chunks")
    next(it)  # first chunk
    backend.cancel()
    remaining = list(it)
    assert remaining == []


def test_unavailable_fake_backend_raises() -> None:
    backend = FakeTtsBackend(available=False)
    assert backend.available() is False
    with pytest.raises(TtsUnavailable):
        list(backend.synthesize("hi"))


# --- Real backends report unavailable without assets ----------------------------


def test_kokoro_unavailable_without_assets(tmp_path) -> None:
    backend = KokoroTtsBackend(model_root=tmp_path)
    assert backend.available() is False
    cap = backend.capability()
    assert cap.name == "kokoro"
    assert cap.license == "Apache-2.0"
    assert cap.sample_rate == 24000
    assert cap.available is False


def test_piper_requires_a_pinned_local_manifest(tmp_path) -> None:
    backend = PiperTtsBackend(model_root=tmp_path)
    assert backend.available() is False
    assert backend.capability().license == "User supplied"
    with pytest.raises(TtsUnavailable, match="manifest.json"):
        list(backend.synthesize("hello"))


# --- Factory --------------------------------------------------------------------


def test_factory_returns_none_when_disabled() -> None:
    assert build_tts_backend(TtsConfig(enabled=False)) is None


def test_factory_returns_none_when_no_assets(tmp_path) -> None:
    # Enabled, but no models downloaded → nothing available → None (so DVAP keeps
    # tts.* unadvertised).
    config = TtsConfig(enabled=True, backend="auto")
    assert build_tts_backend(config, model_root=tmp_path) is None
    assert build_tts_backend(TtsConfig(enabled=True, backend="kokoro"), model_root=tmp_path) is None
    # Piper stays unavailable until a local manifest pins a licensed voice.
    assert build_tts_backend(TtsConfig(enabled=True, backend="piper"), model_root=tmp_path) is None
