# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Dictation evaluation corpus, WER/CER, and formatting scores.

Audio-backed items are scored end-to-end. Text-only items score postprocess
and dictionary quality without pretending to be ASR measurements.
"""

from __future__ import annotations

import json
import re
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice.audio.capture import resample_linear
from dcent_voice.util import paths


def eval_root() -> Path:
    """Repository root that owns the evaluation corpus.

    This module is a development/eval harness: the corpus and its WAV fixtures
    are never shipped. Scoring helpers (``word_error_rate``) are imported by
    runtime code, so the failure is raised here on use, not at import time.
    """
    if paths.is_frozen():
        raise RuntimeError(
            "The evaluation corpus is a development-only harness and is not part of the "
            "packaged DCENT_Voice application; run it from a source checkout instead."
        )
    return paths.source_root()


def default_corpus_path() -> Path:
    return eval_root() / "eval" / "corpus.json"


_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


@dataclass(frozen=True)
class CorpusItem:
    id: str
    reference: str
    tags: tuple[str, ...]
    audio: Path | None = None
    segments: tuple[Path, ...] = ()
    language: str = "en"
    synthetic: str | None = None


@dataclass(frozen=True)
class ItemScore:
    id: str
    reference: str
    hypothesis: str
    wer: float
    cer: float
    format_ok: bool
    kind: str
    tags: tuple[str, ...]
    asr_latency_s: float | None = None
    language: str = ""
    audio_duration_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tags"] = list(self.tags)
        return payload


def load_corpus(path: Path | None = None) -> tuple[CorpusItem, ...]:
    corpus_path = path or default_corpus_path()
    raw = json.loads(corpus_path.read_text(encoding="utf-8"))
    items: list[CorpusItem] = []
    for entry in raw.get("items", []):
        audio_rel = entry.get("audio")
        audio = _resolve_corpus_audio(corpus_path, audio_rel) if audio_rel else None
        segments = tuple(
            _resolve_corpus_audio(corpus_path, segment) for segment in entry.get("segments", ())
        )
        if any(segment is None for segment in segments):
            raise FileNotFoundError(f"missing corpus segment for {entry['id']}")
        items.append(
            CorpusItem(
                id=str(entry["id"]),
                reference=str(entry["reference"]),
                tags=tuple(entry.get("tags") or ()),
                audio=audio,
                segments=tuple(segment for segment in segments if segment is not None),
                language=str(entry.get("language") or "en"),
                synthetic=(str(entry["synthetic"]) if entry.get("synthetic") else None),
            )
        )
    return tuple(items)


def _resolve_corpus_audio(corpus_path: Path, value: str) -> Path | None:
    candidate = (corpus_path.parent / value).resolve()
    if not candidate.is_file():
        candidate = (eval_root() / value).resolve()
    return candidate if candidate.is_file() else None


def tokenize_words(text: str) -> list[str]:
    return [match.group(0).lower() for match in _WORD.finditer(text or "")]


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = tokenize_words(reference)
    hyp = tokenize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def char_error_rate(reference: str, hypothesis: str) -> float:
    ref = re.sub(r"\s+", "", (reference or "").lower())
    hyp = re.sub(r"\s+", "", (hypothesis or "").lower())
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(list(ref), list(hyp)) / len(ref)


def format_score(reference: str, hypothesis: str) -> bool:
    """True when punctuation/casing intent is preserved enough to be usable."""
    if not reference.strip():
        return not hypothesis.strip()
    ref_has_terminal = reference.rstrip()[-1:] in ".?!"
    hyp_stripped = hypothesis.strip()
    if not hyp_stripped:
        return False
    if ref_has_terminal and hyp_stripped[-1] not in ".?!":
        return False
    return (
        tokenize_words(reference) == tokenize_words(hypothesis)
        or word_error_rate(reference, hypothesis) <= 0.25
    )


def _levenshtein(ref: list[str], hyp: list[str]) -> int:
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    previous = list(range(len(hyp) + 1))
    for i, ref_token in enumerate(ref, start=1):
        current = [i]
        for j, hyp_token in enumerate(hyp, start=1):
            cost = 0 if ref_token == hyp_token else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    return previous[-1]


def summarize(scores: tuple[ItemScore, ...]) -> dict[str, Any]:
    if not scores:
        return {
            "items": 0,
            "wer_mean": 0.0,
            "cer_mean": 0.0,
            "format_pass": 0.0,
            "audio_items": 0,
            "text_items": 0,
            "asr_wer_mean": None,
            "asr_wer_micro": None,
            "text_wer_mean": None,
            "code_switch_asr_items": 0,
            "code_switch_asr_wer_mean": None,
            "code_switch_asr_errors": _error_metrics([]),
        }
    audio = [item for item in scores if item.kind == "asr"]
    text = [item for item in scores if item.kind == "text"]
    public = [item for item in audio if "public" in item.tags or "librispeech" in item.tags]
    latency = [item.asr_latency_s for item in audio if item.asr_latency_s is not None]
    duration = [item.audio_duration_s for item in audio if item.audio_duration_s is not None]
    hallucination = _tagged(audio, "hallucination")
    code_switch = _tagged(audio, "code-switch")
    asr_errors = _error_metrics(audio)
    return {
        "items": len(scores),
        "wer_mean": sum(item.wer for item in scores) / len(scores),
        "cer_mean": sum(item.cer for item in scores) / len(scores),
        "format_pass": sum(1 for item in scores if item.format_ok) / len(scores),
        "audio_items": len(audio),
        "text_items": len(text),
        "asr_wer_mean": (sum(item.wer for item in audio) / len(audio)) if audio else None,
        "asr_wer_micro": asr_errors["wer_micro"],
        "asr_cer_micro": asr_errors["cer_micro"],
        "asr_word_edits": asr_errors["word_edits"],
        "asr_reference_words": asr_errors["reference_words"],
        "asr_char_edits": asr_errors["char_edits"],
        "asr_reference_chars": asr_errors["reference_chars"],
        "asr_items_with_word_errors": asr_errors["items_with_word_errors"],
        "text_wer_mean": (sum(item.wer for item in text) / len(text)) if text else None,
        "public_asr_items": len(public),
        "public_asr_wer_mean": (
            (sum(item.wer for item in public) / len(public)) if public else None
        ),
        "noisy_asr_items": len(_tagged(audio, "noisy")),
        "noisy_asr_wer_mean": _mean_wer(_tagged(audio, "noisy")),
        "long_asr_items": len(_tagged(audio, "long")),
        "long_asr_wer_mean": _mean_wer(_tagged(audio, "long")),
        "hallucination_asr_items": len(hallucination),
        "hallucination_failures": sum(1 for item in hallucination if item.hypothesis.strip()),
        "code_switch_asr_items": len(code_switch),
        "code_switch_asr_wer_mean": _mean_wer(code_switch),
        "code_switch_asr_errors": _error_metrics(code_switch),
        "asr_audio_s_total": sum(duration),
        "asr_latency_s_total": sum(latency),
        "asr_latency_p50_s": _percentile(latency, 50),
        "asr_latency_p95_s": _percentile(latency, 95),
        "asr_rtf_total": (sum(latency) / sum(duration) if latency and sum(duration) > 0 else None),
        "by_language": _by_language(audio),
        "by_tag": _by_tag(audio),
    }


def _tagged(audio: list[ItemScore], tag: str) -> list[ItemScore]:
    return [item for item in audio if tag in item.tags]


def _mean_wer(rows: list[ItemScore]) -> float | None:
    if not rows:
        return None
    return sum(item.wer for item in rows) / len(rows)


def _error_metrics(rows: list[ItemScore]) -> dict[str, float | int | None]:
    word_edits = 0
    reference_words = 0
    char_edits = 0
    reference_chars = 0
    items_with_word_errors = 0
    for item in rows:
        ref_words = tokenize_words(item.reference)
        hyp_words = tokenize_words(item.hypothesis)
        edits = _levenshtein(ref_words, hyp_words)
        word_edits += edits
        reference_words += len(ref_words)
        if edits:
            items_with_word_errors += 1
        ref_chars = list(re.sub(r"\s+", "", item.reference.lower()))
        hyp_chars = list(re.sub(r"\s+", "", item.hypothesis.lower()))
        char_edits += _levenshtein(ref_chars, hyp_chars)
        reference_chars += len(ref_chars)
    return {
        "word_edits": word_edits,
        "reference_words": reference_words,
        "wer_micro": word_edits / reference_words if reference_words else None,
        "char_edits": char_edits,
        "reference_chars": reference_chars,
        "cer_micro": char_edits / reference_chars if reference_chars else None,
        "items_with_word_errors": items_with_word_errors,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((q / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def speech_rms(path: Path) -> float:
    """Peak-normalized RMS of a 16-bit WAV. Used to refuse silence-as-speech."""
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        width = handle.getsampwidth()
    if width != 2:
        raise ValueError(f"{path} must be 16-bit PCM")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64) / 32768.0
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


def wav_duration_s(path: Path) -> float:
    """Duration of a checked-in PCM WAV fixture."""
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def concatenate_wav_segments(
    paths: tuple[Path, ...], *, gap_s: float = 0.25
) -> tuple[np.ndarray, int]:
    """Join checked-in PCM speech with deterministic silence between segments.

    This supports an explicitly synthetic code-switch decoder test without
    claiming that separate public speakers form natural conversational speech.
    """

    if len(paths) < 2:
        raise ValueError("a concatenated corpus item requires at least two segments")
    if not 0.0 <= gap_s <= 2.0:
        raise ValueError("concatenation gap must be between 0 and 2 seconds")
    chunks: list[np.ndarray] = []
    samplerate = 16000
    for path in paths:
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise ValueError(f"{path} must be 16-bit mono PCM")
            current_rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0 or float(np.sqrt(np.mean(np.square(samples)))) < 0.01:
            raise ValueError(f"corpus segment is silence: {path}")
        chunks.append(resample_linear(samples, current_rate, samplerate))
    gap = np.zeros(round(samplerate * gap_s), dtype=np.float32)
    joined: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        if index:
            joined.append(gap)
        joined.append(chunk)
    return np.concatenate(joined), samplerate


def _by_language(audio: list[ItemScore]) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[ItemScore]] = {}
    for item in audio:
        key = (item.language or "und").strip().lower() or "und"
        grouped.setdefault(key, []).append(item)
    return {
        lang: {
            "items": len(rows),
            "wer_mean": sum(row.wer for row in rows) / len(rows),
            "cer_mean": sum(row.cer for row in rows) / len(rows),
        }
        for lang, rows in sorted(grouped.items())
    }


def _by_tag(audio: list[ItemScore]) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[str, list[ItemScore]] = {}
    for item in audio:
        for tag in item.tags:
            grouped.setdefault(tag, []).append(item)
    return {
        tag: {"items": len(rows), **_error_metrics(rows)} for tag, rows in sorted(grouped.items())
    }


# Public + product real-speech clips for the shipped default (desktop Parakeet).
# Broader than hello.wav alone: short LibriSpeech, noisy, multilingual smoke.
SHIPPED_DEFAULT_AUDIO_IDS: tuple[str, ...] = (
    "hello",
    "ls-tc-wait",
    "ls-tc-marie",
    "ls-tc-alice",
    "ls-tc-fortune",
    "ls-short-twenties",
    "ls-short-work",
    "ls-classic-exist",
    "ls-noisy-wait",
    "ls-noisy-marie",
    "ll-en-above-all",
    "lingua-libre-de-hallo",
    "lingua-libre-fr-je-mappelle",
    "short-command",
    "punctuation",
    "ls-tc-angor",
)


def score_shipped_default_audio_corpus(transcribe_file: Any) -> tuple[ItemScore, ...]:
    """Score the shipped real-speech corpus WAVs."""
    return _score_audio_ids(transcribe_file, SHIPPED_DEFAULT_AUDIO_IDS)


# Tagged-long clips excluded from the 16-id shipped-default set, plus the two
# hard shorts whose WER sat inside that passing mean (noisy-marie, angor).
SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS: tuple[str, ...] = (
    "ls-christmas",
    "ls-tc-endeavour",
    "ls-long-linnell",
    "ls-noisy-endeavour",
    "ls-noisy-christmas",
    "ls-noisy-marie",
    "ls-tc-angor",
)


def score_shipped_default_longform_corpus(transcribe_file: Any) -> tuple[ItemScore, ...]:
    """Score long-form and difficult real-speech WAVs."""
    return _score_audio_ids(transcribe_file, SHIPPED_DEFAULT_LONGFORM_AUDIO_IDS)


# Product-domain clips excluded from the 16-id general set and the 7-id longform
# set: numbers, URLs, filenames, code, shell, Bitcoin, D-Central terms.
SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS: tuple[str, ...] = (
    "numbers",
    "url-email",
    "filename",
    "developer-file",
    "shell",
    "bitcoin",
    "dcentral-terms",
    "conversational",
)


def score_shipped_default_product_corpus(transcribe_file: Any) -> tuple[ItemScore, ...]:
    """Score product-domain dictation WAVs."""
    return _score_audio_ids(transcribe_file, SHIPPED_DEFAULT_PRODUCT_AUDIO_IDS)


# Dedicated multilingual set: FR/DE already in the 16-clip smoke, plus Spanish
# which that set omitted. Distinct from product-domain and long-form ids.
SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS: tuple[str, ...] = (
    "lingua-libre-fr-je-mappelle",
    "lingua-libre-de-hallo",
    "lingua-libre-es-hola",
)


def score_shipped_default_multilingual_corpus(
    transcribe_file: Any,
) -> tuple[ItemScore, ...]:
    """Score multilingual real-speech WAVs."""
    return _score_audio_ids(transcribe_file, SHIPPED_DEFAULT_MULTILINGUAL_AUDIO_IDS)


# Dedicated UK-accent set: above-all is in the 16-clip smoke; air-pollution is not.
SHIPPED_DEFAULT_ACCENT_AUDIO_IDS: tuple[str, ...] = (
    "ll-en-above-all",
    "ll-en-air-pollution",
)


def score_shipped_default_accent_corpus(transcribe_file: Any) -> tuple[ItemScore, ...]:
    """Score accented real-speech WAVs."""
    return _score_audio_ids(transcribe_file, SHIPPED_DEFAULT_ACCENT_AUDIO_IDS)


# Dedicated 8 dB AWGN set omitted from the 16-clip smoke and the 7-clip long-form
# set (those already carry noisy-wait/marie and noisy-endeavour/christmas).
SHIPPED_DEFAULT_NOISY_AUDIO_IDS: tuple[str, ...] = (
    "ls-noisy-exist",
    "ls-noisy-quilter",
    "ls-noisy-fortune",
)


def score_shipped_default_noisy_corpus(transcribe_file: Any) -> tuple[ItemScore, ...]:
    """Score noisy real-speech WAVs."""
    return _score_audio_ids(transcribe_file, SHIPPED_DEFAULT_NOISY_AUDIO_IDS)


# Dedicated uncommon-name set omitted from the 16-clip, long-form, product,
# multilingual, accent, and noisy sets (Brandd / Shaggy / Quilter).
SHIPPED_DEFAULT_NAMED_AUDIO_IDS: tuple[str, ...] = (
    "ls-importance",
    "ls-rose-princess",
    "ls-quilter",
)


def score_shipped_default_named_corpus(transcribe_file: Any) -> tuple[ItemScore, ...]:
    """Score real-speech WAVs containing uncommon names."""
    return _score_audio_ids(transcribe_file, SHIPPED_DEFAULT_NAMED_AUDIO_IDS)


def _score_audio_ids(transcribe_file: Any, item_ids: tuple[str, ...]) -> tuple[ItemScore, ...]:
    catalog = {item.id: item for item in load_corpus()}
    scores: list[ItemScore] = []
    for item_id in item_ids:
        item = catalog[item_id]
        if item.audio is None or not item.audio.is_file():
            raise FileNotFoundError(f"missing corpus audio: {item_id}")
        if speech_rms(item.audio) < 0.01:
            raise ValueError(f"corpus audio is silence: {item_id}")
        result = transcribe_file(item.audio)
        text = getattr(result, "text", None)
        hypothesis = str(result if text is None else text)
        scores.append(
            ItemScore(
                id=item.id,
                reference=item.reference,
                hypothesis=hypothesis,
                wer=word_error_rate(item.reference, hypothesis),
                cer=char_error_rate(item.reference, hypothesis),
                format_ok=format_score(item.reference, hypothesis),
                kind="asr",
                tags=item.tags,
                asr_latency_s=getattr(result, "asr_latency_s", None),
                language=item.language,
                audio_duration_s=wav_duration_s(item.audio),
            )
        )
    return tuple(scores)
