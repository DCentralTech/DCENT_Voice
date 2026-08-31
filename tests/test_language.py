# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from dcent_voice.asr.language import resolve_language_policy, route_asr_spec
from dcent_voice.config import ASRSpec


def test_english_mode_keeps_base_en() -> None:
    policy = resolve_language_policy("english", "en")
    spec = route_asr_spec(ASRSpec.parse("faster-whisper:base.en:cpu-int8"), policy)
    assert spec.model == "base.en"
    assert policy.whisper_language == "en"


def test_multilingual_mode_swaps_same_size_sibling() -> None:
    policy = resolve_language_policy("multilingual", "auto")
    spec = route_asr_spec(ASRSpec.parse("faster-whisper:base.en:cpu-int8"), policy)
    assert spec.model == "base"
    assert spec.compute_type == "cpu-int8"
    assert policy.whisper_language is None


def test_english_mode_maps_multilingual_base_to_en() -> None:
    policy = resolve_language_policy("english", "en")
    spec = route_asr_spec(ASRSpec.parse("faster-whisper:base:cpu-int8"), policy)
    assert spec.model == "base.en"


def test_cloud_spec_is_not_rewritten() -> None:
    policy = resolve_language_policy("multilingual", "auto")
    spec = route_asr_spec(ASRSpec.parse("xai:grok-stt"), policy)
    assert spec.provider == "xai"
    assert spec.model == "grok-stt"


@pytest.mark.parametrize("mode", ["multilingual", "auto"])
def test_parakeet_keeps_fast_multilingual_default(mode: str) -> None:
    policy = resolve_language_policy(mode, "auto")
    spec = route_asr_spec(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), policy)
    assert spec.raw == "parakeet:tdt-0.6b-v3:int8"


@pytest.mark.parametrize(
    ("configured_mode", "language", "resolved_mode"),
    (("english", "fr", "multilingual"), ("english", "auto", "auto")),
)
def test_explicit_non_english_or_auto_overrides_stale_english_mode(
    configured_mode: str, language: str, resolved_mode: str
) -> None:
    policy = resolve_language_policy(configured_mode, language)
    assert policy.mode == resolved_mode
    routed = route_asr_spec(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), policy)
    assert routed.raw == "parakeet:tdt-0.6b-v3:int8"


def test_parakeet_unsupported_language_routes_to_multilingual_whisper() -> None:
    policy = resolve_language_policy("multilingual", "ja")
    routed = route_asr_spec(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), policy)
    assert routed.raw == "faster-whisper:base:cpu-int8"


def test_invalid_mode_raises() -> None:
    with pytest.raises(ValueError):
        resolve_language_policy("huge-model", "en")


def test_language_picker_uses_human_names_not_model_ids() -> None:
    from dcent_voice.asr.base import PARAKEET_V3_LANGUAGE_CODES
    from dcent_voice.asr.language import (
        PARAKEET_LANGUAGE_NAMES,
        language_choices,
        language_display_name,
    )

    assert set(PARAKEET_LANGUAGE_NAMES) == set(PARAKEET_V3_LANGUAGE_CODES)
    assert language_display_name("fr") == "French"
    assert language_display_name("de") == "German"
    assert language_display_name("ja") == "Japanese"
    assert language_display_name("auto") == "Detect automatically"
    names = " ".join(row["name"] for row in language_choices())
    assert "parakeet" not in names.lower()
    assert "whisper" not in names.lower()
    assert "large-v3" not in names.lower()
    assert any(row["code"] == "fr" and row["name"] == "French" for row in language_choices())
    assert any(row["code"] == "ja" and "fallback" in row["name"] for row in language_choices())


def test_english_mode_hides_overlay_language_chip() -> None:
    assert resolve_language_policy("english", "en").overlay_label == ""
    assert resolve_language_policy("auto", "auto").overlay_label == "Auto"
    assert resolve_language_policy("multilingual", "fr").overlay_label == "French"


def test_mixed_language_english_mode_keeps_parakeet_for_supported_speech() -> None:
    """English (fast) does not switch models when the utterance is French."""
    policy = resolve_language_policy("english", "en")
    routed = route_asr_spec(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), policy)
    assert routed.raw == "parakeet:tdt-0.6b-v3:int8"
    spoken_fr = resolve_language_policy("english", "fr")
    assert spoken_fr.mode == "multilingual"
    assert route_asr_spec(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), spoken_fr).raw == (
        "parakeet:tdt-0.6b-v3:int8"
    )
