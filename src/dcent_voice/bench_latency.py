# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Measure local ASR and optional Ollama-compatible latency baselines."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dcent_voice.config import ASRSpec, ConfigError, LLMSpec, load_config
from dcent_voice.local_http import LocalEndpointError, open_local_http
from dcent_voice.util import paths


@dataclass(frozen=True)
class BenchResult:
    """One measured latency stage and its human-readable detail."""

    name: str
    seconds: float
    detail: str = ""


def main(argv: list[str] | None = None) -> int:
    """Run the latency benchmark and return a process-compatible status code."""
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config) if args.config else load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    profile = config.profiles.get(args.profile or config.active_profile)
    if profile is None:
        print(f"Unknown profile: {args.profile!r}", file=sys.stderr)
        return 2

    asr_spec = ASRSpec.parse(args.asr) if args.asr else profile.asr
    llm_spec = LLMSpec.parse(args.ollama_model) if args.ollama_model else profile.llm

    results: list[BenchResult] = []
    if args.no_asr:
        print("ASR benchmark skipped (--no-asr).")
    elif asr_spec.provider != "faster-whisper":
        print(f"ASR benchmark skipped for non-faster-whisper provider: {asr_spec.raw}")
    else:
        try:
            results.extend(run_faster_whisper_bench(asr_spec, args))
        except MissingDependency as exc:
            print(f"ASR benchmark skipped: {exc}")
        except Exception as exc:
            print(f"ASR benchmark failed: {exc}", file=sys.stderr)
            return 1

    if args.ollama or (llm_spec.provider == "ollama" and llm_spec.model):
        model = args.ollama_model or llm_spec.model
        if model:
            result = run_openai_compat_roundtrip(
                base_url=args.ollama_url,
                model=model,
                prompt=args.ollama_prompt,
            )
            if result:
                results.append(result)

    print_results(results)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the shared CLI parser for source and packaged benchmarks."""
    parser = argparse.ArgumentParser(description="Measure DCENT_Voice Wave 0 latency baselines.")
    parser.add_argument("--config", type=Path, default=None, help="config.toml path")
    parser.add_argument("--profile", default=None, help="Profile name from config.toml")
    parser.add_argument(
        "--asr",
        default=None,
        help="Override ASR spec, e.g. faster-whisper:tiny:cpu-int8",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=None,
        help="16 kHz mono WAV to transcribe (default: tests/fixtures/audio/hello.wav)",
    )
    parser.add_argument(
        "--silence",
        action="store_true",
        help="Use 1.2s of silence instead of a speech fixture (not a realistic gate)",
    )
    parser.add_argument("--repeat", type=int, default=3, help="Steady-state ASR repetitions")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup ASR repetitions")
    parser.add_argument("--no-asr", action="store_true", help="Skip ASR timing")
    parser.add_argument(
        "--ollama", action="store_true", help="Force Ollama-compatible LLM round-trip"
    )
    parser.add_argument(
        "--ollama-model",
        default=None,
        help="Ollama model or full LLM spec. If it contains ':', it is treated as an LLM spec.",
    )
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--ollama-prompt", default="Return exactly: ok")
    return parser


class MissingDependency(RuntimeError):
    """Raised when an optional benchmarking dependency is not installed."""


def run_faster_whisper_bench(asr_spec: ASRSpec, args: argparse.Namespace) -> list[BenchResult]:
    """Load an ASR model and measure warm-up plus steady-state transcription."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MissingDependency(
            'install faster-whisper with python -m pip install -e ".[cuda]"'
        ) from exc

    # Share device resolution with the runtime provider so ``auto`` honestly
    # selects CPU int8 when CUDA/cuDNN are incomplete (common on Windows).
    from dcent_voice.asr.faster_whisper_provider import (
        resolve_device_compute as resolve_runtime_device,
    )

    audio, sample_rate, audio_source = load_audio(
        args.audio, force_silence=bool(getattr(args, "silence", False))
    )
    if sample_rate != 16000:
        audio = resample_to_16k(audio, sample_rate)

    device, compute_type = resolve_runtime_device(asr_spec)
    print(
        f"Loading faster-whisper model={asr_spec.model} device={device} "
        f"compute={compute_type} audio={audio_source}"
    )

    start = time.perf_counter()
    try:
        model = WhisperModel(asr_spec.model, device=device, compute_type=compute_type)
    except Exception as exc:
        if device != "cpu" and looks_like_cuda_failure(exc):
            print(f"CUDA load failed ({exc}); falling back to CPU int8.")
            start = time.perf_counter()
            model = WhisperModel(asr_spec.model, device="cpu", compute_type="int8")
            device = "cpu"
            compute_type = "int8"
        else:
            raise
    load_s = time.perf_counter() - start

    results = [BenchResult("model_load", load_s, f"{asr_spec.model} {device}/{compute_type}")]
    # Incomplete Windows CUDA stacks often *load* then fail on first inference
    # (missing cublas64_*.dll). Mirror the runtime provider: fall back once.
    for index in range(args.warmup):
        try:
            elapsed, text = transcribe_once(model, audio)
        except Exception as exc:
            if device != "cpu" and looks_like_cuda_failure(exc):
                print(f"CUDA inference failed ({exc}); falling back to CPU int8.")
                model = WhisperModel(asr_spec.model, device="cpu", compute_type="int8")
                device = "cpu"
                compute_type = "int8"
                results[0] = BenchResult(
                    "model_load",
                    load_s,
                    f"{asr_spec.model} {device}/{compute_type} (cuda-fallback)",
                )
                elapsed, text = transcribe_once(model, audio)
            else:
                raise
        results.append(BenchResult(f"warmup_{index + 1}", elapsed, preview(text)))

    steady: list[float] = []
    for index in range(args.repeat):
        elapsed, text = transcribe_once(model, audio)
        steady.append(elapsed)
        results.append(BenchResult(f"asr_run_{index + 1}", elapsed, preview(text)))

    if steady:
        detail = f"audio_s={len(audio) / 16000:.2f} source={audio_source}"
        if device == "cpu" and (asr_spec.compute_type or "").startswith("cuda"):
            detail += " ENV_BLOCKER_CUDA_INCOMPLETE"
        results.append(BenchResult("asr_median", statistics.median(steady), detail))
    return results


def transcribe_once(model: Any, audio: Any) -> tuple[float, str]:
    """Transcribe one buffer and return elapsed seconds with normalized text."""
    start = time.perf_counter()
    segments, _info = model.transcribe(audio, vad_filter=True, beam_size=1)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return time.perf_counter() - start, text


def load_audio(
    path: Path | None,
    *,
    force_silence: bool = False,
) -> tuple[Any, int, str]:
    """Load a 16-bit PCM WAV fixture, or a silence buffer when requested.

    Default path prefers ``tests/fixtures/audio/hello.wav`` so p50 numbers
    reflect real speech decode work, not VAD-skipped silence (which can report
    absurd sub-millisecond medians and hide CPU cost).
    """
    import numpy as np

    if force_silence:
        return np.zeros(int(16000 * 1.2), dtype=np.float32), 16000, "silence-1.2s"

    candidate = path
    if candidate is None:
        # Source-checkout-only fixture. A frozen build never ships tests/, so
        # fall straight through to synthetic silence instead of guessing a path.
        repo_fixture = (
            None
            if paths.is_frozen()
            else paths.source_root() / "tests" / "fixtures" / "audio" / "hello.wav"
        )
        if repo_fixture is not None and repo_fixture.is_file():
            candidate = repo_fixture
        else:
            return np.zeros(int(16000 * 1.2), dtype=np.float32), 16000, "silence-1.2s-fallback"

    with wave.open(str(candidate), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if sample_width != 2:
        raise ValueError("Only 16-bit PCM WAV files are supported by the stdlib loader.")
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, sample_rate, str(candidate)


def resample_to_16k(audio: Any, sample_rate: int) -> Any:
    """Resample a mono NumPy buffer with lightweight linear interpolation."""
    import numpy as np

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    duration_s = len(audio) / sample_rate
    old_x = np.linspace(0.0, duration_s, num=len(audio), endpoint=False)
    new_len = int(duration_s * 16000)
    new_x = np.linspace(0.0, duration_s, num=new_len, endpoint=False)
    return np.interp(new_x, old_x, audio).astype(np.float32)


def looks_like_cuda_failure(exc: Exception) -> bool:
    """Return whether an ASR load/inference failure plausibly comes from CUDA.

    Delegates to the runtime provider so bench and product stay aligned
    (cublas missing at *inference* is the common incomplete Windows stack).
    """
    from dcent_voice.asr.faster_whisper_provider import (
        looks_like_cuda_failure as runtime_looks_like_cuda_failure,
    )

    return runtime_looks_like_cuda_failure(exc)


def run_openai_compat_roundtrip(
    base_url: str,
    model: str,
    prompt: str,
    *,
    opener: Any | None = None,
) -> BenchResult | None:
    """Measure one optional local Ollama-compatible completion request."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a latency smoke test."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 8,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with open_local_http(request, timeout=20, opener=opener) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (LocalEndpointError, OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"Ollama-compatible round-trip skipped: {exc}")
        return None

    elapsed = time.perf_counter() - start
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    return BenchResult("ollama_roundtrip", elapsed, preview(text))


def preview(text: str, limit: int = 80) -> str:
    """Compress long benchmark output into a stable one-line preview."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def print_results(results: list[BenchResult]) -> None:
    """Print human-readable benchmark results to standard output."""
    print()
    print("DCENT_Voice latency bench")
    if not results:
        print("No timings recorded.")
        return

    name_width = max(len(result.name) for result in results)
    print(f"{'stage'.ljust(name_width)}  seconds  detail")
    print(f"{'-' * name_width}  -------  ------")
    for result in results:
        print(f"{result.name.ljust(name_width)}  {result.seconds:7.3f}  {result.detail}")
