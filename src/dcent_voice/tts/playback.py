# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Playback of synthesized audio with a hard <100 ms cancel and half-duplex mic.

Two collaborators:

- :class:`PlaybackEngine` owns an :class:`AudioSink` and a worker thread that
  drains a chunk queue. ``cancel()`` stops the device and clears the queue
  immediately — it does not wait for the worker — so audible output stops well
  within the barge-in budget regardless of what the worker is doing.
- :class:`TtsPlayer` glues a :class:`~dcent_voice.tts.base.TtsBackend` and a
  :class:`~dcent_voice.tts.sentence_stream.SentenceStream` to a
  :class:`PlaybackEngine`: incremental text in, streamed sentence synthesis out,
  cancellable at any point. A generation counter makes ``cancel`` abandon
  in-flight synthesis without racing a subsequent ``append``.

Half-duplex barge-in: while TTS plays, a :class:`MicGate` pauses or ducks capture
(config ``[tts].mic_policy``) so the microphone does not hear the speakers; a
PTT/wake/VAD interrupt cancels playback and releases the gate.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from sys import maxsize
from typing import Protocol, runtime_checkable

from dcent_voice.tts.base import AudioChunk, TtsBackend
from dcent_voice.tts.sentence_stream import CodePolicy, SentenceStream


@runtime_checkable
class AudioSink(Protocol):
    """A generation-aware playback device with abortable output admission."""

    def start(self, sample_rate: int, generation: int) -> bool: ...
    def write(self, chunk: AudioChunk, generation: int) -> bool: ...
    def stop_through(self, generation: int) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class MicGate(Protocol):
    """Half-duplex coupling between playback and capture."""

    def on_tts_start(self) -> None: ...
    def on_tts_stop(self) -> None: ...


class NullMicGate:
    """No-op gate (mic and speaker are already isolated, e.g. headphones)."""

    def on_tts_start(self) -> None:  # pragma: no cover - trivial
        pass

    def on_tts_stop(self) -> None:  # pragma: no cover - trivial
        pass


class CallbackMicGate:
    """Half-duplex gate wired to arbitrary start/stop callbacks.

    ``[tts].mic_policy = "pause"`` supplies a pause/resume pair (mic capture is
    suspended while TTS speaks); ``"duck"`` supplies duck/unduck (capture stays
    open at reduced gain). The concrete callbacks are provided by the app when it
    owns an ``AudioCapture``; the gate keeps the policy out of the playback core.
    """

    def __init__(
        self,
        on_start: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self._on_start = on_start
        self._on_stop = on_stop

    def on_tts_start(self) -> None:
        if self._on_start is not None:
            self._on_start()

    def on_tts_stop(self) -> None:
        if self._on_stop is not None:
            self._on_stop()


class RefCountMicGate:
    """Share one mic policy across independent playback engines safely.

    DVAP creates one player per connection, but all players couple to the same
    microphone. The wrapped gate activates on the first playback start and is
    released only after the final active player stops, so one session cannot
    restore capture while another one is still speaking.
    """

    def __init__(self, gate: MicGate) -> None:
        self._gate = gate
        self._lock = threading.RLock()
        self._active = 0

    def on_tts_start(self) -> None:
        with self._lock:
            if self._active == 0:
                self._gate.on_tts_start()
            self._active += 1

    def on_tts_stop(self) -> None:
        with self._lock:
            if self._active == 0:
                return
            self._active -= 1
            if self._active == 0:
                self._gate.on_tts_stop()


class FakeAudioSink:
    """In-memory, generation-aware sink for deterministic playback tests."""

    def __init__(self) -> None:
        self.chunks: list[AudioChunk] = []
        self.write_times: list[float] = []
        self.started_rate: int | None = None
        self.stopped = False
        self.stop_time: float | None = None
        self._lock = threading.Lock()
        self._active_generation: int | None = None
        self._stopped_through = -1

    def start(self, sample_rate: int, generation: int) -> bool:
        with self._lock:
            if generation <= self._stopped_through:
                return False
            self.started_rate = sample_rate
            self.stopped = False
            self._active_generation = generation
            return True

    def write(self, chunk: AudioChunk, generation: int) -> bool:
        with self._lock:
            if generation <= self._stopped_through or self._active_generation != generation:
                return False
            self.chunks.append(chunk)
            self.write_times.append(time.monotonic())
            return True

    def stop_through(self, generation: int) -> None:
        with self._lock:
            self._stopped_through = max(self._stopped_through, generation)
            if (
                self._active_generation is not None
                and self._active_generation <= self._stopped_through
            ):
                self._active_generation = None
            self.stopped = True
            self.stop_time = time.monotonic()

    def stop(self) -> None:
        """Abort the currently active generation without blocking future playback."""
        with self._lock:
            generation = self._active_generation
        self.stop_through(generation if generation is not None else -1)

    def close(self) -> None:  # pragma: no cover - trivial
        self.stop_through(maxsize)

    @property
    def total_frames(self) -> int:
        with self._lock:
            return sum(chunk.frames for chunk in self.chunks)


class SoundDeviceSink:
    """Real speaker output via sounddevice. Never used in CI (no device)."""

    # OutputStream.write may return after queueing short chunks in the device
    # buffer, before speakers have consumed them. PlaybackEngine therefore
    # retains realtime pacing so MicGate remains closed through audible output.

    def __init__(self, *, device: int | str | None = None) -> None:
        self.device = device
        self._stream = None
        self._rate: int | None = None
        self._stream_generation: int | None = None
        self._stopped_through = -1
        self._lock = threading.Lock()

    def start(self, sample_rate: int, generation: int) -> bool:  # pragma: no cover - hardware
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice is required for TTS playback.") from exc

        with self._lock:
            if generation <= self._stopped_through:
                return False
            if self._stream is not None and self._rate == sample_rate:
                return self._stream_generation == generation
            previous = self._detach_locked()
        self._abort_stream(previous)

        stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
        )
        stream.start()

        with self._lock:
            if generation <= self._stopped_through:
                stale = True
            else:
                stale = False
                replaced = self._detach_locked()
                self._stream = stream
                self._rate = sample_rate
                self._stream_generation = generation
        if stale:
            self._abort_stream(stream)
            return False
        self._abort_stream(replaced)
        return True

    def write(self, chunk: AudioChunk, generation: int) -> bool:  # pragma: no cover - hardware
        with self._lock:
            if (
                generation <= self._stopped_through
                or self._stream is None
                or self._stream_generation != generation
                or self._rate != chunk.sample_rate
            ):
                return False
            stream = self._stream
        try:
            stream.write(chunk.samples.reshape(-1, 1))
        except Exception:
            with self._lock:
                if generation <= self._stopped_through or self._stream is not stream:
                    return False
            raise
        with self._lock:
            return generation > self._stopped_through and self._stream is stream

    def stop_through(self, generation: int) -> None:  # pragma: no cover - hardware
        with self._lock:
            self._stopped_through = max(self._stopped_through, generation)
            if (
                self._stream_generation is not None
                and self._stream_generation <= self._stopped_through
            ):
                stream = self._detach_locked()
            else:
                stream = None
        self._abort_stream(stream)

    def stop(self) -> None:  # pragma: no cover - hardware
        """Abort the currently active generation without blocking future playback."""
        with self._lock:
            generation = self._stream_generation
        self.stop_through(generation if generation is not None else -1)

    def close(self) -> None:  # pragma: no cover - hardware
        self.stop_through(maxsize)

    def _detach_locked(self):  # pragma: no cover - hardware
        stream = self._stream
        self._stream = None
        self._rate = None
        self._stream_generation = None
        return stream

    @staticmethod
    def _abort_stream(stream) -> None:  # pragma: no cover - hardware
        if stream is None:
            return
        try:
            stream.abort()
        finally:
            stream.close()


class PlaybackEngine:
    """Threaded chunk player with an immediate, non-blocking cancel."""

    def __init__(
        self,
        sink: AudioSink,
        *,
        mic_gate: MicGate | None = None,
        realtime: bool | None = None,
    ) -> None:
        self._sink = sink
        self._gate = mic_gate or NullMicGate()
        self._realtime = True if realtime is None else realtime
        self._queue: deque[tuple[int, AudioChunk]] = deque()
        self._lock = threading.Lock()
        # Gate callbacks may call into capture while cancellation is happening.
        # Keep their state transitions serialized so every start is paired with
        # exactly one stop, even if a PTT barge-in races worker startup.
        self._gate_lock = threading.RLock()
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self._closed = threading.Event()
        self._playing = False
        self._gate_active = False
        self._generation = 0
        self._idle = threading.Event()
        self._idle.set()
        self.first_chunk_monotonic: float | None = None
        self._worker = threading.Thread(target=self._run, name="TtsPlayback", daemon=True)
        self._worker.start()

    def submit(self, chunk: AudioChunk) -> None:
        """Queue a chunk for playback (no-op if a cancel is pending)."""
        with self._lock:
            if self._cancel.is_set():
                return
            self._queue.append((self._generation, chunk))
            self._idle.clear()
        self._wake.set()

    def cancel(self) -> None:
        """Stop playback now: abort the device and drop queued audio."""
        self._cancel.set()
        with self._lock:
            # A TtsPlayer resets the cancellation event immediately so it can
            # accept a new reply. Stamp queued/in-flight work with a generation
            # as well, otherwise an old worker iteration could start after that
            # reset and revive playback that this cancel was meant to end.
            cancelled_generation = self._generation
            self._generation += 1
            self._queue.clear()
        # Stop the device from the caller thread so audible output ceases without
        # waiting for the worker to notice.
        self._sink.stop_through(cancelled_generation)
        self._set_idle()
        self._wake.set()

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def reset(self) -> None:
        """Clear a prior cancel so the engine accepts new audio again."""
        with self._lock:
            self._cancel.clear()
            self.first_chunk_monotonic = None

    def close(self) -> None:
        # Release any half-duplex gate before the worker exits. Without this,
        # closing during active playback could leave a ducked microphone gain in
        # place until the whole app restarts.
        self.cancel()
        self._closed.set()
        self._wake.set()
        self._worker.join(timeout=2.0)
        self._sink.close()

    def _run(self) -> None:
        while not self._closed.is_set():
            item = self._next_chunk()
            if item is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            generation, chunk = item
            if not self._begin_playing(chunk.sample_rate, generation):
                self._set_idle()
                continue
            if not self._write_chunk(chunk, generation):
                self._set_idle()
                continue
            if self.first_chunk_monotonic is None:
                self.first_chunk_monotonic = time.monotonic()
            if self._realtime and not self._pace_chunk(chunk.duration_s, generation):
                self._set_idle()
                continue
            self._maybe_idle()

    def _next_chunk(self) -> tuple[int, AudioChunk] | None:
        with self._lock:
            if self._queue:
                return self._queue.popleft()
            return None

    def _begin_playing(self, sample_rate: int, generation: int) -> bool:
        """Start a generation only while it remains current and uncancelled."""
        with self._lock:
            if self._cancel.is_set() or generation != self._generation:
                return False
            self._playing = True
            self._idle.clear()
        if not self._sink.start(sample_rate, generation):
            self._set_idle()
            return False
        if not self._activate_gate(generation):
            # cancel() may have stopped a previous device before this worker
            # reached start(); stop again so a stale worker cannot reopen it.
            self._sink.stop_through(generation)
            self._set_idle()
            return False
        return self._can_play(generation)

    def _write_chunk(self, chunk: AudioChunk, generation: int) -> bool:
        """Write only current audio; cancellation wins before stale output starts."""
        with self._lock:
            if self._cancel.is_set() or generation != self._generation or not self._playing:
                return False
        # A device write can block. Never hold the playback-state lock across it:
        # cancel() must reach sink.stop_through() within the barge-in budget even
        # when a driver is slow or wedged. Sink admission is generation-aware, so
        # an invalidated worker cannot reopen output after cancellation.
        try:
            wrote = self._sink.write(chunk, generation)
        except Exception:
            if not self._can_play(generation):
                return False
            raise
        return wrote and self._can_play(generation)

    def _can_play(self, generation: int) -> bool:
        with self._lock:
            return not self._cancel.is_set() and generation == self._generation and self._playing

    def _pace_chunk(self, duration_s: float, generation: int) -> bool:
        """Keep the gate engaged through audible output, interrupting stale work promptly."""
        deadline = time.monotonic() + duration_s
        while True:
            if not self._can_play(generation):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            # Poll the generation rather than waiting solely on _cancel: a
            # TtsPlayer resets that event immediately after cancellation to
            # accept new text, while the old generation must still exit now.
            self._cancel.wait(timeout=min(remaining, 0.01))

    def _activate_gate(self, generation: int) -> bool:
        """Engage the mic gate once, in a cancellation-safe callback order."""
        with self._gate_lock:
            with self._lock:
                if self._cancel.is_set() or generation != self._generation or not self._playing:
                    return False
                if self._gate_active:
                    return True
                self._gate_active = True
            self._gate.on_tts_start()
            return True

    def _maybe_idle(self) -> None:
        with self._lock:
            if self._queue:
                return
        self._set_idle()

    def _set_idle(self) -> None:
        with self._lock:
            self._playing = False
            self._idle.set()
        self._release_gate()

    def _release_gate(self) -> None:
        """Release a previously engaged mic gate exactly once."""
        with self._gate_lock:
            with self._lock:
                was_active = self._gate_active
                self._gate_active = False
            if was_active:
                self._gate.on_tts_stop()


# Sentinels for the TtsPlayer text queue.
_FLUSH = object()
_STOP = object()


class TtsPlayer:
    """Incremental text → streamed sentence synthesis → cancellable playback."""

    def __init__(
        self,
        backend: TtsBackend,
        engine: PlaybackEngine,
        *,
        code_policy: CodePolicy = CodePolicy.SKIP,
    ) -> None:
        self._backend = backend
        self._engine = engine
        self._stream = SentenceStream(code_policy=code_policy)
        self._queue: deque[object] = deque()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._generation = 0
        self._worker = threading.Thread(target=self._run, name="TtsSynth", daemon=True)
        self._worker.start()

    def append(self, text: str) -> None:
        """Queue incremental text to be spoken."""
        with self._lock:
            self._queue.append(text)
        self._wake.set()

    def flush(self) -> None:
        """Speak any trailing partial sentence (end of utterance)."""
        with self._lock:
            self._queue.append(_FLUSH)
        self._wake.set()

    def cancel(self) -> None:
        """Abandon in-flight and queued synthesis and stop playback immediately."""
        with self._lock:
            self._generation += 1
            self._queue.clear()
        self._backend.cancel()
        self._engine.cancel()
        self._stream = SentenceStream(code_policy=self._stream.code_policy)
        self._engine.reset()

    @property
    def is_playing(self) -> bool:
        return self._engine.is_playing

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._engine.wait_idle(timeout)

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        self._worker.join(timeout=2.0)

    def _run(self) -> None:
        while not self._closed.is_set():
            item = self._next_item()
            if item is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            generation = self._generation
            sentences = self._stream.flush() if item is _FLUSH else self._stream.push(item)  # type: ignore[arg-type]
            for sentence in sentences:
                if not self._speak(sentence, generation):
                    break

    def _next_item(self) -> object | None:
        with self._lock:
            if self._queue:
                return self._queue.popleft()
            return None

    def _speak(self, sentence: str, generation: int) -> bool:
        for chunk in self._backend.synthesize(sentence):
            if generation != self._generation or self._closed.is_set():
                return False
            self._engine.submit(chunk)
        return generation == self._generation
