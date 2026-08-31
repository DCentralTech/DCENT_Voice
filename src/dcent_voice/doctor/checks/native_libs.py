# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Can the native extensions actually load on this machine?

Every import happens in a child process (see :mod:`..probe`), so a missing DLL
or an illegal instruction is reported as a check result rather than taking the
diagnostic tool down with it.

Severity follows what the failure actually costs the user: ASR backends and the
audio bridge are fatal, the UI stack is a warning (hold-to-talk dictation still
works without WebView2), and having no microphone attached is a warning too —
CI runners have no audio device and that must not fail the build.
"""

from __future__ import annotations

import os
from typing import Any

from ..probe import PROBES, probe
from ..result import FAIL, PASS, WARN, CheckResult

#: probe id -> (check id, severity when the import fails, what breaks)
_SEVERITY: dict[str, tuple[str, str, str]] = {
    "ctranslate2": ("native.ctranslate2", FAIL, "Faster Whisper transcription"),
    "onnxruntime": ("native.onnxruntime", FAIL, "the default Parakeet transcription backend"),
    "sounddevice": ("native.sounddevice", FAIL, "microphone capture"),
    "pynput": ("native.pynput", FAIL, "the global push-to-talk hotkey"),
    "pystray": ("native.pystray", WARN, "the system tray icon"),
    "PIL": ("native.pillow", WARN, "tray icon rendering"),
    "webview": ("native.webview", WARN, "Settings, the overlay and the setup wizard"),
    "clr": ("native.pythonnet", WARN, "the .NET host that pywebview uses on Windows"),
    "win32gui": ("native.win32gui", WARN, "detecting the foreground application"),
    "uiautomation": ("native.uiautomation", WARN, "UI-Automation text injection"),
}

_REMEDIATION = {
    FAIL: (
        "Reinstall DCENT_Voice from the official Setup.exe. If it still fails, install the "
        "Microsoft Visual C++ 2015-2022 Redistributable (x64) and add the install folder to "
        "your antivirus exclusions."
    ),
    WARN: (
        "Dictation still works. Reinstall DCENT_Voice to restore this component; on Windows "
        "the Settings window additionally needs the Edge WebView2 runtime (see ui.webview2)."
    ),
}


def run(*, timeout_s: float = 60.0) -> list[CheckResult]:
    results: list[CheckResult] = []
    payloads: dict[str, dict[str, Any]] = {}
    # Iterate the severity map, not PROBES: probes owned by another check module
    # (``gi_webkit`` belongs to ``checks.desktop``) must not be reported twice.
    for probe_id in _SEVERITY:
        payload = probe(probe_id, timeout_s=timeout_s)
        payloads[probe_id] = payload
        results.append(_result_for(probe_id, payload))
    results.append(check_audio_inputs(payloads.get("sounddevice", {})))
    results.append(check_onnx_providers(payloads.get("onnxruntime", {})))
    return results


def _result_for(probe_id: str, payload: dict[str, Any]) -> CheckResult:
    check_id, severity, breaks = _SEVERITY[probe_id]
    label = PROBES[probe_id][1]
    if payload.get("skipped"):
        return CheckResult(
            check_id, PASS, f"not applicable on this platform ({label})", data=payload
        )
    if payload.get("ok"):
        version = payload.get("version") or "unknown version"
        return CheckResult(check_id, PASS, f"{label} imported ({version})", data=payload)
    detail = str(payload.get("detail") or "the import failed")
    return CheckResult(
        check_id,
        severity,
        f"{label} could not be imported, so {breaks} is unavailable: {detail}",
        _REMEDIATION[severity],
        payload,
    )


def check_audio_inputs(payload: dict[str, Any]) -> CheckResult:
    """No microphone is a warning, never a failure: CI runners have none."""
    if not payload.get("ok"):
        return CheckResult(
            "native.audio_input",
            WARN,
            "the audio device list is unknown because the PortAudio bridge did not import "
            "(see native.sounddevice)",
            "Fix native.sounddevice first.",
            {},
        )
    inputs = payload.get("inputDevices") or []
    data = {
        "inputDeviceCount": len(inputs),
        "inputDevices": inputs[:20],
        "defaultInput": payload.get("defaultInput"),
        "hostApis": payload.get("hostApis", []),
    }
    if not inputs:
        return CheckResult(
            "native.audio_input",
            WARN,
            "no audio input device is available on this machine, so dictation has nothing to "
            "record",
            "Plug in or enable a microphone, then check Windows Settings > Privacy & security "
            "> Microphone and allow desktop apps to access it.",
            data,
        )
    default = payload.get("defaultInput")
    named = next((item["name"] for item in inputs if item["index"] == default), None)
    suffix = f"; default input: {named}" if named else "; no default input device is selected"
    return CheckResult(
        "native.audio_input",
        PASS,
        f"{len(inputs)} audio input device(s){suffix}",
        data=data,
    )


def check_onnx_providers(payload: dict[str, Any]) -> CheckResult:
    """CPU is the shipped default; CUDA is a bonus, never a requirement."""
    if not payload.get("ok"):
        return CheckResult(
            "native.onnx_providers",
            WARN,
            "ONNX Runtime execution providers are unknown because the import failed "
            "(see native.onnxruntime)",
            "Fix native.onnxruntime first.",
            {},
        )
    providers = list(payload.get("providers") or [])
    data = {"providers": providers, "cuda": "CUDAExecutionProvider" in providers}
    if "CPUExecutionProvider" not in providers:
        return CheckResult(
            "native.onnx_providers",
            FAIL,
            "ONNX Runtime reports no CPU execution provider, so the default Parakeet model "
            f"cannot run. Providers: {providers or 'none'}",
            "Reinstall DCENT_Voice; the shipped ONNX Runtime always provides the CPU backend.",
            data,
        )
    accel = "CPU + CUDA" if data["cuda"] else "CPU"
    return CheckResult(
        "native.onnx_providers",
        PASS,
        f"ONNX Runtime execution providers available: {accel}",
        data=data,
    )


def platform_note() -> str:
    return "windows" if os.name == "nt" else os.name
