# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Benchmark real Windows injection into DCENT_Voice's private native target.

The reported native backend timings cover OS injection through target acknowledgement.
``native_replace`` is verified target-bound ``EM_REPLACESEL`` and ``native_paste`` is a
clipboard transaction followed by verified target-bound ``WM_PASTE``. A separately labeled
``unicode_sendinput_probe`` validates the global ``KEYEVENTF_UNICODE`` fallback.
They intentionally exclude microphone capture and ASR.  Passing ``--executable``
runs the exact frozen binary rather than importing source modules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--executable",
        type=Path,
        default=None,
        help="Exact frozen dcent-voice executable. Default: current source via python -m.",
    )
    parser.add_argument(
        "--runs", type=int, default=10, help="Runs per measured native backend (1-100)."
    )
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_benchmark(
    *, executable: Path | None, runs: int, timeout_s: float
) -> tuple[int, dict[str, Any]]:
    if runs < 1 or runs > 100:
        raise ValueError("runs must be between 1 and 100")
    if timeout_s <= 0:
        raise ValueError("timeout-s must be positive")
    executable_path = executable.resolve() if executable is not None else None
    if executable_path is not None and not executable_path.is_file():
        raise FileNotFoundError(f"Frozen executable does not exist: {executable_path}")

    with tempfile.TemporaryDirectory(prefix="dcent-voice-injection-bench-") as directory:
        child_report = Path(directory) / "child-report.json"
        command = (
            [str(executable_path)]
            if executable_path is not None
            else [sys.executable, "-m", "dcent_voice"]
        )
        command.extend(
            [
                "injection-self-test",
                "--runs",
                str(runs),
                "--output-json",
                str(child_report),
            ]
        )
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
        process_ms = (time.perf_counter() - started) * 1000.0
        if not child_report.is_file():
            return completed.returncode or 1, {
                "schema_version": 2,
                "status": "error",
                "error": "injection command did not produce its required JSON report",
                "diagnostic": {
                    "stage": "child_report_contract",
                    "error": "injection command did not produce its required JSON report",
                },
                "process_exit_code": completed.returncode,
                "process_wall_ms": round(process_ms, 3),
            }
        report = json.loads(child_report.read_text(encoding="utf-8"))

    report["benchmark_process"] = {
        "mode": "frozen-executable" if executable_path is not None else "source-process",
        "executable": str(executable_path or Path(sys.executable).resolve()),
        "executable_sha256": _sha256(executable_path) if executable_path is not None else None,
        "exit_code": completed.returncode,
        "wall_ms": round(process_ms, 3),
    }
    return completed.returncode, report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, report = run_benchmark(
            executable=args.executable,
            runs=args.runs,
            timeout_s=args.timeout_s,
        )
    except Exception as exc:
        report = {
            "schema_version": 2,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "diagnostic": {
                "stage": "benchmark_dispatch",
                "error": f"{type(exc).__name__}: {exc}",
            },
        }
        exit_code = 1
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output_json is None:
        print(rendered)
    else:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered + "\n", encoding="utf-8")
    return 0 if exit_code == 0 and report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
