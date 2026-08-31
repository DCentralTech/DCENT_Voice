# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""A deterministic, dependency-free TTS backend for CI and latency fixtures.

Produces silent-ish PCM whose length and content derive only from the input
text, so tests are reproducible and never touch a model or the network. It is a
real :class:`~dcent_voice.tts.base.TtsBackend`, so the DVAP path and playback are
exercised end to end without downloading a real TTS model.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator

import numpy as np

from dcent_voice.tts.base import AudioChunk, TtsCapability, TtsUnavailable


class FakeTtsBackend:
    """Deterministic text-to-speech backend used by tests."""

    name = "fake"

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        chunk_ms: int = 20,
        available: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self._available = available
        self._cancel = threading.Event()

    def capability(self) -> TtsCapability:
        return TtsCapability(
            name=self.name,
            available=self._available,
            sample_rate=self.sample_rate,
            voice="fake",
            license="none",
            detail="Deterministic test backend.",
        )

    def available(self) -> bool:
        return self._available

    def synthesize(self, text: str) -> Iterator[AudioChunk]:
        if not self._available:
            raise TtsUnavailable("FakeTtsBackend configured as unavailable")
        self._cancel.clear()
        chunk_frames = max(1, self.sample_rate * self.chunk_ms // 1000)
        # ~40 ms of audio per character, deterministic from the text.
        total_frames = max(chunk_frames, int(0.04 * self.sample_rate * max(1, len(text.strip()))))
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big")
        rng = np.random.default_rng(seed)
        produced = 0
        while produced < total_frames:
            if self._cancel.is_set():
                return
            frames = min(chunk_frames, total_frames - produced)
            samples = rng.standard_normal(frames).astype(np.float32) * 1e-3
            produced += frames
            yield AudioChunk(samples=samples, sample_rate=self.sample_rate)

    def cancel(self) -> None:
        self._cancel.set()
