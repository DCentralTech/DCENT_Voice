# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Block on threading events without a timeout poll."""

from __future__ import annotations

import threading


def wait_any(*events: threading.Event, timeout: float | None = None) -> bool:
    """Return True when any event is set.

    The caller thread does not wake on a timer. Each event is waited on by a
    daemon watcher that sleeps inside ``Event.wait``. Idle tray loops must use
    this instead of ``wait(0.25)`` / ``wait(0.1)`` polling.
    """
    if not events:
        raise ValueError("wait_any requires at least one event")
    if any(event.is_set() for event in events):
        return True
    if timeout is not None and timeout <= 0:
        return False

    done = threading.Event()

    def _watch(event: threading.Event) -> None:
        event.wait()
        done.set()

    for event in events:
        threading.Thread(
            target=_watch,
            args=(event,),
            name="dcent-wait-any",
            daemon=True,
        ).start()
    return done.wait(timeout)
