# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Resolve a live input when the OS-default endpoint is electrically dead.

Shipped ``input_device = ""`` means PortAudio's default. On SteelSeries Sonar
hosts that default is often a muted Chat mic while a virtual capture endpoint
carries the signal. This module never uploads audio and never writes config.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

# Chat-silence on this host was ~0.00002–0.00007. Ambient speech and the Sonar
# fixture sit well above 0.01. Stay below the pipeline speech threshold (0.005)
# so a quiet room is not treated as a dead cable.
DEAD_INPUT_RMS = 0.0005
LIVE_INPUT_RMS = 0.002

_MAPPER_FRAGMENTS = (
    "sound mapper",
    "primary sound capture",
    "primary sound driver",
)


@dataclass(frozen=True)
class ResolvedInputDevice:
    device: int | str | None
    name: str
    auto_selected: bool
    default_was_dead: bool
    reason: str

    def no_audio_message(self) -> str:
        label = self.name or "selected microphone"
        return f"No audio from {label}"


def is_dead_rms(rms: float) -> bool:
    return float(rms) < DEAD_INPUT_RMS


def is_live_rms(rms: float) -> bool:
    return float(rms) >= LIVE_INPUT_RMS


def resolve_input_device(
    configured: int | str | None,
    *,
    probe_rms: Callable[[int | str | None], float],
    list_inputs: Callable[[], list[dict[str, Any]]],
    default_index: int | None = None,
    default_name: str = "",
    known_default_rms: float | None = None,
) -> ResolvedInputDevice:
    """Pick a capture endpoint without silently changing a user-set device.

    Explicit ``configured`` values are honored. Only the shipped empty default
    may fail over from a dead OS-default endpoint to a live alternate.
    """

    if configured is not None and configured != "":
        name = _name_for(configured, list_inputs()) or str(configured)
        return ResolvedInputDevice(
            device=configured,
            name=name,
            auto_selected=False,
            default_was_dead=False,
            reason="configured",
        )

    default_rms = (
        float(known_default_rms)
        if known_default_rms is not None
        else float(probe_rms(default_index))
    )
    default_label = (
        default_name or _name_for(default_index, list_inputs()) or "system default microphone"
    )
    if is_live_rms(default_rms):
        return ResolvedInputDevice(
            device=None,
            name=default_label,
            auto_selected=False,
            default_was_dead=False,
            reason="os_default_live",
        )

    default_dead = is_dead_rms(default_rms)
    if not default_dead:
        return ResolvedInputDevice(
            device=None,
            name=default_label,
            auto_selected=False,
            default_was_dead=False,
            reason="os_default_quiet",
        )

    best: tuple[float, int | str, str] | None = None
    seen_names: set[str] = set()
    for item in _unique_alternates(list_inputs(), default_index, default_label):
        name = str(item["name"])
        folded = name.casefold()
        if folded in seen_names:
            continue
        seen_names.add(folded)
        try:
            rms = float(probe_rms(item["id"]))
        except Exception:
            continue
        if not is_live_rms(rms):
            continue
        if best is None or rms > best[0]:
            best = (rms, item["id"], name)

    if best is not None:
        return ResolvedInputDevice(
            device=best[1],
            name=best[2],
            auto_selected=True,
            default_was_dead=True,
            reason="auto_live_alternate",
        )
    return ResolvedInputDevice(
        device=None,
        name=default_label,
        auto_selected=False,
        default_was_dead=True,
        reason="os_default_dead_no_alternate",
    )


def probe_device_rms(device: int | str | None, *, seconds: float = 0.18) -> float:
    """Local-only RMS probe. Audio never leaves the process."""

    import sounddevice as sd

    try:
        info = sd.query_devices(device, kind="input")
        rate = int(info.get("default_samplerate") or 16000)
        if rate < 8000:
            rate = 16000
        frames = max(1, int(seconds * rate))
        recorded = sd.rec(
            frames,
            samplerate=rate,
            channels=1,
            dtype="float32",
            device=device,
        )
        sd.wait()
    except Exception:
        return 0.0
    audio = np.asarray(recorded, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio))))


def list_portaudio_inputs() -> list[dict[str, Any]]:
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    devices: list[dict[str, Any]] = []
    for index, raw in enumerate(sd.query_devices()):
        if int(raw.get("max_input_channels") or 0) <= 0:
            continue
        host_index = int(raw.get("hostapi", -1))
        host = str(hostapis[host_index].get("name", "")) if 0 <= host_index < len(hostapis) else ""
        devices.append(
            {
                "id": index,
                "name": str(raw.get("name", f"input {index}")),
                "hostapi": host,
            }
        )
    return devices


def portaudio_default_input() -> tuple[int | None, str]:
    import sounddevice as sd

    try:
        index = int(sd.default.device[0])
    except Exception:
        return None, ""
    try:
        name = str(sd.query_devices(index, kind="input").get("name", ""))
    except Exception:
        name = ""
    return index, name


def failover_candidates(
    *,
    default_index: int | None = None,
    default_name: str = "",
) -> list[dict[str, Any]]:
    if default_index is None and not default_name:
        default_index, default_name = portaudio_default_input()
    return _unique_alternates(list_portaudio_inputs(), default_index, default_name)


def resolve_empty_input_device(
    *,
    known_default_rms: float | None = None,
) -> ResolvedInputDevice:
    default_index, default_name = portaudio_default_input()
    return resolve_input_device(
        None,
        probe_rms=probe_device_rms,
        list_inputs=list_portaudio_inputs,
        default_index=default_index,
        default_name=default_name,
        known_default_rms=known_default_rms,
    )


def _name_for(device: int | str | None, inputs: list[dict[str, Any]]) -> str:
    if device is None:
        return ""
    for item in inputs:
        if item["id"] == device or str(item["id"]) == str(device):
            return str(item["name"])
        if str(item["name"]) == str(device):
            return str(item["name"])
    return ""


def _is_mapper(name: str) -> bool:
    folded = name.casefold()
    return any(fragment in folded for fragment in _MAPPER_FRAGMENTS)


def _unique_alternates(
    inputs: list[dict[str, Any]],
    default_index: int | None,
    default_name: str,
) -> list[dict[str, Any]]:
    default_folded = default_name.casefold()
    ranked: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for item in inputs:
        name = str(item["name"])
        if _is_mapper(name):
            continue
        if default_index is not None and item["id"] == default_index:
            continue
        if default_folded and name.casefold() == default_folded:
            continue
        host = str(item.get("hostapi", "")).casefold()
        host_rank = 0 if "wdm-ks" in host else 1 if "wasapi" in host else 2
        sonar_rank = 0 if "sonar" in name.casefold() else 1
        ranked.append(((sonar_rank, host_rank), item))
    ranked.sort(key=lambda pair: (pair[0], str(pair[1]["id"])))
    return [item for _rank, item in ranked]
