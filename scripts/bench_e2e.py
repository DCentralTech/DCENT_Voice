# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Benchmark explicitly scoped dictation paths without overstating product latency.

``--executable`` launches a new frozen executable for every sample and measures
wall time outside that process. It covers the frozen bootloader through model
teardown, but not microphone capture or real foreground-app injection.

``--source-warm`` is an engineering diagnostic: one source-process engine is
warmed, then ASR/postprocess and deliberately simulated injection are timed. It
is not shipped-product or user-perceived end-to-end evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.attach.registry import write_text_atomic  # noqa: E402
from dcent_voice.config import ASRSpec, load_config  # noqa: E402
from dcent_voice.engine import VoiceEngine, load_wav_mono  # noqa: E402
from dcent_voice.inject.base import Injector  # noqa: E402
from dcent_voice.inject.clipboard import ClipboardPasteInjector  # noqa: E402
from dcent_voice.inject.router import RoutingInjector  # noqa: E402


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, round((q / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_s": statistics.median(values),
        "p95_s": percentile(values, 95),
        "p99_s": percentile(values, 99),
    }


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure either frozen-cold headless or source-warm simulated paths."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--executable",
        type=Path,
        help="Frozen dcent-voice executable; a fresh process is launched for every run.",
    )
    mode.add_argument(
        "--source-warm",
        action="store_true",
        help="Warm source-engine diagnostic with simulated injection (not product E2E).",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "audio" / "hello.wav",
    )
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--asr", default=None, help="Source-warm ASR override only.")
    parser.add_argument("--timeout", type=float, default=300.0, help="Per cold process timeout.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Atomically persist the complete benchmark report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.executable is not None and args.asr:
        parser.error("--asr is not allowed with --executable; benchmark the artifact's default")

    try:
        if args.executable is not None:
            report = benchmark_frozen_cold(
                executable=args.executable,
                audio=args.audio,
                repeat=args.repeat,
                config_path=args.config,
                timeout_s=args.timeout,
            )
        else:
            report = benchmark_source_warm(
                audio_path=args.audio,
                repeat=args.repeat,
                config_path=args.config,
                asr_override=args.asr,
            )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        write_text_atomic(args.output_json, serialized, require_private=False)
    if args.json:
        print(serialized, end="")
    else:
        print_report(report)
    return 0


def benchmark_frozen_cold(
    *,
    executable: Path,
    audio: Path,
    repeat: int,
    config_path: Path | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Measure complete fresh-process wall time for a real frozen artifact."""

    exe = executable.expanduser().resolve()
    wav = audio.expanduser().resolve()
    if not exe.is_file():
        raise FileNotFoundError(f"frozen executable not found: {exe}")
    if not wav.is_file():
        raise FileNotFoundError(f"audio fixture not found: {wav}")
    config = _resolve_frozen_config(exe, config_path)
    executable_identity = _file_identity(exe)

    samples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dcent-voice-cold-bench-") as temp_dir:
        temp = Path(temp_dir)
        for index in range(repeat):
            output = temp / f"result-{index}.json"
            command = [str(exe)]
            if config is not None:
                command.extend(["--config", str(config)])
            command.extend(
                [
                    "transcribe",
                    str(wav),
                    "--output-json",
                    str(output),
                ]
            )
            started = time.perf_counter()
            completed = subprocess.run(
                command,
                cwd=exe.parent,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            wall_s = time.perf_counter() - started
            if not output.is_file():
                detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
                raise RuntimeError(
                    f"cold run {index + 1} returned {completed.returncode} without JSON: {detail}"
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
            measurement = payload.get("cli_measurement")
            if not isinstance(measurement, dict) or measurement.get("frozen") is not True:
                raise RuntimeError(
                    "the target did not identify itself as a frozen executable with CLI timings"
                )
            rejected = str(payload.get("rejected_reason") or "")
            if completed.returncode != 0 and not rejected:
                raise RuntimeError(f"cold run {index + 1} returned {completed.returncode}")
            timings = payload.get("timings") or {}
            samples.append(
                {
                    "process_wall_s": wall_s,
                    "audio_load_s": float(measurement.get("audio_load_s", 0.0)),
                    "model_load_s": float(measurement.get("model_load_s", 0.0)),
                    "asr_s": float(timings.get("asr", payload.get("asr_latency_s", 0.0))),
                    "postprocess_s": float(timings.get("postprocess", 0.0)),
                    "transcribe_s": float(measurement.get("transcribe_s", 0.0)),
                    "unload_s": float(measurement.get("unload_s", 0.0)),
                    "returncode": completed.returncode,
                    "rejected_reason": rejected,
                    "text": str(payload.get("text") or ""),
                    "provider": str(payload.get("provider") or ""),
                    "model": str(payload.get("model") or ""),
                }
            )

    def values(name: str) -> list[float]:
        return [float(sample[name]) for sample in samples]

    if _file_identity(exe) != executable_identity:
        raise RuntimeError("measured executable changed during the cold benchmark")

    return {
        "schema_version": 1,
        "label": "frozen_cold_headless_transcription",
        "measurement_scope": "fresh frozen process per run",
        "shipped_artifact_measured": True,
        "cold_process": True,
        "frozen_verified": True,
        "user_perceived_e2e": False,
        "includes": [
            "process launch",
            "frozen bootloader and imports",
            "config and WAV load",
            "model load",
            "ASR and postprocess",
            "JSON serialization and model teardown",
        ],
        "excludes": ["microphone capture", "foreground-app text injection"],
        "executable": str(exe),
        "executable_bytes": executable_identity[0],
        "executable_sha256": executable_identity[1],
        "config": str(config) if config is not None else "artifact-internal",
        "bundled_default_config": config is None or _is_bundled_config(exe, config),
        "audio": str(wav),
        "runs": repeat,
        "provider": samples[-1]["provider"],
        "model": samples[-1]["model"],
        "text": samples[-1]["text"],
        "process_wall": _summary(values("process_wall_s")),
        "model_load": _summary(values("model_load_s")),
        "asr": _summary(values("asr_s")),
        "postprocess": _summary(values("postprocess_s")),
        "transcribe": _summary(values("transcribe_s")),
        "samples": samples,
    }


def benchmark_source_warm(
    *,
    audio_path: Path,
    repeat: int,
    config_path: Path | None = None,
    asr_override: str | None = None,
) -> dict[str, Any]:
    """Measure a warmed source engine plus non-mutating simulated injection."""

    source = config_path or ROOT / "config.example.toml"
    config = load_config(source, create=False)
    if asr_override:
        profile = replace(config.current_profile, asr=ASRSpec.parse(asr_override))
        profiles = dict(config.profiles)
        profiles[config.active_profile] = profile
        config = replace(config, profiles=profiles)

    audio, samplerate = load_wav_mono(audio_path)
    engine = VoiceEngine(config)
    injector = _bench_injector(config)
    try:
        engine.load()
        engine.transcribe(audio, samplerate=samplerate)
        samples: list[dict[str, float]] = []
        last_text = ""
        for _ in range(repeat):
            started = time.perf_counter()
            result = engine.transcribe(audio, samplerate=samplerate)
            last_text = result.text
            inject_started = time.perf_counter()
            injector.inject(result.text or " ")
            inject_s = time.perf_counter() - inject_started
            samples.append(
                {
                    "asr_s": float(result.timings.get("asr", result.asr_latency_s)),
                    "postprocess_s": float(result.timings.get("postprocess", 0.0)),
                    "simulated_injection_s": inject_s,
                    "warm_pipeline_s": time.perf_counter() - started,
                }
            )
    finally:
        engine.unload()

    def values(name: str) -> list[float]:
        return [float(sample[name]) for sample in samples]

    return {
        "schema_version": 1,
        "label": "source_warm_engine_simulated_injection",
        "measurement_scope": "warmed source process and simulated injection",
        "shipped_artifact_measured": False,
        "cold_process": False,
        "user_perceived_e2e": False,
        "includes": ["warm ASR", "postprocess", "configured delay and simulated injection"],
        "excludes": [
            "process startup",
            "model load",
            "microphone capture",
            "real OS clipboard or keystrokes",
        ],
        "asr_spec": config.current_profile.asr.raw,
        "audio": str(audio_path),
        "audio_s": len(audio) / float(samplerate),
        "text": last_text,
        "runs": repeat,
        "asr": _summary(values("asr_s")),
        "postprocess": _summary(values("postprocess_s")),
        "simulated_injection": _summary(values("simulated_injection_s")),
        "warm_pipeline": _summary(values("warm_pipeline_s")),
        "samples": samples,
    }


def _resolve_frozen_config(executable: Path, configured: Path | None) -> Path | None:
    if configured is not None:
        resolved = configured.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"config not found: {resolved}")
        return resolved
    candidates = (
        executable.parent / "_internal" / "config.example.toml",
        executable.parent / "config.example.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    if executable.suffix.casefold() == ".appimage":
        # AppImages mount their bundled config only inside the launched process.
        # Omitting --config exercises that immutable artifact-internal default.
        return None
    raise FileNotFoundError(
        "bundled config.example.toml not found beside executable; pass --config explicitly"
    )


def _is_bundled_config(executable: Path, config: Path) -> bool:
    return config in {
        (executable.parent / "_internal" / "config.example.toml").resolve(),
        (executable.parent / "config.example.toml").resolve(),
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"DCENT_Voice benchmark: {report['label']}")
    print(f"scope        {report['measurement_scope']}")
    print(f"artifact     {report['shipped_artifact_measured']}")
    print(f"user E2E     {report['user_perceived_e2e']}")
    if report["shipped_artifact_measured"]:
        print(f"executable   {report['executable']}")
        print(f"model        {report['provider']}:{report['model']}")
        wall = report["process_wall"]
        load = report["model_load"]
        asr = report["asr"]
        print(
            f"process_wall p50 {wall['p50_s']:.3f}  p95 {wall['p95_s']:.3f}  "
            f"p99 {wall['p99_s']:.3f}"
        )
        print(f"model_load   p50 {load['p50_s']:.3f}")
        print(f"asr          p50 {asr['p50_s']:.3f}  p95 {asr['p95_s']:.3f}")
    else:
        warm = report["warm_pipeline"]
        print(f"asr_spec     {report['asr_spec']}")
        print(
            f"warm_pipeline p50 {warm['p50_s']:.3f}  p95 {warm['p95_s']:.3f}  "
            f"p99 {warm['p99_s']:.3f}"
        )
    print(f"text         {report['text']!r}")
    print("excluded     " + ", ".join(report["excludes"]))


def _bench_injector(config) -> RoutingInjector:
    clipboard = ClipboardPasteInjector(
        restore_previous=False,
        paste_delay_s=config.injector.paste_delay_s,
        paste_min_delay_s=config.injector.paste_min_delay_s,
    )
    clipboard.send = lambda: None  # type: ignore[attr-defined]
    import dcent_voice.inject.clipboard as clipboard_mod

    clipboard_mod.send_ctrl_v = lambda: None
    clipboard_mod.set_clipboard_text = lambda *_args, **_kwargs: None
    return RoutingInjector(
        default_name=config.injector.default,
        injectors={"clipboard": clipboard, "keystroke": _TimedKeystroke()},
        short_text_keystroke_chars=config.injector.short_text_keystroke_chars,
        process_name_fn=lambda: "notepad.exe",
    )


class _TimedKeystroke(Injector):
    def inject(self, text: str) -> None:
        time.sleep(min(0.08, 0.002 * max(1, len(text))))

    def retract(self, char_count: int) -> None:
        return None

    def press_enter(self) -> None:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
