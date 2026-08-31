#!/usr/bin/env python3
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Apply the DCENT_Voice SPDX header to every first-party source file.

Run this from the repository root after adding source files. The operation is
idempotent: files that already carry the canonical project header are skipped.
Use ``--check`` in CI or locally to identify files that still need that header.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path

HEADER_LINES = (
    "DCENT_Voice — open-source, local-first voice dictation",
    "Copyright (c) 2026 D-Central Technologies — "
    "decentralized technologies for digital sovereignty",
    "SPDX-License-Identifier: MIT",
)
# Public-beta candidates created before the final branding review used two
# spaces in place of em dashes. Keep this one-way migration so contributors can
# rerun the helper without accumulating a second header.
LEGACY_HEADER_LINES = (
    "DCENT_Voice  open-source, local-first voice dictation",
    "Copyright (c) 2026 D-Central Technologies  decentralized technologies for digital sovereignty",
    "SPDX-License-Identifier: MIT",
)
HASH_SUFFIXES = {".py", ".ps1", ".sh", ".spec"}
WEB_SUFFIXES = {".js", ".css", ".html"}
CODING_COOKIE = re.compile(r"^[ \t]*#.*coding[:=][ \t]*[-\w.]+")


def _header_for(path: Path, newline: str, lines: tuple[str, str, str] = HEADER_LINES) -> str:
    """Return the appropriately commented license header for *path*."""
    if path.suffix in HASH_SUFFIXES:
        return "".join(f"# {line}{newline}" for line in lines)
    if path.suffix == ".js":
        return "".join(f"// {line}{newline}" for line in lines)
    if path.suffix == ".css":
        return f"/*{newline}" + "".join(f" * {line}{newline}" for line in lines) + f" */{newline}"
    if path.suffix == ".html":
        return f"<!--{newline}" + "".join(f"  {line}{newline}" for line in lines) + f"-->{newline}"
    msg = f"No header style configured for {path}"
    raise ValueError(msg)


def _target_files(root: Path) -> Iterable[Path]:
    """Yield all first-party source files that require the branded header."""
    for path in sorted((root / "src" / "dcent_voice").rglob("*.py")):
        yield path
    for path in sorted((root / "tests").rglob("*.py")):
        yield path
    for path in sorted((root / "scripts").glob("*.py")):
        yield path
    for suffix in ("*.ps1", "*.sh"):
        yield from sorted((root / "scripts").glob(suffix))

    yield root / "packaging" / "DCENT_Voice.spec"

    web_root = root / "src" / "dcent_voice" / "ui" / "web"
    for path in sorted(web_root.rglob("*")):
        if "fonts" not in path.parts and path.is_file() and path.suffix in WEB_SUFFIXES:
            yield path


def _read_text(path: Path) -> str:
    """Read UTF-8 text without normalizing a file's line endings."""
    with path.open("r", encoding="utf-8", newline="") as source:
        return source.read()


def _write_text(path: Path, content: str) -> None:
    """Write UTF-8 text without changing its line-ending convention."""
    with path.open("w", encoding="utf-8", newline="") as destination:
        destination.write(content)


def _insertion_prefix(path: Path, body: str, newline: str) -> tuple[str, str]:
    """Return content before the canonical header position and the remaining body."""
    if path.suffix in HASH_SUFFIXES:
        lines = body.splitlines(keepends=True)
        header_index = int(bool(lines and lines[0].startswith("#!")))
        if header_index < len(lines) and CODING_COOKIE.match(lines[header_index]):
            header_index += 1
        return "".join(lines[:header_index]), "".join(lines[header_index:])

    if path.suffix == ".html":
        stripped = body.lstrip()
        leading = body[: len(body) - len(stripped)]
        if stripped.lower().startswith("<!doctype"):
            doctype_end = stripped.find(">")
            if doctype_end != -1:
                remainder = stripped[doctype_end + 1 :]
                if remainder.startswith("\r\n"):
                    remainder = remainder[2:]
                elif remainder.startswith("\n"):
                    remainder = remainder[1:]
                return f"{leading}{stripped[: doctype_end + 1]}{newline}", remainder

    return "", body


def _has_expected_header(path: Path, content: str) -> bool:
    """Return whether *content* has this project's exact header in its legal position."""
    body = content.removeprefix("\ufeff")
    newline = "\r\n" if "\r\n" in body else "\n"
    header = _header_for(path, newline)
    if path.suffix == ".html":
        stripped = body.lstrip()
        leading = body[: len(body) - len(stripped)]
        if stripped.lower().startswith("<!doctype"):
            doctype_end = stripped.find(">")
            if doctype_end != -1:
                prefix = f"{leading}{stripped[: doctype_end + 1]}{newline}"
                return body.startswith(f"{prefix}{header}")
        return body.startswith(header)
    prefix, remainder = _insertion_prefix(path, body, newline)
    return remainder.startswith(header)


def _with_header(path: Path, content: str) -> str:
    """Insert a header while preserving BOM, shebang, encoding, and doctype positions."""
    bom = "\ufeff" if content.startswith("\ufeff") else ""
    body = content.removeprefix(bom)
    newline = "\r\n" if "\r\n" in body else "\n"
    prefix, remainder = _insertion_prefix(path, body, newline)
    header = _header_for(path, newline)
    legacy_header = _header_for(path, newline, LEGACY_HEADER_LINES)

    def remove_project_headers(value: str) -> str:
        """Remove contiguous canonical/legacy project headers at insertion point."""
        while True:
            for candidate in (header, legacy_header):
                if value.startswith(candidate):
                    value = value.removeprefix(candidate)
                    break
                if value.startswith(f"{newline}{candidate}"):
                    value = value.removeprefix(f"{newline}{candidate}")
                    break
            else:
                return value

    return f"{bom}{prefix}{header}{remove_project_headers(remainder)}"


def _missing_headers(root: Path) -> list[Path]:
    """Return first-party target files without the canonical project header."""
    return [
        path
        for path in _target_files(root)
        if _with_header(path, _read_text(path)) != _read_text(path)
    ]


def main() -> int:
    """Apply headers, or report missing headers when invoked with ``--check``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report files missing the header without modifying them",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    missing = _missing_headers(root)

    if args.check:
        for path in missing:
            print(path.relative_to(root))
        return int(bool(missing))

    for path in missing:
        _write_text(path, _with_header(path, _read_text(path)))
        print(f"added header: {path.relative_to(root)}")
    print(f"headers added: {len(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
