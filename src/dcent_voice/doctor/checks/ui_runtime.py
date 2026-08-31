# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Host dependencies the *windows* need — never the ones dictation needs.

Settings, the overlay and the setup wizard are pywebview windows: on Windows
that is WinForms via pythonnet (.NET Framework 4.7.2+) rendering with the Edge
WebView2 Evergreen runtime. Neither is required to hold the hotkey and dictate,
so every finding here is a warning with a concrete link, not a failure. This is
exactly the "I can't open the settings of the app" report from a stripped or
LTSC Windows image.
"""

from __future__ import annotations

import sys
from typing import Any

from ..result import PASS, WARN, CheckResult

WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

#: .NET Framework 4.7.2 — the floor pythonnet 3.x needs to host pywebview.
_DOTNET_472_RELEASE = 461808
_DOTNET_RELEASE_KEY = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"

_EDGE_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    r"\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}",
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}",
)


def run() -> list[CheckResult]:
    if sys.platform != "win32":
        skipped = "not applicable: this check describes the Windows UI host"
        return [
            CheckResult("ui.webview2", PASS, skipped),
            CheckResult("ui.dotnet_framework", PASS, skipped),
            CheckResult("ui.edge", PASS, skipped),
        ]
    return [check_webview2(), check_dotnet_framework(), check_edge()]


def check_webview2() -> CheckResult:
    from dcent_voice.ui.webview_runtime import windows_webview2_runtime_present

    versions = webview2_versions()
    present = windows_webview2_runtime_present()
    data: dict[str, Any] = {"present": present, "versions": versions}
    if not present:
        return CheckResult(
            "ui.webview2",
            WARN,
            "the Microsoft Edge WebView2 runtime is not installed, so Settings, the overlay "
            "and the setup wizard cannot open. Hold-to-talk dictation is unaffected.",
            f"Install the Evergreen WebView2 runtime from {WEBVIEW2_DOWNLOAD} and relaunch "
            "DCENT_Voice.",
            data,
        )
    listed = ", ".join(f"{hive}: {value}" for hive, value in versions.items()) or "registered"
    return CheckResult("ui.webview2", PASS, f"WebView2 runtime present ({listed})", data=data)


def webview2_versions() -> dict[str, str]:
    """Raw ``pv`` values from both hives, so a broken 0.0.0.0 stub is visible."""
    from dcent_voice.ui.webview_runtime import _WEBVIEW2_KEYS

    found: dict[str, str] = {}
    try:
        import winreg
    except ImportError:  # pragma: no cover - CPython on Windows always has winreg
        return found
    hives = (("HKLM", winreg.HKEY_LOCAL_MACHINE), ("HKCU", winreg.HKEY_CURRENT_USER))
    for hive_name, hive in hives:
        for path in _WEBVIEW2_KEYS:
            value = _read_string(hive, path, "pv")
            if value:
                view = "WOW6432Node" if "WOW6432Node" in path else "native"
                found[f"{hive_name}/{view}"] = value
    return found


def check_dotnet_framework() -> CheckResult:
    release = _read_dword(_DOTNET_RELEASE_KEY, "Release")
    version = _read_string_hklm(_DOTNET_RELEASE_KEY, "Version")
    data: dict[str, Any] = {"release": release, "version": version, "required": _DOTNET_472_RELEASE}
    if release is None:
        return CheckResult(
            "ui.dotnet_framework",
            WARN,
            "no .NET Framework 4.x installation was found in the registry, so the pywebview "
            "host cannot start. Hold-to-talk dictation is unaffected.",
            "Install .NET Framework 4.8 from Microsoft (it is preinstalled on Windows 10 "
            "version 1809 and later).",
            data,
        )
    if release < _DOTNET_472_RELEASE:
        return CheckResult(
            "ui.dotnet_framework",
            WARN,
            f".NET Framework release {release} ({version or 'unknown version'}) is older than "
            f"4.7.2 (release {_DOTNET_472_RELEASE}), which pythonnet requires to host the "
            "Settings window.",
            "Install .NET Framework 4.8 from Microsoft, then relaunch DCENT_Voice.",
            data,
        )
    return CheckResult(
        "ui.dotnet_framework",
        PASS,
        f".NET Framework {version or release} satisfies the 4.7.2 floor",
        data=data,
    )


def check_edge() -> CheckResult:
    version = ""
    for path in _EDGE_KEYS:
        version = _read_string_hklm(path, "pv") or _read_string_hkcu(path, "pv") or ""
        if version:
            break
    data = {"version": version}
    if not version:
        return CheckResult(
            "ui.edge",
            PASS,
            "Microsoft Edge is not registered on this machine. That is fine on its own: only "
            "the WebView2 runtime matters (see ui.webview2).",
            data=data,
        )
    return CheckResult("ui.edge", PASS, f"Microsoft Edge {version} present", data=data)


def _read_string(hive: Any, path: str, name: str) -> str:
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return ""
    try:
        with winreg.OpenKey(hive, path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return value.strip() if isinstance(value, str) else ""


def _read_string_hklm(path: str, name: str) -> str:
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return ""
    return _read_string(winreg.HKEY_LOCAL_MACHINE, path, name)


def _read_string_hkcu(path: str, name: str) -> str:
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return ""
    return _read_string(winreg.HKEY_CURRENT_USER, path, name)


def _read_dword(path: str, name: str) -> int | None:
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return int(value) if isinstance(value, int) else None
