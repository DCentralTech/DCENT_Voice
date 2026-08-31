# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import threading
import time

from dcent_voice.util.win_session import SessionResumeMonitor


def test_tick_gap_invokes_on_resume() -> None:
    ticks = [0, 100, 200, 15_000]  # jump > 8000 ms
    index = {"i": 0}
    lock = threading.Lock()
    resumes: list[str] = []
    done = threading.Event()

    def tick_fn() -> int:
        with lock:
            i = min(index["i"], len(ticks) - 1)
            value = ticks[i]
            if index["i"] < len(ticks) - 1:
                index["i"] += 1
            return value

    def on_resume(reason: str) -> None:
        resumes.append(reason)
        done.set()

    monitor = SessionResumeMonitor(
        on_resume,
        poll_interval_s=0.05,
        tick_gap_ms=8_000,
        tick_fn=tick_fn,
    )
    monitor.start()
    assert done.wait(2.0)
    assert resumes and resumes[0] == "tick_gap_resume"
    monitor.stop()


def test_stop_is_idempotent() -> None:
    monitor = SessionResumeMonitor(lambda _r: None, poll_interval_s=0.05, tick_fn=lambda: 0)
    monitor.start()
    time.sleep(0.1)
    monitor.stop()
    monitor.stop()


def test_session_unlock_invokes_on_resume() -> None:
    locked_states = [True, True, False]
    index = {"i": 0}
    resumes: list[str] = []
    done = threading.Event()

    def locked_fn() -> bool:
        i = min(index["i"], len(locked_states) - 1)
        value = locked_states[i]
        if index["i"] < len(locked_states) - 1:
            index["i"] += 1
        return value

    def on_resume(reason: str) -> None:
        resumes.append(reason)
        done.set()

    monitor = SessionResumeMonitor(
        on_resume,
        poll_interval_s=0.05,
        tick_gap_ms=8_000,
        tick_fn=lambda: 0,  # no tick gap
        locked_fn=locked_fn,
    )
    monitor.start()
    assert done.wait(2.0)
    assert resumes and resumes[0] == "session_unlock"
    monitor.stop()


def test_on_resume_exception_does_not_kill_monitor() -> None:
    ticks = [0, 20_000, 20_100, 40_000]
    index = {"i": 0}
    calls = {"n": 0}
    second = threading.Event()

    def tick_fn() -> int:
        i = min(index["i"], len(ticks) - 1)
        value = ticks[i]
        if index["i"] < len(ticks) - 1:
            index["i"] += 1
        return value

    def on_resume(_reason: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        second.set()

    monitor = SessionResumeMonitor(
        on_resume, poll_interval_s=0.05, tick_gap_ms=8_000, tick_fn=tick_fn
    )
    monitor.start()
    assert second.wait(2.0)
    assert calls["n"] >= 2
    monitor.stop()
