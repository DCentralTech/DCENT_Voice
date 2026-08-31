# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Linux desktop e2e: overlay click-through + tray docking, under Xvfb+openbox.

Runs inside a live X session (see scripts/docker_linux_desktop_check.sh):
1. Renders the real overlay via pywebview/webkit2gtk, then verifies
   `apply_x11_click_through()` clears the X Shape input region (the X11 analog
   of the Windows WS_EX_TRANSPARENT click-through overlay).
2. Starts a real XEmbed systray host (stalonetray) and verifies a pystray icon
   — the same library the app's TrayApp uses — actually docks into it.

Requires: Debian python3 with python3-gi + `pip install --break-system-packages
pywebview pystray pillow python-xlib`, plus stalonetray and x11-utils.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from dcent_voice.ui.overlay_linux import (  # noqa: E402
    OVERLAY_TITLE,
    apply_x11_click_through,
    x11_input_shape_is_empty,
)
from dcent_voice.util.owned_process import (  # noqa: E402
    start_owned_process,
    terminate_owned_process,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{f' — {detail}' if detail else ''}", flush=True)
    if not condition:
        FAILURES.append(name)


def _tray_host_has_dock() -> bool:
    """True when the stalonetray host window has at least one docked child."""
    from Xlib import display as xdisplay

    disp = xdisplay.Display()
    try:

        def walk(window, depth):
            if depth > 5:
                return None
            with contextlib.suppress(Exception):
                cls = window.get_wm_class()
                if cls and "stalonetray" in cls[0].lower():
                    return window
            with contextlib.suppress(Exception):
                for child in window.query_tree().children:
                    found = walk(child, depth + 1)
                    if found is not None:
                        return found
            return None

        host = walk(disp.screen().root, 0)
        if host is None:
            return False
        return len(host.query_tree().children) > 0
    finally:
        with contextlib.suppress(Exception):
            disp.close()


def run_checks() -> None:
    try:
        time.sleep(5)  # let webkit map the overlay window

        # --- overlay click-through -------------------------------------------
        applied = apply_x11_click_through()
        check("click-through input shape applied", applied)
        empty = x11_input_shape_is_empty()
        check("overlay input region is empty (clicks pass through)", empty is True)

        # --- tray icon docks into a real systray host -------------------------
        # stalonetray is an XEmbed host, so force pystray's xorg backend (the
        # appindicator backends need a DBus StatusNotifier host instead).
        os.environ["PYSTRAY_BACKEND"] = "xorg"
        tray_proc = start_owned_process(
            ["stalonetray", "--geometry", "1x1+0+0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        try:
            import pystray
            from PIL import Image

            icon = pystray.Icon(
                "dcent-voice-test",
                Image.new("RGBA", (16, 16), (255, 110, 0, 255)),
                "DCENT_Voice",
            )
            thread = threading.Thread(target=icon.run, daemon=True)
            thread.start()
            docked = False
            deadline = time.time() + 8
            while time.time() < deadline:
                if _tray_host_has_dock():
                    docked = True
                    break
                time.sleep(0.4)
            check("pystray icon docked into systray host", docked)
            with contextlib.suppress(Exception):
                icon.stop()
        finally:
            terminate_owned_process(tray_proc, grace_s=1.0, kill_s=5.0)
    except Exception as exc:  # noqa: BLE001
        check(f"unexpected error: {type(exc).__name__}: {exc}", False)
    finally:
        ok = "ALL LINUX DESKTOP E2E CHECKS PASSED"
        print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else ok}", flush=True)
        os._exit(1 if FAILURES else 0)


def main() -> None:
    assets = Path(__file__).resolve().parents[1] / "src" / "dcent_voice" / "ui" / "web"
    webview.create_window(
        OVERLAY_TITLE,
        url=str(assets / "overlay.html"),
        width=420,
        height=180,
        frameless=True,
        on_top=True,
    )
    threading.Thread(target=run_checks, daemon=True).start()
    webview.start(gui="gtk")


if __name__ == "__main__":
    main()
