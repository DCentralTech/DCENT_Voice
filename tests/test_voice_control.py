# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from dcent_voice.audio.capture import AudioCapture
from dcent_voice.devices import AudioDeviceInfo
from dcent_voice.service import voice_control as control_module
from dcent_voice.service.voice_control import VoiceControlError, VoiceRuntimeControl


class FakeWakeService:
    available = True
    enabled = False
    detail = "ready"
    device = None

    def start(self) -> None:
        self.enabled = True

    def stop(self) -> None:
        self.enabled = False

    def set_device(self, device) -> None:
        self.device = device


@pytest.fixture
def devices(monkeypatch):
    values = [
        AudioDeviceInfo("1", "USB mic", "input", 1, True, 48_000),
        AudioDeviceInfo("3", "Speakers", "output", 2, True, 48_000),
    ]
    monkeypatch.setattr(control_module, "query_audio_devices", lambda: values)
    return values


def test_mode_reports_real_wake_availability_and_lifecycle() -> None:
    wake = FakeWakeService()
    modes: list[str] = []
    control = VoiceRuntimeControl(
        AudioCapture(), wake, on_activation_mode=lambda m: modes.append(m)
    )

    enabled = control.set_mode("wake_word")
    assert enabled["mode"] == "wake_word"
    assert enabled["wakeWordAvailable"] is True
    assert enabled["wakeWordEnabled"] is True
    assert "dictation" in enabled["detail"].lower()
    assert modes == ["hold"]
    disabled = control.set_mode("push_to_talk")
    assert disabled["wakeWordEnabled"] is False
    assert disabled["mode"] == "push_to_talk"


def test_toggle_mode_notifies_host_and_describes_behavior() -> None:
    modes: list[str] = []
    control = VoiceRuntimeControl(
        AudioCapture(), FakeWakeService(), on_activation_mode=lambda m: modes.append(m)
    )
    snap = control.set_mode("toggle")
    assert snap["mode"] == "toggle"
    assert "press" in snap["detail"].lower()
    assert modes == ["toggle"]


def test_unavailable_wake_word_fails_honestly_without_changing_mode() -> None:
    wake = FakeWakeService()
    wake.available = False
    control = VoiceRuntimeControl(AudioCapture(), wake)

    snapshot = control.set_mode("wake_word")

    assert snapshot["mode"] == "push_to_talk"
    assert snapshot["wakeWordAvailable"] is False
    assert "unavailable" in snapshot["detail"].lower()


def test_device_selection_accepts_only_live_catalog_ids(devices) -> None:
    capture = AudioCapture()
    wake = FakeWakeService()
    control = VoiceRuntimeControl(capture, wake)

    control.set_device("input", "1")
    control.set_device("output", "3")
    snapshot = control.devices_snapshot()

    assert capture.device == 1
    assert wake.device == 1
    assert control.output_device == 3
    assert snapshot["selectedInput"] == "1"
    assert snapshot["selectedOutput"] == "3"
    with pytest.raises(VoiceControlError, match="currently available"):
        control.set_device("output", "999")
