# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Guard the ignore rules that keep local payload dumps out of commits."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MUST_IGNORE = (
    ".tmp-scratch-run.py",
    "internal/local-runs/example.md",
    "artifacts/local.json",
    "mcps/server.js",
    "config.toml",
    "models/base.en.bin",
    "dist/DCENT_Voice/app.exe",
    "packaging/windows/setup-stub/obj/stub.dll",
)

MUST_TRACK = (
    "src/dcent_voice/app.py",
    "README.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "config.example.toml",
)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_gitignore_hides_local_agent_dumps() -> None:
    ignored = _check_ignore(MUST_IGNORE)
    missing = [path for path in MUST_IGNORE if path not in ignored]
    assert missing == [], f"gitignore missed {missing}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_gitignore_keeps_product_docs_tracked() -> None:
    ignored = _check_ignore(MUST_TRACK)
    leaked = [path for path in MUST_TRACK if path in ignored]
    assert leaked == [], f"gitignore incorrectly hid {leaked}"


def _check_ignore(paths: tuple[str, ...]) -> set[str]:
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", "--", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()
    }
