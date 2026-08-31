# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys

from dcent_voice.ui.webview_runtime import (
    missing_windows_webview2_message,
    windows_webview2_runtime_present,
)


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_missing_webview2_message_points_at_official_runtime() -> None:
    text = missing_windows_webview2_message()
    assert "WebView2" in text
    assert "fwlink" in text
    assert "dictation" in text.lower()


def test_webview2_probe_false_when_registry_empty(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    class EmptyReg:
        HKEY_LOCAL_MACHINE = 1
        HKEY_CURRENT_USER = 2

        @staticmethod
        def OpenKey(*_args, **_kwargs):
            raise OSError("missing")

    monkeypatch.setitem(sys.modules, "winreg", EmptyReg)
    assert windows_webview2_runtime_present() is False


def test_webview2_probe_true_when_pv_registered(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    class PresentReg:
        HKEY_LOCAL_MACHINE = 1
        HKEY_CURRENT_USER = 2

        @staticmethod
        def OpenKey(*_args, **_kwargs):
            return _FakeKey()

        @staticmethod
        def QueryValueEx(_key, name: str):
            assert name == "pv"
            return ("130.0.0.0", 1)

    monkeypatch.setitem(sys.modules, "winreg", PresentReg)
    assert windows_webview2_runtime_present() is True


def test_webview2_probe_ignores_zero_version(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    class ZeroReg:
        HKEY_LOCAL_MACHINE = 1
        HKEY_CURRENT_USER = 2

        @staticmethod
        def OpenKey(*_args, **_kwargs):
            return _FakeKey()

        @staticmethod
        def QueryValueEx(_key, name: str):
            return ("0.0.0.0", 1)

    monkeypatch.setitem(sys.modules, "winreg", ZeroReg)
    assert windows_webview2_runtime_present() is False
