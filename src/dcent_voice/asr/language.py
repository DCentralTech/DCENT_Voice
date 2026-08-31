# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""First-class language modes and hardware-aware model routing.

Users choose English / Multilingual / Auto — not Whisper model internals.
English stays on a fast ``.en`` model. Multilingual and auto swap only to the
same-size multilingual sibling so we never silently load large-v3.
"""

from __future__ import annotations

from dataclasses import dataclass

from dcent_voice.asr.base import PARAKEET_V3_LANGUAGE_CODES
from dcent_voice.config import ASRSpec

LANGUAGE_MODES = frozenset({"english", "multilingual", "auto"})

# Human names for Settings / overlay. Users pick a language, not a model ID.
PARAKEET_LANGUAGE_NAMES: dict[str, str] = {
    "bg": "Bulgarian",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "fi": "Finnish",
    "fr": "French",
    "hr": "Croatian",
    "hu": "Hungarian",
    "it": "Italian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mt": "Maltese",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sv": "Swedish",
    "uk": "Ukrainian",
}

# Same-size Whisper fallback only — never large-v3.
FALLBACK_LANGUAGE_NAMES: dict[str, str] = {
    "ar": "Arabic",
    "hi": "Hindi",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
}

# Same-size English ↔ multilingual siblings. Distil English models have no
# English-only-quality multilingual twin at the same speed, so they promote to
# the matching full Whisper size rather than a giant model.
_EN_TO_MULTI: dict[str, str] = {
    "tiny.en": "tiny",
    "base.en": "base",
    "small.en": "small",
    "medium.en": "medium",
    "distil-small.en": "small",
    "distil-medium.en": "medium",
}

_MULTI_TO_EN: dict[str, str] = {
    "tiny": "tiny.en",
    "base": "base.en",
    "small": "small.en",
    "medium": "medium.en",
}

_PARAKEET_MULTILINGUAL_FALLBACK = ASRSpec.parse("faster-whisper:base:cpu-int8")


@dataclass(frozen=True)
class LanguagePolicy:
    """Resolved user-facing language mode for one session."""

    mode: str
    whisper_language: str | None
    prefer_english_model: bool

    @property
    def label(self) -> str:
        if self.mode == "english":
            return "English (fast)"
        if self.mode == "multilingual":
            return "More languages"
        return "Auto-detect"

    @property
    def overlay_label(self) -> str:
        """Short overlay chip. Empty in English-only so the widget stays quiet."""
        if self.mode == "english":
            return ""
        if self.mode == "auto":
            return "Auto"
        return language_display_name(self.whisper_language or "auto")


def language_display_name(code: str | None) -> str:
    raw = (code or "").strip().lower()
    if raw in {"", "auto", "detect"}:
        return "Detect automatically"
    if raw in PARAKEET_LANGUAGE_NAMES:
        return PARAKEET_LANGUAGE_NAMES[raw]
    if raw in FALLBACK_LANGUAGE_NAMES:
        return FALLBACK_LANGUAGE_NAMES[raw]
    return raw


def language_choices() -> list[dict[str, str]]:
    """Settings picker rows: human name + ISO code. No model IDs."""
    rows = [{"code": "auto", "name": "Detect automatically", "route": "parakeet"}]
    for code, name in sorted(PARAKEET_LANGUAGE_NAMES.items(), key=lambda item: item[1]):
        rows.append({"code": code, "name": name, "route": "parakeet"})
    for code, name in sorted(FALLBACK_LANGUAGE_NAMES.items(), key=lambda item: item[1]):
        rows.append({"code": code, "name": f"{name} (same-size fallback)", "route": "whisper"})
    return rows


def normalize_language_mode(value: str | None) -> str:
    mode = (value or "english").strip().lower()
    if mode in {"en", "eng", "english-only", "en-fast"}:
        return "english"
    if mode in {"multi", "many", "intl", "international"}:
        return "multilingual"
    if mode in {"detect", "auto-detect", "autodetect"}:
        return "auto"
    if mode not in LANGUAGE_MODES:
        raise ValueError(f"language_mode must be english, multilingual, or auto (got {value!r})")
    return mode


def infer_language_mode(language: str, explicit_mode: str | None = None) -> str:
    """Resolve a mode from an explicit setting or a language code."""
    if explicit_mode:
        return normalize_language_mode(explicit_mode)
    lang = (language or "en").strip().lower()
    if lang in {"", "auto", "detect"}:
        return "auto"
    if lang in {"en", "eng", "en-us", "en-gb"}:
        return "english"
    return "multilingual"


def resolve_language_policy(
    language_mode: str | None,
    language: str | None,
) -> LanguagePolicy:
    lang = (language or "").strip().lower()
    mode = infer_language_mode(language or "en", language_mode)
    # A concrete non-English code or a genuine auto request cannot truthfully
    # remain on an English-only decoder, even if an older/stale config still
    # says ``language_mode = english``.
    if lang in {"", "auto", "detect"} and language is not None:
        mode = "auto" if mode == "english" else mode
    elif lang not in {"en", "eng", "en-us", "en-gb"}:
        mode = "multilingual"
    if mode == "english":
        return LanguagePolicy(mode="english", whisper_language="en", prefer_english_model=True)
    if mode == "multilingual":
        whisper = None if lang in {"", "auto", "detect", "en", "eng"} else language
        if whisper is not None and whisper.strip().lower() in {"en", "eng"}:
            whisper = None
        return LanguagePolicy(
            mode="multilingual",
            whisper_language=whisper,
            prefer_english_model=False,
        )
    return LanguagePolicy(mode="auto", whisper_language=None, prefer_english_model=False)


def route_asr_spec(spec: ASRSpec, policy: LanguagePolicy) -> ASRSpec:
    """Resolve an ASR spec that can truthfully honor the language policy.

    Parakeet v3 automatically decodes its documented 25 languages. It accepts
    explicit language as validated metadata but exposes neither decoder bias
    nor a detected-language result, so the routed model remains unchanged.
    """
    if spec.provider == "parakeet":
        requested = (policy.whisper_language or "").strip().lower()
        if requested and requested not in PARAKEET_V3_LANGUAGE_CODES:
            return _PARAKEET_MULTILINGUAL_FALLBACK
        return spec
    if spec.provider != "faster-whisper":
        return spec
    model = spec.model.strip()
    key = model.lower()
    if policy.prefer_english_model:
        mapped = _MULTI_TO_EN.get(key)
        if mapped and mapped != model:
            return _with_model(spec, mapped)
        return spec
    mapped = _EN_TO_MULTI.get(key)
    if mapped and mapped != model:
        return _with_model(spec, mapped)
    return spec


def _with_model(spec: ASRSpec, model: str) -> ASRSpec:
    suffix = f":{spec.compute_type}" if spec.compute_type else ""
    raw = f"{spec.provider}:{model}{suffix}"
    return ASRSpec(raw=raw, provider=spec.provider, model=model, compute_type=spec.compute_type)
