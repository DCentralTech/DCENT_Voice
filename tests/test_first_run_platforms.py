# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""The first-run native fallback on Linux and macOS (WS9/AC3).

AC3 says a first launch must show the user *something*. On Windows that is a
``MessageBoxW`` when WebView2 is absent. The same guarantee has to hold when the
pywebview wizard cannot open on the other two platforms — a Linux host without
the WebKitGTK typelib is exactly as blind as a Windows host without WebView2 —
so these tests pin the surface each platform actually uses and, importantly,
that the education flag is only persisted when a human really saw it.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from dcent_voice.ui import first_run


class _Hotkeys:
    dictation = "ctrl+win"
    mode = "hold"


class _Config:
    hotkeys = _Hotkeys()


@pytest.fixture(autouse=True)
def _allow_dialogs(monkeypatch):
    """These tests exercise the dialog path, so the global suppression is off."""
    monkeypatch.delenv("DCENT_VOICE_NO_DIALOGS", raising=False)
    monkeypatch.setattr("dcent_voice.util.fatal.desktop_session_available", lambda: True)


def _completed(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=b"", stderr=b"")


# --- dialog copy -------------------------------------------------------------


def test_linux_text_explains_webkitgtk_when_the_gui_is_missing() -> None:
    text = first_run.dialog_text(_Config(), gui_missing=True, platform="linux")
    assert "WebKitGTK" in text
    assert "gir1.2-webkit2-4.1" in text
    # Never tell a Linux user to install a Microsoft Windows runtime.
    assert "WebView2" not in text


def test_linux_text_omits_the_webkit_paragraph_when_the_gui_works() -> None:
    text = first_run.dialog_text(_Config(), gui_missing=False, platform="linux")
    assert "WebKitGTK" not in text
    assert "Ctrl+Win" in text


def test_macos_text_never_mentions_a_missing_runtime() -> None:
    """Cocoa/WebKit ships with macOS; there is nothing for the user to install."""
    text = first_run.dialog_text(_Config(), gui_missing=True, platform="darwin")
    assert "WebKitGTK" not in text
    assert "WebView2" not in text
    assert "Ctrl+Win" in text


def test_windows_keeps_the_webview2_paragraph() -> None:
    text = first_run.dialog_text(_Config(), webview2_missing=True, platform="win32")
    assert "WebView2" in text
    assert "chevron" in text


def test_non_windows_text_uses_the_generic_tray_line() -> None:
    text = first_run.dialog_text(_Config(), platform="linux")
    assert first_run.OTHER_TRAY_LINE in text
    assert "chevron" not in text


# --- macOS surface -----------------------------------------------------------


def test_macos_shows_an_osascript_dialog(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seen: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert first_run.show_first_run_dialog(_Config()) is True
    assert seen["argv"][0] == "/usr/bin/osascript"
    script = seen["argv"][2]
    # `display dialog` blocks until OK, which is what "the user saw it" means.
    assert script.startswith("display dialog ")
    assert "Ctrl+Win" in script
    assert first_run.DIALOG_TITLE in script


def test_macos_osascript_failure_is_not_reported_as_shown(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _completed(1))
    # Returning False keeps the education flag unset, so the next launch retries.
    assert first_run.show_first_run_dialog(_Config()) is False


# --- Linux surface -----------------------------------------------------------


def test_linux_uses_notify_send_and_also_writes_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    seen: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert first_run.show_first_run_dialog(_Config(), gui_missing=True) is True

    assert seen["argv"][0] == "/usr/bin/notify-send"
    assert first_run.DIALOG_TITLE in seen["argv"]
    assert "WebKitGTK" in seen["argv"][-1]
    # The stderr copy is what a terminal launch and the AppImage's own warnings
    # share, so it must be written whether or not the notification works.
    assert "Ctrl+Win" in capsys.readouterr().err


def test_linux_without_notify_send_still_writes_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: None)
    # No notification reached a human, so this is not "shown" — but the text is
    # not lost either.
    assert first_run.show_first_run_dialog(_Config()) is False
    assert "DCENT_Voice" in capsys.readouterr().err


def test_linux_notify_send_failure_is_not_reported_as_shown(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _completed(1))
    assert first_run.show_first_run_dialog(_Config()) is False


def test_a_crashing_notifier_never_escapes(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    def boom(argv, **kwargs):
        raise OSError("dbus is not running")

    monkeypatch.setattr(subprocess, "run", boom)
    # First-run education must never be the thing that kills a launch.
    assert first_run.show_first_run_dialog(_Config()) is False


def test_no_desktop_session_skips_every_surface(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("dcent_voice.util.fatal.desktop_session_available", lambda: False)

    def boom(argv, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("no subprocess may run without a session")

    monkeypatch.setattr(subprocess, "run", boom)
    assert first_run.show_first_run_dialog(_Config()) is False


def test_suppression_env_still_wins_on_every_platform(monkeypatch) -> None:
    monkeypatch.setenv("DCENT_VOICE_NO_DIALOGS", "1")
    for platform in ("linux", "darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        assert first_run.show_first_run_dialog(_Config()) is False


# --- app wiring --------------------------------------------------------------


def test_wizard_failure_on_linux_falls_back_to_the_native_dialog(monkeypatch) -> None:
    """The bug this closes: the wizard fails, a balloon nobody sees, nothing else."""
    from dcent_voice import app

    calls: list[dict] = []
    monkeypatch.setattr(
        first_run,
        "show_first_run_dialog",
        lambda config, **kwargs: calls.append(kwargs) or True,
    )
    shown: list[bool] = []
    thread = app._start_first_run_dialog(
        types.SimpleNamespace(hotkeys=_Hotkeys()),
        webview2_missing=False,
        gui_missing=True,
        on_shown=lambda: shown.append(True),
        logger=types.SimpleNamespace(exception=lambda *a, **k: None),
    )
    thread.join(timeout=5)

    assert calls == [{"webview2_missing": False, "gui_missing": True}]
    # Only a dialog a human dismissed may mark the user as educated.
    assert shown == [True]


def test_the_education_flag_is_not_persisted_when_nothing_was_shown(monkeypatch) -> None:
    from dcent_voice import app

    monkeypatch.setattr(first_run, "show_first_run_dialog", lambda config, **kwargs: False)
    shown: list[bool] = []
    thread = app._start_first_run_dialog(
        types.SimpleNamespace(hotkeys=_Hotkeys()),
        webview2_missing=False,
        gui_missing=True,
        on_shown=lambda: shown.append(True),
        logger=types.SimpleNamespace(exception=lambda *a, **k: None),
    )
    thread.join(timeout=5)
    assert shown == []


def test_the_wizard_is_still_attempted_first_off_windows(monkeypatch) -> None:
    """The native dialog is a fallback, never the primary Linux/macOS surface."""
    from dcent_voice import app

    monkeypatch.setattr(sys, "platform", "linux")
    assert app.auto_open_first_run_wizard(no_tray=False, first_run_education_shown=False) is True
    assert app.auto_open_first_run_wizard(no_tray=False, first_run_education_shown=True) is False
    assert app.auto_open_first_run_wizard(no_tray=True, first_run_education_shown=False) is False
