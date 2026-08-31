# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Measure streaming first-partial and stable-partial latency on real speech.

This is a source-warm engineering diagnostic of the desktop streaming cadence
(peek wait + ASR + IncrementalCommitter). It is not user-perceived E2E: there
is no microphone, overlay, or foreground-app injection in the timed path.

Never pass silence. Never substitute a tiny/distil model for the shipped
default. ASR-only numbers are labeled separately from product-cadence numbers.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.attach.registry import write_text_atomic  # noqa: E402
from dcent_voice.config import load_config  # noqa: E402
from dcent_voice.engine import VoiceEngine, load_wav_mono  # noqa: E402
from dcent_voice.pipeline import (  # noqa: E402
    IncrementalCommitter,
    PipelineConfig,
    stream_pass_wait_s,
)

SHIPPED_ASR = "parakeet:tdt-0.6b-v3:int8"
FORBIDDEN_ASR = ("tiny", "distil", "base.en")
SILENCE_RMS = 0.005
DEFAULT_CLIPS = (
    ROOT / "tests" / "fixtures" / "audio" / "hello.wav",
    ROOT / "tests" / "fixtures" / "audio" / "eval" / "conversational.wav",
)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, round((q / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    finite = [float(v) for v in values if v == v]  # drop NaN
    if not finite:
        return {"p50_s": float("nan"), "p95_s": float("nan"), "n": 0.0}
    return {
        "p50_s": statistics.median(finite),
        "p95_s": percentile(finite, 95),
        "n": float(len(finite)),
    }


def rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))


def require_speech(audio: np.ndarray, path: Path) -> None:
    level = rms(audio)
    if level < SILENCE_RMS:
        raise ValueError(
            f"{path} looks like silence (rms={level:.6f} < {SILENCE_RMS}); "
            "streaming benches must use real speech"
        )


def require_shipped_asr(spec: str) -> None:
    normalized = spec.strip().lower()
    if any(token in normalized for token in FORBIDDEN_ASR):
        raise ValueError(f"refusing tiny/distil/base.en stand-in: {spec}")
    if normalized != SHIPPED_ASR:
        raise ValueError(f"refusing ASR {spec!r}; shipped default is {SHIPPED_ASR}")


def word_rewrite(previous: str, current: str) -> bool:
    """True when the new partial abandons the previous committed-looking prefix."""
    if not previous or not current:
        return False
    prev_words = previous.split()
    cur_words = current.split()
    if not prev_words:
        return False
    shared = 0
    for left, right in zip(prev_words, cur_words, strict=False):
        if left != right:
            break
        shared += 1
    return shared < min(len(prev_words), len(cur_words)) and shared < len(prev_words)


def legacy_pipeline_config() -> PipelineConfig:
    return PipelineConfig(
        stream_interval_s=1.0,
        stream_first_peek_s=1.0,
        stream_min_audio_s=0.4,
        stream_agreement_passes=3,
        stream_first_agreement_passes=3,
        local_polish=False,
        spoken_edits=False,
        developer_terms=False,
    )


def shipped_pipeline_config() -> PipelineConfig:
    return PipelineConfig(local_polish=False, spoken_edits=False, developer_terms=False)


def simulate_streaming_cadence(
    *,
    audio: np.ndarray,
    samplerate: int,
    transcribe,
    config: PipelineConfig,
    sleep: Any = time.sleep,
    clock: Any = time.perf_counter,
) -> dict[str, Any]:
    """Replay product peek cadence against a growing realtime buffer.

    ``transcribe(window) -> str`` is the ASR-or-fake hook. Wall time includes
    peek waits and ASR. Injection is not timed.
    """
    duration_s = len(audio) / float(samplerate)
    committer = IncrementalCommitter(
        agreement_passes=config.stream_agreement_passes,
        first_agreement_passes=config.stream_first_agreement_passes,
    )
    t0 = clock()
    next_at = t0
    first = True
    first_partial_s: float | None = None
    first_stable_s: float | None = None
    last_partial = ""
    rewrite_count = 0
    passes = 0
    committed = ""
    last_text = ""

    while True:
        wait_s = stream_pass_wait_s(config, first=first)
        first = False
        next_at += wait_s
        now = clock()
        if next_at > now:
            sleep(next_at - now)
        available_s = min(duration_s, clock() - t0)
        n = max(0, int(available_s * samplerate))
        if n / float(samplerate) < float(config.stream_min_audio_s):
            if available_s >= duration_s - 1e-9:
                break
            continue
        window = audio[:n]
        text = str(transcribe(window) or "")
        elapsed = clock() - t0
        passes += 1
        last_text = text
        if text and first_partial_s is None:
            first_partial_s = elapsed
        if word_rewrite(last_partial, text):
            rewrite_count += 1
        if text:
            last_partial = text
        delta = committer.update(text)
        if delta:
            committed = (committed + " " + delta).strip() if committed else delta
            if first_stable_s is None:
                first_stable_s = elapsed
        if available_s >= duration_s - 1e-9:
            break

    return {
        "audio_duration_s": duration_s,
        "first_partial_s": first_partial_s,
        "first_stable_s": first_stable_s,
        "rewrite_count": rewrite_count,
        "passes": passes,
        "committed": committed,
        "last_partial": last_text,
    }


def hold_to_talk_finalize(
    *,
    audio: np.ndarray,
    transcribe,
    clock: Any = time.perf_counter,
) -> dict[str, Any]:
    started = clock()
    text = str(transcribe(audio) or "")
    return {"hold_to_talk_finalize_s": clock() - started, "text": text}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure streaming first-partial / stable-partial on real speech."
    )
    parser.add_argument(
        "--audio",
        type=Path,
        action="append",
        default=None,
        help="Speech WAV (repeatable). Defaults to hello.wav and conversational.wav.",
    )
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Atomically persist the complete benchmark report.",
    )
    return parser


def _engine_transcribe(engine: VoiceEngine):
    def _call(window: np.ndarray) -> str:
        result = engine.transcribe(window, samplerate=16000, polish=False)
        if result.rejected_reason:
            return ""
        return result.raw or result.text or ""

    return _call


def benchmark_source_warm(
    *,
    audio_paths: list[Path],
    repeat: int,
    config_path: Path | None = None,
    include_legacy: bool = True,
) -> dict[str, Any]:
    if repeat < 1:
        raise ValueError("--repeat must be at least 1")
    source = config_path or ROOT / "config.example.toml"
    config = load_config(source, create=False)
    spec = str(config.current_profile.asr.raw)
    require_shipped_asr(spec)
    engine = VoiceEngine(config)
    engine.load()
    try:
        return _benchmark_loaded(
            engine=engine,
            spec=spec,
            audio_paths=audio_paths,
            repeat=repeat,
            include_legacy=include_legacy,
        )
    finally:
        engine.unload()


def _benchmark_loaded(
    *,
    engine: VoiceEngine,
    spec: str,
    audio_paths: list[Path],
    repeat: int,
    include_legacy: bool,
) -> dict[str, Any]:
    transcribe = _engine_transcribe(engine)
    shipped_cfg = shipped_pipeline_config()
    legacy_cfg = legacy_pipeline_config()

    clips: list[dict[str, Any]] = []
    for audio_path in audio_paths:
        wav = audio_path.expanduser().resolve()
        if not wav.is_file():
            raise FileNotFoundError(f"audio fixture not found: {wav}")
        audio, rate = load_wav_mono(wav)
        if rate != 16000:
            raise ValueError(f"{wav} must be 16 kHz; got {rate}")
        require_speech(audio, wav)
        # Discard the first transcribe as model warmup so cadence numbers are
        # keep-warm, not first-load.
        transcribe(audio[: min(len(audio), int(0.5 * rate))])

        shipped_samples = _run_cadence_samples(
            audio=audio,
            samplerate=rate,
            transcribe=transcribe,
            config=shipped_cfg,
            repeat=repeat,
        )
        clip: dict[str, Any] = {
            "path": str(wav),
            "audio_duration_s": len(audio) / float(rate),
            "rms": rms(audio),
            "shipped": _clip_summary(shipped_samples),
            "samples_shipped": shipped_samples,
        }
        if include_legacy:
            legacy_samples = _run_cadence_samples(
                audio=audio,
                samplerate=rate,
                transcribe=transcribe,
                config=legacy_cfg,
                repeat=repeat,
            )
            clip["legacy"] = _clip_summary(legacy_samples)
            clip["samples_legacy"] = legacy_samples
        clips.append(clip)

    report = {
        "label": "source_warm_streaming_cadence",
        "user_perceived_e2e": False,
        "includes_microphone": False,
        "includes_injection": False,
        "asr_only_unlabeled": False,
        "shipped_asr": spec,
        "model": SHIPPED_ASR,
        "cadence": {
            "stream_first_peek_s": shipped_cfg.stream_first_peek_s,
            "stream_interval_s": shipped_cfg.stream_interval_s,
            "stream_min_audio_s": shipped_cfg.stream_min_audio_s,
            "stream_agreement_passes": shipped_cfg.stream_agreement_passes,
            "stream_first_agreement_passes": shipped_cfg.stream_first_agreement_passes,
        },
        "legacy_cadence": {
            "stream_first_peek_s": legacy_cfg.stream_first_peek_s,
            "stream_interval_s": legacy_cfg.stream_interval_s,
            "stream_min_audio_s": legacy_cfg.stream_min_audio_s,
            "stream_agreement_passes": legacy_cfg.stream_agreement_passes,
            "stream_first_agreement_passes": legacy_cfg.stream_first_agreement_passes,
        },
        "repeat": repeat,
        "clips": clips,
        "aggregate": _aggregate(clips, "shipped"),
    }
    if include_legacy:
        report["aggregate_legacy"] = _aggregate(clips, "legacy")
    return report


def _run_cadence_samples(
    *,
    audio: np.ndarray,
    samplerate: int,
    transcribe,
    config: PipelineConfig,
    repeat: int,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for _ in range(repeat):
        stream = simulate_streaming_cadence(
            audio=audio,
            samplerate=samplerate,
            transcribe=transcribe,
            config=config,
        )
        finalize = hold_to_talk_finalize(audio=audio, transcribe=transcribe)
        samples.append({**stream, **finalize})
    return samples


def _clip_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "first_partial": _summary(
            [s["first_partial_s"] for s in samples if s.get("first_partial_s") is not None]
        ),
        "first_stable": _summary(
            [s["first_stable_s"] for s in samples if s.get("first_stable_s") is not None]
        ),
        "hold_to_talk_finalize": _summary([s["hold_to_talk_finalize_s"] for s in samples]),
        "rewrite_count_mean": (
            statistics.mean(float(s["rewrite_count"]) for s in samples) if samples else 0.0
        ),
        "stable_reached": sum(1 for s in samples if s.get("first_stable_s") is not None),
        "n": len(samples),
    }


def _aggregate(clips: list[dict[str, Any]], key: str) -> dict[str, Any]:
    first_partial: list[float] = []
    first_stable: list[float] = []
    finalize: list[float] = []
    for clip in clips:
        samples = clip.get(f"samples_{key}") or []
        for sample in samples:
            if sample.get("first_partial_s") is not None:
                first_partial.append(float(sample["first_partial_s"]))
            if sample.get("first_stable_s") is not None:
                first_stable.append(float(sample["first_stable_s"]))
            finalize.append(float(sample["hold_to_talk_finalize_s"]))
    return {
        "first_partial": _summary(first_partial),
        "first_stable": _summary(first_stable),
        "hold_to_talk_finalize": _summary(finalize),
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"label: {report['label']}")
    print("scope: source-warm streaming cadence; NOT user-perceived E2E")
    print(f"asr: {report['shipped_asr']}")
    agg = report["aggregate"]
    print(
        "shipped first-partial "
        f"p50={agg['first_partial']['p50_s']:.3f}s "
        f"p95={agg['first_partial']['p95_s']:.3f}s"
    )
    print(
        "shipped stable-partial "
        f"p50={agg['first_stable']['p50_s']:.3f}s "
        f"p95={agg['first_stable']['p95_s']:.3f}s"
    )
    print(
        "hold-to-talk finalize (ASR-only, labeled) "
        f"p50={agg['hold_to_talk_finalize']['p50_s']:.3f}s "
        f"p95={agg['hold_to_talk_finalize']['p95_s']:.3f}s"
    )
    if "aggregate_legacy" in report:
        legacy = report["aggregate_legacy"]
        print(
            "legacy first-partial "
            f"p50={legacy['first_partial']['p50_s']:.3f}s "
            f"p95={legacy['first_partial']['p95_s']:.3f}s"
        )
        print(
            "legacy stable-partial "
            f"p50={legacy['first_stable']['p50_s']:.3f}s "
            f"p95={legacy['first_stable']['p95_s']:.3f}s"
        )
    for clip in report["clips"]:
        shipped = clip["shipped"]
        print(
            f"  {Path(clip['path']).name} dur={clip['audio_duration_s']:.2f}s "
            f"first-partial p50={shipped['first_partial']['p50_s']:.3f}s "
            f"stable p50={shipped['first_stable']['p50_s']:.3f}s "
            f"rewrites={shipped['rewrite_count_mean']:.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    audio_paths = args.audio or list(DEFAULT_CLIPS)
    try:
        report = benchmark_source_warm(
            audio_paths=audio_paths,
            repeat=args.repeat,
            config_path=args.config,
            include_legacy=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)
    if args.output_json:
        write_text_atomic(
            args.output_json,
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            require_private=False,
        )
    return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
