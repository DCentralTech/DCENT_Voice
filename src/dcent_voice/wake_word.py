# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Offline-only openWakeWord adapter for the optional ``hey-dcent`` model."""

from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice.config import default_config_path

WAKE_SAMPLE_RATE = 16_000
WAKE_FRAME_SAMPLES = 1_280


@dataclass(frozen=True)
class WakeWordManifest:
    model_id: str
    model_path: Path
    sha256: str
    phrase: str
    license: str
    threshold: float


def default_wake_manifest_path() -> Path:
    return default_config_path().parent / "models" / "wake-word" / "hey-dcent" / "manifest.json"


def load_wake_manifest(path: Path | None = None) -> WakeWordManifest:
    manifest_path = (path or default_wake_manifest_path()).resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw.get("engine") != "openwakeword" or raw.get("modelId") != "hey-dcent":
        raise ValueError("wake-word manifest must declare openwakeword/hey-dcent")
    relative = str(raw.get("modelPath", ""))
    if "://" in relative or Path(relative).is_absolute():
        raise ValueError("wake-word modelPath must be a local relative path")
    model_path = (manifest_path.parent / relative).resolve()
    if manifest_path.parent not in model_path.parents:
        raise ValueError("wake-word modelPath escapes its manifest directory")
    expected = str(raw.get("sha256", "")).lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError("wake-word manifest requires a SHA-256 checksum")
    actual = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("wake-word model checksum mismatch")
    threshold = float(raw.get("threshold", 0.5))
    if not 0.0 < threshold <= 1.0:
        raise ValueError("wake-word threshold must be in (0, 1]")
    return WakeWordManifest(
        model_id="hey-dcent",
        model_path=model_path,
        sha256=expected,
        phrase=str(raw.get("phrase", "hey-dcent")),
        license=str(raw.get("license", "User supplied")),
        threshold=threshold,
    )


class OpenWakeWordService:
    """Run local wake inference off the PortAudio callback on a bounded worker."""

    def __init__(
        self,
        on_detected,
        *,
        manifest_path: Path | None = None,
        device: int | str | None = None,
        cooldown_s: float = 1.5,
    ) -> None:
        self._on_detected = on_detected
        self._manifest_path = manifest_path
        self._device = device
        self._cooldown_s = cooldown_s
        self._stream: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self._last_error = ""
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        try:
            load_wake_manifest(self._manifest_path)
            from openwakeword.model import Model  # noqa: F401
        except (ImportError, OSError, ValueError, json.JSONDecodeError):
            return False
        return True

    @property
    def detail(self) -> str:
        if self._last_error:
            return self._last_error
        if self.available:
            return "Local hey-dcent model is ready."
        return "Install a checksum-pinned local hey-dcent openWakeWord model to enable wake word."

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._stream is not None

    def set_device(self, device: int | str | None) -> None:
        with self._lock:
            restart = self._stream is not None and device != self._device
        if restart:
            self.stop()
        self._device = device
        if restart:
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            manifest = load_wake_manifest(self._manifest_path)
            try:
                import sounddevice as sd
                from openwakeword.model import Model

                model = Model(
                    wakeword_models=[str(manifest.model_path)], inference_framework="onnx"
                )
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    args=(model, manifest),
                    name="WakeWordInference",
                    daemon=True,
                )
                stream = sd.RawInputStream(
                    samplerate=WAKE_SAMPLE_RATE,
                    blocksize=WAKE_FRAME_SAMPLES,
                    channels=1,
                    dtype="int16",
                    device=self._device,
                    callback=self._audio_callback,
                )
                stream.start()
            except Exception as exc:
                self._stop.set()
                self._last_error = f"Wake word unavailable: {type(exc).__name__}: {exc}"
                raise RuntimeError(self._last_error) from exc
            self._stream = stream
            self._thread.start()
            self._last_error = ""

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            thread = self._thread
            self._stream = None
            self._thread = None
            self._stop.set()
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
        if thread is not None:
            thread.join(timeout=1.0)
        while not self._frames.empty():
            with contextlib.suppress(queue.Empty):
                self._frames.get_nowait()

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        del frames, time_info, status
        data = bytes(indata)
        try:
            self._frames.put_nowait(data)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._frames.get_nowait()
            with contextlib.suppress(queue.Full):
                self._frames.put_nowait(data)

    def _run(self, model: Any, manifest: WakeWordManifest) -> None:
        last_detection = 0.0
        while not self._stop.is_set():
            try:
                raw = self._frames.get(timeout=0.1)
            except queue.Empty:
                continue
            samples = np.frombuffer(raw, dtype=np.int16)
            prediction = model.predict(samples)
            score = max((float(value) for value in prediction.values()), default=0.0)
            now = time.monotonic()
            if score >= manifest.threshold and now - last_detection >= self._cooldown_s:
                last_detection = now
                self._on_detected(manifest.phrase)
