# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Real Windows hold/speak/release probe against isolated scratch applications.

The fixture is played through a caller-selected OS output endpoint and captured by
the configured sounddevice input endpoint. Audio is never passed directly to ASR.
Targets are fresh owned scratch windows only: Notepad, VS Code, a ReadKey console,
Edge, and Chrome. Existing user documents and profiles are never opened.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import tempfile
import threading
import time
import traceback
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult
from dcent_voice.audio.capture import AudioCapture, resample_linear
from dcent_voice.config import AppConfig
from dcent_voice.engine import load_wav_mono
from dcent_voice.eval_corpus import char_error_rate, word_error_rate
from dcent_voice.events import (
    AppMode,
    EventBus,
    HotkeyPressed,
    HotkeyReleased,
    TranscriptReady,
)
from dcent_voice.hotkeys import HotkeyManager
from dcent_voice.inject.base import Injector
from dcent_voice.inject.clipboard import restore_clipboard, snapshot_clipboard
from dcent_voice.inject.router import InjectionRouteDecision, RoutingInjector
from dcent_voice.inject.windows_focus import read_targeted_edit_state
from dcent_voice.personalization import PersonalizationStore
from dcent_voice.pipeline import PipelineWorker
from dcent_voice.util import paths


def _require_source_checkout() -> Path:
    """Fail loudly when this development harness is reached from a frozen build.

    The probe fixtures live under ``tests/`` and ``eval/``, which are never part
    of the shipped payload; without this guard a packaged build would silently
    resolve fixture paths that cannot exist.
    """
    if paths.is_frozen():
        raise RuntimeError(
            "The Windows hold/release probe is a development-only harness that needs the "
            "repository tests/ fixtures; it is not part of the packaged DCENT_Voice "
            "application. Run it from a source checkout instead."
        )
    return paths.source_root()


_REPO_ROOT = _require_source_checkout()


_VK_CONTROL = 0x11
_VK_LWIN = 0x5B
_KEYEVENTF_KEYUP = 0x0002


class _ObservedRouter(Injector):
    """Transparent observer around the exact production RoutingInjector."""

    def __init__(self, router: RoutingInjector) -> None:
        self.router = router
        self.decisions: list[InjectionRouteDecision] = []
        self.allowed_process_id: int | None = None

    def inject(self, text: str) -> None:
        self.router.inject(text)

    def inject_into_target(self, text: str, target: object) -> None:
        target_pid = int(getattr(target, "process_id", 0) or 0)
        if self.allowed_process_id is not None and target_pid != self.allowed_process_id:
            raise RuntimeError(
                "owned-target guard refused injection: "
                f"expected pid={self.allowed_process_id}, captured pid={target_pid}"
            )
        self.decisions.append(self.router.inject_into_target_with_decision(text, target))

    def retract(self, char_count: int) -> None:
        self.router.retract(char_count)

    def press_enter(self) -> None:
        self.router.press_enter()

    def press_enter_into_target(self, target: object) -> None:
        self.router.press_enter_into_target(target)


def _percentiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "minimum": round(ordered[0], 3),
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "p99": round(percentile(0.99), 3),
        "maximum": round(ordered[-1], 3),
    }


def _set_key(key: int, *, down: bool) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(key, 0, 0 if down else _KEYEVENTF_KEYUP, 0)


def _chord_down() -> None:
    _set_key(_VK_CONTROL, down=True)
    _set_key(_VK_LWIN, down=True)


def _chord_up() -> None:
    _set_key(_VK_LWIN, down=False)
    _set_key(_VK_CONTROL, down=False)


def _focus_window_handle(target: object) -> int:
    focus_target: Any = target
    return int(focus_target.focus_hwnd)


def _wait(predicate: Any, *, timeout_s: float, stage: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out at {stage} after {timeout_s:.1f}s")


def _play_fixture(audio: np.ndarray, samplerate: int, output_device: int | str) -> dict[str, Any]:
    import sounddevice as sd

    output_rate = 48000
    samples = resample_linear(audio, samplerate, output_rate).astype(np.float32)
    cursor = 0
    done = threading.Event()
    statuses: list[str] = []

    def callback(outdata: Any, frames: int, _time_info: Any, status: Any) -> None:
        nonlocal cursor
        if status:
            statuses.append(str(status))
        remaining = len(samples) - cursor
        count = min(frames, max(0, remaining))
        outdata.fill(0)
        if count:
            outdata[:count, 0] = samples[cursor : cursor + count]
            if outdata.shape[1] > 1:
                outdata[:count, 1:] = samples[cursor : cursor + count, None]
            cursor += count
        if cursor >= len(samples):
            raise sd.CallbackStop

    stream: Any | None = None
    started = time.perf_counter()
    try:
        stream = sd.OutputStream(
            samplerate=output_rate,
            channels=2,
            dtype="float32",
            blocksize=0,
            device=output_device,
            callback=callback,
            finished_callback=done.set,
        )
        stream.start()
        if not done.wait(len(samples) / output_rate + 5.0):
            raise TimeoutError("OS fixture playback did not finish")
    finally:
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
    return {
        "output_samplerate": output_rate,
        "fixture_frames": len(samples),
        "fixture_duration_s": round(len(samples) / output_rate, 6),
        "playback_wall_s": round(time.perf_counter() - started, 6),
        "portaudio_statuses": statuses,
    }


def _device_info(device: int | str | None, kind: str) -> dict[str, Any]:
    import sounddevice as sd

    info = dict(sd.query_devices(device, kind=kind))
    hostapis = sd.query_hostapis()
    hostapi_index = int(info.get("hostapi", -1))
    return {
        "requested": device,
        "name": str(info.get("name", "")),
        "hostapi": (
            str(hostapis[hostapi_index].get("name", ""))
            if 0 <= hostapi_index < len(hostapis)
            else "unknown"
        ),
        "reported_default_samplerate": float(info.get("default_samplerate", 0.0)),
        "max_input_channels": int(info.get("max_input_channels", 0)),
        "max_output_channels": int(info.get("max_output_channels", 0)),
    }


def _process_metrics() -> dict[str, float | int]:
    """Read this process's CPU and working-set metrics without optional psutil."""

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(MemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    created, exited, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    memory = MemoryCounters()
    memory.cb = ctypes.sizeof(memory)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
        raise ctypes.WinError(ctypes.get_last_error())

    def seconds(value: FileTime) -> float:
        return ((int(value.high) << 32) | int(value.low)) / 10_000_000.0

    return {
        "cpu_seconds": seconds(kernel) + seconds(user),
        "rss_bytes": int(memory.working_set_size),
        "peak_rss_bytes": int(memory.peak_working_set_size),
    }


def _run_adversarial_scenarios(
    *,
    config: AppConfig,
    app: Any,
    asr: Any,
    router: RoutingInjector,
    fixture: np.ndarray,
    fixture_rate: int,
    output_device: int | str,
    root: Path,
) -> dict[str, Any]:
    """Exercise stuck, cancel, and device-error paths through real hotkey edges."""

    from dcent_voice.app import build_pipeline_config
    from dcent_voice.inject import windows_apps_test as apps

    results: dict[str, Any] = {}

    def prepare(name: str) -> tuple[str, object]:
        baseline = f"FAULT-{name}-{os.urandom(8).hex()}"
        records: list[dict[str, Any]] = []
        target = apps._native_edit_control_transaction(
            app,
            expected=baseline,
            select_all_after=True,
            previous_sentinel=None,
            stage=f"hold_release.adversarial.{name}.prepare",
            records=records,
        )
        return baseline, target

    def runtime(
        name: str,
        capture: AudioCapture,
        *,
        stuck_timeout_s: float,
        watchdog_interval_s: float = 2.0,
    ) -> tuple[EventBus, PipelineWorker, HotkeyManager, _ObservedRouter, list[object]]:
        scenario_bus = EventBus(name=f"HoldRelease{name}Bus")
        events: list[object] = []
        scenario_bus.subscribe(events.append)
        observed = _ObservedRouter(router)
        observed.allowed_process_id = int(app.window.pid)
        personalization = PersonalizationStore(
            root / f"personalization-{name}.json",
            enabled=config.personalization.enabled,
            learn=config.personalization.learn,
        )
        scenario_pipeline = PipelineWorker(
            bus=scenario_bus,
            capture=capture,
            asr=asr,
            injector=observed,
            config=build_pipeline_config(
                config,
                cleanup_enabled=False,
                personalization=personalization,
            ),
        )
        scenario_manager = HotkeyManager(
            config.hotkeys,
            scenario_bus,
            stuck_timeout_s=stuck_timeout_s,
            watchdog_interval_s=watchdog_interval_s,
            watchdog_idle_interval_s=watchdog_interval_s,
        )
        scenario_bus.start()
        scenario_pipeline.start()
        scenario_manager.start()
        _wait(
            lambda: scenario_manager.status().listener_running,
            timeout_s=4.0,
            stage=f"{name}.listener_ready",
        )
        return scenario_bus, scenario_pipeline, scenario_manager, observed, events

    # A held physical chord is finalized by the real HotkeyManager watchdog.
    baseline, target = prepare("stuck")
    capture = AudioCapture(device=config.audio.input_device, max_seconds=config.audio.max_seconds)
    stuck_s = len(fixture) / fixture_rate + 0.9
    resources = runtime(
        "stuck",
        capture,
        stuck_timeout_s=stuck_s,
        watchdog_interval_s=0.05,
    )
    scenario_bus, scenario_pipeline, scenario_manager, observed, events = resources
    try:
        _chord_down()
        _wait(
            lambda: any(isinstance(item, HotkeyPressed) for item in events),
            timeout_s=3.0,
            stage="stuck.hotkey_pressed",
        )
        _wait(
            lambda: bool(capture.status_snapshot()["open"]),
            timeout_s=4.0,
            stage="stuck.capture_open",
        )
        playback = _play_fixture(fixture, fixture_rate, output_device)
        _wait(
            lambda: any(isinstance(item, TranscriptReady) for item in events),
            timeout_s=45.0,
            stage="stuck.watchdog_transcript",
        )
        transcript = next(item for item in events if isinstance(item, TranscriptReady))
        observed_text = read_targeted_edit_state(target).text
        release_count = sum(isinstance(item, HotkeyReleased) for item in events)
        results["stuck_watchdog"] = {
            "success": (
                release_count == 1
                and transcript.injected
                and not transcript.discarded
                and observed_text == transcript.cleaned
                and baseline not in observed_text
                and len(observed.decisions) == 1
            ),
            "physical_key_up_before_finalize": False,
            "release_origin": "HotkeyManager_stuck_watchdog",
            "stuck_timeout_s": round(stuck_s, 6),
            "release_event_count": release_count,
            "transcript_event_count": sum(isinstance(item, TranscriptReady) for item in events),
            "baseline_absent": baseline not in observed_text,
            "text": transcript.cleaned,
            "route": asdict(observed.decisions[0]) if observed.decisions else None,
            "playback": playback,
        }
    finally:
        _chord_up()
        scenario_manager.stop(finalize_active=False)
        scenario_pipeline.stop()
        scenario_bus.stop()

    # Cancellation abandons a live hold and must never invoke ASR/injection.
    baseline, target = prepare("cancel")
    capture = AudioCapture(device=config.audio.input_device, max_seconds=config.audio.max_seconds)
    resources = runtime("cancel", capture, stuck_timeout_s=65.0)
    scenario_bus, scenario_pipeline, scenario_manager, observed, events = resources
    try:
        _chord_down()
        _wait(
            lambda: any(isinstance(item, HotkeyPressed) for item in events),
            timeout_s=3.0,
            stage="cancel.hotkey_pressed",
        )
        _wait(
            lambda: bool(capture.status_snapshot()["open"]),
            timeout_s=4.0,
            stage="cancel.capture_open",
        )
        scenario_manager.stop(finalize_active=False)
        scenario_pipeline.stop()
        _chord_up()
        observed_text = read_targeted_edit_state(target).text
        results["cancel_active_hold"] = {
            "success": (
                observed_text == baseline
                and not any(isinstance(item, TranscriptReady) for item in events)
                and not observed.decisions
            ),
            "baseline_unchanged": observed_text == baseline,
            "transcript_event_count": sum(isinstance(item, TranscriptReady) for item in events),
            "injection_count": len(observed.decisions),
            "manager_finalize_active": False,
        }
    finally:
        _chord_up()
        scenario_manager.stop(finalize_active=False)
        scenario_pipeline.stop()
        scenario_bus.stop()

    # An invalid explicit input must recover to idle without a transcript.
    baseline, target = prepare("device_error")
    capture = AudioCapture(device=2_000_000, max_seconds=config.audio.max_seconds)
    resources = runtime("device_error", capture, stuck_timeout_s=65.0)
    scenario_bus, scenario_pipeline, scenario_manager, observed, events = resources
    try:
        _chord_down()
        _wait(
            lambda: any(isinstance(item, HotkeyPressed) for item in events),
            timeout_s=3.0,
            stage="device_error.hotkey_pressed",
        )
        _wait(
            lambda: capture.status_snapshot()["last_error"] is not None,
            timeout_s=4.0,
            stage="device_error.diagnostic",
        )
        _chord_up()
        time.sleep(0.25)
        observed_text = read_targeted_edit_state(target).text
        results["device_error"] = {
            "success": (
                observed_text == baseline
                and not any(isinstance(item, TranscriptReady) for item in events)
                and not observed.decisions
                and scenario_pipeline.status_snapshot()["alive"]
            ),
            "baseline_unchanged": observed_text == baseline,
            "capture": capture.status_snapshot(),
            "pipeline": scenario_pipeline.status_snapshot(),
            "transcript_event_count": sum(isinstance(item, TranscriptReady) for item in events),
            "injection_count": len(observed.decisions),
        }
    finally:
        _chord_up()
        scenario_manager.stop(finalize_active=False)
        scenario_pipeline.stop()
        scenario_bus.stop()

    results["success"] = all(
        bool(value.get("success")) for key, value in results.items() if key != "success"
    )
    return results


def _parse_hold_apps(raw: str) -> list[str]:
    from dcent_voice.inject.windows_apps_test import (
        BROWSER_UIA_TARGETS,
        HOLD_RELEASE_APP_NAMES,
        LIVE_BROWSER_TARGETS,
    )

    requested = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not requested or requested == ["all"]:
        return list(HOLD_RELEASE_APP_NAMES)
    allowed = set(HOLD_RELEASE_APP_NAMES) | set(BROWSER_UIA_TARGETS) | set(LIVE_BROWSER_TARGETS)
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise ValueError(f"unknown hold-release apps: {', '.join(unknown)}")
    return requested


def run_hold_release_probe(
    config: AppConfig,
    *,
    audio_path: Path,
    reference: str,
    output_device: int | str,
    runs: int,
    apps: str = "all",
    allow_default_microphone: bool = False,
    real_documents: bool = False,
) -> dict[str, Any]:
    if platform.system() != "Windows":
        raise RuntimeError("hold-release OS capture probe requires Windows")
    if config.audio.input_device is None and not allow_default_microphone:
        raise RuntimeError(
            "probe refuses the user's default microphone; pass "
            "--allow-default-microphone or an explicit OS loop input_device"
        )
    if runs < 1 or runs > 100:
        raise ValueError("runs must be between 1 and 100")
    if config.hotkeys.mode != "hold" or config.hotkeys.dictation.lower() != "ctrl+win":
        raise RuntimeError("probe requires the shipped hold ctrl+win dictation chord")
    app_names = _parse_hold_apps(apps)
    if real_documents:
        from dcent_voice.inject.windows_apps_test import (
            BROWSER_UIA_TARGETS,
            LIVE_BROWSER_TARGETS,
        )

        allowed = {
            "notepad",
            "vscode",
            "edge",
            "chrome",
            *BROWSER_UIA_TARGETS,
            *LIVE_BROWSER_TARGETS,
        }
        unknown = [name for name in app_names if name not in allowed]
        if unknown:
            raise ValueError(
                "real-document probe supports notepad,vscode,edge,chrome, "
                "UIA field targets, and live browser tabs; "
                f"got {', '.join(unknown)}"
            )
        if apps == "all":
            app_names = ["notepad", "vscode", "edge", "chrome"]

    from dcent_voice.app import build_asr_provider, build_injector, build_pipeline_config
    from dcent_voice.inject import windows_apps_test as apps_mod

    fixture, fixture_rate = load_wav_mono(audio_path)
    fixture_rms = float(np.sqrt(np.mean(np.square(fixture), dtype=np.float64)))
    if len(fixture) / fixture_rate < 0.3 or fixture_rms < 0.005:
        raise RuntimeError("real speech fixture is too short or silent")

    input_info = _device_info(config.audio.input_device, "input")
    output_info = _device_info(output_device, "output")
    using_default_mic = config.audio.input_device is None
    report: dict[str, Any] = {
        "schema": "dcent-hold-release-os-capture-v1",
        "scope": (
            "system_default_microphone_acoustic_capture"
            if using_default_mic
            else "real_os_loop_capture_explicit_input_override_not_default_device"
        ),
        "target_scope": (
            "existing_user_documents" if real_documents else "fresh_owned_isolated_real_apps"
        ),
        "apps_requested": app_names,
        "pipeline": (
            "real_global_hotkey->sounddevice_input->shipped_local_asr->default_postprocess"
            "->production_router->app_native_readback"
        ),
        "audio_bypass": False,
        "injection_bypass": False,
        "fixture": {
            "path": str(audio_path.resolve()),
            "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
            "samplerate": fixture_rate,
            "duration_s": round(len(fixture) / fixture_rate, 6),
            "rms": round(fixture_rms, 8),
            "reference": reference,
        },
        "input_device": input_info,
        "output_device": output_info,
        "config": {
            "source": str(config.source_path or ""),
            "profile": config.active_profile,
            "asr": config.current_profile.asr.raw,
            "language_mode": config.language_mode,
            "language": config.current_profile.language,
            "cleanup_enabled": config.current_profile.cleanup_enabled,
            "dictation": asdict(config.dictation),
            "injector": asdict(config.injector),
            "hotkeys": asdict(config.hotkeys),
        },
        "runs_requested": runs,
        "real_documents": real_documents,
        "device_resolution": None,
        "runs": [],
        "errors": [],
    }

    prior_clipboard = snapshot_clipboard(timeout_s=1.0)
    if prior_clipboard is None:
        raise RuntimeError("clipboard cannot be losslessly snapshotted; probe refused")

    bus = EventBus(name="HoldReleaseProbeBus")
    capture = AudioCapture(
        device=config.audio.input_device,
        max_seconds=config.audio.max_seconds,
    )
    asr = build_asr_provider(config)
    router = build_injector(config)
    observed_router = _ObservedRouter(router)
    manager = HotkeyManager(
        config.hotkeys,
        bus,
        stuck_timeout_s=config.audio.auto_stop_seconds + 5.0,
    )
    pipeline: PipelineWorker | None = None
    notepad_app: Any | None = None
    open_targets: list[Any] = []
    event_lock = threading.Lock()
    pressed_times: list[float] = []
    released_times: list[float] = []
    transcripts: list[tuple[float, TranscriptReady]] = []

    def on_event(event: object) -> None:
        now = time.perf_counter()
        with event_lock:
            if isinstance(event, HotkeyPressed) and event.mode is AppMode.DICTATION:
                pressed_times.append(now)
            elif isinstance(event, HotkeyReleased) and event.mode is AppMode.DICTATION:
                released_times.append(now)
            elif isinstance(event, TranscriptReady) and event.mode is AppMode.DICTATION:
                transcripts.append((now, event))

    bus.subscribe(on_event)

    try:
        with tempfile.TemporaryDirectory(
            prefix="dcent-hold-release-",
            ignore_cleanup_errors=True,
        ) as raw_root:
            root = Path(raw_root)
            personalization = PersonalizationStore(
                root / "personalization.json",
                enabled=config.personalization.enabled,
                learn=config.personalization.learn,
            )
            pipeline = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=asr,
                injector=observed_router,
                config=build_pipeline_config(
                    config,
                    cleanup_enabled=False,
                    personalization=personalization,
                ),
            )

            apps_mod._PREEXISTING_WINDOW_PIDS.clear()
            apps_mod._PREEXISTING_WINDOW_PIDS.update(item.pid for item in apps_mod._windows())

            bus.start()
            pipeline.start()
            manager.start()
            _wait(
                lambda: manager.status().listener_running and manager.status().status == "ok",
                timeout_s=5.0,
                stage="hotkey_listener_ready",
            )
            report["hotkey_listener"] = asdict(manager.status())
            model_load_started = time.perf_counter()
            asr.load()
            report["model_load_s"] = round(time.perf_counter() - model_load_started, 6)

            previous_sentinel: str | None = None
            control_records: list[dict[str, Any]] = []
            reusable: dict[str, Any] = {}
            for app_name in app_names:
                previous_sentinel = None
                for index in range(runs):
                    baseline = f"BASE-{app_name}-{index}-{os.urandom(8).hex()}"
                    fresh_per_run = app_name in {"vscode", "console"}
                    hold_target = reusable.get(app_name)
                    if hold_target is None or fresh_per_run:
                        if hold_target is not None:
                            hold_target.close()
                        opener = (
                            apps_mod.open_existing_document_hold_target
                            if real_documents
                            else apps_mod.open_isolated_hold_target
                        )
                        hold_target = opener(
                            app_name,
                            root,
                            run_index=index,
                            baseline=baseline,
                        )
                        open_targets.append(hold_target)
                        if not fresh_per_run:
                            reusable[app_name] = hold_target
                        if app_name == "notepad":
                            notepad_app = hold_target.app
                    hold_target.prepare(
                        baseline,
                        previous_sentinel,
                        control_records,
                        f"hold_release.{app_name}.prepare.{index}",
                    )
                    hold_target.ensure_ready_for_press()
                    observed_router.allowed_process_id = hold_target.process_id
                    with event_lock:
                        pressed_before = len(pressed_times)
                        released_before = len(released_times)
                        transcripts_before = len(transcripts)
                    decisions_before = len(observed_router.decisions)
                    resources_before = _process_metrics()
                    wall_started = time.perf_counter()
                    press_call = time.perf_counter()
                    try:
                        _chord_down()
                        _wait(
                            lambda before=pressed_before: len(pressed_times) > before,
                            timeout_s=3.0,
                            stage=f"{app_name}.run_{index}.hotkey_pressed",
                        )
                        pressed_callback = pressed_times[pressed_before]
                        _wait(
                            lambda: bool(capture.status_snapshot()["open"]),
                            timeout_s=4.0,
                            stage=f"{app_name}.run_{index}.capture_open",
                        )
                        if using_default_mic:
                            # Arm a live alternate before fixture playback. The
                            # Sonar loop is silent unless its capture side is open.
                            time.sleep(0.25)
                            resolution = capture.maybe_failover_dead_default()
                            report["device_resolution"] = {
                                "device": None if resolution is None else resolution.device,
                                "name": "" if resolution is None else resolution.name,
                                "auto_selected": bool(
                                    resolution.auto_selected if resolution else False
                                ),
                                "default_was_dead": bool(
                                    resolution.default_was_dead if resolution else False
                                ),
                                "reason": "" if resolution is None else resolution.reason,
                            }
                            _wait(
                                lambda: bool(capture.status_snapshot()["open"]),
                                timeout_s=4.0,
                                stage=f"{app_name}.run_{index}.failover_open",
                            )
                        playback = _play_fixture(
                            fixture,
                            fixture_rate,
                            output_device,
                        )
                        # The measured Sonar route is buffered; keep recording until
                        # its real tail has reached the input callback.
                        time.sleep(0.45)
                        captured_before_release = capture.peek_utterance()
                        capture_rms = float(
                            np.sqrt(
                                np.mean(
                                    np.square(captured_before_release),
                                    dtype=np.float64,
                                )
                            )
                        )
                        release_call = time.perf_counter()
                        _chord_up()
                        _wait(
                            lambda before=released_before: len(released_times) > before,
                            timeout_s=3.0,
                            stage=f"{app_name}.run_{index}.hotkey_released",
                        )
                        released_callback = released_times[released_before]
                        _wait(
                            lambda before=transcripts_before: len(transcripts) > before,
                            timeout_s=45.0,
                            stage=f"{app_name}.run_{index}.transcript_ready",
                        )
                    finally:
                        _chord_up()

                    if len(transcripts) <= transcripts_before:
                        raise RuntimeError(f"{app_name} run {index}: TranscriptReady never arrived")
                    transcript_time, event = transcripts[transcripts_before]
                    observed_text = hold_target.readback(event.cleaned)
                    visible_time = time.perf_counter()
                    if len(observed_router.decisions) <= decisions_before:
                        report["runs"].append(
                            {
                                "app": app_name,
                                "index": index,
                                "success": False,
                                "reason": event.reason,
                                "injected": event.injected,
                                "discarded": event.discarded,
                                "raw": event.raw,
                                "text": event.cleaned,
                                "observed": observed_text,
                                "captured_rms": round(capture_rms, 8),
                                "captured_duration_s": round(
                                    len(captured_before_release) / 16000, 6
                                ),
                                "release_to_transcript_ms": round(
                                    (transcript_time - release_call) * 1000, 3
                                ),
                                "release_to_visible_ms": round(
                                    (visible_time - release_call) * 1000, 3
                                ),
                                "route": None,
                            }
                        )
                        report["errors"].append(
                            f"{app_name} run {index}: no inject decision "
                            f"(reason={event.reason!r} injected={event.injected} "
                            f"captured_rms={capture_rms:.5f})"
                        )
                        previous_sentinel = baseline
                        if fresh_per_run:
                            hold_target.close()
                            reusable.pop(app_name, None)
                        continue
                    decision = observed_router.decisions[decisions_before]
                    exact = (
                        event.injected
                        and not event.discarded
                        and event.reason in {"ok", "auto_stop"}
                        and observed_text == event.cleaned
                        and baseline not in observed_text
                    )
                    resources_after = _process_metrics()
                    run_result = {
                        "app": app_name,
                        "index": index,
                        "success": exact,
                        "baseline_contract": (
                            "fresh_empty_capture"
                            if app_name == "console"
                            else "unique_selected_baseline_replaced"
                        ),
                        "baseline_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
                        "baseline_absent": baseline not in observed_text,
                        "hotkey_press_observed": len(pressed_times) == pressed_before + 1,
                        "hotkey_release_observed": len(released_times) == released_before + 1,
                        "hotkey_pressed_callback_after_keydown_ms": round(
                            (pressed_callback - press_call) * 1000, 3
                        ),
                        "hotkey_released_callback_after_keyup_ms": round(
                            (released_callback - release_call) * 1000, 3
                        ),
                        "listener_status_after": asdict(manager.status()),
                        "press_to_release_ms": round((release_call - press_call) * 1000, 3),
                        "release_to_transcript_ms": round(
                            (transcript_time - release_call) * 1000, 3
                        ),
                        "release_to_visible_ms": round((visible_time - release_call) * 1000, 3),
                        "wall_ms": round((time.perf_counter() - wall_started) * 1000, 3),
                        "capture_samplerate": capture.status_snapshot()["capture_samplerate"],
                        "captured_duration_s": round(len(captured_before_release) / 16000, 6),
                        "captured_rms": round(capture_rms, 8),
                        "raw": event.raw,
                        "text": event.cleaned,
                        "observed": observed_text,
                        "wer": word_error_rate(reference, event.cleaned),
                        "cer": char_error_rate(reference, event.cleaned),
                        "injected": event.injected,
                        "discarded": event.discarded,
                        "reason": event.reason,
                        "pipeline_timings_s": event.timings,
                        "route": asdict(decision),
                        "playback": playback,
                        "os_loop_tail_wait_s": 0.45,
                        "device_resolution": report.get("device_resolution"),
                        "effective_input_device": capture.status_snapshot().get("device"),
                        "resolved_name": capture.status_snapshot().get("resolved_name"),
                        "auto_selected": capture.status_snapshot().get("auto_selected"),
                        "cpu_seconds": round(
                            float(resources_after["cpu_seconds"])
                            - float(resources_before["cpu_seconds"]),
                            6,
                        ),
                        "rss_before_bytes": int(resources_before["rss_bytes"]),
                        "rss_after_bytes": int(resources_after["rss_bytes"]),
                        "process_peak_rss_bytes": int(resources_after["peak_rss_bytes"]),
                    }
                    report["runs"].append(run_result)
                    if not exact:
                        report["errors"].append(
                            f"{app_name} run {index}: exact injection/readback failed"
                        )
                    previous_sentinel = baseline
                    if fresh_per_run:
                        hold_target.close()
                        reusable.pop(app_name, None)

            report["control_transactions"] = control_records
            # Fault scenarios use fresh hook/capture/pipeline instances after the
            # measured runtime is fully stopped, so their events cannot leak into
            # benchmark latency or satisfy a later readback.
            manager.stop(finalize_active=False)
            pipeline.stop()
            bus.stop()
            if notepad_app is not None:
                report["adversarial"] = _run_adversarial_scenarios(
                    config=config,
                    app=notepad_app,
                    asr=asr,
                    router=router,
                    fixture=fixture,
                    fixture_rate=fixture_rate,
                    output_device=output_device,
                    root=root,
                )
                if not report["adversarial"]["success"]:
                    report["errors"].append("one or more adversarial scenarios failed")
            else:
                report["adversarial"] = {
                    "success": True,
                    "skipped": "notepad was not in the requested app set",
                }
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    finally:
        _chord_up()
        with contextlib.suppress(Exception):
            manager.stop(finalize_active=False)
        if pipeline is not None:
            with contextlib.suppress(Exception):
                pipeline.stop()
        with contextlib.suppress(Exception):
            asr.unload()
        with contextlib.suppress(Exception):
            bus.stop()
        for hold_target in open_targets:
            with contextlib.suppress(Exception):
                hold_target.close()
        current_clipboard = snapshot_clipboard(timeout_s=1.0)
        report["clipboard_restored_exact"] = current_clipboard == prior_clipboard
        if current_clipboard != prior_clipboard:
            with contextlib.suppress(Exception):
                restore_clipboard(prior_clipboard, timeout_s=1.0)
        report["process_cleanup"] = apps_mod._verify_owned_process_cleanup()

    resolution_value = report.get("device_resolution")
    device_resolution = resolution_value if isinstance(resolution_value, dict) else {}
    if using_default_mic and device_resolution.get("auto_selected"):
        report["scope"] = "shipped_empty_input_live_alternate_failover"
    elif using_default_mic and device_resolution.get("default_was_dead"):
        report["scope"] = "system_default_microphone_dead_no_alternate"
    successful = [item for item in report["runs"] if item["success"]]
    report["success_count"] = len(successful)
    report["runs_expected"] = runs * len(app_names)
    report["exact"] = (
        len(successful) == runs * len(app_names)
        and not report["errors"]
        and bool(report.get("clipboard_restored_exact"))
        and bool(report.get("process_cleanup", {}).get("success"))
    )
    report["latency_ms"] = {
        "press_to_release": _percentiles(
            [float(item["press_to_release_ms"]) for item in successful]
        ),
        "release_to_text": _percentiles(
            [float(item["release_to_transcript_ms"]) for item in successful]
        ),
        "release_to_visible": _percentiles(
            [float(item["release_to_visible_ms"]) for item in successful]
        ),
        "full_wall": _percentiles([float(item["wall_ms"]) for item in successful]),
    }
    report["quality"] = {
        "wer_mean": (
            round(statistics.fmean(float(item["wer"]) for item in successful), 6)
            if successful
            else None
        ),
        "cer_mean": (
            round(statistics.fmean(float(item["cer"]) for item in successful), 6)
            if successful
            else None
        ),
    }
    report["resources"] = {
        "peak_rss_bytes": int(_process_metrics()["peak_rss_bytes"]),
        "cpu_seconds_total": round(
            sum(float(item["cpu_seconds"] or 0.0) for item in successful), 6
        ),
    }
    return report


def run_hold_release_command(
    config: AppConfig,
    *,
    audio_path: Path,
    reference: str,
    output_device: str,
    runs: int,
    output_json: Path | None,
    apps: str = "all",
    allow_default_microphone: bool = False,
    real_documents: bool = False,
) -> int:
    try:
        resolved_output: int | str = (
            int(output_device) if output_device.strip().lstrip("-").isdigit() else output_device
        )
        report = run_hold_release_probe(
            config,
            audio_path=audio_path,
            reference=reference,
            output_device=resolved_output,
            runs=runs,
            apps=apps,
            allow_default_microphone=allow_default_microphone,
            real_documents=real_documents,
        )
    except Exception as exc:
        report = {
            "schema": "dcent-hold-release-os-capture-v1",
            "exact": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    payload = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0 if report.get("exact") else 1


@dataclass(frozen=True)
class HoldReleaseAppScore:
    """One hold-release inject into an owned real app (not a recording injector)."""

    id: str
    app: str
    reference: str
    hypothesis: str
    observed: str
    baseline: str
    injected: bool
    discarded: bool
    reason: str
    wer: float
    cer: float
    timings: dict[str, float]
    rms: float
    duration_s: float
    kind: str = "hold_release_app"
    route: str = ""


def score_shipped_default_hold_release_app(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 30.0,
) -> HoldReleaseAppScore:
    """Score hold-release real speech through an isolated real app."""
    from dcent_voice.app import build_injector
    from dcent_voice.eval_corpus import char_error_rate, word_error_rate
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import (
        _HOLD_RELEASE_FIXTURE,
        _HOLD_RELEASE_REFERENCE,
        PipelineConfig,
        PipelineWorker,
        _HoldReleaseCapture,
        _load_hold_release_wav,
        _rms,
    )

    if platform.system() != "Windows":
        raise RuntimeError("hold-release real-app scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _HOLD_RELEASE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing hold-release audio: {wav}")
    audio, samplerate = _load_hold_release_wav(wav)
    rms = _rms(audio)
    if rms < 0.01:
        raise ValueError(f"hold-release audio is silence: {wav}")
    baseline = f"DCENT-hold-app-{os.urandom(4).hex()}"
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-app-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    hold_target = open_isolated_hold_target(
        "notepad",
        root,
        run_index=0,
        baseline=baseline,
    )
    bus: EventBus | None = None
    worker: PipelineWorker | None = None
    try:
        # Bind the owned Notepad EDIT HWND even if this process is not
        # foreground. prepare() requires GetForegroundWindow, which agents steal.
        focus_target = hold_target.ensure_ready_for_press()
        if bool(getattr(focus_target, "supports_edit_messages", False)):
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendMessageW(
                _focus_window_handle(focus_target),
                0x00B1,  # EM_SETSEL
                0,
                -1,
            )
        observed_router = _ObservedRouter(build_injector(config))
        observed_router.allowed_process_id = hold_target.process_id
        capture = _HoldReleaseCapture(audio, samplerate)
        bus = EventBus(name="HoldReleaseAppScoreBus")
        done = threading.Event()
        ready: list[TranscriptReady] = []

        def _on_event(ev: object) -> None:
            if isinstance(ev, TranscriptReady):
                ready.append(ev)
                done.set()

        bus.subscribe(_on_event)
        bus.start()
        worker = PipelineWorker(
            bus=bus,
            capture=capture,  # type: ignore[arg-type]
            asr=asr,
            injector=observed_router,
            config=PipelineConfig(focus_guard_enabled=False, samplerate=samplerate),
        )
        worker.start()
        bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
        bus.publish(HotkeyReleased(AppMode.DICTATION))
        if not done.wait(timeout_s):
            raise TimeoutError("hold-release real-app inject did not finish")
        event = ready[-1]
        hypothesis = event.cleaned or event.raw
        observed = hold_target.readback(hypothesis)
        route = ""
        if observed_router.decisions:
            route = observed_router.decisions[-1].delivery
        if not capture.started:
            raise RuntimeError("hold-release capture never began")
        return HoldReleaseAppScore(
            id=wav.stem,
            app=hold_target.name,
            reference=_HOLD_RELEASE_REFERENCE,
            hypothesis=hypothesis,
            observed=observed,
            baseline=baseline,
            injected=bool(event.injected),
            discarded=bool(event.discarded),
            reason=event.reason,
            wer=word_error_rate(_HOLD_RELEASE_REFERENCE, observed),
            cer=char_error_rate(_HOLD_RELEASE_REFERENCE, observed),
            timings=dict(event.timings),
            rms=rms,
            duration_s=len(audio) / float(samplerate),
            kind="hold_release_app",
            route=route,
        )
    finally:
        if worker is not None:
            worker.stop()
        if bus is not None:
            bus.stop()
        hold_target.close()


_LOOPBACK_FRAGMENT = "SteelSeries_Sonar_VAD Chat Capture Wave"


@dataclass(frozen=True)
class AcousticHoldReleaseScore:
    """Acoustic loopback hold-release into owned real apps (not default mic)."""

    apps: tuple[str, ...]
    observed: tuple[str, ...]
    captured_rms: tuple[float, ...]
    wer: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic"


def _loopback_endpoints() -> tuple[int, int]:
    import sounddevice as sd

    devices = sd.query_devices()
    default_in = sd.default.device[0] if sd.default.device is not None else None
    in_idx: int | None = None
    out_idx: int | None = None
    for index, device in enumerate(devices):
        name = str(device.get("name") or "")
        if _LOOPBACK_FRAGMENT not in name:
            continue
        if int(device.get("max_input_channels") or 0) > 0 and in_idx is None:
            in_idx = index
        if int(device.get("max_output_channels") or 0) > 0 and out_idx is None:
            out_idx = index
    if in_idx is None or out_idx is None:
        raise RuntimeError("acoustic loopback endpoints missing")
    if default_in is not None and int(in_idx) == int(default_in):
        raise RuntimeError("loopback input must not be the OS default microphone")
    return int(in_idx), int(out_idx)


def score_shipped_default_hold_release_acoustic(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    apps: tuple[str, ...] = ("notepad",),
    scratch_root: Path | str | None = None,
    timeout_s: float = 45.0,
) -> AcousticHoldReleaseScore:
    """Score acoustic loopback hold-release through isolated real apps."""
    from dcent_voice.app import build_injector
    from dcent_voice.eval_corpus import word_error_rate
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import (
        _HOLD_RELEASE_FIXTURE,
        _HOLD_RELEASE_REFERENCE,
        PipelineConfig,
        PipelineWorker,
        _rms,
    )

    if platform.system() != "Windows":
        raise RuntimeError("acoustic hold-release scoring requires Windows")
    if len(apps) < 1:
        raise ValueError("acoustic hold-release requires a real app")
    wav = Path(audio_path) if audio_path is not None else _HOLD_RELEASE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing hold-release audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"hold-release audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-acoustic-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    observed: list[str] = []
    captured_rms: list[float] = []
    wers: list[float] = []
    for index, app_name in enumerate(apps):
        baseline = f"DCENT-hold-ac-{app_name}-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            app_name,
            root,
            run_index=index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if app_name == "notepad" and bool(
                getattr(focus_target, "supports_edit_messages", False)
            ):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseAcoustic-{app_name}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(
                ev: object,
                _done: threading.Event = done,
                _ready: list[TranscriptReady] = ready,
            ) -> None:
                if isinstance(ev, TranscriptReady):
                    _ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=asr,
                injector=observed_router,
                config=PipelineConfig(focus_guard_enabled=False, samplerate=samplerate),
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(f"{app_name}: capture did not open")
            _play_fixture(audio, samplerate, output_device)
            time.sleep(0.45)
            peeked = capture.peek_utterance()
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"{app_name}: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError(f"{app_name}: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    f"{app_name}: inject failed injected={event.injected} "
                    f"discarded={event.discarded} reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"{app_name}: baseline still present in {text!r}")
            observed.append(text)
            captured_rms.append(rms)
            wers.append(word_error_rate(_HOLD_RELEASE_REFERENCE, text))
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()
    return AcousticHoldReleaseScore(
        apps=tuple(apps),
        observed=tuple(observed),
        captured_rms=tuple(captured_rms),
        wer=tuple(wers),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        audio_bypass=False,
        kind="hold_release_acoustic",
    )


@dataclass(frozen=True)
class AcousticExtraAppScore:
    """Acoustic loopback hold-release into extra apps that need foreground."""

    apps: tuple[str, ...]
    observed: tuple[str, ...]
    captured_rms: tuple[float, ...]
    wer: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    extra_app: bool
    stolen_foreground: bool
    restored_foreground: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_extra_app"


def _steal_foreground(target_hwnd: int, scratch: Path) -> Any | None:
    """Leave the extra-app window so checked injection must restore it."""
    from dcent_voice.inject.windows_apps_test import _OwnedApp, _wait_window
    from dcent_voice.inject.windows_focus import restore_foreground
    from dcent_voice.util.owned_process import owned_process_contains_pid

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    current = int(user32.GetForegroundWindow() or 0)
    if current and current != int(target_hwnd):
        return None
    steal_file = Path(scratch) / f"steal-foreground-{os.urandom(2).hex()}.txt"
    steal_file.write_text("steal", encoding="utf-8")
    app = _OwnedApp("focus_steal", Path("notepad.exe"))
    process = app.start(
        ["notepad.exe", str(steal_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        window = _wait_window(steal_file.name, timeout_s=4.0)
        if not owned_process_contains_pid(process, window.pid):
            raise RuntimeError("foreground thief window is not in the launched process job")
        app.window = window
        restore_foreground(int(window.hwnd))
    except Exception:
        app.close()
        return None
    return app


def score_shipped_default_hold_release_acoustic_extra_app(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    apps: tuple[str, ...] = ("vscode",),
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticExtraAppScore:
    """Score acoustic loopback hold-release through additional real apps."""
    from dcent_voice.app import build_injector
    from dcent_voice.eval_corpus import word_error_rate
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.inject.windows_focus import restore_foreground
    from dcent_voice.pipeline import (
        _HOLD_RELEASE_FIXTURE,
        _HOLD_RELEASE_REFERENCE,
        PipelineConfig,
        PipelineWorker,
        _rms,
    )

    if platform.system() != "Windows":
        raise RuntimeError("extra-app acoustic hold-release scoring requires Windows")
    if len(apps) < 1:
        raise ValueError("extra-app acoustic hold-release requires a real app")
    if apps == ("notepad",):
        raise ValueError("extra-app acoustic hold-release is not notepad")
    wav = Path(audio_path) if audio_path is not None else _HOLD_RELEASE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing hold-release audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"hold-release audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-extra-app-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    observed: list[str] = []
    captured_rms: list[float] = []
    wers: list[float] = []
    stolen_any = False
    restored_any = False
    for index, app_name in enumerate(apps):
        baseline = f"DCENT-hold-xa-{app_name}-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            app_name,
            root,
            run_index=index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        steal_proc: Any | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            target_hwnd = int(getattr(focus_target, "top_hwnd", 0) or 0)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseExtraApp-{app_name}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(
                ev: object,
                _done: threading.Event = done,
                _ready: list[TranscriptReady] = ready,
            ) -> None:
                if isinstance(ev, TranscriptReady):
                    _ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=asr,
                injector=observed_router,
                config=PipelineConfig(
                    focus_guard_enabled=True,
                    samplerate=samplerate,
                ),
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(f"{app_name}: capture did not open")
            _play_fixture(audio, samplerate, output_device)
            time.sleep(0.45)
            peeked = capture.peek_utterance()
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"{app_name}: loopback capture is silence rms={rms:.5f}")
            steal_proc = _steal_foreground(target_hwnd, root)
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.restype = wintypes.HWND
            stolen = int(user32.GetForegroundWindow() or 0) != int(target_hwnd)
            stolen_any = stolen_any or stolen
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError(f"{app_name}: extra-app acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            if target_hwnd:
                restore_foreground(target_hwnd)
            restored_any = restored_any or bool(stolen and event.injected)
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    f"{app_name}: inject failed injected={event.injected} "
                    f"discarded={event.discarded} reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"{app_name}: baseline still present in {text!r}")
            observed.append(text)
            captured_rms.append(rms)
            wers.append(word_error_rate(_HOLD_RELEASE_REFERENCE, text))
        finally:
            if steal_proc is not None:
                steal_proc.close()
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()
    return AcousticExtraAppScore(
        apps=tuple(apps),
        observed=tuple(observed),
        captured_rms=tuple(captured_rms),
        wer=tuple(wers),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        extra_app=True,
        stolen_foreground=stolen_any,
        restored_foreground=restored_any,
        audio_bypass=False,
        kind="hold_release_acoustic_extra_app",
    )


@dataclass(frozen=True)
class AcousticBrowserScore:
    """Acoustic loopback hold-release into owned browser fields."""

    apps: tuple[str, ...]
    observed: tuple[str, ...]
    captured_rms: tuple[float, ...]
    wer: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    extra_app: bool
    browser: bool
    stolen_foreground: bool
    restored_foreground: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_browser"


def score_shipped_default_hold_release_acoustic_browser(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    apps: tuple[str, ...] = ("edge-ce",),
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticBrowserScore:
    """Score acoustic loopback hold-release through isolated browsers."""
    from dcent_voice.app import build_injector
    from dcent_voice.eval_corpus import word_error_rate
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.inject.windows_focus import restore_foreground
    from dcent_voice.pipeline import (
        _HOLD_RELEASE_FIXTURE,
        _HOLD_RELEASE_REFERENCE,
        PipelineConfig,
        PipelineWorker,
        _rms,
    )

    if platform.system() != "Windows":
        raise RuntimeError("browser acoustic hold-release scoring requires Windows")
    if len(apps) < 1:
        raise ValueError("browser acoustic hold-release requires a real app")
    if apps in {("notepad",), ("vscode",)}:
        raise ValueError("browser acoustic hold-release is not notepad/vscode")
    wav = Path(audio_path) if audio_path is not None else _HOLD_RELEASE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing hold-release audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"hold-release audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-browser-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    observed: list[str] = []
    captured_rms: list[float] = []
    wers: list[float] = []
    stolen_any = False
    restored_any = False
    for index, app_name in enumerate(apps):
        baseline = f"DCENT-hold-br-{app_name}-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            app_name,
            root,
            run_index=index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        steal_proc: Any | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            target_hwnd = int(getattr(focus_target, "top_hwnd", 0) or 0)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseBrowser-{app_name}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(
                ev: object,
                _done: threading.Event = done,
                _ready: list[TranscriptReady] = ready,
            ) -> None:
                if isinstance(ev, TranscriptReady):
                    _ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=asr,
                injector=observed_router,
                config=PipelineConfig(
                    focus_guard_enabled=True,
                    samplerate=samplerate,
                ),
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(f"{app_name}: capture did not open")
            _play_fixture(audio, samplerate, output_device)
            time.sleep(0.45)
            peeked = capture.peek_utterance()
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"{app_name}: loopback capture is silence rms={rms:.5f}")
            steal_proc = _steal_foreground(target_hwnd, root)
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.restype = wintypes.HWND
            stolen = int(user32.GetForegroundWindow() or 0) != int(target_hwnd)
            stolen_any = stolen_any or stolen
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError(f"{app_name}: browser acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            if target_hwnd:
                restore_foreground(target_hwnd)
            restored_any = restored_any or bool(stolen and event.injected)
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    f"{app_name}: inject failed injected={event.injected} "
                    f"discarded={event.discarded} reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"{app_name}: baseline still present in {text!r}")
            observed.append(text)
            captured_rms.append(rms)
            wers.append(word_error_rate(_HOLD_RELEASE_REFERENCE, text))
        finally:
            if steal_proc is not None:
                steal_proc.close()
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()
    return AcousticBrowserScore(
        apps=tuple(apps),
        observed=tuple(observed),
        captured_rms=tuple(captured_rms),
        wer=tuple(wers),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        extra_app=True,
        browser=True,
        stolen_foreground=stolen_any,
        restored_foreground=restored_any,
        audio_bypass=False,
        kind="hold_release_acoustic_browser",
    )


@dataclass(frozen=True)
class AcousticChromeScore:
    """Acoustic loopback hold-release into owned Chrome fields."""

    apps: tuple[str, ...]
    observed: tuple[str, ...]
    captured_rms: tuple[float, ...]
    wer: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    extra_app: bool
    browser: bool
    chrome: bool
    stolen_foreground: bool
    restored_foreground: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_chrome"


def score_shipped_default_hold_release_acoustic_chrome(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    apps: tuple[str, ...] = ("chrome-ce",),
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticChromeScore:
    """Score acoustic loopback hold-release through isolated Chrome."""
    from dcent_voice.app import build_injector
    from dcent_voice.eval_corpus import word_error_rate
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.inject.windows_focus import restore_foreground
    from dcent_voice.pipeline import (
        _HOLD_RELEASE_FIXTURE,
        _HOLD_RELEASE_REFERENCE,
        PipelineConfig,
        PipelineWorker,
        _rms,
    )

    if platform.system() != "Windows":
        raise RuntimeError("Chrome acoustic hold-release scoring requires Windows")
    if len(apps) < 1:
        raise ValueError("Chrome acoustic hold-release requires a real app")
    if apps in {("notepad",), ("vscode",), ("edge-ce",)}:
        raise ValueError("Chrome acoustic hold-release is not notepad/vscode/edge-ce")
    wav = Path(audio_path) if audio_path is not None else _HOLD_RELEASE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing hold-release audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"hold-release audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-chrome-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    observed: list[str] = []
    captured_rms: list[float] = []
    wers: list[float] = []
    stolen_any = False
    restored_any = False
    for index, app_name in enumerate(apps):
        baseline = f"DCENT-hold-ch-{app_name}-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            app_name,
            root,
            run_index=index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        steal_proc: Any | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            target_hwnd = int(getattr(focus_target, "top_hwnd", 0) or 0)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseChrome-{app_name}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(
                ev: object,
                _done: threading.Event = done,
                _ready: list[TranscriptReady] = ready,
            ) -> None:
                if isinstance(ev, TranscriptReady):
                    _ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=asr,
                injector=observed_router,
                config=PipelineConfig(
                    focus_guard_enabled=True,
                    samplerate=samplerate,
                ),
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(f"{app_name}: capture did not open")
            _play_fixture(audio, samplerate, output_device)
            time.sleep(0.45)
            peeked = capture.peek_utterance()
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"{app_name}: loopback capture is silence rms={rms:.5f}")
            steal_proc = _steal_foreground(target_hwnd, root)
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.restype = wintypes.HWND
            stolen = int(user32.GetForegroundWindow() or 0) != int(target_hwnd)
            stolen_any = stolen_any or stolen
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError(f"{app_name}: Chrome acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            if target_hwnd:
                restore_foreground(target_hwnd)
            restored_any = restored_any or bool(stolen and event.injected)
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    f"{app_name}: inject failed injected={event.injected} "
                    f"discarded={event.discarded} reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"{app_name}: baseline still present in {text!r}")
            observed.append(text)
            captured_rms.append(rms)
            wers.append(word_error_rate(_HOLD_RELEASE_REFERENCE, text))
        finally:
            if steal_proc is not None:
                steal_proc.close()
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()
    return AcousticChromeScore(
        apps=tuple(apps),
        observed=tuple(observed),
        captured_rms=tuple(captured_rms),
        wer=tuple(wers),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        extra_app=True,
        browser=True,
        chrome=True,
        stolen_foreground=stolen_any,
        restored_foreground=restored_any,
        audio_bypass=False,
        kind="hold_release_acoustic_chrome",
    )


@dataclass(frozen=True)
class AcousticNamedScore:
    """Acoustic loopback similar-sounding name hold-release after learn."""

    before: str
    after: str
    before_wer: float
    after_wer: float
    spoken: str
    written: str
    captured_rms: tuple[float, float]
    input_device: int
    output_device: int
    default_microphone: bool
    similar_sounding: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_named"


def score_shipped_default_hold_release_acoustic_named(
    asr: Any,
    config: Any,
    personalization: Any,
    *,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticNamedScore:
    """Score acoustic loopback recognition of similar-sounding names."""
    from dcent_voice.app import build_injector
    from dcent_voice.eval_corpus import load_corpus, word_error_rate
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("named acoustic hold-release scoring requires Windows")
    if personalization is None:
        raise RuntimeError("named acoustic hold-release requires personalization")
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["ls-importance"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing named acoustic audio: ls-importance")
    audio, samplerate = load_wav_mono(item.audio)
    if _rms(audio) < 0.01:
        raise ValueError(f"named acoustic audio is silence: {item.audio}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-named-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=personalization,
    )

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-nm-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseNamed-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=asr,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("named: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"named: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("named: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "named: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"named: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    before_text, before_rms = _cycle(1)
    term = personalization.record_correction("brand", "Brandd", source="typed")
    if term is None:
        raise RuntimeError("named acoustic: learn failed")
    after_text, after_rms = _cycle(2)
    return AcousticNamedScore(
        before=before_text,
        after=after_text,
        before_wer=word_error_rate(item.reference, before_text),
        after_wer=word_error_rate(item.reference, after_text),
        spoken="brand",
        written="Brandd",
        captured_rms=(before_rms, after_rms),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        similar_sounding=True,
        audio_bypass=False,
        kind="hold_release_acoustic_named",
    )


_RAMBLE_REWRITE_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "ramble-meeting.wav"
)
_RAMBLE_REWRITE_SPOKEN = "five actually six"
_RAMBLE_REWRITE_WRITTEN = "The meeting is at 6."


class _SourceASR(ASRProvider):
    """Forward production ASR and remember the pre-rewrite transcript."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.locality: Locality = inner.locality
        self.supports_per_call_language = bool(inner.supports_per_call_language)
        self.source = ""

    @property
    def supports_language_auto_detection(self) -> bool:
        return bool(self._inner.supports_language_auto_detection)

    @property
    def supported_language_codes(self) -> frozenset[str] | None:
        return self._inner.supported_language_codes

    def load(self) -> None:
        self._inner.load()

    def unload(self) -> None:
        self._inner.unload()

    def is_loaded(self) -> bool:
        return bool(self._inner.is_loaded())

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        result: TranscriptResult = self._inner.transcribe(
            audio,
            samplerate=samplerate,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            language=language,
        )
        self.source = str(getattr(result, "text", "") or "")
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@dataclass(frozen=True)
class AcousticRewriteScore:
    """Acoustic loopback ramble rewrite into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    ramble: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_rewrite"


def score_shipped_default_hold_release_acoustic_rewrite(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticRewriteScore:
    """Score acoustic loopback ramble rewriting."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("ramble rewrite acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _RAMBLE_REWRITE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing ramble rewrite audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"ramble rewrite audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-ramble-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-rw-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseRamble-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("ramble: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"ramble: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("ramble: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "ramble: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"ramble: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    if "actually" not in source.lower():
        raise RuntimeError(f"ramble: ASR missed correction marker: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.lower()
    if "actually" in folded:
        raise RuntimeError(f"ramble: rewrite did not land in {observed!r} from {source!r}")
    if "meeting" not in folded:
        raise RuntimeError(f"ramble: meeting missing from {observed!r}")
    if "6" not in observed and "six" not in folded:
        raise RuntimeError(f"ramble: six missing from {observed!r}")
    return AcousticRewriteScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_RAMBLE_REWRITE_SPOKEN,
        written=_RAMBLE_REWRITE_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        ramble=True,
        audio_bypass=False,
        kind="hold_release_acoustic_rewrite",
    )


_EMAIL_REWRITE_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "email-style-report.wav"
)
_EMAIL_REWRITE_SPOKEN = "email style"
_EMAIL_REWRITE_WRITTEN = "Could you send a report?"


@dataclass(frozen=True)
class AcousticEmailScore:
    """Acoustic loopback spoken email destination rewrite into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    email: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_email"


def score_shipped_default_hold_release_acoustic_email(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticEmailScore:
    """Score acoustic loopback email-destination rewriting."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("email rewrite acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _EMAIL_REWRITE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing email rewrite audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"email rewrite audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-email-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-em-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseEmail-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("email: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"email: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("email: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "email: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"email: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "email" not in folded_source or "style" not in folded_source:
        raise RuntimeError(f"email: ASR missed spoken destination cue: {source!r}")
    rewritten = compose_dictation(source)
    normalized = observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = normalized.lower()
    if "email style" in folded:
        raise RuntimeError(f"email: spoken cue was not peeled in {observed!r}")
    if "could you" not in folded:
        raise RuntimeError(f"email: Could you missing from {observed!r} source={source!r}")
    if "thanks" not in folded:
        raise RuntimeError(f"email: thanks missing from {observed!r}")
    if "\n\n" not in normalized:
        raise RuntimeError(f"email: paragraph break missing from {observed!r}")
    return AcousticEmailScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_EMAIL_REWRITE_SPOKEN,
        written=_EMAIL_REWRITE_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        email=True,
        audio_bypass=False,
        kind="hold_release_acoustic_email",
    )


_HIGH_CLEANUP_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "cleanup-high-think.wav"
)
_HIGH_CLEANUP_SPOKEN = "cleanup high"
_HIGH_CLEANUP_WRITTEN = "We should ship Monday."


@dataclass(frozen=True)
class AcousticHighScore:
    """Acoustic loopback spoken high cleanup into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    high: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_high"


def score_shipped_default_hold_release_acoustic_high(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticHighScore:
    """Score acoustic loopback with high cleanup."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("high cleanup acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _HIGH_CLEANUP_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing high cleanup audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"high cleanup audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-high-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-hi-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseHigh-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("high: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"high: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("high: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "high: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"high: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "cleanup" not in folded_source or "high" not in folded_source:
        raise RuntimeError(f"high: ASR missed spoken cleanup cue: {source!r}")
    if "i think" not in folded_source:
        raise RuntimeError(f"high: ASR missed hedge: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "cleanup high" in folded or "high cleanup" in folded:
        raise RuntimeError(f"high: spoken cue was not peeled in {observed!r}")
    if "i think" in folded:
        raise RuntimeError(f"high: hedge survived in {observed!r} source={source!r}")
    if "monday" not in folded:
        raise RuntimeError(f"high: Monday missing from {observed!r}")
    if "we should ship" not in folded:
        raise RuntimeError(f"high: rewritten core missing from {observed!r}")
    return AcousticHighScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_HIGH_CLEANUP_SPOKEN,
        written=_HIGH_CLEANUP_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        high=True,
        audio_bypass=False,
        kind="hold_release_acoustic_high",
    )


_SNIPPET_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "snippet-calendar.wav"
_SNIPPET_SPOKEN = "my calendar"
_SNIPPET_WRITTEN = "https://cal.example/me"


@dataclass(frozen=True)
class AcousticSnippetScore:
    """Acoustic loopback snippet expansion into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    snippet: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_snippet"


def score_shipped_default_hold_release_acoustic_snippet(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticSnippetScore:
    """Score acoustic loopback snippet expansion."""
    from dcent_voice.app import build_injector
    from dcent_voice.config import SnippetEntry
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("snippet acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _SNIPPET_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing snippet audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"snippet audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-snip-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    snippets = (SnippetEntry(spoken=_SNIPPET_SPOKEN, expansion=_SNIPPET_WRITTEN, starred=True),)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        snippets=snippets,
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-sn-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseSnippet-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("snippet: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"snippet: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("snippet: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "snippet: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"snippet: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    if "my calendar" not in source.lower():
        raise RuntimeError(f"snippet: ASR missed spoken cue: {source!r}")
    rewritten = compose_dictation(source, snippets=snippets)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "my calendar" in folded:
        raise RuntimeError(f"snippet: cue was not expanded in {observed!r}")
    if "https://cal.example/me" not in folded:
        raise RuntimeError(f"snippet: expansion missing from {observed!r} source={source!r}")
    return AcousticSnippetScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_SNIPPET_SPOKEN,
        written=_SNIPPET_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        snippet=True,
        audio_bypass=False,
        kind="hold_release_acoustic_snippet",
    )


_SCRATCH_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "scratch-report.wav"
_SCRATCH_SPOKEN = "scratch that"
_SCRATCH_WRITTEN = "Send the report."


@dataclass(frozen=True)
class AcousticScratchScore:
    """Acoustic loopback scratch-that spoken edit into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    scratch: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_scratch"


def score_shipped_default_hold_release_acoustic_scratch(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticScratchScore:
    """Score the acoustic loopback scratch-that command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("scratch acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _SCRATCH_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing scratch audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"scratch audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-scratch-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-sc-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseScratch-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("scratch: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"scratch: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("scratch: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "scratch: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"scratch: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "scratch that" not in folded_source:
        raise RuntimeError(f"scratch: ASR missed spoken edit: {source!r}")
    if "hello" not in folded_source:
        raise RuntimeError(f"scratch: ASR missed scratched clause: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "hello" in folded:
        raise RuntimeError(f"scratch: scratched clause survived in {observed!r}")
    if "scratch" in folded:
        raise RuntimeError(f"scratch: cue survived in {observed!r}")
    if "report" not in folded:
        raise RuntimeError(f"scratch: kept clause missing from {observed!r} source={source!r}")
    return AcousticScratchScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_SCRATCH_SPOKEN,
        written=_SCRATCH_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        scratch=True,
        audio_bypass=False,
        kind="hold_release_acoustic_scratch",
    )


_REPLACE_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "replace-monday.wav"
_REPLACE_SPOKEN = "replace Monday with Friday"
_REPLACE_WRITTEN = "Ship Friday."


@dataclass(frozen=True)
class AcousticReplaceScore:
    """Acoustic loopback spoken replace-with into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    replace: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_replace"


def score_shipped_default_hold_release_acoustic_replace(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticReplaceScore:
    """Score the acoustic loopback spoken-replace command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("replace acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _REPLACE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing replace audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"replace audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-replace-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-rp-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseReplace-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("replace: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"replace: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("replace: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "replace: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"replace: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "replace" not in folded_source or "with" not in folded_source:
        raise RuntimeError(f"replace: ASR missed spoken edit: {source!r}")
    if "monday" not in folded_source:
        raise RuntimeError(f"replace: ASR missed replaced word: {source!r}")
    if "friday" not in folded_source:
        raise RuntimeError(f"replace: ASR missed replacement: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "monday" in folded:
        raise RuntimeError(f"replace: replaced word survived in {observed!r}")
    if "replace" in folded:
        raise RuntimeError(f"replace: cue survived in {observed!r}")
    if "friday" not in folded:
        raise RuntimeError(f"replace: replacement missing from {observed!r} source={source!r}")
    return AcousticReplaceScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_REPLACE_SPOKEN,
        written=_REPLACE_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        replace=True,
        audio_bypass=False,
        kind="hold_release_acoustic_replace",
    )


_MEANT_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "meant-alice.wav"
_MEANT_SPOKEN = "no I meant"
_MEANT_WRITTEN = "Meet Alice."


@dataclass(frozen=True)
class AcousticMeantScore:
    """Acoustic loopback spoken no-I-meant into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    meant: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_meant"


def score_shipped_default_hold_release_acoustic_meant(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticMeantScore:
    """Score the acoustic loopback spoken-meant correction."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("meant acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _MEANT_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing meant audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"meant audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-meant-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-mn-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseMeant-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("meant: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"meant: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("meant: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "meant: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"meant: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "meant" not in folded_source:
        raise RuntimeError(f"meant: ASR missed spoken edit: {source!r}")
    if "bob" not in folded_source:
        raise RuntimeError(f"meant: ASR missed replaced name: {source!r}")
    if "alice" not in folded_source:
        raise RuntimeError(f"meant: ASR missed correction: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "bob" in folded:
        raise RuntimeError(f"meant: replaced name survived in {observed!r}")
    if "meant" in folded:
        raise RuntimeError(f"meant: cue survived in {observed!r}")
    if "alice" not in folded:
        raise RuntimeError(f"meant: correction missing from {observed!r} source={source!r}")
    return AcousticMeantScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_MEANT_SPOKEN,
        written=_MEANT_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        meant=True,
        audio_bypass=False,
        kind="hold_release_acoustic_meant",
    )


_ENTER_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "press-enter.wav"
_ENTER_SPOKEN = "press enter"
_ENTER_WRITTEN = "Hello world."


@dataclass(frozen=True)
class AcousticEnterScore:
    """Acoustic loopback spoken press-enter into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    enter: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_enter"


def score_shipped_default_hold_release_acoustic_enter(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticEnterScore:
    """Score the acoustic loopback press-enter command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("enter acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _ENTER_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing enter audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"enter audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-enter-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float, str]:
        baseline = f"DCENT-hold-en-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseEnter-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("enter: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"enter: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("enter: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "enter: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"enter: baseline still present in {text!r}")
            return text, rms, event.reason
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms, reason = _cycle(1)
    if reason != "sent":
        raise RuntimeError(f"enter: Enter was not sent reason={reason!r}")
    source = observing.source
    folded_source = source.lower()
    if "press" not in folded_source or "enter" not in folded_source:
        raise RuntimeError(f"enter: ASR missed spoken cue: {source!r}")
    if "hello" not in folded_source:
        raise RuntimeError(f"enter: ASR missed body: {source!r}")
    rewritten = compose_dictation(source)
    normalized = observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = normalized.lower()
    if "press enter" in folded:
        raise RuntimeError(f"enter: spoken cue survived in {observed!r}")
    if "hello" not in folded:
        raise RuntimeError(f"enter: body missing from {observed!r} source={source!r}")
    if not normalized.endswith("\n"):
        raise RuntimeError(f"enter: Enter was not applied in {observed!r}")
    return AcousticEnterScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_ENTER_SPOKEN,
        written=_ENTER_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        enter=True,
        audio_bypass=False,
        kind="hold_release_acoustic_enter",
    )


_PARAGRAPH_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "new-paragraph.wav"
_PARAGRAPH_SPOKEN = "new paragraph"
_PARAGRAPH_WRITTEN = "Hello\n\nWorld"


@dataclass(frozen=True)
class AcousticParagraphScore:
    """Acoustic loopback spoken new-paragraph into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    paragraph: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_paragraph"


def score_shipped_default_hold_release_acoustic_paragraph(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticParagraphScore:
    """Score the acoustic loopback new-paragraph command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("paragraph acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _PARAGRAPH_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing paragraph audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"paragraph audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-para-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-pg-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseParagraph-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("paragraph: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"paragraph: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("paragraph: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "paragraph: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"paragraph: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "new paragraph" not in folded_source:
        raise RuntimeError(f"paragraph: ASR missed spoken cue: {source!r}")
    if "hello" not in folded_source:
        raise RuntimeError(f"paragraph: ASR missed lead-in: {source!r}")
    if "world" not in folded_source:
        raise RuntimeError(f"paragraph: ASR missed after-break: {source!r}")
    rewritten = compose_dictation(source)
    normalized = observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = normalized.lower()
    if "new paragraph" in folded:
        raise RuntimeError(f"paragraph: spoken cue survived in {observed!r}")
    if "hello" not in folded or "world" not in folded:
        raise RuntimeError(f"paragraph: body missing from {observed!r} source={source!r}")
    if "\n\n" not in normalized:
        raise RuntimeError(f"paragraph: paragraph break missing from {observed!r}")
    return AcousticParagraphScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_PARAGRAPH_SPOKEN,
        written=_PARAGRAPH_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        paragraph=True,
        audio_bypass=False,
        kind="hold_release_acoustic_paragraph",
    )


_DELETE_WORD_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "delete-last-word.wav"
_DELETE_WORD_SPOKEN = "delete last word"
_DELETE_WORD_WRITTEN = "Ship Monday."


@dataclass(frozen=True)
class AcousticDeleteWordScore:
    """Acoustic loopback spoken delete-last-word into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    delete_word: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_delete_word"


def score_shipped_default_hold_release_acoustic_delete_word(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticDeleteWordScore:
    """Score the acoustic loopback delete-last-word command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("delete-word acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _DELETE_WORD_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing delete-word audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"delete-word audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-delw-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-dw-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseDeleteWord-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("delete-word: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"delete-word: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("delete-word: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "delete-word: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"delete-word: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "delete last word" not in folded_source:
        raise RuntimeError(f"delete-word: ASR missed spoken cue: {source!r}")
    if "monday" not in folded_source:
        raise RuntimeError(f"delete-word: ASR missed kept word: {source!r}")
    if "tuesday" not in folded_source:
        raise RuntimeError(f"delete-word: ASR missed dropped word: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "delete last word" in folded:
        raise RuntimeError(f"delete-word: spoken cue survived in {observed!r}")
    if "tuesday" in folded:
        raise RuntimeError(f"delete-word: dropped word survived in {observed!r}")
    if "monday" not in folded:
        raise RuntimeError(f"delete-word: kept word missing from {observed!r} source={source!r}")
    return AcousticDeleteWordScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_DELETE_WORD_SPOKEN,
        written=_DELETE_WORD_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        delete_word=True,
        audio_bypass=False,
        kind="hold_release_acoustic_delete_word",
    )


_DELETE_SENTENCE_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "delete-last-sentence.wav"
)
_DELETE_SENTENCE_SPOKEN = "delete last sentence"
_DELETE_SENTENCE_WRITTEN = "The meeting is Monday."


@dataclass(frozen=True)
class AcousticDeleteSentenceScore:
    """Acoustic loopback spoken delete-last-sentence into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    delete_sentence: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_delete_sentence"


def score_shipped_default_hold_release_acoustic_delete_sentence(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticDeleteSentenceScore:
    """Score the acoustic loopback delete-last-sentence command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("delete-sentence acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _DELETE_SENTENCE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing delete-sentence audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"delete-sentence audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-dels-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-ds-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseDeleteSentence-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("delete-sentence: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"delete-sentence: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("delete-sentence: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "delete-sentence: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"delete-sentence: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "delete last sentence" not in folded_source:
        raise RuntimeError(f"delete-sentence: ASR missed spoken cue: {source!r}")
    if "monday" not in folded_source:
        raise RuntimeError(f"delete-sentence: ASR missed kept sentence: {source!r}")
    if "tuesday" not in folded_source:
        raise RuntimeError(f"delete-sentence: ASR missed dropped sentence: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "delete last sentence" in folded:
        raise RuntimeError(f"delete-sentence: spoken cue survived in {observed!r}")
    if "tuesday" in folded:
        raise RuntimeError(f"delete-sentence: dropped sentence survived in {observed!r}")
    if "monday" not in folded or "meeting" not in folded:
        raise RuntimeError(
            f"delete-sentence: kept sentence missing from {observed!r} source={source!r}"
        )
    return AcousticDeleteSentenceScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_DELETE_SENTENCE_SPOKEN,
        written=_DELETE_SENTENCE_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        delete_sentence=True,
        audio_bypass=False,
        kind="hold_release_acoustic_delete_sentence",
    )


_DELETE_LINE_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "delete-last-line.wav"
_DELETE_LINE_SPOKEN = "delete last line"
_DELETE_LINE_WRITTEN = "Keep the intro."


@dataclass(frozen=True)
class AcousticDeleteLineScore:
    """Acoustic loopback spoken delete-last-line into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    delete_line: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_delete_line"


def score_shipped_default_hold_release_acoustic_delete_line(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticDeleteLineScore:
    """Score the acoustic loopback delete-last-line command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("delete-line acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _DELETE_LINE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing delete-line audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"delete-line audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-dell-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-dl-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseDeleteLine-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("delete-line: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"delete-line: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("delete-line: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "delete-line: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"delete-line: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "delete last line" not in folded_source:
        raise RuntimeError(f"delete-line: ASR missed spoken cue: {source!r}")
    if "new line" not in folded_source:
        raise RuntimeError(f"delete-line: ASR missed new-line token: {source!r}")
    if "intro" not in folded_source:
        raise RuntimeError(f"delete-line: ASR missed kept line: {source!r}")
    if "outro" not in folded_source:
        raise RuntimeError(f"delete-line: ASR missed dropped line: {source!r}")
    rewritten = compose_dictation(source)
    folded = observed.replace("\r\n", "\n").replace("\r", "\n").lower()
    if "delete last line" in folded:
        raise RuntimeError(f"delete-line: spoken cue survived in {observed!r}")
    if "outro" in folded:
        raise RuntimeError(f"delete-line: dropped line survived in {observed!r}")
    if "new line" in folded:
        raise RuntimeError(f"delete-line: new-line cue survived in {observed!r}")
    if "intro" not in folded:
        raise RuntimeError(f"delete-line: kept line missing from {observed!r} source={source!r}")
    return AcousticDeleteLineScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_DELETE_LINE_SPOKEN,
        written=_DELETE_LINE_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        delete_line=True,
        audio_bypass=False,
        kind="hold_release_acoustic_delete_line",
    )


_NEWLINE_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "new-line.wav"
_NEWLINE_SPOKEN = "new line"
_NEWLINE_WRITTEN = "Alpha Report.\nBravo Draft."


@dataclass(frozen=True)
class AcousticNewlineScore:
    """Acoustic loopback spoken new-line into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    newline: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_newline"


def score_shipped_default_hold_release_acoustic_newline(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticNewlineScore:
    """Score the acoustic loopback new-line command."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("newline acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _NEWLINE_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing newline audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"newline audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-nl-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-nl-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseNewline-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("newline: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"newline: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("newline: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "newline: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"newline: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "new line" not in folded_source:
        raise RuntimeError(f"newline: ASR missed spoken cue: {source!r}")
    if "alpha" not in folded_source:
        raise RuntimeError(f"newline: ASR missed lead-in: {source!r}")
    if "bravo" not in folded_source:
        raise RuntimeError(f"newline: ASR missed after-break: {source!r}")
    rewritten = compose_dictation(source)
    normalized = observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = normalized.lower()
    if "new line" in folded:
        raise RuntimeError(f"newline: spoken cue survived in {observed!r}")
    if "alpha" not in folded or "bravo" not in folded:
        raise RuntimeError(f"newline: body missing from {observed!r} source={source!r}")
    if "\n" not in normalized:
        raise RuntimeError(f"newline: line break missing from {observed!r}")
    if "\n\n" in normalized:
        raise RuntimeError(f"newline: paragraph break leaked into {observed!r}")
    return AcousticNewlineScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_NEWLINE_SPOKEN,
        written=_NEWLINE_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        newline=True,
        audio_bypass=False,
        kind="hold_release_acoustic_newline",
    )


_BULLET_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "audio" / "eval" / "bullet-list.wav"
_BULLET_SPOKEN = "bullet point"
_BULLET_WRITTEN = "Shopping list.\n- milk\n- rice"


@dataclass(frozen=True)
class AcousticBulletScore:
    """Acoustic loopback spoken bullet list into owned Notepad."""

    source: str
    observed: str
    rewritten: str
    spoken: str
    written: str
    captured_rms: tuple[float, ...]
    input_device: int
    output_device: int
    default_microphone: bool
    bullet: bool
    audio_bypass: bool
    kind: str = "hold_release_acoustic_bullet"


def score_shipped_default_hold_release_acoustic_bullet(
    asr: Any,
    config: Any,
    *,
    audio_path: Path | str | None = None,
    scratch_root: Path | str | None = None,
    timeout_s: float = 60.0,
) -> AcousticBulletScore:
    """Score acoustic loopback bullet-list formatting."""
    from dcent_voice.app import build_injector
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.inject.windows_apps_test import open_isolated_hold_target
    from dcent_voice.pipeline import PipelineConfig, PipelineWorker, _rms

    if platform.system() != "Windows":
        raise RuntimeError("bullet acoustic hold-release scoring requires Windows")
    wav = Path(audio_path) if audio_path is not None else _BULLET_FIXTURE
    if not wav.is_file():
        raise FileNotFoundError(f"missing bullet audio: {wav}")
    audio, samplerate = load_wav_mono(wav)
    if _rms(audio) < 0.01:
        raise ValueError(f"bullet audio is silence: {wav}")
    input_device, output_device = _loopback_endpoints()
    root = (
        Path(scratch_root)
        if scratch_root is not None
        else Path(tempfile.mkdtemp(prefix="dcent-hold-bul-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    pipeline_config = PipelineConfig(
        focus_guard_enabled=False,
        samplerate=samplerate,
        personalization=PersonalizationStore(root / "personalization.json"),
    )
    observing = _SourceASR(asr)

    def _cycle(run_index: int) -> tuple[str, float]:
        baseline = f"DCENT-hold-bl-{os.urandom(3).hex()}"
        hold_target = open_isolated_hold_target(
            "notepad",
            root,
            run_index=run_index,
            baseline=baseline,
        )
        bus: EventBus | None = None
        worker: PipelineWorker | None = None
        capture = AudioCapture(device=input_device, max_seconds=30.0)
        try:
            focus_target = hold_target.ensure_ready_for_press()
            if bool(getattr(focus_target, "supports_edit_messages", False)):
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SendMessageW(_focus_window_handle(focus_target), 0x00B1, 0, -1)
            observed_router = _ObservedRouter(build_injector(config))
            observed_router.allowed_process_id = hold_target.process_id
            bus = EventBus(name=f"HoldReleaseBullet-{run_index}")
            done = threading.Event()
            ready: list[TranscriptReady] = []

            def _on_event(ev: object, _done: threading.Event = done) -> None:
                if isinstance(ev, TranscriptReady):
                    ready.append(ev)
                    _done.set()

            bus.subscribe(_on_event)
            bus.start()
            worker = PipelineWorker(
                bus=bus,
                capture=capture,
                asr=observing,
                injector=observed_router,
                config=pipeline_config,
            )
            worker.start()
            bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=focus_target))
            opened = time.monotonic() + 4.0
            while time.monotonic() < opened:
                if capture.status_snapshot().get("open"):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("bullet: capture did not open")
            time.sleep(0.35)
            _play_fixture(audio, samplerate, output_device)
            deadline = time.monotonic() + 2.0
            peeked = capture.peek_utterance()
            while time.monotonic() < deadline:
                peeked = capture.peek_utterance()
                if len(np.asarray(peeked).reshape(-1)) >= int(0.85 * len(audio)):
                    break
                time.sleep(0.05)
            rms = _rms(np.asarray(peeked, dtype=np.float32).reshape(-1))
            if rms < 0.01:
                raise RuntimeError(f"bullet: loopback capture is silence rms={rms:.5f}")
            bus.publish(HotkeyReleased(AppMode.DICTATION))
            if not done.wait(timeout_s):
                raise TimeoutError("bullet: acoustic hold-release did not finish")
            event = ready[-1]
            hypothesis = event.cleaned or event.raw
            text = hold_target.readback(hypothesis)
            if not event.injected or event.discarded:
                raise RuntimeError(
                    "bullet: inject failed injected="
                    f"{event.injected} discarded={event.discarded} "
                    f"reason={event.reason!r}"
                )
            if baseline in text:
                raise RuntimeError(f"bullet: baseline still present in {text!r}")
            return text, rms
        finally:
            if worker is not None:
                worker.stop()
            if bus is not None:
                bus.stop()
            capture.stop()
            hold_target.close()

    _cycle(0)
    observed, captured_rms = _cycle(1)
    source = observing.source
    folded_source = source.lower()
    if "bullet point" not in folded_source:
        raise RuntimeError(f"bullet: ASR missed bullet-point cue: {source!r}")
    if "next bullet" not in folded_source:
        raise RuntimeError(f"bullet: ASR missed next-bullet cue: {source!r}")
    if "milk" not in folded_source:
        raise RuntimeError(f"bullet: ASR missed first item: {source!r}")
    if "rice" not in folded_source:
        raise RuntimeError(f"bullet: ASR missed second item: {source!r}")
    rewritten = compose_dictation(source)
    normalized = observed.replace("\r\n", "\n").replace("\r", "\n")
    folded = normalized.lower()
    if "bullet point" in folded:
        raise RuntimeError(f"bullet: spoken cue survived in {observed!r}")
    if "next bullet" in folded:
        raise RuntimeError(f"bullet: next-bullet cue survived in {observed!r}")
    if "milk" not in folded or "rice" not in folded:
        raise RuntimeError(f"bullet: items missing from {observed!r} source={source!r}")
    if not re.search(r"\n-\s*milk", folded):
        raise RuntimeError(f"bullet: milk marker missing from {observed!r}")
    if not re.search(r"\n-\s*rice", folded):
        raise RuntimeError(f"bullet: rice marker missing from {observed!r}")
    if "\n\n" in normalized:
        raise RuntimeError(f"bullet: paragraph break leaked into {observed!r}")
    return AcousticBulletScore(
        source=source,
        observed=observed,
        rewritten=rewritten,
        spoken=_BULLET_SPOKEN,
        written=_BULLET_WRITTEN,
        captured_rms=(captured_rms,),
        input_device=input_device,
        output_device=output_device,
        default_microphone=False,
        bullet=True,
        audio_bypass=False,
        kind="hold_release_acoustic_bullet",
    )
