# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from dcent_voice.runtime_settings import VoiceRuntimeSettings


def test_runtime_settings_reads_dcent_voice_environment(monkeypatch) -> None:
    monkeypatch.setenv("DCENT_VOICE_FAKE_AUDIO", "true")
    monkeypatch.setenv("DCENT_VOICE_SERVICE_PORT", "9988")

    settings = VoiceRuntimeSettings()

    assert settings.fake_audio is True
    assert settings.service_port == 9988
