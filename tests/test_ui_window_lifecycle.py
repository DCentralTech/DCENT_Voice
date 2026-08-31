# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dcent_voice.ui.settings import SettingsController, WizardController


class FakeEvent:
    def __init__(self, *, set_: bool = False) -> None:
        self._set = set_
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def is_set(self) -> bool:
        return self._set

    def fire(self) -> None:
        self._set = True
        for handler in tuple(self.handlers):
            handler()


class FakeNative:
    def __init__(self) -> None:
        self.activate_calls = 0

    def Activate(self) -> None:
        self.activate_calls += 1


class FakeWindow:
    def __init__(self, *, shown: bool = False) -> None:
        self.events = SimpleNamespace(
            shown=FakeEvent(set_=shown),
            closed=FakeEvent(),
        )
        self.native = FakeNative()
        self.restore_calls = 0
        self.show_calls = 0
        self.destroy_calls = 0
        self.fail_show = False

    def restore(self) -> None:
        self.restore_calls += 1

    def show(self) -> None:
        self.show_calls += 1
        if self.fail_show:
            raise RuntimeError("native window is gone")

    def destroy(self) -> None:
        self.destroy_calls += 1
        # Exercise synchronous event dispatch: close() must not hold its lock
        # while the callback runs.
        self.events.closed.fire()


class FakeWebview:
    def __init__(self, windows: list[FakeWindow | None] | None = None) -> None:
        self.windows = list(windows or [])
        self.created: list[tuple[str, dict]] = []

    def create_window(self, title: str, **kwargs):
        self.created.append((title, kwargs))
        if self.windows:
            return self.windows.pop(0)
        return FakeWindow()


class RaisingWebview(FakeWebview):
    def create_window(self, title: str, **kwargs):
        self.created.append((title, kwargs))
        raise RuntimeError("native backend creation failed")


@pytest.fixture(params=[SettingsController, WizardController])
def controller_type(request):
    return request.param


def make_controller(controller_type, **kwargs):
    return controller_type(
        config=MagicMock(),
        bus=MagicMock(),
        privacy=MagicMock(),
        **kwargs,
    )


def test_settings_js_bridge_does_not_expose_recursive_runtime_objects() -> None:
    controller = make_controller(SettingsController)

    # pywebview recursively descends through public, non-callable js_api
    # attributes. AppConfig eventually exposes Path.parent indefinitely at a
    # filesystem root, so these objects must remain private bridge state.
    for name in ("config", "bus", "privacy", "credential_store"):
        assert not hasattr(controller.api, name)


def install_webview(monkeypatch, webview: FakeWebview) -> None:
    monkeypatch.setitem(sys.modules, "webview", webview)


def test_first_open_requests_host_before_creating_window(monkeypatch, controller_type) -> None:
    sequence: list[str] = []
    webview = FakeWebview()
    original_create = webview.create_window

    def create_window(title: str, **kwargs):
        sequence.append("create")
        return original_create(title, **kwargs)

    webview.create_window = create_window  # type: ignore[method-assign]
    install_webview(monkeypatch, webview)
    controller = make_controller(
        controller_type,
        on_window_requested=lambda: sequence.append("request"),
    )

    assert controller.open() is True
    assert sequence == ["request", "create"]
    assert controller.window is not None
    assert len(webview.created) == 1
    # pywebview 6.x accepts an application-level icon in webview.start(), not
    # a per-window icon in create_window(). Passing it here made every Settings
    # and Wizard first launch fail with TypeError in the packaged application.
    assert "icon" not in webview.created[0][1]


def test_second_open_before_gui_loop_does_not_block_on_window_methods(
    monkeypatch, controller_type
) -> None:
    window = FakeWindow(shown=False)
    webview = FakeWebview([window])
    install_webview(monkeypatch, webview)
    request_count = 0

    def requested() -> None:
        nonlocal request_count
        request_count += 1

    controller = make_controller(controller_type, on_window_requested=requested)
    assert controller.open() is True
    assert controller.open() is True

    assert len(webview.created) == 1
    assert request_count == 2
    assert window.restore_calls == 0
    assert window.show_calls == 0


def test_existing_window_is_restored_shown_and_activated(monkeypatch, controller_type) -> None:
    window = FakeWindow(shown=True)
    webview = FakeWebview([window])
    install_webview(monkeypatch, webview)
    controller = make_controller(controller_type)
    assert controller.open() is True

    assert controller.open() is True
    assert len(webview.created) == 1
    assert window.restore_calls == 1
    assert window.show_calls == 1
    assert window.native.activate_calls == 1


def test_native_close_clears_reference_and_next_open_recreates(
    monkeypatch, controller_type
) -> None:
    first = FakeWindow()
    second = FakeWindow()
    webview = FakeWebview([first, second])
    install_webview(monkeypatch, webview)
    controller = make_controller(controller_type)

    assert controller.open() is True
    assert controller.window is first
    first.events.closed.fire()
    assert controller.window is None

    assert controller.open() is True
    assert controller.window is second
    assert len(webview.created) == 2


def test_failed_show_recreates_and_delayed_old_close_cannot_clear_replacement(
    monkeypatch, controller_type
) -> None:
    first = FakeWindow(shown=True)
    first.fail_show = True
    second = FakeWindow()
    webview = FakeWebview([first, second])
    install_webview(monkeypatch, webview)
    controller = make_controller(controller_type)

    assert controller.open() is True
    assert controller.open() is True
    assert controller.window is second

    first.events.closed.fire()
    assert controller.window is second


def test_close_is_idempotent_and_safe_with_synchronous_closed_event(
    monkeypatch, controller_type
) -> None:
    window = FakeWindow()
    webview = FakeWebview([window])
    install_webview(monkeypatch, webview)
    controller = make_controller(controller_type)
    assert controller.open() is True

    controller.close()
    controller.close()

    assert controller.window is None
    assert window.destroy_calls == 1


@pytest.mark.parametrize("failure", [None, RuntimeError("host unavailable")])
def test_creation_or_host_failure_returns_false_without_stale_reference(
    monkeypatch, controller_type, failure
) -> None:
    webview = FakeWebview([None])
    install_webview(monkeypatch, webview)

    def requested() -> None:
        if failure is not None:
            raise failure

    controller = make_controller(controller_type, on_window_requested=requested)
    assert controller.open() is False
    assert controller.window is None
    assert len(webview.created) == (0 if failure is not None else 1)


def test_native_creation_exception_returns_false_without_stale_reference(
    monkeypatch, controller_type
) -> None:
    webview = RaisingWebview()
    install_webview(monkeypatch, webview)
    controller = make_controller(controller_type)

    assert controller.open() is False
    assert controller.window is None
    assert len(webview.created) == 1


def test_concurrent_open_creates_only_one_window(monkeypatch, controller_type) -> None:
    window = FakeWindow(shown=False)
    webview = FakeWebview([window])
    install_webview(monkeypatch, webview)
    callers_ready = threading.Barrier(2)

    def requested() -> None:
        callers_ready.wait(timeout=2)

    controller = make_controller(controller_type, on_window_requested=requested)
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(controller.open())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads)
    assert results == [True, True]
    assert len(webview.created) == 1
    assert window.show_calls == 0
