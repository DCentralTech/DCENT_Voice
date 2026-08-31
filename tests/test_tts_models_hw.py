# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Opt-in real-model TTS synthesis (skipped in CI).

Run with ``pytest --run-tts-models`` on a machine that has the Kokoro library
installed and the model assets downloaded (via the consent-gated
``dcent_voice.tts.assets`` path). CI never downloads models, so these are marked
``tts_models`` and skipped by default.
"""

from __future__ import annotations

import numpy as np
import pytest

from dcent_voice.tts.kokoro import KokoroTtsBackend

pytestmark = pytest.mark.tts_models


@pytest.mark.parametrize("factory", [KokoroTtsBackend])
def test_real_backend_synthesizes_audio(factory) -> None:
    backend = factory()
    if not backend.available():
        pytest.skip(f"{backend.name} model assets/library not present")
    chunks = list(backend.synthesize("Hello from DCENT Voice."))
    assert chunks, "no audio produced"
    audio = np.concatenate([chunk.samples for chunk in chunks])
    assert audio.dtype == np.float32
    assert audio.size > 0
    assert float(np.abs(audio).max()) > 0.0
