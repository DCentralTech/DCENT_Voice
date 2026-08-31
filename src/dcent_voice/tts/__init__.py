# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Local text-to-speech: backends, sentence streaming, and cancellable playback.

Kokoro-82M (Apache-2.0) is the default backend. Piper is an optional fallback
for user-supplied, checksum-pinned voices with an explicit license declaration;
XTTS is deliberately excluded (non-commercial license). Nothing imports an inference library or
touches the network at import time; backends are lazy and model assets are
consent-gated (:mod:`dcent_voice.tts.assets`).
"""

from __future__ import annotations

from dcent_voice.tts.assets import install_backend_assets
from dcent_voice.tts.base import (
    AudioChunk,
    TtsBackend,
    TtsCapability,
    TtsError,
    TtsUnavailable,
)
from dcent_voice.tts.factory import build_tts_backend
from dcent_voice.tts.fake import FakeTtsBackend
from dcent_voice.tts.playback import (
    AudioSink,
    CallbackMicGate,
    FakeAudioSink,
    MicGate,
    NullMicGate,
    PlaybackEngine,
    RefCountMicGate,
    SoundDeviceSink,
    TtsPlayer,
)
from dcent_voice.tts.sentence_stream import CodePolicy, SentenceStream

__all__ = [
    "AudioChunk",
    "AudioSink",
    "CallbackMicGate",
    "CodePolicy",
    "FakeAudioSink",
    "FakeTtsBackend",
    "MicGate",
    "NullMicGate",
    "PlaybackEngine",
    "RefCountMicGate",
    "SentenceStream",
    "SoundDeviceSink",
    "TtsBackend",
    "TtsCapability",
    "TtsError",
    "TtsPlayer",
    "TtsUnavailable",
    "build_tts_backend",
    "install_backend_assets",
]
