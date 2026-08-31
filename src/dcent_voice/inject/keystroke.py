# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Inject text through pynput keystrokes."""

from __future__ import annotations

import ctypes
import platform
import time
import unicodedata
from ctypes import wintypes

from dcent_voice.inject.base import Injector


class PynputTypeInjector(Injector):
    """Text injector that types through pynput."""

    def __init__(self, *, chunk_size: int = 1000, chunk_delay_s: float = 0.01) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.chunk_size = min(chunk_size, 1000)
        self.chunk_delay_s = chunk_delay_s
        self._controller = None

    def inject(self, text: str) -> None:
        controller = self._get_controller()
        for chunk in _chunks(text, self.chunk_size):
            controller.type(chunk)
            if self.chunk_delay_s > 0:
                time.sleep(self.chunk_delay_s)

    def retract(self, char_count: int) -> None:
        if char_count <= 0:
            return
        controller = self._get_controller()
        from pynput.keyboard import Key

        for _ in range(int(char_count)):
            controller.press(Key.backspace)
            controller.release(Key.backspace)
            if self.chunk_delay_s > 0:
                time.sleep(self.chunk_delay_s)

    def press_enter(self) -> None:
        controller = self._get_controller()
        from pynput.keyboard import Key

        controller.press(Key.enter)
        controller.release(Key.enter)

    def _get_controller(self):
        if self._controller is None:
            try:
                from pynput.keyboard import Controller
            except ImportError as exc:  # pragma: no cover - dependency/environment specific
                raise RuntimeError("pynput is required for keystroke injection.") from exc
            self._controller = Controller()
        return self._controller


class WindowsSendInputInjector(Injector):
    """Type Unicode through the native Win32 ``SendInput`` API.

    ``pynput.Controller.type`` relies on keyboard-layout virtual-key mappings on
    Windows.  Characters absent from the active layout (including most CJK and
    emoji) can therefore fail even though the OS supports layout-independent
    ``KEYEVENTF_UNICODE`` packets.  The desktop app uses this backend on Windows
    so the short-text route has the same Unicode contract as clipboard paste.
    """

    def __init__(self, *, batch_utf16_units: int = 256) -> None:
        if batch_utf16_units <= 0:
            raise ValueError("batch_utf16_units must be positive")
        self.batch_utf16_units = min(int(batch_utf16_units), 1024)

    def inject(self, text: str) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("WindowsSendInputInjector requires Windows.")
        if not text:
            return
        for chunk in _utf16_unit_chunks(text, self.batch_utf16_units):
            _send_unicode_units(chunk)

    def inject_targeted(self, text: str, target: object) -> None:
        from dcent_voice.inject.windows_focus import send_targeted_edit_text

        send_targeted_edit_text(target, text)

    def inject_checked(self, text: str, target: object) -> None:
        from dcent_voice.inject.windows_focus import (
            WindowsFocusTarget,
            focus_is_unchanged,
            require_focus_unchanged,
            restore_foreground,
        )

        if not isinstance(target, WindowsFocusTarget):
            raise TypeError("target must be a WindowsFocusTarget")
        # Windows offers no HWND-bound SendInput primitive. Restore the
        # press-time window after CoreWindow/overlay steal, then re-check.
        if not focus_is_unchanged(target):
            restore_foreground(int(target.top_hwnd))
        require_focus_unchanged(target)
        self.inject(text)

    def retract(self, char_count: int) -> None:
        if char_count <= 0:
            return
        if platform.system() != "Windows":
            raise RuntimeError("WindowsSendInputInjector requires Windows.")
        # Share the shipped, validated Win32 INPUT layout and bounded batching.
        from dcent_voice.inject.clipboard import send_backspaces

        send_backspaces(int(char_count))

    def press_enter(self) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("WindowsSendInputInjector requires Windows.")
        from dcent_voice.inject.clipboard import send_enter

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


def _chunks(text: str, size: int):
    for index in range(0, len(text), size):
        yield text[index : index + size]


def _utf16_units(text: str) -> list[int]:
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    return [
        int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)
    ]


def is_lossless_windows_keystroke_text(text: str) -> bool:
    """Whether KEYEVENTF_UNICODE is exact for the shipped short-text route.

    Windows controls commonly canonicalize line endings and interpret C0/C1
    controls as commands. Combining marks, emoji, ZWJ sequences, and ordinary
    international text remain layout-independent Unicode and are accepted.
    """
    return all(
        character not in {"\r", "\n", "\t"}
        and unicodedata.category(character) not in {"Cc", "Cs", "Zl", "Zp"}
        for character in text
    )


def _utf16_unit_chunks(text: str, size: int):
    units = _utf16_units(text)
    for index in range(0, len(units), size):
        yield units[index : index + size]


def _send_unicode_units(units: list[int]) -> None:
    # Import lazily so this module remains importable on non-Windows hosts.
    from dcent_voice.inject import clipboard

    keyeventf_unicode = 0x0004
    inputs: list[clipboard.INPUT] = []
    for unit in units:
        key_down = clipboard.KEYBDINPUT(0, unit, keyeventf_unicode, 0, 0)
        key_up = clipboard.KEYBDINPUT(
            0,
            unit,
            keyeventf_unicode | clipboard.KEYEVENTF_KEYUP,
            0,
            0,
        )
        inputs.append(
            clipboard.INPUT(
                type=clipboard.INPUT_KEYBOARD,
                union=clipboard.INPUT_UNION(ki=key_down),
            )
        )
        inputs.append(
            clipboard.INPUT(
                type=clipboard.INPUT_KEYBOARD,
                union=clipboard.INPUT_UNION(ki=key_up),
            )
        )
    array = (clipboard.INPUT * len(inputs))(*inputs)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(clipboard.INPUT),
        ctypes.c_int,
    ]
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(clipboard.INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())
