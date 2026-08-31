# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Benchmark shipped injection in isolated real Windows applications.

Targets are fresh scratch documents/profiles for Notepad, VS Code, a controlled console,
Edge, and Chrome.  The measurement starts after focus and excludes microphone and ASR.
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
    parser.add_argument("--executable", type=Path, default=None)
    parser.add_argument("--apps", default="all")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout-s", type=float, default=300.0)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_benchmark(
    *, executable: Path | None, apps: str, runs: int, timeout_s: float
) -> tuple[int, dict[str, Any]]:
    if runs < 1 or runs > 20:
        raise ValueError("runs must be between 1 and 20")
    if timeout_s <= 0:
        raise ValueError("timeout-s must be positive")
    frozen = executable.resolve() if executable is not None else None
    if frozen is not None and not frozen.is_file():
        raise FileNotFoundError(f"Frozen executable does not exist: {frozen}")
    with tempfile.TemporaryDirectory(prefix="dcent-voice-app-bench-dispatch-") as directory:
        child_report = Path(directory) / "report.json"
        command = [str(frozen)] if frozen is not None else [sys.executable, "-m", "dcent_voice"]
        command.extend(
            [
                "injection-app-matrix",
                "--apps",
                apps,
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
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        wall_ms = (time.perf_counter() - started) * 1000.0
        if not child_report.is_file():
            return completed.returncode or 1, {
                "schema_version": 1,
                "status": "error",
                "diagnostic": {
                    "stage": "child_report_contract",
                    "error": completed.stderr[-2000:],
                },
            }
        report = json.loads(child_report.read_text(encoding="utf-8"))
    report["benchmark_process"] = {
        "mode": "frozen-executable" if frozen is not None else "source-process",
        "executable": str(frozen or Path(sys.executable).resolve()),
        "executable_sha256": _sha256(frozen) if frozen is not None else None,
        "exit_code": completed.returncode,
        "wall_ms": round(wall_ms, 3),
    }
    return completed.returncode, report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code, report = run_benchmark(
            executable=args.executable,
            apps=args.apps,
            runs=args.runs,
            timeout_s=args.timeout_s,
        )
    except Exception as exc:
        code = 1
        report = {
            "schema_version": 1,
            "status": "error",
            "diagnostic": {
                "stage": "benchmark_dispatch",
                "error": f"{type(exc).__name__}: {exc}",
            },
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json is None:
        print(rendered, end="")
    else:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8")
    return 0 if code == 0 and report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
