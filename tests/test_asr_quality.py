# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from dcent_voice.asr.quality import (
    collapse_consecutive_repeats,
    is_hint_echo,
    sanitize_transcript,
    segment_text_keep,
)


def test_collapse_should_be_able_loop() -> None:
    looped = ("should be able to " * 40).strip()
    collapsed = collapse_consecutive_repeats(looped)
    assert collapsed.lower().count("should be able to") <= 2
    assert len(collapsed) < len(looped) // 4


def test_sanitize_rejects_extreme_density() -> None:
    # 26s of audio with 1100+ chars is the screenshot-class failure mode.
    # Pure loops must be hard-rejected (not soft-accepted as a short remnant).
    text = ("should be able to " * 50).strip()
    result = sanitize_transcript(text, duration_s=26.0, max_chars_per_s=28.0)
    assert result.rejected_reason == "asr_hallucination"
    assert result.text == ""
    assert result.collapsed is True


def test_sanitize_keeps_normal_dictation() -> None:
    text = (
        "I want you to look at the previous work on the map editor "
        "and prepare a full implementation plan for the world editor."
    )
    result = sanitize_transcript(text, duration_s=12.0)
    assert result.rejected_reason == ""
    assert "map editor" in result.text
    assert result.chars_per_s < 28


def test_segment_drops_high_no_speech() -> None:
    assert not segment_text_keep(
        "Thanks for watching",
        no_speech_prob=0.95,
        avg_logprob=-0.2,
        compression_ratio=1.1,
    )


def test_segment_keeps_normal() -> None:
    assert segment_text_keep(
        "Hello world",
        no_speech_prob=0.1,
        avg_logprob=-0.3,
        compression_ratio=1.2,
    )


def test_phantom_only_rejected() -> None:
    result = sanitize_transcript("Thanks for watching.", duration_s=2.0)
    assert result.rejected_reason == "asr_phantom"
    assert result.text == ""


def test_hint_echo_requires_high_multi_term_overlap() -> None:
    hints = "D-Central d central Lightning Network Satoshi Nakamoto hardware wallet"
    assert is_hint_echo("D-Central, DCENTRAL, Lightning, D-Satoshi, Lightning.com", hints)
    assert not is_hint_echo("D-Central ships voice software for sovereign dictation", hints)
