#!/usr/bin/env python3
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Run the isolated real Windows OS-loop hold/release probe.

This plays committed public speech through an explicit OS output endpoint and
changes only ``audio.input_device`` in a temporary copy of the bundled default
config. It is not evidence for the system-default microphone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_for_executable(executable: Path | None) -> Path:
    if executable is None:
        return ROOT / "config.example.toml"
    candidate = executable.parent / "_internal" / "config.example.toml"
    if not candidate.is_file():
        raise FileNotFoundError(f"frozen bundled default config missing: {candidate}")
    return candidate


def _write_isolated_config(source: Path, destination: Path, input_device: str) -> None:
    text = source.read_text(encoding="utf-8")
    if input_device.strip().lower() in {"", "default", "none"}:
        replacement = 'input_device = ""'
    else:
        replacement = (
            f'input_device = "{input_device}"'
            if not input_device.lstrip("-").isdigit()
            else f"input_device = {int(input_device)}"
        )
    updated, count = re.subn(
        r"(?m)^input_device\s*=\s*.*$",
        replacement,
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("bundled default config has no unique audio.input_device")
    destination.write_text(updated, encoding="utf-8")


def _command(executable: Path | None) -> list[str]:
    if executable is not None:
        return [str(executable)]
    return ["uv", "run", "dcent-voice"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, default=None)
    parser.add_argument(
        "--input-device",
        required=True,
        help="PortAudio input index/name, or 'default' for the OS default microphone.",
    )
    parser.add_argument("--output-device", required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--apps",
        default="all",
        help="Comma-separated isolated targets, or all.",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument(
        "--real-documents",
        action="store_true",
        help="Inject into existing Notepad/VS Code/browser documents.",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="AUDIO|REFERENCE",
        help="Repeatable fixture/reference pair; defaults to committed EN and CC0 FR.",
    )
    args = parser.parse_args()
    executable = args.executable.resolve() if args.executable else None
    if executable is not None and not executable.is_file():
        parser.error(f"executable not found: {executable}")
    source_config = _config_for_executable(executable)
    cases = args.case or [
        "tests/fixtures/audio/hello.wav|Hello world.",
        ("tests/fixtures/audio/eval/multilingual/fr-je-mappelle.wav|Je m'appelle."),
    ]
    parsed_cases: list[tuple[Path, str]] = []
    for item in cases:
        audio_raw, separator, reference = item.partition("|")
        if not separator or not reference:
            parser.error(f"invalid --case {item!r}; expected AUDIO|REFERENCE")
        audio = Path(audio_raw)
        if not audio.is_absolute():
            audio = ROOT / audio
        if not audio.is_file():
            parser.error(f"audio fixture missing: {audio}")
        parsed_cases.append((audio.resolve(), reference))

    default_mic = args.input_device.strip().lower() in {"", "default", "none"}
    result: dict[str, Any] = {
        "schema": "dcent-hold-release-benchmark-v1",
        "scope": (
            "system_default_microphone_acoustic_capture"
            if default_mic
            else "explicit_os_loop_device_override_not_default_microphone"
        ),
        "executable": str(executable) if executable else "source:uv-run-dcent-voice",
        "executable_sha256": _hash(executable) if executable else None,
        "bundled_default_config": str(source_config.resolve()),
        "bundled_default_config_sha256": _hash(source_config),
        "only_config_override": {"audio.input_device": "" if default_mic else args.input_device},
        "output_device_fixture_route": args.output_device,
        "apps": args.apps,
        "real_documents": args.real_documents,
        "cases": [],
        "errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="dcent-hold-release-config-") as raw_root:
        root = Path(raw_root)
        isolated_config = root / "config.toml"
        _write_isolated_config(source_config, isolated_config, args.input_device)
        result["isolated_config_sha256"] = _hash(isolated_config)
        for index, (audio, reference) in enumerate(parsed_cases):
            report_path = root / f"case-{index}.json"
            command = [
                *_command(executable),
                "--config",
                str(isolated_config),
                "hold-release-self-test",
                "--audio",
                str(audio),
                "--reference",
                reference,
                "--output-device",
                args.output_device,
                "--runs",
                str(args.runs),
                "--apps",
                args.apps,
                "--output-json",
                str(report_path),
            ]
            if default_mic:
                command.append("--allow-default-microphone")
            if args.real_documents:
                command.append("--real-documents")
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=max(300 if args.real_documents else 180, args.runs * 90),
                check=False,
            )
            case_result: dict[str, Any]
            if report_path.is_file():
                case_result = json.loads(report_path.read_text(encoding="utf-8"))
            else:
                case_result = {
                    "exact": False,
                    "errors": ["probe produced no JSON report"],
                }
            case_result["process_returncode"] = completed.returncode
            case_result["stderr_tail"] = completed.stderr[-4000:]
            result["cases"].append(case_result)
            if completed.returncode != 0 or not case_result.get("exact"):
                result["errors"].append(f"case {index} failed")

    result["exact"] = not result["errors"]
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if result["exact"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
