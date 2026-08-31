# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Capture microphone audio into a thread-safe ring buffer."""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from dcent_voice.audio.device_select import (
    ResolvedInputDevice,
    failover_candidates,
    is_dead_rms,
    portaudio_default_input,
)
from dcent_voice.audio.levels import AmplitudeMeter
from dcent_voice.config import APP_NAME

logger = logging.getLogger(APP_NAME).getChild("capture")

# PortAudio's ``InputStream.start()`` means the backend accepted the start
# request; on several Windows hosts it returns before the first input callback
# is deliverable.  A hold-to-talk caller must not be told capture is ready until
# audio has actually crossed that boundary.  Normal callbacks arrive in a few
# milliseconds; this bound leaves room for a cold device/driver without making
# a dead endpoint stall startup indefinitely.
_CALLBACK_READY_TIMEOUT_S = 0.5


class RingBuffer:
    """Single-writer ring buffer for mono float32 audio."""

    def __init__(self, capacity_samples: int) -> None:
        if capacity_samples <= 0:
            raise ValueError("capacity_samples must be positive")
        self.capacity_samples = capacity_samples
        self._data = np.zeros(capacity_samples, dtype=np.float32)
        self._write_pos = 0
        self._total_written = 0
        self._mark_total = 0
        # The PortAudio callback writes while streaming ASR snapshots from a
        # worker thread. NumPy copies can release the GIL, so Python's implicit
        # serialization is not sufficient here.
        self._lock = threading.Lock()

    @classmethod
    def for_seconds(cls, seconds: float, samplerate: int = 16000) -> RingBuffer:
        return cls(max(1, int(seconds * samplerate)))

    def write(self, samples: Any) -> None:
        array = np.asarray(samples, dtype=np.float32).reshape(-1)
        count = int(array.size)
        if count == 0:
            return
        with self._lock:
            self._write_unlocked(array, count)

    def _write_unlocked(self, array: np.ndarray, count: int) -> None:
        if count >= self.capacity_samples:
            tail = array[-self.capacity_samples :]
            end_pos = (self._total_written + count) % self.capacity_samples
            if end_pos == 0:
                self._data[:] = tail
            else:
                self._data[:end_pos] = tail[-end_pos:]
                self._data[end_pos:] = tail[:-end_pos]
            self._write_pos = end_pos
            self._total_written += count
            return

        end = self._write_pos + count
        if end <= self.capacity_samples:
            self._data[self._write_pos : end] = array
        else:
            first = self.capacity_samples - self._write_pos
            self._data[self._write_pos :] = array[:first]
            self._data[: end % self.capacity_samples] = array[first:]
        self._write_pos = end % self.capacity_samples
        self._total_written += count

    def mark(self) -> None:
        with self._lock:
            self._mark_total = self._total_written

    def drain_from_mark(self) -> np.ndarray:
        with self._lock:
            return self._drain_from_mark_unlocked()

    def _drain_from_mark_unlocked(self) -> np.ndarray:
        end_total = self._total_written
        available = max(0, end_total - self._mark_total)
        if available == 0:
            return np.zeros(0, dtype=np.float32)

        count = min(available, self.capacity_samples)
        start_total = end_total - count
        start_pos = start_total % self.capacity_samples
        end_pos = end_total % self.capacity_samples

        if count == self.capacity_samples:
            if end_pos == 0:
                return self._data.copy()
            return np.concatenate((self._data[end_pos:], self._data[:end_pos])).astype(np.float32)
        if start_pos < end_pos:
            return self._data[start_pos:end_pos].copy()
        return np.concatenate((self._data[start_pos:], self._data[:end_pos])).astype(np.float32)

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._total_written

    @property
    def mark_total(self) -> int:
        with self._lock:
            return self._mark_total

    def samples_since_mark(self) -> int:
        with self._lock:
            return max(0, self._total_written - self._mark_total)


@dataclass
class AudioCapture:
    """Microphone capture service backed by a ring buffer."""

    samplerate: int = 16000
    channels: int = 1
    # Default matches AudioConfig.max_seconds so auto-stop has headroom.
    max_seconds: float = 90.0
    meter: AmplitudeMeter | None = None
    device: int | str | None = None

    def __post_init__(self) -> None:
        # capture_samplerate is the rate we actually open the device at; it may
        # differ from the downstream `samplerate` (16 kHz) and is resolved when
        # the stream starts. end_utterance() resamples down to `samplerate`.
        self.capture_samplerate = self.samplerate
        self.ring = RingBuffer.for_seconds(self.max_seconds, self.capture_samplerate)
        self.meter = self.meter or AmplitudeMeter()
        self._lifecycle_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._gain_lock = threading.Lock()
        self._stream: Any | None = None
        self._callback_generation = 0
        self._callback_ready: threading.Event | None = None
        self._last_error: str | None = None
        self._status_events = 0
        self._input_gain = 1.0
        self._configured_device = self.device
        self._resolution: ResolvedInputDevice | None = None
        self._failover_tried: set[int | str] = set()
        self._failover_armed_at = 0.0
        self._default_label = "system default microphone"

    def set_input_gain(self, gain: float) -> None:
        """Set the capture gain used by future microphone callbacks.

        TTS half-duplex ducking changes this value while speakers are active.
        The callback snapshots it under a dedicated lock, so playback threads can
        change gain without racing PortAudio's real-time input thread.
        """

        value = float(gain)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("input gain must be a finite number between 0 and 1")
        with self._gain_lock:
            self._input_gain = value

    @property
    def input_gain(self) -> float:
        """Return the gain currently applied to captured microphone samples."""

        with self._gain_lock:
            return self._input_gain

    def set_device(self, device: int | str | None) -> None:
        """Update the configured input device; closes any open stream so the next PTT reopens."""
        with self._lifecycle_lock:
            if device == self._configured_device and device == self.device:
                return
            logger.info("audio input device changed: %r -> %r", self.device, device)
            # Quiesce callbacks from the old device before publishing the new
            # identity or allowing start() to replace the ring.
            self._stop_locked()
            self._configured_device = device
            self.device = device
            self._resolution = None
            self._failover_tried.clear()

    def maybe_failover_dead_default(self) -> ResolvedInputDevice | None:
        """If the shipped empty default is electrically dead, try the next input.

        Opens one alternate at a time during the hold so a virtual loop (Sonar)
        can be armed before speech or fixture playback. Never replaces an
        explicit user-selected device. Never writes config.
        """
        with self._lifecycle_lock:
            if self._configured_device is not None:
                return self._resolution
            raw = self.ring.drain_from_mark()
            current_rms = float(np.sqrt(np.mean(np.square(raw)))) if raw.size else 0.0
            if not is_dead_rms(current_rms):
                return self._resolution
            if (
                self._resolution is not None
                and self._resolution.auto_selected
                and self._failover_armed_at
                and (time.monotonic() - self._failover_armed_at) < 0.8
            ):
                return self._resolution
            was_open = self._stream is not None
            if was_open:
                self._stop_locked()
        try:
            default_index, default_name = portaudio_default_input()
            candidates = failover_candidates(
                default_index=default_index,
                default_name=default_name,
            )
        except Exception:
            logger.exception("live-input candidate list failed")
            candidates = []
            default_name = self._default_label
        if default_name:
            self._default_label = default_name
        with self._lifecycle_lock:
            if self._configured_device is not None:
                if was_open and self._stream is None:
                    with contextlib.suppress(Exception):
                        self._start_locked()
                return self._resolution
            for item in candidates:
                device_id = item["id"]
                if device_id in self._failover_tried:
                    continue
                self._failover_tried.add(device_id)
                opened = False
                for candidate in (device_id, item.get("name")):
                    if candidate is None or candidate == "":
                        continue
                    self.device = candidate
                    try:
                        if was_open:
                            self._start_locked()
                            self.ring.mark()
                            if self.meter is not None:
                                self.meter.reset()
                        opened = True
                        device_id = candidate
                        break
                    except Exception:
                        logger.warning(
                            "dead-default failover could not open %r (%s)",
                            candidate,
                            item.get("name"),
                        )
                        self._stop_locked()
                if not opened:
                    continue
                self._resolution = ResolvedInputDevice(
                    device=device_id,
                    name=str(item.get("name") or device_id),
                    auto_selected=True,
                    default_was_dead=True,
                    reason="auto_live_alternate",
                )
                self._failover_armed_at = time.monotonic()
                logger.info(
                    "default microphone was silent; trying live input %r (%s)",
                    device_id,
                    self._resolution.name,
                )
                return self._resolution
            self.device = None
            self._resolution = ResolvedInputDevice(
                device=None,
                name=self._default_label,
                auto_selected=False,
                default_was_dead=True,
                reason="os_default_dead_no_alternate",
            )
            if was_open and self._stream is None:
                with contextlib.suppress(Exception):
                    self._start_locked()
            return self._resolution

    def resolved_label(self) -> str:
        if self._resolution is not None and self._resolution.name:
            return self._resolution.name
        if self.device is None:
            return "system default microphone"
        return str(self.device)

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - dependency/environment specific
            raise RuntimeError("sounddevice is required for live audio capture.") from exc

        attempts: list[str] = []
        last_exc: Exception | None = None
        for candidate in self._capture_samplerate_candidates(sd):
            stream: Any | None = None
            # The callback may run inside start(), so publish the candidate ring
            # before opening. A failed candidate is discarded before retrying.
            self.capture_samplerate = candidate
            self.ring = RingBuffer.for_seconds(self.max_seconds, candidate)
            ready = threading.Event()
            self._callback_generation += 1
            generation = self._callback_generation
            self._callback_ready = ready
            try:
                stream = sd.InputStream(
                    samplerate=candidate,
                    channels=self.channels,
                    dtype="float32",
                    blocksize=0,
                    device=self.device,
                    callback=self._make_stream_callback(
                        generation=generation,
                        ring=self.ring,
                        ready=ready,
                    ),
                )
                stream.start()
                if not ready.wait(_CALLBACK_READY_TIMEOUT_S):
                    raise TimeoutError(
                        "no input callback within "
                        f"{_CALLBACK_READY_TIMEOUT_S:.1f}s after stream start"
                    )
            except Exception as exc:
                last_exc = exc
                attempts.append(f"{candidate}Hz={type(exc).__name__}: {exc}")
                # Quiesce a stream that partially started before raising so its
                # callback cannot outlive this failed lifecycle transition.
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.stop()
                    with contextlib.suppress(Exception):
                        stream.close()
                # A non-conforming backend can still invoke a callback after
                # close.  Fence it before publishing another candidate ring.
                self._callback_generation += 1
                self._callback_ready = None
                continue

            self._stream = stream
            self._last_error = None
            logger.debug(
                "microphone stream open device=%r rate=%s",
                self.device,
                self.capture_samplerate,
            )
            return

        detail = "; ".join(attempts) or "no valid sample-rate candidates"
        self._last_error = f"microphone open failed device={self.device!r}: {detail}"
        self._callback_ready = None
        logger.error(self._last_error)
        raise RuntimeError(self._last_error) from last_exc

    def _resolve_capture_samplerate(self, sd: Any) -> int:
        # Forcing a non-native low rate (e.g. 16 kHz) on some Windows MME devices
        # yields badly attenuated, near-silent audio. Capture at the device's
        # native rate instead and resample on drain.
        try:
            info = sd.query_devices(self.device, kind="input")
            native = int(round(float(info.get("default_samplerate") or 0)))
        except Exception:
            native = 0
        return native if native >= 8000 else self.samplerate

    def _capture_samplerate_candidates(self, sd: Any) -> tuple[int, ...]:
        """Return bounded rates for one device, preferring its advertised native rate.

        Some Windows WDM-KS endpoints advertise 44.1 kHz through PortAudio but
        accept only 48 kHz. Retrying rates on the *same configured device* makes
        that mismatch recoverable without silently falling back to another mic.
        """

        preferred = self._resolve_capture_samplerate(sd)
        candidates: list[int] = []
        for rate in (preferred, 48000, 44100, self.samplerate):
            value = int(rate)
            if value >= 8000 and value not in candidates:
                candidates.append(value)
        return tuple(candidates)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        stream = self._stream
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
        self._stream = None
        # Invalidate callbacks only after stop/close so a synchronous final
        # release-tail callback remains part of the utterance, while any truly
        # post-close straggler cannot write into a later capture generation.
        self._callback_generation += 1
        self._callback_ready = None

    def begin_utterance(self) -> None:
        with self._lifecycle_lock:
            if self._configured_device is None:
                self.device = None
                self._resolution = None
                self._failover_tried.clear()
                self._failover_armed_at = 0.0
            was_open = self._stream is not None
            self._start_locked()
            # A freshly opened ring is already marked at sample zero. Keep the
            # readiness callback: it arrived after the user's press and may
            # contain the leading phoneme. A pre-opened warm stream instead
            # needs a new mark to discard audio from before this utterance.
            if was_open:
                self.ring.mark()
            if self.meter is not None:
                self.meter.reset()

    def elapsed_s(self) -> float:
        """Seconds captured since the last begin_utterance mark (capture rate)."""
        with self._lifecycle_lock:
            rate = float(self.capture_samplerate or self.samplerate)
            samples = self.ring.samples_since_mark()
        if rate <= 0:
            return 0.0
        return samples / rate

    def peek_utterance(self) -> np.ndarray:
        """Snapshot the audio captured so far without stopping the stream.

        Streaming re-transcribes the growing window while the key is held, so it
        needs to read the accumulated audio repeatedly (unlike end_utterance,
        which finalizes and closes the device).
        """
        with self._lifecycle_lock:
            raw = self.ring.drain_from_mark()
            capture_samplerate = self.capture_samplerate
            samplerate = self.samplerate
        if capture_samplerate != samplerate:
            raw = resample_linear(raw, capture_samplerate, samplerate)
        return raw

    def end_utterance(self) -> np.ndarray:
        with self._lifecycle_lock:
            # Stop first so PortAudio's final callback is complete before the
            # snapshot. Draining first could omit release-tail frames and race a
            # NumPy write in the callback.
            self._stop_locked()
            raw = self.ring.drain_from_mark()
            capture_samplerate = self.capture_samplerate
            samplerate = self.samplerate
            if self.meter is not None:
                self.meter.reset()
        if capture_samplerate != samplerate:
            raw = resample_linear(raw, capture_samplerate, samplerate)
        return raw

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """Process a direct callback (kept for deterministic tests and adapters)."""

        self._process_callback(indata, frames, time_info, status, ring=self.ring)

    def _make_stream_callback(
        self,
        *,
        generation: int,
        ring: RingBuffer,
        ready: threading.Event,
    ) -> Any:
        """Bind one PortAudio callback to its ring and lifecycle generation."""

        def _stream_callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if generation != self._callback_generation:
                return
            if self._process_callback(indata, frames, time_info, status, ring=ring):
                ready.set()

        return _stream_callback

    def _process_callback(
        self,
        indata: Any,
        frames: int,
        time_info: Any,
        status: Any,
        *,
        ring: RingBuffer,
    ) -> bool:
        """Write one input block and report whether it proves capture readiness."""

        del frames, time_info
        if status:
            with self._status_lock:
                self._status_events += 1
                status_events = self._status_events
            # PortAudio flags (overflow/underflow/device disconnect). Rate-limit
            # so a flapping device doesn't flood the log.
            if status_events <= 5 or status_events % 50 == 0:
                logger.warning("PortAudio input status=%s count=%s", status, status_events)
        if self.channels == 1:
            mono = indata[:, 0] if getattr(indata, "ndim", 1) == 2 else indata
        else:
            mono = np.asarray(indata, dtype=np.float32).mean(axis=1)
        if np.asarray(mono).size == 0:
            return False
        gain = self.input_gain
        if gain != 1.0:
            # Do not mutate PortAudio's buffer in place; it may be reused by the
            # backend after this callback returns.
            mono = np.asarray(mono, dtype=np.float32) * np.float32(gain)
        ring.write(mono)
        if self.meter is not None:
            self.meter.update(mono)
        return True

    def status_snapshot(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            device = self.device
            is_open = self._stream is not None
            last_error = self._last_error
            capture_samplerate = self.capture_samplerate
            callback_ready = bool(self._callback_ready and self._callback_ready.is_set())
            configured = self._configured_device
            resolution = self._resolution
        with self._status_lock:
            status_events = self._status_events
        return {
            "device": device,
            "open": is_open,
            "last_error": last_error,
            "status_events": status_events,
            "capture_samplerate": capture_samplerate,
            "callback_ready": callback_ready,
            "input_gain": self.input_gain,
            "configured_device": configured,
            "resolved_name": resolution.name if resolution is not None else None,
            "auto_selected": bool(resolution.auto_selected) if resolution is not None else False,
            "default_was_dead": (
                bool(resolution.default_was_dead) if resolution is not None else False
            ),
        }


def resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if src_rate == dst_rate or audio.size == 0 or src_rate <= 0 or dst_rate <= 0:
        return audio.astype(np.float32)
    duration_s = audio.size / float(src_rate)
    dst_len = max(1, int(round(duration_s * dst_rate)))
    src_x = np.linspace(0.0, duration_s, num=audio.size, endpoint=False)
    dst_x = np.linspace(0.0, duration_s, num=dst_len, endpoint=False)
    return np.interp(dst_x, src_x, audio).astype(np.float32)
