# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Keep faster-whisper's unused file decoder out of native release artifacts.

DCENT_Voice decodes WAV input itself and calls faster-whisper with float32 PCM.
The upstream package nevertheless imports PyAV eagerly.  Installing this small
placeholder before application imports keeps that optional path fail-closed
without shipping PyAV's large codec stack.
"""

from __future__ import annotations

import sys
from types import ModuleType


class _UnavailablePyAV(ModuleType):
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(
            "PyAV file decoding is not included in DCENT_Voice; pass decoded PCM audio"
        )


sys.modules.setdefault("av", _UnavailablePyAV("av"))
