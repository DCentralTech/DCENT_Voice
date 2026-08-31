# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Thread-safe DVAP v1.2 voice mode and audio-device controller."""

from __future__ import annotations

import threading
from typing import Any

from dcent_voice.audio.capture import AudioCapture
from dcent_voice.devices import AudioDeviceInfo, query_audio_devices

VOICE_MODES = frozenset({"push_to_talk", "toggle", "wake_word"})


class VoiceControlError(ValueError):
    pass


class VoiceRuntimeControl:
    def __init__(
        self,
        capture: AudioCapture,
        wake_service: Any | None = None,
        *,
        on_activation_mode: Any | None = None,
    ) -> None:
        self._capture = capture
        self._wake = wake_service
        # Optional callback(mode: str) so the host can reconfigure hotkey
        # hold/toggle behavior when ADE changes activation mode.
        self._on_activation_mode = on_activation_mode
        self._mode = "push_to_talk"
        self._output_device: int | str | None = None
        self._lock = threading.RLock()

    @property
    def output_device(self) -> int | str | None:
        with self._lock:
            return self._output_device

    def mode_snapshot(self, detail: str = "") -> dict[str, Any]:
        with self._lock:
            available = bool(self._wake is not None and self._wake.available)
            enabled = bool(self._wake is not None and self._wake.enabled)
            message = detail or (self._wake.detail if self._wake is not None else "")
            value: dict[str, Any] = {
                "type": "voice.mode",
                "mode": self._mode,
                "wakeWordAvailable": available,
                "wakeWordEnabled": enabled,
            }
            if message:
                value["detail"] = message
            return value

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in VOICE_MODES:
            raise VoiceControlError(f"unsupported voice mode: {mode!r}")
        with self._lock:
            if mode == "wake_word":
                if self._wake is None or not self._wake.available:
                    return self.mode_snapshot(
                        "Wake word is unavailable; push-to-talk remains active."
                    )
                try:
                    self._wake.start()
                except RuntimeError as exc:
                    return self.mode_snapshot(str(exc))
                self._mode = "wake_word"
                detail = (
                    "Wake word listening: say the phrase to start dictation; "
                    "silence ends the utterance."
                )
                if self._on_activation_mode is not None:
                    self._on_activation_mode("hold")
                return self.mode_snapshot(detail)
            if self._wake is not None:
                self._wake.stop()
            self._mode = mode
            # push_to_talk and toggle map onto hotkey hold vs press-to-toggle.
            if self._on_activation_mode is not None:
                self._on_activation_mode("toggle" if mode == "toggle" else "hold")
            if mode == "toggle":
                return self.mode_snapshot(
                    "Toggle mode: press the dictation hotkey to start, press again to stop."
                )
            return self.mode_snapshot("Push-to-talk: hold the dictation hotkey to speak.")

    def devices_snapshot(self) -> dict[str, Any]:
        devices = query_audio_devices()
        return {
            "type": "voice.devices",
            "inputs": [item.to_dvap() for item in devices if item.kind == "input"],
            "outputs": [item.to_dvap() for item in devices if item.kind == "output"],
            "selectedInput": _device_id(self._capture.device),
            "selectedOutput": _device_id(self.output_device),
        }

    def set_device(self, kind: str, device_id: str | None) -> dict[str, Any]:
        if kind not in {"input", "output"}:
            raise VoiceControlError("voice.device.set.kind must be input or output")
        device = _resolve_device(kind, device_id, query_audio_devices())
        with self._lock:
            if kind == "input":
                self._capture.set_device(device)
                if self._wake is not None:
                    self._wake.set_device(device)
            else:
                self._output_device = device
        return self.devices_snapshot()

    def close(self) -> None:
        if self._wake is not None:
            self._wake.stop()


def _device_id(device: int | str | None) -> str | None:
    return None if device is None else str(device)


def _resolve_device(kind: str, device_id: str | None, devices: list[AudioDeviceInfo]) -> int | None:
    if device_id is None:
        return None
    if not isinstance(device_id, str) or not device_id.isdigit():
        raise VoiceControlError("deviceId must be a listed numeric device id or null")
    if not any(item.kind == kind and item.id == device_id for item in devices):
        raise VoiceControlError("deviceId is not a currently available device of that kind")
    return int(device_id)
