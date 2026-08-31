# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from dcent_voice.audio.capture import AudioCapture, RingBuffer, resample_linear
from dcent_voice.audio.levels import AmplitudeMeter


def test_ringbuffer_mark_and_drain() -> None:
    ring = RingBuffer(8)
    ring.write(np.array([1, 2, 3], dtype=np.float32))
    ring.mark()
    ring.write(np.array([4, 5, 6], dtype=np.float32))

    assert ring.drain_from_mark().tolist() == [4, 5, 6]


def test_resample_linear_downsamples_and_preserves_amplitude() -> None:
    audio = np.full(48000, 0.5, dtype=np.float32)  # 1 s of constant level at 48 kHz
    out = resample_linear(audio, 48000, 16000)

    assert out.shape[0] == 16000  # 1 s at 16 kHz
    # A constant signal must keep its amplitude through resampling (this is the
    # bug we were guarding against: attenuation collapsing speech to "silence").
    assert np.isclose(float(np.max(np.abs(out))), 0.5, atol=1e-4)


def test_end_utterance_resamples_native_capture_to_target() -> None:
    # Simulate a device captured at 44.1 kHz feeding a 16 kHz downstream path.
    cap = AudioCapture(samplerate=16000, max_seconds=2.0)
    cap.capture_samplerate = 44100
    cap.ring = RingBuffer.for_seconds(2.0, 44100)
    cap.ring.mark()
    cap.ring.write(np.full(44100, 0.3, dtype=np.float32))  # 1 s of speech-level audio

    out = cap.end_utterance()

    assert abs(out.shape[0] - 16000) <= 2  # resampled to ~16 kHz
    assert float(np.sqrt(np.mean(out**2))) > 0.2  # level preserved, not attenuated


def test_ringbuffer_wraps_and_keeps_recent_samples() -> None:
    ring = RingBuffer(5)
    ring.mark()
    ring.write(np.array([1, 2, 3, 4, 5, 6, 7], dtype=np.float32))

    assert ring.drain_from_mark().tolist() == [3, 4, 5, 6, 7]


def test_ringbuffer_wrap_after_mark() -> None:
    ring = RingBuffer(5)
    ring.write(np.array([1, 2, 3, 4], dtype=np.float32))
    ring.mark()
    ring.write(np.array([5, 6, 7], dtype=np.float32))

    assert ring.drain_from_mark().tolist() == [5, 6, 7]


def test_amplitude_meter_normalizes_to_unit_interval() -> None:
    meter = AmplitudeMeter(smoothing=1.0)

    assert meter.update(np.zeros(160, dtype=np.float32)) == 0.0
    loud = meter.update(np.ones(160, dtype=np.float32))

    assert 0.0 < loud <= 1.0
    meter.reset()
    assert meter.read() == 0.0


def test_capture_input_gain_scales_callback_audio_and_meter() -> None:
    meter = AmplitudeMeter(smoothing=1.0)
    cap = AudioCapture(samplerate=16000, max_seconds=1.0, meter=meter)
    cap.ring.mark()
    cap.set_input_gain(0.25)

    cap._callback(np.ones((4, 1), dtype=np.float32), 4, None, None)

    np.testing.assert_allclose(cap.ring.drain_from_mark(), np.full(4, 0.25))
    assert cap.input_gain == 0.25
    assert meter.read() > 0.0
    cap.set_input_gain(1.0)
    assert cap.status_snapshot()["input_gain"] == 1.0


def test_capture_input_gain_rejects_invalid_values() -> None:
    cap = AudioCapture()

    for value in (-0.1, 1.1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="input gain"):
            cap.set_input_gain(value)


def test_ringbuffer_concurrent_snapshots_are_consistent() -> None:
    capacity = 257
    ring = RingBuffer(capacity)
    ring.mark()
    done = threading.Event()
    errors: list[str] = []
    snapshots_seen: list[int] = []

    def writer() -> None:
        next_value = 0
        for _ in range(400):
            chunk = np.arange(next_value, next_value + 7, dtype=np.float32)
            ring.write(chunk)
            next_value += len(chunk)
        done.set()

    def reader() -> None:
        while not done.is_set():
            snapshot = ring.drain_from_mark()
            snapshots_seen.append(snapshot.size)
            if snapshot.size > 1 and not np.all(np.diff(snapshot) == 1):
                errors.append(f"torn snapshot: {snapshot[:20]!r}")
                return

    writer_thread = threading.Thread(target=writer)
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in readers:
        thread.start()
    writer_thread.start()
    writer_thread.join(timeout=5.0)
    for thread in readers:
        thread.join(timeout=5.0)

    assert done.is_set()
    assert not errors
    assert snapshots_seen
    expected = np.arange(2800 - capacity, 2800, dtype=np.float32)
    np.testing.assert_array_equal(ring.drain_from_mark(), expected)
    assert ring.total_written == 2800
    assert ring.samples_since_mark() == 2800


def test_end_utterance_quiesces_stream_before_final_snapshot() -> None:
    cap = AudioCapture(samplerate=16000, max_seconds=1.0)
    cap.ring.mark()
    cap.ring.write(np.array([1, 2], dtype=np.float32))

    class TailCallbackStream:
        def __init__(self) -> None:
            self.stopped = False
            self.closed = False

        def stop(self) -> None:
            # PortAudio may deliver its final callback while stop() quiesces the
            # stream. Those release-tail frames must be in the final snapshot.
            cap._callback(np.array([[3], [4]], dtype=np.float32), 2, None, None)
            self.stopped = True

        def close(self) -> None:
            self.closed = True

    stream = TailCallbackStream()
    cap._stream = stream

    out = cap.end_utterance()

    assert out.tolist() == [1, 2, 3, 4]
    assert stream.stopped is True
    assert stream.closed is True
    assert cap.status_snapshot()["open"] is False


class _FakeSoundDevice:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.streams: list[_FakeInputStream] = []
        self.active = 0
        self.max_active = 0

    def query_devices(self, device, *, kind):
        return {"default_samplerate": 16000}

    def InputStream(self, **kwargs):  # noqa: N802 - matches sounddevice API
        stream = _FakeInputStream(self, kwargs["callback"])
        with self.lock:
            self.streams.append(stream)
        return stream


class _FakeInputStream:
    def __init__(self, owner: _FakeSoundDevice, callback) -> None:
        self.owner = owner
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self) -> None:
        # Widen the old check-then-create race without making the test slow.
        time.sleep(0.002)
        with self.owner.lock:
            if self.started:
                return
            self.started = True
            self.owner.active += 1
            self.owner.max_active = max(self.owner.max_active, self.owner.active)
        self.callback(np.zeros((16, 1), dtype=np.float32), 16, None, None)

    def stop(self) -> None:
        with self.owner.lock:
            if self.started:
                self.started = False
                self.owner.active -= 1

    def close(self) -> None:
        self.stop()
        self.closed = True


def test_capture_lifecycle_is_serialized_and_idempotent(monkeypatch) -> None:
    fake = _FakeSoundDevice()
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(InputStream=fake.InputStream, query_devices=fake.query_devices),
    )
    cap = AudioCapture(samplerate=16000, max_seconds=1.0)
    barrier = threading.Barrier(7)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            for iteration in range(20):
                operation = (index + iteration) % 3
                if operation == 0:
                    cap.start()
                elif operation == 1:
                    cap.stop()
                else:
                    cap.set_device((index + iteration) % 2)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10.0)
    cap.stop()
    cap.stop()  # idempotent

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert fake.max_active <= 1
    assert fake.active == 0
    assert all(stream.closed for stream in fake.streams)
    assert cap.status_snapshot()["open"] is False


def test_capture_retries_same_device_when_advertised_rate_cannot_open(monkeypatch) -> None:
    attempts: list[tuple[int, int | str | None]] = []

    class Stream:
        def __init__(self, callback) -> None:
            self.callback = callback
            self.started = False
            self.closed = False

        def start(self) -> None:
            self.started = True
            self.callback(np.zeros((16, 1), dtype=np.float32), 16, None, None)

        def stop(self) -> None:
            self.started = False

        def close(self) -> None:
            self.closed = True

    def input_stream(**kwargs):
        rate = int(kwargs["samplerate"])
        attempts.append((rate, kwargs["device"]))
        if rate == 44100:
            raise RuntimeError("PortAudio invalid device")
        assert rate == 48000
        return Stream(kwargs["callback"])

    fake = SimpleNamespace(
        query_devices=lambda _device, *, kind: {"default_samplerate": 44100},
        InputStream=input_stream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    cap = AudioCapture(device=21)

    cap.start()

    assert attempts == [(44100, 21), (48000, 21)]
    assert cap.capture_samplerate == 48000
    assert cap.status_snapshot()["open"] is True
    assert cap.status_snapshot()["callback_ready"] is True
    assert cap.status_snapshot()["last_error"] is None
    accepted = cap._stream
    cap.stop()
    assert accepted is not None
    assert accepted.closed is True


def test_capture_all_rate_failures_are_bounded_and_diagnostic(monkeypatch) -> None:
    attempts: list[int] = []
    streams: list[object] = []

    class FailingStartStream:
        stopped = False
        closed = False

        def start(self) -> None:
            raise OSError("backend refused stream")

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

    def input_stream(**kwargs):
        rate = int(kwargs["samplerate"])
        attempts.append(rate)
        if rate == 48000:
            raise ValueError("constructor rejected rate")
        stream = FailingStartStream()
        streams.append(stream)
        return stream

    fake = SimpleNamespace(
        query_devices=lambda _device, *, kind: {"default_samplerate": 44100},
        InputStream=input_stream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    cap = AudioCapture(samplerate=16000, device="loop-input")

    with pytest.raises(RuntimeError, match="microphone open failed") as raised:
        cap.start()

    assert attempts == [44100, 48000, 16000]
    message = str(raised.value)
    assert "device='loop-input'" in message
    assert "44100Hz=OSError" in message
    assert "48000Hz=ValueError" in message
    assert "16000Hz=OSError" in message
    assert cap.status_snapshot()["open"] is False
    assert cap.status_snapshot()["last_error"] == message
    assert all(stream.stopped and stream.closed for stream in streams)


def test_capture_waits_for_first_nonempty_callback_before_open(monkeypatch) -> None:
    callback_entered = threading.Event()

    class DelayedStream:
        def __init__(self, callback) -> None:
            self.callback = callback
            self.thread: threading.Thread | None = None
            self.closed = False

        def start(self) -> None:
            def deliver() -> None:
                time.sleep(0.02)
                self.callback(np.zeros((8, 1), dtype=np.float32), 8, None, None)
                callback_entered.set()

            self.thread = threading.Thread(target=deliver)
            self.thread.start()

        def stop(self) -> None:
            if self.thread is not None:
                self.thread.join(timeout=1.0)

        def close(self) -> None:
            self.closed = True

    fake = SimpleNamespace(
        query_devices=lambda _device, *, kind: {"default_samplerate": 16000},
        InputStream=lambda **kwargs: DelayedStream(kwargs["callback"]),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    cap = AudioCapture()

    cap.start()

    assert callback_entered.is_set()
    assert cap.ring.total_written == 8
    assert cap.status_snapshot()["open"] is True
    assert cap.status_snapshot()["callback_ready"] is True
    cap.stop()
    assert cap.status_snapshot()["callback_ready"] is False


def test_capture_no_callback_fails_closed_and_closes_every_rate(monkeypatch) -> None:
    streams: list[object] = []

    class SilentStream:
        stopped = False
        closed = False

        def start(self) -> None:
            return

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

    def input_stream(**_kwargs):
        stream = SilentStream()
        streams.append(stream)
        return stream

    monkeypatch.setattr("dcent_voice.audio.capture._CALLBACK_READY_TIMEOUT_S", 0.01)
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            query_devices=lambda _device, *, kind: {"default_samplerate": 16000},
            InputStream=input_stream,
        ),
    )
    cap = AudioCapture(device=7)

    with pytest.raises(RuntimeError, match="no input callback"):
        cap.start()

    assert len(streams) == 3
    assert all(stream.stopped and stream.closed for stream in streams)
    assert cap.status_snapshot()["open"] is False
    assert cap.status_snapshot()["callback_ready"] is False


def test_stale_callback_cannot_write_into_reopened_capture(monkeypatch) -> None:
    streams: list[object] = []

    class Stream:
        def __init__(self, callback) -> None:
            self.callback = callback

        def start(self) -> None:
            self.callback(np.ones((4, 1), dtype=np.float32), 4, None, None)

        def stop(self) -> None:
            return

        def close(self) -> None:
            return

    def input_stream(**kwargs):
        stream = Stream(kwargs["callback"])
        streams.append(stream)
        return stream

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            query_devices=lambda _device, *, kind: {"default_samplerate": 16000},
            InputStream=input_stream,
        ),
    )
    cap = AudioCapture()
    cap.start()
    stale = streams[-1]
    cap.stop()
    cap.start()
    current_ring = cap.ring
    written = current_ring.total_written

    stale.callback(np.full((4, 1), 9.0, dtype=np.float32), 4, None, None)

    assert current_ring.total_written == written
    cap.stop()


def test_cold_begin_keeps_first_ready_callback_but_warm_begin_marks(monkeypatch) -> None:
    streams: list[object] = []

    class Stream:
        def __init__(self, callback) -> None:
            self.callback = callback

        def start(self) -> None:
            self.callback(np.ones((4, 1), dtype=np.float32), 4, None, None)

        def stop(self) -> None:
            return

        def close(self) -> None:
            return

    def input_stream(**kwargs):
        stream = Stream(kwargs["callback"])
        streams.append(stream)
        return stream

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            query_devices=lambda _device, *, kind: {"default_samplerate": 16000},
            InputStream=input_stream,
        ),
    )
    cold = AudioCapture()

    cold.begin_utterance()

    np.testing.assert_array_equal(cold.end_utterance(), np.ones(4, dtype=np.float32))

    warm = AudioCapture()
    warm.start()
    warm.begin_utterance()
    streams[-1].callback(np.full((3, 1), 2.0, dtype=np.float32), 3, None, None)

    np.testing.assert_array_equal(warm.end_utterance(), np.full(3, 2.0, dtype=np.float32))
