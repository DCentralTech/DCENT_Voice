# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Optional Piper fallback using only user-supplied, checksum-pinned local assets."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import threading
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice.tts.assets import tts_model_dir
from dcent_voice.tts.base import AudioChunk, TtsCapability, TtsUnavailable
from dcent_voice.tts.kokoro import _chunk


class PiperTtsBackend:
    """Load Piper only when an offline manifest proves model identity and license."""

    name = "piper"

    def __init__(self, *, model_root: Path | None = None, chunk_ms: int = 40) -> None:
        self._dir = tts_model_dir("piper", root=model_root)
        self.chunk_ms = chunk_ms
        self._cancel = threading.Event()
        self._voice: Any | None = None
        self._lock = threading.Lock()

    def _manifest(self) -> dict[str, Any]:
        path = self._dir / "manifest.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("engine") != "piper":
            raise ValueError("Piper manifest must declare engine=piper")
        license_name = str(raw.get("license", "")).strip()
        if not license_name:
            raise ValueError("Piper manifest must declare the selected voice license")
        for key in ("modelPath", "configPath"):
            relative = str(raw.get(key, ""))
            if "://" in relative or Path(relative).is_absolute():
                raise ValueError(f"Piper {key} must be a local relative path")
            resolved = (self._dir / relative).resolve()
            if self._dir.resolve() not in resolved.parents:
                raise ValueError(f"Piper {key} escapes its model directory")
            expected = str(raw.get("sha256", {}).get(key, "")).lower()
            if len(expected) != 64 or hashlib.sha256(resolved.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Piper {key} checksum mismatch")
        return raw

    def available(self) -> bool:
        try:
            self._manifest()
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        try:
            return importlib.util.find_spec("piper.voice") is not None
        except ModuleNotFoundError:
            return False

    def capability(self) -> TtsCapability:
        try:
            manifest = self._manifest()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return TtsCapability(
                name=self.name,
                available=False,
                sample_rate=0,
                license="User supplied",
                detail=f"Piper local manifest unavailable: {exc}",
            )
        return TtsCapability(
            name=self.name,
            available=self.available(),
            sample_rate=int(manifest.get("sampleRate", 0) or 0),
            voice=str(manifest.get("voice", "")),
            license=str(manifest["license"]),
            detail="Piper via a checksum-pinned local voice manifest.",
        )

    def _load(self):
        with self._lock:
            if self._voice is not None:
                return self._voice
            try:
                manifest = self._manifest()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise TtsUnavailable(str(exc)) from exc
            try:
                from piper.voice import PiperVoice
            except ImportError as exc:
                raise TtsUnavailable("Install the optional Piper runtime locally.") from exc
            model = self._dir / str(manifest["modelPath"])
            config = self._dir / str(manifest["configPath"])
            self._voice = PiperVoice.load(str(model), config_path=str(config))
            return self._voice

    def synthesize(self, text: str) -> Iterator[AudioChunk]:
        self._cancel.clear()
        voice = self._load()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            synthesize = getattr(voice, "synthesize_wav", None) or getattr(
                voice, "synthesize", None
            )
            if synthesize is None:
                raise TtsUnavailable("The installed Piper runtime has no WAV synthesis API.")
            synthesize(text, wav_file)
        buffer.seek(0)
        with wave.open(buffer, "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            width = wav_file.getsampwidth()
            channels = wav_file.getnchannels()
            if width != 2 or channels != 1:
                raise TtsUnavailable("Piper must emit mono 16-bit PCM.")
            pcm = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i2")
        samples = pcm.astype(np.float32) / np.float32(32768.0)
        yield from _chunk(samples, sample_rate, self.chunk_ms, self._cancel)

    def cancel(self) -> None:
        self._cancel.set()
