# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    fix: str = ""


def main() -> int:
    results = [
        check_python(),
        check_import("numpy", "Required by audio buffers and ASR providers."),
        check_import("fastapi", "Required for the local HTTP service.", optional=True),
        check_import("faster_whisper", "Required for local Whisper ASR.", optional=True),
        check_import("keyring", "Required for Windows Credential Manager storage.", optional=True),
        check_import("sounddevice", "Required for live microphone capture.", optional=True),
        check_import("pynput", "Required for global push-to-talk hotkeys.", optional=True),
        check_import("uvicorn", "Required for the local service thread.", optional=True),
        check_import(
            "webview",
            "Required for overlay and settings windows (pywebview).",
            optional=True,
        ),
        check_import("pystray", "Required for tray controls.", optional=True),
        check_import("win32gui", "Required for foreground-app injector overrides.", optional=True),
        check_import("uiautomation", "Required for UIA selection probing.", optional=True),
        check_hotkey_listener(),
        check_cuda_runtime(),
        check_microphone(),
        check_http("Ollama", "http://127.0.0.1:11434/api/tags"),
        check_http("LM Studio", "http://127.0.0.1:1234/v1/models"),
    ]
    print_results(results)
    return 1 if any(item.status == "FAIL" for item in results) else 0


def check_python() -> CheckResult:
    version = platform.python_version()
    # Diagnostic runs against whatever interpreter invokes it (possibly a stale
    # system Python), so this runtime version gate is intentional, not dead code.
    if sys.version_info >= (3, 11):  # noqa: UP036
        return CheckResult("Python", "OK", version)
    return CheckResult(
        "Python",
        "FAIL",
        version,
        "Install Python 3.11 or newer and recreate the virtual environment.",
    )


def check_import(module: str, detail: str, *, optional: bool = False) -> CheckResult:
    spec = importlib.util.find_spec(module)
    if spec is not None:
        return CheckResult(module, "OK", detail)
    status = "WARN" if optional else "FAIL"
    extra = "Feature unavailable until installed." if optional else "Required for the base install."
    return CheckResult(
        module,
        status,
        f"Not installed. {extra}",
        'Install project dependencies with python -m pip install -e ".[dev,cuda]" as needed.',
    )


def check_hotkey_listener() -> CheckResult:
    """Start a pynput listener briefly and confirm it reports running."""
    if importlib.util.find_spec("pynput") is None:
        return CheckResult(
            "hotkey listener",
            "WARN",
            "pynput not installed; cannot probe global hooks.",
            'Install project dependencies with python -m pip install -e ".[dev]".',
        )
    try:
        from pynput import keyboard

        listener = keyboard.Listener(on_press=lambda _k: None, on_release=lambda _k: None)
        listener.start()
        try:
            import time

            time.sleep(0.15)
            running = bool(getattr(listener, "running", False))
            alive = listener.is_alive() if hasattr(listener, "is_alive") else running
        finally:
            listener.stop()
        if running and alive:
            return CheckResult(
                "hotkey listener",
                "OK",
                "pynput Listener started and reported running.",
            )
        return CheckResult(
            "hotkey listener",
            "WARN",
            "Listener started but did not report running=True.",
            "Try running as a normal (non-elevated) user session; reboot if hooks are wedged.",
        )
    except Exception as exc:
        return CheckResult(
            "hotkey listener",
            "WARN",
            f"Could not start listener: {type(exc).__name__}: {exc}",
            "Check accessibility permissions / other global hook apps.",
        )


def check_cuda_runtime() -> CheckResult:
    if platform.system() != "Windows":
        return CheckResult("CUDA/cuDNN", "SKIP", "Windows-specific DLL check skipped.")

    path_dirs = [Path(value) for value in os.environ.get("PATH", "").split(os.pathsep) if value]
    existing_dirs = [value for value in path_dirs if value.exists()]
    cudnn8_names = {
        "cudnn64_8.dll",
        "cudnn_ops_infer64_8.dll",
        "cudnn_cnn_infer64_8.dll",
    }

    found_cudnn8: list[str] = []
    found_cuda: list[str] = []
    for directory in existing_dirs:
        for name in cudnn8_names:
            if (directory / name).exists():
                found_cudnn8.append(str(directory / name))
        found_cuda.extend(str(path) for path in directory.glob("cublas64_*.dll"))

    gpu_line = _run_nvidia_smi()
    if found_cudnn8 and found_cuda:
        detail = "cuDNN 8 and CUDA runtime DLLs found on PATH."
        if gpu_line:
            detail += f" GPU: {gpu_line}"
        return CheckResult("CUDA/cuDNN", "OK", detail)

    missing = []
    if not found_cuda:
        missing.append("CUDA runtime DLLs")
    if not found_cudnn8:
        missing.append("cuDNN 8 DLLs")
    detail = "Missing " + " and ".join(missing) + " on PATH."
    if gpu_line:
        detail += f" GPU detected: {gpu_line}"
    return CheckResult(
        "CUDA/cuDNN",
        "WARN",
        detail,
        "Install CUDA plus cuDNN 8 compatible with CTranslate2, or use CPU int8 profiles.",
    )


def _run_nvidia_smi() -> str:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""


def check_microphone() -> CheckResult:
    if importlib.util.find_spec("sounddevice") is None:
        return CheckResult(
            "Microphone",
            "WARN",
            "sounddevice is not installed, so live input devices cannot be inspected.",
            "Install sounddevice when starting the P1 audio wave.",
        )

    try:
        import sounddevice as sd  # type: ignore[import-not-found]

        devices = sd.query_devices()
        input_devices = [
            device for device in devices if int(device.get("max_input_channels", 0) or 0) > 0
        ]
    except Exception as exc:  # pragma: no cover - hardware dependent
        return CheckResult(
            "Microphone",
            "WARN",
            f"Could not query PortAudio devices: {exc}",
            "Check Windows microphone permissions and audio drivers.",
        )

    if input_devices:
        names = ", ".join(str(device["name"]) for device in input_devices[:3])
        suffix = " ..." if len(input_devices) > 3 else ""
        return CheckResult(
            "Microphone", "OK", f"{len(input_devices)} input device(s): {names}{suffix}"
        )
    return CheckResult(
        "Microphone",
        "WARN",
        "No input devices reported by PortAudio.",
        "Connect or enable a microphone, then rerun this script.",
    )


def check_http(name: str, url: str) -> CheckResult:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            detail = f"Reachable at {url} (HTTP {response.status})."
            return CheckResult(name, "OK", detail)
    except urllib.error.HTTPError as exc:
        return CheckResult(name, "WARN", f"{url} responded HTTP {exc.code}.")
    except OSError:
        return CheckResult(
            name,
            "WARN",
            f"Not reachable at {url}.",
            "Start the local service if you plan to use this provider.",
        )


def print_results(results: list[CheckResult]) -> None:
    name_width = max(len(result.name) for result in results)
    status_width = max(len(result.status) for result in results)
    print("DCENT_Voice environment check")
    print()
    for result in results:
        print(
            f"{result.name.ljust(name_width)}  {result.status.ljust(status_width)}  {result.detail}"
        )
        if result.fix:
            print(f"{'':{name_width}}  {'':{status_width}}  fix: {result.fix}")


if __name__ == "__main__":
    raise SystemExit(main())
