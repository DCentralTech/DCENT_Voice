# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Fail closed when an upstream caller tries to use PyAV file decoding.

DCENT_Voice always passes decoded float32 PCM to Faster Whisper.  Its upstream
package nevertheless imports ``av`` eagerly.  The offline wheelhouse uses this
small compatibility distribution instead of redistributing PyAV's unused
FFmpeg and patent-encumbered codec stack.
"""

from __future__ import annotations

__version__ = "18.0.0+dcentshim.1"


def __getattr__(name: str) -> object:
    raise RuntimeError("PyAV file decoding is not included in DCENT_Voice; pass decoded PCM audio")
