# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""The host-desktop dependencies Linux and macOS need — and Windows does not.

Windows' "why can't I open Settings" story is WebView2 and .NET Framework
(:mod:`.ui_runtime`). The equivalent stories on the other two platforms are
completely different, and until now doctor said nothing about either:

**Linux** — text insertion is not an API, it is a set of external programs, and
which ones are needed depends on the session type. Under X11 that is
``xclip``/``xsel`` plus ``xdotool``; under Wayland it is ``wl-copy`` plus
``wtype`` or ``ydotool``. The AppImage's ``AppRun`` and the ``.deb`` wrapper
already warn about this on stderr, where a double-clicking user never looks.
``pynput`` additionally cannot read a global hotkey on Wayland without access to
``/dev/uinput``, which normally means membership of the ``input`` group. And the
Settings window is GTK/WebKit2, so a host without the ``gir1.2-webkit2-4.1``
typelib gets the same "nothing opens" symptom as a Windows host without
WebView2.

**macOS** — both of the things dictation needs are TCC permissions the user has
to grant by hand: Microphone (to record at all) and Accessibility (so the
keyboard listener sees a global hotkey and the injector can type). Neither can
be granted programmatically; the remediation is the exact System Settings pane
to open, and the checks are written to be usable when read out over a chat.

Severity follows the same rule as everywhere else in doctor: something that
stops dictation is a failure, something that only stops a window is a warning,
and an answer we cannot obtain is a warning that says so rather than a guess.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from ..result import FAIL, PASS, WARN, CheckResult

#: The System Settings panes a macOS user has to visit, by check.
ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
MICROPHONE_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"

#: X11 needs a clipboard writer and a synthetic-key sender.
_X11_CLIPBOARD = ("xclip", "xsel")
_X11_KEYSTROKE = ("xdotool",)
#: Wayland's equivalents. ``wtype`` is preferred; ``ydotool`` needs the daemon.
_WAYLAND_CLIPBOARD = ("wl-copy",)
_WAYLAND_KEYSTROKE = ("wtype", "ydotool")

_UINPUT = "/dev/uinput"


def run(*, timeout_s: float = 60.0) -> list[CheckResult]:
    """Every desktop-host check for the platform we are actually on."""
    if sys.platform.startswith("linux"):
        session = session_type()
        return [
            check_session(session),
            check_portaudio(),
            check_injection_tools(session),
            check_uinput(session),
            check_webkitgtk(timeout_s=timeout_s),
        ]
    if sys.platform == "darwin":
        return [check_macos_accessibility(), check_macos_microphone(), check_macos_dependencies()]
    skipped = "not applicable: this check describes the Linux and macOS desktop hosts"
    return [CheckResult("desktop.session", PASS, skipped)]


# --- Linux -------------------------------------------------------------------


def session_type(environ: dict[str, str] | None = None) -> str:
    """``"wayland"``, ``"x11"`` or ``"none"`` for the current login session.

    ``XDG_SESSION_TYPE`` is authoritative when the display manager sets it, but
    plenty of setups (``startx``, containers, ``xvfb-run``, remote shells) do
    not, so fall back to which display socket is actually advertised.
    """
    env = os.environ if environ is None else environ
    declared = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if declared in {"wayland", "x11"}:
        return declared
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "none"


def check_session(session: str) -> CheckResult:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or "unknown"
    data = {
        "sessionType": session,
        "desktop": desktop,
        "display": os.environ.get("DISPLAY") or "",
        "waylandDisplay": os.environ.get("WAYLAND_DISPLAY") or "",
    }
    if session == "none":
        return CheckResult(
            "desktop.session",
            WARN,
            "no graphical session is attached to this process, so the tray icon, the overlay "
            "and Settings have nowhere to appear. Dictation into another window is not "
            "possible either.",
            "Run DCENT_Voice from inside your desktop session. Over SSH, this is expected "
            "and only the headless API is usable.",
            data,
        )
    return CheckResult(
        "desktop.session",
        PASS,
        f"{session} session on {desktop}",
        data=data,
    )


def check_portaudio() -> CheckResult:
    """PortAudio is the one shared library the wheel does *not* carry on Linux."""
    from ctypes.util import find_library

    found = find_library("portaudio")
    candidates = [
        name
        for name in ("libportaudio.so.2", "libportaudio.so")
        if any(os.path.exists(os.path.join(d, name)) for d in _library_dirs())
    ]
    data: dict[str, Any] = {"findLibrary": found or "", "candidates": candidates}
    if found or candidates:
        return CheckResult(
            "desktop.portaudio",
            PASS,
            f"PortAudio is installed ({found or candidates[0]})",
            data=data,
        )
    return CheckResult(
        "desktop.portaudio",
        FAIL,
        "PortAudio (libportaudio2) is not installed, so DCENT_Voice cannot open a microphone "
        "and dictation cannot record anything.",
        "Install it: `sudo apt install libportaudio2` (Debian/Ubuntu), "
        "`sudo dnf install portaudio` (Fedora), `sudo pacman -S portaudio` (Arch).",
        data,
    )


def _library_dirs() -> list[str]:
    dirs = ["/usr/lib", "/usr/lib64", "/usr/local/lib", "/lib", "/lib64"]
    for extra in ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu"):
        dirs.append(extra)
    dirs.extend(part for part in (os.environ.get("LD_LIBRARY_PATH") or "").split(":") if part)
    return dirs


def check_injection_tools(session: str) -> CheckResult:
    """Can we put text into the window the user was typing in?"""
    found = {name: shutil.which(name) or "" for name in _ALL_TOOLS}
    x11_ready = _any(found, _X11_CLIPBOARD) and _any(found, _X11_KEYSTROKE)
    wayland_ready = _any(found, _WAYLAND_CLIPBOARD) and _any(found, _WAYLAND_KEYSTROKE)
    data: dict[str, Any] = {
        "sessionType": session,
        "tools": {name: path for name, path in found.items() if path},
        "missing": sorted(name for name, path in found.items() if not path),
        "x11Ready": x11_ready,
        "waylandReady": wayland_ready,
    }
    if session == "none":
        # The question cannot be answered without knowing the session type, and
        # desktop.session already carries that finding. Failing here too would
        # turn every headless run (CI, SSH) red twice over for one cause.
        return CheckResult(
            "desktop.injection_tools",
            WARN,
            "no graphical session is attached, so which text-insertion helpers are needed "
            "cannot be determined (see desktop.session). Installed helpers: "
            + (", ".join(sorted(data["tools"])) or "none"),
            "Run DCENT_Voice from inside your desktop session, then re-run diagnostics.",
            data,
        )
    ready = wayland_ready if session == "wayland" else x11_ready
    if ready:
        return CheckResult(
            "desktop.injection_tools",
            PASS,
            f"the helpers universal text insertion needs on {session} are installed",
            data=data,
        )
    if session == "wayland":
        detail = (
            "this is a Wayland session and the helpers universal text insertion needs are "
            "missing, so transcribed text cannot be placed into other applications"
        )
        remediation = (
            "Install them: `sudo apt install wl-clipboard wtype` (or `ydotool` with its "
            "daemon running). See docs/TROUBLESHOOTING.md."
        )
    elif session == "x11":
        detail = (
            "this is an X11 session and the helpers universal text insertion needs are "
            "missing, so transcribed text cannot be placed into other applications"
        )
        remediation = "Install them: `sudo apt install xclip xdotool`."
    else:  # pragma: no cover - session_type only ever returns x11/wayland/none
        detail = f"unrecognized session type {session!r}"
        remediation = "Report this: DCENT_Voice does not know how to insert text here."
    return CheckResult("desktop.injection_tools", FAIL, detail, remediation, data)


_ALL_TOOLS = (*_X11_CLIPBOARD, *_X11_KEYSTROKE, *_WAYLAND_CLIPBOARD, *_WAYLAND_KEYSTROKE)


def _any(found: dict[str, str], names: tuple[str, ...]) -> bool:
    return any(found.get(name) for name in names)


def check_uinput(session: str) -> CheckResult:
    """``pynput`` cannot see a global hotkey on Wayland without ``/dev/uinput``."""
    groups = _group_names()
    exists = os.path.exists(_UINPUT)
    writable = exists and os.access(_UINPUT, os.W_OK)
    data: dict[str, Any] = {
        "sessionType": session,
        "device": _UINPUT,
        "exists": exists,
        "writable": writable,
        "groups": groups,
        "inInputGroup": "input" in groups,
    }
    if session != "wayland":
        return CheckResult(
            "desktop.uinput",
            PASS,
            "not required: X11 sessions deliver global hotkeys through the X server, so "
            f"{_UINPUT} access is not needed",
            data=data,
        )
    if writable:
        return CheckResult(
            "desktop.uinput",
            PASS,
            f"{_UINPUT} is writable, so the global push-to-talk hotkey works on Wayland",
            data=data,
        )
    detail = (
        f"{_UINPUT} does not exist" if not exists else f"{_UINPUT} is not writable by this user"
    )
    return CheckResult(
        "desktop.uinput",
        WARN,
        f"{detail}. Wayland compositors do not deliver global hotkeys to ordinary clients, so "
        "hold-to-talk may not fire outside DCENT_Voice's own window.",
        "Add yourself to the `input` group (`sudo usermod -aG input $USER`), log out and back "
        "in, and load the module if needed (`sudo modprobe uinput`). An X11 session needs "
        "none of this.",
        data,
    )


def _group_names() -> list[str]:
    """Supplementary group names for this process. Empty on a host without grp."""
    try:
        import grp  # noqa: PLC0415 - POSIX-only, imported where it exists
    except ImportError:  # pragma: no cover - non-POSIX
        return []
    names: list[str] = []
    try:
        gids = os.getgroups()
    except (AttributeError, OSError):  # pragma: no cover - defensive
        return names
    for gid in gids:
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except (KeyError, OSError):
            names.append(str(gid))
    return sorted(set(names))


def check_webkitgtk(*, timeout_s: float = 60.0) -> CheckResult:
    """Linux's answer to ``ui.webview2``: no WebKitGTK, no Settings window."""
    from ..probe import probe

    payload = probe("gi_webkit", timeout_s=timeout_s)
    data = dict(payload)
    if payload.get("skipped"):
        return CheckResult("desktop.webkitgtk", PASS, "not applicable on this platform", data=data)
    webkit = payload.get("webkit")
    if payload.get("ok") and webkit:
        return CheckResult(
            "desktop.webkitgtk",
            PASS,
            f"WebKitGTK is available (WebKit2 {webkit} / Soup {payload.get('soup')})",
            data=data,
        )
    reason = str(payload.get("webkitError") or payload.get("detail") or "the import failed")
    return CheckResult(
        "desktop.webkitgtk",
        WARN,
        "WebKitGTK is not usable, so Settings, the overlay and the setup wizard cannot open. "
        f"Hold-to-talk dictation is unaffected. Reason: {reason}",
        "Install the GTK WebKit typelib: `sudo apt install gir1.2-webkit2-4.1` (Ubuntu 24.04+) "
        "or `gir1.2-webkit2-4.0` (Ubuntu 22.04), plus `python3-gi` and `gir1.2-gtk-3.0`.",
        data,
    )


# --- macOS -------------------------------------------------------------------


def check_macos_accessibility() -> CheckResult:
    """Accessibility gates both the global hotkey and keystroke injection."""
    trusted = accessibility_trusted()
    data = {"trusted": trusted, "pane": ACCESSIBILITY_PANE}
    if trusted is None:
        return CheckResult(
            "desktop.accessibility",
            WARN,
            "the Accessibility permission could not be read on this host, so whether the "
            "global hotkey can fire is unknown",
            "Open System Settings > Privacy & Security > Accessibility and confirm DCENT "
            "Voice is listed and enabled.",
            data,
        )
    if trusted:
        return CheckResult(
            "desktop.accessibility",
            PASS,
            "Accessibility is granted, so the global hotkey and keystroke injection work",
            data=data,
        )
    return CheckResult(
        "desktop.accessibility",
        FAIL,
        "Accessibility is not granted to DCENT Voice, so macOS will not deliver the global "
        "push-to-talk hotkey and transcribed text cannot be typed into other applications.",
        "Open System Settings > Privacy & Security > Accessibility, add or enable "
        f"DCENT Voice, then relaunch it. Direct link: {ACCESSIBILITY_PANE}",
        data,
    )


def accessibility_trusted() -> bool | None:
    """``AXIsProcessTrusted()``; ``None`` when the framework cannot be reached."""
    try:
        import ctypes  # noqa: PLC0415
        import ctypes.util  # noqa: PLC0415

        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return None
        framework = ctypes.cdll.LoadLibrary(path)
        framework.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(framework.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001 - an unreadable permission is "unknown", not a crash
        return None


def check_macos_microphone() -> CheckResult:
    """TCC microphone authorization: without it every recording is silence."""
    status = microphone_authorization()
    data = {"status": status, "pane": MICROPHONE_PANE}
    if status is None:
        return CheckResult(
            "desktop.microphone",
            WARN,
            "the Microphone permission could not be read on this host, so whether recording "
            "will produce audio is unknown",
            "Open System Settings > Privacy & Security > Microphone and confirm DCENT Voice "
            "is enabled.",
            data,
        )
    if status == "authorized":
        return CheckResult("desktop.microphone", PASS, "Microphone access is granted", data=data)
    if status == "notDetermined":
        return CheckResult(
            "desktop.microphone",
            WARN,
            "Microphone access has not been requested yet. macOS will prompt the first time "
            "you hold the dictation hotkey.",
            "Hold the dictation hotkey once and choose Allow. If no prompt appears, enable "
            f"DCENT Voice under {MICROPHONE_PANE}",
            data,
        )
    return CheckResult(
        "desktop.microphone",
        FAIL,
        f"Microphone access is {status}, so every recording will be silent and dictation "
        "will transcribe nothing.",
        "Open System Settings > Privacy & Security > Microphone and enable DCENT Voice, "
        f"then relaunch it. Direct link: {MICROPHONE_PANE}",
        data,
    )


_AV_STATUS = {0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized"}


def microphone_authorization() -> str | None:
    """AVFoundation's TCC status for audio, or ``None`` when unreadable."""
    try:
        import objc  # noqa: PLC0415
        from AVFoundation import AVCaptureDevice  # noqa: PLC0415

        _ = objc
        raw = AVCaptureDevice.authorizationStatusForMediaType_("soun")
        return _AV_STATUS.get(int(raw), f"unknown({int(raw)})")
    except Exception:  # noqa: BLE001 - pyobjc may be absent; that is "unknown"
        return None


def check_macos_dependencies() -> CheckResult:
    """The .app is self-contained: Homebrew is never a runtime requirement."""
    data = {"homebrew": shutil.which("brew") or "", "bundled": True}
    return CheckResult(
        "desktop.dependencies",
        PASS,
        "no Homebrew or system Python is required: the .app ships its own Python, "
        "PortAudio and speech models",
        data=data,
    )
