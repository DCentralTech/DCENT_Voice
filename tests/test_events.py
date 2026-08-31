# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import threading

from dcent_voice.events import AppMode, EventBus, HotkeyPressed


def test_event_bus_dispatches_to_subscriber() -> None:
    bus = EventBus()
    received = []
    done = threading.Event()
    bus.subscribe(lambda ev: (received.append(ev), done.set()))
    bus.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))

    assert done.wait(1.0)
    assert isinstance(received[0], HotkeyPressed)
    bus.stop()


def test_event_bus_keeps_dispatching_after_subscriber_error(caplog) -> None:
    bus = EventBus()
    good: list[object] = []
    done = threading.Event()

    def bad(_ev) -> None:
        raise RuntimeError("boom")

    def ok(ev) -> None:
        good.append(ev)
        done.set()

    bus.subscribe(bad)
    bus.subscribe(ok)
    bus.start()
    with caplog.at_level(logging.ERROR, logger="DCENT_Voice.events"):
        bus.publish(HotkeyPressed(AppMode.DICTATION))
        assert done.wait(1.0)
    assert good
    assert any("subscriber failed" in r.message for r in caplog.records)
    bus.stop()
