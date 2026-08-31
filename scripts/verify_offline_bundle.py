# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Fail-closed, standard-library verification for offline wheel bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath

SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_manifest(manifest_path: Path) -> tuple[bool, str]:
    try:
        manifest_path = manifest_path.resolve(strict=True)
        root = manifest_path.parent.resolve(strict=True)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0)) < 2:
            return False, "manifest does not bind offline wheels to SHA-256 (version < 2)"
        if payload.get("remoteUrls"):
            return False, "offline bundle manifest contains remote URLs"

        wheel_dir_value = payload.get("wheelDir")
        if not isinstance(wheel_dir_value, str) or not wheel_dir_value.strip():
            return False, "wheelDir is missing"
        if Path(wheel_dir_value).is_absolute():
            return False, "wheelDir must be relative to the bundle"
        wheel_dir_path = root / wheel_dir_value
        if wheel_dir_path.is_symlink() or _is_reparse_point(wheel_dir_path):
            return False, "wheelDir must not be a link or reparse point"
        wheel_dir = wheel_dir_path.resolve(strict=True)
        if not wheel_dir.is_dir() or not _is_within(wheel_dir, root):
            return False, "wheelDir escapes the offline bundle"

        wheels = payload.get("wheels")
        hashes = payload.get("wheelSha256")
        if not isinstance(wheels, list) or not wheels:
            return False, "offline bundle contains no wheels"
        if len(wheels) != len(set(wheels)):
            return False, "offline bundle contains duplicate wheel entries"
        if not isinstance(hashes, dict) or set(hashes) != set(wheels):
            return False, "wheelSha256 keys must exactly match wheels"

        actual_wheels = {
            path.relative_to(root).as_posix() for path in wheel_dir.glob("*.whl") if path.is_file()
        }
        if actual_wheels != set(wheels):
            return False, "wheel directory differs from the closed manifest"

        for relative in wheels:
            if not isinstance(relative, str) or "\\" in relative:
                return False, "wheel paths must use relative POSIX syntax"
            posix = PurePosixPath(relative)
            if posix.is_absolute() or ".." in posix.parts or posix.suffix != ".whl":
                return False, f"unsafe wheel path: {relative!r}"
            candidate_path = root.joinpath(*posix.parts)
            if candidate_path.is_symlink() or _is_reparse_point(candidate_path):
                return False, f"wheel must not be a link or reparse point: {relative}"
            candidate = candidate_path.resolve(strict=True)
            if candidate.parent != wheel_dir or not candidate.is_file():
                return False, f"wheel is outside wheelDir: {relative}"
            expected = hashes[relative]
            if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected.lower()):
                return False, f"invalid wheel SHA-256: {relative}"
            if _sha256(candidate) != expected.lower():
                return False, f"wheel SHA-256 mismatch: {relative}"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return False, f"offline wheel verification error: {exc}"
    return True, f"verified {len(wheels)} offline wheels"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: verify_offline_bundle.py MANIFEST", file=sys.stderr)
        return 2
    valid, detail = verify_manifest(Path(args[0]))
    print(detail, file=sys.stdout if valid else sys.stderr)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
