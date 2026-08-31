# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""First-launch education copy, its native fallback dialog, and shell actions.

A person who double-clicks DCENT_Voice for the first time must see something.
The primary surface is the setup wizard (pywebview → WebView2 on Windows). On a
host without the Edge WebView2 runtime the wizard cannot open at all, so the
same three facts are shown once in a native ``MessageBoxW``:

* the hotkey, humanized from the live config ("Hold Ctrl+Win and speak");
* where the tray icon is, including Windows' overflow chevron;
* that nothing leaves the machine.

Both surfaces are first-run only and gated by
``privacy.first_run_education_shown``; neither may be shown on a later launch.
Hold-to-talk is already live before either appears — education never blocks
dictation.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dcent_voice.config import APP_NAME
from dcent_voice.ui.webview_runtime import WEBVIEW2_DOWNLOAD_URL

logger = logging.getLogger(APP_NAME).getChild("first_run")

DIALOG_TITLE = "DCENT_Voice is running"

_MB_OK = 0x0
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000

_KEY_DISPLAY = {
    "ctrl": "Ctrl",
    "win": "Win",
    "cmd": "Cmd",
    "alt": "Alt",
    "shift": "Shift",
    "esc": "Esc",
}

LOCAL_ONLY_LINE = (
    "Everything stays on this machine. Your voice is transcribed locally and nothing is uploaded."
)

WINDOWS_TRAY_LINE = (
    "The tray icon is in the notification area next to the clock. Windows may "
    "hide it under the ^ chevron; drag it to the taskbar to pin it."
)

OTHER_TRAY_LINE = (
    "The tray icon is in your system tray. Settings and this setup wizard open "
    "from that icon at any time."
)

WEBKITGTK_MISSING_PARAGRAPH = (
    "Settings, the overlay, and the setup wizard need WebKitGTK, which is not "
    "installed on this computer. Hold-to-talk dictation works without it. "
    "Install it with: sudo apt install gir1.2-webkit2-4.1 python3-gi "
    "gir1.2-gtk-3.0 (use gir1.2-webkit2-4.0 on Ubuntu 22.04)."
)

WEBVIEW2_MISSING_PARAGRAPH = (
    "Settings, the overlay, and the setup wizard need the Microsoft Edge "
    "WebView2 runtime, which is not installed on this computer. Hold-to-talk "
    f"dictation works without it. Install it from {WEBVIEW2_DOWNLOAD_URL} — the "
    'tray menu also has "Install WebView2 runtime…".'
)

SHOWN_ONCE_LINE = "This message is shown once. Everything else lives in the tray icon."


def format_chord(chord: str) -> str:
    """Render a config chord like ``ctrl+win`` as ``Ctrl+Win`` for humans."""
    parts = [p.strip() for p in (chord or "").split("+") if p.strip()]
    display = [_KEY_DISPLAY.get(p.lower(), p.upper() if len(p) == 1 else p.title()) for p in parts]
    return "+".join(display)


def hotkey_line(chord: str, mode: str = "hold") -> str:
    """The one sentence a new user needs, using the configured chord."""
    verb = "Hold" if mode == "hold" else "Press"
    return (
        f"{verb} {format_chord(chord)} and speak. Release and the text lands where you were typing."
    )


def tray_line(platform: str | None = None) -> str:
    return WINDOWS_TRAY_LINE if (platform or sys.platform) == "win32" else OTHER_TRAY_LINE


def education_lines(config: Any, *, platform: str | None = None) -> tuple[str, str, str]:
    """The three first-run facts, in the order both surfaces present them."""
    hotkeys = getattr(config, "hotkeys", None)
    chord = getattr(hotkeys, "dictation", "") or ""
    mode = getattr(hotkeys, "mode", "hold") or "hold"
    return (hotkey_line(chord, mode), tray_line(platform), LOCAL_ONLY_LINE)


def dialog_text(
    config: Any,
    *,
    webview2_missing: bool = False,
    gui_missing: bool = False,
    platform: str | None = None,
) -> str:
    """Body of the native first-run dialog (the wizard shows the same facts).

    ``webview2_missing`` is the Windows story; ``gui_missing`` is the same fact
    on a host with no usable web view for any other reason — in practice a Linux
    machine without the WebKitGTK typelib, which is exactly as fatal to the
    Settings window as a missing WebView2 runtime is on Windows.
    """
    target = platform or sys.platform
    blocks = ["DCENT_Voice is running."]
    blocks.extend(f"• {line}" for line in education_lines(config, platform=target))
    if webview2_missing:
        blocks.append(WEBVIEW2_MISSING_PARAGRAPH)
    elif gui_missing and target.startswith("linux"):
        blocks.append(WEBKITGTK_MISSING_PARAGRAPH)
    blocks.append(SHOWN_ONCE_LINE)
    return "\n\n".join(blocks)


def dialogs_suppressed() -> bool:
    """Honor ``DCENT_VOICE_NO_DIALOGS=1`` (CI, smokes, tests)."""
    from dcent_voice.util.fatal import NO_DIALOGS_ENV

    return (os.environ.get(NO_DIALOGS_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def show_first_run_dialog(
    config: Any, *, webview2_missing: bool = False, gui_missing: bool = False
) -> bool:
    """Show the native first-run dialog. Returns True when a human saw it.

    Blocks until the dialog is dismissed, so callers run it off the main thread.
    Every platform has a surface, because "the wizard did not open" must never
    mean "the user was told nothing":

    * Windows — ``MessageBoxW`` (modal, always seen);
    * macOS   — ``osascript display dialog`` (modal, always seen);
    * Linux   — ``notify-send`` *and* stderr. A desktop notification is the only
      thing a Linux host is guaranteed to have without pulling in a toolkit, and
      the stderr copy is written unconditionally so a terminal launch and the
      AppImage's own stderr warnings end up in the same place.
    """
    if dialogs_suppressed():
        logger.info("first-run dialog suppressed by %s", "DCENT_VOICE_NO_DIALOGS")
        return False
    from dcent_voice.util.fatal import desktop_session_available

    if not desktop_session_available():
        logger.info("first-run dialog skipped: no desktop session")
        return False
    text = dialog_text(config, webview2_missing=webview2_missing, gui_missing=gui_missing)
    try:
        if sys.platform == "win32":
            return _show_windows_dialog(text)
        if sys.platform == "darwin":
            return _show_macos_dialog(text)
        return _show_linux_notification(text)
    except Exception:
        logger.warning("first-run dialog could not be shown", exc_info=True)
        return False


def _show_windows_dialog(text: str) -> bool:
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        0,
        text,
        DIALOG_TITLE,
        _MB_OK | _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST,
    )
    return True


def _show_macos_dialog(text: str) -> bool:
    """``display dialog`` blocks until OK, which is what "the user saw it" means."""
    import subprocess  # noqa: S404 - fixed argv, no shell

    from dcent_voice.util.applescript import quote

    # Not json.dumps: AppleScript has no \uXXXX escape, and the education text
    # is full of bullets ("•") that an ASCII-safe JSON literal would mangle.
    script = (
        f"display dialog {quote(text)} with title {quote(DIALOG_TITLE)} "
        'buttons {"OK"} default button "OK" with icon note'
    )
    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/osascript", "-e", script],
        check=False,
        capture_output=True,
        timeout=300,
    )
    if completed.returncode != 0:
        logger.warning("osascript first-run dialog failed: %s", completed.stderr[:400])
        return False
    return True


def _show_linux_notification(text: str) -> bool:
    """notify-send plus stderr. Returns True only when the notification landed."""
    import shutil
    import subprocess  # noqa: S404 - fixed argv, no shell

    _write_stderr(text)
    notify = shutil.which("notify-send")
    if not notify:
        logger.info("notify-send is not installed; the first-run text went to stderr only")
        return False
    completed = subprocess.run(  # noqa: S603
        [notify, "--app-name=DCENT_Voice", "--expire-time=30000", DIALOG_TITLE, text],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        logger.warning("notify-send failed: %s", completed.stderr[:400])
        return False
    return True


def _write_stderr(text: str) -> None:
    """Best-effort stderr copy. A frozen windowed build has no stderr at all."""
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    with contextlib.suppress(Exception):
        stream.write(f"\nDCENT_Voice: {text}\n\n")
        stream.flush()


def log_folder() -> Path:
    """Directory that holds ``dcent_voice.log`` / ``startup.log``."""
    from dcent_voice.util import paths

    return paths.user_config_dir() / "logs"


def open_folder(path: Path) -> bool:
    """Open a folder in the platform file manager. Never raises."""
    target = Path(path)
    with contextlib.suppress(Exception):
        target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        logger.error("cannot open missing folder: %s", target)
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606 - fixed local path, no shell
            return True
        import shutil
        import subprocess  # noqa: S404 - fixed argv, no shell

        opener = "/usr/bin/open" if sys.platform == "darwin" else shutil.which("xdg-open")
        if not opener:
            logger.error("no folder opener available for %s", target)
            return False
        subprocess.Popen(  # noqa: S603
            [opener, str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        logger.exception("failed to open folder %s", target)
        return False


def open_webview2_download() -> bool:
    """Open the Microsoft WebView2 page. Only ever from an explicit user click."""
    import webbrowser

    try:
        return bool(webbrowser.open(WEBVIEW2_DOWNLOAD_URL))
    except Exception:
        logger.exception("failed to open the WebView2 download page")
        return False
