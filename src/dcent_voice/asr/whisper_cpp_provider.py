# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Optional whisper.cpp local ASR backend (pywhispercpp)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from dcent_voice.asr.base import (
    WHISPER_LANGUAGE_CODES,
    ASRProvider,
    Locality,
    TranscriptResult,
    UnsupportedLanguageError,
    normalize_language_hint,
)
from dcent_voice.asr.quality import sanitize_transcript
from dcent_voice.config import APP_NAME, ASRSpec

logger = logging.getLogger(APP_NAME).getChild("asr")


class WhisperCppASRProvider(ASRProvider):
    """Local whisper.cpp provider. Optional extra; never silently required."""

    locality = Locality.LOCAL
    supports_per_call_language = True

    @property
    def supported_language_codes(self) -> frozenset[str]:
        if self.spec.model.strip().lower().endswith(".en"):
            return frozenset({"en"})
        return WHISPER_LANGUAGE_CODES

    @property
    def supports_language_auto_detection(self) -> bool:
        return not self.spec.model.strip().lower().endswith(".en")

    def validate_language_hint(self, language: str | None) -> str | None:
        normalized = normalize_language_hint(language)
        if self.spec.model.strip().lower().endswith(".en"):
            if normalized == "":
                raise UnsupportedLanguageError(
                    "the selected whisper.cpp .en model does not support "
                    "automatic language detection"
                )
            if normalized not in {None, "en"}:
                raise UnsupportedLanguageError(
                    "the selected whisper.cpp .en model supports only English"
                )
        return super().validate_language_hint(normalized)

    def __init__(self, spec: ASRSpec, *, language: str = "en") -> None:
        if spec.provider != "whisper-cpp":
            raise ValueError(f"Unsupported ASR provider for WhisperCppASRProvider: {spec.raw}")
        self.spec = spec
        configured_language = self.validate_language_hint(language)
        self.language = "auto" if configured_language == "" else configured_language or "en"
        self._model: Any | None = None
        self._lock = threading.RLock()
        self._last_decode_s: float | None = None

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                from pywhispercpp.model import Model
            except ImportError as exc:
                raise RuntimeError(
                    "whisper-cpp requires the optional extra: "
                    'python -m pip install "dcent-voice[whispercpp]" '
                    "or `uv pip install pywhispercpp`."
                ) from exc
            started = time.perf_counter()
            language = (self.language or "en").strip().lower()
            lang_arg = None if language in {"", "auto", "detect"} else language
            self._model = Model(
                self.spec.model,
                language=lang_arg or "en",
                n_threads=_default_threads(),
                print_progress=False,
                print_realtime=False,
                print_special=False,
                redirect_whispercpp_logs_to=None,
            )
            logger.info(
                "whisper.cpp ready model=%s language=%s load_s=%.3f",
                self.spec.model,
                lang_arg or "en",
                time.perf_counter() - started,
            )

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        requested_language = self.validate_language_hint(language)
        with self._lock:
            self.load()
            model = self._model
        assert model is not None
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samplerate != 16000:
            duration_s = len(samples) / float(samplerate)
            old_x = np.linspace(0.0, duration_s, num=len(samples), endpoint=False)
            new_len = int(duration_s * 16000)
            new_x = np.linspace(0.0, duration_s, num=new_len, endpoint=False)
            samples = np.interp(new_x, old_x, samples).astype(np.float32)
        started = time.perf_counter()
        effective_language = self.language if requested_language is None else requested_language
        params: dict[str, Any] = {
            "language": None
            if (effective_language or "").strip().lower() in {"", "auto", "detect"}
            else effective_language,
            "single_segment": True,
            "no_context": True,
        }
        if initial_prompt:
            params["initial_prompt"] = initial_prompt
        segments = model.transcribe(samples, **params)
        text = " ".join(getattr(seg, "text", "") or "" for seg in segments).strip()
        duration_s = len(np.asarray(audio).reshape(-1)) / float(samplerate)
        quality = sanitize_transcript(text, duration_s=duration_s)
        latency = time.perf_counter() - started
        self._last_decode_s = latency
        return TranscriptResult(
            text=quality.text if not quality.rejected_reason else "",
            language=effective_language or "",
            duration_s=duration_s,
            asr_latency_s=latency,
            rejected_reason=quality.rejected_reason,
            chars_per_s=quality.chars_per_s,
        )


def _default_threads() -> int:
    import os

    cpu = os.cpu_count() or 4
    return max(2, min(6, cpu))


def whisper_cpp_available() -> bool:
    try:
        import pywhispercpp  # noqa: F401
    except ImportError:
        return False
    return True
