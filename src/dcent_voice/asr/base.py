# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Define common speech-recognition provider types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Whisper's published language vocabulary. This is deliberately a concrete
# set rather than a shape check: accepting an invented pair such as ``zz`` can
# disclose audio to a cloud provider before the provider returns its own 4xx.
WHISPER_LANGUAGE_CODES = frozenset(
    """af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi
    fo fr gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln
    lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd
    si sk sl sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh
    yue""".split()  # noqa: SIM905 - auditable published table
)

# xAI documents Filipino with the ISO-639-3 identifier ``fil``. It is the one
# intentional provider-specific extension to the otherwise ISO-639-1 contract.
KNOWN_LANGUAGE_IDENTIFIERS = WHISPER_LANGUAGE_CODES | {"fil"}

# NVIDIA's immutable Parakeet TDT 0.6B v3 model card declares these 25
# languages. The onnx-asr adapter decodes them automatically without a
# language-bias parameter or detected-language result.
PARAKEET_V3_LANGUAGE_CODES = frozenset(
    "bg cs da de el en es et fi fr hr hu it lt lv mt nl pl pt ro ru sk sl sv uk".split()  # noqa: SIM905 - auditable upstream table
)


class Locality(Enum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    language: str
    duration_s: float
    asr_latency_s: float
    # Set when quality filters drop the transcript (hallucination/phantom/empty).
    rejected_reason: str = ""
    chars_per_s: float = 0.0


class ASRProvider(ABC):
    """Protocol implemented by speech-recognition backends."""

    locality: Locality
    supports_per_call_language = False

    @property
    def supports_language_auto_detection(self) -> bool:
        return False

    @property
    def supported_language_codes(self) -> frozenset[str] | None:
        return None

    @abstractmethod
    def load(self) -> None:
        """Load model resources lazily."""

    @abstractmethod
    def unload(self) -> None:
        """Release loaded model resources."""

    def is_loaded(self) -> bool:
        """True when weights are resident. Cloud backends are always ready."""
        return True

    @abstractmethod
    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        """Transcribe mono float audio into text."""

    def validate_language_hint(self, language: str | None) -> str | None:
        normalized = normalize_language_hint(language)
        if normalized == "" and not self.supports_language_auto_detection:
            raise UnsupportedLanguageError(
                f"{type(self).__name__} does not support automatic language detection"
            )
        supported = self.supported_language_codes
        if normalized not in {None, ""} and supported is not None and normalized not in supported:
            allowed = ", ".join(sorted(supported))
            raise UnsupportedLanguageError(f"{type(self).__name__} supports only: {allowed}")
        return normalized


class UnsupportedLanguageError(ValueError):
    """An explicit language cannot be honored by the selected ASR provider."""


def normalize_language_hint(language: str | None) -> str | None:
    """Normalize a recognized language hint; empty string means auto-detect."""
    if language is None:
        return None
    if type(language) is not str:
        raise UnsupportedLanguageError("language must be a recognized string or null")
    normalized = language.strip().lower()
    if normalized in {"", "auto", "detect"}:
        return ""
    if normalized not in KNOWN_LANGUAGE_IDENTIFIERS:
        raise UnsupportedLanguageError(
            "language must be a recognized ISO-639-1/provider code or auto"
        )
    return normalized
