# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Register and monitor global dictation hotkeys."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dcent_voice.config import APP_NAME, HotkeyConfig
from dcent_voice.events import (
    AppMode,
    EventBus,
    HotkeyHealthChanged,
    HotkeyPressed,
    HotkeyReleased,
)

logger = logging.getLogger(APP_NAME).getChild("hotkeys")

KEY_ALIASES = {
    "control": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "cmd": "win",
    "cmd_l": "win",
    "cmd_r": "win",
    "windows": "win",
    "alt_l": "alt",
    "alt_r": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
}

# Watchdog tick while a chord is down or keys are held (stuck-chord recovery).
_WATCHDOG_INTERVAL_S = 2.0
# Idle tick: dead-listener / prophylactic rebind only. 2 s forever would
# wake the process ~1800 times/hour for no user-visible work.
_WATCHDOG_IDLE_INTERVAL_S = 15.0
# After this many consecutive failed restarts, status becomes "dead".
_DEAD_AFTER_FAILURES = 3
# Backoff sequence for restart attempts (seconds).
_RESTART_BACKOFF_S = (1.0, 2.0, 5.0, 15.0, 30.0)
# Windows LL-hooks can zombie (thread alive, no callbacks). Rebind periodically
# when not mid-chord so PTT never stays silently dead after long uptime.
_PROPHYLACTIC_REBIND_S = 900.0


ListenerFactory = Callable[[Callable[[Any], None], Callable[[Any], None]], Any]


@dataclass(frozen=True)
class Chord:
    mode: AppMode
    keys: frozenset[str]


@dataclass(frozen=True)
class HotkeyStatus:
    """Snapshot for /health and tray tooling."""

    enabled: bool
    status: str  # ok | recovering | dead | stopped
    listener_running: bool
    last_event_age_s: float | None
    restarts: int
    consecutive_failures: int
    detail: str = ""


class HotkeyManager:
    """Global push-to-talk chords with stuck-key and dead-listener recovery.

    pynput's Windows keyboard hook can stop delivering events after sleep,
    session lock, RDP, or hook-chain breakage while the process stays alive.
    The watchdog restarts a dead listener so hotkeys never stay silently dead.
    """

    def __init__(
        self,
        config: HotkeyConfig,
        bus: EventBus,
        *,
        stuck_timeout_s: float = 60.0,
        watchdog_interval_s: float = _WATCHDOG_INTERVAL_S,
        watchdog_idle_interval_s: float | None = None,
        listener_factory: ListenerFactory | None = None,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.bus = bus
        self.stuck_timeout_s = stuck_timeout_s
        self.watchdog_interval_s = watchdog_interval_s
        # Production: 2 s while a chord is held, 15 s when idle.
        # Tests that only shorten the active interval keep idle == active so
        # existing restart/stuck cases still finish quickly.
        if watchdog_idle_interval_s is not None:
            self.watchdog_idle_interval_s = watchdog_idle_interval_s
        elif watchdog_interval_s == _WATCHDOG_INTERVAL_S:
            self.watchdog_idle_interval_s = _WATCHDOG_IDLE_INTERVAL_S
        else:
            self.watchdog_idle_interval_s = watchdog_interval_s
        self._listener_factory = listener_factory
        self._time = time_fn
        self._prophylactic_rebind_s = _PROPHYLACTIC_REBIND_S
        self._chords = tuple(
            chord
            for chord in (
                _parse_chord(AppMode.DICTATION, config.dictation),
                _parse_chord(AppMode.COMMAND, config.command),
                _parse_chord(AppMode.STREAMING, config.streaming),
            )
            if chord.keys
        )
        self._pressed: set[str] = set()
        self._active: AppMode | None = None
        self._active_since = 0.0
        # Toggle mode: first chord starts recording; second chord stops. Chord
        # must fully release before another edge is accepted.
        self._toggle_recording = False
        self._toggle_chord_down = False
        self._lock = threading.Lock()
        # Serializes listener lifecycle transitions. Callback state uses the
        # separate short-held _lock so hook-thread joins never deadlock.
        self._lifecycle_lock = threading.Lock()
        self._listener: Any | None = None
        self._listener_generation = 0
        self._ever_started = False
        self._started = False
        self._stopped = False
        self._watchdog_stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._status = "stopped"
        self._status_detail = ""
        self._restarts = 0
        self._consecutive_failures = 0
        self._last_event_at: float | None = None
        self._listener_bound_at: float | None = None
        self._next_restart_at = 0.0
        self._health_published = ""

    def start(self) -> None:
        with self._lifecycle_lock:
            # Recheck only after taking the lifecycle lock: stop() may have won
            # the race while this caller was waiting.
            if self._stopped:
                raise RuntimeError(
                    "HotkeyManager cannot be restarted after stop(); create a new instance."
                )
            if self._started and self._listener_alive():
                return
            self._started = True
            self._watchdog_stop.clear()
            ok = self._bind_listener(count_as_restart=False)
            self._ensure_watchdog()
        if not ok:
            logger.error("global hotkey listener failed to start")

    def stop(self, *, finalize_active: bool = False) -> None:
        """Permanently stop hooks, abandoning an active chord by default.

        Application shutdown must not synthesize a release that starts ASR
        during teardown. A deliberate live manager replacement can explicitly
        request the old finalize behavior.
        """
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            self._started = False
            self._watchdog_stop.set()
            watchdog = self._watchdog
            self._watchdog = None
            with self._lock:
                active = self._active
                self._active = None
                self._pressed.clear()
                self._active_since = 0.0
                self._status = "stopped"
                self._status_detail = "stopped"
                listener = self._detach_listener_unlocked()

        # pynput stop/join can wait for its hook thread. Never perform either
        # while holding the lifecycle or callback-state lock.
        self._stop_listener(listener)
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=1.0)
        self._join_listener(listener)
        if finalize_active and active is not None:
            self.bus.publish(HotkeyReleased(active))
        self._publish_health("stopped", "stopped")

    def force_rebind(self, *, reason: str = "manual", skip_if_active: bool = False) -> bool:
        """Drop and recreate the keyboard listener (e.g. after session unlock)."""
        old_listener = None
        with self._lifecycle_lock:
            # The old pre-lock check allowed stop() to win and this method to
            # install a fresh hook afterwards. Recheck after serialization.
            with self._lock:
                if self._stopped or not self._started:
                    return False
                # A partial chord has no active mode yet, but dropping it loses
                # modifier releases and can create stuck key state.
                if skip_if_active and (self._active is not None or self._pressed):
                    return False
                was_active = self._active
                self._active = None
                self._pressed.clear()
                self._active_since = 0.0
                old_listener = self._detach_listener_unlocked()
            logger.info("forcing hotkey rebind (%s)", reason)
            self._stop_listener(old_listener)
            ok = self._bind_listener(count_as_restart=True)

        self._join_listener(old_listener)
        if was_active is not None:
            self.bus.publish(HotkeyReleased(was_active))
        return ok

    def status(self) -> HotkeyStatus:
        now = self._time()
        with self._lock:
            last = self._last_event_at
            age = None if last is None else max(0.0, now - last)
            return HotkeyStatus(
                enabled=self._started and not self._stopped,
                status=self._status,
                listener_running=self._listener_alive_unlocked(),
                last_event_age_s=age,
                restarts=self._restarts,
                consecutive_failures=self._consecutive_failures,
                detail=self._status_detail,
            )

    def _toggle_mode(self) -> bool:
        return str(getattr(self.config, "mode", "hold") or "hold").lower() == "toggle"

    def _on_press(self, key: Any, *, generation: int | None = None) -> None:
        name = normalize_key(key)
        if not name:
            return
        events: list[Any] = []
        with self._lock:
            if not self._accept_callback_unlocked(generation):
                return
            # Any key proves the LL-hook is delivering callbacks (zombie detection).
            self._last_event_at = self._time()
            self._pressed.add(name)
            next_mode = self._best_match()
            if self._toggle_mode():
                # Rising edge of a full chord: start or stop recording.
                if next_mode is not None and not self._toggle_chord_down:
                    self._toggle_chord_down = True
                    if self._toggle_recording:
                        mode = self._active or next_mode
                        events.append(HotkeyReleased(mode))
                        self._toggle_recording = False
                        self._active = None
                        self._active_since = 0.0
                        logger.info("hotkey toggle stop mode=%s", mode.value)
                    else:
                        self._active = next_mode
                        self._active_since = self._last_event_at
                        self._toggle_recording = True
                        events.append(_pressed_event(next_mode))
                        logger.info("hotkey toggle start mode=%s", next_mode.value)
            elif next_mode is not None and next_mode is not self._active:
                previous = self._active
                self._active = next_mode
                self._active_since = self._last_event_at
                if previous is not None:
                    events.append(HotkeyReleased(previous))
                events.append(_pressed_event(next_mode))
                logger.info("hotkey pressed mode=%s", next_mode.value)
        for event in events:
            self.bus.publish(event)

    def _on_release(self, key: Any, *, generation: int | None = None) -> None:
        name = normalize_key(key)
        if not name:
            return
        event = None
        with self._lock:
            if not self._accept_callback_unlocked(generation):
                return
            self._last_event_at = self._time()
            self._pressed.discard(name)
            if self._toggle_mode():
                # Key-up never ends the utterance; only clears chord-down latch.
                if self._best_match() is None:
                    self._toggle_chord_down = False
            elif self._active is not None:
                active_keys = self._keys_for_mode(self._active)
                if not active_keys.issubset(self._pressed):
                    event = HotkeyReleased(self._active)
                    logger.info("hotkey released mode=%s", self._active.value)
                    self._active = None
                    self._active_since = 0.0
        if event is not None:
            self.bus.publish(event)

    def _best_match(self) -> AppMode | None:
        matches = [chord for chord in self._chords if chord.keys.issubset(self._pressed)]
        if not matches:
            return None
        return max(matches, key=lambda chord: len(chord.keys)).mode

    def _keys_for_mode(self, mode: AppMode) -> frozenset[str]:
        for chord in self._chords:
            if chord.mode is mode:
                return chord.keys
        return frozenset()

    def _ensure_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="HotkeyWatchdog", daemon=True
        )
        self._watchdog.start()

    def _watchdog_sleep_s(self) -> float:
        """Longer sleep when nothing is held so idle CPU stays near zero."""
        with self._lock:
            busy = self._active is not None or bool(self._pressed)
        return self.watchdog_interval_s if busy else self.watchdog_idle_interval_s

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._watchdog_sleep_s()):
            try:
                self._watchdog_tick()
            except Exception:
                logger.exception("hotkey watchdog tick failed")

    def _watchdog_tick(self) -> None:
        event = None
        with self._lock:
            active = self._active
            stuck = active is not None and self._time() - self._active_since > self.stuck_timeout_s
            if stuck:
                assert active is not None
                logger.warning(
                    "hotkey stuck for >%.0fs; synthesizing release mode=%s",
                    self.stuck_timeout_s,
                    active.value,
                )
                event = HotkeyReleased(active)
                self._active = None
                self._pressed.clear()
                self._active_since = 0.0
        if event is not None:
            self.bus.publish(event)

        if self._stopped or not self._started:
            return
        if self._listener_alive():
            if self._should_prophylactic_rebind():
                logger.info("prophylactic hotkey rebind (listener age threshold)")
                # skip_if_active avoids aborting mid-hold if the user pressed PTT
                # between the check and rebind (TOCTOU).
                self.force_rebind(reason="prophylactic", skip_if_active=True)
                return
            snap = self.status()
            if snap.status != "ok":
                with self._lock:
                    self._status = "ok"
                    self._status_detail = "listener running"
                    self._consecutive_failures = 0
                self._publish_health("ok", "listener running")
            return

        now = self._time()
        with self._lock:
            if now < self._next_restart_at:
                return
        logger.error("global hotkey listener is not running; attempting rebind")
        with self._lock:
            self._status = "recovering"
            self._status_detail = "listener not running"
        self._publish_health("recovering", "listener not running")
        # Clear stuck chord state the same way force_rebind does.
        ok = self.force_rebind(reason="listener_dead")
        if not ok:
            with self._lock:
                fails = self._consecutive_failures
                idx = min(max(fails - 1, 0), len(_RESTART_BACKOFF_S) - 1)
                self._next_restart_at = self._time() + _RESTART_BACKOFF_S[idx]
                if fails >= _DEAD_AFTER_FAILURES:
                    self._status = "dead"
                    self._status_detail = f"rebind failed {fails} times"
                    detail = self._status_detail
                else:
                    detail = self._status_detail
            if fails >= _DEAD_AFTER_FAILURES:
                self._publish_health("dead", detail)

    def _should_prophylactic_rebind(self) -> bool:
        """True when the listener has been bound too long without a mid-hold."""
        with self._lock:
            if self._active is not None or self._pressed:
                return False
            bound = self._listener_bound_at
            if bound is None:
                return False
            return (self._time() - bound) >= self._prophylactic_rebind_s

    def _listener_alive(self) -> bool:
        with self._lock:
            return self._listener_alive_unlocked()

    def _listener_alive_unlocked(self) -> bool:
        listener = self._listener
        if listener is None:
            return False
        running = getattr(listener, "running", None)
        if running is False:
            return False
        is_alive = getattr(listener, "is_alive", None)
        if callable(is_alive):
            try:
                if not is_alive():
                    return False
            except Exception:
                return False
        if running is None and not callable(is_alive):
            return True
        return True if running is None else bool(running)

    def _bind_listener(self, *, count_as_restart: bool) -> bool:
        with self._lock:
            self._listener_generation += 1
            generation = self._listener_generation
        try:
            listener = self._create_listener(generation)
            listener.start()
        except Exception as exc:
            logger.exception("failed to start hotkey listener: %s", exc)
            with self._lock:
                # Fence a listener that partially started before raising.
                if self._listener_generation == generation:
                    self._listener_generation += 1
                self._listener = None
                self._consecutive_failures += 1
                fails = self._consecutive_failures
                if fails >= _DEAD_AFTER_FAILURES:
                    self._status = "dead"
                    self._status_detail = f"{type(exc).__name__}: {exc}"
                else:
                    self._status = "recovering"
                    self._status_detail = f"{type(exc).__name__}: {exc}"
                detail = self._status_detail
                status = self._status
            self._publish_health(status, detail)
            return False

        with self._lock:
            self._listener = listener
            if count_as_restart and self._ever_started:
                self._restarts += 1
            self._ever_started = True
            self._consecutive_failures = 0
            self._next_restart_at = 0.0
            self._listener_bound_at = self._time()
            self._status = "ok"
            self._status_detail = "listener running"
            restarts = self._restarts
        logger.info("hotkey listener running (restarts=%s)", restarts)
        self._publish_health("ok", "listener running")
        return True

    def _create_listener(self, generation: int) -> Any:
        def on_press(key: Any) -> None:
            self._on_press(key, generation=generation)

        def on_release(key: Any) -> None:
            self._on_release(key, generation=generation)

        if self._listener_factory is not None:
            return self._listener_factory(on_press, on_release)
        try:
            from pynput import keyboard
        except ImportError as exc:  # pragma: no cover - dependency/environment specific
            raise RuntimeError("pynput is required for global hotkeys.") from exc
        return keyboard.Listener(on_press=on_press, on_release=on_release)

    def _accept_callback_unlocked(self, generation: int | None) -> bool:
        if self._stopped or not self._started:
            return False
        return generation is None or generation == self._listener_generation

    def _detach_listener_unlocked(self) -> Any | None:
        """Detach and generation-fence the listener while _lock is held."""
        listener = self._listener
        self._listener = None
        self._listener_generation += 1
        return listener

    @staticmethod
    def _stop_listener(listener: Any | None) -> None:
        if listener is not None:
            with contextlib.suppress(Exception):
                listener.stop()

    @staticmethod
    def _join_listener(listener: Any | None, timeout: float = 1.0) -> None:
        if listener is None or listener is threading.current_thread():
            return
        join = getattr(listener, "join", None)
        if callable(join):
            with contextlib.suppress(Exception):
                join(timeout=timeout)

    def _publish_health(self, status: str, detail: str) -> None:
        key = f"{status}|{detail}"
        with self._lock:
            if key == self._health_published:
                return
            self._health_published = key
        self.bus.publish(HotkeyHealthChanged(status=status, detail=detail))


def _parse_chord(mode: AppMode, value: str) -> Chord:
    if (value or "").strip().lower() in {"", "off", "none", "disabled"}:
        return Chord(mode=mode, keys=frozenset())
    keys = frozenset(part.strip().lower() for part in value.split("+") if part.strip())
    normalized = frozenset(KEY_ALIASES.get(key, key) for key in keys)
    return Chord(mode=mode, keys=normalized)


def _pressed_event(mode: AppMode) -> HotkeyPressed:
    """Capture the destination during the actual global-key callback."""

    target = None
    try:
        import platform

        if platform.system() == "Windows":
            from dcent_voice.inject.windows_focus import capture_foreground_target

            target = capture_foreground_target()
    except Exception:
        # Non-native/unaddressable targets retain the existing guarded fallback.
        logger.exception("failed to capture Windows focus in hotkey callback")
    return HotkeyPressed(mode, focus_target=target)


def normalize_key(key: Any) -> str:
    name = ""
    if hasattr(key, "char") and key.char:
        name = str(key.char).lower()
    elif hasattr(key, "name") and key.name:
        name = str(key.name).lower()
    else:
        text = str(key).lower()
        name = text.removeprefix("key.").strip("'")
    return KEY_ALIASES.get(name, name)
