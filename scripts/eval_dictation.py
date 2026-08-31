# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Score the shipped dictation path against eval/corpus.json.

ASR items use the real default engine. Text items score offline polish only.
Never reports text-only scores as ASR WER.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.attach.registry import write_text_atomic  # noqa: E402
from dcent_voice.config import VocabEntry, load_config  # noqa: E402
from dcent_voice.dictation.postprocess import apply_dictation_postprocess  # noqa: E402
from dcent_voice.eval_corpus import (  # noqa: E402
    ItemScore,
    char_error_rate,
    concatenate_wav_segments,
    format_score,
    load_corpus,
    summarize,
    wav_duration_s,
    word_error_rate,
)
from dcent_voice.pipeline import apply_dictionary  # noqa: E402


def require_shipped_asr(spec: str) -> None:
    lowered = spec.lower()
    if any(token in lowered for token in ("tiny", "distil")):
        raise ValueError(f"refusing tiny/distil stand-in: {spec}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate DCENT_Voice dictation quality.")
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--asr", default=None, help="Override ASR spec for audio items.")
    parser.add_argument(
        "--language-mode",
        choices=("english", "multilingual", "auto"),
        default=None,
        help="Override language policy (required for an explicit multilingual ASR eval).",
    )
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Atomically persist the complete JSON report.",
    )
    args = parser.parse_args(argv)

    corpus_path = (args.corpus or ROOT / "eval" / "corpus.json").resolve()

    config = load_config(ROOT / "config.example.toml", create=False)
    dictionary = config.dictionary
    scores: list[ItemScore] = []
    engine = None
    if not args.skip_asr:
        from dataclasses import replace

        from dcent_voice.config import ASRSpec
        from dcent_voice.engine import VoiceEngine

        if args.asr:
            profile = replace(config.current_profile, asr=ASRSpec.parse(args.asr))
            profiles = dict(config.profiles)
            profiles[config.active_profile] = profile
            config = replace(config, profiles=profiles)
        if args.language_mode:
            config = replace(config, language_mode=args.language_mode)
        require_shipped_asr(config.current_profile.asr.raw)
        engine = VoiceEngine(config)

    try:
        for item in load_corpus(corpus_path):
            duration = None
            if item.synthetic == "silence_2s" and engine is not None:
                duration = 2.0
                result = engine.transcribe(
                    np.zeros(int(16000 * duration), dtype=np.float32),
                    samplerate=16000,
                    language=item.language,
                )
                hypothesis = result.text
                kind = "asr"
                latency = result.asr_latency_s
            elif item.synthetic == "concatenate_wavs" and engine is not None:
                audio, samplerate = concatenate_wav_segments(item.segments)
                duration = len(audio) / float(samplerate)
                result = engine.transcribe(
                    audio,
                    samplerate=samplerate,
                    language=item.language,
                )
                hypothesis = result.text
                kind = "asr"
                latency = result.asr_latency_s
            elif item.synthetic:
                if item.synthetic not in {"silence_2s", "concatenate_wavs"}:
                    raise ValueError(f"unsupported synthetic corpus item: {item.synthetic}")
                hypothesis = _text_path(item.reference, dictionary, item.id)
                kind = "text"
                latency = None
            elif item.audio is not None and engine is not None:
                duration = wav_duration_s(item.audio)
                result = engine.transcribe_file(item.audio, language=item.language)
                hypothesis = result.text
                kind = "asr"
                latency = result.asr_latency_s
            else:
                hypothesis = _text_path(item.reference, dictionary, item.id)
                kind = "text"
                latency = None
            scores.append(
                ItemScore(
                    id=item.id,
                    reference=item.reference,
                    hypothesis=hypothesis,
                    wer=word_error_rate(item.reference, hypothesis),
                    cer=char_error_rate(item.reference, hypothesis),
                    format_ok=format_score(item.reference, hypothesis),
                    kind=kind,
                    tags=item.tags,
                    asr_latency_s=latency,
                    language=item.language,
                    audio_duration_s=duration,
                )
            )
    finally:
        if engine is not None:
            engine.unload()

    report: dict[str, Any] = {
        "schema": "dcent-dictation-eval-result-v1",
        "scope": "offline_file_corpus_no_microphone_no_injection",
        "corpus": str(corpus_path),
        "summary": summarize(tuple(scores)),
        "items": [item.to_dict() for item in scores],
        "asr_spec": config.current_profile.asr.raw,
        "language_mode": config.language_mode,
    }
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        write_text_atomic(args.output_json, serialized, require_private=False)
    if args.json:
        print(serialized, end="")
    else:
        summary = report["summary"]
        print("DCENT_Voice dictation eval")
        asr_wer = summary.get("asr_wer_mean")
        pub_wer = summary.get("public_asr_wer_mean")
        asr_label = "n/a" if asr_wer is None else f"{asr_wer:.3f}"
        pub_label = "n/a" if pub_wer is None else f"{pub_wer:.3f}"
        print(
            f"items={summary['items']} audio={summary['audio_items']} "
            f"text={summary['text_items']} asr_wer_mean={asr_label} "
            f"public_asr_wer_mean={pub_label} "
            f"mixed_wer_mean={summary['wer_mean']:.3f} "
            f"format_pass={summary['format_pass']:.2f}"
        )
        print(
            "asr_wer_mean is all audio. public_asr_wer_mean is public-tagged audio "
            "(LibriSpeech, Lingua Libre). mixed_wer_mean includes text polish items. "
            "format_pass is not a WER substitute."
        )
        for score in scores:
            mark = "ok" if score.wer <= 0.25 else "miss"
            print(
                f"  {score.id:16} {score.kind:4} WER={score.wer:.2f} {mark}  {score.hypothesis!r}"
            )
    return 0


def _text_path(reference: str, dictionary: tuple[VocabEntry, ...], item_id: str) -> str:
    spoken = {
        "spoken-dev": "Open the file app dot py in vs code",
        "correction": "Use d cent voice no I meant DCENT_Voice",
        "dcentral-terms": "d central ships d cent voice for sovereign dictation",
        "silence": "",
    }.get(item_id, reference)
    text = apply_dictionary(spoken, dictionary)
    return apply_dictation_postprocess(text)


if __name__ == "__main__":
    raise SystemExit(main())
