# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import numpy as np

from dcent_voice.tts import (
    AudioChunk,
    CallbackMicGate,
    FakeAudioSink,
    FakeTtsBackend,
    PlaybackEngine,
    RefCountMicGate,
    SoundDeviceSink,
    TtsPlayer,
)


def _chunk(seconds: float, rate: int = 24000) -> AudioChunk:
    return AudioChunk(samples=np.zeros(int(seconds * rate), dtype=np.float32), sample_rate=rate)


def test_engine_plays_submitted_chunks() -> None:
    sink = FakeAudioSink()
    engine = PlaybackEngine(sink, realtime=False)
    try:
        for _ in range(5):
            engine.submit(_chunk(0.02))
        assert engine.wait_idle(2.0)
        assert len(sink.chunks) == 5
        assert sink.started_rate == 24000
    finally:
        engine.close()


def test_sounddevice_sink_keeps_realtime_pacing_for_queued_output() -> None:
    engine = PlaybackEngine(SoundDeviceSink())
    try:
        assert engine._realtime is True
    finally:
        engine.close()


def test_sounddevice_sink_rejects_stale_write_without_reopening_output(monkeypatch) -> None:
    streams = []

    class Stream:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.started = False
            self.aborted = False
            self.closed = False
            self.writes = []
            streams.append(self)

        def start(self) -> None:
            self.started = True

        def write(self, samples) -> None:
            self.writes.append(samples)

        def abort(self) -> None:
            self.aborted = True

        def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=Stream))
    sink = SoundDeviceSink(device=7)
    chunk = _chunk(0.02)

    assert sink.start(chunk.sample_rate, generation=0)
    assert streams[0].kwargs["device"] == 7
    sink.stop_through(0)

    assert sink.write(chunk, generation=0) is False
    assert len(streams) == 1
    assert streams[0].aborted and streams[0].closed

    assert sink.start(chunk.sample_rate, generation=1)
    assert sink.write(chunk, generation=1)
    assert len(streams) == 2
    assert len(streams[1].writes) == 1
    sink.close()


def test_cancel_stops_within_100ms() -> None:
    sink = FakeAudioSink()
    # realtime pacing: each chunk represents 0.5 s of audio, so without cancel the
    # engine would take seconds. Cancel must stop the device far sooner.
    engine = PlaybackEngine(sink, realtime=True)
    try:
        for _ in range(20):
            engine.submit(_chunk(0.5))
        # Wait until playback has actually started (first chunk written).
        deadline = time.monotonic() + 1.0
        while not sink.chunks and time.monotonic() < deadline:
            time.sleep(0.005)
        assert sink.chunks, "playback never started"

        t0 = time.monotonic()
        engine.cancel()
        stopped = engine.wait_idle(1.0)
        elapsed = time.monotonic() - t0

        assert stopped
        assert sink.stopped
        assert elapsed < 0.1, f"cancel took {elapsed * 1000:.1f} ms"
        # No chunk is written after cancel.
        chunks_after_cancel = sum(1 for t in sink.write_times if t > t0)
        assert chunks_after_cancel == 0
    finally:
        engine.close()


def test_cancel_drops_queued_audio() -> None:
    sink = FakeAudioSink()
    engine = PlaybackEngine(sink, realtime=True)
    try:
        for _ in range(50):
            engine.submit(_chunk(0.5))
        deadline = time.monotonic() + 1.0
        while not sink.chunks and time.monotonic() < deadline:
            time.sleep(0.005)
        engine.cancel()
        engine.wait_idle(1.0)
        # Far fewer than 50 chunks played — the queue was dropped, not drained.
        assert len(sink.chunks) < 50
    finally:
        engine.close()


def test_cancel_reaches_sink_stop_while_a_write_is_blocked() -> None:
    class BlockingSink(FakeAudioSink):
        def __init__(self) -> None:
            super().__init__()
            self.write_started = threading.Event()
            self.release_write = threading.Event()
            self.write_finished = threading.Event()

        def write(self, chunk: AudioChunk, generation: int) -> bool:
            self.write_started.set()
            assert self.release_write.wait(2.0)
            try:
                return super().write(chunk, generation)
            finally:
                self.write_finished.set()

        def stop_through(self, generation: int) -> None:
            super().stop_through(generation)
            self.release_write.set()

    sink = BlockingSink()
    engine = PlaybackEngine(sink, realtime=True)
    cancel_finished = threading.Event()
    try:
        engine.submit(_chunk(0.5))
        assert sink.write_started.wait(1.0)

        started = time.monotonic()

        def cancel() -> None:
            engine.cancel()
            cancel_finished.set()

        thread = threading.Thread(target=cancel)
        thread.start()
        assert cancel_finished.wait(0.1)
        assert (time.monotonic() - started) < 0.1
        assert sink.stopped
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert sink.write_finished.wait(1.0)
        assert sink.chunks == []
    finally:
        sink.release_write.set()
        engine.close()


def test_mic_gate_pauses_and_resumes_around_playback() -> None:
    events: list[str] = []
    gate = CallbackMicGate(
        on_start=lambda: events.append("pause"),
        on_stop=lambda: events.append("resume"),
    )
    sink = FakeAudioSink()
    engine = PlaybackEngine(sink, mic_gate=gate, realtime=False)
    try:
        engine.submit(_chunk(0.02))
        assert engine.wait_idle(2.0)
        # Give the worker a moment to fire the idle (resume) callback.
        deadline = time.monotonic() + 1.0
        while "resume" not in events and time.monotonic() < deadline:
            time.sleep(0.005)
        assert events == ["pause", "resume"]
    finally:
        engine.close()


def test_barge_in_via_cancel_releases_mic_gate() -> None:
    events: list[str] = []
    gate = CallbackMicGate(
        on_start=lambda: events.append("pause"),
        on_stop=lambda: events.append("resume"),
    )
    sink = FakeAudioSink()
    engine = PlaybackEngine(sink, mic_gate=gate, realtime=True)
    try:
        for _ in range(10):
            engine.submit(_chunk(0.5))
        deadline = time.monotonic() + 1.0
        while "pause" not in events and time.monotonic() < deadline:
            time.sleep(0.005)
        engine.cancel()  # simulates a PTT barge-in
        engine.wait_idle(1.0)
        assert events == ["pause", "resume"]
    finally:
        engine.close()


def test_shared_mic_gate_stays_engaged_until_all_players_stop() -> None:
    events: list[str] = []
    gate = RefCountMicGate(
        CallbackMicGate(
            on_start=lambda: events.append("duck"),
            on_stop=lambda: events.append("restore"),
        )
    )
    first = PlaybackEngine(FakeAudioSink(), mic_gate=gate, realtime=True)
    second = PlaybackEngine(FakeAudioSink(), mic_gate=gate, realtime=True)
    try:
        first.submit(_chunk(0.8))
        second.submit(_chunk(0.8))
        deadline = time.monotonic() + 1.0
        while (not first.is_playing or not second.is_playing) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert first.is_playing and second.is_playing
        assert events == ["duck"]

        first.cancel()
        assert second.is_playing
        assert events == ["duck"]

        second.cancel()
        assert events == ["duck", "restore"]
    finally:
        first.close()
        second.close()


def test_cancel_cannot_revive_stale_playback_or_leave_mic_ducked(monkeypatch) -> None:
    events: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    sink = FakeAudioSink()
    engine = PlaybackEngine(
        sink,
        mic_gate=CallbackMicGate(
            on_start=lambda: events.append("duck"),
            on_stop=lambda: events.append("restore"),
        ),
        realtime=True,
    )
    original_begin = engine._begin_playing

    def delayed_begin(sample_rate: int, generation: int) -> bool:
        entered.set()
        assert release.wait(1.0)
        try:
            return original_begin(sample_rate, generation)
        finally:
            finished.set()

    monkeypatch.setattr(engine, "_begin_playing", delayed_begin)
    try:
        engine.submit(_chunk(0.5))
        assert entered.wait(1.0)

        engine.cancel()  # PTT barge-in before the worker can start the chunk.
        release.set()
        assert finished.wait(1.0)

        assert sink.chunks == []
        assert engine.is_playing is False
        assert events in ([], ["duck", "restore"])
    finally:
        release.set()
        engine.close()


def test_close_releases_mic_gate_during_active_playback() -> None:
    events: list[str] = []
    gate = CallbackMicGate(
        on_start=lambda: events.append("duck"),
        on_stop=lambda: events.append("restore"),
    )
    sink = FakeAudioSink()
    engine = PlaybackEngine(sink, mic_gate=gate, realtime=True)
    engine.submit(_chunk(0.5))
    deadline = time.monotonic() + 1.0
    while "duck" not in events and time.monotonic() < deadline:
        time.sleep(0.005)

    engine.close()

    assert events == ["duck", "restore"]


# --- TtsPlayer (backend -> chunker -> playback) ---------------------------------


def test_player_speaks_appended_text() -> None:
    backend = FakeTtsBackend()
    sink = FakeAudioSink()
    engine = PlaybackEngine(sink, realtime=False)
    player = TtsPlayer(backend, engine)
    try:
        player.append("Hello there. General Kenobi.")
        player.flush()
        deadline = time.monotonic() + 2.0
        while sink.total_frames == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert engine.wait_idle(2.0)
        assert sink.total_frames > 0
    finally:
        player.close()
        engine.close()


def test_player_cancel_stops_synthesis_and_playback() -> None:
    backend = FakeTtsBackend()
    sink = FakeAudioSink()
    engine = PlaybackEngine(sink, realtime=True)
    player = TtsPlayer(backend, engine)
    try:
        player.append("A very long sentence that keeps going and going and going.")
        deadline = time.monotonic() + 1.0
        while sink.total_frames == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        t0 = time.monotonic()
        player.cancel()
        assert engine.wait_idle(1.0)
        assert (time.monotonic() - t0) < 0.1
        assert sink.stopped
        # After cancel the player accepts new speech again.
        player.append("New sentence. ")
        player.flush()
        assert engine.wait_idle(2.0)
    finally:
        player.close()
        engine.close()
