# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path

import pytest

from dcent_voice.config import load_config
from dcent_voice.events import EventBus, HotkeyHealthChanged, PrivacyChanged, ShutdownRequested
from dcent_voice.ui.tray import TrayApp, TrayCallbacks


class _RecordingIcon:
    def __init__(self, *, fail_notify: bool = False) -> None:
        self.fail_notify = fail_notify
        self.notifications: list[tuple[str, str]] = []
        self.stopped = False
        self.title = ""
        self.menu_updates = 0

    def notify(self, body: str, title: str) -> None:
        self.notifications.append((title, body))
        if self.fail_notify:
            raise RuntimeError("native notification failed")

    def stop(self) -> None:
        self.stopped = True

    def update_menu(self) -> None:
        self.menu_updates += 1


def test_tray_privacy_line_updates_from_event() -> None:
    bus = EventBus()
    config = load_config(Path("config.example.toml"), create=False)
    tray = TrayApp(config=config, bus=bus)

    assert tray._privacy_line() == "Sovereign - everything on-device"
    tray._on_event(PrivacyChanged("hybrid"))

    assert tray._privacy_line() == "Hybrid - some data leaves this machine"
    tray.stop()


def test_tray_model_line_tracks_asr_ready() -> None:
    from dcent_voice.events import AsrReadyChanged, EventBus
    from dcent_voice.ui.tray import TrayApp

    bus = EventBus()
    tray = TrayApp(config=load_config(Path("config.example.toml"), create=False), bus=bus)
    assert tray._model_line() == "Model: ready"
    tray._on_event(AsrReadyChanged(False, "idle_unload"))
    assert "unloaded" in tray._model_line()
    tray._on_event(AsrReadyChanged(True, "loaded"))
    assert tray._model_line() == "Model: ready"


def test_tray_hotkey_line_updates_from_health_event() -> None:
    bus = EventBus()
    config = load_config(Path("config.example.toml"), create=False)
    tray = TrayApp(config=config, bus=bus)

    assert tray._hotkey_line() == "Hotkeys: OK"
    tray._on_event(HotkeyHealthChanged(status="dead", detail="rebind failed"))
    assert "FAILED" in tray._hotkey_line()
    tray._on_event(HotkeyHealthChanged(status="ok", detail="listener running"))
    assert tray._hotkey_line() == "Hotkeys: OK"
    tray.stop()


def test_first_run_tray_teaches_hold_and_marks_shown() -> None:
    bus = EventBus()
    config = load_config(Path("config.example.toml"), create=False)
    marked: list[bool] = []
    tray = TrayApp(
        config=config,
        bus=bus,
        callbacks=TrayCallbacks(mark_first_run_education_shown=lambda: marked.append(True)),
    )
    icon = _RecordingIcon()
    tray._icon = icon  # type: ignore[assignment]
    assert tray._show_first_run_education() is True
    assert marked == [True]
    assert icon.notifications
    title, body = icon.notifications[0]
    assert title == "DCENT_Voice"
    assert "Hold Ctrl+Win" in body
    assert "tray" in body.lower()
    tray.stop()


def test_failed_first_run_notification_keeps_education_pending_and_refreshes_tray(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    marked: list[bool] = []
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
        callbacks=TrayCallbacks(mark_first_run_education_shown=lambda: marked.append(True)),
    )
    icon = _RecordingIcon(fail_notify=True)
    tray._icon = icon

    assert tray._show_first_run_education() is False
    assert marked == []
    assert "Hold Ctrl+Win" in icon.title
    assert icon.menu_updates == 1
    assert "first-run tray notification failed" in caplog.text
    assert "tray tooltip and menu" in caplog.text
    tray.stop()


def test_tray_model_line_reports_startup_loading_separately_from_idle_unload() -> None:
    """Startup now loads the model on a worker thread, so "ready" would lie."""
    from dcent_voice.events import AsrReadyChanged

    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
        asr_ready=False,
    )
    assert "loading" in tray._model_line().lower()
    tray._on_event(AsrReadyChanged(True, "loaded"))
    assert tray._model_line() == "Model: ready"
    tray._on_event(AsrReadyChanged(False, "idle_unload"))
    assert tray._model_line() == "Model: unloaded — next hold loads"
    tray.stop()


def test_troubleshooting_menu_items_invoke_their_callbacks() -> None:
    calls: list[str] = []
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
        callbacks=TrayCallbacks(
            open_log_folder=lambda: calls.append("logs"),
            run_diagnostics=lambda: calls.append("doctor"),
            install_webview2=lambda: calls.append("webview2"),
        ),
    )
    tray._icon = _RecordingIcon()

    assert tray._open_log_folder() is True
    assert tray._run_diagnostics() is True
    assert tray._install_webview2() is True
    assert calls == ["logs", "doctor", "webview2"]
    tray.stop()


def test_missing_troubleshooting_callbacks_notify_instead_of_failing_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
    )
    icon = _RecordingIcon()
    tray._icon = icon

    assert tray._run_diagnostics() is False
    assert tray._open_log_folder() is False
    assert "tray action unavailable" in caplog.text
    assert [title for title, _body in icon.notifications] == [
        "DCENT_Voice diagnostics",
        "DCENT_Voice logs",
    ]
    tray.stop()


def test_webview2_install_item_is_hidden_when_the_runtime_is_present() -> None:
    """The remediation entry must not send users to a page they do not need."""
    try:
        import pystray
    except (ImportError, ValueError):
        pytest.skip("pystray unavailable")
    config = load_config(Path("config.example.toml"), create=False)

    present = TrayApp(config=config, bus=EventBus(), webview2_missing=False)
    missing = TrayApp(config=config, bus=EventBus(), webview2_missing=True)
    try:
        labels = {}
        for name, tray in (("present", present), ("missing", missing)):
            try:
                menu = tray.build_menu(pystray)
            except ValueError as exc:  # pragma: no cover - Linux AppIndicator
                pytest.skip(f"AppIndicator typelib missing: {exc}")
            troubleshooting = next(
                item for item in menu.items if str(item.text) == "Troubleshooting"
            )
            labels[name] = [str(sub.text) for sub in troubleshooting.submenu.items if sub.visible]
        assert "Run diagnostics" in labels["present"]
        assert "Open log folder" in labels["present"]
        assert "Install WebView2 runtime…" not in labels["present"]
        assert "Install WebView2 runtime…" in labels["missing"]
    finally:
        present.stop()
        missing.stop()


def test_tray_dictate_hint_shows_hotkey() -> None:
    # The tray is the only always-visible surface; it must tell a new user how
    # to start dictating, in human key names.
    bus = EventBus()
    config = load_config(Path("config.example.toml"), create=False)
    tray = TrayApp(config=config, bus=bus)

    assert tray._dictate_hint() == "Hold Ctrl+Win and speak to dictate"
    assert "Ctrl+Win" in tray._tooltip()
    tray.stop()


def test_load_tray_image_from_brand_assets() -> None:
    from dcent_voice.ui.icons import icon_path, load_tray_image

    assert icon_path("app-icon-64.png") is not None
    image = load_tray_image()
    assert image is not None
    assert image.size[0] >= 32
    assert image.mode == "RGBA"


def test_tray_build_menu_actions_accepted_by_pystray() -> None:
    # Regression: pystray._assert_action rejects any action callable with more
    # than two parameters. The per-profile menu items previously used a
    # `profile=name` default-arg (3 params), which made tray.start() raise
    # ValueError and crash the whole app on launch. Building the menu here runs
    # that validation on every item without needing a display.
    try:
        import pystray
    except ImportError:
        pytest.skip("pystray not installed")
    except ValueError as exc:
        pytest.skip(f"Linux AppIndicator typelib missing: {exc}")
    config = load_config(Path("config.example.toml"), create=False)
    tray = TrayApp(config=config, bus=EventBus())

    try:
        menu = tray.build_menu(pystray)
    except ValueError as exc:
        if "AppIndicator" in str(exc) or "Ayatana" in str(exc):
            pytest.skip(f"Linux AppIndicator typelib missing: {exc}")
        raise

    # config.example.toml defines multiple profiles; ensure they were built.
    assert len(config.profiles) >= 2
    assert menu is not None
    tray.stop()


@pytest.mark.parametrize(
    ("method_name", "callback_field", "expected_title"),
    [
        ("_open_settings", "open_settings", "DCENT_Voice Settings"),
        ("_open_setup", "open_setup", "DCENT_Voice Setup"),
    ],
)
def test_window_action_exception_is_logged_and_notified(
    method_name: str,
    callback_field: str,
    expected_title: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail() -> None:
        raise RuntimeError("window creation failed")

    callbacks = TrayCallbacks(**{callback_field: fail})
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
        callbacks=callbacks,
    )
    icon = _RecordingIcon()
    tray._icon = icon

    assert getattr(tray, method_name)() is False
    assert "tray action failed" in caplog.text
    assert "window creation failed" in caplog.text
    assert icon.notifications[0][0] == expected_title
    tray.stop()


def test_explicit_false_result_is_reported_as_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
        callbacks=TrayCallbacks(open_settings=lambda: False),
    )
    icon = _RecordingIcon()
    tray._icon = icon

    assert tray._open_settings() is False
    assert "tray action reported failure: open settings" in caplog.text
    assert len(icon.notifications) == 1
    tray.stop()


def test_legacy_none_result_is_success() -> None:
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
        callbacks=TrayCallbacks(open_settings=lambda: None),
    )
    icon = _RecordingIcon()
    tray._icon = icon

    assert tray._open_settings() is True
    assert icon.notifications == []
    tray.stop()


def test_profile_and_cleanup_callbacks_receive_values_and_report_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile_calls: list[str] = []
    cleanup_calls: list[bool] = []

    def set_profile(profile: str) -> bool:
        profile_calls.append(profile)
        return False

    def set_cleanup(enabled: bool) -> None:
        cleanup_calls.append(enabled)
        raise OSError("config file is locked")

    config = load_config(Path("config.example.toml"), create=False)
    tray = TrayApp(
        config=config,
        bus=EventBus(),
        callbacks=TrayCallbacks(
            set_profile=set_profile,
            set_cleanup_enabled=set_cleanup,
        ),
    )
    icon = _RecordingIcon()
    tray._icon = icon

    assert tray._set_profile("tiny") is False
    assert tray._toggle_cleanup() is False
    assert profile_calls == ["tiny"]
    assert cleanup_calls == [not config.current_profile.cleanup_enabled]
    assert "tray action reported failure: set profile 'tiny'" in caplog.text
    assert "config file is locked" in caplog.text
    assert [title for title, _body in icon.notifications] == [
        "DCENT_Voice profile",
        "DCENT_Voice cleanup",
    ]
    tray.stop()


def test_notification_failure_is_logged_without_recursion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
        callbacks=TrayCallbacks(open_settings=lambda: False),
    )
    icon = _RecordingIcon(fail_notify=True)
    tray._icon = icon

    assert tray._open_settings() is False
    assert len(icon.notifications) == 1
    assert "tray notification failed" in caplog.text
    tray.stop()


def test_notification_rate_limit_is_independent_per_key() -> None:
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=EventBus(),
    )
    icon = _RecordingIcon()
    tray._icon = icon

    tray.notify_user("A", "first", key="a", min_interval_s=60.0)
    tray.notify_user("B", "different key", key="b", min_interval_s=60.0)
    tray.notify_user("A", "must remain throttled", key="a", min_interval_s=60.0)

    assert icon.notifications == [("A", "first"), ("B", "different key")]
    tray.stop()


def test_quit_action_publishes_shutdown_request() -> None:
    bus = EventBus()
    published: list[object] = []
    bus.publish = published.append  # type: ignore[method-assign]
    tray = TrayApp(
        config=load_config(Path("config.example.toml"), create=False),
        bus=bus,
    )

    assert tray._quit() is True
    assert published == [ShutdownRequested("tray")]
    tray.stop()
