# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from dcent_voice.inject import linux, macos, selection
from dcent_voice.inject.clipboard import ClipboardPreservationError
from dcent_voice.inject.selection import get_selected_text
from tests.win32_native import requires_win32_native


@requires_win32_native
def test_get_selected_text_prefers_uia_getter() -> None:
    value = get_selected_text(uia_getter=lambda: "selected text")

    assert value == "selected text"


def test_get_selected_text_routes_to_macos(monkeypatch) -> None:
    monkeypatch.setattr("dcent_voice.inject.selection.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "dcent_voice.inject.selection.get_selected_text_macos",
        lambda **_kwargs: "mac selection",
    )

    assert get_selected_text() == "mac selection"


def test_get_selected_text_routes_to_linux(monkeypatch) -> None:
    monkeypatch.setattr("dcent_voice.inject.selection.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "dcent_voice.inject.selection.get_selected_text_linux",
        lambda **_kwargs: "linux selection",
    )

    assert get_selected_text() == "linux selection"


def test_macos_selection_capture_restores_all_format_snapshot(monkeypatch) -> None:
    snapshot = (
        (
            macos.PasteboardFormat("public.rtf", b"rich"),
            macos.PasteboardFormat("public.png", b"image"),
        ),
        (macos.PasteboardFormat("public.file-url", b"file:///tmp/a"),),
    )
    calls: list[object] = []
    monkeypatch.setattr(macos, "snapshot_clipboard", lambda: snapshot)
    monkeypatch.setattr(macos, "set_clipboard_text", lambda value: calls.append(("set", value)))
    monkeypatch.setattr(macos, "send_cmd_c", lambda: calls.append("copy"))
    monkeypatch.setattr(macos, "get_clipboard_text", lambda: "selected text")
    monkeypatch.setattr(macos, "get_clipboard_change_count", lambda: 12)
    monkeypatch.setattr(macos, "restore_clipboard", lambda value: calls.append(("restore", value)))

    assert selection.get_selected_text_macos() == "selected text"
    assert calls == [("set", ""), "copy", ("restore", snapshot)]


def test_linux_selection_capture_checks_helpers_and_restores_all_mime(monkeypatch) -> None:
    snapshot = (
        linux.ClipboardTarget("text/rtf", 8, b"rich"),
        linux.ClipboardTarget("image/png", 8, b"image"),
        linux.ClipboardTarget("text/uri-list", 8, b"file:///tmp/a"),
    )
    calls: list[object] = []
    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["wl-copy"], ["wl-paste"]))
    monkeypatch.setattr(linux, "_copy_argv", lambda: ["wtype", "copy"])
    monkeypatch.setattr(linux, "snapshot_clipboard", lambda: snapshot)
    monkeypatch.setattr(linux, "restore_clipboard", lambda value: calls.append(("restore", value)))

    def run(_argv, *, text=None, stage="helper"):
        calls.append(stage)
        return "selected text" if stage == "selection_read" else ""

    monkeypatch.setattr(linux, "_run", run)

    assert selection.get_selected_text_linux() == "selected text"
    assert calls == [
        "selection_clear",
        "selection_clear_verify",
        "selection_copy_shortcut",
        "selection_read",
        ("restore", snapshot),
    ]


def test_linux_selection_clear_zero_exit_with_old_text_never_sends_copy(monkeypatch) -> None:
    snapshot = (linux.ClipboardTarget("image/png", 8, b"image"),)
    calls: list[str] = []
    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["xclip"], ["xclip", "-o"]))
    monkeypatch.setattr(linux, "_copy_argv", lambda: ["xdotool", "copy"])
    monkeypatch.setattr(linux, "snapshot_clipboard", lambda: snapshot)
    monkeypatch.setattr(linux, "restore_clipboard", lambda _value: calls.append("restore"))

    def run(_argv, *, text=None, stage="helper"):
        calls.append(stage)
        if stage == "selection_clear_verify":
            return "old clipboard"
        if stage == "selection_copy_shortcut":
            pytest.fail("copy must not run until the clear is verified")
        return ""

    monkeypatch.setattr(linux, "_run", run)

    with pytest.raises(linux.ClipboardCommandError) as raised:
        selection.get_selected_text_linux()

    assert raised.value.stage == "selection_clear_verify"
    assert calls == ["selection_clear", "selection_clear_verify", "restore"]


@pytest.mark.parametrize("platform_module", [macos, linux])
def test_selection_snapshot_failure_causes_no_clipboard_mutation(
    monkeypatch, platform_module
) -> None:
    def unsafe_snapshot():
        raise ClipboardPreservationError("unreadable image")

    monkeypatch.setattr(platform_module, "snapshot_clipboard", unsafe_snapshot)
    if platform_module is macos:
        monkeypatch.setattr(
            macos,
            "set_clipboard_text",
            lambda _value: pytest.fail("macOS clipboard must not mutate"),
        )
        with pytest.raises(ClipboardPreservationError):
            selection.get_selected_text_macos()
        return

    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["wl-copy"], ["wl-paste"]))
    monkeypatch.setattr(linux, "_copy_argv", lambda: ["wtype", "copy"])
    monkeypatch.setattr(
        linux,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("Linux clipboard must not mutate"),
    )
    with pytest.raises(ClipboardPreservationError):
        selection.get_selected_text_linux()
