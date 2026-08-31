# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Local ASR tournament on the real hello.wav fixture.

Does not change the shipped default. Prints speed and transcript together so
a fast model cannot win on silence or on a different fixture.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.config import ASRSpec, load_config  # noqa: E402
from dcent_voice.engine import VoiceEngine, load_wav_mono  # noqa: E402
from dcent_voice.eval_corpus import word_error_rate  # noqa: E402

CANDIDATES = (
    "faster-whisper:tiny.en:cpu-int8",
    "faster-whisper:base.en:cpu-int8",
    "faster-whisper:base:cpu-int8",
    "faster-whisper:distil-small.en:cpu-int8",
    "whisper-cpp:base.en",
    "whisper-cpp:tiny.en",
    "parakeet:tdt-0.6b-v3:int8",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare local ASR candidates.")
    parser.add_argument(
        "--audio",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "audio" / "hello.wav",
    )
    parser.add_argument("--reference", default="Hello world")
    parser.add_argument(
        "--language", default="en", help="Recognized language code or auto for every candidate."
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    audio, samplerate = load_wav_mono(args.audio)
    rows = []
    for spec in CANDIDATES:
        row = _run_candidate(spec, audio, samplerate, args.reference, args.repeat, args.language)
        rows.append(row)
        if not args.json:
            print(f"{spec:44} p50={row['asr_p50']} WER={row['wer']:.2f} text={row['text']!r}")

    if args.json:
        print(
            json.dumps(
                {"candidates": rows, "audio": str(args.audio), "language": args.language},
                indent=2,
            )
        )
    else:
        print("Shipped default is parakeet:tdt-0.6b-v3:int8. Whisper remains the fallback.")
    return 0


def _run_candidate(
    spec: str, audio, samplerate: int, reference: str, repeat: int, language: str = "en"
) -> dict:
    from dataclasses import replace

    config = load_config(ROOT / "config.example.toml", create=False)
    profile = replace(config.current_profile, asr=ASRSpec.parse(spec))
    profiles = dict(config.profiles)
    profiles[config.active_profile] = profile
    language_mode = (
        "auto"
        if language in {"", "auto", "detect"}
        else ("english" if language == "en" else "multilingual")
    )
    config = replace(config, profiles=profiles, language=language, language_mode=language_mode)
    engine = VoiceEngine(config, polish=False)
    try:
        engine.load()
        engine.transcribe(audio, samplerate=samplerate, language=language)
        times = []
        text = ""
        for _ in range(max(1, repeat)):
            started = time.perf_counter()
            result = engine.transcribe(audio, samplerate=samplerate, language=language)
            times.append(time.perf_counter() - started)
            text = result.text
    except Exception as exc:
        return {
            "spec": spec,
            "error": f"{type(exc).__name__}: {exc}",
            "asr_p50": None,
            "wer": 1.0,
            "text": "",
        }
    finally:
        engine.unload()
    times.sort()
    return {
        "spec": spec,
        "asr_p50": times[len(times) // 2],
        "wer": word_error_rate(reference, text),
        "text": text,
    }


if __name__ == "__main__":
    raise SystemExit(main())
