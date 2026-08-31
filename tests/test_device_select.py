# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from dcent_voice.audio.device_select import (
    _unique_alternates,
    resolve_input_device,
)
from dcent_voice.pipeline import _discard_message


def _inputs() -> list[dict[str, object]]:
    return [
        {"id": 1, "name": "Microphone (Arctis 5 Chat)", "hostapi": "MME"},
        {"id": 8, "name": "Microphone (Arctis 5 Chat)", "hostapi": "Windows DirectSound"},
        {"id": 21, "name": "SteelSeries Sonar - Microphone", "hostapi": "Windows WDM-KS"},
        {"id": 0, "name": "Microsoft Sound Mapper - Input", "hostapi": "MME"},
    ]


def test_explicit_device_is_never_auto_replaced() -> None:
    resolved = resolve_input_device(
        1,
        probe_rms=lambda _device: 0.0,
        list_inputs=_inputs,
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert resolved.device == 1
    assert resolved.auto_selected is False
    assert resolved.reason == "configured"
    assert resolved.name == "Microphone (Arctis 5 Chat)"


def test_live_os_default_is_kept() -> None:
    resolved = resolve_input_device(
        None,
        probe_rms=lambda device: 0.04 if device in {None, 1} else 0.0,
        list_inputs=_inputs,
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert resolved.device is None
    assert resolved.auto_selected is False
    assert resolved.reason == "os_default_live"


def test_dead_default_failsover_to_live_alternate() -> None:
    def probe(device):
        if device == 21:
            return 0.078
        return 0.00002

    resolved = resolve_input_device(
        None,
        probe_rms=probe,
        list_inputs=_inputs,
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert resolved.device == 21
    assert resolved.auto_selected is True
    assert resolved.default_was_dead is True
    assert resolved.reason == "auto_live_alternate"
    assert resolved.name == "SteelSeries Sonar - Microphone"


def test_dead_default_without_live_alternate_fails_closed() -> None:
    resolved = resolve_input_device(
        None,
        probe_rms=lambda _device: 0.00002,
        list_inputs=_inputs,
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert resolved.device is None
    assert resolved.auto_selected is False
    assert resolved.default_was_dead is True
    assert resolved.reason == "os_default_dead_no_alternate"
    assert resolved.no_audio_message() == "No audio from Microphone (Arctis 5 Chat)"


def test_mapper_and_duplicate_default_name_are_not_alternates() -> None:
    seen: list[object] = []

    def probe(device):
        seen.append(device)
        return 0.00002

    resolve_input_device(
        None,
        probe_rms=probe,
        list_inputs=_inputs,
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert 0 not in seen
    assert 8 not in seen


def test_sonar_virtual_capture_is_tried_before_other_hosts() -> None:
    ranked = _unique_alternates(
        _inputs(),
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert ranked[0]["id"] == 21
    assert "Sonar" in ranked[0]["name"]


def test_quiet_os_default_is_kept() -> None:
    resolved = resolve_input_device(
        None,
        probe_rms=lambda device: 0.001 if device in {None, 1} else 0.08,
        list_inputs=_inputs,
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert resolved.device is None
    assert resolved.auto_selected is False
    assert resolved.reason == "os_default_quiet"


def test_invalid_alternate_is_skipped_for_later_live_device() -> None:
    def probe(device):
        if device == 9:
            raise RuntimeError("Invalid device")
        if device == 21:
            return 0.078
        return 0.00002

    resolved = resolve_input_device(
        None,
        probe_rms=probe,
        list_inputs=lambda: _inputs() + [{"id": 9, "name": "Broken endpoint", "hostapi": "MME"}],
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
    )
    assert resolved.device == 21
    assert resolved.reason == "auto_live_alternate"


def test_known_default_rms_skips_default_probe() -> None:
    seen: list[object] = []

    def probe(device):
        seen.append(device)
        return 0.08 if device == 21 else 0.0

    resolved = resolve_input_device(
        None,
        probe_rms=probe,
        list_inputs=_inputs,
        default_index=1,
        default_name="Microphone (Arctis 5 Chat)",
        known_default_rms=0.00002,
    )
    assert resolved.device == 21
    assert 1 not in seen
    assert None not in seen


def test_no_audio_discard_names_the_microphone() -> None:
    assert _discard_message("no_audio") == "No audio from selected microphone"
    assert _discard_message("silence") == "No speech detected"
