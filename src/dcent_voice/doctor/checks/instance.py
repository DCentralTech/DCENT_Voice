# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Is something already running, and did a previous run leave litter behind?

"I double-click it and nothing happens" is the same symptom whether the app is
already running (single-instance lock, tray icon hidden in the overflow chevron)
or whether a dead run left a stale lock and a port squatter. These checks
distinguish the two without disturbing either: the mutex is opened and closed,
the port is probed with a bind, and nothing is ever reclaimed or killed.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Any

from dcent_voice.util import paths

from ..result import PASS, WARN, CheckResult

_ERROR_ALREADY_EXISTS = 183


def run(*, port: int | None = None) -> list[CheckResult]:
    return [
        check_mutex(),
        check_lock_file(),
        check_ade_registry(),
        check_service_port(port),
        check_autostart(),
    ]


def check_mutex() -> CheckResult:
    """Open the production single-instance mutex; never take it."""
    if sys.platform != "win32":
        return CheckResult(
            "instance.mutex",
            PASS,
            "not applicable: the named-mutex single-instance primitive is Windows-only",
        )
    from dcent_voice.attach.single_instance import _WINDOWS_MUTEX_NAME

    held, error = _mutex_held(_WINDOWS_MUTEX_NAME)
    data = {"name": _WINDOWS_MUTEX_NAME, "held": held, "error": error}
    if error:
        return CheckResult(
            "instance.mutex",
            WARN,
            f"the single-instance mutex could not be probed: {error}",
            "This usually means an unusual session or security policy. Sign out and back in, "
            "then try again.",
            data,
        )
    if held:
        return CheckResult(
            "instance.mutex",
            WARN,
            "another DCENT_Voice instance already holds the single-instance lock, so a new "
            "launch will hand off to it instead of opening a second window.",
            "This is normal if the app is already running: look for the tray icon (Windows "
            "hides new icons under the '^' chevron next to the clock). To start fresh, exit "
            "from the tray menu, or end dcent-voice.exe in Task Manager.",
            data,
        )
    return CheckResult("instance.mutex", PASS, "the single-instance mutex is free", data=data)


def check_lock_file() -> CheckResult:
    from dcent_voice.attach.registry import default_registry_dir, is_pid_running

    path = default_registry_dir() / "dcent-voice.lock"
    data: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return CheckResult(
            "instance.lock_file", PASS, f"no instance lock file at {path}", data=data
        )
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        data["error"] = str(exc)
        return CheckResult(
            "instance.lock_file",
            WARN,
            f"the instance lock file at {path} could not be read: {exc}",
            "Close DCENT_Voice and delete the file, then relaunch.",
            data,
        )
    pid = int(raw) if raw.isdigit() else None
    data["pid"] = pid
    alive = is_pid_running(pid) if pid else False
    data["pidAlive"] = alive
    if pid and alive:
        return CheckResult(
            "instance.lock_file",
            PASS,
            f"the instance lock is held by a live process (pid {pid})",
            data=data,
        )
    return CheckResult(
        "instance.lock_file",
        WARN,
        f"a stale instance lock file remains at {path} (pid {raw or 'unknown'} is not running). "
        "The app clears this automatically on the next launch.",
        "No action needed. If a launch still refuses to start, delete the file manually.",
        data,
    )


def check_ade_registry() -> CheckResult:
    from dcent_voice.attach.registry import (
        default_registry_dir,
        is_pid_running,
        read_registry_entry,
    )

    root = default_registry_dir()
    data: dict[str, Any] = {"registryDir": str(root), "entries": []}
    if not root.is_dir():
        return CheckResult(
            "instance.ade_registry",
            PASS,
            f"no ADE module registry directory yet at {root} (it is created on first launch)",
            data=data,
        )
    stale: list[str] = []
    unreadable: list[str] = []
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".install.json"):
            continue
        try:
            entry = read_registry_entry(path)
        except Exception as exc:  # noqa: BLE001 - a corrupt entry is a finding, not a crash
            unreadable.append(f"{path.name}: {type(exc).__name__}")
            continue
        alive = entry.pid is not None and is_pid_running(entry.pid)
        data["entries"].append(
            {
                "file": path.name,
                "moduleId": entry.moduleId,
                "endpoint": entry.endpoint,
                "pid": entry.pid,
                "pidAlive": alive,
            }
        )
        if entry.pid is not None and not alive:
            stale.append(path.name)
    data["staleEntries"] = stale
    data["unreadableEntries"] = unreadable
    if unreadable:
        return CheckResult(
            "instance.ade_registry",
            WARN,
            f"{len(unreadable)} ADE registry file(s) could not be parsed: "
            + "; ".join(unreadable[:5]),
            f"Delete the listed files under {root}; DCENT_Voice rewrites its entry on launch.",
            data,
        )
    if stale:
        return CheckResult(
            "instance.ade_registry",
            WARN,
            f"{len(stale)} ADE registry entry(ies) point at processes that are gone: "
            + ", ".join(stale),
            "No action needed: DCENT_Voice removes stale entries at startup.",
            data,
        )
    return CheckResult(
        "instance.ade_registry",
        PASS,
        f"{len(data['entries'])} ADE registry entry(ies) under {root}, none stale",
        data=data,
    )


def check_service_port(port: int | None = None) -> CheckResult:
    from dcent_voice.attach.registry import (
        default_registry_dir,
        is_pid_running,
        read_registry_entry,
    )

    resolved = port or 8765
    free, error = _port_is_bindable("127.0.0.1", resolved)
    data: dict[str, Any] = {"host": "127.0.0.1", "port": resolved, "free": free, "error": error}
    if free:
        return CheckResult(
            "instance.service_port",
            PASS,
            f"the loopback service port 127.0.0.1:{resolved} is free",
            data=data,
        )

    owner = None
    entry_path = default_registry_dir() / "dcent-voice.json"
    if entry_path.is_file():
        try:
            entry = read_registry_entry(entry_path)
        except Exception:  # noqa: BLE001 - unreadable entry is covered by instance.ade_registry
            entry = None
        if entry is not None and entry.endpoint.endswith(f":{resolved}"):
            owner = {
                "pid": entry.pid,
                "endpoint": entry.endpoint,
                "alive": entry.pid is not None and is_pid_running(entry.pid),
            }
    data["owner"] = owner
    if owner and owner["alive"]:
        return CheckResult(
            "instance.service_port",
            PASS,
            f"127.0.0.1:{resolved} is bound by this app's own running instance "
            f"(pid {owner['pid']})",
            data=data,
        )
    # With an isolated profile root the ADE registry is empty by construction, so
    # a real instance running on the user's normal profile cannot be recognised.
    # Say that, rather than implying an unknown program has taken the port.
    isolated = paths.profile_root() is not None
    data["isolatedProfile"] = isolated
    if isolated:
        return CheckResult(
            "instance.service_port",
            WARN,
            f"127.0.0.1:{resolved} is already in use ({error or 'bind refused'}). This run uses "
            "an isolated profile root, so its ADE registry cannot tell us whether the owner is "
            "another DCENT_Voice instance or an unrelated program.",
            "If DCENT_Voice is running normally, this is expected. Otherwise close whatever "
            "owns the port, or change [service] port in config.toml.",
            data,
        )
    return CheckResult(
        "instance.service_port",
        WARN,
        f"127.0.0.1:{resolved} is already in use and does not belong to a live DCENT_Voice "
        f"instance ({error or 'bind refused'}). The local API will fail to start.",
        "Close whatever owns the port, or change [service] port in config.toml to a free "
        "port and relaunch.",
        data,
    )


def check_autostart() -> CheckResult:
    """Is the login item registered, and does it still point at a real file?

    All three platforms have the same failure: DCENT_Voice was moved, upgraded
    or reinstalled somewhere else, the login item still names the old path, and
    "start at login" quietly does nothing forever. Only the storage differs — a
    registry value on Windows, a file on macOS and Linux.
    """
    if sys.platform != "win32":
        return _check_autostart_login_item()
    from dcent_voice.autostart import _RUN_KEY, APP_NAME

    try:
        import winreg
    except ImportError:  # pragma: no cover
        return CheckResult("instance.autostart", PASS, "winreg unavailable", data={})
    value = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            raw, _kind = winreg.QueryValueEx(key, APP_NAME)
        value = raw if isinstance(raw, str) else ""
    except OSError:
        value = ""
    data: dict[str, Any] = {"key": f"HKCU\\{_RUN_KEY}", "valueName": APP_NAME, "value": value}
    if not value:
        return CheckResult(
            "instance.autostart",
            PASS,
            "start at login is off (no HKCU Run value)",
            data=data,
        )
    target = _first_quoted_or_word(value)
    data["target"] = target
    if target and not Path(target).exists():
        return CheckResult(
            "instance.autostart",
            # Self-healing, so a warning rather than a failure: autostart.py
            # rewrites this value on every launch. The installer's post-install
            # self-check runs BEFORE that first launch, so a reinstall into a new
            # folder would otherwise report "the self-check found a problem" for a
            # condition that repairs itself moments later.
            WARN,
            f"start at login points at {target}, which does not exist, so signing in would "
            "do nothing until the app has run once from its current location.",
            "Launch DCENT_Voice once; it repairs its login item on every start. If it "
            "still points at the wrong place, toggle 'Start at login' off and on in "
            "Settings.",
            data,
        )
    return CheckResult(
        "instance.autostart",
        PASS,
        f"start at login is on and points at an existing target: {target or value}",
        data=data,
    )


def _check_autostart_login_item() -> CheckResult:
    """macOS LaunchAgent plist / Linux XDG autostart entry."""
    from dcent_voice.autostart import login_item_path

    path = login_item_path()
    if path is None:
        return CheckResult(
            "instance.autostart",
            PASS,
            "not applicable: this platform has no supported login-item mechanism",
        )
    label = "LaunchAgent" if sys.platform == "darwin" else "XDG autostart entry"
    data: dict[str, Any] = {"path": str(path), "exists": path.exists(), "kind": label}
    if not path.exists():
        return CheckResult(
            "instance.autostart",
            PASS,
            f"start at login is off (no {label} at {path})",
            data=data,
        )
    target, error = _login_item_target(path)
    data["target"] = target
    data["parseError"] = error
    if error:
        return CheckResult(
            "instance.autostart",
            WARN,
            f"the {label} at {path} could not be read: {error}",
            f"Turn 'Start at login' off and on again in Settings to rewrite it, or delete {path}.",
            data,
        )
    if target and not Path(target).exists():
        return CheckResult(
            "instance.autostart",
            # Self-healing for the same reason as the Windows Run value above.
            WARN,
            f"start at login points at {target}, which does not exist, so logging in would "
            "do nothing until the app has run once. This is what a moved .app, a renamed "
            "AppImage or a reinstall leaves behind.",
            "Launch DCENT_Voice once from its current location; it rewrites the login item "
            f"on every start. Otherwise delete {path}.",
            data,
        )
    return CheckResult(
        "instance.autostart",
        PASS,
        f"start at login is on and its {label} points at an existing target: {target}",
        data=data,
    )


def _login_item_target(path: Path) -> tuple[str, str]:
    """(executable, error) for a LaunchAgent plist or a desktop entry."""
    try:
        if path.suffix == ".plist":
            import plistlib

            payload = plistlib.loads(path.read_bytes())
            arguments = payload.get("ProgramArguments") or []
            if not arguments:
                return "", "the plist has no ProgramArguments"
            return str(arguments[0]), ""
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Exec="):
                return _first_quoted_or_word(line[len("Exec=") :]), ""
        return "", "the desktop entry has no Exec= line"
    except Exception as exc:  # noqa: BLE001 - an unreadable login item is a finding
        return "", f"{type(exc).__name__}: {exc}"


def _mutex_held(name: str) -> tuple[bool, str]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateMutexW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    handle = create(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        return False, f"CreateMutexW failed (WinError {error})"
    try:
        return error == _ERROR_ALREADY_EXISTS, ""
    finally:
        kernel32.CloseHandle(handle)


def _port_is_bindable(host: str, port: int) -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            # Without this a lingering TIME_WAIT socket reads as "in use" on POSIX,
            # which the app itself would not experience.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc.strerror or exc}"
    return True, ""


def _first_quoted_or_word(command: str) -> str:
    text = command.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text[1:]
    return text.split(" ", 1)[0]
