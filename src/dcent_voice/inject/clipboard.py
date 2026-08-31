# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Inject text through the system clipboard."""

from __future__ import annotations

import ctypes
import logging
import platform
import threading
import time
from ctypes import wintypes
from typing import Any

from dcent_voice.inject.base import Injector

CF_UNICODETEXT = 13
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_HDROP = 15
CF_DIB = 8
CF_PALETTE = 9
CF_ENHMETAFILE = 14
CF_DIBV5 = 17
CF_LOCALE = 16
CF_OWNERDISPLAY = 0x0080
CF_DSPBITMAP = 0x0082
CF_DSPMETAFILEPICT = 0x0083
CF_DSPENHMETAFILE = 0x008E
CF_PRIVATEFIRST = 0x0200
CF_PRIVATELAST = 0x02FF
CF_GDIOBJFIRST = 0x0300
CF_GDIOBJLAST = 0x03FF
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_BACK = 0x08
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
ULONG_PTR = wintypes.WPARAM

logger = logging.getLogger("dcent_voice.inject.clipboard")

_PROCESS_CLIPBOARD_TRANSACTION_LOCK = threading.RLock()
_CLIPBOARD_MUTEX_NAME = "Local\\D-Central.DCENT_Voice.ClipboardTransaction.v1"
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
ERROR_ACCESS_DENIED = 5
# Snapshot/set can wait out another process holding the clipboard. Restore after
# we mutated must wait longer: the user's prior contents are already cloned.
_DEFAULT_OPEN_TIMEOUT_S = 2.0
_DEFAULT_RESTORE_TIMEOUT_S = 3.0


class ClipboardPreservationError(RuntimeError):
    """The current clipboard cannot be cloned without silent format loss."""


class ClipboardOpenTimeout(TimeoutError):
    """Bounded clipboard acquisition exhausted its retry budget."""

    def __init__(
        self,
        *,
        stage: str,
        timeout_s: float,
        attempts: int,
        elapsed_s: float,
        last_error: int,
    ) -> None:
        self.stage = stage
        self.timeout_s = timeout_s
        self.attempts = attempts
        self.elapsed_s = elapsed_s
        self.last_error = last_error
        super().__init__(
            f"Timed out opening Windows clipboard at stage {stage!r} after "
            f"{elapsed_s:.3f}s/{timeout_s:.3f}s and {attempts} attempts "
            f"(last_error={last_error})."
        )


class ClipboardPasteInjector(Injector):
    """Text injector that pastes through the system clipboard."""

    def __init__(
        self,
        *,
        restore_previous: bool = True,
        open_timeout_s: float = _DEFAULT_OPEN_TIMEOUT_S,
        restore_timeout_s: float | None = None,
        paste_delay_s: float = 0.10,
        paste_min_delay_s: float = 0.04,
        transaction_timeout_s: float = 2.0,
    ) -> None:
        self.restore_previous = restore_previous
        self.open_timeout_s = open_timeout_s
        self.restore_timeout_s = (
            _DEFAULT_RESTORE_TIMEOUT_S if restore_timeout_s is None else restore_timeout_s
        )
        self.paste_delay_s = paste_delay_s
        self.paste_min_delay_s = paste_min_delay_s
        self.transaction_timeout_s = transaction_timeout_s

    def inject(self, text: str) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("ClipboardPasteInjector currently requires Windows.")

        self._inject_transaction(text, send_ctrl_v)

    def inject_targeted(self, text: str, target: object) -> None:
        """Paste into a captured native edit control and verify exact consumption."""
        from dcent_voice.inject.windows_focus import send_targeted_paste

        self._inject_transaction(
            text,
            lambda: send_targeted_paste(target, text),
            wait_after_paste=False,
        )

    def inject_checked(self, text: str, target: object) -> None:
        """Global Ctrl+V fallback with a final focused-control validation."""
        from dcent_voice.inject.windows_focus import (
            WindowsFocusTarget,
            focus_is_unchanged,
            require_focus_unchanged,
            restore_foreground,
        )

        if not isinstance(target, WindowsFocusTarget):
            raise TypeError("target must be a WindowsFocusTarget")

        def checked_paste() -> None:
            if not focus_is_unchanged(target):
                restore_foreground(int(target.top_hwnd))
            require_focus_unchanged(target)
            send_ctrl_v()

        self._inject_transaction(text, checked_paste)

    def _inject_transaction(
        self, text: str, paste: object, *, wait_after_paste: bool = True
    ) -> None:
        with _clipboard_transaction(self.transaction_timeout_s):
            previous: list[tuple[int, bytes]] | None = None
            if self.restore_previous:
                previous = snapshot_clipboard(timeout_s=self.open_timeout_s)
                if previous is None:
                    raise ClipboardPreservationError(
                        "Clipboard contains a non-HGLOBAL or otherwise non-cloneable format; "
                        "paste was refused before mutation."
                    )
            sequence_number: int | None = None
            clipboard_may_have_changed = False
            try:
                try:
                    set_clipboard_text(text, timeout_s=self.open_timeout_s)
                except ClipboardOpenTimeout as exc:
                    # The only timeout raised by set_clipboard_text is its
                    # pre-EmptyClipboard acquisition stage. Restoring here is
                    # both unnecessary and harmful: the same external owner is
                    # likely still holding the clipboard, and its restore error
                    # would mask the safe failure that a native-edit route can
                    # recover from.
                    if exc.stage != "set":
                        clipboard_may_have_changed = True
                    raise
                except Exception:
                    # EmptyClipboard may already have succeeded before a later
                    # publish failure. Conservatively restore in that case.
                    clipboard_may_have_changed = True
                    raise
                clipboard_may_have_changed = True
                sequence_number = get_clipboard_sequence_number()
                paste_fn = paste
                if not callable(paste_fn):
                    raise TypeError("paste transaction callback must be callable")
                paste_fn()
                if wait_after_paste:
                    self._wait_for_paste()
            finally:
                if self.restore_previous and previous is not None and clipboard_may_have_changed:
                    if (
                        sequence_number is not None
                        and get_clipboard_sequence_number() != sequence_number
                    ):
                        logger.warning(
                            "clipboard changed during serialized paste; refusing to overwrite "
                            "the newer value"
                        )
                    else:
                        self._restore_snapshot(previous)

    def _restore_snapshot(self, previous: list[tuple[int, bytes]]) -> None:
        """Put the user's clipboard back. ACCESS_DENIED is retried, never swallowed."""
        try:
            restore_clipboard(previous, timeout_s=self.restore_timeout_s)
            return
        except ClipboardOpenTimeout as exc:
            if exc.last_error != ERROR_ACCESS_DENIED:
                raise
            logger.warning(
                "clipboard restore hit ACCESS_DENIED after %s attempts; retrying once",
                exc.attempts,
            )
        restore_clipboard(previous, timeout_s=self.restore_timeout_s)

    def _wait_for_paste(self) -> None:
        """Wait for the target to consume Ctrl+V before clipboard restore.

        We cannot observe consumption, so this is still a wait — but the shipped
        default is 100 ms, not 300 ms, and the wait is sliced so it cannot
        overshoot by a long blocking sleep.
        """
        delay = float(self.paste_delay_s or 0.0)
        if delay <= 0:
            return
        minimum = min(float(self.paste_min_delay_s or 0.0), delay)
        if minimum > 0:
            time.sleep(minimum)
        remaining = delay - minimum
        deadline = time.perf_counter() + remaining
        slice_s = 0.01
        while remaining > 0 and time.perf_counter() < deadline:
            time.sleep(min(slice_s, max(0.0, deadline - time.perf_counter())))

    def retract(self, char_count: int) -> None:
        """Delete already-pasted stream text with Backspace (not clipboard)."""
        if char_count <= 0:
            return
        if platform.system() != "Windows":
            raise RuntimeError("ClipboardPasteInjector currently requires Windows.")
        send_backspaces(int(char_count))

    def press_enter(self) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("ClipboardPasteInjector currently requires Windows.")
        send_enter()

    def press_enter_into_target(self, target: object) -> None:
        from dcent_voice.inject.windows_focus import (
            WindowsFocusTarget,
            focus_is_unchanged,
            require_focus_unchanged,
            restore_foreground,
            send_targeted_enter,
        )

        if isinstance(target, WindowsFocusTarget) and target.supports_edit_messages:
            send_targeted_enter(target)
            return
        if isinstance(target, WindowsFocusTarget):
            if not focus_is_unchanged(target):
                restore_foreground(int(target.top_hwnd))
            require_focus_unchanged(target)
        self.press_enter()


def snapshot_clipboard(
    *, timeout_s: float = _DEFAULT_OPEN_TIMEOUT_S
) -> list[tuple[int, bytes]] | None:
    """Clone every HGLOBAL clipboard format or fail closed with ``None``.

    Registered formats are intentionally not allowlisted: applications routinely
    attach private metadata as arbitrary registered HGLOBAL values.  GDI/object
    formats require type-specific duplication and are rejected as a whole before
    a paste may mutate the clipboard.
    """
    captured: list[tuple[int, bytes]] = []
    formats_seen = False
    with _opened_clipboard(timeout_s, stage="snapshot"):
        clipboard_format = 0
        while True:
            ctypes.set_last_error(0)
            clipboard_format = _user32.EnumClipboardFormats(clipboard_format)
            if not clipboard_format:
                error = ctypes.get_last_error()
                if error:
                    raise ctypes.WinError(error)
                break
            formats_seen = True
            if _requires_type_specific_clone(clipboard_format):
                return None
            handle = _user32.GetClipboardData(clipboard_format)
            if not handle:
                return None
            ctypes.set_last_error(0)
            size = _kernel32.GlobalSize(handle)
            if not size:
                return None
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                captured.append((clipboard_format, ctypes.string_at(pointer, size)))
            finally:
                _kernel32.GlobalUnlock(handle)
    if formats_seen and not captured:
        return None
    return captured


class _clipboard_transaction:
    """Serialize the complete clipboard mutation across threads and processes."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = max(0.0, float(timeout_s))
        self.handle: int | None = None

    def __enter__(self) -> None:
        _PROCESS_CLIPBOARD_TRANSACTION_LOCK.acquire()
        if platform.system() != "Windows":
            return None
        try:
            handle = _kernel32.CreateMutexW(None, False, _CLIPBOARD_MUTEX_NAME)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = handle
            timeout_ms = min(0xFFFFFFFE, round(self.timeout_s * 1000.0))
            wait = int(_kernel32.WaitForSingleObject(handle, timeout_ms))
            if wait not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
                if wait == _WAIT_TIMEOUT:
                    raise TimeoutError(
                        "Timed out acquiring the Windows clipboard transaction mutex."
                    )
                raise ctypes.WinError(ctypes.get_last_error())
            if wait == _WAIT_ABANDONED:
                logger.warning("recovered an abandoned Windows clipboard transaction mutex")
            return None
        except Exception:
            if self.handle:
                _kernel32.CloseHandle(self.handle)
                self.handle = None
            _PROCESS_CLIPBOARD_TRANSACTION_LOCK.release()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if self.handle:
                if not _kernel32.ReleaseMutex(self.handle):
                    logger.error("failed to release Windows clipboard transaction mutex")
                _kernel32.CloseHandle(self.handle)
                self.handle = None
        finally:
            _PROCESS_CLIPBOARD_TRANSACTION_LOCK.release()


def restore_clipboard(
    snapshot: list[tuple[int, bytes]], *, timeout_s: float = _DEFAULT_RESTORE_TIMEOUT_S
) -> None:
    """Replace the clipboard with a snapshot created by snapshot_clipboard()."""
    formats = [clipboard_format for clipboard_format, _data in snapshot]
    if len(set(formats)) != len(formats) or any(
        clipboard_format <= 0 or _requires_type_specific_clone(clipboard_format)
        for clipboard_format in formats
    ):
        raise ClipboardPreservationError(
            "Snapshot contains duplicate, invalid, or non-HGLOBAL clipboard formats."
        )
    with _opened_clipboard(timeout_s, stage="restore"):
        if not _user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        for clipboard_format, data in snapshot:
            handle = _global_alloc_bytes(data)
            if not _user32.SetClipboardData(clipboard_format, handle):
                _kernel32.GlobalFree(handle)
                raise ctypes.WinError(ctypes.get_last_error())


def get_clipboard_sequence_number() -> int:
    """Return the system clipboard sequence number used to detect newer writes."""
    return int(_user32.GetClipboardSequenceNumber())


def _global_alloc_bytes(data: bytes) -> int:
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not handle:
        raise OSError("GlobalAlloc failed while restoring clipboard data.")
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        raise OSError("GlobalLock failed while restoring clipboard data.")
    try:
        if data:
            ctypes.memmove(pointer, data, len(data))
    finally:
        _kernel32.GlobalUnlock(handle)
    return handle


def get_clipboard_text(*, timeout_s: float = _DEFAULT_OPEN_TIMEOUT_S) -> str | None:
    with _opened_clipboard(timeout_s, stage="read"):
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)


def set_clipboard_text(text: str, *, timeout_s: float = _DEFAULT_OPEN_TIMEOUT_S) -> None:
    if "\0" in text:
        raise ValueError("CF_UNICODETEXT cannot losslessly represent an embedded NUL.")
    data = (text + "\0").encode("utf-16-le")
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise OSError("GlobalAlloc failed while setting clipboard text.")
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        raise OSError("GlobalLock failed while setting clipboard text.")
    try:
        ctypes.memmove(pointer, data, len(data))
    finally:
        _kernel32.GlobalUnlock(handle)

    transferred = False
    try:
        with _opened_clipboard(timeout_s, stage="set"):
            if not _user32.EmptyClipboard():
                raise ctypes.WinError(ctypes.get_last_error())
            if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            transferred = True
    finally:
        # SetClipboardData transfers ownership only on success.  In particular,
        # an OpenClipboard timeout occurs before the context body and used to
        # leak one movable allocation per failed dictation attempt.
        if not transferred:
            _kernel32.GlobalFree(handle)


def clear_clipboard(*, timeout_s: float = _DEFAULT_OPEN_TIMEOUT_S) -> None:
    with _opened_clipboard(timeout_s, stage="clear"):
        if not _user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())


class _opened_clipboard:
    """Central bounded OpenClipboard retry/backoff contract."""

    def __init__(
        self,
        timeout_s: float,
        *,
        owner_hwnd: int | None = None,
        stage: str = "clipboard_access",
    ) -> None:
        self.timeout_s = max(0.0, float(timeout_s))
        self.owner_hwnd = owner_hwnd
        self.stage = stage

    def __enter__(self) -> None:
        started = time.monotonic()
        deadline = started + self.timeout_s
        attempts = 0
        last_error = 0
        while True:
            attempts += 1
            ctypes.set_last_error(0)
            if _user32.OpenClipboard(self.owner_hwnd):
                return None
            last_error = int(ctypes.get_last_error())
            now = time.monotonic()
            if now >= deadline:
                raise ClipboardOpenTimeout(
                    stage=self.stage,
                    timeout_s=self.timeout_s,
                    attempts=attempts,
                    elapsed_s=now - started,
                    last_error=last_error,
                )
            # ERROR_ACCESS_DENIED (5): another window owns OpenClipboard. Wait
            # longer between attempts than a generic failure — 0.5s of 2ms
            # spins still flakes on a busy desktop.
            if last_error == ERROR_ACCESS_DENIED:
                backoff_s = min(0.05, 0.005 * (2 ** min(attempts - 1, 4)))
            else:
                backoff_s = min(0.025, 0.002 * (2 ** min(attempts - 1, 4)))
            time.sleep(min(backoff_s, max(0.0, deadline - now)))

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _user32.CloseClipboard()


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    # The union must include every member so sizeof(INPUT) matches the Windows
    # INPUT structure (40 bytes on x64, 28 on x86). With only KEYBDINPUT it was
    # too small, so SendInput(..., cbSize=sizeof(INPUT)) failed with
    # ERROR_INVALID_PARAMETER (87) and nothing was ever injected.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


def _key(vk: int, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))


def send_ctrl_v() -> None:
    inputs = (
        _key(VK_CONTROL),
        _key(VK_V),
        _key(VK_V, KEYEVENTF_KEYUP),
        _key(VK_CONTROL, KEYEVENTF_KEYUP),
    )
    array = (INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


def send_backspaces(count: int) -> None:
    """Synthesize ``count`` Backspace keypresses into the focused window."""
    if count <= 0:
        return
    # Batch to keep SendInput sizes reasonable for long streaming retracts.
    batch = 64
    remaining = int(count)
    while remaining > 0:
        n = min(batch, remaining)
        keys: list[INPUT] = []
        for _ in range(n):
            keys.append(_key(VK_BACK))
            keys.append(_key(VK_BACK, KEYEVENTF_KEYUP))
        array = (INPUT * len(keys))(*keys)
        sent = _user32.SendInput(len(keys), array, ctypes.sizeof(INPUT))
        if sent != len(keys):
            raise ctypes.WinError(ctypes.get_last_error())
        remaining -= n


def send_enter() -> None:
    """Synthesize one Enter/Return keypress into the focused window."""
    keys = (_key(VK_RETURN), _key(VK_RETURN, KEYEVENTF_KEYUP))
    array = (INPUT * len(keys))(*keys)
    sent = _user32.SendInput(len(keys), array, ctypes.sizeof(INPUT))
    if sent != len(keys):
        raise ctypes.WinError(ctypes.get_last_error())


_user32: Any
_kernel32: Any
if platform.system() == "Windows":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    _user32.EnumClipboardFormats.restype = wintypes.UINT
    _user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterClipboardFormatW.restype = wintypes.UINT
    _user32.GetClipboardSequenceNumber.argtypes = []
    _user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    _kernel32.ReleaseMutex.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover - Windows-only implementation guard
    _user32 = None
    _kernel32 = None

_NON_HGLOBAL_FORMATS = frozenset(
    {
        CF_BITMAP,
        CF_METAFILEPICT,
        CF_PALETTE,
        CF_ENHMETAFILE,
        CF_DSPBITMAP,
        CF_DSPMETAFILEPICT,
        CF_DSPENHMETAFILE,
        CF_OWNERDISPLAY,
    }
)


def _requires_type_specific_clone(clipboard_format: int) -> bool:
    # Win32 frees the standard handle-owned formats with format-specific APIs:
    # DeleteObject for bitmap/palette handles, DeleteMetaFile for metafiles, and
    # no automatic release for CF_OWNERDISPLAY. Those cannot be restored by
    # publishing a byte-cloned HGLOBAL.
    #
    # CF_PRIVATEFIRST..CF_PRIVATELAST is owner-managed: the system does not free
    # its handles and the owning window normally handles WM_DESTROYCLIPBOARD.
    # DCENT deliberately has no surrogate clipboard-owner window, so fail closed.
    #
    # Microsoft documents CF_GDIOBJFIRST..CF_GDIOBJLAST as GMEM_MOVEABLE handles,
    # but also gives special lifetime semantics for this application-defined
    # range. Keep the already-conservative fail-closed policy rather than assume
    # that raw bytes reproduce an unknown application's object contract.
    return (
        clipboard_format in _NON_HGLOBAL_FORMATS
        or CF_PRIVATEFIRST <= clipboard_format <= CF_PRIVATELAST
        or CF_GDIOBJFIRST <= clipboard_format <= CF_GDIOBJLAST
    )
