# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import numpy as np

from dcent_voice.audio.capture import AudioCapture


def test_explicit_device_is_not_failed_over(monkeypatch) -> None:
    capture = AudioCapture(device=1)
    capture.ring.mark()
    capture.ring.write(np.zeros(1600, dtype=np.float32))
    called = {"count": 0}

    def boom(**_kwargs):
        called["count"] += 1
        raise AssertionError("explicit devices must not be scanned")

    monkeypatch.setattr("dcent_voice.audio.capture.failover_candidates", boom)
    assert capture.maybe_failover_dead_default() is None
    assert capture.device == 1
    assert capture._configured_device == 1
    assert called["count"] == 0


def test_dead_default_switches_without_writing_config(monkeypatch) -> None:
    capture = AudioCapture(device=None)
    capture.ring.mark()
    capture.ring.write(np.zeros(1600, dtype=np.float32))
    monkeypatch.setattr(
        "dcent_voice.audio.capture.portaudio_default_input",
        lambda: (1, "Microphone (Arctis 5 Chat)"),
    )
    monkeypatch.setattr(
        "dcent_voice.audio.capture.failover_candidates",
        lambda **_kwargs: [
            {
                "id": 21,
                "name": "SteelSeries Sonar - Microphone",
                "hostapi": "Windows WDM-KS",
            }
        ],
    )
    seen = capture.maybe_failover_dead_default()
    assert seen is not None
    assert seen.device == 21
    assert seen.auto_selected is True
    assert capture.device == 21
    assert capture._configured_device is None
    snap = capture.status_snapshot()
    assert snap["auto_selected"] is True
    assert snap["resolved_name"] == "SteelSeries Sonar - Microphone"
    assert snap["default_was_dead"] is True


def test_dead_alternate_advances_to_next_candidate(monkeypatch) -> None:
    capture = AudioCapture(device=None)
    capture.ring.mark()
    capture.ring.write(np.zeros(1600, dtype=np.float32))
    monkeypatch.setattr(
        "dcent_voice.audio.capture.portaudio_default_input",
        lambda: (1, "Microphone (Arctis 5 Chat)"),
    )
    monkeypatch.setattr(
        "dcent_voice.audio.capture.failover_candidates",
        lambda **_kwargs: [
            {"id": 18, "name": "Microphone (Arctis 5 Chat)", "hostapi": "WASAPI"},
            {"id": 21, "name": "SteelSeries Sonar - Microphone", "hostapi": "WDM-KS"},
        ],
    )
    first = capture.maybe_failover_dead_default()
    assert first is not None and first.device == 18
    capture._failover_armed_at = 0.0
    capture.ring.mark()
    capture.ring.write(np.zeros(1600, dtype=np.float32))
    second = capture.maybe_failover_dead_default()
    assert second is not None and second.device == 21
    assert capture._configured_device is None
