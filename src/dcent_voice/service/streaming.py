# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Manage incremental dictation streaming sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from dcent_voice.audio.vad import EnergyVAD
from dcent_voice.service.api import (
    MAX_AUDIO_SECONDS,
    AudioValidationError,
    ServiceEngine,
    TranscribeRequest,
    validate_audio_payload,
)


@dataclass
class StreamMessage:
    type: str
    text: str
    committed: str
    partial: str
    speech: bool
    result: dict[str, Any] | None = None


@dataclass
class StreamingSession:
    """Maintain one incremental ADE dictation streaming session."""

    engine: ServiceEngine
    samplerate: int = 16000
    block_seconds: float = 0.5
    max_window_seconds: float = 8.0
    vad: Any = field(default_factory=EnergyVAD)
    committed: str = ""
    last_partial: str = ""
    style: str | None = None
    polish: bool = True
    app_context: str | None = None
    prose_context: bool = False
    _buffer: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _active_samplerate: int | None = None
    _utterance_samples: int = 0

    def push(
        self,
        audio: Any,
        *,
        final: bool = False,
        samplerate: int | None = None,
        style: str | None = None,
        polish: bool | None = None,
        app_context: str | None = None,
        prose_context: bool | None = None,
    ) -> StreamMessage:
        if style is not None:
            self.style = style
        if polish is not None:
            self.polish = polish
        if app_context is not None:
            self.app_context = app_context
        if prose_context is not None:
            self.prose_context = prose_context
        # style/polish set on an earlier frame remain for later final=True.
        requested_rate = (
            samplerate if samplerate is not None else (self._active_samplerate or self.samplerate)
        )
        rate = validate_audio_payload(audio, requested_rate)
        if self._buffer.size and self._active_samplerate != rate:
            raise AudioValidationError("samplerate cannot change within a streaming utterance")
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        accumulated_samples = max(self._utterance_samples, int(self._buffer.size))
        if accumulated_samples + samples.size > rate * MAX_AUDIO_SECONDS:
            raise AudioValidationError(
                f"streaming utterance exceeds {MAX_AUDIO_SECONDS} seconds",
                too_large=True,
            )
        if samples.size and self._active_samplerate is None:
            self._active_samplerate = rate
        vad_result = self.vad.is_speech(samples, rate)
        if samples.size:
            self._utterance_samples = accumulated_samples + samples.size
            self._buffer = np.concatenate((self._buffer, samples))
            max_samples = int(self.max_window_seconds * rate)
            if self._buffer.size > max_samples:
                self._buffer = self._buffer[-max_samples:]

        if not vad_result.speech and not final:
            return StreamMessage(
                type="silence",
                text=self.committed,
                committed=self.committed,
                partial=self.last_partial,
                speech=False,
            )

        result = self.engine.transcribe(
            TranscribeRequest(
                audio=self._buffer.astype(float).tolist(),
                samplerate=rate,
                cleanup=final,
                polish=self.polish,
                style=self.style,
                app_context=self.app_context,
                prose_context=self.prose_context,
            )
        )
        partial = result["cleaned"] if final else result["raw"]
        if final:
            message = StreamMessage(
                type="final",
                text=partial,
                committed=partial,
                partial=partial,
                speech=vad_result.speech,
                result=result,
            )
            # Reset so the next utterance on the same connection starts clean;
            # otherwise its audio is appended to this one's residual buffer and
            # transcribed as "tail of A + B".
            self._buffer = np.zeros(0, dtype=np.float32)
            self._active_samplerate = None
            self._utterance_samples = 0
            self.committed = ""
            self.last_partial = ""
            self.style = None
            self.polish = True
            self.app_context = None
            self.prose_context = False
            return message

        prefix = common_word_prefix(self.last_partial, partial)
        if len(prefix) > len(self.committed):
            self.committed = prefix
        self.last_partial = partial
        return StreamMessage(
            type="partial",
            text=self.committed or partial,
            committed=self.committed,
            partial=partial,
            speech=vad_result.speech,
            result=result,
        )


def common_word_prefix(left: str, right: str) -> str:
    left_words = left.split()
    right_words = right.split()
    shared: list[str] = []
    for left_word, right_word in zip(left_words, right_words, strict=False):
        if left_word != right_word:
            break
        shared.append(left_word)
    return " ".join(shared)
