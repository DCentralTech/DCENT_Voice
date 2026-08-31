# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Launch-at-login registration.

The ``launch_at_startup`` config flag was parsed and shown in the UI but never
acted on. This registers/unregisters the app with the OS login mechanism so the
flag actually does something, through one ``set_enabled`` entry point on all
three platforms: an HKCU ``Run`` value on Windows, a launchd ``LaunchAgent``
plist on macOS, and an XDG ``autostart`` desktop entry on Linux.

``app.run_app`` calls ``set_enabled(config.launch_at_startup)`` on **every**
launch. That is deliberate and is what keeps the three registrations honest: a
user who moves the ``.app`` to ``/Applications``, upgrades an AppImage to a new
filename, or reinstalls the ``.deb`` would otherwise keep a login item pointing
at an executable that no longer exists — the login item silently does nothing
and "start at login" appears broken. Rewriting the entry to the *current*
``sys.executable`` every time makes a relocation self-healing, and
``set_enabled(False)`` deletes the entry outright rather than leaving a disabled
stub behind. ``doctor``'s ``instance.autostart`` check reports a login item that
points at a missing path on all three platforms.

Neither writer raises: an unwritable ``~/.config`` or ``~/Library`` must degrade
to "start at login could not be registered", never take down a launch.
"""

from __future__ import annotations

import contextlib
import os
import platform
import plistlib
import shlex
import sys
from pathlib import Path

APP_NAME = "DCENT_Voice"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    """The command the OS should run at login to start this app."""
    if getattr(sys, "frozen", False):
        # Packaged one-file/one-dir exe: run it directly.
        return f'"{sys.executable}"'
    # Dev / editable install: prefer pythonw.exe so no console window flashes.
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    launcher = pyw if pyw.exists() else exe
    return f'"{launcher}" -m dcent_voice'


def set_enabled(enabled: bool) -> bool:
    """Make the OS login item match ``enabled``. Returns True on success."""
    system = platform.system()
    if system == "Windows":
        return _set_windows(enabled)
    if system == "Darwin":
        return _set_macos(enabled)
    if system == "Linux":
        return _set_linux(enabled)
    return False


def login_item_path() -> Path | None:
    """The file that registers this app at login, or ``None`` on Windows.

    Windows uses a registry value, not a file; ``doctor`` handles that case
    separately. Exposed so the diagnostic and the tests read the same location
    the writers below use.
    """
    system = platform.system()
    if system == "Darwin":
        return _macos_agent_path()
    if system == "Linux":
        return _linux_desktop_path()
    return None


def _set_windows(enabled: bool) -> bool:
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, APP_NAME)
        return True
    except OSError:
        return False


def _macos_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "tech.d-central.dcent-voice.plist"


def _set_macos(enabled: bool) -> bool:
    plist = _macos_agent_path()
    if not enabled:
        with contextlib.suppress(FileNotFoundError):
            plist.unlink()
        return True
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        # Rewritten in full on every launch, so a bundle moved to /Applications
        # (or any other relocation) repairs its own login item.
        with plist.open("wb") as handle:
            plistlib.dump(
                {
                    "Label": "tech.d-central.dcent-voice",
                    "ProgramArguments": _split(launch_command()),
                    "RunAtLoad": True,
                },
                handle,
                sort_keys=False,
            )
    except OSError:
        return False
    return True


def _linux_desktop_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "dcent-voice.desktop"


def _set_linux(enabled: bool) -> bool:
    entry = _linux_desktop_path()
    if not enabled:
        with contextlib.suppress(FileNotFoundError):
            entry.unlink()
        return True
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        # Rewritten in full on every launch: an AppImage upgraded to a new
        # filename would otherwise leave an entry pointing at a deleted file.
        entry.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=DCENT_Voice\n"
            "Comment=Local-first voice dictation\n"
            # The desktop-entry spec supports double-quoted arguments, so keep the
            # quoting — stripping it would break executable paths containing spaces.
            f"Exec={launch_command()}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def _split(command: str) -> list[str]:
    # posix=False keeps Windows backslashes intact, but it also keeps the
    # surrounding double-quotes on quoted tokens — strip those so consumers
    # (launchd ProgramArguments) get bare executable paths, not `"…\pythonw.exe"`.
    return [token.strip('"') for token in shlex.split(command, posix=False)]
