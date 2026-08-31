# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Bounded Windows injection proof against a private native EDIT control.

This module deliberately cannot select an arbitrary target.  The public command
creates a nonce-bearing temporary contract, launches its own helper process, and
injects only while that helper's top-level HWND is foreground.  The helper reads
its real EDIT control locally and reports through a small file contract because
Win32 intentionally restricts cross-process edit-text reads.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

from dcent_voice.inject import clipboard as clipboard_mod
from dcent_voice.inject.clipboard import (
    CF_PRIVATEFIRST,
    CF_UNICODETEXT,
    ClipboardPasteInjector,
    ClipboardPreservationError,
    get_clipboard_sequence_number,
    get_clipboard_text,
    restore_clipboard,
    snapshot_clipboard,
)
from dcent_voice.inject.keystroke import WindowsSendInputInjector
from dcent_voice.inject.router import RoutingInjector
from dcent_voice.inject.windows_focus import capture_target_from_hwnd
from dcent_voice.util.owned_process import start_owned_process, terminate_owned_process


class FocusGuardError(RuntimeError):
    """Raised before injection when the private target lost foreground focus."""


class TargetTimeoutError(TimeoutError):
    """Raised when the private helper misses a bounded contract deadline."""


class InjectionSelfTestStageError(RuntimeError):
    """Preserve the exact harness stage that failed."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")


_SOURCE_ENV_PREFIX = "DCENT_VOICE_SELF_TEST_ENV_PREFIX"


def _self_test_stage(stage: str, operation):
    try:
        return operation()
    except InjectionSelfTestStageError:
        raise
    except Exception as exc:
        raise InjectionSelfTestStageError(stage, exc) from exc


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _wait_until(predicate, *, timeout_s: float, description: str) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.01, remaining))
    raise TargetTimeoutError(f"Timed out after {timeout_s:.2f}s waiting for {description}.")


def _app_command(command: str, *arguments: object) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, command, *(str(argument) for argument in arguments)]
    # The venv python.exe launcher creates an intermediate process on Windows,
    # which breaks inherited-handle parent binding. Execute the base interpreter
    # directly and provide the current import roots explicitly.
    executable = str(getattr(sys, "_base_executable", sys.executable))
    return [
        executable,
        "-m",
        "dcent_voice",
        command,
        *(str(argument) for argument in arguments),
    ]


def _child_command(contract: Path) -> list[str]:
    return _app_command("injection-test-target", contract)


def _source_environment_prefix(environment: dict[str, str]) -> Path:
    """Locate dependencies when helpers deliberately run the base interpreter.

    A Windows venv launcher exposes ``sys._base_executable``.  Parent-bound
    helpers use that executable to avoid the launcher's intermediate process,
    so descendants no longer have the original ``sys.prefix``.  Preserve the
    source prefix in a self-test-specific variable across helper generations.
    """
    candidates = (
        environment.get(_SOURCE_ENV_PREFIX, ""),
        sys.prefix,
        environment.get("VIRTUAL_ENV", ""),
    )
    for candidate in candidates:
        if not candidate:
            continue
        prefix = Path(candidate)
        if (prefix / "Lib" / "site-packages" / "pywin32_system32").is_dir():
            return prefix
    return Path(sys.prefix)


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if not getattr(sys, "frozen", False):
        import_roots = [entry for entry in sys.path if entry and Path(entry).exists()]
        existing = environment.get("PYTHONPATH", "")
        if existing:
            import_roots.append(existing)
        environment["PYTHONPATH"] = os.pathsep.join(import_roots)
        environment_prefix = _source_environment_prefix(environment)
        environment[_SOURCE_ENV_PREFIX] = str(environment_prefix)
        pywin32_dlls = environment_prefix / "Lib" / "site-packages" / "pywin32_system32"
        if pywin32_dlls.is_dir():
            environment["PATH"] = str(pywin32_dlls) + os.pathsep + environment.get("PATH", "")
    return environment


def _restrict_directory_to_current_user(path: Path) -> None:
    """Protect helper contracts from other local users with a non-inherited DACL."""
    if platform.system() != "Windows":
        return
    dll_directory = None
    environment_prefix = _source_environment_prefix(dict(os.environ))
    pywin32_dlls = environment_prefix / "Lib" / "site-packages" / "pywin32_system32"
    if pywin32_dlls.is_dir():
        dll_directory = os.add_dll_directory(str(pywin32_dlls))
    try:
        import win32api
        import win32con
        import win32security
    finally:
        if dll_directory is not None:
            dll_directory.close()

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, user_sid)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, system_sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def _actual_parent_pid() -> int:
    """Read this process's kernel-recorded parent PID from Toolhelp."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or int(snapshot) == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        own_pid = os.getpid()
        while True:
            if int(entry.th32ProcessID) == own_pid:
                return int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    raise RuntimeError("Current process was absent from the Toolhelp process snapshot.")


class _NativeTarget:
    def __init__(self, root: Path, name: str) -> None:
        self.root = root / name
        self.root.mkdir(parents=True)
        _restrict_directory_to_current_user(self.root)
        self.contract = self.root / "contract.json"
        self.ready = self.root / "ready.json"
        self.request_path = self.root / "request.json"
        self.response_path = self.root / "response.json"
        self.error_path = self.root / "error.json"
        self.token = uuid.uuid4().hex
        _write_json(
            self.contract,
            {
                "schema_version": 1,
                "token": self.token,
                "ready": str(self.ready),
                "request": str(self.request_path),
                "response": str(self.response_path),
                "error": str(self.error_path),
                "title": f"DCENT Voice Injection Self-Test {self.token[:10]}",
                "parent_pid": os.getpid(),
            },
        )
        self.process = start_owned_process(
            _child_command(self.contract),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=_child_environment(),
        )
        try:
            ready = _wait_until(self._ready_payload, timeout_s=8.0, description="native target")
        except Exception:
            terminate_owned_process(self.process, grace_s=1.0, kill_s=1.0)
            raise
        self.hwnd = int(ready["hwnd"])
        self.pid = int(ready["pid"])
        self._sequence = 0

    def _ready_payload(self) -> dict[str, Any] | None:
        if self.process.poll() is not None:
            detail = ""
            if self.error_path.is_file():
                detail = f" {_read_json(self.error_path).get('error', '')}"
            raise RuntimeError(
                f"Native target exited with code {self.process.returncode}.{detail}".rstrip()
            )
        if not self.ready.is_file():
            return None
        payload = _read_json(self.ready)
        return payload if payload.get("token") == self.token else None

    def request(
        self,
        operation: str,
        *,
        timeout_s: float = 2.0,
        **parameters: Any,
    ) -> dict[str, Any]:
        self._sequence += 1
        sequence = self._sequence
        request_file = self.request_path.with_name(
            f"{self.request_path.stem}-{sequence}{self.request_path.suffix}"
        )
        response_file = self.response_path.with_name(
            f"{self.response_path.stem}-{sequence}{self.response_path.suffix}"
        )
        _write_json(
            request_file,
            {
                "op": operation,
                "sequence": sequence,
                "token": self.token,
                **parameters,
            },
        )

        def matching_response() -> dict[str, Any] | None:
            if response_file.is_file():
                try:
                    payload = _read_json(response_file)
                except (OSError, ValueError, json.JSONDecodeError):
                    payload = None
                if (
                    payload is not None
                    and payload.get("token") == self.token
                    and payload.get("sequence") == sequence
                ):
                    return payload
            if self.error_path.is_file():
                raise RuntimeError(str(_read_json(self.error_path).get("error", "target error")))
            if self.process.poll() is not None:
                raise RuntimeError(f"Native target exited with code {self.process.returncode}.")
            return None

        return _wait_until(
            matching_response,
            timeout_s=timeout_s,
            description=f"native target {operation!r} response",
        )

    def focus(self) -> None:
        # The test parent is the only process authorized to launch this target.
        # Best-effort foreground delegation plus bounded retries absorbs ordinary
        # Win32 foreground-lock timing without using AttachThreadInput or touching
        # the input queues of unrelated user applications.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        user32.AllowSetForegroundWindow(self.pid)
        deadline = time.monotonic() + 1.25
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                response = self.request("focus", timeout_s=min(0.30, remaining))
            except TargetTimeoutError:
                break
            if response.get("focused") and _foreground_window() == self.hwnd:
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise FocusGuardError("The private native target could not acquire foreground focus.")

    def clear(self) -> None:
        self.request("clear")

    def text(self) -> str:
        return str(self.request("read").get("text", ""))

    def close(self) -> None:
        if self.process.poll() is None:
            with contextlib.suppress(Exception):
                self.request("shutdown", timeout_s=1.0)
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        finally:
            terminate_owned_process(self.process, grace_s=1.0, kill_s=1.0)


def _foreground_window() -> int:
    if platform.system() != "Windows":
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.GetGuiResources.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    return int(user32.GetForegroundWindow() or 0)


def guarded_inject(router: RoutingInjector, text: str, *, expected_hwnd: int) -> None:
    """Inject only if the caller-owned native target is still foreground."""
    actual = _foreground_window()
    if not expected_hwnd or actual != int(expected_hwnd):
        raise FocusGuardError(
            f"Foreground window changed before injection (expected {expected_hwnd}, got {actual})."
        )
    router.inject(text)


def _router(
    route: str,
    *,
    paste_delay_s: float = 0.10,
    timeout_s: float = 2.0,
    process_name_fn=None,
):
    clipboard = ClipboardPasteInjector(
        restore_previous=True,
        open_timeout_s=timeout_s,
        paste_delay_s=paste_delay_s,
        paste_min_delay_s=min(0.04, paste_delay_s),
    )
    return RoutingInjector(
        default_name=route,
        injectors={"clipboard": clipboard, "keystroke": WindowsSendInputInjector()},
        process_name_fn=process_name_fn or (lambda: "dcent-injection-test-target.exe"),
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _measure_native_backend(
    target: _NativeTarget,
    *,
    backend: str,
    text: str,
    runs: int,
    paste_delay_s: float = 0.10,
) -> dict[str, Any]:
    configured_route = {"native_replace": "keystroke", "native_paste": "clipboard"}[backend]
    router = _router(configured_route, paste_delay_s=paste_delay_s)
    timings_ms: list[float] = []
    successes = 0
    mismatches: list[dict[str, Any]] = []
    for run_index in range(runs):
        target.clear()
        target.focus()
        started = time.perf_counter()
        guarded_inject(router, text, expected_hwnd=target.hwnd)
        observed = target.text()
        timings_ms.append((time.perf_counter() - started) * 1000.0)
        successes += int(observed == text)
        if observed != text:
            mismatches.append(
                {
                    "run_index": run_index,
                    "expected_escaped": text.encode("unicode_escape").decode("ascii"),
                    "observed_escaped": observed.encode("unicode_escape").decode("ascii"),
                }
            )
    return {
        "route": backend,
        "native_api": (
            "verified EM_REPLACESEL (target-bound)"
            if backend == "native_replace"
            else "clipboard transaction + verified WM_PASTE (target-bound)"
        ),
        "target": "private Win32 multiline EDIT control",
        "runs": runs,
        "success_count": successes,
        "mismatches": mismatches,
        "payload_utf16_units": len(text.encode("utf-16-le")) // 2,
        "latency_ms": {
            "p50": round(_percentile(timings_ms, 0.50), 3),
            "p95": round(_percentile(timings_ms, 0.95), 3),
            "p99": round(_percentile(timings_ms, 0.99), 3),
            "mean": round(statistics.fmean(timings_ms), 3),
            "minimum": round(min(timings_ms), 3),
        },
        "consumption_acknowledgement": (
            "single native message; immediate read plus bounded read-only grace polling; "
            "exact post-state text and selection required"
        ),
        "post_consumption_delay_ms": 0.0 if backend == "native_paste" else None,
    }


def _clipboard_custom_format_bytes(format_id: int) -> bytes | None:
    user32 = clipboard_mod._user32
    kernel32 = clipboard_mod._kernel32
    assert user32 is not None and kernel32 is not None
    with clipboard_mod._opened_clipboard(0.75, stage=f"self_test_read_format_0x{format_id:04X}"):
        handle = user32.GetClipboardData(format_id)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)


def _clipboard_restore_check(target: _NativeTarget) -> dict[str, Any]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    html_format = int(user32.RegisterClipboardFormatW("HTML Format"))
    custom_format = int(user32.RegisterClipboardFormatW("DCENT Voice Arbitrary Exact Format"))
    seeded_text = "previous clipboard — 日本語 🔥"
    seeded_html = b"Version:0.9\r\n<html><body>private-format</body></html>\x00"
    seeded_custom = b"DCENT-CUSTOM-UNLISTED-\x00\x01\xff"
    seeded = [
        (CF_UNICODETEXT, (seeded_text + "\0").encode("utf-16-le")),
        (html_format, seeded_html),
        (custom_format, seeded_custom),
    ]
    restore_clipboard(seeded)
    target.clear()
    target.focus()
    inserted = "Clipboard route exact Unicode: naïve 日本語 🔥 " + ("x" * 64)
    guarded_inject(_router("clipboard"), inserted, expected_hwnd=target.hwnd)
    observed = target.text()
    restored_text = get_clipboard_text()
    restored_html = _clipboard_custom_format_bytes(html_format)
    restored_custom = _clipboard_custom_format_bytes(custom_format)
    return {
        "success": (
            observed == inserted
            and restored_text == seeded_text
            and restored_html == seeded_html
            and restored_custom == seeded_custom
        ),
        "unicode_text_restored": restored_text == seeded_text,
        "registered_html_format_restored": restored_html == seeded_html,
        "arbitrary_registered_format_restored": restored_custom == seeded_custom,
        "target_text_exact": observed == inserted,
    }


def _current_process_gdi_handles() -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.GetGuiResources.restype = wintypes.DWORD
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    return int(user32.GetGuiResources(kernel32.GetCurrentProcess(), 0))


def _private_clipboard_fail_closed_check(
    target: _NativeTarget, *, iterations: int = 8
) -> dict[str, Any]:
    """Preserve owner-managed formats while native EDIT injection stays available."""
    previous = snapshot_clipboard(timeout_s=0.75)
    if previous is None:
        raise ClipboardPreservationError("Private-format probe could not preserve the clipboard.")
    gdi_before = _current_process_gdi_handles()
    attempts: list[dict[str, Any]] = []
    try:
        for index in range(iterations):
            target.clear()
            focus_target = capture_target_from_hwnd(target.hwnd)
            if focus_target is None or not focus_target.supports_edit_messages:
                raise RuntimeError("private native EDIT target could not be captured")
            payload = b"DCENT-PRIVATE-OWNER-MANAGED-" + bytes([index, 0, 0xFF])
            inserted = f"PRIVATE-FORMAT-NATIVE-FALLBACK-{index}"
            seeded = target.request("seed_private_clipboard", payload_hex=payload.hex())
            sequence_before = get_clipboard_sequence_number()
            content_before = _clipboard_custom_format_bytes(CF_PRIVATEFIRST)
            decision = _router("clipboard", paste_delay_s=0).inject_into_target_with_decision(
                inserted,
                focus_target,
            )
            sequence_after = get_clipboard_sequence_number()
            content_after = _clipboard_custom_format_bytes(CF_PRIVATEFIRST)
            observed = target.text()
            cleaned = target.request("clear_private_clipboard")
            attempts.append(
                {
                    "native_fallback_exact": (
                        decision.delivery == "native_replace" and observed == inserted
                    ),
                    "delivery": decision.delivery,
                    "sequence_unchanged": sequence_after == sequence_before,
                    "content_unchanged": content_before == payload and content_after == payload,
                    "owner_allocation_incremented": int(seeded.get("private_allocations", -1))
                    == index + 1,
                    "owner_freed_private_handle": int(cleaned.get("private_frees", -1))
                    == index + 1,
                    "owner_allocations_outstanding": int(
                        cleaned.get("private_allocations_outstanding", -1)
                    ),
                }
            )
        restore_clipboard(previous, timeout_s=0.75)
    except Exception:
        with contextlib.suppress(Exception):
            restore_clipboard(previous, timeout_s=0.75)
        raise
    gdi_after = _current_process_gdi_handles()
    final_stats = target.request("private_clipboard_stats")
    success = all(
        bool(attempt["native_fallback_exact"])
        and bool(attempt["sequence_unchanged"])
        and bool(attempt["content_unchanged"])
        and bool(attempt["owner_allocation_incremented"])
        and bool(attempt["owner_freed_private_handle"])
        and int(attempt["owner_allocations_outstanding"]) == 0
        for attempt in attempts
    )
    allocated = int(final_stats.get("private_allocations", -1))
    freed = int(final_stats.get("private_frees", -1))
    return {
        "success": success
        and allocated == freed
        and gdi_after == gdi_before
        and len(attempts) == iterations,
        "format": "CF_PRIVATEFIRST (0x0200)",
        "iterations": iterations,
        "all_rejected_before_mutation": False,
        "all_native_fallback_exact": all(attempt["native_fallback_exact"] for attempt in attempts),
        "all_sequences_unchanged": all(attempt["sequence_unchanged"] for attempt in attempts),
        "all_content_unchanged": all(attempt["content_unchanged"] for attempt in attempts),
        "probe_global_allocations": allocated,
        "probe_global_frees": freed,
        "probe_global_allocations_outstanding": allocated - freed,
        "gdi_handles_before": gdi_before,
        "gdi_handles_after": gdi_after,
        "gdi_handle_delta": gdi_after - gdi_before,
        "attempts": attempts,
    }


def _concurrency_check(target: _NativeTarget) -> dict[str, Any]:
    target.clear()
    target.focus()
    routers = [_router("clipboard", paste_delay_s=0.04) for _ in range(2)]
    barrier = threading.Barrier(3)
    completed: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def inject(router: RoutingInjector, value: str) -> None:
        try:
            barrier.wait(timeout=2.0)
            guarded_inject(router, value, expected_hwnd=target.hwnd)
            with lock:
                completed.append(value)
        except Exception as exc:
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    values = [f"<{index}:independent-router-日本語>" for index in range(2)]
    threads = [
        threading.Thread(target=inject, args=(router, value), daemon=True)
        for router, value in zip(routers, values, strict=True)
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2.0)
    for thread in threads:
        thread.join(timeout=2.0)
    observed = target.text()
    expected_orders = ["".join(values), "".join(reversed(values))]
    serialized_exact = observed in expected_orders
    return {
        "success": not errors and len(completed) == len(values) and serialized_exact,
        "attempted": len(values),
        "completed": len(completed),
        "serialized_exact": serialized_exact,
        "observed_text_escaped": observed.encode("unicode_escape").decode("ascii"),
        "expected_orders_escaped": [
            value.encode("unicode_escape").decode("ascii") for value in expected_orders
        ],
        "errors": errors,
    }


def _cross_process_serialization_check(target: _NativeTarget) -> dict[str, Any]:
    target.clear()
    target.focus()
    from dcent_voice.inject.windows_focus import capture_foreground_target

    bound_target = capture_foreground_target()
    if bound_target is None or bound_target.top_hwnd != target.hwnd:
        raise FocusGuardError("Could not bind the cross-process test target.")
    start = target.root / "cross-start"
    done = target.root / "cross-done.json"
    child_payload = "<child-process-clipboard-日本語>"
    parent_payload = "<parent-process-clipboard-🔥>"
    target.request(
        "prepare_cross_process",
        payload=child_payload,
        start=str(start),
        done=str(done),
    )
    parent_errors: list[str] = []

    def parent_inject() -> None:
        try:
            ClipboardPasteInjector(
                restore_previous=True,
                paste_delay_s=0.04,
                paste_min_delay_s=0.04,
            ).inject_targeted(parent_payload, bound_target)
        except Exception as exc:
            parent_errors.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=parent_inject, daemon=True)
    worker.start()
    _write_json(start, {"go": True})
    worker.join(timeout=3.0)
    child_result = _wait_until(
        lambda: _read_json(done) if done.is_file() else None,
        timeout_s=3.0,
        description="cross-process clipboard worker",
    )
    observed = target.text()
    exact_once = observed in {
        parent_payload + child_payload,
        child_payload + parent_payload,
    }
    return {
        "success": (
            not worker.is_alive() and not parent_errors and child_result.get("ok") and exact_once
        ),
        "parent_completed": not worker.is_alive(),
        "child_completed": bool(child_result.get("ok")),
        "both_payloads_exact_once": exact_once,
        "observed_text_escaped": observed.encode("unicode_escape").decode("ascii"),
        "expected_orders_escaped": [
            (parent_payload + child_payload).encode("unicode_escape").decode("ascii"),
            (child_payload + parent_payload).encode("unicode_escape").decode("ascii"),
        ],
        "parent_payload_occurrences": observed.count(parent_payload),
        "child_payload_occurrences": observed.count(child_payload),
        "errors": parent_errors
        + ([str(child_result.get("error"))] if not child_result.get("ok") else []),
    }


def _abandoned_mutex_check(primary: _NativeTarget, abandoned: _NativeTarget) -> dict[str, Any]:
    response = abandoned.request("abandon_clipboard_mutex")
    abandoned.process.wait(timeout=2.0)
    primary.clear()
    primary.focus()
    payload = "abandoned-mutex-recovered"
    started = time.perf_counter()
    guarded_inject(_router("clipboard", paste_delay_s=0.04), payload, expected_hwnd=primary.hwnd)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "success": (
            bool(response.get("mutex_acquired"))
            and abandoned.process.returncode == 23
            and primary.text() == payload
        ),
        "abandoned_exit_code": abandoned.process.returncode,
        "recovered_exact": primary.text() == payload,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def _short_route_suitability_check(target: _NativeTarget) -> dict[str, Any]:
    selected: list[str] = []

    class ObservedClipboard(ClipboardPasteInjector):
        def inject(self, text: str) -> None:
            selected.append("native_paste")
            super().inject(text)

        def inject_targeted(self, text: str, focus_target: object) -> None:
            selected.append("native_paste")
            super().inject_targeted(text, focus_target)

        def inject_checked(self, text: str, focus_target: object) -> None:
            selected.append("native_paste")
            super().inject_checked(text, focus_target)

    class ObservedKeystroke(WindowsSendInputInjector):
        def inject(self, text: str) -> None:
            selected.append("unicode_sendinput")
            super().inject(text)

        def inject_targeted(self, text: str, focus_target: object) -> None:
            selected.append("native_replace")
            super().inject_targeted(text, focus_target)

        def inject_checked(self, text: str, focus_target: object) -> None:
            selected.append("unicode_sendinput_checked")
            super().inject_checked(text, focus_target)

    router = RoutingInjector(
        default_name="clipboard",
        injectors={
            "clipboard": ObservedClipboard(
                restore_previous=True,
                paste_delay_s=0.04,
                paste_min_delay_s=0.04,
            ),
            "keystroke": ObservedKeystroke(),
        },
        process_name_fn=lambda: "dcent-injection-test-target.exe",
        short_text_keystroke_chars=48,
    )
    cases = [
        ("unicode", "café 日本語 👨‍👩‍👧‍👦 e\u0301", "native_replace"),
        ("lf", "line one\nline two", "native_paste"),
        ("crlf_tab", "A\tB\r\nC\nD", "native_paste"),
        ("control", "control:\x01 end", "native_paste"),
    ]
    case_results: list[dict[str, Any]] = []
    for name, payload, expected_route in cases:
        selected.clear()
        target.clear()
        target.focus()
        guarded_inject(router, payload, expected_hwnd=target.hwnd)
        observed = target.text()
        actual_route = selected[-1] if selected else "none"
        case_results.append(
            {
                "case": name,
                "expected_route": expected_route,
                "actual_route": actual_route,
                "route_correct": actual_route == expected_route,
                "target_exact": observed == payload,
                "target_observed_escaped": observed.encode("unicode_escape").decode("ascii"),
                "note": (
                    "Native EDIT controls canonicalize or interpret this clipboard payload; "
                    "DCENT_Voice did not rewrite the source string."
                    if observed != payload
                    else "exact"
                ),
            }
        )
    return {
        "success": all(case["route_correct"] for case in case_results)
        and case_results[0]["target_exact"],
        "cases": case_results,
        "global_fallback_limitation": (
            "Non-EDIT controls have no universal HWND-bound text API. DCENT_Voice performs a "
            "final focus recheck immediately before SendInput/Ctrl+V, but Windows provides no "
            "atomic target binding for that fallback."
        ),
    }


def _unicode_sendinput_probe(target: _NativeTarget, *, runs: int = 1) -> dict[str, Any]:
    """Correctness probe for the global KEYEVENTF_UNICODE fallback.

    This deliberately bypasses RoutingInjector so the result cannot accidentally
    measure the preferred target-bound EDIT backend.
    """
    layout = target.request("set_keyboard_layout", layout="00011009")
    payload = "KEYEVENTF_UNICODE: 日本語 👨‍👩‍👧‍👦 e\u0301 🇨🇦"
    timings_ms: list[float] = []
    exact = 0
    stable_focus = 0
    injector = WindowsSendInputInjector()
    for _ in range(runs):
        target.clear()
        target.focus()
        before = _foreground_window() == target.hwnd
        started = time.perf_counter()
        injector.inject(payload)
        after = _foreground_window() == target.hwnd
        # SendInput queues packets to the target thread. Leave a short bounded
        # quiescence window before the helper acknowledgement so no Unicode packet
        # can spill into the next harness-owned target during rapid process stress.
        time.sleep(0.05)
        observed = target.text()
        timings_ms.append((time.perf_counter() - started) * 1000.0)
        exact += int(observed == payload)
        stable_focus += int(before and after)
    return {
        "success": bool(layout.get("layout_loaded")) and exact == runs and stable_focus == runs,
        "probe": "unicode_sendinput_probe",
        "native_api": "global SendInput with KEYEVENTF_UNICODE",
        "keyboard_layout_requested": "00011009 (Canadian Multilingual Standard)",
        "keyboard_layout_active_hkl": layout.get("active_keyboard_layout_hkl"),
        "runs": runs,
        "success_count": exact,
        "stable_focus_count": stable_focus,
        "payload_utf16_units": len(payload.encode("utf-16-le")) // 2,
        "latency_ms": {
            "p50": round(_percentile(timings_ms, 0.50), 3),
            "p95": round(_percentile(timings_ms, 0.95), 3),
            "p99": round(_percentile(timings_ms, 0.99), 3),
        },
        "limitation": (
            "Stable-focus correctness probe only. This is the global-input fallback, not the "
            "focus-safe target-bound native_replace path; SendInput cannot atomically bind an HWND."
        ),
    }


def _focus_guard_check(primary: _NativeTarget, distractor: _NativeTarget) -> dict[str, Any]:
    primary.clear()
    distractor.focus()
    rejected = False
    error = ""
    try:
        guarded_inject(
            _router("keystroke", paste_delay_s=0),
            "must-not-land",
            expected_hwnd=primary.hwnd,
        )
    except FocusGuardError as exc:
        rejected = True
        error = str(exc)
    mismatch_safe = rejected and primary.text() == "" and distractor.text() == ""

    primary.clear()
    distractor.clear()
    primary.focus()

    def steal_during_route_selection() -> str:
        distractor.focus()
        return "dcent-injection-test-target.exe"

    race_payload = "FOCUS-RACE-TARGET-BOUND"
    guarded_inject(
        _router(
            "keystroke",
            paste_delay_s=0,
            process_name_fn=steal_during_route_selection,
        ),
        race_payload,
        expected_hwnd=primary.hwnd,
    )
    primary_value = primary.text()
    distractor_value = distractor.text()
    race_safe = primary_value == race_payload and distractor_value == ""

    primary.clear()
    distractor.clear()
    primary.focus()
    clipboard_race_payload = "FOCUS-RACE-CLIPBOARD-TARGET-BOUND-" + ("x" * 48)
    guarded_inject(
        _router(
            "clipboard",
            paste_delay_s=0.04,
            process_name_fn=steal_during_route_selection,
        ),
        clipboard_race_payload,
        expected_hwnd=primary.hwnd,
    )
    clipboard_primary_value = primary.text()
    clipboard_distractor_value = distractor.text()
    clipboard_race_safe = (
        clipboard_primary_value == clipboard_race_payload and clipboard_distractor_value == ""
    )
    return {
        "success": mismatch_safe and race_safe and clipboard_race_safe,
        "preexisting_mismatch_rejected": rejected,
        "preexisting_mismatch_error": error,
        "native_replace_selection_race_target_bound": race_safe,
        "native_replace_primary_exact": primary_value == race_payload,
        "native_replace_distractor_received_nothing": distractor_value == "",
        "native_paste_selection_race_target_bound": clipboard_race_safe,
        "native_paste_distractor_received_nothing": clipboard_distractor_value == "",
    }


def _clipboard_contention_check(target: _NativeTarget) -> dict[str, Any]:
    target.clear()
    focus_target = capture_target_from_hwnd(target.hwnd)
    if focus_target is None or not focus_target.supports_edit_messages:
        raise RuntimeError("private native EDIT target could not be captured")
    # The helper is a separate process, so this exercises genuine global
    # clipboard contention rather than same-process Win32 re-entry semantics.
    held = target.request("hold_clipboard")
    started = time.perf_counter()
    payload = "clipboard-busy-native-fallback"
    injected = False
    delivery = ""
    error = ""
    try:
        decision = _router(
            "clipboard", paste_delay_s=0, timeout_s=0.05
        ).inject_into_target_with_decision(
            payload,
            focus_target,
        )
        delivery = decision.delivery
        injected = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    time.sleep(0.30)
    observed = target.text()
    return {
        "success": (
            bool(held.get("clipboard_held"))
            and injected
            and delivery == "native_replace"
            and observed == payload
            and elapsed_ms < 500.0
        ),
        "rejected": False,
        "clipboard_held": bool(held.get("clipboard_held")),
        "native_fallback_exact": observed == payload,
        "delivery": delivery,
        "observed": observed,
        "bounded_ms": round(elapsed_ms, 3),
        "error": error,
    }


def _target_timeout_check(target: _NativeTarget) -> dict[str, Any]:
    started = time.perf_counter()
    timeout_rejected = False
    timeout_error = ""
    try:
        target.request("stall", timeout_s=0.05)
    except TargetTimeoutError as exc:
        timeout_rejected = True
        timeout_error = str(exc)
    timeout_ms = (time.perf_counter() - started) * 1000.0
    time.sleep(0.30)

    target.process.terminate()
    target.process.wait(timeout=2.0)
    started = time.perf_counter()
    failure_rejected = False
    failure_error = ""
    try:
        target.request("read", timeout_s=0.10)
    except (RuntimeError, TargetTimeoutError) as exc:
        failure_rejected = True
        failure_error = str(exc)
    failure_ms = (time.perf_counter() - started) * 1000.0
    return {
        "success": (
            timeout_rejected and timeout_ms < 200.0 and failure_rejected and failure_ms < 500.0
        ),
        "timeout_rejected": timeout_rejected,
        "timeout_bounded_ms": round(timeout_ms, 3),
        "timeout_error": timeout_error,
        "process_failure_rejected": failure_rejected,
        "process_failure_bounded_ms": round(failure_ms, 3),
        "process_failure_error": failure_error,
    }


def _process_is_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, int(pid))
    if not handle:
        return False
    try:
        return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _parent_binding_check(root: Path) -> dict[str, Any]:
    forged = root / "forged"
    forged.mkdir()
    _restrict_directory_to_current_user(forged)
    ready = forged / "ready.json"
    contract = forged / "contract.json"
    _write_json(
        contract,
        {
            "schema_version": 1,
            "token": "a" * 32,
            "ready": str(ready),
            "request": str(forged / "request.json"),
            "response": str(forged / "response.json"),
            "error": str(forged / "error.json"),
            "title": "DCENT forged parent must fail",
            "parent_pid": 99_999_999,
        },
    )
    forged_process = subprocess.run(
        _child_command(contract),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_child_environment(),
        timeout=5.0,
        check=False,
    )

    orphan_root = root / "orphan-parent"
    orphan_root.mkdir()
    _restrict_directory_to_current_user(orphan_root)
    orphan_output = orphan_root / "child.json"
    parent = start_owned_process(
        _app_command("injection-test-orphan-parent", orphan_root, orphan_output),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )
    try:
        parent_stdout, parent_stderr = parent.communicate(timeout=8.0)
        if not orphan_output.is_file():
            raise RuntimeError(
                "Orphan-parent probe did not publish its child PID "
                f"(exit={parent.returncode}, stdout={parent_stdout!r}, stderr={parent_stderr!r})."
            )
        child_payload = _wait_until(
            lambda: _read_json(orphan_output) if orphan_output.is_file() else None,
            timeout_s=2.0,
            description="orphan helper PID",
        )
        child_pid = int(child_payload["pid"])
        orphan_exited = bool(
            _wait_until(
                lambda: not _process_is_alive(child_pid),
                timeout_s=2.0,
                description="orphan helper exit",
            )
        )
        return {
            "success": forged_process.returncode != 0 and not ready.exists() and orphan_exited,
            "forged_parent_rejected": forged_process.returncode != 0 and not ready.exists(),
            "orphan_child_pid": child_pid,
            "orphan_exited": orphan_exited,
            "orphan_parent_exit_code": parent.returncode,
        }
    finally:
        terminate_owned_process(parent, grace_s=0.0, kill_s=2.0)


def run_windows_injection_self_test(*, runs: int = 5) -> dict[str, Any]:
    if platform.system() != "Windows":
        raise InjectionSelfTestStageError(
            "platform_validation", RuntimeError("injection-self-test requires Windows.")
        )
    if runs < 1 or runs > 100:
        raise InjectionSelfTestStageError(
            "runs_validation", ValueError("runs must be between 1 and 100")
        )
    original = _self_test_stage(
        "initial_clipboard_snapshot", lambda: snapshot_clipboard(timeout_s=0.75)
    )
    if original is None:
        raise RuntimeError(
            "The current clipboard contains no safely restorable supported format; "
            "self-test refused to overwrite it."
        )
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="dcent-voice-injection-") as directory:
        root = Path(directory)
        primary = _self_test_stage("primary_target_start", lambda: _NativeTarget(root, "primary"))
        distractor: _NativeTarget | None = None
        timeout_target: _NativeTarget | None = None
        abandoned_target: _NativeTarget | None = None
        try:
            results["routes"] = [
                _self_test_stage(
                    "route_native_replace",
                    lambda: _measure_native_backend(
                        primary,
                        backend="native_replace",
                        text="Short Unicode: café 日本語 🔥",
                        runs=runs,
                        paste_delay_s=0,
                    ),
                ),
                _self_test_stage(
                    "route_native_paste",
                    lambda: _measure_native_backend(
                        primary,
                        backend="native_paste",
                        text="Long Unicode clipboard payload: naïve 日本語 🔥 " + ("x" * 64),
                        runs=runs,
                    ),
                ),
            ]
            results["clipboard_restore"] = _self_test_stage(
                "clipboard_restore", lambda: _clipboard_restore_check(primary)
            )
            results["private_clipboard_fail_closed"] = _self_test_stage(
                "private_clipboard_fail_closed",
                lambda: _private_clipboard_fail_closed_check(primary),
            )
            results["concurrent_serialization"] = _self_test_stage(
                "concurrent_serialization", lambda: _concurrency_check(primary)
            )
            results["cross_process_serialization"] = _self_test_stage(
                "cross_process_serialization",
                lambda: _cross_process_serialization_check(primary),
            )
            distractor = _self_test_stage(
                "distractor_target_start", lambda: _NativeTarget(root, "distractor")
            )
            results["focus_guard"] = _self_test_stage(
                "focus_guard", lambda: _focus_guard_check(primary, distractor)
            )
            distractor.close()
            distractor = None
            results["clipboard_contention"] = _self_test_stage(
                "clipboard_contention", lambda: _clipboard_contention_check(primary)
            )
            abandoned_target = _self_test_stage(
                "abandoned_target_start", lambda: _NativeTarget(root, "abandoned")
            )
            results["abandoned_mutex"] = _self_test_stage(
                "abandoned_mutex", lambda: _abandoned_mutex_check(primary, abandoned_target)
            )
            timeout_target = _self_test_stage(
                "timeout_target_start", lambda: _NativeTarget(root, "timeout")
            )
            results["target_timeout"] = _self_test_stage(
                "target_timeout", lambda: _target_timeout_check(timeout_target)
            )
            results["short_route_suitability"] = _self_test_stage(
                "short_route_suitability", lambda: _short_route_suitability_check(primary)
            )
            results["unicode_sendinput_probe"] = _self_test_stage(
                "unicode_sendinput_probe", lambda: _unicode_sendinput_probe(primary)
            )
            results["parent_binding"] = _self_test_stage(
                "parent_binding", lambda: _parent_binding_check(root)
            )
        finally:
            if abandoned_target is not None:
                abandoned_target.close()
            if timeout_target is not None:
                timeout_target.close()
            if distractor is not None:
                distractor.close()
            primary.close()
            _self_test_stage(
                "final_clipboard_restore",
                lambda: restore_clipboard(original, timeout_s=0.75),
            )

    checks = [
        *(route["success_count"] == route["runs"] for route in results["routes"]),
        results["clipboard_restore"]["success"],
        results["private_clipboard_fail_closed"]["success"],
        results["concurrent_serialization"]["success"],
        results["cross_process_serialization"]["success"],
        results["focus_guard"]["success"],
        results["clipboard_contention"]["success"],
        results["abandoned_mutex"]["success"],
        results["target_timeout"]["success"],
        results["short_route_suitability"]["success"],
        results["unicode_sendinput_probe"]["success"],
        results["parent_binding"]["success"],
    ]
    diagnostics: dict[str, Any] | None = None
    if not all(checks):
        failed_stages = [
            route["route"] for route in results["routes"] if route["success_count"] != route["runs"]
        ]
        failed_stages.extend(
            name
            for name, result in results.items()
            if isinstance(result, dict) and result.get("success") is False
        )
        diagnostics = {
            "stage": "aggregate_verification",
            "error": "One or more exact postconditions failed.",
            "failed_stages": failed_stages,
        }
    return {
        "schema_version": 2,
        "status": "pass" if all(checks) else "fail",
        "scope": "real Windows OS injection into a harness-owned native EDIT control",
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": platform.platform(),
        "diagnostic": diagnostics,
        "excludes": [
            "microphone capture",
            "ASR and transcription latency",
            "browser/editor/terminal/Electron application compatibility",
            "elevated and cross-integrity-level targets",
        ],
        **results,
    }


def run_self_test_command(*, output_json: Path | None, runs: int) -> int:
    try:
        report = run_windows_injection_self_test(runs=runs)
    except Exception as exc:
        stage = exc.stage if isinstance(exc, InjectionSelfTestStageError) else "self_test_command"
        report = {
            "schema_version": 2,
            "status": "error",
            "scope": "real Windows OS injection into a harness-owned native EDIT control",
            "diagnostic": {
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
            },
            "error": f"{type(exc).__name__}: {exc}",
        }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if output_json is None:
        print(rendered)
    else:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.get("status") == "pass" else 1


def run_target_command(contract_path: Path) -> int:
    if platform.system() != "Windows":
        return 2
    try:
        return _run_native_target(contract_path.resolve())
    except Exception as exc:
        with contextlib.suppress(Exception):
            contract = _read_json(contract_path.resolve())
            _write_json(
                Path(str(contract["error"])),
                {"error": f"{type(exc).__name__}: {exc}"},
            )
        return 1


def run_orphan_parent_command(root: Path, output: Path) -> int:
    """Private test parent that exits without running helper cleanup."""
    if platform.system() != "Windows":
        return 2
    resolved_root = root.resolve()
    resolved_output = output.resolve()
    if resolved_output.parent != resolved_root:
        return 2
    target = _NativeTarget(resolved_root, "child")
    _write_json(resolved_output, {"pid": target.pid})
    os._exit(0)


def _run_native_target(contract_path: Path) -> int:
    contract = _read_json(contract_path)
    token = str(contract["token"])
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("Invalid injection helper nonce.")
    ready = Path(str(contract["ready"])).resolve()
    request_path = Path(str(contract["request"])).resolve()
    response_path = Path(str(contract["response"])).resolve()
    error_path = Path(str(contract["error"])).resolve()
    contract_files = (ready, request_path, response_path, error_path)
    if not all(path.parent == contract_path.parent for path in contract_files):
        raise ValueError("Injection helper contract paths must share one private directory.")
    claimed_parent_pid = int(contract.get("parent_pid", 0))
    actual_parent_pid = _actual_parent_pid()
    if claimed_parent_pid <= 0 or claimed_parent_pid != actual_parent_pid:
        raise PermissionError(
            f"Injection helper parent mismatch: claimed {claimed_parent_pid}, "
            f"kernel reports {actual_parent_pid}."
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    parent_handle = kernel32.OpenProcess(0x00100000, False, claimed_parent_pid)
    if not parent_handle:
        raise PermissionError("Injection helper could not bind a synchronize handle to its parent.")
    wndproc_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.SetWindowTextW.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
    user32.LoadKeyboardLayoutW.restype = wintypes.HANDLE
    user32.ActivateKeyboardLayout.argtypes = [wintypes.HANDLE, wintypes.UINT]
    user32.ActivateKeyboardLayout.restype = wintypes.HANDLE
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL

    wm_destroy = 0x0002
    wm_close = 0x0010
    wm_size = 0x0005
    wm_timer = 0x0113
    wm_destroyclipboard = 0x0307
    ws_overlappedwindow = 0x00CF0000
    ws_visible = 0x10000000
    ws_child = 0x40000000
    ws_border = 0x00800000
    ws_vscroll = 0x00200000
    es_left = 0x0000
    es_multiline = 0x0004
    es_autovscroll = 0x0040
    sw_show = 5
    state: dict[str, Any] = {
        "edit": 0,
        "sequence": 0,
        "shutdown": False,
        "cross_process": None,
        "private_handle": 0,
        "private_allocations": 0,
        "private_frees": 0,
        "private_free_failures": 0,
    }

    def respond(request: dict[str, Any]) -> None:
        sequence = int(request.get("sequence", -1))
        sequence_response = response_path.with_name(
            f"{response_path.stem}-{sequence}{response_path.suffix}"
        )
        operation = str(request.get("op", ""))
        if sequence <= state["sequence"] or request.get("token") != token:
            return
        state["sequence"] = sequence
        edit = int(state["edit"])
        extra_response: dict[str, Any] = {}
        if operation == "clear":
            user32.SetWindowTextW(edit, "")
        elif operation == "focus":
            user32.ShowWindow(state["window"], sw_show)
            user32.SetForegroundWindow(state["window"])
            user32.SetFocus(edit)
        elif operation == "set_keyboard_layout":
            layout_name = str(request.get("layout", ""))
            if len(layout_name) != 8 or any(
                character not in "0123456789abcdefABCDEF" for character in layout_name
            ):
                raise ValueError("Keyboard layout must be an eight-digit hexadecimal KLID.")
            keyboard_layout = user32.LoadKeyboardLayoutW(layout_name, 0x00000001)
            if not keyboard_layout:
                raise ctypes.WinError(ctypes.get_last_error())
            user32.ActivateKeyboardLayout(keyboard_layout, 0)
            extra_response = {
                "layout_loaded": True,
                "active_keyboard_layout_hkl": f"0x{int(keyboard_layout):X}",
            }
        elif operation == "seed_private_clipboard":
            payload = bytes.fromhex(str(request.get("payload_hex", "")))
            if not payload or len(payload) > 4096:
                raise ValueError("Private clipboard probe payload must be 1..4096 bytes.")
            if state["private_handle"]:
                raise RuntimeError("A prior private clipboard probe is still owned.")
            private_handle = clipboard_mod._global_alloc_bytes(payload)
            with clipboard_mod._opened_clipboard(
                0.75,
                owner_hwnd=int(state["window"]),
                stage="self_test_seed_private_format",
            ):
                if not user32.EmptyClipboard():
                    clipboard_mod._kernel32.GlobalFree(private_handle)
                    raise OSError(
                        f"private EmptyClipboard failed: {ctypes.WinError(ctypes.get_last_error())}"
                    )
                published_handle = user32.SetClipboardData(CF_PRIVATEFIRST, private_handle)
                if not published_handle:
                    clipboard_mod._kernel32.GlobalFree(private_handle)
                    raise OSError(
                        "private SetClipboardData failed: "
                        f"{ctypes.WinError(ctypes.get_last_error())}"
                    )
                state["private_handle"] = int(published_handle)
                state["private_allocations"] += 1
            extra_response = {
                "private_allocations": state["private_allocations"],
                "private_frees": state["private_frees"],
                "private_allocations_outstanding": state["private_allocations"]
                - state["private_frees"],
            }
        elif operation == "clear_private_clipboard":
            with clipboard_mod._opened_clipboard(
                0.75,
                owner_hwnd=int(state["window"]),
                stage="self_test_clear_private_format",
            ):
                if not user32.EmptyClipboard():
                    raise OSError(
                        f"private cleanup EmptyClipboard failed: "
                        f"{ctypes.WinError(ctypes.get_last_error())}"
                    )
            extra_response = {
                "private_allocations": state["private_allocations"],
                "private_frees": state["private_frees"],
                "private_allocations_outstanding": state["private_allocations"]
                - state["private_frees"],
                "private_free_failures": state["private_free_failures"],
            }
        elif operation == "private_clipboard_stats":
            extra_response = {
                "private_allocations": state["private_allocations"],
                "private_frees": state["private_frees"],
                "private_allocations_outstanding": state["private_allocations"]
                - state["private_frees"],
                "private_free_failures": state["private_free_failures"],
            }
        elif operation == "resource_counts":
            process = kernel32.OpenProcess(0x1000, False, os.getpid())
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            handle_count = wintypes.DWORD()
            try:
                if not kernel32.GetProcessHandleCount(process, ctypes.byref(handle_count)):
                    raise ctypes.WinError(ctypes.get_last_error())
                extra_response = {
                    # Exclude this short-lived measurement handle from the stable count.
                    "process_handles": int(handle_count.value) - 1,
                    "gdi_handles": int(user32.GetGuiResources(process, 0)),
                }
            finally:
                kernel32.CloseHandle(process)
        elif operation == "stall":
            # Deliberately exceed the parent's 50 ms timeout to prove bounded
            # failure without hanging the desktop or touching another app.
            time.sleep(0.25)
        elif operation == "prepare_cross_process":
            start = Path(str(request.get("start", ""))).resolve()
            done = Path(str(request.get("done", ""))).resolve()
            if start.parent != contract_path.parent or done.parent != contract_path.parent:
                raise ValueError("Cross-process marker paths escaped the helper directory.")
            state["cross_process"] = {
                "payload": str(request.get("payload", "")),
                "start": start,
                "done": done,
                "launched": False,
            }
        elif operation == "abandon_clipboard_mutex":
            from dcent_voice.inject.clipboard import _clipboard_transaction

            transaction = _clipboard_transaction(1.0)
            transaction.__enter__()
            _write_json(
                sequence_response,
                {
                    "token": token,
                    "sequence": sequence,
                    "text": "",
                    "focused": True,
                    "mutex_acquired": True,
                },
            )
            os._exit(23)
        if operation == "hold_clipboard":
            try:
                with clipboard_mod._opened_clipboard(
                    0.75,
                    owner_hwnd=int(state["window"]),
                    stage="self_test_hold_clipboard",
                ):
                    held = True
                    _write_json(
                        sequence_response,
                        {
                            "token": token,
                            "sequence": sequence,
                            "text": "",
                            "focused": int(user32.GetForegroundWindow() or 0)
                            == int(state["window"]),
                            "clipboard_held": held,
                        },
                    )
                    time.sleep(0.25)
            except clipboard_mod.ClipboardOpenTimeout:
                held = False
                _write_json(
                    sequence_response,
                    {
                        "token": token,
                        "sequence": sequence,
                        "text": "",
                        "focused": int(user32.GetForegroundWindow() or 0) == int(state["window"]),
                        "clipboard_held": held,
                    },
                )
            return
        length = int(user32.GetWindowTextLengthW(edit))
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(edit, buffer, len(buffer))
        _write_json(
            sequence_response,
            {
                "token": token,
                "sequence": sequence,
                "text": buffer.value,
                "focused": int(user32.GetForegroundWindow() or 0) == int(state["window"]),
                **extra_response,
            },
        )
        if operation == "shutdown":
            state["shutdown"] = True
            user32.PostMessageW(state["window"], wm_close, 0, 0)

    @wndproc_type
    def wndproc(hwnd, message, wparam, lparam):
        if message == wm_destroyclipboard and state["private_handle"]:
            private_handle = state["private_handle"]
            state["private_handle"] = 0
            if clipboard_mod._kernel32.GlobalFree(private_handle):
                state["private_free_failures"] += 1
            else:
                state["private_frees"] += 1
            return 0
        if message == wm_size and state["edit"]:
            width = int(lparam) & 0xFFFF
            height = (int(lparam) >> 16) & 0xFFFF
            user32.MoveWindow(state["edit"], 8, 8, max(1, width - 16), max(1, height - 16), True)
            return 0
        if message == wm_timer:
            next_sequence = int(state["sequence"]) + 1
            next_request = request_path.with_name(
                f"{request_path.stem}-{next_sequence}{request_path.suffix}"
            )
            if next_request.is_file():
                request_payload: dict[str, Any] | None = None
                try:
                    request_payload = _read_json(next_request)
                except PermissionError:
                    # Antivirus/indexers can briefly deny a newly replaced file.
                    # The immutable sequence file remains present for the next
                    # timer tick, so retry without poisoning the helper contract.
                    pass
                except Exception as exc:
                    _write_json(
                        error_path,
                        {"error": f"target callback {type(exc).__name__}: {exc}"},
                    )
                if request_payload is not None:
                    try:
                        respond(request_payload)
                    except Exception as exc:
                        _write_json(
                            error_path,
                            {"error": f"target callback {type(exc).__name__}: {exc}"},
                        )
            cross_process = state.get("cross_process")
            if (
                isinstance(cross_process, dict)
                and not cross_process["launched"]
                and cross_process["start"].is_file()
            ):
                cross_process["launched"] = True

                def run_cross_process_injection() -> None:
                    try:
                        from dcent_voice.inject.windows_focus import WindowsFocusTarget

                        thread_id = int(user32.GetWindowThreadProcessId(state["window"], None))
                        bound_target = WindowsFocusTarget(
                            top_hwnd=int(state["window"]),
                            focus_hwnd=int(state["edit"]),
                            thread_id=thread_id,
                            class_name="Edit",
                        )
                        ClipboardPasteInjector(
                            restore_previous=True,
                            paste_delay_s=0.04,
                            paste_min_delay_s=0.04,
                        ).inject_targeted(cross_process["payload"], bound_target)
                        _write_json(cross_process["done"], {"ok": True})
                    except Exception as exc:
                        _write_json(
                            cross_process["done"],
                            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                        )

                threading.Thread(
                    target=run_cross_process_injection,
                    name="dcent-cross-process-inject",
                    daemon=True,
                ).start()
            return 0
        if message == wm_close:
            user32.DestroyWindow(hwnd)
            return 0
        if message == wm_destroy:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = f"DCENTVoiceInjectionTarget{token}"
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = wndproc
    window_class.hInstance = instance
    window_class.hbrBackground = ctypes.c_void_p(6)
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise ctypes.WinError(ctypes.get_last_error())
    window = user32.CreateWindowExW(
        0,
        class_name,
        str(contract["title"]),
        ws_overlappedwindow | ws_visible,
        100,
        100,
        640,
        240,
        None,
        None,
        instance,
        None,
    )
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    edit = user32.CreateWindowExW(
        0,
        "EDIT",
        "",
        ws_child | ws_visible | ws_border | ws_vscroll | es_left | es_multiline | es_autovscroll,
        8,
        8,
        608,
        184,
        window,
        None,
        instance,
        None,
    )
    if not edit:
        raise ctypes.WinError(ctypes.get_last_error())
    state.update(window=int(window), edit=int(edit))
    user32.SetTimer(window, 1, 10, None)
    user32.ShowWindow(window, sw_show)
    user32.SetForegroundWindow(window)
    user32.SetFocus(edit)
    _write_json(ready, {"token": token, "hwnd": int(window), "pid": os.getpid()})

    def monitor_parent() -> None:
        kernel32.WaitForSingleObject(parent_handle, 0xFFFFFFFF)
        user32.PostMessageW(window, wm_close, 0, 0)
        kernel32.CloseHandle(parent_handle)

    threading.Thread(target=monitor_parent, name="dcent-parent-watch", daemon=True).start()

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0
