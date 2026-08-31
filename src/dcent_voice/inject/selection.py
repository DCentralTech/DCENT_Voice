# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Capture selected text for command context."""

from __future__ import annotations

import platform
import time
from collections.abc import Callable

from dcent_voice.inject.clipboard import (
    ClipboardPreservationError,
    clear_clipboard,
    get_clipboard_text,
    set_clipboard_text,
)


def get_selected_text(
    *,
    timeout_s: float = 0.3,
    restore_previous: bool = True,
    prefer_uia: bool = True,
    uia_getter: Callable[[], str] | None = None,
) -> str:
    system = platform.system()
    if system == "Darwin":
        return get_selected_text_macos(timeout_s=timeout_s, restore_previous=restore_previous)
    if system == "Linux":
        return get_selected_text_linux(timeout_s=timeout_s, restore_previous=restore_previous)
    if prefer_uia:
        try:
            uia_text = (uia_getter or get_selected_text_uia)().strip()
        except Exception:
            uia_text = ""
        if uia_text:
            return uia_text
    return get_selected_text_clipboard(timeout_s=timeout_s, restore_previous=restore_previous)


def get_selected_text_macos(*, timeout_s: float = 0.3, restore_previous: bool = True) -> str:
    from dcent_voice.inject.macos import (
        get_clipboard_change_count,
        get_clipboard_text,
        restore_clipboard,
        send_cmd_c,
        set_clipboard_text,
        snapshot_clipboard,
    )

    previous = snapshot_clipboard() if restore_previous else None
    copied_change_count: int | None = None
    try:
        set_clipboard_text("")
        send_cmd_c()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = get_clipboard_text()
            if value:
                copied_change_count = get_clipboard_change_count()
                return str(value)
            time.sleep(0.01)
        copied_change_count = get_clipboard_change_count()
        return ""
    finally:
        if previous is not None:
            if (
                copied_change_count is not None
                and get_clipboard_change_count() != copied_change_count
            ):
                raise ClipboardPreservationError(
                    "macOS pasteboard changed during selected-text capture; "
                    "refusing to overwrite the newer contents."
                )
            restore_clipboard(previous)


def get_selected_text_linux(*, timeout_s: float = 0.3, restore_previous: bool = True) -> str:
    from dcent_voice.inject.linux import (
        ClipboardCommandError,
        _clipboard_tools,
        _copy_argv,
        _run,
        restore_clipboard,
        snapshot_clipboard,
    )

    set_argv, get_argv = _clipboard_tools()
    copy_argv = _copy_argv()
    if set_argv is None or get_argv is None or copy_argv is None:
        return ""
    previous = snapshot_clipboard() if restore_previous else None
    copied_snapshot = None
    try:
        _run(set_argv, text="", stage="selection_clear")
        if _run(get_argv, stage="selection_clear_verify") != "":
            raise ClipboardCommandError(
                stage="selection_clear_verify",
                argv=set_argv,
                detail="helper exited successfully but left prior clipboard text in place",
            )
        _run(copy_argv, stage="selection_copy_shortcut")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = _run(get_argv, stage="selection_read")
            if value:
                if previous is not None:
                    copied_snapshot = snapshot_clipboard()
                return value
            time.sleep(0.01)
        if previous is not None:
            copied_snapshot = snapshot_clipboard()
        return ""
    finally:
        if previous is not None:
            if copied_snapshot is not None and snapshot_clipboard() != copied_snapshot:
                raise ClipboardPreservationError(
                    "Linux clipboard changed during selected-text capture; "
                    "refusing to overwrite the newer contents."
                )
            restore_clipboard(previous)


def get_selected_text_clipboard(*, timeout_s: float = 0.3, restore_previous: bool = True) -> str:
    previous = get_clipboard_text(timeout_s=timeout_s)
    try:
        clear_clipboard(timeout_s=timeout_s)
        send_ctrl_c()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = get_clipboard_text(timeout_s=timeout_s)
            if value:
                return value
            time.sleep(0.01)
        return ""
    finally:
        if restore_previous:
            if previous is None:
                clear_clipboard(timeout_s=timeout_s)
            else:
                set_clipboard_text(previous, timeout_s=timeout_s)


def get_selected_text_uia() -> str:
    try:
        import uiautomation as auto
    except ImportError:
        return ""

    control = auto.GetFocusedControl()
    if control is None:
        return ""

    # Text controls expose TextPattern.GetSelection(); other controls may expose
    # SelectionPattern selected items. Both APIs differ slightly across wrappers,
    # so this path is intentionally defensive and falls back to clipboard probing.
    for pattern_name in ("GetTextPattern", "GetSelectionPattern"):
        pattern_fn = getattr(control, pattern_name, None)
        if not callable(pattern_fn):
            continue
        try:
            pattern = pattern_fn()
        except Exception:
            continue
        text = _text_from_uia_pattern(pattern)
        if text:
            return text
    return ""


def _text_from_uia_pattern(pattern) -> str:
    selection_fn = getattr(pattern, "GetSelection", None)
    if callable(selection_fn):
        try:
            ranges = selection_fn()
        except Exception:
            ranges = []
        chunks: list[str] = []
        for item in ranges or []:
            get_text = getattr(item, "GetText", None)
            if callable(get_text):
                try:
                    chunks.append(get_text(-1))
                except Exception:
                    continue
            else:
                value = getattr(item, "Name", "")
                if value:
                    chunks.append(str(value))
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    return ""


def send_ctrl_c() -> None:
    # Reuse the SendInput structure from clipboard.py but send C instead of V.
    import ctypes

    from dcent_voice.inject import clipboard

    inputs = (
        clipboard._key(clipboard.VK_CONTROL),
        clipboard._key(0x43),
        clipboard._key(0x43, clipboard.KEYEVENTF_KEYUP),
        clipboard._key(clipboard.VK_CONTROL, clipboard.KEYEVENTF_KEYUP),
    )
    array = (clipboard.INPUT * len(inputs))(*inputs)
    sent = clipboard._user32.SendInput(len(inputs), array, ctypes.sizeof(clipboard.INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())
