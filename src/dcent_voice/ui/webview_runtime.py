# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Detect the host WebView2 runtime used by Settings, overlay, and wizard."""

from __future__ import annotations

import sys

#: Microsoft's Evergreen WebView2 download page. Opened only on an explicit
#: user click (tray menu / first-run dialog text) — never fetched by the app.
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# Evergreen WebView2 runtime client id used by EdgeUpdate.
_WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_KEYS = (
    rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}",
    rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}",
)


def windows_webview2_runtime_present() -> bool:
    """Return True when a usable Evergreen WebView2 runtime is registered."""

    if sys.platform != "win32":
        return True
    try:
        import winreg
    except ImportError:  # pragma: no cover - CPython on Windows always has winreg
        return False
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in _WEBVIEW2_KEYS:
            try:
                with winreg.OpenKey(hive, path) as key:
                    pv, _kind = winreg.QueryValueEx(key, "pv")
            except OSError:
                continue
            if isinstance(pv, str) and pv.strip() and pv != "0.0.0.0":
                return True
    return False


def missing_windows_webview2_message() -> str:
    return (
        "Settings, the overlay, and the setup wizard need the Microsoft Edge "
        "WebView2 runtime (included with Edge and typical Windows 11). Install "
        f"it from {WEBVIEW2_DOWNLOAD_URL} then retry. "
        "Hold-to-talk dictation does not require WebView2."
    )
