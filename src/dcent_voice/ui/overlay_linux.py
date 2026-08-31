# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Linux overlay window styling.

On X11 the overlay floats above other windows (wmctrl `_NET_WM_STATE_ABOVE`)
and is made click-through by clearing its input shape via the X Shape extension
(python-xlib — pystray already depends on it on Linux). Wayland needs
gtk-layer-shell for the same and falls back to a plain on-top frameless window.
Everything is best-effort and fully guarded so a missing dependency degrades to
a plain (still functional) overlay rather than an error.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

OVERLAY_TITLE = "DCENT_Voice"


def is_wayland() -> bool:
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE") == "wayland"


def apply_overlay_styles(native_window: Any) -> None:  # pragma: no cover - Linux only
    if is_wayland():
        # Needs gtk-layer-shell for a proper always-on-top, non-focusable layer;
        # the plain frameless on-top window from pywebview is the fallback.
        return
    _apply_x11_above()
    with contextlib.suppress(Exception):
        apply_x11_click_through()


def _apply_x11_above() -> None:  # pragma: no cover - Linux/X11 only
    # Prefer the lightweight wmctrl route (no extra Python deps) to mark the
    # active overlay window always-on-top; the window is created on_top already,
    # this reinforces it for EWMH-compliant window managers.
    with contextlib.suppress(Exception):
        import shutil
        import subprocess

        if shutil.which("wmctrl"):
            # -F forces an exact title match; the default substring match could
            # grab "DCENT_Voice Settings" or the setup wizard window instead.
            subprocess.run(
                ["wmctrl", "-F", "-r", OVERLAY_TITLE, "-b", "add,above"],
                capture_output=True,
                timeout=1.5,
                check=False,
            )


def apply_x11_click_through(title: str = OVERLAY_TITLE) -> bool:
    """Make the overlay ignore mouse input by clearing its X Shape input region.

    The Windows overlay does this with WS_EX_TRANSPARENT; the X11 analog is an
    empty ShapeInput region — clicks then fall through to whatever is beneath.
    Returns True when the shape was applied to a window with `title`.
    """
    try:
        from Xlib import display as xdisplay
        from Xlib.ext import shape
    except Exception:  # pragma: no cover - python-xlib not installed
        return False

    disp = xdisplay.Display()
    try:
        if not disp.has_extension("SHAPE"):
            return False
        window = _find_window_by_title(disp, title)
        if window is None:
            return False
        # An empty rectangle list = empty input region = fully click-through.
        window.shape_rectangles(shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
        disp.sync()
        return True
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            disp.close()


def x11_input_shape_is_empty(title: str = OVERLAY_TITLE) -> bool | None:
    """Whether the titled window's input region is empty (None = unknown)."""
    try:
        from Xlib import display as xdisplay
        from Xlib.ext import shape
    except Exception:  # pragma: no cover - python-xlib not installed
        return None

    disp = xdisplay.Display()
    try:
        window = _find_window_by_title(disp, title)
        if window is None:
            return None
        rects = window.shape_get_rectangles(shape.SK.Input)
        return not rects.rectangles
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            disp.close()


def _find_window_by_title(disp: Any, title: str) -> Any | None:
    """Depth-first search of the X window tree for an exact WM_NAME match."""

    def walk(window: Any, depth: int) -> Any | None:
        if depth > 6:
            return None
        with contextlib.suppress(Exception):
            name = window.get_wm_name()
            if name == title:
                return window
        with contextlib.suppress(Exception):
            for child in window.query_tree().children:
                found = walk(child, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(disp.screen().root, 0)
