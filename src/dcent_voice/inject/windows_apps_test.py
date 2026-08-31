# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Real Windows application injection matrix for isolated scratch targets only.

This command launches fresh documents/profiles that it owns.  It never attaches to an
existing user window and never sends dictated text to a command shell.  The console case
starts a fixed PowerShell key-capture program before any text is injected.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import platform
import secrets
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dcent_voice.inject.clipboard import (
    get_clipboard_text,
    restore_clipboard,
    snapshot_clipboard,
)
from dcent_voice.inject.router import RoutingInjector
from dcent_voice.inject.windows_focus import (
    capture_foreground_target,
    capture_target_from_hwnd,
    read_targeted_edit_state,
    send_targeted_edit_text,
)
from dcent_voice.inject.windows_uia import (
    _control_from_hwnd,
    _first_page_editable,
    _walk_named_editable,
    _walk_root_web_area,
    _walk_search_host,
    focus_page_editable,
    focused_page_search_field,
    looks_like_navigated_url,
    looks_like_search_chrome_label,
    read_focused_editable,
    set_focused_editable_text,
)
from dcent_voice.util import paths
from dcent_voice.util.owned_process import (
    owned_process_contains_pid,
    start_owned_process,
    terminate_owned_process,
)


def _require_source_checkout() -> Path:
    """Fail loudly when this development harness is reached from a frozen build.

    The scratch-document fixtures live under ``eval/``, which is never part of
    the shipped payload; without this guard a packaged build would silently
    resolve fixture paths that cannot exist.
    """
    if paths.is_frozen():
        raise RuntimeError(
            "The Windows injection app matrix is a development-only harness that needs the "
            "repository eval/ fixtures; it is not part of the packaged DCENT_Voice "
            "application. Run it from a source checkout instead."
        )
    return paths.source_root()


_VK_CONTROL = 0x11
_VK_A = 0x41
_VK_C = 0x43
_VK_BACK = 0x08
_VK_R = 0x52
_VK_S = 0x53
_VK_OEM_2 = 0xBF
_VK_ESCAPE = 0x1B
_SW_RESTORE = 9
_WM_CLOSE = 0x0010
_CREATE_NEW_CONSOLE = 0x00000010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_PREEXISTING_WINDOW_PIDS: set[int] = set()
_OWNED_PROCESS_HANDLES: list[tuple[int, int]] = []


@dataclass
class _Window:
    hwnd: int
    pid: int
    title: str


@dataclass
class _OwnedApp:
    name: str
    executable: Path
    process: subprocess.Popen[Any] | None = None
    window: _Window | None = None
    extra_pids: set[int] = field(default_factory=set)
    _closed: bool = field(default=False, init=False, repr=False)

    def start(
        self, command: Sequence[str | os.PathLike[str]], **kwargs: Any
    ) -> subprocess.Popen[Any]:
        if self.process is not None:
            raise RuntimeError(f"owned app {self.name!r} was already started")
        self.process = start_owned_process(command, **kwargs)
        return self.process

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        pids: set[int] = set()
        if process is not None and process.poll() is None:
            pids.add(process.pid)
        if process is not None:
            pids.update(pid for pid in self.extra_pids if owned_process_contains_pid(process, pid))
        window_owned = bool(
            process is not None
            and self.window is not None
            and owned_process_contains_pid(process, self.window.pid)
        )
        if window_owned and self.window is not None:
            pids.add(self.window.pid)
        handles: list[int] = []
        for pid in sorted(pids):
            if pid > 0 and pid != os.getpid():
                handle = _record_owned_process(pid)
                if handle is not None:
                    handles.append(handle)
        # A window-title match is not ownership proof.  Windows Terminal and
        # Chromium can host multiple callers in one process, so never close a
        # window unless the kernel says its PID belongs to this app's job.
        if window_owned and self.window is not None and _is_window(self.window.hwnd):
            _user32().PostMessageW(self.window.hwnd, _WM_CLOSE, 0, 0)
            deadline = time.monotonic() + 2.0
            while _is_window(self.window.hwnd) and time.monotonic() < deadline:
                time.sleep(0.05)
        if process is not None:
            terminate_owned_process(process, grace_s=2.0, kill_s=5.0)
        if handles:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            for handle in handles:
                kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 5000)


def _record_owned_process(pid: int) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if handle:
        raw_handle = int(handle)
        _OWNED_PROCESS_HANDLES.append((pid, raw_handle))
        return raw_handle
    return None


def _verify_owned_process_cleanup() -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    live: list[int] = []
    seen: set[int] = set()
    tracked_count = len(_OWNED_PROCESS_HANDLES)
    for pid, raw_handle in _OWNED_PROCESS_HANDLES:
        handle = wintypes.HANDLE(raw_handle)
        try:
            if kernel32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0 and pid not in seen:
                live.append(pid)
                seen.add(pid)
        finally:
            kernel32.CloseHandle(handle)
    _OWNED_PROCESS_HANDLES.clear()
    return {
        "success": not live,
        "tracked_handle_count": tracked_count,
        "live_owned_pids": live,
    }


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, wintypes.LPARAM]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPARAM,
    ]
    return user32


def _is_window(hwnd: int) -> bool:
    return bool(hwnd and _user32().IsWindow(hwnd))


def _windows() -> list[_Window]:
    user32 = _user32()
    found: list[_Window] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(_Window(int(hwnd), int(pid.value), buffer.value))
        return True

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    return found


def _wait_window(title_fragment: str, *, timeout_s: float = 15.0) -> _Window:
    return _wait_new_window((title_fragment,), timeout_s=timeout_s)


def _wait_new_window(title_fragments: Sequence[str], *, timeout_s: float = 15.0) -> _Window:
    deadline = time.monotonic() + timeout_s
    fragments = [item.casefold() for item in title_fragments if item.strip()]
    if not fragments:
        raise ValueError("window title fragments are required")
    while time.monotonic() < deadline:
        matches = [
            item
            for item in _windows()
            if item.pid not in _PREEXISTING_WINDOW_PIDS
            and any(fragment in item.title.casefold() for fragment in fragments)
        ]
        if matches:
            return matches[0]
        time.sleep(0.05)
    raise TimeoutError(f"No isolated app window containing {title_fragments!r} appeared.")


def _claim_new_window(window: _Window) -> _Window:
    if window.pid in _PREEXISTING_WINDOW_PIDS:
        raise RuntimeError(
            f"refusing pre-existing PID {window.pid} for scratch window {window.title!r}"
        )
    return window


def _client_wh(hwnd: int) -> tuple[int, int]:
    client = wintypes.RECT()
    if not _user32().GetClientRect(hwnd, ctypes.byref(client)):
        return 0, 0
    return int(client.right - client.left), int(client.bottom - client.top)


def _wait_browser_client_ready(window: _Window, *, timeout_s: float = 8.0) -> None:
    """Live tabs can report a title before the client area is actually laid out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _user32().ShowWindow(window.hwnd, _SW_RESTORE)
        _user32().SetForegroundWindow(window.hwnd)
        width, height = _client_wh(window.hwnd)
        if width >= 600 and height >= 400:
            return
        time.sleep(0.1)
    width, height = _client_wh(window.hwnd)
    raise RuntimeError(f"browser client never became usable: {width}x{height} ({window.title!r})")


def _search_prepare_preserves_autofocus(name: str) -> bool:
    """Google/Wiki/DDG autofocus the page box. Escape / Ctrl+A select the page."""
    return "github" not in name.casefold()


def _page_search_field(window: _Window, names: Sequence[str]) -> Any | None:
    """Focused ComboBox/Edit first — Google's box is often absent from the hwnd walk."""
    focused = focused_page_search_field(names)
    if focused is not None:
        return focused
    root = _control_from_hwnd(window.hwnd)
    if root is None:
        return None
    named = _walk_named_editable(root, names) if names else None
    if named is not None:
        return named
    web = _walk_root_web_area(root)
    return _first_page_editable(web or root)


def _wait_live_search_field(
    window: _Window,
    names: Sequence[str],
    *,
    timeout_s: float = 8.0,
) -> Any | None:
    """Google paints the search box after the window title and client size exist."""
    deadline = time.monotonic() + timeout_s
    match = None
    while time.monotonic() < deadline:
        match = _page_search_field(window, names)
        if match is not None:
            return match
        time.sleep(0.15)
    return match


def _focus(window: _Window, *, timeout_s: float = 3.0, require_edit: bool = False) -> object:
    user32 = _user32()
    user32.ShowWindow(window.hwnd, _SW_RESTORE)
    # A benign ALT transition permits SetForegroundWindow from this test process.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 0x0002, 0)
    deadline = time.monotonic() + timeout_s
    last_class = ""
    while time.monotonic() < deadline:
        user32.SetForegroundWindow(window.hwnd)
        bound = capture_target_from_hwnd(window.hwnd)
        if bound is not None and (not require_edit or bound.supports_edit_messages):
            if int(user32.GetForegroundWindow() or 0) == window.hwnd:
                return bound
            if require_edit:
                return bound
        current = capture_foreground_target()
        if current is not None:
            last_class = current.class_name
            if current.top_hwnd == window.hwnd and (
                not require_edit or current.supports_edit_messages
            ):
                return current
        time.sleep(0.05)
    raise TimeoutError(
        f"Could not focus isolated window {window.title!r} (last foreground class {last_class!r})."
    )


def _hotkey(*keys: int) -> None:
    user32 = _user32()
    for key in keys:
        user32.keybd_event(key, 0, 0, 0)
    for key in reversed(keys):
        user32.keybd_event(key, 0, 0x0002, 0)


def _browser_content_click_offset(client_width: int, client_height: int) -> tuple[int, int]:
    """Click the page body, not Chromium's tab strip or omnibox."""
    width = max(1, int(client_width))
    height = max(1, int(client_height))
    x = max(40, width // 2)
    y = max(220, int(height * 0.55))
    return x, min(y, max(0, height - 16))


def _browser_search_click_offset(
    client_width: int,
    client_height: int,
    *,
    frac: float = 0.46,
) -> tuple[int, int]:
    """Click a typical live search box: below chrome, above the page footer."""
    width = max(1, int(client_width))
    height = max(1, int(client_height))
    x = max(40, width // 2)
    y = max(140, int(height * frac))
    return x, min(y, max(0, height - 16))


def _click_owned_window(
    window: _Window,
    *,
    browser_page: bool = False,
    search_field: bool = False,
    search_frac: float | None = None,
) -> None:
    user32 = _user32()
    client = wintypes.RECT()
    if not user32.GetClientRect(window.hwnd, ctypes.byref(client)):
        raise ctypes.WinError(ctypes.get_last_error())
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(window.hwnd, ctypes.byref(origin)):
        raise ctypes.WinError(ctypes.get_last_error())
    width = int(client.right - client.left)
    height = int(client.bottom - client.top)
    if search_field or search_frac is not None:
        offset_x, offset_y = _browser_search_click_offset(
            width, height, frac=0.46 if search_frac is None else search_frac
        )
    elif browser_page:
        offset_x, offset_y = _browser_content_click_offset(width, height)
    else:
        offset_x = max(40, width // 2)
        offset_y = max(140, height // 2)
    user32.SetCursorPos(origin.x + offset_x, origin.y + offset_y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _click_uia_control(control: Any, *, window: _Window | None = None) -> bool:
    """Click the center of a UIA control if it is below Chromium chrome."""
    rect = getattr(control, "BoundingRectangle", None)
    if rect is None:
        return False
    try:
        left = int(getattr(rect, "left", getattr(rect, "Left", rect[0])))
        top = int(getattr(rect, "top", getattr(rect, "Top", rect[1])))
        width = int(getattr(rect, "width", getattr(rect, "Width", 0)) or 0)
        height = int(getattr(rect, "height", getattr(rect, "Height", 0)) or 0)
        if width == 0 and height == 0:
            width = int(rect[2] - rect[0])
            height = int(rect[3] - rect[1])
    except Exception:
        return False
    if width < 8 or height < 8 or height > 160:
        return False
    x = left + width // 2
    # Tall search hosts include advanced-search chrome; the input is on the first row.
    y = top + (min(max(16, height // 5), 28) if height > 48 else height // 2)
    if window is not None:
        origin = wintypes.POINT(0, 0)
        if _user32().ClientToScreen(window.hwnd, ctypes.byref(origin)) and y < origin.y + 80:
            return False
    user32 = _user32()
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return True


def _select_all(target: object) -> None:
    if bool(getattr(target, "supports_edit_messages", False)):
        focus_target: Any = target
        user32 = _user32()
        user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = wintypes.LPARAM
        user32.SendMessageW(int(focus_target.focus_hwnd), 0x00B1, 0, -1)
    else:
        _hotkey(_VK_CONTROL, _VK_A)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _latencies(values: list[float]) -> dict[str, float]:
    return {
        "minimum": round(min(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
    }


def _version(path: Path) -> str:
    try:
        import win32api

        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = int(info["FileVersionMS"]), int(info["FileVersionLS"])
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return "unknown"


def _case_result(
    name: str,
    decisions: list[dict[str, str]],
    latencies: list[float],
    errors: list[str],
    payload_hashes: list[str],
    requested_runs: int,
    control_transactions: list[dict[str, Any]],
) -> dict[str, Any]:
    deliveries = sorted({item["delivery"] for item in decisions})
    return {
        "case": name,
        "route": deliveries[0] if len(deliveries) == 1 else "mixed",
        "route_decisions": decisions,
        "runs": requested_runs,
        "success_count": len(latencies),
        "exact": not errors,
        "latency_ms": _latencies(latencies) if latencies else None,
        "unique_payload_hashes": len(set(payload_hashes)),
        "sentinel_entropy_bits": 128,
        "control_transactions": _control_transaction_summary(control_transactions),
        "errors": errors,
    }


def _control_transaction_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(item["latency_ms"]) for item in records]
    return {
        "success": bool(records) and all(bool(item["success"]) for item in records),
        "count": len(records),
        "attempts": sum(int(item["attempts"]) for item in records),
        "retry_count": sum(max(0, int(item["attempts"]) - 1) for item in records),
        "latency_ms": _latencies(latencies) if latencies else None,
        "failure_stages": [item["failure_stage"] for item in records if item.get("failure_stage")],
        "records": records,
    }


def _record_control_transaction(
    records: list[dict[str, Any]],
    *,
    stage: str,
    attempts: int,
    started: float,
    expected: str,
    previous_sentinel: str | None,
    observed: str,
    focus_verified: bool,
    selection_verified: bool,
    success: bool,
    failure_stage: str | None = None,
) -> None:
    records.append(
        {
            "stage": stage,
            "attempts": attempts,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "expected_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
            "observed_sha256": hashlib.sha256(observed.encode("utf-8")).hexdigest(),
            "previous_sentinel_absent": not previous_sentinel or previous_sentinel not in observed,
            "focus_verified": focus_verified,
            "selection_verified": selection_verified,
            "success": success,
            "failure_stage": failure_stage,
        }
    )


def _native_edit_control_transaction(
    app: _OwnedApp,
    *,
    expected: str,
    select_all_after: bool,
    previous_sentinel: str | None,
    stage: str,
    records: list[dict[str, Any]],
    attempts_limit: int = 3,
) -> object:
    started = time.perf_counter()
    observed = ""
    focus_verified = False
    selection_verified = False
    for attempt in range(1, attempts_limit + 1):
        target = _focus(app.window, require_edit=True)  # type: ignore[arg-type]
        _select_all(target)
        send_targeted_edit_text(target, expected)
        if select_all_after:
            _select_all(target)
        state = read_targeted_edit_state(target)
        observed = state.text
        expected_end = len(expected.encode("utf-16-le", errors="surrogatepass")) // 2
        expected_selection = (
            (0, expected_end)
            if select_all_after
            else (
                expected_end,
                expected_end,
            )
        )
        selection_verified = (
            state.selection_start,
            state.selection_end,
        ) == expected_selection
        current = capture_foreground_target()
        focus_verified = bool(
            current
            and current.top_hwnd == int(getattr(target, "top_hwnd", 0))
            and current.focus_hwnd == int(getattr(target, "focus_hwnd", 0))
        )
        if (
            observed == expected
            and selection_verified
            and focus_verified
            and (not previous_sentinel or previous_sentinel not in observed)
        ):
            _record_control_transaction(
                records,
                stage=stage,
                attempts=attempt,
                started=started,
                expected=expected,
                previous_sentinel=previous_sentinel,
                observed=observed,
                focus_verified=True,
                selection_verified=True,
                success=True,
            )
            return target
    _record_control_transaction(
        records,
        stage=stage,
        attempts=attempts_limit,
        started=started,
        expected=expected,
        previous_sentinel=previous_sentinel,
        observed=observed,
        focus_verified=focus_verified,
        selection_verified=selection_verified,
        success=False,
        failure_stage=f"{stage}.verification",
    )
    raise AssertionError(
        f"{stage} target-bound control transaction failed after {attempts_limit} attempts"
    )


def _sentinel_payload(kind: str) -> tuple[str, str]:
    sentinel = f"D{secrets.token_hex(16)}"
    if kind == "short":
        payload = f"日🔥e\u0301-{sentinel}"
    elif kind == "lines":
        payload = f"{sentinel}\r\n日本語\t🔥"
    elif kind == "browser_lines":
        payload = f"{sentinel}\n日本語\t🔥"
    elif kind == "long":
        payload = f"{sentinel}-" + ("abcdef0123456789" * 256)
    elif kind == "terminal_long":
        payload = f"{sentinel}-" + ("abcdef0123456789" * 32)
    else:
        raise ValueError(f"unknown sentinel payload kind: {kind}")
    return payload, sentinel


def _verify_baseline(*, app: str, run: int, expected: str, observed: str) -> None:
    if observed != expected:
        raise AssertionError(
            f"{app} run {run} baseline mismatch: expected={expected!r}, observed={observed!r}"
        )


def _verify_delivery(*, app: str, run: int, expected: str, observed: str, sentinel: str) -> None:
    if sentinel not in observed:
        raise AssertionError(
            f"{app} run {run} unique sentinel was not delivered; "
            f"observed={observed.encode('unicode_escape')!r}"
        )
    if observed.count(sentinel) != 1:
        raise AssertionError(f"{app} run {run} sentinel was duplicated")
    if observed != expected:
        raise AssertionError(
            f"{app} run {run} exact mismatch: expected={expected.encode('unicode_escape')!r}, "
            f"observed={observed.encode('unicode_escape')!r}"
        )


def _inject_repeated(
    *,
    app: _OwnedApp,
    router: RoutingInjector,
    payload_kind: str,
    case_name: str,
    runs: int,
    prepare: Any,
    readback: Any,
    clear_after: Any,
) -> dict[str, Any]:
    latencies: list[float] = []
    errors: list[str] = []
    decisions: list[dict[str, str]] = []
    payload_hashes: list[str] = []
    control_transactions: list[dict[str, Any]] = []
    previous_sentinel: str | None = None
    for index in range(runs):
        target = None
        sentinel: str | None = None
        try:
            payload, sentinel = _sentinel_payload(payload_kind)
            payload_hashes.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
            target = prepare(index, payload, previous_sentinel, control_transactions)
            started = time.perf_counter()
            decision = router.inject_into_target_with_decision(payload, target)
            decisions.append(
                {
                    "process_name": decision.process_name,
                    "configured_injector": decision.configured_injector,
                    "resolved_injector": decision.resolved_injector,
                    "delivery": decision.delivery,
                }
            )
            observed = readback(index, payload)
            elapsed = (time.perf_counter() - started) * 1000.0
            _verify_delivery(
                app=app.name, run=index, expected=payload, observed=observed, sentinel=sentinel
            )
            latencies.append(elapsed)
        except Exception as exc:
            errors.append(f"run {index}: {type(exc).__name__}: {exc}")
        finally:
            try:
                clear_after(index, target, sentinel, control_transactions)
            except Exception as exc:
                errors.append(f"run {index} cleanup: {type(exc).__name__}: {exc}")
            if sentinel is not None:
                previous_sentinel = sentinel
    return _case_result(
        case_name,
        decisions,
        latencies,
        errors,
        payload_hashes,
        requested_runs=runs,
        control_transactions=control_transactions,
    )


def _notepad(executable: Path, root: Path, runs: int, router: RoutingInjector) -> dict[str, Any]:
    scratch = root / "notepad-matrix.txt"
    scratch.write_text("prefix selection suffix", encoding="utf-8")
    token = scratch.name
    app = _OwnedApp("notepad", executable)
    try:
        app.start([str(executable), str(scratch)])
        app.window = _claim_new_window(_wait_window(token))

        def make_prepare(baseline: str):
            def prepare(
                index: int,
                _payload: str,
                previous_sentinel: str | None,
                records: list[dict[str, Any]],
            ) -> object:
                return _native_edit_control_transaction(
                    app,
                    expected=baseline,
                    select_all_after=True,
                    previous_sentinel=previous_sentinel,
                    stage=f"{app.name}.prepare.run_{index}",
                    records=records,
                )

            return prepare

        def readback(_index: int, _payload: str) -> str:
            target = capture_foreground_target()
            if target is None:
                raise RuntimeError("Notepad focus target disappeared before readback")
            return read_targeted_edit_state(target).text

        def clear_after(
            index: int,
            target: object | None,
            sentinel: str | None,
            records: list[dict[str, Any]],
        ) -> None:
            if target is None or not _is_window(int(getattr(target, "focus_hwnd", 0))):
                return
            _native_edit_control_transaction(
                app,
                expected="",
                select_all_after=False,
                previous_sentinel=sentinel,
                stage=f"{app.name}.clear.run_{index}",
                records=records,
            )

        cases = [
            _inject_repeated(
                app=app,
                router=router,
                payload_kind="short",
                case_name="unicode_short_selection_replace",
                runs=runs,
                prepare=make_prepare("prefix selection suffix"),
                readback=readback,
                clear_after=clear_after,
            ),
            _inject_repeated(
                app=app,
                router=router,
                payload_kind="lines",
                case_name="crlf_tab_clipboard",
                runs=runs,
                prepare=make_prepare(""),
                readback=readback,
                clear_after=clear_after,
            ),
            _inject_repeated(
                app=app,
                router=router,
                payload_kind="long",
                case_name="long_clipboard",
                runs=runs,
                prepare=make_prepare(""),
                readback=readback,
                clear_after=clear_after,
            ),
        ]
        diagnostics = _native_failure_diagnostics(executable, root, router, runs=min(3, runs))
        return _app_report(app, cases, diagnostics=diagnostics)
    finally:
        app.close()


def _native_failure_diagnostics(
    executable: Path, root: Path, router: RoutingInjector, *, runs: int
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for mode in ("death", "hang"):
        latencies: list[float] = []
        diagnostics: list[str] = []
        errors: list[str] = []
        for index in range(runs):
            scratch = root / f"notepad-{mode}-{index}.txt"
            scratch.write_text("", encoding="utf-8")
            probe = _OwnedApp(f"notepad_{mode}", executable)
            handle = None
            try:
                probe.start([str(executable), str(scratch)])
                probe.window = _claim_new_window(_wait_window(scratch.name))
                target = _focus(probe.window)
                if mode == "death":
                    if probe.process is None:
                        raise RuntimeError("owned death probe has no launched process")
                    terminate_owned_process(probe.process, grace_s=0.0, kill_s=5.0)
                    deadline = time.monotonic() + 3
                    while _is_window(probe.window.hwnd) and time.monotonic() < deadline:
                        time.sleep(0.03)
                else:
                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    ntdll = ctypes.WinDLL("ntdll")
                    kernel32.OpenProcess.restype = wintypes.HANDLE
                    handle = kernel32.OpenProcess(0x0800 | 0x1000, False, probe.window.pid)
                    if not handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    status = int(ntdll.NtSuspendProcess(handle))
                    if status != 0:
                        raise OSError(f"NtSuspendProcess status=0x{status:08X}")
                started = time.perf_counter()
                failure_payload, _sentinel = _sentinel_payload("short")
                try:
                    router.inject_into_target(failure_payload, target)
                    errors.append(f"run {index}: injection unexpectedly succeeded")
                except Exception as exc:
                    latencies.append((time.perf_counter() - started) * 1000.0)
                    diagnostics.append(f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                errors.append(f"run {index}: {type(exc).__name__}: {exc}")
            finally:
                if mode == "hang" and handle:
                    try:
                        ctypes.WinDLL("ntdll").NtResumeProcess(handle)
                    finally:
                        ctypes.WinDLL("kernel32").CloseHandle(handle)
                probe.close()
        report[mode] = {
            "success": not errors and len(latencies) == runs,
            "runs": runs,
            "success_count": len(latencies),
            "latency_ms": _latencies(latencies) if latencies else None,
            "diagnostics": diagnostics,
            "errors": errors,
        }
    return report


def _code(executable: Path, root: Path, runs: int, router: RoutingInjector) -> dict[str, Any]:
    profile = root / "vscode-profile"
    extensions = root / "vscode-extensions"
    scratch = root / "vscode-matrix.txt"
    scratch.write_bytes(b"prefix selection suffix")
    app = _OwnedApp("vscode", executable)
    try:
        app.start(
            [
                str(executable),
                "--user-data-dir",
                str(profile),
                "--extensions-dir",
                str(extensions),
                "--disable-extensions",
                "--skip-welcome",
                "--skip-release-notes",
                "--new-window",
                str(scratch),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app.window = _claim_new_window(_wait_window(scratch.name, timeout_s=25))
        # The Electron window can become visible before Monaco accepts editor
        # commands.  This bounded settle is preparation and is outside the
        # focus-ready injection timing interval.
        time.sleep(1.0)

        def control_empty(
            *,
            previous_sentinel: str | None,
            stage: str,
            records: list[dict[str, Any]],
        ) -> object:
            started = time.perf_counter()
            observed = scratch.read_bytes().decode("utf-8")
            focus_verified = False
            for attempt in range(1, 4):
                _focus(app.window)  # type: ignore[arg-type]
                _click_owned_window(app.window)  # type: ignore[arg-type]
                _hotkey(_VK_CONTROL, _VK_A)
                time.sleep(0.05)
                _hotkey(_VK_CONTROL, _VK_A)
                _hotkey(_VK_BACK)
                _hotkey(_VK_CONTROL, _VK_S)
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    observed = scratch.read_bytes().decode("utf-8")
                    current = capture_foreground_target()
                    focus_verified = bool(
                        current and current.top_hwnd == app.window.hwnd  # type: ignore[union-attr]
                    )
                    if (
                        observed == ""
                        and focus_verified
                        and (not previous_sentinel or previous_sentinel not in observed)
                    ):
                        _record_control_transaction(
                            records,
                            stage=stage,
                            attempts=attempt,
                            started=started,
                            expected="",
                            previous_sentinel=previous_sentinel,
                            observed=observed,
                            focus_verified=True,
                            selection_verified=True,
                            success=True,
                        )
                        return current
                    time.sleep(0.02)
            _record_control_transaction(
                records,
                stage=stage,
                attempts=3,
                started=started,
                expected="",
                previous_sentinel=previous_sentinel,
                observed=observed,
                focus_verified=focus_verified,
                selection_verified=False,
                success=False,
                failure_stage=f"{stage}.saved_file_verification",
            )
            raise AssertionError(f"{stage} VS Code reset transaction failed")

        def prepare(
            index: int,
            _payload: str,
            previous_sentinel: str | None,
            records: list[dict[str, Any]],
        ) -> object:
            control_empty(
                previous_sentinel=previous_sentinel,
                stage=f"{app.name}.prepare.run_{index}",
                records=records,
            )
            target = capture_foreground_target()
            if target is None:
                raise RuntimeError("VS Code focus target unavailable")
            return target

        def readback(_index: int, payload: str) -> str:
            _hotkey(_VK_CONTROL, _VK_S)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                value = scratch.read_bytes().decode("utf-8")
                if value == payload:
                    return value
                time.sleep(0.05)
            return scratch.read_bytes().decode("utf-8")

        def clear_after(
            index: int,
            _target: object | None,
            sentinel: str | None,
            records: list[dict[str, Any]],
        ) -> None:
            control_empty(
                previous_sentinel=sentinel,
                stage=f"{app.name}.clear.run_{index}",
                records=records,
            )

        cases: list[dict[str, Any]] = []
        for name, payload_kind in (
            ("unicode_short", "short"),
            ("crlf_tab", "lines"),
            ("long", "long"),
        ):
            cases.append(
                _inject_repeated(
                    app=app,
                    router=router,
                    payload_kind=payload_kind,
                    case_name=name,
                    runs=runs,
                    prepare=prepare,
                    readback=readback,
                    clear_after=clear_after,
                )
            )
        return _app_report(app, cases)
    finally:
        app.close()


class _PageState:
    def __init__(self, title: str) -> None:
        self.title = title
        self.control_token = secrets.token_urlsafe(32)
        self.value = "prefix selection suffix"
        self.control_generation = 0
        self.control_value = self.value
        self.control_select_all = True
        self.ack_generation = -1
        self.ack_value = ""
        self.ack_active_id = ""
        self.ack_selection_start = -1
        self.ack_selection_end = -1
        self.ack_count = 0
        self.page_ready = False
        self.lock = threading.Lock()

    def request_control(self, *, value: str, select_all: bool) -> int:
        with self.lock:
            self.control_generation += 1
            self.control_value = value
            self.control_select_all = select_all
            self.ack_generation = -1
            self.ack_value = ""
            self.ack_active_id = ""
            self.ack_selection_start = -1
            self.ack_selection_end = -1
            self.ack_count = 0
            return self.control_generation

    def control_payload(self) -> dict[str, Any]:
        with self.lock:
            return {
                "generation": self.control_generation,
                "value": self.control_value,
                "select_all": self.control_select_all,
            }


class _PageHandler(BaseHTTPRequestHandler):
    state: _PageState

    def _authorized(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        return secrets.compare_digest(query.get("token", [""])[0], self.state.control_token)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/control":
            if not self._authorized():
                self._send_json({"error": "forbidden"}, status=403)
                return
            self._send_json(self.state.control_payload())
            return
        if route != "/":
            self.send_error(404)
            return
        title = json.dumps(self.state.title)
        token = json.dumps(self.state.control_token)
        page = f"""<!doctype html><meta charset=utf-8><title>{self.state.title}</title>
<style>
html,body{{margin:0;height:100%;background:#111;color:#eee}}
header{{height:48px;padding:12px 16px;font:16px sans-serif}}
#t{{position:absolute;left:0;top:48px;right:0;bottom:0;width:100%;
height:calc(100% - 48px);box-sizing:border-box;padding:16px;font:18px/1.4 sans-serif}}
</style>
<header>Project notes</header>
<textarea id=t>prefix selection suffix</textarea><script>
document.title={title}; const token={token}; const t=document.getElementById('t');
let generation=0; t.focus(); t.select();
function post(path,body){{return fetch(path+'?token='+encodeURIComponent(token),{{method:'POST',
headers:{{'Content-Type':'application/json;charset=UTF-8'}},body:JSON.stringify(body)}})}}
function send(){{post('/value',{{generation:generation,value:t.value}})}}
function ack(){{return post('/control-ack',{{generation:generation,value:t.value,
  active_id:document.activeElement&&document.activeElement.id,
  selection_start:t.selectionStart,selection_end:t.selectionEnd}})}}
async function control(){{
  try {{
    const response=await fetch('/control?token='+encodeURIComponent(token),{{cache:'no-store'}});
    const command=await response.json();
    if(command.generation!==generation){{
      generation=command.generation; t.value=command.value; t.focus();
      const end=t.value.length;
      if(command.select_all){{t.setSelectionRange(0,end)}}else{{t.setSelectionRange(end,end)}}
      await ack(); setTimeout(ack,30); setTimeout(ack,70);
    }}
  }} catch(_error) {{}}
}}
t.addEventListener('input',send); send(); control(); setInterval(control,20);
</script>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send_json({"error": "forbidden"}, status=403)
            return
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "invalid_json"}, status=400)
            return
        if route == "/value":
            with self.state.lock:
                self.state.page_ready = True
                if int(payload.get("generation", -1)) == self.state.control_generation:
                    self.state.value = str(payload.get("value", ""))
        elif route == "/control-ack":
            with self.state.lock:
                generation = int(payload.get("generation", -1))
                if generation == self.state.control_generation:
                    self.state.ack_generation = generation
                    self.state.ack_value = str(payload.get("value", ""))
                    self.state.ack_active_id = str(payload.get("active_id", ""))
                    self.state.ack_selection_start = int(payload.get("selection_start", -1))
                    self.state.ack_selection_end = int(payload.get("selection_end", -1))
                    self.state.ack_count += 1
                    self.state.value = self.state.ack_value
        else:
            self._send_json({"error": "not_found"}, status=404)
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *args: Any) -> None:
        return


def _wait_browser_page_ready(state: _PageState, *, timeout_s: float = 8.0) -> None:
    """Block until the isolated page script has posted its first heartbeat."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with state.lock:
            if state.page_ready:
                return
        time.sleep(0.05)
    raise RuntimeError("browser page script did not become ready")


def _chromium_hold_args(
    executable: Path,
    profile: Path,
    url: str,
    *,
    live: bool = False,
) -> list[str]:
    args = [
        str(executable),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--disable-session-crashed-bubble",
        "--disable-features=TranslateUI,InfiniteSessionRestore",
        "--force-renderer-accessibility",
        "--window-size=1280,800",
        "--new-window",
    ]
    if not live:
        args.insert(6, "--disable-background-networking")
    args.append(url)
    return args


def _browser_control_transaction(
    state: _PageState,
    *,
    expected: str,
    select_all: bool,
    previous_sentinel: str | None,
    stage: str,
    records: list[dict[str, Any]],
    attempts_limit: int = 3,
    attempt_timeout_s: float = 1.0,
) -> None:
    started = time.perf_counter()
    observed = ""
    focus_verified = False
    selection_verified = False
    failure_stage = f"{stage}.request"
    for attempt in range(1, attempts_limit + 1):
        generation = state.request_control(value=expected, select_all=select_all)
        failure_stage = f"{stage}.ack"
        deadline = time.monotonic() + attempt_timeout_s
        stable_since: float | None = None
        stable_ack_count = 0
        while time.monotonic() < deadline:
            with state.lock:
                ack_generation = state.ack_generation
                observed = state.ack_value
                focus_verified = state.ack_active_id == "t"
                selection_start = state.ack_selection_start
                selection_end = state.ack_selection_end
                server_value = state.value
                ack_count = state.ack_count
            expected_end = len(expected)
            expected_selection = (
                (0, expected_end)
                if select_all
                else (
                    expected_end,
                    expected_end,
                )
            )
            selection_verified = (selection_start, selection_end) == expected_selection
            previous_absent = not previous_sentinel or previous_sentinel not in observed
            if (
                ack_generation == generation
                and observed == expected
                and server_value == expected
                and focus_verified
                and selection_verified
                and previous_absent
            ):
                if stable_since is None:
                    stable_since = time.monotonic()
                    stable_ack_count = ack_count
                if time.monotonic() - stable_since >= 0.05 and ack_count >= stable_ack_count + 2:
                    _record_control_transaction(
                        records,
                        stage=stage,
                        attempts=attempt,
                        started=started,
                        expected=expected,
                        previous_sentinel=previous_sentinel,
                        observed=observed,
                        focus_verified=True,
                        selection_verified=True,
                        success=True,
                    )
                    return
            else:
                stable_since = None
                stable_ack_count = 0
            time.sleep(0.01)
        failure_stage = f"{stage}.verification"
    _record_control_transaction(
        records,
        stage=stage,
        attempts=attempts_limit,
        started=started,
        expected=expected,
        previous_sentinel=previous_sentinel,
        observed=observed,
        focus_verified=focus_verified,
        selection_verified=selection_verified,
        success=False,
        failure_stage=failure_stage,
    )
    raise AssertionError(
        f"{stage} control transaction failed after {attempts_limit} attempts; "
        f"expected={expected!r}, observed={observed!r}, "
        f"focus_verified={focus_verified}, selection_verified={selection_verified}"
    )


def _wait_for_page_value(
    state: _PageState,
    expected: str,
    *,
    timeout_s: float = 2.0,
    stable_s: float = 0.05,
) -> str:
    """Wait for asynchronous browser input and require a short stable result."""
    deadline = time.monotonic() + timeout_s
    stable_since: float | None = None
    observed = ""
    while time.monotonic() < deadline:
        with state.lock:
            observed = state.value
        if observed == expected:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_s:
                return observed
        else:
            stable_since = None
        time.sleep(0.01)
    with state.lock:
        return state.value


def _focus_theft_outcome(
    *,
    refused: bool,
    before: str,
    expected_recovery: str,
    browser_after: str,
    distractor_after: str,
) -> str:
    """Classify recovery without confusing it with wrong-target insertion."""
    if distractor_after:
        return "wrong_target"
    if refused:
        return "refused" if browser_after == before else "mutated_after_refusal"
    if browser_after == expected_recovery:
        return "recovered"
    if browser_after == before:
        return "not_delivered"
    return "unexpected_target_state"


def _browser(
    name: str,
    executable: Path,
    root: Path,
    runs: int,
    router: RoutingInjector,
) -> dict[str, Any]:
    token = f"DCENT-{name}-{os.getpid()}-{time.time_ns()}"
    state = _PageState(token)
    handler = type(f"{name.title()}Handler", (_PageHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = root / f"{name}-profile"
    url = f"http://127.0.0.1:{server.server_port}/"
    app = _OwnedApp(name, executable)
    try:
        app.start(
            _chromium_hold_args(executable, profile, url),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app.window = _claim_new_window(_wait_window(token, timeout_s=25))
        # The browser frame can become visible before the isolated renderer has
        # accepted focus. This settle is preparation, outside measured delivery.
        time.sleep(1.0)
        _wait_browser_page_ready(state)

        def wait_value(expected: str) -> str:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with state.lock:
                    value = state.value
                if value == expected:
                    return value
                time.sleep(0.02)
            with state.lock:
                return state.value

        def make_prepare(baseline: str):
            def prepare(
                index: int,
                _payload: str,
                previous_sentinel: str | None,
                records: list[dict[str, Any]],
            ) -> object:
                _focus(app.window)  # type: ignore[arg-type]
                _click_owned_window(app.window, browser_page=True)  # type: ignore[arg-type]
                _browser_control_transaction(
                    state,
                    expected=baseline,
                    select_all=True,
                    previous_sentinel=previous_sentinel,
                    stage=f"{app.name}.prepare.run_{index}",
                    records=records,
                )
                target = capture_foreground_target()
                if target is None or target.top_hwnd != app.window.hwnd:  # type: ignore[union-attr]
                    raise RuntimeError(f"{name} focus target unavailable")
                return target

            return prepare

        def readback(_index: int, payload: str) -> str:
            return wait_value(payload)

        def clear_after(
            index: int,
            _target: object | None,
            sentinel: str | None,
            records: list[dict[str, Any]],
        ) -> None:
            _focus(app.window)  # type: ignore[arg-type]
            _click_owned_window(app.window, browser_page=True)  # type: ignore[arg-type]
            _browser_control_transaction(
                state,
                expected="",
                select_all=False,
                previous_sentinel=sentinel,
                stage=f"{app.name}.clear.run_{index}",
                records=records,
            )

        cases: list[dict[str, Any]] = []
        for case_name, payload_kind, baseline in (
            ("unicode_short_selection_replace", "short", "prefix selection suffix"),
            ("lf_tab", "browser_lines", ""),
            ("long", "long", ""),
        ):
            cases.append(
                _inject_repeated(
                    app=app,
                    router=router,
                    payload_kind=payload_kind,
                    case_name=case_name,
                    runs=runs,
                    prepare=make_prepare(baseline),
                    readback=readback,
                    clear_after=clear_after,
                )
            )
        diagnostics = _global_app_diagnostics(app, state, root, router, runs=min(3, runs))
        return _app_report(app, cases, diagnostics=diagnostics)
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _global_app_diagnostics(
    app: _OwnedApp,
    state: _PageState,
    root: Path,
    router: RoutingInjector,
    *,
    runs: int,
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    with state.lock:
        before = state.value
    distractor_path = root / f"{app.name}-focus-distractor.txt"
    distractor_path.write_text("", encoding="utf-8")
    notepad = discover_apps().get("notepad")
    distractor = _OwnedApp("focus_distractor", notepad or Path("notepad.exe"))
    try:
        if notepad is None:
            report["focus_theft"] = {
                "success": False,
                "reason": "isolated Notepad distractor unavailable",
            }
        else:
            distractor.start([str(notepad), str(distractor_path)])
            distractor.window = _claim_new_window(_wait_window(distractor_path.name))
            successes = 0
            recovered = 0
            refused_count = 0
            diagnostics: list[str] = []
            errors: list[str] = []
            trials: list[dict[str, Any]] = []
            control_transactions: list[dict[str, Any]] = []
            for index in range(runs):
                _focus(app.window)  # type: ignore[arg-type]
                _click_owned_window(app.window, browser_page=True)  # type: ignore[arg-type]
                _browser_control_transaction(
                    state,
                    expected=before,
                    select_all=False,
                    previous_sentinel=None,
                    stage=f"{app.name}.focus_theft.prepare.run_{index}",
                    records=control_transactions,
                )
                browser_target = capture_foreground_target()
                if browser_target is None:
                    errors.append(f"run {index}: browser target unavailable")
                    continue
                distractor_target = _native_edit_control_transaction(
                    distractor,
                    expected="",
                    select_all_after=False,
                    previous_sentinel=None,
                    stage=f"{app.name}.focus_theft.distractor_prepare.run_{index}",
                    records=control_transactions,
                )
                focus_payload, sentinel = _sentinel_payload("short")
                expected_recovery = before + focus_payload
                try:
                    router.inject_into_target(focus_payload, browser_target)
                    diagnostic = "injection returned successfully"
                    refused = False
                except Exception as exc:
                    diagnostic = f"{type(exc).__name__}: {exc}"
                    refused = True
                browser_after = _wait_for_page_value(
                    state,
                    before if refused else expected_recovery,
                )
                distractor_after = read_targeted_edit_state(distractor_target).text
                outcome = _focus_theft_outcome(
                    refused=refused,
                    before=before,
                    expected_recovery=expected_recovery,
                    browser_after=browser_after,
                    distractor_after=distractor_after,
                )
                exact = outcome in {"recovered", "refused"}
                successes += int(exact)
                recovered += int(outcome == "recovered")
                refused_count += int(outcome == "refused")
                diagnostics.append(diagnostic)
                if not exact:
                    errors.append(f"run {index}: focus-theft postcondition failed ({outcome})")
                trials.append(
                    {
                        "outcome": outcome,
                        "press_time_target_exact": browser_after == expected_recovery,
                        "press_time_target_unchanged": browser_after == before,
                        "bystander_unchanged": distractor_after == "",
                        "payload_sha256": hashlib.sha256(focus_payload.encode("utf-8")).hexdigest(),
                    }
                )

                # A successful recovery intentionally writes the synthetic
                # sentinel. Reset both owned controls and verify the browser's
                # asynchronous state before the following contention trial.
                _native_edit_control_transaction(
                    distractor,
                    expected="",
                    select_all_after=False,
                    previous_sentinel=sentinel,
                    stage=f"{app.name}.focus_theft.distractor_reset.run_{index}",
                    records=control_transactions,
                )
                _focus(app.window)  # type: ignore[arg-type]
                _click_owned_window(app.window, browser_page=True)  # type: ignore[arg-type]
                _browser_control_transaction(
                    state,
                    expected=before,
                    select_all=False,
                    previous_sentinel=sentinel,
                    stage=f"{app.name}.focus_theft.browser_reset.run_{index}",
                    records=control_transactions,
                )
            report["focus_theft"] = {
                "success": not errors and successes == runs,
                "runs": runs,
                "success_count": successes,
                "recovered_count": recovered,
                "refused_count": refused_count,
                "diagnostics": diagnostics,
                "errors": errors,
                "trials": trials,
                "control_transactions": _control_transaction_summary(control_transactions),
            }
    finally:
        distractor.close()

    contention = [
        _clipboard_contention_once(app, state, root, router, before=before, index=index)
        for index in range(runs)
    ]
    latencies = [float(item["elapsed_ms"]) for item in contention if item["success"]]
    report["clipboard_contention"] = {
        "success": len(latencies) == runs,
        "runs": runs,
        "success_count": len(latencies),
        "latency_ms": _latencies(latencies) if latencies else None,
        "trials": contention,
    }
    return report


def _clipboard_contention_once(
    app: _OwnedApp,
    state: _PageState,
    root: Path,
    router: RoutingInjector,
    *,
    before: str,
    index: int,
) -> dict[str, Any]:
    _focus(app.window)  # type: ignore[arg-type]
    _click_owned_window(app.window, browser_page=True)  # type: ignore[arg-type]
    target = capture_foreground_target()
    if target is None:
        return {"success": False, "error": "browser target unavailable"}
    release_path = root / f"{app.name}-clipboard-release-{index}.signal"
    ready_path = root / f"{app.name}-clipboard-ready-{index}.signal"
    holder_script = root / f"{app.name}-clipboard-holder-{index}.ps1"
    holder_script.write_text(
        "param([string]$ReleasePath,[string]$ReadyPath)\n"
        "$source=@'\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public static class ClipboardHolder {\n"
        ' [DllImport("user32.dll", SetLastError=true)]\n'
        " public static extern bool OpenClipboard(IntPtr owner);\n"
        ' [DllImport("user32.dll", SetLastError=true)]\n'
        " public static extern bool CloseClipboard();\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $source\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "$owner=New-Object System.Windows.Forms.Form\n"
        "$owner.CreateControl()\n"
        "$deadline=[DateTime]::UtcNow.AddSeconds(2)\n"
        "while(-not [ClipboardHolder]::OpenClipboard($owner.Handle)){\n"
        " if([DateTime]::UtcNow -ge $deadline){exit 2}\n"
        " Start-Sleep -Milliseconds 10\n"
        "}\n"
        "try {\n"
        " [IO.File]::WriteAllText($ReadyPath,'READY')\n"
        " $releaseDeadline=[DateTime]::UtcNow.AddSeconds(3)\n"
        " while((-not (Test-Path -LiteralPath $ReleasePath)) -and "
        "([DateTime]::UtcNow -lt $releaseDeadline)){Start-Sleep -Milliseconds 10}\n"
        "} finally {[void][ClipboardHolder]::CloseClipboard();$owner.Dispose()}\n",
        encoding="utf-8",
    )
    holder = start_owned_process(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(holder_script),
            "-ReleasePath",
            str(release_path),
            "-ReadyPath",
            str(ready_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    ready_deadline = time.monotonic() + 3.0
    while not ready_path.is_file() and holder.poll() is None and time.monotonic() < ready_deadline:
        time.sleep(0.01)
    holder_ready = ready_path.read_text(encoding="ascii") if ready_path.is_file() else ""
    contention_probe = _user32()
    contention_probe.OpenClipboard.argtypes = [wintypes.HWND]
    contention_probe.OpenClipboard.restype = wintypes.BOOL
    contention_probe.CloseClipboard.argtypes = []
    contention_probe.CloseClipboard.restype = wintypes.BOOL
    unexpectedly_opened = bool(contention_probe.OpenClipboard(0))
    if unexpectedly_opened:
        contention_probe.CloseClipboard()
    try:
        started = time.perf_counter()
        contention_payload, _sentinel = _sentinel_payload("browser_lines")
        try:
            router.inject_into_target(contention_payload, target)
            refused = False
            diagnostic = "injection unexpectedly succeeded"
        except Exception as exc:
            refused = True
            diagnostic = f"{type(exc).__name__}: {exc}"
        with state.lock:
            after_contention = state.value
        return {
            "success": (
                holder_ready == "READY"
                and not unexpectedly_opened
                and refused
                and after_contention == before
            ),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "diagnostic": diagnostic,
            "target_unchanged": after_contention == before,
            "holder_status": holder_ready,
            "independent_open_refused": not unexpectedly_opened,
        }
    finally:
        release_path.write_text("release", encoding="ascii")
        try:
            holder.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            pass
        finally:
            terminate_owned_process(holder, grace_s=0.0, kill_s=2.0)


def _console(root: Path, runs: int, router: RoutingInjector) -> dict[str, Any]:
    executable = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
    script = root / "terminal-capture.ps1"
    script.write_text(
        "param([string]$Output,[string]$Baseline,[int]$Units,[string]$Title)\n"
        "$Host.UI.RawUI.WindowTitle=$Title\n"
        "$e=New-Object Text.UTF8Encoding($false);"
        "[IO.File]::WriteAllText($Baseline,'',$e)\n"
        "$b=New-Object Text.StringBuilder\n"
        "while($b.Length -lt $Units){$k=[Console]::ReadKey($true);"
        "if([int]$k.KeyChar -ne 0){[void]$b.Append($k.KeyChar)}}\n"
        "[IO.File]::WriteAllText($Output,$b.ToString(),$e)\n",
        encoding="utf-8",
    )
    cases: list[dict[str, Any]] = []
    for case_name, payload_kind in (
        ("unicode_short", "short"),
        ("long", "terminal_long"),
    ):
        latencies: list[float] = []
        errors: list[str] = []
        decisions: list[dict[str, str]] = []
        payload_hashes: list[str] = []
        control_transactions: list[dict[str, Any]] = []
        previous_sentinel: str | None = None
        for index in range(runs):
            payload, sentinel = _sentinel_payload(payload_kind)
            payload_hashes.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
            output = root / f"terminal-{case_name}-{index}.txt"
            baseline = root / f"terminal-{case_name}-{index}.baseline"
            title = f"DCENT-terminal-{os.getpid()}-{case_name}-{index}"
            units = len(payload.encode("utf-16-le", errors="surrogatepass")) // 2
            app = _OwnedApp("console_terminal", executable)
            control_started = time.perf_counter()
            try:
                command = [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-Output",
                    str(output),
                    "-Units",
                    str(units),
                    "-Baseline",
                    str(baseline),
                    "-Title",
                    title,
                ]
                app.start(command, creationflags=_CREATE_NEW_CONSOLE)
                app.window = _claim_new_window(_wait_window(title))
                target = _focus(app.window)
                deadline = time.monotonic() + 3
                while not baseline.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not baseline.is_file():
                    raise TimeoutError("controlled terminal baseline was not published")
                _verify_baseline(
                    app=app.name,
                    run=index,
                    expected="",
                    observed=baseline.read_text(encoding="utf-8"),
                )
                current = capture_foreground_target()
                focus_verified = bool(
                    current and current.top_hwnd == app.window.hwnd  # type: ignore[union-attr]
                )
                baseline_value = baseline.read_text(encoding="utf-8")
                _record_control_transaction(
                    control_transactions,
                    stage=f"{app.name}.fresh_process_prepare.run_{index}",
                    attempts=1,
                    started=control_started,
                    expected="",
                    previous_sentinel=previous_sentinel,
                    observed=baseline_value,
                    focus_verified=focus_verified,
                    selection_verified=True,
                    success=focus_verified
                    and (not previous_sentinel or previous_sentinel not in baseline_value),
                    failure_stage=None
                    if focus_verified
                    else f"{app.name}.fresh_process_prepare.run_{index}.focus",
                )
                if not focus_verified:
                    raise AssertionError("controlled terminal focus was not verified")
                started = time.perf_counter()
                decision = router.inject_into_target_with_decision(payload, target)
                decisions.append(
                    {
                        "process_name": decision.process_name,
                        "configured_injector": decision.configured_injector,
                        "resolved_injector": decision.resolved_injector,
                        "delivery": decision.delivery,
                    }
                )
                deadline = time.monotonic() + 5
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.03)
                if not output.exists():
                    raise TimeoutError("controlled terminal capture did not complete")
                observed = output.read_bytes().decode("utf-8")
                _verify_delivery(
                    app=app.name,
                    run=index,
                    expected=payload,
                    observed=observed,
                    sentinel=sentinel,
                )
                latencies.append((time.perf_counter() - started) * 1000.0)
            except Exception as exc:
                errors.append(f"run {index}: {type(exc).__name__}: {exc}")
            finally:
                app.close()
                clear_started = time.perf_counter()
                process_gone = app.process is None or app.process.poll() is not None
                _record_control_transaction(
                    control_transactions,
                    stage=f"{app.name}.fresh_process_close.run_{index}",
                    attempts=1,
                    started=clear_started,
                    expected="",
                    previous_sentinel=sentinel,
                    observed="",
                    focus_verified=process_gone,
                    selection_verified=True,
                    success=process_gone,
                    failure_stage=None
                    if process_gone
                    else f"{app.name}.fresh_process_close.run_{index}.process_alive",
                )
                previous_sentinel = sentinel
        cases.append(
            _case_result(
                case_name,
                decisions,
                latencies,
                errors,
                payload_hashes,
                requested_runs=runs,
                control_transactions=control_transactions,
            )
        )
    pseudo = _OwnedApp("console_terminal", executable)
    return _app_report(pseudo, cases, scope="cmd-hosted fixed PowerShell ReadKey capture")


def _app_report(
    app: _OwnedApp,
    cases: list[dict[str, Any]],
    *,
    diagnostics: dict[str, Any] | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    diagnostics = diagnostics or {}
    exact = all(case["exact"] for case in cases) and all(
        not isinstance(value, dict) or value.get("success") is not False
        for value in diagnostics.values()
    )
    return {
        "app": app.name,
        "status": "pass" if exact else "fail",
        "executable": str(app.executable),
        "version": _version(app.executable),
        "scope": scope or "isolated scratch document/profile",
        "cases": cases,
        "diagnostics": diagnostics,
    }


def discover_apps() -> dict[str, Path | None]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    candidates = {
        "notepad": [Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/notepad.exe"],
        "vscode": [
            local / "Programs/Microsoft VS Code/Code.exe",
            program_files / "Microsoft VS Code/Code.exe",
        ],
        "console": [Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))],
        "edge": [
            program_x86 / "Microsoft/Edge/Application/msedge.exe",
            program_files / "Microsoft/Edge/Application/msedge.exe",
        ],
        "chrome": [
            program_files / "Google/Chrome/Application/chrome.exe",
            program_x86 / "Google/Chrome/Application/chrome.exe",
        ],
    }
    return {
        name: next((path for path in paths if path.is_file()), None)
        for name, paths in candidates.items()
    }


def run_apps_matrix(*, apps: list[str], runs: int) -> dict[str, Any]:
    if platform.system() != "Windows":
        raise RuntimeError("real application injection matrix requires Windows")
    if runs < 1 or runs > 20:
        raise ValueError("runs must be between 1 and 20")
    from dcent_voice.app import build_injector
    from dcent_voice.config import load_bundled_default_config

    config = load_bundled_default_config()
    config_path = config.source_path
    if config_path is None:
        raise RuntimeError("bundled default config did not retain its source path")
    router = build_injector(config)
    available = discover_apps()
    requested = list(available) if apps == ["all"] else apps
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ValueError(f"unknown apps: {', '.join(unknown)}")
    _OWNED_PROCESS_HANDLES.clear()
    _PREEXISTING_WINDOW_PIDS.clear()
    _PREEXISTING_WINDOW_PIDS.update(item.pid for item in _windows())
    clipboard_before = snapshot_clipboard(timeout_s=0.5)
    reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dcent-voice-real-app-matrix-") as directory:
        root = Path(directory)
        for name in requested:
            executable = available[name]
            if executable is None:
                reports.append(
                    {
                        "app": name,
                        "status": "skip",
                        "reason": "application executable not installed",
                    }
                )
                continue
            try:
                if name == "notepad":
                    reports.append(_notepad(executable, root, runs, router))
                elif name == "vscode":
                    reports.append(_code(executable, root, runs, router))
                elif name == "console":
                    reports.append(_console(root, runs, router))
                else:
                    reports.append(_browser(name, executable, root, runs, router))
            except Exception as exc:
                reports.append(
                    {
                        "app": name,
                        "status": "fail",
                        "executable": str(executable),
                        "version": _version(executable),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    isolated_root_removed = not root.exists()
    process_cleanup = _verify_owned_process_cleanup()
    clipboard_after = snapshot_clipboard(timeout_s=0.5)
    clipboard_exact = clipboard_before == clipboard_after
    failures = [item for item in reports if item["status"] == "fail"]
    passes = [item for item in reports if item["status"] == "pass"]
    return {
        "schema_version": 1,
        "status": (
            "pass"
            if passes
            and not failures
            and clipboard_exact
            and isolated_root_removed
            and process_cleanup["success"]
            else "fail"
        ),
        "platform": platform.platform(),
        "production_injector": {
            "builder": "dcent_voice.app.build_injector",
            "config_path": str(config_path),
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "default": config.injector.default,
            "restore_clipboard": config.injector.restore_clipboard,
            "paste_delay_s": config.injector.paste_delay_s,
            "paste_min_delay_s": config.injector.paste_min_delay_s,
            "short_text_keystroke_chars": config.injector.short_text_keystroke_chars,
            "per_app": dict(config.injector.per_app),
        },
        "scope": (
            "real isolated Windows applications; injection begins after target focus "
            "and excludes microphone/ASR"
        ),
        "excludes": [
            "microphone capture",
            "ASR/model latency",
            "existing user profiles, windows, sessions, and documents",
            "elevated integrity target (not available in isolated non-admin run)",
        ],
        "safety": {
            "profiles": "fresh temporary profiles/documents only",
            "terminal": (
                "fixed ReadKey capture was running before injection; dictated text was "
                "never parsed as shell code"
            ),
            "cleanup": {
                "owned_windows_and_process_trees_only": True,
                "preexisting_window_pid_count": len(_PREEXISTING_WINDOW_PIDS),
                "preexisting_process_targeted": False,
                "isolated_root_removed": isolated_root_removed,
                "processes": process_cleanup,
            },
        },
        "integrity_boundary": {
            "status": "not_exercised",
            "reason": "no isolated elevated target is available without requesting elevation",
        },
        "clipboard_restoration": {
            "snapshot_cloneable": clipboard_before is not None,
            "exact_after_matrix": clipboard_exact,
        },
        "apps": reports,
    }


def run_apps_test_command(*, output_json: Path | None, apps: list[str], runs: int) -> int:
    try:
        report = run_apps_matrix(apps=apps, runs=runs)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "error",
            "diagnostic": {"stage": "apps_matrix", "error": f"{type(exc).__name__}: {exc}"},
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output_json is None:
        print(rendered, end="")
    else:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(rendered, encoding="utf-8")
    return 0 if report.get("status") == "pass" else 1


HOLD_RELEASE_APP_NAMES = ("notepad", "vscode", "console", "edge", "chrome")
BROWSER_UIA_TARGETS = {
    "edge-ce": ("edge", "contenteditable", "Article draft"),
    "edge-form": ("edge", "form", "Meeting notes"),
    "chrome-ce": ("chrome", "contenteditable", "Article draft"),
    "chrome-form": ("chrome", "form", "Meeting notes"),
}
LIVE_BROWSER_TARGETS = {
    "edge-ddg": (
        "edge",
        "search",
        ("Search", "Search DuckDuckGo", "Search without being tracked"),
        "https://duckduckgo.com/",
        ("DuckDuckGo",),
    ),
    "edge-google": (
        "edge",
        "search",
        ("Search", "Search Google", "q"),
        "https://www.google.com/webhp?hl=en&igu=1",
        ("Google",),
    ),
    "edge-wiki": (
        "edge",
        "search",
        ("Search Wikipedia", "Search"),
        "https://en.wikipedia.org/wiki/Special:Search",
        ("Wikipedia",),
    ),
    "edge-github": (
        "edge",
        "search",
        (
            "Search GitHub",
            "Search or jump to…",
            "Search or jump to",
            "Type / to search",
            "Search",
        ),
        "https://github.com/search",
        ("GitHub",),
    ),
    "edge-gmail": (
        "edge",
        "composer",
        ("Message Body", "Body", "Compose"),
        "https://mail.google.com/mail/u/0/#inbox?compose=new",
        ("Gmail", "Inbox", "Sign in"),
    ),
}
EXISTING_DOCUMENT_APP_NAMES = (
    "notepad",
    "vscode",
    "edge",
    "chrome",
    *BROWSER_UIA_TARGETS,
    *LIVE_BROWSER_TARGETS,
)


def hold_release_app_allowed(name: str) -> bool:
    return (
        name in HOLD_RELEASE_APP_NAMES
        or name in BROWSER_UIA_TARGETS
        or name in LIVE_BROWSER_TARGETS
    )


def browser_backend_for_hold_app(name: str) -> str | None:
    if name in {"edge", "chrome"}:
        return name
    spec = BROWSER_UIA_TARGETS.get(name) or LIVE_BROWSER_TARGETS.get(name)
    return None if spec is None else spec[0]


def existing_document_paths() -> dict[str, Path]:
    """Stable on-disk documents used by the real-document hold/release probe."""
    notes_dir = Path.home() / "Documents" / "DCENT_Voice_DesktopTests"
    workspace = None
    for root in (_require_source_checkout(), Path.cwd()):
        candidate = root / "eval" / "w29-workspace-note.txt"
        if candidate.is_file():
            workspace = candidate
            break
    browser = None
    contenteditable = None
    nested_form = None
    for root in (_require_source_checkout(), Path.cwd()):
        if browser is None:
            candidate = root / "eval" / "w29-browser-field.html"
            if candidate.is_file():
                browser = candidate
        if contenteditable is None:
            candidate = root / "eval" / "w31-contenteditable.html"
            if candidate.is_file():
                contenteditable = candidate
        if nested_form is None:
            candidate = root / "eval" / "w31-nested-form.html"
            if candidate.is_file():
                nested_form = candidate
    return {
        "notepad": notes_dir / "meeting-notes.txt",
        "vscode": workspace or (notes_dir / "workspace-note.txt"),
        "edge": browser or (notes_dir / "browser-field.html"),
        "chrome": browser or (notes_dir / "browser-field.html"),
        "edge-ce": contenteditable or (notes_dir / "contenteditable.html"),
        "chrome-ce": contenteditable or (notes_dir / "contenteditable.html"),
        "edge-form": nested_form or (notes_dir / "nested-form.html"),
        "chrome-form": nested_form or (notes_dir / "nested-form.html"),
    }


@dataclass
class IsolatedHoldTarget:
    """Owned scratch target with unique-baseline prepare and app-native readback."""

    name: str
    app: _OwnedApp
    root: Path
    scratch: Path | None = None
    state: _PageState | None = None
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    console_output: Path | None = None
    console_done: Path | None = None
    profile: Path | None = None
    field_kind: str | None = None
    field_name: str | None = None
    field_names: tuple[str, ...] | None = None
    search_click_frac: float | None = None
    _closed: bool = False

    def _uia_field_names(self) -> tuple[str, ...]:
        if self.field_names:
            return self.field_names
        if self.field_name:
            return (self.field_name,)
        return ()

    def _is_live_web_field(self) -> bool:
        return self.field_kind in {"search", "composer"}

    def _focus_live_page_field(self) -> None:
        names = self._uia_field_names()
        if self.app.window is None or not names:
            raise RuntimeError(f"{self.name} UIA hold target is not open")
        search = self.field_kind == "search"
        if search and focused_page_search_field(names) is not None:
            return
        _focus(self.app.window)
        if search:
            try:
                focus_page_editable(
                    names=names,
                    search_from=_control_from_hwnd(self.app.window.hwnd),
                    timeout_s=8.0,
                    named_only=True,
                )
                return
            except Exception:
                pass
            try:
                import uiautomation as auto

                host = _walk_search_host(
                    _control_from_hwnd(self.app.window.hwnd) or auto.GetForegroundControl()
                )
            except Exception:
                host = None
            host_height = 0
            if host is not None:
                rect = getattr(host, "BoundingRectangle", None)
                if rect is not None:
                    host_height = int(getattr(rect, "height", getattr(rect, "Height", 0)) or 0)
            if (
                host is not None
                and host_height <= 80
                and _click_uia_control(host, window=self.app.window)
            ):
                time.sleep(0.15)
                return
            _click_owned_window(
                self.app.window,
                browser_page=True,
                search_field=True,
                search_frac=self.search_click_frac,
            )
            time.sleep(0.15)
            return
        try:
            focus_page_editable(names=names, timeout_s=8.0)
            return
        except Exception:
            if not self._is_live_web_field():
                raise
        _click_owned_window(self.app.window, browser_page=True, search_field=True)
        time.sleep(0.15)

    def _type_into_autofocused_search(self, baseline: str, injector: Any) -> str:
        """Type without SetForeground/Escape/Ctrl+A so Google's ComboBox stays focused."""
        names = self._uia_field_names()
        deadline = time.monotonic() + 2.0
        observed = ""
        while time.monotonic() < deadline:
            try:
                if focused_page_search_field(names) is None:
                    time.sleep(0.15)
                    continue
                injector.inject(baseline)
                observed = self._read_live_field_text()
            except Exception:
                observed = ""
            if observed == baseline:
                self.search_click_frac = 0.46
                return observed
            time.sleep(0.2)
        return observed

    def _land_page_search_field(
        self,
        baseline: str,
        injector: Any,
        names: Sequence[str],
    ) -> str:
        """Named edit, then first RootWebArea edit/combobox. No omnibox."""
        window = self.app.window
        if window is None:
            raise RuntimeError(f"{self.name} browser window is not open")
        try:
            _focus(window)
            match = _page_search_field(window, names)
            if match is None:
                match = focus_page_editable(
                    names=names,
                    search_from=_control_from_hwnd(window.hwnd),
                    timeout_s=1.2,
                    named_only=False,
                )
            _click_uia_control(match, window=window)
            setter = getattr(match, "SetFocus", None)
            if callable(setter):
                setter()
            time.sleep(0.08)
            try:
                set_focused_editable_text(baseline)
            except Exception:
                injector.inject(baseline)
            return self._read_live_field_text()
        except Exception:
            return ""

    def _read_live_field_text(self) -> str:
        try:
            snapshot = read_focused_editable()
            if looks_like_navigated_url(snapshot.text):
                raise RuntimeError("focused control is a URL, not a page field")
            if looks_like_search_chrome_label(snapshot.text):
                raise RuntimeError(f"focused a label, not the search field: {snapshot.text!r}")
            if (
                snapshot.text.strip()
                and snapshot.text.strip().casefold() != snapshot.name.casefold()
            ):
                return snapshot.text
        except RuntimeError:
            if not self._is_live_web_field():
                raise
        prior = snapshot_clipboard(timeout_s=0.3)
        try:
            _hotkey(_VK_CONTROL, _VK_A)
            time.sleep(0.05)
            _hotkey(_VK_CONTROL, _VK_C)
            time.sleep(0.08)
            text = get_clipboard_text(timeout_s=0.5) or ""
        finally:
            if prior is not None:
                with contextlib.suppress(Exception):
                    restore_clipboard(prior, timeout_s=0.5)
        if looks_like_navigated_url(text):
            raise RuntimeError("clipboard readback is a URL; omnibox landing refused")
        if "\n" in text and len(text) > 80:
            raise RuntimeError("clipboard readback selected the page, not the field")
        if looks_like_search_chrome_label(text):
            raise RuntimeError(f"focused a label, not the search field: {text!r}")
        return text

    @property
    def process_id(self) -> int:
        if self.app.window is None:
            raise RuntimeError(f"{self.name} has no owned window")
        return int(self.app.window.pid)

    def prepare(
        self,
        baseline: str,
        previous_sentinel: str | None,
        records: list[dict[str, Any]],
        stage: str,
    ) -> object:
        if self.name == "notepad":
            _focus(self.app.window, require_edit=True)  # type: ignore[arg-type]
            return _native_edit_control_transaction(
                self.app,
                expected=baseline,
                select_all_after=True,
                previous_sentinel=previous_sentinel,
                stage=stage,
                records=records,
            )
        if self.name == "vscode":
            if self.app.window is None or self.scratch is None:
                raise RuntimeError("VS Code hold target is not open")
            started = time.perf_counter()
            _focus(self.app.window)
            _click_owned_window(self.app.window)
            # Save first. If Monaco never loaded the scratch baseline, this
            # persists an empty or stale buffer and the prepare must fail.
            _hotkey(_VK_CONTROL, _VK_S)
            deadline = time.monotonic() + 1.5
            observed = ""
            while time.monotonic() < deadline:
                observed = self.scratch.read_bytes().decode("utf-8")
                if observed == baseline:
                    break
                time.sleep(0.05)
            _hotkey(_VK_CONTROL, _VK_A)
            time.sleep(0.05)
            _hotkey(_VK_CONTROL, _VK_A)
            _focus(self.app.window)
            current = capture_foreground_target()
            focus_verified = bool(current and current.top_hwnd == self.app.window.hwnd)
            success = (
                observed == baseline
                and focus_verified
                and (not previous_sentinel or previous_sentinel not in observed)
            )
            _record_control_transaction(
                records,
                stage=stage,
                attempts=1,
                started=started,
                expected=baseline,
                previous_sentinel=previous_sentinel,
                observed=observed,
                focus_verified=focus_verified,
                selection_verified=success,
                success=success,
                failure_stage=None if success else f"{stage}.editor_baseline",
            )
            if not success:
                raise RuntimeError(f"VS Code editor baseline was not loaded exactly: {observed!r}")
            if current is None:
                raise RuntimeError("VS Code focus target unavailable")
            return current
        if self.field_kind and self._uia_field_names():
            return self._prepare_uia_field(
                baseline,
                previous_sentinel,
                records,
                stage,
            )
        if self.name in {"edge", "chrome"}:
            if self.app.window is None or self.state is None:
                raise RuntimeError(f"{self.name} hold target is not open")
            _focus(self.app.window)
            _click_owned_window(self.app.window, browser_page=True)
            _wait_browser_page_ready(self.state)
            _browser_control_transaction(
                self.state,
                expected=baseline,
                select_all=True,
                previous_sentinel=previous_sentinel,
                stage=stage,
                records=records,
                attempt_timeout_s=2.5,
            )
            target = capture_foreground_target()
            if target is None or target.top_hwnd != self.app.window.hwnd:
                raise RuntimeError(f"{self.name} focus target unavailable")
            return target
        if self.name == "console":
            if self.app.window is None:
                raise RuntimeError("console hold target is not open")
            focused_target = _focus(self.app.window)
            current = capture_foreground_target()
            focus_verified = bool(current and current.top_hwnd == self.app.window.hwnd)
            _record_control_transaction(
                records,
                stage=stage,
                attempts=1,
                started=time.perf_counter(),
                expected="",
                previous_sentinel=previous_sentinel,
                observed="",
                focus_verified=focus_verified,
                selection_verified=True,
                success=focus_verified,
                failure_stage=None if focus_verified else f"{stage}.focus",
            )
            if not focus_verified or target is None:
                raise RuntimeError("controlled terminal focus was not verified")
            return focused_target
        raise RuntimeError(f"unsupported hold-release app: {self.name}")

    def _prepare_uia_field(
        self,
        baseline: str,
        previous_sentinel: str | None,
        records: list[dict[str, Any]],
        stage: str,
    ) -> object:
        names = self._uia_field_names()
        if self.app.window is None or not names:
            raise RuntimeError(f"{self.name} UIA hold target is not open")
        title = self.app.window.title.casefold()
        if self.name == "edge-gmail" and "sign in" in title:
            raise RuntimeError("Gmail compose unavailable: isolated profile hit a login wall")
        from dcent_voice.inject.keystroke import WindowsSendInputInjector

        started = time.perf_counter()
        live = self._is_live_web_field()
        observed = ""
        snapshot = None
        injector = WindowsSendInputInjector()
        if live and self.field_kind == "search":
            github = not _search_prepare_preserves_autofocus(self.name)
            if github:
                self._focus_live_page_field()
                try:
                    set_focused_editable_text(baseline)
                except Exception:
                    _hotkey(_VK_CONTROL, _VK_A)
                    time.sleep(0.04)
                    injector.inject(baseline)
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline:
                    try:
                        observed = self._read_live_field_text()
                    except RuntimeError:
                        observed = ""
                    if observed == baseline:
                        break
                    time.sleep(0.05)
            else:
                observed = self._type_into_autofocused_search(baseline, injector)
            if observed != baseline and not github:
                observed = self._land_page_search_field(baseline, injector, names)
            if observed != baseline and not github:
                for frac in (0.46, 0.38, 0.34, 0.42, 0.30, 0.50, 0.26, 0.22, 0.18, 0.16, 0.12):
                    _focus(self.app.window)
                    _click_owned_window(
                        self.app.window,
                        browser_page=True,
                        search_field=True,
                        search_frac=frac,
                    )
                    time.sleep(0.15)
                    injector.inject(baseline)
                    try:
                        observed = self._read_live_field_text()
                    except RuntimeError:
                        observed = ""
                    if observed == baseline:
                        self.search_click_frac = frac
                        break
        else:
            if live:
                self._focus_live_page_field()
            else:
                _focus(self.app.window)
                _click_owned_window(self.app.window, browser_page=True)
                focus_page_editable(names=names, timeout_s=8.0)
            try:
                set_focused_editable_text(baseline)
            except Exception:
                _hotkey(_VK_CONTROL, _VK_A)
                time.sleep(0.05)
                injector.inject(baseline)
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                try:
                    observed = (
                        self._read_live_field_text() if live else read_focused_editable().text
                    )
                    if not live:
                        snapshot = read_focused_editable()
                        observed = snapshot.text
                except RuntimeError:
                    observed = ""
                    snapshot = None
                if observed == baseline:
                    break
                time.sleep(0.05)
        _hotkey(_VK_CONTROL, _VK_A)
        time.sleep(0.05)
        _hotkey(_VK_CONTROL, _VK_A)
        current = capture_foreground_target()
        uia_ok = snapshot is not None and snapshot.kind in {"edit", "contenteditable"}
        focus_verified = bool(
            current
            and current.top_hwnd == self.app.window.hwnd
            and (uia_ok or (live and observed == baseline))
        )
        success = (
            observed == baseline
            and focus_verified
            and (not previous_sentinel or previous_sentinel not in observed)
            and not looks_like_navigated_url(observed)
        )
        _record_control_transaction(
            records,
            stage=stage,
            attempts=1,
            started=started,
            expected=baseline,
            previous_sentinel=previous_sentinel,
            observed=observed,
            focus_verified=focus_verified,
            selection_verified=success,
            success=success,
            failure_stage=None if success else f"{stage}.uia_field",
        )
        if not success:
            raise RuntimeError(
                f"{self.name} UIA field prepare failed: "
                f"expected={baseline!r} observed={observed!r} "
                f"kind={None if snapshot is None else snapshot.kind}"
            )
        if current is None:
            raise RuntimeError(f"{self.name} focus target unavailable")
        return current

    def ensure_ready_for_press(self) -> object:
        """Re-assert the owned window immediately before the production chord."""
        if self.app.window is None:
            raise RuntimeError(f"{self.name} has no owned window")
        if self.name == "notepad":
            return _focus(self.app.window, require_edit=True)
        if self.name == "vscode":
            if self.scratch is not None:
                with contextlib.suppress(Exception):
                    self.app.window = _claim_new_window(
                        _wait_window(self.scratch.name, timeout_s=3.0)
                    )
            with contextlib.suppress(TimeoutError):
                _focus(self.app.window)
            _click_owned_window(self.app.window)
            _hotkey(_VK_CONTROL, _VK_A)
            time.sleep(0.05)
            _hotkey(_VK_CONTROL, _VK_A)
            current = capture_foreground_target()
            if current is not None and current.top_hwnd == self.app.window.hwnd:
                return current
            bound = capture_target_from_hwnd(self.app.window.hwnd)
            if bound is None:
                raise RuntimeError("VS Code hold target HWND disappeared")
            return bound
        if self.field_kind and self._uia_field_names():
            if self._is_live_web_field():
                self._focus_live_page_field()
            else:
                with contextlib.suppress(TimeoutError):
                    _focus(self.app.window)
                _click_owned_window(self.app.window, browser_page=True)
                focus_page_editable(names=self._uia_field_names(), timeout_s=8.0)
            _hotkey(_VK_CONTROL, _VK_A)
            time.sleep(0.05)
            _hotkey(_VK_CONTROL, _VK_A)
            current = capture_foreground_target()
            if current is not None and current.top_hwnd == self.app.window.hwnd:
                return current
            bound = capture_target_from_hwnd(self.app.window.hwnd)
            if bound is None:
                raise RuntimeError(f"{self.name} hold target HWND disappeared")
            return bound
        if self.name in {"edge", "chrome"}:
            with contextlib.suppress(TimeoutError):
                _focus(self.app.window)
            _click_owned_window(self.app.window, browser_page=True)
            _hotkey(_VK_CONTROL, _VK_A)
            time.sleep(0.05)
            _hotkey(_VK_CONTROL, _VK_A)
            current = capture_foreground_target()
            if current is not None and current.top_hwnd == self.app.window.hwnd:
                return current
            bound = capture_target_from_hwnd(self.app.window.hwnd)
            if bound is None:
                raise RuntimeError(f"{self.name} hold target HWND disappeared")
            return bound
        return _focus(self.app.window)

    def readback(self, expected: str, *, timeout_s: float = 5.0) -> str:
        if self.name == "notepad":
            if self.app.window is None:
                raise RuntimeError("Notepad hold target is not open")
            target = capture_target_from_hwnd(self.app.window.hwnd)
            if target is None or not target.supports_edit_messages:
                target = capture_foreground_target()
            if target is None:
                raise RuntimeError("Notepad focus target disappeared before readback")
            return read_targeted_edit_state(target).text
        if self.name == "vscode":
            if self.scratch is None:
                raise RuntimeError("VS Code scratch path missing")
            if self.app.window is not None:
                from dcent_voice.inject.windows_focus import restore_foreground

                restore_foreground(int(self.app.window.hwnd))
            _hotkey(_VK_CONTROL, _VK_S)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                value = self.scratch.read_bytes().decode("utf-8")
                if value == expected:
                    return value
                time.sleep(0.05)
            return self.scratch.read_bytes().decode("utf-8")
        if self.field_kind and self._uia_field_names():
            if self.app.window is not None:
                from dcent_voice.inject.windows_focus import restore_foreground

                restore_foreground(int(self.app.window.hwnd))
            deadline = time.monotonic() + timeout_s
            observed = ""
            while time.monotonic() < deadline:
                try:
                    if self._is_live_web_field():
                        try:
                            observed = self._read_live_field_text()
                        except RuntimeError:
                            observed = ""
                        if observed != expected:
                            self._focus_live_page_field()
                            observed = self._read_live_field_text()
                    else:
                        focus_page_editable(names=self._uia_field_names(), timeout_s=1.0)
                        observed = read_focused_editable().text
                except RuntimeError:
                    observed = ""
                if observed == expected:
                    return observed
                time.sleep(0.05)
            return observed
        if self.name in {"edge", "chrome"}:
            if self.state is None:
                raise RuntimeError(f"{self.name} page state missing")
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                with self.state.lock:
                    value = self.state.value
                if value == expected:
                    return value
                time.sleep(0.02)
            with self.state.lock:
                return self.state.value
        if self.name == "console":
            if self.console_output is None or self.console_done is None:
                raise RuntimeError("console capture paths missing")
            time.sleep(0.15)
            self.console_done.write_text("done", encoding="ascii")
            deadline = time.monotonic() + timeout_s
            while not self.console_output.is_file() and time.monotonic() < deadline:
                time.sleep(0.03)
            if not self.console_output.is_file():
                raise TimeoutError("controlled terminal capture did not complete")
            return self.console_output.read_bytes().decode("utf-8")
        raise RuntimeError(f"unsupported hold-release app: {self.name}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.app.close()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.server_thread is not None:
            self.server_thread.join(timeout=2)
        if self.profile is not None:
            _rmtree_best_effort(self.profile)


class _StaticPageHandler(BaseHTTPRequestHandler):
    html: bytes

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.html)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.wfile.write(self.html)

    def log_message(self, _format: str, *args: Any) -> None:
        return


def _render_uia_browser_page(template: Path, *, title: str, baseline: str) -> bytes:
    html = template.read_text(encoding="utf-8")
    html = html.replace("<title>Article draft</title>", f"<title>{title}</title>", 1)
    html = html.replace("<title>Incident report</title>", f"<title>{title}</title>", 1)
    html = html.replace("Existing article draft", baseline, 1)
    html = html.replace("Existing form notes", baseline, 1)
    if title not in html or baseline not in html:
        raise RuntimeError(f"browser field template {template} is missing title or baseline slots")
    return html.encode("utf-8")


def _open_uia_browser_hold_target(
    name: str,
    executable: Path,
    root: Path,
    *,
    run_index: int,
    baseline: str,
    existing: bool,
) -> IsolatedHoldTarget:
    browser, kind, field_name = BROWSER_UIA_TARGETS[name]
    documents = existing_document_paths()
    template = documents[name]
    if not template.is_file():
        raise RuntimeError(f"existing {name} document is missing: {template}")
    token = f"DCENT-{name}-{os.getpid()}-{time.time_ns()}"
    html = _render_uia_browser_page(template, title=token, baseline=baseline)
    handler = type(
        f"{name.title().replace('-', '')}StaticHandler",
        (_StaticPageHandler,),
        {"html": html},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = root / f"{name}-{'existing' if existing else 'hold'}-profile-{run_index}"
    url = f"http://127.0.0.1:{server.server_port}/"
    app = _OwnedApp(
        f"hold_release_{name}{'_existing' if existing else ''}",
        executable,
    )
    app.start(
        _chromium_hold_args(executable, profile, url),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    app.window = _claim_new_window(_wait_window(token, timeout_s=25))
    time.sleep(1.5)
    return IsolatedHoldTarget(
        name=name,
        app=app,
        root=root,
        server=server,
        server_thread=thread,
        profile=profile,
        field_kind=kind,
        field_name=field_name,
    )


def _open_live_browser_hold_target(
    name: str,
    executable: Path,
    root: Path,
    *,
    run_index: int,
    existing: bool,
) -> IsolatedHoldTarget:
    """Open a live public page in an isolated Chromium profile.

    Isolated profiles are not logged into Gmail/Docs. That is recorded as a
    login wall, not a dictation PASS. The address bar is never a landing site.
    """

    browser, kind, field_names, url, title_fragments = LIVE_BROWSER_TARGETS[name]
    profile = root / f"{name}-{'existing' if existing else 'hold'}-profile-{run_index}"
    app = _OwnedApp(
        f"hold_release_{name}{'_existing' if existing else ''}",
        executable,
    )
    app.start(
        _chromium_hold_args(executable, profile, url, live=True),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    app.window = _claim_new_window(_wait_new_window(title_fragments, timeout_s=40.0))
    time.sleep(4.0)
    _wait_browser_client_ready(app.window)
    if kind == "search":
        _wait_live_search_field(app.window, field_names, timeout_s=8.0)
    return IsolatedHoldTarget(
        name=name,
        app=app,
        root=root,
        profile=profile,
        field_kind=kind,
        field_name=field_names[0],
        field_names=field_names,
    )


def open_isolated_hold_target(
    name: str,
    root: Path,
    *,
    run_index: int,
    baseline: str,
) -> IsolatedHoldTarget:
    """Launch one isolated app for hold/speak/release. Never targets existing windows."""

    available = discover_apps()
    if not hold_release_app_allowed(name):
        raise ValueError(f"unknown hold-release app: {name}")
    backend = browser_backend_for_hold_app(name)
    executable = available.get(backend or (name if name != "console" else "console"))
    if name != "console" and executable is None:
        raise RuntimeError(f"{backend or name} is not installed")

    if name == "notepad":
        if executable is None:
            raise RuntimeError("Notepad is not installed")
        scratch = root / f"hold-release-notepad-{run_index}.txt"
        scratch.write_text(baseline, encoding="utf-8")
        app = _OwnedApp("hold_release_notepad", executable)
        app.start([str(executable), str(scratch)])
        app.window = _claim_new_window(_wait_window(scratch.name))
        return IsolatedHoldTarget(name=name, app=app, root=root, scratch=scratch)

    if name == "vscode":
        if executable is None:
            raise RuntimeError("VS Code is not installed")
        profile = root / f"vscode-profile-{run_index}"
        extensions = root / f"vscode-extensions-{run_index}"
        scratch = root / f"hold-release-vscode-{run_index}.txt"
        scratch.write_text(baseline, encoding="utf-8")
        app = _OwnedApp("hold_release_vscode", executable)
        app.start(
            [
                str(executable),
                "--user-data-dir",
                str(profile),
                "--extensions-dir",
                str(extensions),
                "--disable-extensions",
                "--skip-welcome",
                "--skip-release-notes",
                "--new-window",
                str(scratch),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app.window = _claim_new_window(_wait_window(scratch.name, timeout_s=25))
        time.sleep(1.0)
        return IsolatedHoldTarget(
            name=name,
            app=app,
            root=root,
            scratch=scratch,
            profile=profile,
        )

    if name in BROWSER_UIA_TARGETS:
        if executable is None:
            raise RuntimeError(f"{BROWSER_UIA_TARGETS[name][0]} is not installed")
        return _open_uia_browser_hold_target(
            name,
            executable,
            root,
            run_index=run_index,
            baseline=baseline,
            existing=False,
        )

    if name in LIVE_BROWSER_TARGETS:
        if executable is None:
            raise RuntimeError(f"{LIVE_BROWSER_TARGETS[name][0]} is not installed")
        return _open_live_browser_hold_target(
            name,
            executable,
            root,
            run_index=run_index,
            existing=False,
        )

    if name in {"edge", "chrome"}:
        if executable is None:
            raise RuntimeError(f"{name} is not installed")
        token = f"DCENT-hold-{name}-{os.getpid()}-{time.time_ns()}"
        state = _PageState(token)
        handler = type(f"{name.title()}HoldHandler", (_PageHandler,), {"state": state})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = root / f"{name}-hold-profile-{run_index}"
        url = f"http://127.0.0.1:{server.server_port}/"
        app = _OwnedApp(f"hold_release_{name}", executable)
        app.start(
            _chromium_hold_args(executable, profile, url),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app.window = _claim_new_window(_wait_window(token, timeout_s=25))
        time.sleep(1.0)
        _wait_browser_page_ready(state)
        return IsolatedHoldTarget(
            name=name,
            app=app,
            root=root,
            state=state,
            server=server,
            server_thread=thread,
            profile=profile,
        )

    script = root / "hold-release-terminal-capture.ps1"
    script.write_text(
        "param([string]$Output,[string]$DonePath,[string]$Title)\n"
        "$Host.UI.RawUI.WindowTitle=$Title\n"
        "$e=New-Object Text.UTF8Encoding($false)\n"
        "$b=New-Object Text.StringBuilder\n"
        "$deadline=[DateTime]::UtcNow.AddSeconds(45)\n"
        "while((-not (Test-Path -LiteralPath $DonePath)) -and "
        "([DateTime]::UtcNow -lt $deadline)){\n"
        " if([Console]::KeyAvailable){\n"
        "  $k=[Console]::ReadKey($true)\n"
        "  if([int]$k.KeyChar -ne 0){[void]$b.Append($k.KeyChar)}\n"
        " } else { Start-Sleep -Milliseconds 10 }\n"
        "}\n"
        "[IO.File]::WriteAllText($Output,$b.ToString(),$e)\n",
        encoding="utf-8",
    )
    output = root / f"hold-release-terminal-{run_index}.txt"
    done = root / f"hold-release-terminal-{run_index}.done"
    title = f"DCENT-hold-terminal-{os.getpid()}-{run_index}"
    console_exe = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
    app = _OwnedApp("hold_release_console", console_exe)
    app.start(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Output",
            str(output),
            "-DonePath",
            str(done),
            "-Title",
            title,
        ],
        creationflags=_CREATE_NEW_CONSOLE,
    )
    app.window = _claim_new_window(_wait_window(title))
    return IsolatedHoldTarget(
        name="console",
        app=app,
        root=root,
        console_output=output,
        console_done=done,
    )


def open_existing_document_hold_target(
    name: str,
    root: Path,
    *,
    run_index: int,
    baseline: str,
) -> IsolatedHoldTarget:
    """Open a pre-existing user document. Never creates the file if it is missing."""

    if name not in EXISTING_DOCUMENT_APP_NAMES:
        raise ValueError(f"existing-document probe does not support {name}")

    available = discover_apps()
    backend = browser_backend_for_hold_app(name)
    executable = available.get(backend or name)
    if executable is None:
        raise RuntimeError(f"{backend or name} is not installed")

    if name in LIVE_BROWSER_TARGETS:
        return _open_live_browser_hold_target(
            name,
            executable,
            root,
            run_index=run_index,
            existing=True,
        )

    documents = existing_document_paths()
    document = documents[name]
    if not document.is_file():
        raise RuntimeError(f"existing {name} document is missing: {document}")

    if name == "notepad":
        document.write_text(baseline, encoding="utf-8")
        app = _OwnedApp("hold_release_notepad_existing", executable)
        app.start([str(executable), str(document)])
        app.window = _claim_new_window(_wait_window(document.name))
        return IsolatedHoldTarget(name=name, app=app, root=root, scratch=document)

    if name == "vscode":
        document.write_text(baseline, encoding="utf-8")
        profile = root / f"vscode-existing-profile-{run_index}"
        extensions = root / f"vscode-existing-extensions-{run_index}"
        app = _OwnedApp("hold_release_vscode_existing", executable)
        app.start(
            [
                str(executable),
                "--user-data-dir",
                str(profile),
                "--extensions-dir",
                str(extensions),
                "--disable-extensions",
                "--skip-welcome",
                "--skip-release-notes",
                "--new-window",
                str(document),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app.window = _claim_new_window(_wait_window(document.name, timeout_s=25))
        time.sleep(1.0)
        return IsolatedHoldTarget(
            name=name,
            app=app,
            root=root,
            scratch=document,
            profile=profile,
        )

    if name in BROWSER_UIA_TARGETS:
        return _open_uia_browser_hold_target(
            name,
            executable,
            root,
            run_index=run_index,
            baseline=baseline,
            existing=True,
        )

    token = f"DCENT-notes-{os.getpid()}-{time.time_ns()}"
    state = _PageState(token)
    handler = type("ExistingNotesHandler", (_PageHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = root / f"{name}-existing-profile-{run_index}"
    url = f"http://127.0.0.1:{server.server_port}/"
    app = _OwnedApp(f"hold_release_{name}_existing", executable)
    app.start(
        _chromium_hold_args(executable, profile, url),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    app.window = _claim_new_window(_wait_window(token, timeout_s=25))
    time.sleep(1.0)
    _wait_browser_page_ready(state)
    return IsolatedHoldTarget(
        name=name,
        app=app,
        root=root,
        state=state,
        server=server,
        server_thread=thread,
        profile=profile,
    )


def _rmtree_best_effort(path: Path) -> None:
    """Remove an isolated profile after the owned process tree is gone.

    Chromium can keep a metrics file mapped briefly after owned-job teardown. That is not
    a dictation failure and must not fail the hold/release probe.
    """

    if not path.exists():
        return
    for delay in (0.0, 0.25, 0.75):
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(path)
            return
        except OSError:
            continue
    shutil.rmtree(path, ignore_errors=True)


def executable_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
