# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Skip tests that must not enter Win32 ctypes / UIA / Windows EXE on Linux."""

from __future__ import annotations

import sys

import pytest

WIN32_NATIVE_REASON = "requires Win32 ctypes/UIA/Windows EXE"

requires_win32_native = pytest.mark.skipif(
    sys.platform != "win32",
    reason=WIN32_NATIVE_REASON,
)
