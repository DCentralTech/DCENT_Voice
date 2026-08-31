# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Build ASR providers from a spec without pulling in the desktop shell."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from dcent_voice.asr.base import ASRProvider, Locality
from dcent_voice.asr.cloud import build_cloud_asr_provider
from dcent_voice.asr.faster_whisper_provider import FasterWhisperASRProvider
from dcent_voice.asr.language import LanguagePolicy, resolve_language_policy, route_asr_spec
from dcent_voice.asr.lifecycle import DEFAULT_IDLE_UNLOAD_S
from dcent_voice.asr.model_registry import faster_whisper_model_status
from dcent_voice.config import APP_NAME, ASRSpec

logger = logging.getLogger(APP_NAME).getChild("asr")

FALLBACK_WHISPER = ASRSpec.parse("faster-whisper:base:cpu-int8")

EgressLogger = Callable[[str, str, int], None]


def build_asr_provider(
    spec: ASRSpec,
    *,
    language: str = "en",
    language_mode: str | None = None,
    api_key: str | None = None,
    egress_logger: EgressLogger | None = None,
    policy: LanguagePolicy | None = None,
    idle_unload_s: float = DEFAULT_IDLE_UNLOAD_S,
) -> ASRProvider:
    """Construct the runtime ASR backend for ``spec``.

    Language mode may rewrite an English-only local backend to a bounded
    multilingual Faster Whisper sibling. Cloud providers are never selected
    implicitly.
    """
    resolved_policy = policy or resolve_language_policy(language_mode, language)
    routed = route_asr_spec(spec, resolved_policy)
    whisper_language = (
        "auto"
        if resolved_policy.mode == "auto"
        else resolved_policy.whisper_language or language or "en"
    )
    if routed.provider == "faster-whisper":
        return FasterWhisperASRProvider(
            routed, language=whisper_language or "en", idle_unload_s=idle_unload_s
        )
    if routed.provider == "whisper-cpp":
        from dcent_voice.asr.whisper_cpp_provider import WhisperCppASRProvider

        return WhisperCppASRProvider(routed, language=whisper_language or "en")
    if routed.provider == "parakeet":
        from dcent_voice.asr.parakeet_provider import ParakeetASRProvider, parakeet_available

        if not parakeet_available():
            # The native payload includes the pinned multilingual ``base``
            # snapshot. Keep this dependency fallback on that shipped model;
            # English preference must not rewrite it to an unbundled base.en.
            fallback = FALLBACK_WHISPER
            logger.warning(
                "parakeet requested but onnx-asr failed to import; falling back to %s",
                fallback.raw,
            )
            return FasterWhisperASRProvider(
                fallback, language=whisper_language or "en", idle_unload_s=idle_unload_s
            )
        return ParakeetASRProvider(
            routed, language=whisper_language or "en", idle_unload_s=idle_unload_s
        )
    if egress_logger is None:
        raise RuntimeError("Cloud ASR requires a consent-enforcing metadata egress logger.")
    return build_cloud_asr_provider(routed, api_key=api_key, egress_logger=egress_logger)


def describe_asr(spec: ASRSpec, policy: LanguagePolicy) -> dict[str, Any]:
    """Machine-readable capability record for ADE / CLI / Settings."""
    routed = route_asr_spec(spec, policy)
    english_only = routed.provider == "parakeet" or (
        routed.provider in {"faster-whisper", "whisper-cpp"}
        and routed.model.strip().lower().endswith(".en")
    )
    info: dict[str, Any] = {
        "requested": spec.raw,
        "resolved": routed.raw,
        "provider": routed.provider,
        "model": routed.model,
        "compute_type": routed.compute_type,
        "language_mode": policy.mode,
        "language": policy.whisper_language or "auto",
        "language_hint": {
            "supported": True,
            "codes": ["en"] if english_only else "iso-639-1",
            "auto": not english_only,
        },
        "local": routed.provider in {"faster-whisper", "whisper-cpp", "parakeet"},
    }
    if routed.provider == "parakeet":
        from dcent_voice.asr.base import PARAKEET_V3_LANGUAGE_CODES

        info["language_hint"] = {
            "supported": True,
            "codes": sorted(PARAKEET_V3_LANGUAGE_CODES),
            "auto": True,
            "effect": "metadata_only",
            "reports_detected_language": False,
        }
    if routed.provider == "faster-whisper":
        info["model_readiness"] = faster_whisper_model_status(routed.model)
    elif routed.provider == "parakeet":
        from dcent_voice.asr.parakeet_provider import parakeet_model_status

        info["model_readiness"] = parakeet_model_status()
    return info


def describe_active_asr(
    provider: ASRProvider,
    policy: LanguagePolicy,
    *,
    requested: str | None = None,
) -> dict[str, Any]:
    """Describe the actual injected/resolved provider an engine will invoke."""
    spec = getattr(provider, "spec", None)
    provider_name = getattr(spec, "provider", type(provider).__name__)
    supported = getattr(provider, "supported_language_codes", None)
    language_hint: dict[str, Any] = {
        "supported": bool(getattr(provider, "supports_per_call_language", False)),
        "codes": sorted(supported) if supported is not None else "recognized",
        "auto": bool(getattr(provider, "supports_language_auto_detection", False)),
    }
    info: dict[str, Any] = {
        "requested": requested or getattr(spec, "raw", provider_name),
        "resolved": getattr(spec, "raw", provider_name),
        "provider": provider_name,
        "model": getattr(spec, "model", ""),
        "compute_type": getattr(spec, "compute_type", None),
        "language_mode": policy.mode,
        "language": policy.whisper_language or "auto",
        "language_hint": language_hint,
        "local": getattr(provider, "locality", None) is Locality.LOCAL,
    }
    effect = getattr(provider, "language_hint_effect", None)
    if effect is not None:
        language_hint["effect"] = effect
        language_hint["reports_detected_language"] = bool(
            getattr(provider, "reports_detected_language", False)
        )
    if provider_name == "faster-whisper":
        info["model_readiness"] = faster_whisper_model_status(getattr(spec, "model", ""))
    elif provider_name == "parakeet":
        from dcent_voice.asr.parakeet_provider import parakeet_model_status

        info["model_readiness"] = parakeet_model_status()
    return info
