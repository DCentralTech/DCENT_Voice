# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Inspect and benchmark available microphone devices."""

from __future__ import annotations

import json
import statistics
import urllib.error
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .local_http import LocalEndpointError, open_local_http

VoiceDeviceClass = str


class DeviceBenchError(ValueError):
    """Raised when device bench input cannot produce a useful recommendation."""


@dataclass(frozen=True)
class InputDeviceInfo:
    name: str
    channels: int
    default_samplerate: float | None = None


@dataclass(frozen=True)
class AudioDeviceInfo:
    id: str
    name: str
    kind: str
    channels: int
    default: bool = False
    sample_rate: float | None = None

    def to_dvap(self) -> dict[str, object]:
        value: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "channels": self.channels,
            "default": self.default,
        }
        if self.sample_rate is not None:
            value["sampleRate"] = self.sample_rate
        return value


@dataclass(frozen=True)
class DeviceBenchReport:
    device_class: VoiceDeviceClass
    input_device_count: int
    sample_count: int
    threshold_ms: int
    p50_finalize_ms: float | None
    suggested_profile: str
    suggested_asr: str
    suggested_llm: str
    cleanup_enabled: bool
    status: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "device_class": self.device_class,
            "input_device_count": self.input_device_count,
            "sample_count": self.sample_count,
            "threshold_ms": self.threshold_ms,
            "p50_finalize_ms": self.p50_finalize_ms,
            "suggested_profile": self.suggested_profile,
            "suggested_asr": self.suggested_asr,
            "suggested_llm": self.suggested_llm,
            "cleanup_enabled": self.cleanup_enabled,
            "status": self.status,
            "reason": self.reason,
        }


def run_device_bench(
    config: AppConfig,
    *,
    device_class: VoiceDeviceClass,
    samples_ms: list[float] | None = None,
    input_devices: list[InputDeviceInfo] | None = None,
    assumed_input_devices: int | None = None,
) -> DeviceBenchReport:
    normalized_class = normalize_device_class(device_class)
    devices = input_devices if input_devices is not None else query_input_devices()
    input_device_count = (
        assumed_input_devices if assumed_input_devices is not None else len(devices)
    )
    samples = samples_ms or []
    p50 = percentile(samples, 0.5) if samples else None
    threshold = voice_threshold_ms(normalized_class)
    suggested_profile, status, reason = choose_profile(
        config,
        device_class=normalized_class,
        input_device_count=input_device_count,
        p50_finalize_ms=p50,
        threshold_ms=threshold,
    )
    profile = config.profiles[suggested_profile]

    return DeviceBenchReport(
        device_class=normalized_class,
        input_device_count=input_device_count,
        sample_count=len(samples),
        threshold_ms=threshold,
        p50_finalize_ms=p50,
        suggested_profile=suggested_profile,
        suggested_asr=profile.asr.raw,
        suggested_llm=profile.llm.raw,
        cleanup_enabled=profile.cleanup_enabled,
        status=status,
        reason=reason,
    )


def choose_profile(
    config: AppConfig,
    *,
    device_class: VoiceDeviceClass,
    input_device_count: int,
    p50_finalize_ms: float | None,
    threshold_ms: int,
) -> tuple[str, str, str]:
    if input_device_count <= 0:
        return (
            fallback_profile(config, "tiny"),
            "no-input-device",
            (
                "No input microphone was detected; use the tiny profile until an input device "
                "is available."
            ),
        )

    if p50_finalize_ms is None:
        if device_class == "gpu":
            return (
                fallback_profile(config, "gpu"),
                "needs-latency-samples",
                (
                    "GPU class was selected, but no finalize samples were supplied; the gpu "
                    "profile is the best starting point before a real speech bench."
                ),
            )
        return (
            fallback_profile(config, "desktop"),
            "needs-latency-samples",
            (
                "No finalize samples were supplied; desktop (CPU int8) is the default for "
                "machines without a high-end GPU."
            ),
        )

    if p50_finalize_ms >= threshold_ms:
        return (
            fallback_profile(config, "tiny"),
            "over-target",
            (
                f"Finalize p50 {p50_finalize_ms:.1f}ms is above the {threshold_ms}ms "
                f"{device_class} target; use tiny until hardware or model settings are tuned."
            ),
        )

    if device_class == "gpu":
        return (
            fallback_profile(config, "gpu"),
            "meets-target",
            (f"Finalize p50 {p50_finalize_ms:.1f}ms is under the {threshold_ms}ms GPU target."),
        )

    return (
        fallback_profile(config, "desktop"),
        "meets-target",
        (f"Finalize p50 {p50_finalize_ms:.1f}ms is under the {threshold_ms}ms CPU target."),
    )


def fallback_profile(config: AppConfig, preferred: str) -> str:
    if preferred in config.profiles:
        return preferred
    if config.active_profile in config.profiles:
        return config.active_profile
    try:
        return next(iter(config.profiles))
    except StopIteration as exc:  # pragma: no cover - config parser prevents this.
        raise DeviceBenchError("No profiles are available for device recommendation.") from exc


def parse_samples_ms(raw: str | None) -> list[float]:
    if not raw:
        return []

    samples: list[float] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        try:
            sample = float(value)
        except ValueError as exc:
            raise DeviceBenchError(f"Invalid latency sample: {value!r}") from exc
        if sample < 0:
            raise DeviceBenchError("Latency samples must be non-negative.")
        samples.append(sample)
    return samples


def fetch_samples_ms(url: str, *, opener: Any | None = None) -> list[float]:
    """Read latency samples from an explicitly local, isolated HTTP endpoint."""

    try:
        with open_local_http(url, timeout=10, opener=opener) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except LocalEndpointError as exc:
        raise DeviceBenchError(str(exc)) from exc
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise DeviceBenchError(f"Could not read local bench samples: {exc}") from exc

    value = (
        payload.get("finalizeMs", payload.get("samplesMs")) if isinstance(payload, dict) else None
    )
    if not isinstance(value, list):
        raise DeviceBenchError("Bench URL must return JSON with finalizeMs or samplesMs array.")
    return parse_samples_ms(",".join(str(sample) for sample in value))


def query_input_devices(sounddevice_module: Any | None = None) -> list[InputDeviceInfo]:
    try:
        sd = sounddevice_module or __import__("sounddevice")
        raw_devices = sd.query_devices()
    except Exception:
        return []

    devices: list[InputDeviceInfo] = []
    for device in raw_devices:
        channels = int(device.get("max_input_channels", 0) or 0)
        if channels <= 0:
            continue
        samplerate = device.get("default_samplerate")
        devices.append(
            InputDeviceInfo(
                name=str(device.get("name", "Unknown input")),
                channels=channels,
                default_samplerate=float(samplerate) if samplerate is not None else None,
            )
        )
    return devices


def query_audio_devices(sounddevice_module: Any | None = None) -> list[AudioDeviceInfo]:
    """Return stable-in-this-process PortAudio device ids for DVAP selection."""

    try:
        sd = sounddevice_module or __import__("sounddevice")
        raw_devices = sd.query_devices()
        default_pair = getattr(sd.default, "device", (None, None))
        default_input = int(default_pair[0]) if default_pair[0] is not None else None
        default_output = int(default_pair[1]) if default_pair[1] is not None else None
    except Exception:
        return []

    devices: list[AudioDeviceInfo] = []
    for index, device in enumerate(raw_devices):
        sample_rate = device.get("default_samplerate")
        for kind, key, default_id in (
            ("input", "max_input_channels", default_input),
            ("output", "max_output_channels", default_output),
        ):
            channels = int(device.get(key, 0) or 0)
            if channels <= 0:
                continue
            devices.append(
                AudioDeviceInfo(
                    id=str(index),
                    name=str(device.get("name", f"Unknown {kind}")),
                    kind=kind,
                    channels=channels,
                    default=index == default_id,
                    sample_rate=float(sample_rate) if sample_rate is not None else None,
                )
            )
    return devices


def normalize_device_class(value: str) -> VoiceDeviceClass:
    normalized = value.strip().lower()
    if normalized not in {"gpu", "cpu"}:
        raise DeviceBenchError("device class must be 'gpu' or 'cpu'.")
    return normalized


def voice_threshold_ms(device_class: VoiceDeviceClass) -> int:
    return 500 if normalize_device_class(device_class) == "gpu" else 800


def percentile(values: list[float], q: float) -> float:
    if not values:
        raise DeviceBenchError("At least one latency sample is required.")
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    return statistics.quantiles(ordered, n=100, method="inclusive")[int(q * 100) - 1]


def format_device_bench_report(report: DeviceBenchReport) -> str:
    lines = [
        "DCENT_Voice device bench",
        f"device_class: {report.device_class}",
        f"input_devices: {report.input_device_count}",
        f"sample_count: {report.sample_count}",
        f"threshold_ms: {report.threshold_ms}",
        f"p50_finalize_ms: {format_ms(report.p50_finalize_ms)}",
        f"suggested_profile: {report.suggested_profile}",
        f"suggested_asr: {report.suggested_asr}",
        f"suggested_llm: {report.suggested_llm}",
        f"cleanup_enabled: {str(report.cleanup_enabled).lower()}",
        f"status: {report.status}",
        f"reason: {report.reason}",
    ]
    return "\n".join(lines)


def format_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"
