# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import threading
import time

from dcent_voice.util.coalescing import CoalescingWorker


def test_worker_coalesces_pending_items_while_callback_is_busy() -> None:
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    seen: list[int] = []

    def callback(value: int) -> None:
        seen.append(value)
        if value == 1:
            entered.set()
            release.wait(1.0)
        else:
            completed.set()

    worker = CoalescingWorker(callback, name="TestCoalescing")
    worker.start()
    worker.request(1)
    assert entered.wait(1.0)
    worker.request(2)
    worker.request(3)
    worker.request(4)
    release.set()

    assert completed.wait(1.0)
    assert seen == [1, 4]
    assert worker.stop()


def test_worker_stop_rejects_new_work_and_joins() -> None:
    seen: list[int] = []
    worker = CoalescingWorker(seen.append, name="TestCoalescingStop")
    worker.start()
    assert worker.stop()
    worker.request(1)
    time.sleep(0.01)

    assert seen == []
    assert worker.alive is False
