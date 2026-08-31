# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Pin the Linux suite honesty gate: Win32 paths are skipped, not failed."""

from __future__ import annotations

from pathlib import Path

from tests.win32_native import WIN32_NATIVE_REASON, requires_win32_native


def test_win32_native_reason_is_explicit() -> None:
    assert "Win32" in WIN32_NATIVE_REASON
    reason = requires_win32_native.kwargs.get("reason") or (
        requires_win32_native.args[1] if len(requires_win32_native.args) > 1 else ""
    )
    assert reason == WIN32_NATIVE_REASON


def test_w25e_failure_classes_are_gated() -> None:
    root = Path("tests")
    sources = {
        "test_clipboard_inject.py": "pytestmark = requires_win32_native",
        "test_injector_router.py": "@requires_win32_native",
        "test_windows_apps_matrix.py": "@requires_win32_native",
        "test_windows_uia.py": "@requires_win32_native",
        "test_pipeline.py": "@requires_win32_native",
        "test_selection.py": "@requires_win32_native",
        "test_app_payload.py": 'pytest.skip("Windows frozen EXE")',
        "test_tray.py": "Linux AppIndicator typelib missing",
    }
    for name, needle in sources.items():
        text = (root / name).read_text(encoding="utf-8")
        assert needle in text, f"{name} must gate Win32 ctypes/UIA/Windows EXE"
