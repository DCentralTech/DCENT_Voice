# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Shipped domain vocabulary. Local. No audio. No network.

These terms are always applied after ASR on the default path so Bitcoin and
D-Central product names land correctly even when the user dictionary is empty.
They are not personal data. Cloud ASR hints still use only the user's explicit
``[dictionary]`` table — never this list and never learned corrections.
"""

from __future__ import annotations

import re

from dcent_voice.config import VocabEntry

# Distinctive multi-word / product forms only. Lone "bitcoin" stays lowercase
# so the LibriSpeech-style eval item is not rewritten. "d central" stays out
# so personalization's conservative whole-utterance gate remains testable.
_SHIPPED: tuple[VocabEntry, ...] = (
    VocabEntry(spoken="d-central technologies", written="D-Central Technologies"),
    VocabEntry(spoken="d central technologies", written="D-Central Technologies"),
    VocabEntry(spoken="lightning network", written="Lightning Network"),
    VocabEntry(spoken="lightning invoice", written="Lightning invoice"),
    VocabEntry(spoken="bitcoin core", written="Bitcoin Core"),
    VocabEntry(spoken="satoshi nakamoto", written="Satoshi Nakamoto"),
    VocabEntry(spoken="nostr relay", written="Nostr relay"),
    VocabEntry(spoken="hardware wallet", written="hardware wallet"),
)


def shipped_domain_vocab() -> tuple[VocabEntry, ...]:
    return _SHIPPED


def apply_shipped_vocab(text: str) -> str:
    """Deterministic spoken→written replacements for shipped domain terms."""
    if not text:
        return text
    result = text
    ordered = sorted(_SHIPPED, key=lambda entry: len(entry.spoken), reverse=True)
    for entry in ordered:
        spoken = entry.spoken.strip()
        written = entry.written.strip()
        if not spoken or spoken == written:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(spoken)}(?!\w)", re.IGNORECASE)
        result = pattern.sub(written, result)
    return result
