# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""macOS overlay window styling.

Makes the pywebview (Cocoa/WKWebView) overlay behave like the Windows overlay:
borderless, floating above other windows, click-through, and — most importantly
— non-activating so showing it never steals key focus from the app the paste
must land in. This is the macOS analog of overlay_win32.apply_overlay_window_styles.

Best-effort and fully guarded: if pyobjc isn't present or the native handle isn't
an NSWindow, it no-ops. Needs verification on a real Mac.
"""

from __future__ import annotations

import contextlib
from typing import Any


def apply_overlay_styles(native_window: Any) -> None:  # pragma: no cover - macOS only
    if native_window is None:
        return
    with contextlib.suppress(Exception):
        from AppKit import (
            NSApp,
            NSApplicationActivationPolicyAccessory,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary,
        )

        # Agent app: no Dock icon, never becomes the active app / key window.
        with contextlib.suppress(Exception):
            NSApp().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        window = native_window
        # Float above normal windows, ignore clicks, show on every Space.
        with contextlib.suppress(Exception):
            window.setLevel_(_status_window_level())
        with contextlib.suppress(Exception):
            window.setIgnoresMouseEvents_(True)
        with contextlib.suppress(Exception):
            window.setOpaque_(False)
        with contextlib.suppress(Exception):
            window.setHasShadow_(False)
        with contextlib.suppress(Exception):
            window.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
            )


def _status_window_level() -> int:  # pragma: no cover - macOS only
    # NSStatusWindowLevel (25) — above normal and floating windows.
    return 25
