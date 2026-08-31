# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Coalesce bursty background work into latest-value jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class CoalescingWorker(Generic[T]):
    """Run one callback at a time and collapse pending work to the latest item."""

    def __init__(self, callback: Callable[[T], None], *, name: str) -> None:
        self._callback = callback
        self._pending = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: T | None = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def request(self, item: T) -> None:
        if self._stop.is_set():
            return
        with self._lock:
            self._latest = item
        self._pending.set()

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        self._pending.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while True:
            self._pending.wait()
            self._pending.clear()
            if self._stop.is_set():
                return
            with self._lock:
                item = self._latest
                self._latest = None
            if item is not None:
                self._callback(item)
