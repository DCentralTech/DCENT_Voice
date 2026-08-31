# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Run a packaging/smoke subprocess with a hard deadline and process-tree cleanup."""

from __future__ import annotations

import argparse
import subprocess
import sys

from dcent_voice.util.owned_process import start_owned_process, terminate_owned_process


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if args.timeout <= 0 or not command:
        print("run_bounded requires a positive timeout and a command", file=sys.stderr)
        return 2
    process = start_owned_process(command)
    try:
        return process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        terminate_owned_process(process, grace_s=1.0, kill_s=5.0)
        print(
            f"command timed out after {args.timeout:g}s and was terminated: {command[0]}",
            file=sys.stderr,
        )
        return 124
    finally:
        # Close the Job Object/session even after a normal exit so descendants
        # cannot outlive the bounded command.
        terminate_owned_process(process, grace_s=0.0, kill_s=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
