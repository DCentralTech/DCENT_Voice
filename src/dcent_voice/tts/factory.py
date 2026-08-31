# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Select the public-beta TTS backend from configuration."""

from __future__ import annotations

from pathlib import Path

from dcent_voice.config import TtsConfig
from dcent_voice.tts.base import TtsBackend
from dcent_voice.tts.kokoro import KokoroTtsBackend
from dcent_voice.tts.piper import PiperTtsBackend


def build_tts_backend(config: TtsConfig, *, model_root: Path | None = None) -> TtsBackend | None:
    """Return an available backend for ``config``, or ``None`` if none is ready.

    ``kokoro`` returns only when its runtime and assets are actually available.
    Piper never downloads a voice: it is selected only when a local manifest
    pins both model files and declares their license. Returning ``None`` keeps
    ``tts.*`` unadvertised through DVAP.
    """

    if not config.enabled:
        return None

    kokoro = KokoroTtsBackend(voice=config.voice, model_root=model_root)
    if config.backend == "kokoro":
        return kokoro if kokoro.available() else None
    if config.backend == "piper":
        piper = PiperTtsBackend(model_root=model_root)
        return piper if piper.available() else None
    # auto
    if kokoro.available():
        return kokoro
    piper = PiperTtsBackend(model_root=model_root)
    return piper if piper.available() else None
