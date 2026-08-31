# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Linux X11 GUI end-to-end checks, run inside Xvfb + openbox.

Exercises the real Linux backends against a live X server:
1. Clipboard set/get round-trip through the injector's own helpers (real xclip),
   including a timing assertion that the daemonize/DEVNULL fix holds (a
   regression would block ~2s per set).
2. Full LinuxClipboardPasteInjector.inject() — set, paste keystroke via real
   xdotool, restore-previous — verifying the clipboard is restored afterwards.
3. Global hotkeys: a real pynput listener receives a ctrl+win chord synthesized
   by xdotool, end-to-end through HotkeyManager and the EventBus.

Run on any Linux box/CI with an X session (or Xvfb), or from Windows via Docker
Desktop using scripts/docker_linux_e2e.sh:

    docker run --rm -v "<repo>:/src:ro" \
      -v "<repo>/scripts/linux_gui_e2e.py:/e2e.py:ro" \
      -v "<repo>/scripts/docker_linux_e2e.sh:/run.sh:ro" \
      python:3.11-slim bash /run.sh

Verified green 2026-07-07 (python:3.11-slim, Xvfb+openbox): clipboard set 4ms,
inject 88ms with restore, hotkey press+release detected.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcent_voice.config import HotkeyConfig  # noqa: E402
from dcent_voice.events import EventBus, HotkeyPressed, HotkeyReleased  # noqa: E402
from dcent_voice.hotkeys import HotkeyManager  # noqa: E402
from dcent_voice.inject.linux import (  # noqa: E402
    LinuxClipboardPasteInjector,
    _clipboard_tools,
    _paste_argv,
    _run,
    is_wayland,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{f' — {detail}' if detail else ''}", flush=True)
    if not condition:
        FAILURES.append(name)


def main() -> int:
    check("session is X11 (not Wayland)", not is_wayland())

    set_argv, get_argv = _clipboard_tools()
    check("clipboard tools detected", set_argv is not None and get_argv is not None)
    paste = _paste_argv()
    check("paste tool detected (xdotool)", paste is not None)
    if set_argv is None or get_argv is None or paste is None:
        return 1

    # 1) Round-trip + daemonize timing (regression guard for the xclip
    #    capture_output deadlock: a regression makes every set block ~2s).
    start = time.time()
    _run(set_argv, text="dcent-e2e-payload")
    set_ms = (time.time() - start) * 1000
    check("clipboard set returns promptly", set_ms < 1000, f"{set_ms:.0f}ms")
    read_back = (_run(get_argv) or "").strip()
    check("clipboard get returns what was set", read_back == "dcent-e2e-payload", repr(read_back))

    # 2) Full injector call: previous content must be restored after the paste.
    _run(set_argv, text="previous-content")
    injector = LinuxClipboardPasteInjector(restore_previous=True)
    start = time.time()
    injector.inject("injected text")
    inject_ms = (time.time() - start) * 1000
    time.sleep(0.3)  # allow the restore write to settle
    restored = (_run(get_argv) or "").strip()
    check("inject() completes promptly", inject_ms < 3000, f"{inject_ms:.0f}ms")
    check("clipboard restored after paste", restored == "previous-content", repr(restored))

    # 3) Real global hotkey: pynput listener + xdotool-synthesized ctrl+super.
    bus = EventBus()
    pressed = threading.Event()
    released = threading.Event()

    def on_event(ev) -> None:
        if isinstance(ev, HotkeyPressed):
            pressed.set()
        elif isinstance(ev, HotkeyReleased):
            released.set()

    bus.subscribe(on_event)
    bus.start()
    manager = HotkeyManager(HotkeyConfig(), bus)  # defaults: dictation=ctrl+win
    manager.start()
    time.sleep(1.0)  # listener warm-up

    subprocess.run(["xdotool", "keydown", "ctrl", "keydown", "super"], check=True)
    got_press = pressed.wait(3.0)
    subprocess.run(["xdotool", "keyup", "super", "keyup", "ctrl"], check=True)
    got_release = released.wait(3.0)
    check("global hotkey press detected (ctrl+win via xdotool)", got_press)
    check("global hotkey release detected", got_release)

    manager.stop()
    bus.stop()

    ok = "ALL LINUX GUI E2E CHECKS PASSED"
    verdict = "FAILED: " + ", ".join(FAILURES) if FAILURES else ok
    print(f"\n{verdict}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
