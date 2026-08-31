# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Implement Windows-specific overlay positioning and behavior."""

from __future__ import annotations

import ctypes
import platform
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
MONITOR_DEFAULTTONEAREST = 2


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def apply_overlay_window_styles(hwnd: int | None) -> None:
    if platform.system() != "Windows" or not hwnd:
        return
    style = _get_window_long(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    _set_window_long(hwnd, GWL_EXSTYLE, style)


def bottom_center_position(
    anchor_hwnd: int | None, width: int, height: int, *, margin: int = 72
) -> tuple[int, int]:
    rect = monitor_rect_for_window(anchor_hwnd)
    x = rect.left + max(0, (rect.width - width) // 2)
    y = rect.bottom - height - margin
    return x, max(rect.top, y)


def overlay_position(
    anchor_hwnd: int | None,
    width: int,
    height: int,
    position: str,
    *,
    margin: int = 72,
) -> tuple[int, int]:
    return position_in_rect(
        monitor_rect_for_window(anchor_hwnd), width, height, position, margin=margin
    )


def position_in_rect(
    rect: Rect, width: int, height: int, position: str, *, margin: int = 72
) -> tuple[int, int]:
    x = rect.left + max(0, (rect.width - width) // 2)
    if position == "top-center":
        y = rect.top + margin
    elif position == "center":
        y = rect.top + max(0, (rect.height - height) // 2)
    else:
        y = rect.bottom - height - margin
    return x, min(max(rect.top, y), max(rect.top, rect.bottom - height))


def monitor_rect_for_window(hwnd: int | None) -> Rect:
    if platform.system() != "Windows":
        return Rect(0, 0, 1920, 1080)
    if hwnd:
        monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    else:
        monitor = _user32.MonitorFromWindow(_user32.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if monitor and _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return Rect(info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
    return Rect(
        0,
        0,
        int(_user32.GetSystemMetrics(0)),
        int(_user32.GetSystemMetrics(1)),
    )


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _get_window_long(hwnd: int, index: int) -> int:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        return int(_user32.GetWindowLongPtrW(hwnd, index))
    return int(_user32.GetWindowLongW(hwnd, index))


def _set_window_long(hwnd: int, index: int, value: int) -> None:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _user32.SetWindowLongPtrW(hwnd, index, value)
    else:
        _user32.SetWindowLongW(hwnd, index, value)


_user32: Any
if platform.system() == "Windows":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
    _user32.SetWindowLongPtrW.restype = ctypes.c_longlong
else:  # pragma: no cover - Windows-only implementation guard
    _user32 = None
