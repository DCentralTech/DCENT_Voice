# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Create the system tray interface and callbacks."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dcent_voice.config import APP_NAME, AppConfig
from dcent_voice.events import (
    AsrReadyChanged,
    EventBus,
    HotkeyHealthChanged,
    PrivacyChanged,
    ShutdownRequested,
)

logger = logging.getLogger(APP_NAME).getChild("tray")

_KEY_DISPLAY = {
    "ctrl": "Ctrl",
    "win": "Win",
    "cmd": "Cmd",
    "alt": "Alt",
    "shift": "Shift",
    "esc": "Esc",
}


def _format_chord(chord: str) -> str:
    """Render a config chord like ``ctrl+win`` as ``Ctrl+Win`` for humans."""
    parts = [p.strip() for p in (chord or "").split("+") if p.strip()]
    display = [_KEY_DISPLAY.get(p.lower(), p.upper() if len(p) == 1 else p.title()) for p in parts]
    return "+".join(display)


@dataclass
class TrayCallbacks:
    """Callbacks that connect tray actions to application controls."""

    # ``None`` is retained as the successful result for existing callbacks.
    # Returning ``False`` lets a callback report a handled failure without
    # raising (for example, a window controller which could not create a native
    # window). Any other return value is treated as success.
    set_profile: Callable[[str], object] | None = None
    set_cleanup_enabled: Callable[[bool], object] | None = None
    open_settings: Callable[[], object] | None = None
    open_setup: Callable[[], object] | None = None
    mark_first_run_education_shown: Callable[[], object] | None = None
    # Troubleshooting surfaces (WS3/WS4): the tray is the only always-visible
    # place a stuck user can reach them from.
    open_log_folder: Callable[[], object] | None = None
    run_diagnostics: Callable[[], object] | None = None
    install_webview2: Callable[[], object] | None = None


class TrayApp:
    """System tray interface for the desktop application."""

    def __init__(
        self,
        *,
        config: AppConfig,
        bus: EventBus,
        callbacks: TrayCallbacks | None = None,
        asr_ready: bool = True,
        webview2_missing: bool = False,
    ) -> None:
        self.config = config
        self.bus = bus
        self.callbacks = callbacks or TrayCallbacks()
        self._icon: Any | None = None
        self._privacy_status = config.session_locality.value
        self._privacy_detail = ""
        self._hotkey_status = "ok"
        self._hotkey_detail = ""
        # Startup loads the model on a background thread so the icon appears
        # first; until the load completes the menu must say so rather than
        # claim a model that is not resident yet.
        self._asr_ready = asr_ready
        self._asr_ever_ready = asr_ready
        self._webview2_missing = webview2_missing
        self._state_lock = threading.RLock()
        self._last_toast_at_by_key: dict[str, float] = {}
        self._unsubscribe = self.bus.subscribe(self._on_event)

    def start(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover - dependency/environment specific
            logger.warning("pystray/Pillow unavailable; continuing without tray")
            return

        try:
            image = self._build_icon_image(Image, ImageDraw)
            menu = self.build_menu(pystray)
            icon = pystray.Icon("DCENT_Voice", image, self._tooltip(), menu)
            with self._state_lock:
                self._icon = icon
            icon.run_detached()
            self._show_first_run_education()
        except Exception:
            logger.exception("tray failed to start; continuing without tray")
            with self._state_lock:
                self._icon = None

    @staticmethod
    def _build_icon_image(image_mod: Any, draw_mod: Any) -> Any:
        """Prefer the brand particles+waveform asset; fall back to a simple mark."""
        from dcent_voice.ui.icons import load_tray_image

        brand = load_tray_image()
        if brand is not None:
            return brand
        # Fallback if assets missing from a broken install.
        image = image_mod.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = draw_mod.Draw(image)
        draw.ellipse((4, 4, 60, 60), fill=(255, 122, 24, 255))
        draw.ellipse((20, 20, 44, 44), fill=(24, 19, 16, 255))
        return image

    def build_menu(self, pystray: Any) -> Any:
        """Construct the tray menu.

        Building each ``MenuItem`` runs pystray's ``_assert_action`` validation,
        which rejects any action callable with more than two parameters. Keeping
        this separate from ``run_detached()`` makes the menu (and that arity
        contract) testable without a display.
        """

        def profile_item(name: str) -> Any:
            # `name` is already bound per-call by this factory, so no default-arg
            # capture is needed. The action lambda must stay at two parameters.
            return pystray.MenuItem(
                name,
                lambda _icon, _item: self._set_profile(name),
                checked=lambda _item: name == self.config.active_profile,
                radio=True,
            )

        return pystray.Menu(
            # Callable text so update_menu() re-evaluates the privacy line on
            # PrivacyChanged; a plain string would freeze the launch-time value.
            pystray.MenuItem(lambda _item: self._dictate_hint(), None, enabled=False),
            pystray.MenuItem(lambda _item: self._privacy_line(), None, enabled=False),
            pystray.MenuItem(lambda _item: self._hotkey_line(), None, enabled=False),
            pystray.MenuItem(lambda _item: self._model_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings...", lambda _icon, _item: self._open_settings()),
            pystray.MenuItem(
                "Advanced",
                pystray.Menu(
                    pystray.MenuItem(
                        "AI cleanup (optional)",
                        lambda _icon, _item: self._toggle_cleanup(),
                        checked=lambda _item: self.config.current_profile.cleanup_enabled,
                    ),
                    pystray.MenuItem(
                        "Profiles",
                        pystray.Menu(*(profile_item(name) for name in self.config.profiles)),
                    ),
                    pystray.MenuItem(
                        "Setup wizard...",
                        lambda _icon, _item: self._open_setup(),
                    ),
                ),
            ),
            pystray.MenuItem(
                "Troubleshooting",
                pystray.Menu(
                    pystray.MenuItem(
                        "Run diagnostics",
                        lambda _icon, _item: self._run_diagnostics(),
                    ),
                    pystray.MenuItem(
                        "Open log folder",
                        lambda _icon, _item: self._open_log_folder(),
                    ),
                    # Only meaningful while the runtime really is absent: with
                    # it installed the entry would send users to a page they
                    # have no reason to visit.
                    pystray.MenuItem(
                        "Install WebView2 runtime…",
                        lambda _icon, _item: self._install_webview2(),
                        visible=lambda _item: self._webview2_missing,
                    ),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Quit",
                lambda _icon, _item: self._quit(),
            ),
        )

    def stop(self) -> None:
        with self._state_lock:
            icon = self._icon
            self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logger.warning("tray icon failed to stop cleanly", exc_info=True)
        self._unsubscribe()

    def notify_user(
        self, title: str, body: str, *, key: str = "", min_interval_s: float = 0.0
    ) -> None:
        """Show a tray balloon if available, rate-limited when ``key`` is set."""
        now = time.monotonic()
        with self._state_lock:
            if key and now - self._last_toast_at_by_key.get(key, float("-inf")) < min_interval_s:
                return
            if key:
                self._last_toast_at_by_key[key] = now
            icon = self._icon
        if icon is None:
            logger.info("notify (no tray): %s — %s", title, body)
            return
        try:
            icon.notify(body, title)
        except Exception:
            # Notification is the last-resort error surface for tray actions.
            # Log its own failure, but never route it through the tray action
            # reporter (which would recursively try to notify again).
            logger.warning("tray notification failed: %s", title, exc_info=True)

    def _dictate_hint(self) -> str:
        """The one thing a new user must know: how to start dictating."""
        chord = _format_chord(self.config.hotkeys.dictation)
        verb = "Hold" if self.config.hotkeys.mode == "hold" else "Press"
        return f"{verb} {chord} and speak to dictate"

    def _privacy_line(self) -> str:
        locality = self._privacy_status
        if locality in {"local", "sovereign"}:
            return "Sovereign - everything on-device"
        if locality == "hybrid":
            return "Hybrid - some data leaves this machine"
        if locality == "consent_required":
            return "Cloud consent required"
        return "Cloud profile active"

    def _hotkey_line(self) -> str:
        status = self._hotkey_status
        if status == "ok":
            return "Hotkeys: OK"
        if status == "recovering":
            return "Hotkeys: reconnecting…"
        if status == "dead":
            return "Hotkeys: FAILED — open Settings"
        if status == "stopped":
            return "Hotkeys: off"
        return f"Hotkeys: {status}"

    def _model_line(self) -> str:
        if self._asr_ready:
            return "Model: ready"
        if not self._asr_ever_ready:
            return "Model: loading… (dictation queues until ready)"
        return "Model: unloaded — next hold loads"

    def _tooltip(self) -> str:
        return f"DCENT_Voice — {self._dictate_hint()}"

    def _invoke_callback(
        self,
        action: str,
        callback: Callable[..., object] | None,
        *args: object,
        failure_title: str,
        failure_body: str,
    ) -> bool:
        """Run a pystray action without allowing a failure to disappear.

        pystray invokes menu callbacks on its platform event thread. Actions
        therefore remain synchronous and short; window callbacks signal their
        owning UI loop rather than trying to run pywebview here. Exceptions are
        contained so a broken action cannot terminate the tray event loop.
        """
        if callback is None:
            logger.error("tray action unavailable: %s", action)
            self.notify_user(
                failure_title,
                failure_body,
                key=f"tray_action:{action}",
                min_interval_s=5.0,
            )
            return False

        try:
            result = callback(*args)
        except Exception:
            logger.exception("tray action failed: %s", action)
            self.notify_user(
                failure_title,
                failure_body,
                key=f"tray_action:{action}",
                min_interval_s=5.0,
            )
            return False

        if result is False:
            logger.error("tray action reported failure: %s", action)
            self.notify_user(
                failure_title,
                failure_body,
                key=f"tray_action:{action}",
                min_interval_s=5.0,
            )
            return False
        return True

    def _set_profile(self, profile: str) -> bool:
        return self._invoke_callback(
            f"set profile {profile!r}",
            self.callbacks.set_profile,
            profile,
            failure_title="DCENT_Voice profile",
            failure_body=(
                f"Could not switch to {profile!r}. Your previous profile is still active. "
                "Check the application log for details."
            ),
        )

    def _toggle_cleanup(self) -> bool:
        enabled = not self.config.current_profile.cleanup_enabled
        verb = "enable" if enabled else "disable"
        return self._invoke_callback(
            f"{verb} AI cleanup",
            self.callbacks.set_cleanup_enabled,
            enabled,
            failure_title="DCENT_Voice cleanup",
            failure_body=(
                f"Could not {verb} AI cleanup. Your previous setting is still active. "
                "Check the application log for details."
            ),
        )

    def refresh(self, config: AppConfig | None = None) -> None:
        """Adopt a new config snapshot and redraw the menu (radio/check state)."""
        if config is not None:
            self.config = config
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        with self._state_lock:
            icon = self._icon
        if icon is None:
            return
        try:
            icon.title = self._tooltip()
        except Exception:
            logger.warning("tray tooltip refresh failed", exc_info=True)
        try:
            icon.update_menu()
        except Exception:
            logger.warning("tray menu refresh failed", exc_info=True)

    def _open_settings(self) -> bool:
        return self._invoke_callback(
            "open settings",
            self.callbacks.open_settings,
            failure_title="DCENT_Voice Settings",
            failure_body=(
                "Settings could not be opened. Try again, then check the application log "
                "if the problem continues."
            ),
        )

    def _open_setup(self) -> bool:
        return self._invoke_callback(
            "open setup wizard",
            self.callbacks.open_setup,
            failure_title="DCENT_Voice Setup",
            failure_body=(
                "The setup wizard could not be opened. Try again, then check the application "
                "log if the problem continues."
            ),
        )

    def _open_log_folder(self) -> bool:
        return self._invoke_callback(
            "open log folder",
            self.callbacks.open_log_folder,
            failure_title="DCENT_Voice logs",
            failure_body=(
                "The log folder could not be opened. It is under your DCENT_Voice "
                "profile directory in a 'logs' subfolder."
            ),
        )

    def _run_diagnostics(self) -> bool:
        return self._invoke_callback(
            "run diagnostics",
            self.callbacks.run_diagnostics,
            failure_title="DCENT_Voice diagnostics",
            failure_body=(
                "Diagnostics could not be started. Run 'dcent-voice doctor --open' "
                "from a terminal, or check the application log."
            ),
        )

    def _install_webview2(self) -> bool:
        return self._invoke_callback(
            "install webview2 runtime",
            self.callbacks.install_webview2,
            failure_title="DCENT_Voice",
            failure_body=(
                "The WebView2 download page could not be opened. Visit "
                "https://go.microsoft.com/fwlink/p/?LinkId=2124703 in your browser."
            ),
        )

    def _quit(self) -> bool:
        return self._invoke_callback(
            "quit",
            self.bus.publish,
            ShutdownRequested("tray"),
            failure_title="DCENT_Voice",
            failure_body="DCENT_Voice could not shut down. Check the application log.",
        )

    def _on_event(self, ev) -> None:
        if isinstance(ev, AsrReadyChanged):
            self._asr_ready = ev.ready
            if ev.ready:
                self._asr_ever_ready = True
            self._refresh_icon()
            return
        if isinstance(ev, PrivacyChanged):
            self._privacy_status = ev.status
            self._privacy_detail = ev.detail
            self._refresh_icon()
            return
        if isinstance(ev, HotkeyHealthChanged):
            previous = self._hotkey_status
            self._hotkey_status = ev.status
            self._hotkey_detail = ev.detail
            self._refresh_icon()
            if ev.status == "dead" and previous != "dead":
                self.notify_user(
                    "DCENT_Voice",
                    "Global hotkeys failed — open Settings or restart the app",
                    key="hotkeys_dead",
                    min_interval_s=60.0,
                )
            elif ev.status == "ok" and previous in {"recovering", "dead"}:
                self.notify_user(
                    "DCENT_Voice",
                    "Global hotkeys reconnected",
                    key="hotkeys_ok",
                    min_interval_s=10.0,
                )

    def _first_run_notify_text(self) -> str:
        """Teach hold-to-talk without opening Settings or the wizard."""
        return (
            f"{self._dictate_hint()}. Voice stays on this machine. "
            "Open Setup from the tray only if you need it."
        )

    def _show_first_run_education(self) -> bool:
        if self.config.privacy.first_run_education_shown:
            return True
        with self._state_lock:
            icon = self._icon
        if icon is None:
            logger.warning("first-run education deferred because the tray is unavailable")
            return False
        try:
            icon.notify(self._first_run_notify_text(), "DCENT_Voice")
        except Exception:
            logger.warning("first-run tray notification failed", exc_info=True)
            # The tooltip and first disabled menu row carry the same instruction
            # without opening or focusing a window. Refresh them now and leave
            # education incomplete so the next launch retries the notification.
            self._refresh_icon()
            logger.info("first-run guidance remains available in the tray tooltip and menu")
            return False

        callback = self.callbacks.mark_first_run_education_shown
        if callback is None:
            logger.warning("first-run education shown but completion cannot be persisted")
            return False
        try:
            result = callback()
        except Exception:
            logger.warning("could not persist first-run education flag", exc_info=True)
            return False
        if result is False:
            logger.warning("first-run education completion was not persisted")
            return False
        return True
