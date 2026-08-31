# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Post-ASR quality filters: density, repetition collapse, phantom phrases.

Whisper (and faster-whisper) can enter multi-window repetition cascades that
emit physically implausible transcripts (e.g. 40+ chars/s of "should be able").
These helpers run after decode and before inject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Normal English dictation is ~10–15 characters of transcript per second of audio.
# Above this, treat as loop/hallucination after collapse attempts.
DEFAULT_MAX_CHARS_PER_S = 28.0

# Collapse only when the same phrase runs many times in a row.
_MIN_REPEAT_RUN = 4

_PHANTOM_WHOLE = re.compile(
    r"^(thanks for watching\.?|thank you for watching\.?|"
    r"please subscribe\.?|subscribe to the channel\.?|"
    r"see you next time\.?)$",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"\S+")
_HINT_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True)
class QualityResult:
    text: str
    rejected_reason: str = ""
    chars_per_s: float = 0.0
    collapsed: bool = False


def chars_per_second(text: str, duration_s: float) -> float:
    dur = max(float(duration_s), 0.1)
    return len(text) / dur


def collapse_consecutive_repeats(text: str, *, min_run: int = _MIN_REPEAT_RUN) -> str:
    """Collapse runs of identical n-grams (2–6 words) repeated min_run+ times."""
    words = _WORD_RE.findall(text)
    if len(words) < min_run * 2:
        return _normalize_spaces(text)

    for n in range(2, 7):
        words = _collapse_ngram_run(words, n=n, min_run=min_run)
    return _normalize_spaces(" ".join(words))


def _collapse_ngram_run(words: list[str], *, n: int, min_run: int) -> list[str]:
    if len(words) < n * min_run:
        return words
    out: list[str] = []
    i = 0
    while i < len(words):
        if i + n * min_run <= len(words):
            gram = tuple(w.lower() for w in words[i : i + n])
            run = 1
            j = i + n
            while j + n <= len(words) and tuple(w.lower() for w in words[j : j + n]) == gram:
                run += 1
                j += n
            if run >= min_run:
                # Keep a single copy of the phrase.
                out.extend(words[i : i + n])
                i = j
                continue
        out.append(words[i])
        i += 1
    return out


def is_phantom_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_PHANTOM_WHOLE.match(stripped))


def is_hint_echo(text: str, hint_text: str | None) -> bool:
    """Detect a transcript dominated by decoder hotwords.

    Whisper can replace short, low-confidence speech with a list of supplied
    hotwords. Require several distinct hint tokens and high token coverage so
    normal product sentences merely containing one or two terms remain valid.
    Providers may retry without hotwords when this triggers.
    """

    words = [match.group(0).casefold() for match in _HINT_WORD_RE.finditer(text)]
    hints = {match.group(0).casefold() for match in _HINT_WORD_RE.finditer(hint_text or "")}
    if len(words) < 5 or not hints:
        return False
    matched = [word for word in words if word in hints]
    return len(set(matched)) >= 3 and len(matched) / len(words) >= 0.6


def sanitize_transcript(
    text: str,
    *,
    duration_s: float,
    max_chars_per_s: float = DEFAULT_MAX_CHARS_PER_S,
    hint_text: str | None = None,
) -> QualityResult:
    """Collapse loops and reject density/phantom hallucinations."""
    cleaned = _normalize_spaces(text)
    if not cleaned:
        return QualityResult(text="", rejected_reason="", chars_per_s=0.0)

    if is_phantom_only(cleaned):
        return QualityResult(
            text="",
            rejected_reason="asr_phantom",
            chars_per_s=chars_per_second(cleaned, duration_s),
        )

    if is_hint_echo(cleaned, hint_text):
        return QualityResult(
            text="",
            rejected_reason="asr_hint_echo",
            chars_per_s=chars_per_second(cleaned, duration_s),
        )

    raw_rate = chars_per_second(cleaned, duration_s)
    collapsed = collapse_consecutive_repeats(cleaned)
    did_collapse = collapsed != cleaned
    rate = chars_per_second(collapsed, duration_s)

    # Pre-collapse density: pure loops collapse to a short remnant that would
    # look "normal" if we only measured density after collapse. If the raw
    # transcript was absurdly dense and collapse removed most of it, reject.
    raw_density_applies = duration_s >= 1.5 or len(cleaned) >= 80
    if (
        did_collapse
        and raw_density_applies
        and raw_rate > max_chars_per_s
        and len(collapsed) <= max(1, int(len(cleaned) * 0.5))
    ):
        return QualityResult(
            text="",
            rejected_reason="asr_hallucination",
            chars_per_s=raw_rate,
            collapsed=True,
        )

    # Density is only meaningful on longer clips (or already-huge text). Short
    # PTT ("hi" in 0.2s) would otherwise false-positive at 28 c/s.
    density_applies = duration_s >= 1.5 or len(collapsed) >= 80
    if density_applies and rate > max_chars_per_s:
        # Second pass: more aggressive collapse of short n-grams.
        collapsed2 = collapse_consecutive_repeats(collapsed, min_run=3)
        rate2 = chars_per_second(collapsed2, duration_s)
        if rate2 > max_chars_per_s:
            return QualityResult(
                text="",
                rejected_reason="asr_hallucination",
                chars_per_s=rate2,
                collapsed=did_collapse or collapsed2 != collapsed,
            )
        return QualityResult(
            text=collapsed2,
            rejected_reason="",
            chars_per_s=rate2,
            collapsed=True,
        )

    return QualityResult(
        text=collapsed,
        rejected_reason="",
        chars_per_s=rate,
        collapsed=did_collapse,
    )


def segment_text_keep(
    text: str,
    *,
    no_speech_prob: float | None,
    avg_logprob: float | None,
    compression_ratio: float | None,
    no_speech_threshold: float = 0.6,
    log_prob_threshold: float = -1.0,
    compression_ratio_threshold: float = 2.4,
) -> bool:
    """Return False if this Whisper segment should be dropped."""
    body = (text or "").strip()
    if not body:
        return False
    if body in {".", ",", "!", "?", "…", "..."}:
        return False
    if no_speech_prob is not None and no_speech_prob > no_speech_threshold:
        return False
    if compression_ratio is not None and compression_ratio > compression_ratio_threshold:
        return False
    # Only drop on low logprob when the model is also unsure about speech.
    return not (
        avg_logprob is not None
        and avg_logprob < log_prob_threshold
        and no_speech_prob is not None
        and no_speech_prob > 0.4
    )


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
