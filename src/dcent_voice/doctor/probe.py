# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Import native extensions in a child process so a hard crash is reportable.

``import onnxruntime`` (or ctranslate2, or pythonnet) can abort the process
outright when a DLL is missing or the CPU lacks an instruction set. If doctor
imported those in-process, the very failure the user needs described would kill
the tool that describes it. Every native probe therefore runs in a subprocess:
a segfault becomes "exit code -1073741795 (illegal instruction)" in the report
instead of a vanished window.

The child side is reachable two ways so it works in both packaging layouts:

* frozen  — ``dcent-voice.exe doctor-probe <id>`` (a hidden subcommand; the
  frozen ``sys.executable`` *is* the app, so ``-c`` is not available)
* source  — ``python -c "...emit('<id>')"`` with the package's parent on
  ``sys.path``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Sentinel around the child's JSON so a chatty native import (banners, warnings
#: written straight to fd 1) cannot corrupt the payload.
_BEGIN = "<<<DCENT_VOICE_PROBE_BEGIN>>>"
_END = "<<<DCENT_VOICE_PROBE_END>>>"

#: WebKit2/Soup ABI pairs pywebview's GTK backend accepts, newest first. Ubuntu
#: 24.04 ships 4.1/3.0; Jammy (22.04) only has 4.0/2.4. Shared with
#: ``dcent_voice.app._require_linux_webkit`` so the runtime and the diagnostic
#: can never disagree about what "WebKitGTK is available" means.
LINUX_WEBKIT_ABIS: tuple[tuple[str, str], ...] = (("4.1", "3.0"), ("4.0", "2.4"))

#: probe id -> (import name, human label, required-on-platforms)
PROBES: dict[str, tuple[str, str]] = {
    "ctranslate2": ("ctranslate2", "CTranslate2 (Faster Whisper backend)"),
    "onnxruntime": ("onnxruntime", "ONNX Runtime (Parakeet backend)"),
    "sounddevice": ("sounddevice", "PortAudio bridge (microphone capture)"),
    "pynput": ("pynput", "global hotkey listener"),
    "pystray": ("pystray", "system tray icon"),
    "PIL": ("PIL.Image", "Pillow (tray icon rendering)"),
    "webview": ("webview", "pywebview (Settings, overlay, wizard)"),
    "clr": ("clr", "pythonnet (.NET host for pywebview on Windows)"),
    "win32gui": ("win32gui", "pywin32 (foreground window detection)"),
    "uiautomation": ("uiautomation", "UI Automation (text injection targets)"),
    "gi_webkit": ("gi", "WebKitGTK via PyGObject (Settings, overlay, wizard on Linux)"),
}

#: Probes that only exist on Windows; elsewhere they are reported as skipped.
WINDOWS_ONLY = frozenset({"clr", "win32gui", "uiautomation"})

#: Probes that only exist on Linux; elsewhere they are reported as skipped.
#: ``gi_webkit`` is owned by :mod:`..checks.desktop`, not by ``native_libs``.
LINUX_ONLY = frozenset({"gi_webkit"})


def emit(probe_id: str) -> int:
    """Child side: import one module and report a JSON verdict. Never raises.

    The exit code carries the verdict too (0 imported, 1 did not). The parent
    normally reads the JSON from a pipe, but the windowed frozen build has no
    stdout at all when it is launched detached, and a diagnostic tool that
    crashes on ``None.write`` would be the joke that writes itself.
    """
    payload = _import_probe(probe_id)
    _write_verdict(_BEGIN + json.dumps(payload) + _END + "\n")
    return 0 if payload.get("ok") else 1


def _write_verdict(line: str) -> None:
    """Write to stdout when there is one; a missing or closed stream is not fatal."""
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        stream.write(line)
        stream.flush()
    except (OSError, ValueError, AttributeError):
        return


def run_probe_command(probe_id: str) -> int:
    """``dcent-voice doctor-probe <id>`` entry point."""
    return emit(probe_id)


def probe(probe_id: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """Parent side: run one probe in a child process and return its verdict.

    The returned mapping always has ``ok`` (bool) and ``detail`` (str); a crash
    or timeout is a normal, reported outcome rather than an exception.
    """
    if probe_id not in PROBES:
        raise KeyError(f"unknown probe: {probe_id!r}")
    if probe_id in WINDOWS_ONLY and os.name != "nt":
        return {"ok": True, "skipped": True, "detail": "not applicable on this platform"}
    if probe_id in LINUX_ONLY and not sys.platform.startswith("linux"):
        return {"ok": True, "skipped": True, "detail": "not applicable on this platform"}

    argv = _child_argv(probe_id)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_child_env(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "detail": f"the import did not finish within {timeout_s:.0f} s (it hung)",
            "timeout": True,
        }
    except OSError as exc:
        return {"ok": False, "detail": f"could not start the probe process: {exc}"}

    parsed = _extract(completed.stdout)
    if parsed is not None:
        parsed.setdefault("ok", False)
        return parsed
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return {
        "ok": False,
        "detail": (
            f"the probe process exited {completed.returncode} without a verdict"
            + (f": {tail[-1][:300]}" if tail else " (the import crashed the process)")
        ),
        "returncode": completed.returncode,
        "crashed": True,
    }


def _child_argv(probe_id: str) -> list[str]:
    executable = sys.executable or ""
    if getattr(sys, "frozen", False):
        # sys.executable is dcent-voice.exe; re-enter through the hidden subcommand.
        return [executable, "doctor-probe", probe_id]
    # The directory that holds the ``dcent_voice`` package, derived from the
    # imported package itself rather than guessed by counting path segments.
    import dcent_voice

    package_parent = str(Path(dcent_voice.__file__).resolve().parent.parent)
    code = (
        f"import sys;sys.path.insert(0, {package_parent!r});"
        f"from dcent_voice.doctor.probe import emit;raise SystemExit(emit({probe_id!r}))"
    )
    return [executable, "-c", code]


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # A probe must never be the thing that reaches the network.
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    env.setdefault("DO_NOT_TRACK", "1")
    return env


def _extract(stdout: str) -> dict[str, Any] | None:
    start = stdout.find(_BEGIN)
    end = stdout.find(_END, start + 1)
    if start < 0 or end < 0:
        return None
    try:
        payload = json.loads(stdout[start + len(_BEGIN) : end])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _import_probe(probe_id: str) -> dict[str, Any]:
    entry = PROBES.get(probe_id)
    if entry is None:
        return {"ok": False, "detail": f"unknown probe: {probe_id}"}
    module_name, label = entry
    try:
        module = __import__(module_name, fromlist=["__version__"])
    except BaseException as exc:  # noqa: BLE001 - a native import can raise anything
        return {
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}".strip()[:600],
            "label": label,
        }
    payload: dict[str, Any] = {
        "ok": True,
        "label": label,
        "version": str(getattr(module, "__version__", "") or ""),
        "file": str(getattr(module, "__file__", "") or ""),
        "detail": "imported",
    }
    extra = _EXTRAS.get(probe_id)
    if extra is not None:
        try:
            payload.update(extra(module))
        except BaseException as exc:  # noqa: BLE001 - best-effort enrichment only
            payload["extraError"] = f"{type(exc).__name__}: {exc}"[:300]
    return payload


def _onnxruntime_extra(module: Any) -> dict[str, Any]:
    return {"providers": list(module.get_available_providers())}


def _sounddevice_extra(module: Any) -> dict[str, Any]:
    devices: list[dict[str, Any]] = [
        {
            "index": index,
            "name": str(device.get("name", "")),
            "inputChannels": int(device.get("max_input_channels", 0)),
        }
        for index, device in enumerate(module.query_devices())
    ]
    inputs = [device for device in devices if device["inputChannels"] > 0]
    try:
        default_input = module.default.device[0]
    except Exception:  # noqa: BLE001 - no default device is a normal state
        default_input = None
    return {
        "inputDevices": inputs,
        "defaultInput": None if default_input in (None, -1) else int(default_input),
        "hostApis": [str(api["name"]) for api in module.query_hostapis()],
    }


def _gi_webkit_extra(module: Any) -> dict[str, Any]:
    """Resolve the WebKit2/Soup ABI pywebview's GTK backend would actually use.

    Mirrors ``dcent_voice.app._require_linux_webkit`` and then really imports
    the typelibs: ``gi`` alone installs fine from a wheel on a host that has no
    ``gir1.2-webkit2-4.1`` package at all, so importing ``gi`` proves nothing.
    """
    errors: list[str] = []
    for webkit_version, soup_version in LINUX_WEBKIT_ABIS:
        try:
            module.require_version("WebKit2", webkit_version)
            module.require_version("Soup", soup_version)
            module.require_version("Gtk", "3.0")
            from gi.repository import Gtk, Soup, WebKit2  # noqa: PLC0415

            _ = (Gtk, Soup, WebKit2)
        except BaseException as exc:  # noqa: BLE001 - a typelib import can raise anything
            errors.append(f"WebKit2 {webkit_version}/Soup {soup_version}: {exc}"[:300])
            continue
        return {"webkit": webkit_version, "soup": soup_version, "gtk": "3.0"}
    return {"webkitError": "; ".join(errors)[:600] or "no WebKit2 typelib was usable"}


_EXTRAS = {
    "onnxruntime": _onnxruntime_extra,
    "sounddevice": _sounddevice_extra,
    "gi_webkit": _gi_webkit_extra,
}


if __name__ == "__main__":  # pragma: no cover - exercised through subprocesses
    raise SystemExit(emit(sys.argv[1] if len(sys.argv) > 1 else ""))
