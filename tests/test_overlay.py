# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import threading
import time
from pathlib import Path

from dcent_voice.audio.levels import AmplitudeMeter
from dcent_voice.config import OverlayConfig
from dcent_voice.ui.overlay import OverlayController
from dcent_voice.ui.overlay_win32 import Rect, position_in_rect


class FakeWindow:
    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.hidden = False
        self.destroyed = False
        self.moves: list[tuple[int, int]] = []

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)

    def hide(self) -> None:
        self.hidden = True

    def destroy(self) -> None:
        self.destroyed = True

    def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))


def test_overlay_assets_define_dcent_api() -> None:
    root = Path("src/dcent_voice/ui/web")

    assert "window.dcent" in (root / "overlay.js").read_text(encoding="utf-8")
    assert "dcent_voice_icon.svg" in (root / "overlay.js").read_text(encoding="utf-8")
    assert "--voice-pulse" in (root / "overlay.css").read_text(encoding="utf-8")
    assert "setReducedMotion" in (root / "overlay.js").read_text(encoding="utf-8")
    js = (root / "overlay.js").read_text(encoding="utf-8")
    html = (root / "overlay.html").read_text(encoding="utf-8")
    assert 'id="stateLabel"' in html
    assert 'id="langBadge"' in html
    assert 'id="styleBadge"' in html
    assert 'id="cleanupBadge"' in html
    assert 'id="priorityBadge"' in html
    assert '<main class="overlay-shell" aria-hidden="true">' in html
    assert (
        '<p id="accessibleStatus" class="sr-only" role="status" '
        'aria-live="polite" aria-atomic="true"></p>' in html
    )
    assert html.index("</main>") < html.index('id="accessibleStatus"')
    assert "tabindex=" not in html
    assert "updateAccessibleStatus" in js
    assert 'document.getElementById("accessibleStatus")' in js
    assert "Starred: ${text}" in js
    assert "overlayPriorityTitle" in js
    assert 'setChip("priorityBadge", title)' in js
    assert 'setChip("priorityBadge", label)' not in js
    assert "#priorityBadge" in (root / "overlay.css").read_text(encoding="utf-8")
    assert "text-transform: none" in (root / "overlay.css").read_text(encoding="utf-8")
    assert 'el.setAttribute("aria-label", title)' in js
    assert 'removeAttribute("aria-label")' in js
    assert "Starred term or snippet spoken" not in js
    assert "setLanguage" in js
    assert "setStyle" in js
    assert "setCleanup" in js
    assert "setPriority" in js
    assert "state-streaming" in js
    assert "state-permission" in js
    assert 'streaming: "Live"' in js
    assert 'loading: "Loading model"' in js
    assert "WS_EX_NOACTIVATE" in Path("src/dcent_voice/ui/overlay_win32.py").read_text(
        encoding="utf-8"
    )


def test_overlay_position_setting_changes_vertical_anchor() -> None:
    rect = Rect(100, 50, 1100, 850)

    assert position_in_rect(rect, 360, 190, "top-center") == (420, 122)
    assert position_in_rect(rect, 360, 190, "center") == (420, 355)
    assert position_in_rect(rect, 360, 190, "bottom-center") == (420, 588)


def test_overlay_controller_evaluates_state_and_privacy() -> None:
    controller = OverlayController(config=OverlayConfig(), meter=AmplitudeMeter())
    fake = FakeWindow()
    controller.window = fake
    controller._loop_running = True  # GUI loop is up: window ops are allowed.

    controller.set_state("listening")
    controller.set_privacy("cloud")
    controller.set_style("Email")
    controller.set_cleanup("Medium")
    controller.set_priority("VIP")
    controller.hide()
    controller.destroy()

    assert any("setState" in script for script in fake.scripts)
    assert any("setPrivacy" in script for script in fake.scripts)
    assert any("setStyle" in script for script in fake.scripts)
    assert any("setCleanup" in script for script in fake.scripts)
    assert any("setPriority" in script for script in fake.scripts)
    assert fake.hidden is True
    assert fake.destroyed is True


def test_overlay_controller_defers_window_ops_until_loop_running() -> None:
    # Regression: pywebview evaluate_js/destroy block ~20 s if the GUI loop has
    # not started. Before the loop runs, the controller must not touch the
    # window at all, so startup/shutdown never stall.
    controller = OverlayController(config=OverlayConfig(), meter=AmplitudeMeter())
    fake = FakeWindow()
    controller.window = fake  # _loop_running is still False.

    controller.set_state("listening")
    controller.set_privacy("cloud")
    controller.hide()
    controller.destroy()

    assert fake.scripts == []  # no evaluate_js before the loop is running
    assert fake.hidden is False  # no blocking hide() before the loop runs
    assert fake.destroyed is False  # no blocking destroy() before the loop runs
    # But the privacy status is cached, so show() re-applies it once running.
    assert controller._privacy_status == "cloud"


def test_lazy_overlay_requests_start_and_replays_state() -> None:
    controller = OverlayController(config=OverlayConfig(lazy=True), meter=AmplitudeMeter())
    controller.set_state("listening")
    controller.set_message("Listening")

    controller.show()

    assert controller.wait_for_start_request(0.01) is True
    assert controller.start_requested.is_set()
    assert controller._pending_show is True
    assert controller._state == "listening"
    assert controller._message == "Listening"


def test_event_loop_flag_resets_when_native_loop_returns() -> None:
    controller = OverlayController(config=OverlayConfig(), meter=AmplitudeMeter())
    controller.window = FakeWindow()

    class FakeWebview:
        @staticmethod
        def start(runner) -> None:
            runner()

    controller._webview = FakeWebview()
    controller.start_event_loop()

    assert controller._loop_running is False


def test_overlay_creation_failure_is_recoverable(monkeypatch) -> None:
    import sys

    class Webview:
        @staticmethod
        def create_window(*_args, **_kwargs):
            raise RuntimeError("transparent windows unsupported")

    monkeypatch.setitem(sys.modules, "webview", Webview)
    controller = OverlayController(config=OverlayConfig(), meter=AmplitudeMeter())

    assert controller.create_window() is False
    assert controller.window is None
    assert controller._webview is None


def test_unexpected_overlay_exit_continues_until_shutdown() -> None:
    from dcent_voice.app import _GuiHostGate, _run_overlay_until_shutdown

    shutdown = threading.Event()
    gate = _GuiHostGate(shutdown, startup_timeout_s=0.5)
    notifications: list[tuple[str, str]] = []

    class FakeOverlay:
        detached = False

        def start_event_loop(self, _background) -> None:
            return

        def detach(self) -> None:
            self.detached = True

    class FakePipeline:
        overlay = object()

    class FakeLogger:
        def error(self, *_args) -> None:
            return

    overlay = FakeOverlay()
    pipeline = FakePipeline()
    timer = threading.Timer(0.02, shutdown.set)
    timer.start()
    try:
        _run_overlay_until_shutdown(
            shutdown=shutdown,
            overlay=overlay,  # type: ignore[arg-type]
            pipeline=pipeline,  # type: ignore[arg-type]
            notify=lambda title, body: notifications.append((title, body)),
            logger=FakeLogger(),
            gui_gate=gate,
        )
    finally:
        timer.cancel()

    assert overlay.detached is True
    assert pipeline.overlay is None
    assert notifications and "still running" in notifications[0][1]
    try:
        gate.request()
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("a request after GUI loop exit must fail fast")


def test_overlay_loop_start_failure_continues_headless_and_releases_waiter() -> None:
    from dcent_voice.app import _GuiHostGate, _run_overlay_until_shutdown

    shutdown = threading.Event()
    gate = _GuiHostGate(shutdown, startup_timeout_s=0.5)
    notifications: list[str] = []
    logged: list[str] = []

    class Overlay:
        detached = False

        def start_event_loop(self, _background) -> None:
            raise RuntimeError("native backend failed")

        def detach(self) -> None:
            self.detached = True

    class Pipeline:
        overlay = object()

    class Logger:
        def exception(self, message, *_args) -> None:
            logged.append(message)

        def error(self, message, *_args) -> None:
            logged.append(message)

    overlay = Overlay()
    pipeline = Pipeline()
    timer = threading.Timer(0.02, shutdown.set)
    timer.start()
    try:
        _run_overlay_until_shutdown(
            shutdown=shutdown,
            overlay=overlay,  # type: ignore[arg-type]
            pipeline=pipeline,  # type: ignore[arg-type]
            notify=lambda _title, body: notifications.append(body),
            logger=Logger(),
            gui_gate=gate,
        )
    finally:
        timer.cancel()

    assert overlay.detached is True
    assert pipeline.overlay is None
    assert notifications and "still running" in notifications[0]
    assert any("GUI loop failed" in message for message in logged)
    try:
        gate.request()
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("startup failure must release future requests")


def test_gui_host_gate_releases_waiter_when_ready() -> None:
    from dcent_voice.app import _GuiHostGate

    shutdown = threading.Event()
    gate = _GuiHostGate(shutdown, startup_timeout_s=0.5)
    outcomes: list[str] = []

    thread = threading.Thread(
        target=lambda: (gate.request(), outcomes.append("ready")), daemon=True
    )
    thread.start()
    assert gate.requested.wait(0.2)
    assert thread.is_alive()

    gate.mark_ready()
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert outcomes == ["ready"]


def test_gui_host_gate_releases_waiters_on_failure_shutdown_and_timeout() -> None:
    from dcent_voice.app import _GuiHostGate

    def request_error(gate: _GuiHostGate) -> str:
        try:
            gate.request()
        except RuntimeError as exc:
            return str(exc)
        raise AssertionError("request unexpectedly succeeded")

    failed = _GuiHostGate(threading.Event(), startup_timeout_s=0.5)
    failed.mark_unavailable()
    started = time.monotonic()
    assert "unavailable" in request_error(failed)
    assert time.monotonic() - started < 0.1

    stopping_event = threading.Event()
    stopping = _GuiHostGate(stopping_event, startup_timeout_s=0.5)
    errors: list[str] = []
    waiter = threading.Thread(target=lambda: errors.append(request_error(stopping)))
    waiter.start()
    assert stopping.requested.wait(0.2)
    stopping_event.set()
    waiter.join(timeout=0.5)
    assert errors and "shutting down" in errors[0]

    timed_out = _GuiHostGate(threading.Event(), startup_timeout_s=0.05)
    assert "did not start in time" in request_error(timed_out)

    ready_but_stopping_event = threading.Event()
    ready_but_stopping = _GuiHostGate(ready_but_stopping_event)
    ready_but_stopping.mark_ready()
    ready_but_stopping_event.set()
    assert "shutting down" in request_error(ready_but_stopping)


def test_first_run_wizard_is_created_after_hidden_master_host(monkeypatch) -> None:
    """Regression: auto-setup must not become pywebview's master window."""
    import sys
    from types import SimpleNamespace

    from dcent_voice.app import _GuiHostGate, _run_ui_host_until_shutdown

    shutdown = threading.Event()
    gate = _GuiHostGate(shutdown, startup_timeout_s=0.5)
    calls: list[str] = []

    class Window:
        def destroy(self) -> None:
            calls.append("destroy-host")

    class Webview:
        @staticmethod
        def create_window(title: str, **_kwargs):
            calls.append(title)
            return Window()

        @staticmethod
        def start(ready) -> None:
            ready()

    class Wizard:
        window = None

        def open(self) -> bool:
            gate.request()
            calls.append("wizard")
            shutdown.set()
            return True

        def close(self) -> None:
            calls.append("close-wizard")

    logger = SimpleNamespace(exception=lambda *_args: None, warning=lambda *_args: None)
    monkeypatch.setitem(sys.modules, "webview", Webview)
    gate.requested.set()  # run_app's non-blocking initial-wizard request

    _run_ui_host_until_shutdown(
        shutdown=shutdown,
        ui_controllers=(Wizard(),),
        logger=logger,
        gui_gate=gate,
        on_host_ready=Wizard().open,
    )

    assert calls[0] == "DCENT_Voice UI host"
    assert calls.index("DCENT_Voice UI host") < calls.index("wizard")


def test_first_run_wizard_with_lazy_overlay_has_no_main_thread_wait(monkeypatch) -> None:
    """The lazy overlay is master before auto-setup asks the ready gate."""
    import sys
    from types import SimpleNamespace

    from dcent_voice.app import (
        _GuiHostGate,
        _run_overlay_until_shutdown,
    )

    shutdown = threading.Event()
    gate = _GuiHostGate(shutdown, startup_timeout_s=0.5)
    calls: list[str] = []

    class Window(FakeWindow):
        def destroy(self) -> None:
            calls.append("destroy-overlay")
            super().destroy()

    class Webview:
        @staticmethod
        def create_window(title: str, **_kwargs):
            calls.append(title)
            return Window()

        @staticmethod
        def start(ready) -> None:
            ready()

    class Pipeline:
        overlay = object()

    monkeypatch.setitem(sys.modules, "webview", Webview)
    overlay = OverlayController(config=OverlayConfig(lazy=True), meter=AmplitudeMeter())
    assert overlay.create_window()
    gate.requested.set()  # Non-blocking request made by run_app's main thread.

    def open_wizard() -> None:
        gate.request()
        calls.append("wizard")
        shutdown.set()

    _run_overlay_until_shutdown(
        shutdown=shutdown,
        overlay=overlay,
        pipeline=Pipeline(),  # type: ignore[arg-type]
        notify=lambda *_args: None,
        logger=SimpleNamespace(error=lambda *_args: None),
        gui_gate=gate,
        on_host_ready=open_wizard,
    )

    assert calls[0] == "DCENT_Voice"
    assert calls.index("DCENT_Voice") < calls.index("wizard")
