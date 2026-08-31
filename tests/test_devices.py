# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path

import pytest

from dcent_voice.config import load_config
from dcent_voice.devices import (
    DeviceBenchError,
    InputDeviceInfo,
    parse_samples_ms,
    run_device_bench,
)


def test_device_bench_suggests_desktop_when_gpu_finalize_meets_target() -> None:
    config = load_config(Path("config.example.toml"), create=False)

    report = run_device_bench(
        config,
        device_class="gpu",
        samples_ms=[420, 455, 480],
        input_devices=[InputDeviceInfo(name="Studio Mic", channels=1)],
    )

    assert report.status == "meets-target"
    assert report.threshold_ms == 500
    assert report.p50_finalize_ms == 455
    assert report.suggested_profile == "gpu"
    assert report.suggested_asr == "faster-whisper:distil-small.en:cuda-float16"
    assert report.cleanup_enabled is False


def test_device_bench_suggests_desktop_cpu_when_cpu_finalize_meets_target() -> None:
    config = load_config(Path("config.example.toml"), create=False)

    report = run_device_bench(
        config,
        device_class="cpu",
        samples_ms=[650, 760, 790],
        input_devices=[InputDeviceInfo(name="Laptop Mic", channels=1)],
    )

    assert report.status == "meets-target"
    assert report.threshold_ms == 800
    assert report.p50_finalize_ms == 760
    assert report.suggested_profile == "desktop"
    assert report.suggested_asr == "parakeet:tdt-0.6b-v3:int8"


def test_device_bench_falls_back_to_tiny_when_finalize_exceeds_target() -> None:
    config = load_config(Path("config.example.toml"), create=False)

    report = run_device_bench(
        config,
        device_class="gpu",
        samples_ms=[510, 530, 550],
        input_devices=[InputDeviceInfo(name="USB Mic", channels=1)],
    )

    assert report.status == "over-target"
    assert report.suggested_profile == "tiny"
    assert report.cleanup_enabled is False
    assert "above the 500ms gpu target" in report.reason


def test_device_bench_surfaces_missing_input_device() -> None:
    config = load_config(Path("config.example.toml"), create=False)

    report = run_device_bench(
        config,
        device_class="cpu",
        samples_ms=[300, 320, 340],
        input_devices=[],
    )

    assert report.status == "no-input-device"
    assert report.input_device_count == 0
    assert report.suggested_profile == "tiny"


def test_parse_samples_rejects_bad_values() -> None:
    with pytest.raises(DeviceBenchError, match="Invalid latency sample"):
        parse_samples_ms("420, nope")
