# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Core TTS types: the backend protocol, audio chunks, and capability reports.

Audio convention (matches ``dcent_voice.audio``): synthesized audio is **mono
float32** in ``[-1.0, 1.0]``. Unlike capture (fixed 16 kHz), a TTS backend emits
at its model's native rate (Kokoro uses 24 kHz), so each
:class:`AudioChunk` carries its own ``sample_rate`` and the playback sink is
responsible for any device resampling.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class AudioChunk:
    """A block of mono float32 PCM at a given sample rate.

    ``samples`` is a 1-D float32 array in ``[-1.0, 1.0]``. Chunks are kept short
    (tens of milliseconds) so playback can be cancelled between them well within
    the 100 ms barge-in budget.
    """

    samples: np.ndarray
    sample_rate: int

    def __post_init__(self) -> None:
        array = np.asarray(self.samples, dtype=np.float32).reshape(-1)
        object.__setattr__(self, "samples", array)
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

    @property
    def duration_s(self) -> float:
        return self.samples.size / float(self.sample_rate)

    @property
    def frames(self) -> int:
        return int(self.samples.size)


@dataclass(frozen=True)
class TtsCapability:
    """What a backend can do right now — advertised to callers and the DVAP layer.

    ``available`` is the load-bearing flag: it is ``True`` only when the backend's
    inference library imports and its model assets are present on disk, so a
    fresh install (no downloaded models) reports ``available=False`` and the DVAP
    endpoint keeps ``tts.*`` out of its advertised capabilities.
    """

    name: str
    available: bool
    sample_rate: int
    voice: str = ""
    license: str = ""
    detail: str = ""


class TtsError(RuntimeError):
    """Base class for TTS failures."""


class TtsUnavailable(TtsError):
    """Raised when synthesis is attempted but the backend/model is not ready."""


@runtime_checkable
class TtsBackend(Protocol):
    """A local text-to-speech engine.

    Implementations are lazy: importing the module never imports the inference
    library or touches the network. ``synthesize`` streams :class:`AudioChunk`s so
    the caller can begin playback on the first chunk; ``cancel`` makes an
    in-flight ``synthesize`` stop yielding promptly (checked between chunks).
    """

    name: str

    def capability(self) -> TtsCapability:
        """Report availability, native sample rate, voice, and license."""

    def available(self) -> bool:
        """True when inference library and model assets are ready (no network)."""

    def synthesize(self, text: str) -> Iterator[AudioChunk]:
        """Stream mono float32 audio chunks for ``text``.

        Raises :class:`TtsUnavailable` if the backend is not ready.
        """

    def cancel(self) -> None:
        """Signal an in-flight :meth:`synthesize` to stop yielding chunks."""
