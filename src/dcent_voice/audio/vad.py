# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Detect voiced speech in captured audio."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class VADResult:
    speech: bool
    probability: float


class EnergyVAD:
    """Voice-activity detector based on audio energy thresholds."""

    def __init__(self, *, threshold: float = 0.01) -> None:
        self.threshold = threshold

    def is_speech(self, audio: Any, samplerate: int = 16000) -> VADResult:
        samples = np.asarray(audio, dtype=np.float32)
        if samples.size == 0:
            return VADResult(False, 0.0)
        rms = float(np.sqrt(np.mean(np.square(samples))))
        probability = min(1.0, rms / max(self.threshold, 1e-6))
        return VADResult(probability >= 1.0, probability)


class SileroVAD:
    """Voice-activity detector backed by the optional Silero model."""

    def __init__(self, *, threshold: float = 0.5, fallback_threshold: float = 0.01) -> None:
        self.threshold = threshold
        self.fallback = EnergyVAD(threshold=fallback_threshold)
        self._model: Any | None = None
        self._torch: Any | None = None

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import torch

            model, _utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
                trust_repo=True,
            )
        except Exception:
            return False
        self._torch = torch
        self._model = model
        return True

    def is_speech(self, audio: Any, samplerate: int = 16000) -> VADResult:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return VADResult(False, 0.0)
        if not self.load():
            return self.fallback.is_speech(samples, samplerate)
        torch_module = self._torch
        model = self._model
        if torch_module is None or model is None:
            return self.fallback.is_speech(samples, samplerate)
        tensor = torch_module.from_numpy(samples)
        probability = float(model(tensor, samplerate).item())
        return VADResult(probability >= self.threshold, probability)
