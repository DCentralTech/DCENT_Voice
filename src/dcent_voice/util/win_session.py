# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Sleep / resume / session-unlock detection for hotkey rebind.

After sleep, long suspend, or Win+L unlock, pynput's keyboard hook may stop
delivering events. This monitor:

1. Detects multi-second jumps in the OS tick counter (sleep/resume).
2. On Windows, polls whether the input desktop is reachable (session lock).
   Transition locked → unlocked fires ``on_resume("session_unlock")``.

Previously a custom Win32 message window (WTS + power broadcast) caused native
access violations mid-PTT. Polling only — no custom HWND — avoids that path.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import threading
from collections.abc import Callable

from dcent_voice.config import APP_NAME

logger = logging.getLogger(APP_NAME).getChild("win_session")

# Wall wait between polls.
_POLL_INTERVAL_S = 2.0
# Tick advance larger than this (ms) while we only waited ~poll interval ⇒ sleep.
_TICK_GAP_MS = 8_000
# DESKTOP_SWITCHDESKTOP — enough to probe OpenInputDesktop without full rights.
_DESKTOP_SWITCHDESKTOP = 0x0100


class SessionResumeMonitor:
    """Invoke ``on_resume`` after sleep/resume or session unlock."""

    def __init__(
        self,
        on_resume: Callable[[str], None],
        *,
        poll_interval_s: float = _POLL_INTERVAL_S,
        tick_gap_ms: int = _TICK_GAP_MS,
        tick_fn: Callable[[], int] | None = None,
        locked_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._on_resume = on_resume
        self._poll_interval_s = poll_interval_s
        self._tick_gap_ms = tick_gap_ms
        self._tick_fn = tick_fn
        # When provided (tests), replaces OpenInputDesktop probing.
        self._locked_fn = locked_fn
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if platform.system() != "Windows" and self._tick_fn is None and self._locked_fn is None:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="WinSessionResume", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            self._poll_loop()
        except Exception:
            logger.exception("session resume monitor failed")

    def _read_tick(self) -> int:
        if self._tick_fn is not None:
            return int(self._tick_fn())
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return int(kernel32.GetTickCount64())

    def _is_session_locked(self) -> bool:
        if self._locked_fn is not None:
            return bool(self._locked_fn())
        if platform.system() != "Windows":
            return False
        return _windows_input_desktop_locked()

    def _poll_loop(self) -> None:
        last = self._read_tick()
        was_locked = self._is_session_locked()
        while not self._stop.wait(self._poll_interval_s):
            now = self._read_tick()
            # Unsigned wrap is ~49 days for 32-bit; GetTickCount64 is 64-bit.
            delta = now - last
            if delta > self._tick_gap_ms:
                self._safe_resume("tick_gap_resume")
            last = now

            locked = self._is_session_locked()
            if was_locked and not locked:
                self._safe_resume("session_unlock")
            was_locked = locked

    def _safe_resume(self, reason: str) -> None:
        logger.info("session resume signal: %s", reason)
        try:
            self._on_resume(reason)
        except Exception:
            logger.exception("on_resume handler failed (%s)", reason)


def _windows_input_desktop_locked() -> bool:
    """True when the secure desktop owns input (Win+L / UAC), without HWND APIs.

    ``OpenInputDesktop`` fails while the session is locked. No custom window or
    WTS message pump — probe only.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL

    handle = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
    if not handle:
        return True
    with contextlib.suppress(Exception):
        user32.CloseDesktop(handle)
    return False
