# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Capture and safely address the focused native Windows control."""

from __future__ import annotations

import ctypes
import platform
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


class FocusChangedError(RuntimeError):
    """Foreground/focus changed before a global-input fallback could execute."""


class TargetedInjectionUnsupported(RuntimeError):
    """The captured control has no proven target-bound injection mechanism."""


class TargetedInjectionVerificationError(RuntimeError):
    """A target message completed without producing the exact expected text."""


@dataclass(frozen=True)
class WindowsFocusTarget:
    top_hwnd: int
    focus_hwnd: int
    thread_id: int
    class_name: str
    process_id: int = 0

    @property
    def supports_edit_messages(self) -> bool:
        name = self.class_name.casefold()
        return name == "edit" or name.startswith("richedit")


@dataclass(frozen=True)
class TargetEditState:
    text: str
    selection_start: int
    selection_end: int


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _win32():
    if platform.system() != "Windows":
        raise RuntimeError("Windows focus capture requires Windows.")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.IsChild.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
    ]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    return user32


def capture_foreground_target() -> WindowsFocusTarget | None:
    """Atomically-enough snapshot top-level and focused control for one GUI thread."""
    if platform.system() != "Windows":
        return None
    user32 = _win32()
    top = int(user32.GetForegroundWindow() or 0)
    if not top:
        return None
    process_id = wintypes.DWORD()
    thread_id = int(user32.GetWindowThreadProcessId(top, ctypes.byref(process_id)))
    if not thread_id:
        return None
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return None
    focus = int(info.hwndFocus or top)
    if focus != top and not user32.IsChild(top, focus):
        return None
    class_buffer = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(focus, class_buffer, len(class_buffer)):
        return None
    return WindowsFocusTarget(
        top_hwnd=top,
        focus_hwnd=focus,
        thread_id=thread_id,
        class_name=class_buffer.value,
        process_id=int(process_id.value),
    )


def _find_edit_child(user32: Any, top_hwnd: int) -> int:
    for class_name in ("Edit", "RichEdit20W", "RichEdit50W", "RICHEDIT50W"):
        child = int(user32.FindWindowExW(top_hwnd, None, class_name, None) or 0)
        if child:
            return child
    return 0


def capture_target_from_hwnd(top_hwnd: int) -> WindowsFocusTarget | None:
    """Bind a known top-level window without requiring it to be foreground.

    Cursor, the Start menu, and the overlay can steal GetForegroundWindow
    during prepare or hold. Notepad's EDIT child remains addressable by HWND.
    """
    if platform.system() != "Windows" or not top_hwnd:
        return None
    user32 = _win32()
    if not user32.IsWindow(top_hwnd):
        return None
    process_id = wintypes.DWORD()
    thread_id = int(user32.GetWindowThreadProcessId(top_hwnd, ctypes.byref(process_id)))
    if not thread_id:
        return None
    focus = int(top_hwnd)
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        candidate = int(info.hwndFocus or 0)
        if candidate and (candidate == top_hwnd or user32.IsChild(top_hwnd, candidate)):
            focus = candidate
    if focus == top_hwnd:
        child = _find_edit_child(user32, top_hwnd)
        if child:
            focus = child
    class_buffer = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(focus, class_buffer, len(class_buffer)):
        return None
    return WindowsFocusTarget(
        top_hwnd=int(top_hwnd),
        focus_hwnd=focus,
        thread_id=thread_id,
        class_name=class_buffer.value,
        process_id=int(process_id.value),
    )


def focus_is_unchanged(target: WindowsFocusTarget) -> bool:
    current = capture_foreground_target()
    if not (
        current and current.top_hwnd == target.top_hwnd and current.thread_id == target.thread_id
    ):
        return False
    if current.focus_hwnd == target.focus_hwnd:
        return True
    return _chromium_page_search_still_focused(target, current)


def _chromium_page_search_still_focused(
    press: WindowsFocusTarget,
    current: WindowsFocusTarget,
) -> bool:
    """Google's ComboBox can retarget the renderer hwnd without leaving the page box."""
    blob = f"{press.class_name} {current.class_name}".casefold()
    if "chrome" not in blob and "mozilla" not in blob:
        return False
    try:
        from dcent_voice.inject.windows_uia import (
            _COMMON_PAGE_FIELD_NAMES,
            focused_page_search_field,
        )

        return focused_page_search_field(_COMMON_PAGE_FIELD_NAMES) is not None
    except Exception:
        return False


def require_focus_unchanged(target: WindowsFocusTarget) -> None:
    if not focus_is_unchanged(target):
        raise FocusChangedError(
            "Foreground control changed before the global-input fallback; injection was refused."
        )


def restore_foreground(hwnd: int | None) -> bool:
    """Bring ``hwnd`` back so extra-app SendInput lands after a steal.

    Cursor, CoreWindow, and the overlay can grab GetForegroundWindow while ASR
    runs. AttachThreadInput plus a benign ALT unlock SetForegroundWindow.
    """
    if platform.system() != "Windows" or not hwnd:
        return False
    user32 = _win32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.BOOL,
    ]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.keybd_event.restype = None
    current = int(user32.GetForegroundWindow() or 0)
    target = int(hwnd)
    if current == target:
        return True
    cur_thread = int(kernel32.GetCurrentThreadId())
    fg_thread = int(user32.GetWindowThreadProcessId(current, None) or 0) if current else 0
    tgt_thread = int(user32.GetWindowThreadProcessId(target, None) or 0)
    attached_fg = bool(fg_thread) and bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    attached_tgt = bool(tgt_thread) and bool(user32.AttachThreadInput(cur_thread, tgt_thread, True))
    try:
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        user32.ShowWindow(target, 9)  # SW_RESTORE
        user32.SetForegroundWindow(target)
        user32.BringWindowToTop(target)
    finally:
        if attached_tgt:
            user32.AttachThreadInput(cur_thread, tgt_thread, False)
        if attached_fg:
            user32.AttachThreadInput(cur_thread, fg_thread, False)
    return int(user32.GetForegroundWindow() or 0) == target


def _send_target_message(
    target: WindowsFocusTarget,
    message: int,
    *,
    wparam: int = 0,
    lparam: int = 0,
    timeout_ms: int = 1000,
) -> int:
    user32 = _win32()
    _validate_target_identity(target, user32)
    if not target.supports_edit_messages:
        raise TargetedInjectionUnsupported(
            f"Target class {target.class_name!r} has no proven edit-message injection path."
        )
    result = ctypes.c_size_t()
    smto_block_abort_erroronexit = 0x0001 | 0x0002 | 0x0020
    ctypes.set_last_error(0)
    sent = user32.SendMessageTimeoutW(
        target.focus_hwnd,
        message,
        wparam,
        lparam,
        smto_block_abort_erroronexit,
        timeout_ms,
        ctypes.byref(result),
    )
    if not sent:
        error = ctypes.get_last_error()
        if not error:
            raise TimeoutError(
                f"Timed out after {timeout_ms} ms sending Win32 message 0x{message:04X}."
            )
        raise ctypes.WinError(error)
    return int(result.value)


def _validate_target_identity(target: WindowsFocusTarget, user32=None) -> None:
    user32 = user32 or _win32()
    if not user32.IsWindow(target.top_hwnd) or not user32.IsWindow(target.focus_hwnd):
        raise TargetedInjectionVerificationError("Captured target HWND no longer exists.")
    process_id = wintypes.DWORD()
    thread_id = int(user32.GetWindowThreadProcessId(target.focus_hwnd, ctypes.byref(process_id)))
    class_buffer = ctypes.create_unicode_buffer(256)
    class_length = int(user32.GetClassNameW(target.focus_hwnd, class_buffer, len(class_buffer)))
    root = int(user32.GetAncestor(target.focus_hwnd, 2) or 0)  # GA_ROOT
    if (
        not thread_id
        or thread_id != target.thread_id
        or (target.process_id and int(process_id.value) != target.process_id)
        or not class_length
        or class_buffer.value.casefold() != target.class_name.casefold()
        or root != target.top_hwnd
    ):
        raise TargetedInjectionVerificationError(
            "Captured target identity changed before the bounded native message."
        )


def read_targeted_edit_state(target: object, *, timeout_ms: int = 1000) -> TargetEditState:
    """Synchronously read standard EDIT/RichEdit text and selection with bounds."""
    if not isinstance(target, WindowsFocusTarget):
        raise TypeError("target must be a WindowsFocusTarget")
    length = _send_target_message(target, 0x000E, timeout_ms=timeout_ms)  # WM_GETTEXTLENGTH
    if length < 0 or length > 16 * 1024 * 1024:
        raise TargetedInjectionVerificationError(
            f"Target reported an unsafe text length ({length} UTF-16 code units)."
        )
    buffer = ctypes.create_unicode_buffer(length + 1)
    copied = _send_target_message(
        target,
        0x000D,  # WM_GETTEXT
        wparam=len(buffer),
        lparam=int(ctypes.cast(buffer, ctypes.c_void_p).value or 0),
        timeout_ms=timeout_ms,
    )
    text = buffer.value
    text_utf16_units = len(text.encode("utf-16-le", errors="surrogatepass")) // 2
    if copied != text_utf16_units:
        raise TargetedInjectionVerificationError(
            f"WM_GETTEXT reported {copied} UTF-16 units but returned {text_utf16_units}."
        )
    selection_start = wintypes.DWORD()
    selection_end = wintypes.DWORD()
    _send_target_message(
        target,
        0x00B0,  # EM_GETSEL
        wparam=int(ctypes.cast(ctypes.byref(selection_start), ctypes.c_void_p).value or 0),
        lparam=int(ctypes.cast(ctypes.byref(selection_end), ctypes.c_void_p).value or 0),
        timeout_ms=timeout_ms,
    )
    start = int(selection_start.value)
    end = int(selection_end.value)
    if start > end or end > text_utf16_units:
        raise TargetedInjectionVerificationError(
            f"Target selection {start}:{end} is outside text length {text_utf16_units}."
        )
    return TargetEditState(text=text, selection_start=start, selection_end=end)


def _expected_replacement(before: TargetEditState, text: str) -> TargetEditState:
    before_utf16 = before.text.encode("utf-16-le", errors="surrogatepass")
    inserted_utf16 = text.encode("utf-16-le", errors="surrogatepass")
    expected_text = (
        before_utf16[: before.selection_start * 2]
        + inserted_utf16
        + before_utf16[before.selection_end * 2 :]
    ).decode("utf-16-le", errors="surrogatepass")
    caret = before.selection_start + len(inserted_utf16) // 2
    return TargetEditState(expected_text, caret, caret)


def _normalize_edit_newlines(text: str) -> str:
    """Compare EDIT/RichEdit text without treating CR LF as a different insert."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _caret_in_normalized(text: str, caret: int) -> int:
    if caret <= 0:
        return 0
    if caret >= len(text):
        return len(_normalize_edit_newlines(text))
    return len(_normalize_edit_newlines(text[:caret]))


def _edit_states_match(after: TargetEditState, expected: TargetEditState) -> bool:
    """True when observed equals expected, including Windows CR LF vs LF."""
    if after == expected:
        return True
    if _normalize_edit_newlines(after.text) != _normalize_edit_newlines(expected.text):
        return False
    return _caret_in_normalized(after.text, after.selection_start) == _caret_in_normalized(
        expected.text, expected.selection_start
    ) and _caret_in_normalized(after.text, after.selection_end) == _caret_in_normalized(
        expected.text, expected.selection_end
    )


def _format_state(state: TargetEditState) -> str:
    escaped = state.text.encode("unicode_escape").decode("ascii")
    if len(escaped) > 240:
        escaped = escaped[:237] + "..."
    return f"text={escaped!r}, selection={state.selection_start}:{state.selection_end}"


def send_targeted_paste(
    target: object,
    text: str,
    *,
    timeout_ms: int = 1000,
    consumption_grace_ms: int = 50,
    poll_interval_ms: int = 2,
) -> None:
    """Paste and acknowledge exact insertion before clipboard restoration.

    Exactly one WM_PASTE is ever sent. If its immediate post-state is unchanged,
    bounded read-only polling allows delayed consumption to become visible. We do
    not resend: even an immediate final recheck leaves a theoretical check/send
    interval in which delayed consumption could duplicate text. Any partial or
    unexpected insertion fails closed.
    """
    if not isinstance(target, WindowsFocusTarget):
        raise TypeError("target must be a WindowsFocusTarget")
    if consumption_grace_ms < 0:
        raise ValueError("consumption_grace_ms cannot be negative")
    if poll_interval_ms <= 0:
        raise ValueError("poll_interval_ms must be positive")
    before = read_targeted_edit_state(target, timeout_ms=timeout_ms)
    expected = _expected_replacement(before, text)
    _send_target_message(target, 0x0302, timeout_ms=timeout_ms)  # WM_PASTE
    deadline = time.monotonic() + consumption_grace_ms / 1000.0
    polls = 0
    while True:
        polls += 1
        after = read_targeted_edit_state(target, timeout_ms=timeout_ms)
        if _edit_states_match(after, expected):
            return
        if after != before:
            raise TargetedInjectionVerificationError(
                "Target-bound WM_PASTE produced an unexpected partial/result state; "
                f"poll={polls}, before=({_format_state(before)}), "
                f"expected=({_format_state(expected)}), observed=({_format_state(after)})."
            )
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            break
        time.sleep(min(poll_interval_ms / 1000.0, remaining_s))
    raise TargetedInjectionVerificationError(
        "The single target-bound WM_PASTE remained a verified no-op through a "
        f"{consumption_grace_ms} ms read-only grace window ({polls} polls); "
        f"before=({_format_state(before)}), "
        f"expected=({_format_state(expected)})."
    )


def send_targeted_edit_text(target: object, text: str, *, timeout_ms: int = 1000) -> None:
    if not isinstance(target, WindowsFocusTarget):
        raise TypeError("target must be a WindowsFocusTarget")
    before = read_targeted_edit_state(target, timeout_ms=timeout_ms)
    expected = _expected_replacement(before, text)
    buffer = ctypes.create_unicode_buffer(text)
    _send_target_message(
        target,
        0x00C2,  # EM_REPLACESEL
        wparam=1,
        lparam=int(ctypes.cast(buffer, ctypes.c_void_p).value or 0),
        timeout_ms=timeout_ms,
    )
    after = read_targeted_edit_state(target, timeout_ms=timeout_ms)
    if not _edit_states_match(after, expected):
        raise TargetedInjectionVerificationError(
            "Target-bound EM_REPLACESEL did not produce the exact expected state; "
            f"before=({_format_state(before)}), expected=({_format_state(expected)}), "
            f"observed=({_format_state(after)})."
        )


def send_targeted_enter(target: object, *, timeout_ms: int = 1000) -> None:
    """Send Enter into a native EDIT/RichEdit via the same targeted path as text.

    Global SendInput misses the control after HWND-bound insert. A targeted
    newline is the notepad-submit analog. Not a cloud submit.
    """
    send_targeted_edit_text(target, "\r\n", timeout_ms=timeout_ms)
