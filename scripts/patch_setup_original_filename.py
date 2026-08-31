#!/usr/bin/env python3
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Correct .NET apphost's managed-DLL OriginalFilename before SFX packing."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def patch(path: Path) -> None:
    source = "DCENT_Voice-Setup.dll".encode("utf-16le")
    replacement = "DCENT_Voice-Setup.exe".encode("utf-16le")
    if len(source) != len(replacement):  # pragma: no cover - invariant
        raise ValueError("version-resource replacement must remain size-neutral")
    payload = path.read_bytes()
    label = "OriginalFilename".encode("utf-16le")
    positions: list[int] = []
    offset = 0
    while (candidate := payload.find(source, offset)) >= 0:
        if label in payload[max(0, candidate - 96) : candidate]:
            positions.append(candidate)
        offset = candidate + len(source)
    if not positions:
        existing = "DCENT_Voice-Setup.exe".encode("utf-16le")
        if any(
            label in payload[max(0, candidate - 96) : candidate]
            for candidate in _positions(payload, existing)
        ):
            return
        raise ValueError("Setup apphost has no OriginalFilename resource")
    updated = bytearray(payload)
    for position in positions:
        updated[position : position + len(source)] = replacement
    temporary = path.with_name(f".{path.name}.{os.getpid()}.version.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _positions(payload: bytes, value: bytes) -> list[int]:
    found: list[int] = []
    offset = 0
    while (candidate := payload.find(value, offset)) >= 0:
        found.append(candidate)
        offset = candidate + len(value)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        patch(args.path)
    except (OSError, ValueError) as exc:
        print(f"Setup version-resource patch failed: {exc}", file=sys.stderr)
        return 1
    print(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
