# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from types import SimpleNamespace

from scripts import bench_engines


def test_tournament_propagates_explicit_multilingual_policy(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, config, polish=False):
            seen["language"] = config.language
            seen["language_mode"] = config.language_mode
            seen["polish"] = polish

        def load(self):
            pass

        def transcribe(self, _audio, samplerate=16000, *, language=None):
            seen.setdefault("calls", []).append(language)
            return SimpleNamespace(text="Je m'appelle.")

        def unload(self):
            pass

    monkeypatch.setattr(bench_engines, "VoiceEngine", FakeEngine)
    row = bench_engines._run_candidate(
        "parakeet:tdt-0.6b-v3:int8",
        [0.1],
        16000,
        "Je m'appelle.",
        2,
        "fr",
    )

    assert seen["language"] == "fr"
    assert seen["language_mode"] == "multilingual"
    assert seen["calls"] == ["fr", "fr", "fr"]
    assert row["wer"] == 0.0
