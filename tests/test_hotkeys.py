# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import threading
import time

from dcent_voice.config import HotkeyConfig
from dcent_voice.events import AppMode, EventBus, HotkeyHealthChanged, HotkeyPressed, HotkeyReleased
from dcent_voice.hotkeys import HotkeyManager, _parse_chord, normalize_key


def test_parse_chord_off_is_empty() -> None:
    assert _parse_chord(AppMode.COMMAND, "off").keys == frozenset()
    assert _parse_chord(AppMode.STREAMING, "none").keys == frozenset()


def test_idle_watchdog_sleeps_longer_when_no_keys() -> None:
    manager, bus, listeners = _make_manager(
        watchdog_interval_s=2.0,
        watchdog_idle_interval_s=15.0,
    )
    manager.start()
    assert manager._watchdog_sleep_s() == 15.0
    listeners[0].on_press(_FakeKey("ctrl"))
    assert manager._watchdog_sleep_s() == 2.0
    listeners[0].on_release(_FakeKey("ctrl"))
    assert manager._watchdog_sleep_s() == 15.0
    manager.stop()
    bus.stop()


def test_default_watchdog_uses_idle_interval() -> None:
    bus = EventBus()
    bus.start()
    manager = HotkeyManager(
        HotkeyConfig(),
        bus,
        listener_factory=lambda *_args: _FakeListener(*_args),
    )
    assert manager.watchdog_interval_s == 2.0
    assert manager.watchdog_idle_interval_s == 15.0
    manager.stop()
    bus.stop()


def test_default_hotkeys_register_only_dictation() -> None:
    bus = EventBus()
    bus.start()
    manager = HotkeyManager(
        HotkeyConfig(),
        bus,
        listener_factory=lambda *_args: _FakeListener(*_args),
    )
    modes = {chord.mode for chord in manager._chords}
    assert modes == {AppMode.DICTATION}
    manager.stop()
    bus.stop()


def test_parse_chord_normalizes_aliases() -> None:
    chord = _parse_chord(AppMode.COMMAND, "control+windows+alt")

    assert chord.keys == frozenset({"ctrl", "win", "alt"})


def test_normalize_key_from_pynput_style_name() -> None:
    class Key:
        name = "ctrl_l"

    assert normalize_key(Key()) == "ctrl"


class _FakeKey:
    def __init__(self, name: str) -> None:
        self.name = name
        self.char = None


class _FakeListener:
    def __init__(self, on_press, on_release, *, die_after: int | None = None) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self.running = False
        self._alive = False
        self.die_after = die_after
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.die_after is not None and self.start_calls > self.die_after:
            raise RuntimeError("bind failed")
        self.running = True
        self._alive = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self.running = False
        self._alive = False


def _make_manager(
    *,
    bus: EventBus | None = None,
    stuck_timeout_s: float = 60.0,
    watchdog_interval_s: float = 0.05,
    watchdog_idle_interval_s: float | None = None,
    die_after: int | None = None,
    clock: list[float] | None = None,
) -> tuple[HotkeyManager, EventBus, list[_FakeListener]]:
    bus = bus or EventBus()
    bus.start()
    listeners: list[_FakeListener] = []

    def factory(on_press, on_release):
        listener = _FakeListener(on_press, on_release, die_after=die_after)
        listeners.append(listener)
        return listener

    times = clock if clock is not None else [0.0]

    def time_fn() -> float:
        return times[0]

    manager = HotkeyManager(
        HotkeyConfig(dictation="ctrl+win", command="ctrl+win+alt", streaming="ctrl+win+shift"),
        bus,
        stuck_timeout_s=stuck_timeout_s,
        watchdog_interval_s=watchdog_interval_s,
        watchdog_idle_interval_s=watchdog_idle_interval_s,
        listener_factory=factory,
        time_fn=time_fn,
    )
    return manager, bus, listeners


def test_toggle_mode_latches_until_second_press() -> None:
    bus = EventBus()
    bus.start()
    received: list[object] = []
    pressed = threading.Event()
    released = threading.Event()
    bus.subscribe(
        lambda ev: (
            received.append(ev),
            pressed.set() if isinstance(ev, HotkeyPressed) else None,
            released.set() if isinstance(ev, HotkeyReleased) else None,
        )
    )
    listeners: list[_FakeListener] = []

    def factory(on_press, on_release):
        listener = _FakeListener(on_press, on_release)
        listeners.append(listener)
        return listener

    manager = HotkeyManager(
        HotkeyConfig(mode="toggle", dictation="ctrl+win"),
        bus,
        listener_factory=factory,
        time_fn=lambda: 0.0,
    )
    manager.start()
    listener = listeners[0]
    listener.on_press(_FakeKey("ctrl"))
    listener.on_press(_FakeKey("cmd"))  # cmd aliases to win
    assert pressed.wait(1.0)
    assert any(isinstance(ev, HotkeyPressed) for ev in received)
    received.clear()
    released.clear()
    # Key-up must NOT stop recording in toggle mode.
    listener.on_release(_FakeKey("cmd"))
    listener.on_release(_FakeKey("ctrl"))
    time.sleep(0.05)
    assert not any(isinstance(ev, HotkeyReleased) for ev in received)
    # Second chord ends recording.
    listener.on_press(_FakeKey("ctrl"))
    listener.on_press(_FakeKey("cmd"))
    assert released.wait(1.0)
    assert any(isinstance(ev, HotkeyReleased) for ev in received)
    manager.stop()
    bus.stop()


def test_hotkey_press_release_publishes_events() -> None:
    manager, bus, listeners = _make_manager()
    received: list[object] = []
    pressed = threading.Event()
    released = threading.Event()
    bus.subscribe(
        lambda ev: (
            received.append(ev),
            pressed.set() if isinstance(ev, HotkeyPressed) else None,
            released.set() if isinstance(ev, HotkeyReleased) else None,
        )
    )
    manager.start()
    assert listeners[0].running is True

    listeners[0].on_press(_FakeKey("ctrl"))
    listeners[0].on_press(_FakeKey("cmd"))
    assert pressed.wait(1.0)
    assert any(isinstance(e, HotkeyPressed) and e.mode is AppMode.DICTATION for e in received)

    listeners[0].on_release(_FakeKey("cmd"))
    assert released.wait(1.0)
    assert any(isinstance(e, HotkeyReleased) for e in received)

    manager.stop()
    bus.stop()


def test_any_key_updates_last_event_age() -> None:
    clock = [10.0]
    manager, bus, listeners = _make_manager(clock=clock)
    manager.start()
    assert manager.status().last_event_age_s is None

    listeners[0].on_press(_FakeKey("a"))
    clock[0] = 12.5
    age = manager.status().last_event_age_s
    assert age is not None
    assert 2.4 <= age <= 2.6

    manager.stop()
    bus.stop()


def test_prophylactic_rebind_after_bound_age() -> None:
    clock = [0.0]
    manager, bus, listeners = _make_manager(watchdog_interval_s=0.05, clock=clock)
    manager._prophylactic_rebind_s = 5.0
    manager.start()
    assert len(listeners) == 1

    clock[0] = 6.0
    deadline = time.time() + 2.0
    while time.time() < deadline and len(listeners) < 2:
        time.sleep(0.05)

    assert len(listeners) >= 2
    assert manager.status().restarts >= 1
    assert manager.status().status == "ok"

    manager.stop()
    bus.stop()


def test_concurrent_force_rebind_serializes() -> None:
    manager, bus, listeners = _make_manager()
    manager.start()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(8):
                manager.force_rebind(reason="race")
        except BaseException as exc:  # pragma: no cover - surface in assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors
    # Exactly one live listener; earlier ones must have been stopped.
    alive = [listener for listener in listeners if listener.running]
    assert len(alive) == 1
    assert sum(listener.stop_calls for listener in listeners) >= len(listeners) - 1

    manager.stop()
    bus.stop()


def test_watchdog_restarts_dead_listener() -> None:
    manager, bus, listeners = _make_manager(watchdog_interval_s=0.05)
    health: list[HotkeyHealthChanged] = []
    bus.subscribe(lambda ev: health.append(ev) if isinstance(ev, HotkeyHealthChanged) else None)
    manager.start()
    assert len(listeners) == 1

    listeners[0].kill()
    deadline = time.time() + 2.0
    while time.time() < deadline and len(listeners) < 2:
        time.sleep(0.05)

    assert len(listeners) >= 2
    assert listeners[-1].running is True
    assert manager.status().restarts >= 1
    assert manager.status().status == "ok"
    health_deadline = time.monotonic() + 1.0
    while not any(h.status == "recovering" for h in health) and time.monotonic() < health_deadline:
        time.sleep(0.01)
    assert any(h.status == "recovering" for h in health)

    manager.stop()
    bus.stop()


def test_stuck_chord_synthesizes_release() -> None:
    clock = [0.0]
    manager, bus, listeners = _make_manager(
        stuck_timeout_s=0.1, watchdog_interval_s=0.05, clock=clock
    )
    releases: list[HotkeyReleased] = []
    done = threading.Event()
    bus.subscribe(
        lambda ev: (
            (
                releases.append(ev),
                done.set(),
            )
            if isinstance(ev, HotkeyReleased)
            else None
        )
    )
    manager.start()
    listeners[0].on_press(_FakeKey("ctrl"))
    listeners[0].on_press(_FakeKey("cmd"))
    clock[0] = 1.0  # past stuck timeout
    assert done.wait(2.0)
    assert releases and releases[0].mode is AppMode.DICTATION

    manager.stop()
    bus.stop()


def test_status_reports_dead_after_repeated_failures() -> None:
    manager, bus, listeners = _make_manager(
        watchdog_interval_s=0.05,
        die_after=0,  # every start fails
    )
    # First start fails immediately.
    manager.start()
    assert manager.status().status in {"recovering", "dead"}
    # Drive more failures via watchdog / force rebind.
    for _ in range(5):
        manager.force_rebind(reason="test")
        time.sleep(0.02)
    snap = manager.status()
    assert snap.consecutive_failures >= 1
    # Eventually dead after threshold.
    deadline = time.time() + 2.0
    while time.time() < deadline and manager.status().status != "dead":
        manager._watchdog_tick()
        time.sleep(0.02)
    assert manager.status().status == "dead"
    assert listeners  # factory was called

    manager.stop()
    bus.stop()


def test_force_rebind_creates_new_listener() -> None:
    manager, bus, listeners = _make_manager()
    manager.start()
    first = listeners[0]
    assert manager.force_rebind(reason="session_unlock") is True
    assert len(listeners) == 2
    assert first.stop_calls >= 1
    assert listeners[1].running is True
    assert manager.status().restarts >= 1

    manager.stop()
    bus.stop()


def test_prophylactic_rebind_skips_partial_chord() -> None:
    clock = [0.0]
    manager, bus, listeners = _make_manager(watchdog_interval_s=10.0, clock=clock)
    manager._prophylactic_rebind_s = 5.0
    manager.start()

    # Ctrl alone is not an active chord, but rebinding here would lose its
    # eventual release and poison the next chord's pressed-key state.
    listeners[0].on_press(_FakeKey("ctrl"))
    clock[0] = 6.0
    manager._watchdog_tick()
    assert len(listeners) == 1

    listeners[0].on_release(_FakeKey("ctrl"))
    manager._watchdog_tick()
    assert len(listeners) == 2
    assert listeners[-1].running is True

    manager.stop()
    bus.stop()


def test_stop_abandons_active_chord_by_default() -> None:
    manager, bus, listeners = _make_manager(watchdog_interval_s=10.0)
    releases: list[HotkeyReleased] = []
    pressed = threading.Event()
    bus.subscribe(
        lambda ev: (
            pressed.set() if isinstance(ev, HotkeyPressed) else None,
            releases.append(ev) if isinstance(ev, HotkeyReleased) else None,
        )
    )
    manager.start()
    listeners[0].on_press(_FakeKey("ctrl"))
    listeners[0].on_press(_FakeKey("cmd"))
    assert pressed.wait(1.0)

    manager.stop()
    time.sleep(0.05)
    assert releases == []
    assert manager.status().status == "stopped"
    assert not any(listener.running for listener in listeners)

    bus.stop()


def test_stop_can_finalize_active_chord_once() -> None:
    manager, bus, listeners = _make_manager(watchdog_interval_s=10.0)
    releases: list[HotkeyReleased] = []
    released = threading.Event()
    bus.subscribe(
        lambda ev: (releases.append(ev), released.set()) if isinstance(ev, HotkeyReleased) else None
    )
    manager.start()
    listeners[0].on_press(_FakeKey("ctrl"))
    listeners[0].on_press(_FakeKey("cmd"))

    manager.stop(finalize_active=True)
    manager.stop(finalize_active=True)  # idempotent; never double-finalize
    assert released.wait(1.0)
    assert [event.mode for event in releases] == [AppMode.DICTATION]

    bus.stop()


def test_stale_listener_callbacks_are_generation_fenced() -> None:
    manager, bus, listeners = _make_manager(watchdog_interval_s=10.0)
    presses: list[HotkeyPressed] = []
    bus.subscribe(lambda ev: presses.append(ev) if isinstance(ev, HotkeyPressed) else None)
    manager.start()
    stale = listeners[0]
    assert manager.force_rebind(reason="generation_test") is True

    stale.on_press(_FakeKey("ctrl"))
    stale.on_press(_FakeKey("cmd"))
    time.sleep(0.05)
    assert presses == []

    # The current generation still works.
    listeners[-1].on_press(_FakeKey("ctrl"))
    listeners[-1].on_press(_FakeKey("cmd"))
    deadline = time.time() + 1.0
    while not presses and time.time() < deadline:
        time.sleep(0.01)
    assert len(presses) == 1

    manager.stop()
    bus.stop()


def test_stop_racing_force_rebind_never_reinstalls_listener() -> None:
    manager, bus, listeners = _make_manager(watchdog_interval_s=10.0)
    manager.start()
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def rebind() -> None:
        try:
            barrier.wait()
            for _ in range(25):
                manager.force_rebind(reason="stop_race")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def stop() -> None:
        try:
            barrier.wait()
            manager.stop()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    rebind_thread = threading.Thread(target=rebind)
    stop_thread = threading.Thread(target=stop)
    rebind_thread.start()
    stop_thread.start()
    barrier.wait()
    rebind_thread.join(timeout=5.0)
    stop_thread.join(timeout=5.0)

    assert not errors
    assert not rebind_thread.is_alive()
    assert not stop_thread.is_alive()
    assert manager.status().status == "stopped"
    assert manager.force_rebind(reason="after_stop") is False
    assert not any(listener.running for listener in listeners)

    bus.stop()
