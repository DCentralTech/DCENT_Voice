# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Measure headless service-idle or explicitly enabled interactive-idle use.

The host-safe default disables tray, hotkeys, and overlay while keeping the
ASR service loaded for headless transcription. ``--interactive-desktop``
measures the real desktop surfaces only when the exact interactive-test opt-in
is present. Default ``idle_unload_s = 600`` drops model weights after ten idle
minutes; a short benchmark window therefore measures the keep-warm state.

``--self-test`` samples this Python process only (no desktop launch).
``--split-model`` loads the shipped default in-process and reports shell RSS,
model RSS, after-unload RSS, first-load, reload, and warm-transcribe times.
``--compare-model`` separately peak-RSS a one-shot ``transcribe hello.wav``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.attach.registry import write_text_atomic  # noqa: E402
from dcent_voice.util.owned_process import (  # noqa: E402
    start_owned_process,
    terminate_owned_process,
)


def _windows_api() -> Any:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    return SimpleNamespace(
        ctypes=ctypes,
        kernel32=kernel32,
        psapi=psapi,
        FILETIME=FILETIME,
        PROCESS_MEMORY_COUNTERS=PROCESS_MEMORY_COUNTERS,
        PROCESSENTRY32W=PROCESSENTRY32W,
    )


def _windows_children(pid: int) -> list[int]:
    api = _windows_api()
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = api.ctypes.c_void_p(-1).value
    snap = api.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID:
        return []
    try:
        entry = api.PROCESSENTRY32W()
        entry.dwSize = api.ctypes.sizeof(api.PROCESSENTRY32W)
        if not api.kernel32.Process32FirstW(snap, api.ctypes.byref(entry)):
            return []
        children: list[int] = []
        while True:
            if int(entry.th32ParentProcessID) == pid:
                children.append(int(entry.th32ProcessID))
            if not api.kernel32.Process32NextW(snap, api.ctypes.byref(entry)):
                break
        return children
    finally:
        api.kernel32.CloseHandle(snap)


def _windows_one(pid: int) -> tuple[float, int]:
    api = _windows_api()
    access = 0x0410  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    handle = api.kernel32.OpenProcess(access, False, pid)
    if not handle:
        access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        handle = api.kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise OSError(f"OpenProcess failed for pid={pid}")
    try:
        creation = api.FILETIME()
        exit_time = api.FILETIME()
        kernel = api.FILETIME()
        user = api.FILETIME()
        if not api.kernel32.GetProcessTimes(
            handle,
            api.ctypes.byref(creation),
            api.ctypes.byref(exit_time),
            api.ctypes.byref(kernel),
            api.ctypes.byref(user),
        ):
            raise OSError("GetProcessTimes failed")
        counters = api.PROCESS_MEMORY_COUNTERS()
        counters.cb = api.ctypes.sizeof(api.PROCESS_MEMORY_COUNTERS)
        if not api.psapi.GetProcessMemoryInfo(handle, api.ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        cpu_100ns = (kernel.dwHighDateTime << 32 | kernel.dwLowDateTime) + (
            user.dwHighDateTime << 32 | user.dwLowDateTime
        )
        return cpu_100ns / 10_000_000.0, int(counters.WorkingSetSize)
    finally:
        api.kernel32.CloseHandle(handle)


def _posix_sample(pid: int) -> tuple[float, int]:
    sysconf = getattr(os, "sysconf", None)
    if not callable(sysconf):
        raise RuntimeError("POSIX sysconf is unavailable")
    ticks = sysconf("SC_CLK_TCK")
    page = sysconf("SC_PAGE_SIZE")
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    cpu_s = (int(stat[13]) + int(stat[14])) / float(ticks)
    rss = int(stat[23]) * int(page)
    return cpu_s, rss


def sample_process(pid: int) -> tuple[float, int]:
    if sys.platform != "win32":
        return _posix_sample(pid)
    cpu_s, rss = _windows_one(pid)
    for child in _windows_children(pid):
        try:
            child_cpu, child_rss = _windows_one(child)
        except OSError:
            continue
        cpu_s += child_cpu
        rss += child_rss
    return cpu_s, rss


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "max": 0.0, "min": 0.0}
    return {
        "mean": float(statistics.fmean(values)),
        "max": float(max(values)),
        "min": float(min(values)),
    }


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def collect_samples(pid: int, *, seconds: float, interval_s: float) -> list[dict[str, float]]:
    deadline = time.monotonic() + seconds
    prev_cpu, prev_t = sample_process(pid)[0], time.monotonic()
    rows: list[dict[str, float]] = []
    while time.monotonic() < deadline:
        time.sleep(interval_s)
        cpu_s, rss = sample_process(pid)
        now = time.monotonic()
        elapsed = max(1e-6, now - prev_t)
        cpu_pct = max(0.0, (cpu_s - prev_cpu) / elapsed * 100.0)
        rows.append({"t_s": round(now, 3), "cpu_percent": cpu_pct, "rss_bytes": float(rss)})
        prev_cpu, prev_t = cpu_s, now
    return rows


def _write_idle_config(path: Path, *, port: int) -> None:
    source = ROOT / "config.example.toml"
    text = source.read_text(encoding="utf-8")
    text = text.replace(
        "first_run_education_shown = false",
        "first_run_education_shown = true",
        1,
    )
    text = text.replace("port = 8765", f"port = {port}", 1)
    path.write_text(text, encoding="utf-8")


def _kill_tree(process: subprocess.Popen[Any]) -> None:
    terminate_owned_process(process, grace_s=1.0, kill_s=5.0)


def _launch_idle(
    *,
    executable: Path | None,
    config_path: Path,
    mutex: str,
    interactive_desktop: bool,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    if sys.platform == "win32":
        env["DCENT_VOICE_SMOKE_MUTEX"] = mutex
    if executable is not None:
        command = [str(executable), "--config", str(config_path)]
    else:
        command = [
            sys.executable,
            "-m",
            "dcent_voice",
            "--config",
            str(config_path),
        ]
    if not interactive_desktop:
        command.extend(("--no-tray", "--no-hotkeys", "--no-overlay"))
    log_path = config_path.with_name("idle-launch.log")
    log_file = log_path.open("wb")
    return start_owned_process(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=log_file,
    )


def _transcribe_peak_rss(executable: Path | None) -> int | None:
    wav = ROOT / "tests" / "fixtures" / "audio" / "hello.wav"
    if not wav.is_file():
        return None
    if executable is not None:
        command = [str(executable), "transcribe", str(wav), "--no-polish"]
    else:
        command = [sys.executable, "-m", "dcent_voice", "transcribe", str(wav), "--no-polish"]
    process = start_owned_process(
        command,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    peak = 0
    try:
        while process.poll() is None:
            try:
                _cpu, rss = sample_process(process.pid)
            except OSError:
                break
            peak = max(peak, rss)
            time.sleep(0.1)
        process.wait(timeout=120)
    finally:
        if process.poll() is None:
            _kill_tree(process)
        else:
            # Reap any model/runtime descendants that outlived the CLI root.
            terminate_owned_process(process, grace_s=0.0, kill_s=5.0)
    return peak or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure DCENT_Voice idle resources.")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--settle-s", type=float, default=3.0)
    parser.add_argument("--interval-s", type=float, default=0.5)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--executable", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--interactive-desktop",
        action="store_true",
        help=(
            "Measure the real tray/hotkey desktop. Requires explicit "
            "DCENT_VOICE_ALLOW_INTERACTIVE_TESTS=1."
        ),
    )
    parser.add_argument(
        "--compare-model",
        action="store_true",
        help="Also peak-RSS a one-shot transcribe (CLI+model, not idle tray).",
    )
    parser.add_argument(
        "--split-model",
        action="store_true",
        help="In-process load of the shipped default; report model RSS delta.",
    )
    return parser


def _split_model_rss() -> dict[str, float]:
    from dcent_voice.app import build_asr_provider
    from dcent_voice.config import load_config
    from dcent_voice.engine import load_wav_mono

    config = load_config(ROOT / "config.example.toml", create=False)
    asr = build_asr_provider(config)
    asr.idle_unload_s = 0
    before = sample_process(os.getpid())[1]
    load_started = time.perf_counter()
    asr.load()
    first_load_s = time.perf_counter() - load_started
    after = sample_process(os.getpid())[1]
    wav = ROOT / "tests" / "fixtures" / "audio" / "hello.wav"
    warm_s = 0.0
    if wav.is_file():
        audio, samplerate = load_wav_mono(wav)
        asr.transcribe(audio, samplerate=samplerate)
        started = time.perf_counter()
        asr.transcribe(audio, samplerate=samplerate)
        warm_s = time.perf_counter() - started
    asr.unload()
    time.sleep(0.4)
    unloaded = sample_process(os.getpid())[1]
    reload_started = time.perf_counter()
    asr.load()
    reload_s = time.perf_counter() - reload_started
    asr.unload()
    return {
        "shell_rss_mb": before / (1024 * 1024),
        "shell_plus_model_rss_mb": after / (1024 * 1024),
        "after_unload_rss_mb": unloaded / (1024 * 1024),
        "model_rss_mb": max(0.0, (after - before) / (1024 * 1024)),
        "first_load_s": first_load_s,
        "reload_s": reload_s,
        "warm_transcribe_s": warm_s,
        "default_idle_unload_s": 600.0,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interactive_desktop and os.environ.get("DCENT_VOICE_ALLOW_INTERACTIVE_TESTS") != "1":
        print(
            "--interactive-desktop requires explicit DCENT_VOICE_ALLOW_INTERACTIVE_TESTS=1",
            file=sys.stderr,
        )
        return 2
    if args.self_test:
        samples = collect_samples(os.getpid(), seconds=min(args.seconds, 0.6), interval_s=0.15)
        self_test_report = {
            "scope": "self-test",
            "model_resident_at_idle": False,
            "ready_to_dictate": True,
            "ready_to_dictate_note": (
                "Self-test does not launch the tray. Desktop run_app loads ASR at startup."
            ),
            "shell_rss_mb": _summary([row["rss_bytes"] / (1024 * 1024) for row in samples]),
            "model_rss_mb": 0.0,
            "cpu_percent": _summary([row["cpu_percent"] for row in samples]),
            "samples": samples,
        }
        text = json.dumps(self_test_report, indent=2)
        print(text)
        if args.output is not None:
            write_text_atomic(args.output, text + "\n", require_private=False)
        return 0

    port = 18765
    mutex = r"Local\DCENT_Voice_Smoke_idle"
    executable = args.executable
    executable_identity: tuple[int, str] | None = None
    if executable is not None:
        executable = executable.resolve()
        if not executable.is_file():
            print(f"executable not found: {executable}", file=sys.stderr)
            return 2
        executable_identity = _file_identity(executable)

    tmp = Path(tempfile.mkdtemp(prefix="dcent-idle-"))
    process: subprocess.Popen[bytes] | None = None
    try:
        config_path = tmp / "config.toml"
        _write_idle_config(config_path, port=port)
        process = _launch_idle(
            executable=executable,
            config_path=config_path,
            mutex=mutex,
            interactive_desktop=args.interactive_desktop,
        )
        time.sleep(max(0.0, args.settle_s))
        if process.poll() is not None:
            log_text = (tmp / "idle-launch.log").read_text(encoding="utf-8", errors="replace")
            print(f"idle process exited early code={process.returncode}", file=sys.stderr)
            if log_text.strip():
                print(log_text, file=sys.stderr)
            return 1
        samples = collect_samples(process.pid, seconds=args.seconds, interval_s=args.interval_s)
        report_pid = process.pid
    finally:
        try:
            if process is not None:
                # Always close the private Job Object/process session, including
                # early-exit, sampling, and report-read failure paths.
                _kill_tree(process)
                process.wait(timeout=10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if executable is not None and _file_identity(executable) != executable_identity:
        raise RuntimeError("measured executable changed during the idle benchmark")

    rss_mb = [row["rss_bytes"] / (1024 * 1024) for row in samples]
    report: dict[str, Any] = {
        "scope": (
            "idle_tray_no_dictation"
            if args.interactive_desktop
            else "idle_headless_service_no_dictation"
        ),
        "interactive_desktop": bool(args.interactive_desktop),
        "desktop_surfaces_disabled": not args.interactive_desktop,
        "seconds": args.seconds,
        "settle_s": args.settle_s,
        "pid": report_pid,
        "executable": str(executable) if executable else "source",
        "executable_bytes": executable_identity[0] if executable_identity else None,
        "executable_sha256": executable_identity[1] if executable_identity else None,
        "model_resident_at_idle": True,
        "ready_to_dictate": bool(args.interactive_desktop),
        "ready_to_transcribe": True,
        "ready_to_dictate_note": (
            "Interactive desktop surfaces are disabled in this host-safe run; "
            "the loaded local service is ready for headless transcription."
            if not args.interactive_desktop
            else "run_app calls asr.load() at startup so first hold is ready. "
            "Default idle_unload_s=600 then unloads; overlay/tray show Loading "
            "on the next hold. This short bench stays keep-warm."
        ),
        "idle_rss_mb": _summary(rss_mb),
        "shell_rss_mb": _summary(rss_mb),
        "model_rss_mb": None,
        "cpu_percent": _summary([row["cpu_percent"] for row in samples]),
        "cpu_percent_steady": _summary([row["cpu_percent"] for row in samples[1:]]),
        "wakeup_note": (
            "Main thread blocks on wait_any(shutdown, Settings, overlay start). "
            "No 4 Hz lazy-overlay poll and no 10 Hz GUI-or-shutdown poll."
        ),
        "samples": samples,
    }
    if args.split_model:
        model_split = _split_model_rss()
        report["model_split"] = model_split
        report["model_rss_mb"] = model_split["model_rss_mb"]
        report["after_unload_rss_mb"] = model_split["after_unload_rss_mb"]
        report["shell_rss_mb"] = {
            "mean": model_split["shell_rss_mb"],
            "max": model_split["shell_rss_mb"],
            "min": model_split["shell_rss_mb"],
        }
    if args.compare_model:
        peak = _transcribe_peak_rss(executable)
        if peak is not None:
            report["transcribe_peak_rss_mb"] = peak / (1024 * 1024)
    text = json.dumps(report, indent=2)
    print(text)
    if args.output is not None:
        write_text_atomic(args.output, text + "\n", require_private=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
