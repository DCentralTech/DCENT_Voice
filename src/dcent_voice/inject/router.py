# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Select and coordinate platform text injectors."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dcent_voice.inject.base import Injector, RetractUnsupported

_PROCESS_INJECTION_LOCK = threading.RLock()


@dataclass(frozen=True)
class InjectionRouteDecision:
    """Resolved production route for one payload and captured target."""

    process_name: str
    configured_injector: str
    resolved_injector: str
    delivery: str


class RoutingInjector(Injector):
    """Select an appropriate text injector for the active application."""

    def __init__(
        self,
        *,
        default_name: str,
        injectors: dict[str, Injector],
        per_app: dict[str, str] | None = None,
        process_name_fn: Callable[[], str | None] | None = None,
        short_text_keystroke_chars: int = 0,
    ) -> None:
        if default_name not in injectors:
            raise ValueError(f"Unknown default injector: {default_name}")
        self.default_name = default_name
        self.injectors = injectors
        self.per_app = {key.lower(): value for key, value in (per_app or {}).items()}
        self.process_name_fn = process_name_fn or get_foreground_process_name
        self.short_text_keystroke_chars = int(short_text_keystroke_chars)

    def inject(self, text: str) -> None:
        with _PROCESS_INJECTION_LOCK:
            target = None
            if platform.system() == "Windows":
                from dcent_voice.inject.windows_focus import capture_foreground_target

                target = capture_foreground_target()
            self._inject_locked(text, target)

    def inject_into_target(self, text: str, target: object) -> None:
        """Inject into a focus target captured when push-to-talk began."""
        self.inject_into_target_with_decision(text, target)

    def inject_into_target_with_decision(self, text: str, target: object) -> InjectionRouteDecision:
        """Inject once and return the exact decision used by that transaction."""

        with _PROCESS_INJECTION_LOCK:
            return self._inject_locked(text, target, bind_process_to_target=True)

    def _inject_locked(
        self,
        text: str,
        target: object | None,
        *,
        bind_process_to_target: bool = False,
    ) -> InjectionRouteDecision:
        decision = self.resolve_decision(
            text, target, bind_process_to_target=bind_process_to_target
        )
        injector = (
            self.injectors.get(decision.resolved_injector) or self.injectors[self.default_name]
        )
        targeted = getattr(injector, "inject_targeted", None)
        checked = getattr(injector, "inject_checked", None)
        if decision.delivery in {"native_replace", "native_paste"} and callable(targeted):
            try:
                targeted(text, target)
            except Exception as exc:
                fallback = self._native_pre_mutation_clipboard_fallback(text, target, decision, exc)
                if fallback is None:
                    raise
                return fallback
        elif decision.delivery in {
            "unicode_sendinput_checked",
            "clipboard_ctrl_v_checked",
        } and callable(checked):
            checked(text, target)
        else:
            injector.inject(text)
        return decision

    def _native_pre_mutation_clipboard_fallback(
        self,
        text: str,
        target: object | None,
        decision: InjectionRouteDecision,
        error: Exception,
    ) -> InjectionRouteDecision | None:
        """Use exact native replacement when clipboard access failed safely.

        Snapshot/publish acquisition failures happen before clipboard mutation,
        so an HWND-bound edit replacement cannot duplicate an insert.  Restore
        failures happen after paste and deliberately remain fatal: retrying via
        another backend could insert the same dictation twice.
        """
        if decision.delivery != "native_paste" or not bool(
            getattr(target, "supports_edit_messages", False)
        ):
            return None
        from dcent_voice.inject.clipboard import (
            ClipboardOpenTimeout,
            ClipboardPreservationError,
        )

        if isinstance(error, ClipboardOpenTimeout) and error.stage not in {
            "snapshot",
            "set",
        }:
            return None
        if not isinstance(error, (ClipboardOpenTimeout, ClipboardPreservationError)):
            return None
        fallback = self.injectors.get("keystroke")
        targeted = getattr(fallback, "inject_targeted", None)
        if not callable(targeted):
            return None
        targeted(text, target)
        return InjectionRouteDecision(
            process_name=decision.process_name,
            configured_injector=decision.configured_injector,
            resolved_injector="keystroke",
            delivery="native_replace",
        )

    def resolve_decision(
        self,
        text: str,
        target: object | None = None,
        *,
        bind_process_to_target: bool = False,
    ) -> InjectionRouteDecision:
        """Return the exact route production injection will use without injecting."""

        process = ""
        if bind_process_to_target:
            process = _process_name_for_captured_target(target)
        if not process:
            process = (self.process_name_fn() or "").lower()
        configured_name = self.per_app.get(process, self.default_name)
        injector_name = configured_name
        if (
            process not in self.per_app
            and injector_name == "clipboard"
            and self.short_text_keystroke_chars > 0
            and len(text) <= self.short_text_keystroke_chars
            and "keystroke" in self.injectors
        ):
            injector_name = "keystroke"
        if injector_name == "keystroke" and platform.system() == "Windows":
            from dcent_voice.inject.keystroke import is_lossless_windows_keystroke_text

            native_edit = bool(getattr(target, "supports_edit_messages", False))
            if (
                not is_lossless_windows_keystroke_text(text)
                and "clipboard" in self.injectors
                and not (native_edit and bind_process_to_target)
            ):
                injector_name = "clipboard"
        injector = self.injectors.get(injector_name) or self.injectors[self.default_name]
        if injector_name not in self.injectors:
            injector_name = self.default_name
        targeted = getattr(injector, "inject_targeted", None)
        checked = getattr(injector, "inject_checked", None)
        supports_edit = bool(getattr(target, "supports_edit_messages", False))
        if target is not None and supports_edit and callable(targeted):
            delivery = "native_replace" if injector_name == "keystroke" else "native_paste"
        elif target is not None and callable(checked):
            delivery = (
                "unicode_sendinput_checked"
                if injector_name == "keystroke"
                else "clipboard_ctrl_v_checked"
            )
        else:
            delivery = f"{injector_name}_unbound"
        return InjectionRouteDecision(
            process_name=process,
            configured_injector=configured_name,
            resolved_injector=injector_name,
            delivery=delivery,
        )

    def retract(self, char_count: int) -> None:
        if char_count <= 0:
            return
        with _PROCESS_INJECTION_LOCK:
            process = (self.process_name_fn() or "").lower()
            injector_name = self.per_app.get(process, self.default_name)
            injector = self.injectors.get(injector_name) or self.injectors[self.default_name]
            # Prefer the active app's injector. Clipboard backends that cannot
            # synthesize Backspace raise RetractUnsupported; fall back to the
            # keystroke injector so streaming polish rewrites never double-paste.
            try:
                injector.retract(char_count)
                return
            except RetractUnsupported:
                pass
            except Exception:
                # Unexpected failure — still try keystroke before giving up.
                keystroke = self.injectors.get("keystroke")
                if keystroke is not None and keystroke is not injector:
                    keystroke.retract(char_count)
                    return
                raise
            keystroke = self.injectors.get("keystroke")
            if keystroke is not None and keystroke is not injector:
                keystroke.retract(char_count)
                return
            raise RetractUnsupported(
                f"{type(injector).__name__} cannot retract and no keystroke fallback is registered"
            )

    def press_enter(self) -> None:
        with _PROCESS_INJECTION_LOCK:
            process = (self.process_name_fn() or "").lower()
            injector_name = self.per_app.get(process, self.default_name)
            injector = self.injectors.get(injector_name) or self.injectors[self.default_name]
            try:
                injector.press_enter()
                return
            except Exception:
                keystroke = self.injectors.get("keystroke")
                if keystroke is not None and keystroke is not injector:
                    keystroke.press_enter()
                    return
                raise

    def press_enter_into_target(self, target: object) -> None:
        """Send Enter into the press-time target, not whoever holds foreground."""
        with _PROCESS_INJECTION_LOCK:
            process = _process_name_for_captured_target(target)
            if not process:
                process = (self.process_name_fn() or "").lower()
            injector_name = self.per_app.get(process, self.default_name)
            injector = self.injectors.get(injector_name) or self.injectors[self.default_name]
            targeted = getattr(injector, "press_enter_into_target", None)
            try:
                if callable(targeted):
                    targeted(target)
                    return
                injector.press_enter()
            except Exception:
                keystroke = self.injectors.get("keystroke")
                if keystroke is not None and keystroke is not injector:
                    fallback = getattr(keystroke, "press_enter_into_target", None)
                    if callable(fallback):
                        fallback(target)
                        return
                    keystroke.press_enter()
                    return
                raise


def _process_name_for_captured_target(target: object | None) -> str:
    """Route from the press-time window, not whoever stole foreground later."""
    if target is None:
        return ""
    process_id = int(getattr(target, "process_id", 0) or 0)
    if process_id <= 0:
        return ""
    return (process_name_from_pid(process_id) or "").lower()


def process_name_from_pid(process_id: int) -> str | None:
    if process_id <= 0 or platform.system() != "Windows":
        return None
    try:
        import win32api
        import win32process
    except ImportError:
        return None
    try:
        handle = win32api.OpenProcess(0x1000 | 0x0400, False, int(process_id))
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            close = getattr(handle, "Close", None)
            if callable(close):
                close()
    except Exception:
        return None
    return path.rsplit("\\", 1)[-1]


def get_foreground_process_name() -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _macos_foreground_process_name()
    if system == "Linux":
        return _linux_foreground_process_name()
    if system != "Windows":
        return None
    try:
        import win32api
        import win32gui
        import win32process
    except ImportError:
        return None

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    try:
        _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(0x1000 | 0x0400, False, process_id)
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            close = getattr(handle, "Close", None)
            if callable(close):
                close()
    except Exception:
        return None
    return path.rsplit("\\", 1)[-1]


def _macos_foreground_process_name() -> str | None:  # pragma: no cover - macOS only
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        url = app.executableURL() if app is not None else None
        path = str(url.path()) if url is not None else ""
        if path:
            return Path(path).name
        name = app.localizedName() if app is not None else None
        return str(name) if name else None
    except Exception:
        return None


def _linux_foreground_process_name() -> str | None:  # pragma: no cover - Linux only
    # Native Wayland intentionally exposes no global foreground-process API.
    # XWayland/X11 can provide it through xdotool when available.
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return None
    if not shutil.which("xdotool"):
        return None
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowpid"],
            capture_output=True,
            text=True,
            timeout=0.3,
            check=False,
        )
        if result.returncode != 0:
            return None
        pid = int(result.stdout.strip())
        return Path(os.readlink(f"/proc/{pid}/exe")).name
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
