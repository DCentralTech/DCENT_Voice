# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Measure audio signal amplitude for the interface."""

from __future__ import annotations

import math
import threading
from typing import Any

import numpy as np


class AmplitudeMeter:
    """RMS amplitude meter with exponential smoothing and 0..1 log scaling."""

    def __init__(self, *, smoothing: float = 0.35) -> None:
        if not 0.0 <= smoothing <= 1.0:
            raise ValueError("smoothing must be between 0 and 1")
        self._smoothing = smoothing
        self._level = 0.0
        self._lock = threading.Lock()

    def update(self, samples: Any) -> float:
        array = np.asarray(samples, dtype=np.float32)
        if array.size == 0:
            scaled = 0.0
        else:
            rms = float(np.sqrt(np.mean(np.square(array))))
            scaled = min(1.0, math.log1p(rms * 40.0) / math.log1p(40.0))
        with self._lock:
            self._level = (self._level * (1.0 - self._smoothing)) + (scaled * self._smoothing)
            return self._level

    def read(self) -> float:
        with self._lock:
            return self._level

    def reset(self) -> None:
        with self._lock:
            self._level = 0.0
