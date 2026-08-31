# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Idle-unload policy for local ASR weights.

Measured on this host (W40/W41): keep-warm ASR is ~0.2 s; a cold Parakeet
reload is ~4 s and ~688 MB. Default 600 s keeps a dictation session warm and
lets an all-day tray drop toward shell RSS. 0 disables unload (keep-warm).
"""

from __future__ import annotations

import contextlib
import gc
import threading
from collections.abc import Callable

# Keep-warm window after startup load or last utterance. Justified by reload
# cost (~4 s, shown on overlay) vs session ASR (~0.2 s). Not a 1.0 Mac/cert fix.
DEFAULT_IDLE_UNLOAD_S = 600.0

LifecycleListener = Callable[[bool, str], None]


class IdleUnloadMixin:
    """Timer + listener used by local ASR providers."""

    idle_unload_s: float
    _idle_timer: threading.Timer | None
    _lifecycle_listener: LifecycleListener | None
    _idle_lock: threading.RLock

    def init_idle_unload(self, idle_unload_s: float) -> None:
        self.idle_unload_s = float(idle_unload_s)
        self._idle_timer = None
        self._lifecycle_listener = None
        self._idle_lock = threading.RLock()

    def set_lifecycle_listener(self, listener: LifecycleListener | None) -> None:
        self._lifecycle_listener = listener

    def notify_lifecycle(self, ready: bool, detail: str) -> None:
        listener = self._lifecycle_listener
        if listener is None:
            return
        with contextlib.suppress(Exception):
            listener(ready, detail)

    def cancel_idle_timer(self) -> None:
        with self._idle_lock:
            timer = self._idle_timer
            self._idle_timer = None
        if timer is not None:
            timer.cancel()

    def arm_idle_timer(self, unload: Callable[[], None]) -> None:
        if not self.idle_unload_s or self.idle_unload_s <= 0:
            return
        with self._idle_lock:
            self.cancel_idle_timer()
            timer = threading.Timer(self.idle_unload_s, unload)
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    @staticmethod
    def collect_unloaded() -> None:
        gc.collect()
