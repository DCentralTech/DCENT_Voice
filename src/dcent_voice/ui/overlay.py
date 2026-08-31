# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Control the dictation status overlay."""

from __future__ import annotations

import contextlib
import json
import platform
import threading
from pathlib import Path
from typing import Any

from dcent_voice.audio.levels import AmplitudeMeter
from dcent_voice.config import OverlayConfig
from dcent_voice.pipeline import get_foreground_window
from dcent_voice.ui.overlay_win32 import apply_overlay_window_styles, overlay_position


class OverlayController:
    """Controls the visual status overlay for dictation."""

    def __init__(
        self,
        *,
        config: OverlayConfig,
        meter: AmplitudeMeter,
        width: int = 360,
        height: int = 190,
        assets_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.meter = meter
        self.width = width
        self.height = height
        self.assets_dir = assets_dir or Path(__file__).resolve().parent / "web"
        self.window: Any | None = None
        self._webview: Any | None = None
        self._pump_stop = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self._hide_timer: threading.Timer | None = None
        self._privacy_status = "sovereign"
        self._state = "idle"
        self._message = ""
        self._style_label = ""
        self._cleanup_label = ""
        self._priority_label = ""
        self._start_requested = threading.Event()
        self._pending_show = False
        # pywebview window operations (evaluate_js, show, move, destroy) block on
        # the window's loaded event for ~20 s if the GUI event loop is not running
        # yet. Guard every such call until start_event_loop() actually spins the
        # loop, so startup and shutdown never eat that 20 s stall.
        self._loop_running = False

    def create_window(self) -> bool:
        if self.window is not None:
            return True
        try:
            import webview
        except ImportError:
            return False

        self._webview = webview
        html_path = self.assets_dir / "overlay.html"
        try:
            self.window = webview.create_window(
                "DCENT_Voice",
                url=html_path.as_uri(),
                width=self.width,
                height=self.height,
                frameless=True,
                easy_drag=False,
                on_top=True,
                transparent=True,
                hidden=True,
                # focus=False makes pywebview apply WS_EX_NOACTIVATE at creation and
                # re-assert it on every activation, so showing the overlay never
                # steals foreground from the target window (where the paste must go).
                focus=False,
            )
        except Exception:
            # Some backends reject transparent/frameless windows while ordinary
            # Settings windows remain usable. Let app.py fall back to a plain
            # hidden GUI host instead of crashing the voice runtime.
            self.window = None
            self._webview = None
            return False
        shown_event = getattr(getattr(self.window, "events", None), "shown", None)
        if shown_event is not None:
            with contextlib.suppress(Exception):
                shown_event += self._on_shown
        return True

    def start_event_loop(self, background_fn: Any | None = None) -> None:
        if self._webview is None:
            raise RuntimeError("Overlay window has not been created.")

        def _runner() -> None:
            # pywebview invokes this once the GUI loop is up and running, so it is
            # the point after which window operations no longer block.
            self._loop_running = True
            if self._pending_show:
                self._show_ready()
            if background_fn is not None:
                background_fn()

        try:
            self._webview.start(_runner)
        finally:
            # A native WebView can exit independently of the dictation runtime.
            # Never leave callers sending operations to a dead GUI loop.
            self._loop_running = False

    def show(self) -> None:
        # window.show()/move() also block until the loop is running; skip the
        # visual for the sub-second startup window rather than stall a hotkey.
        self._pending_show = True
        if self.window is None:
            # Lazy mode: wake the main thread, which must create/start pywebview
            # (notably on macOS, where GUI work belongs on the main thread).
            self._start_requested.set()
            return
        if not self._loop_running:
            return
        self._show_ready()

    def _show_ready(self) -> None:
        """Show an already-created window after the native loop is running."""
        if self.window is None or not self._loop_running or not self._pending_show:
            return
        self._cancel_hide_timer()
        self._position()
        with contextlib.suppress(Exception):
            self.window.show()
        # Re-assert click-through / no-activate after show — long holds and
        # WebView focus races have been observed stealing foreground (stealer
        # name "DCENT_Voice") before paste.
        if platform.system() == "Windows":
            with contextlib.suppress(Exception):
                apply_overlay_window_styles(self._native_hwnd())
        self.set_privacy(self._privacy_status)
        self._evaluate(
            "window.dcent && window.dcent.setReducedMotion("
            f"{str(bool(self.config.reduced_motion)).lower()})"
        )
        self.set_state(self._state)
        if self._message:
            self.set_message(self._message)
        self.set_style(self._style_label)
        self.set_cleanup(self._cleanup_label)
        self.set_priority(self._priority_label)
        self._start_pump()

    def hide(self) -> None:
        self._pending_show = False
        self._cancel_hide_timer()
        self._stop_pump()
        self._evaluate("window.dcent && window.dcent.hide()")
        if self.window is not None and self._loop_running:
            with contextlib.suppress(Exception):
                self.window.hide()

    def hide_later(self, delay_s: float) -> None:
        self._cancel_hide_timer()
        self._hide_timer = threading.Timer(delay_s, self.hide)
        self._hide_timer.daemon = True
        self._hide_timer.start()

    def set_state(self, state: str) -> None:
        self._state = state
        self._evaluate(f"window.dcent && window.dcent.setState({json.dumps(state)})")

    def set_message(self, message: str) -> None:
        """Brief status line on the overlay (discard / clipboard hints)."""
        self._message = message
        self._evaluate(f"window.dcent && window.dcent.setMessage({json.dumps(message)})")

    def set_privacy(self, status: str) -> None:
        self._privacy_status = status
        self._evaluate(f"window.dcent && window.dcent.setPrivacy({json.dumps(status)})")

    def set_language(self, label: str) -> None:
        """Optional language chip. Empty hides it (English-only default)."""
        self._evaluate(f"window.dcent && window.dcent.setLanguage({json.dumps(label)})")

    def set_style(self, label: str) -> None:
        """Writing-style chip resolved from the destination app at hold."""
        self._style_label = label
        self._evaluate(f"window.dcent && window.dcent.setStyle({json.dumps(label)})")

    def set_cleanup(self, label: str) -> None:
        """Auto Cleanup level chip (None / Light / Medium / High)."""
        self._cleanup_label = label
        self._evaluate(f"window.dcent && window.dcent.setCleanup({json.dumps(label)})")

    def set_priority(self, label: str) -> None:
        """Show the starred written form or snippet expansion.

        The visual chip and external live status receive the same ``Starred:``
        label. An empty value hides and clears it.
        """
        self._priority_label = label
        self._evaluate(f"window.dcent && window.dcent.setPriority({json.dumps(label)})")

    def destroy(self) -> None:
        self._cancel_hide_timer()
        self._stop_pump()
        # Only tear the window down through pywebview if its loop actually ran;
        # calling destroy() on a never-started window blocks ~20 s.
        if self.window is not None and self._loop_running:
            with contextlib.suppress(Exception):
                self.window.destroy()
        self.window = None
        self._loop_running = False

    def detach(self) -> None:
        """Forget a native window whose event loop has already exited."""
        self._cancel_hide_timer()
        self._stop_pump()
        self.window = None
        self._loop_running = False
        self._pending_show = False

    @property
    def start_requested(self) -> threading.Event:
        """Set on first lazy show so the main thread can create the window."""
        return self._start_requested

    def wait_for_start_request(self, timeout: float) -> bool:
        """Wait for first use when configured for lazy creation."""
        return self._start_requested.wait(timeout)

    def _on_shown(self) -> None:
        system = platform.system()
        if system == "Windows":
            with contextlib.suppress(Exception):
                apply_overlay_window_styles(self._native_hwnd())
        elif system == "Darwin":
            from dcent_voice.ui.overlay_macos import apply_overlay_styles

            with contextlib.suppress(Exception):
                apply_overlay_styles(getattr(self.window, "native", None))
        elif system == "Linux":
            from dcent_voice.ui.overlay_linux import apply_overlay_styles

            with contextlib.suppress(Exception):
                apply_overlay_styles(getattr(self.window, "native", None))

    def _native_hwnd(self) -> int | None:
        # pywebview's Window has no `hwnd`; the Win32 handle is on the WinForms
        # form at window.native.Handle. Without this the click-through / no-focus
        # / no-taskbar ex-styles were silently never applied.
        native = getattr(self.window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is None:
            return None
        for converter in ("ToInt64", "ToInt32"):
            fn = getattr(handle, converter, None)
            if callable(fn):
                with contextlib.suppress(Exception):
                    return int(fn())
        with contextlib.suppress(Exception):
            return int(handle)
        return None

    def _position(self) -> None:
        if self.window is None:
            return
        x, y = overlay_position(
            get_foreground_window(), self.width, self.height, self.config.position
        )
        with contextlib.suppress(Exception):
            self.window.move(x, y)

    def _start_pump(self) -> None:
        if self._pump_thread is not None and self._pump_thread.is_alive():
            return
        self._pump_stop.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_levels, name="OverlayAmplitudePump", daemon=True
        )
        self._pump_thread.start()

    def _stop_pump(self) -> None:
        self._pump_stop.set()
        thread = self._pump_thread
        self._pump_thread = None
        if thread is not None:
            thread.join(timeout=0.5)

    def _pump_levels(self) -> None:
        # 15 Hz is enough for a reactive meter and halves WebView evaluate_js
        # traffic during long holds.
        while not self._pump_stop.wait(1.0 / 15.0):
            self._evaluate(f"window.dcent && window.dcent.setLevel({self.meter.read():.4f})")

    def _evaluate(self, script: str) -> None:
        # Before the GUI loop runs, evaluate_js blocks ~20 s waiting for the
        # window to load. The current privacy/state is cached on the controller
        # and re-applied by show(), so skipping here loses nothing.
        if self.window is None or not self._loop_running:
            return
        with contextlib.suppress(Exception):
            self.window.evaluate_js(script)

    def _cancel_hide_timer(self) -> None:
        timer = self._hide_timer
        self._hide_timer = None
        if timer is not None:
            timer.cancel()
