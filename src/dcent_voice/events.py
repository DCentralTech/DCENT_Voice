# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Define application events and the in-process event bus."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("DCENT_Voice.events")


class AppMode(Enum):
    DICTATION = "dictation"
    COMMAND = "command"
    STREAMING = "streaming"


@dataclass(frozen=True)
class HotkeyPressed:
    mode: AppMode
    # Captured synchronously in the OS keyboard callback. On Windows the Win
    # modifier can change foreground ownership before PipelineWorker drains its
    # queue, so a later focus lookup is not equivalent to the press target.
    focus_target: object | None = None


@dataclass(frozen=True)
class HotkeyReleased:
    mode: AppMode


@dataclass(frozen=True)
class WakeWordDetected:
    """A real local wake-word inference crossed its configured threshold."""

    phrase: str = "hey-dcent"


@dataclass(frozen=True)
class HotkeyHealthChanged:
    """Global hotkey subsystem status (ok | recovering | dead | stopped)."""

    status: str
    detail: str = ""


@dataclass(frozen=True)
class TranscriptReady:
    raw: str
    cleaned: str
    mode: AppMode
    timings: dict[str, float]
    injected: bool = True
    discarded: bool = False
    reason: str = ""


@dataclass(frozen=True)
class StateChanged:
    state: str
    mode: AppMode | None = None


@dataclass(frozen=True)
class PrivacyChanged:
    status: str
    detail: str = ""
    # Optional consent transition metadata. Defaults preserve the existing
    # constructor while allowing ADE and the Settings UI to distinguish an
    # enabled cloud path from one that was explicitly revoked or blocked.
    consent_state: str = ""
    reason: str = ""
    missing_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigChanged:
    patch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShutdownRequested:
    reason: str = ""


@dataclass(frozen=True)
class AsrReadyChanged:
    """Local ASR weights became resident or were released after idle."""

    ready: bool
    detail: str = ""


AppEvent = (
    HotkeyPressed
    | HotkeyReleased
    | WakeWordDetected
    | HotkeyHealthChanged
    | TranscriptReady
    | StateChanged
    | PrivacyChanged
    | ConfigChanged
    | ShutdownRequested
    | AsrReadyChanged
)

Subscriber = Callable[[AppEvent], None]


class EventBus:
    """Thread-safe, non-blocking event bus with a single dispatcher fanout thread."""

    def __init__(self, *, name: str = "DCENTEventBus") -> None:
        self._queue: queue.SimpleQueue[AppEvent | None] = queue.SimpleQueue()
        self._subscribers: list[Subscriber] = []
        self._lock = threading.RLock()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        if self._started.is_set():
            return
        self._started.set()
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if not self._started.is_set():
            return
        self._queue.put(None)
        self._thread.join(timeout)
        self._stopped.set()

    def publish(self, ev: AppEvent) -> None:
        self._queue.put(ev)

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    def _run(self) -> None:
        while True:
            ev = self._queue.get()
            if ev is None:
                return
            with self._lock:
                subscribers = tuple(self._subscribers)
            for fn in subscribers:
                try:
                    fn(ev)
                except Exception:
                    # Keep dispatching even if one consumer misbehaves; never
                    # swallow without a trail for pythonw / headless runs.
                    logger.exception("event bus subscriber failed on %s", type(ev).__name__)
