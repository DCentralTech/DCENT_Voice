# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Kokoro-82M TTS backend (default, Apache-2.0).

Inference route: **kokoro-onnx on onnxruntime**. onnxruntime already ships in this
stack (faster-whisper depends on it), so this route adds a thin ONNX wrapper and
the model/voice files — no torch, no CUDA toolchain, no heavyweight graph. That is
why Kokoro-via-onnx is the default over the reference PyTorch route.

The ``kokoro_onnx`` package and the model assets are optional: importing this
module never imports them. ``available()`` is ``True`` only when both the library
imports and the downloaded assets are present, so a fresh install reports
unavailable and the DVAP layer keeps ``tts.*`` unadvertised.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from dcent_voice.tts.assets import KOKORO_ASSETS, tts_model_dir
from dcent_voice.tts.base import AudioChunk, TtsCapability, TtsUnavailable

KOKORO_SAMPLE_RATE = 24000
DEFAULT_VOICE = "af_heart"


class KokoroTtsBackend:
    """Local Kokoro text-to-speech backend."""

    name = "kokoro"

    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        lang: str = "en-us",
        model_root: Path | None = None,
        chunk_ms: int = 40,
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.lang = lang
        self._dir = tts_model_dir("kokoro", root=model_root)
        self.chunk_ms = chunk_ms
        self._kokoro = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def _asset_paths(self) -> tuple[Path, Path]:
        model = self._dir / KOKORO_ASSETS[0].filename
        voices = self._dir / KOKORO_ASSETS[1].filename
        return model, voices

    def available(self) -> bool:
        model, voices = self._asset_paths()
        if not (model.exists() and voices.exists()):
            return False
        import importlib.util

        return importlib.util.find_spec("kokoro_onnx") is not None

    def capability(self) -> TtsCapability:
        return TtsCapability(
            name=self.name,
            available=self.available(),
            sample_rate=KOKORO_SAMPLE_RATE,
            voice=self.voice,
            license="Apache-2.0",
            detail="Kokoro-82M via kokoro-onnx (onnxruntime).",
        )

    def _load(self):  # pragma: no cover - requires model + library
        with self._lock:
            if self._kokoro is not None:
                return self._kokoro
            model, voices = self._asset_paths()
            if not (model.exists() and voices.exists()):
                raise TtsUnavailable("Kokoro model assets are not downloaded.")
            try:
                from kokoro_onnx import Kokoro
            except ImportError as exc:
                raise TtsUnavailable("kokoro-onnx is required for the Kokoro TTS backend.") from exc
            self._kokoro = Kokoro(str(model), str(voices))
            return self._kokoro

    def synthesize(self, text: str) -> Iterator[AudioChunk]:  # pragma: no cover - hardware
        self._cancel.clear()
        kokoro = self._load()
        samples, sample_rate = kokoro.create(
            text, voice=self.voice, speed=self.speed, lang=self.lang
        )
        yield from _chunk(
            np.asarray(samples, dtype=np.float32), int(sample_rate), self.chunk_ms, self._cancel
        )

    def cancel(self) -> None:
        self._cancel.set()


def _chunk(
    samples: np.ndarray, sample_rate: int, chunk_ms: int, cancel: threading.Event
) -> Iterator[AudioChunk]:
    frames = max(1, sample_rate * chunk_ms // 1000)
    for start in range(0, samples.size, frames):
        if cancel.is_set():
            return
        yield AudioChunk(samples=samples[start : start + frames], sample_rate=sample_rate)
