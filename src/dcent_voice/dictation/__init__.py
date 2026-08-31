# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Local-only dictation post-processing: polish, spoken edits, snippets, dev terms."""

from __future__ import annotations

from dcent_voice.dictation.postprocess import (
    apply_dictation_postprocess,
    apply_snippets,
    apply_spoken_edits,
    compose_dictation,
    extract_last_correction,
    extract_spoken_corrections,
    is_undo_last_command,
    local_polish,
    peel_spoken_cleanup,
    peel_spoken_press_enter,
)
from dcent_voice.dictation.style import apply_style, peel_spoken_style, resolve_style

__all__ = [
    "apply_dictation_postprocess",
    "apply_snippets",
    "apply_spoken_edits",
    "apply_style",
    "compose_dictation",
    "extract_last_correction",
    "extract_spoken_corrections",
    "is_undo_last_command",
    "local_polish",
    "peel_spoken_cleanup",
    "peel_spoken_press_enter",
    "peel_spoken_style",
    "resolve_style",
]
