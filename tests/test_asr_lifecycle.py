# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import time

from dcent_voice.asr.lifecycle import DEFAULT_IDLE_UNLOAD_S
from dcent_voice.asr.parakeet_provider import ParakeetASRProvider
from dcent_voice.config import ASRSpec


def test_default_idle_unload_is_ten_minutes() -> None:
    assert DEFAULT_IDLE_UNLOAD_S == 600.0


def test_parakeet_idle_unload_releases_and_notifies() -> None:
    provider = ParakeetASRProvider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), idle_unload_s=0.05)
    events: list[tuple[bool, str]] = []
    provider.set_lifecycle_listener(lambda ready, detail: events.append((ready, detail)))
    provider._model = object()
    provider.arm_idle_timer(provider.unload)
    deadline = time.monotonic() + 1.0
    while provider.is_loaded() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert provider.is_loaded() is False
    assert (False, "idle_unload") in events


def test_parakeet_keep_warm_zero_does_not_arm_timer() -> None:
    provider = ParakeetASRProvider(ASRSpec.parse("parakeet:tdt-0.6b-v3:int8"), idle_unload_s=0)
    provider._model = object()
    provider.arm_idle_timer(provider.unload)
    time.sleep(0.05)
    assert provider.is_loaded() is True
    assert provider._idle_timer is None
