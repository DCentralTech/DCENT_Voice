# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import numpy as np

from dcent_voice.audio.vad import EnergyVAD


def test_energy_vad_detects_silence_and_speech() -> None:
    vad = EnergyVAD(threshold=0.01)

    assert vad.is_speech(np.zeros(1600, dtype=np.float32)).speech is False
    assert vad.is_speech(np.ones(1600, dtype=np.float32) * 0.05).speech is True
