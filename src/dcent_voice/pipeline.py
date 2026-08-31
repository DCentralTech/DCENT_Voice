# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Coordinate capture, transcription, cleanup, and injection."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import platform
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice.asr.base import ASRProvider
from dcent_voice.asr.model_registry import ModelUnavailableError
from dcent_voice.audio.capture import AudioCapture
from dcent_voice.commands.actions import CommandExecutor
from dcent_voice.commands.ade_dispatch import ADEDispatcher
from dcent_voice.commands.router import CommandRouter
from dcent_voice.config import APP_NAME, SnippetEntry, VocabEntry, starred_first
from dcent_voice.dictation.postprocess import (
    VocabLike,
    apply_snippets,
    apply_spoken_tokens,
    compose_dictation,
    extract_last_correction,
    extract_spoken_corrections,
    is_undo_last_command,
    peel_spoken_cleanup,
    peel_spoken_press_enter,
)
from dcent_voice.dictation.style import peel_spoken_style, resolve_style
from dcent_voice.events import (
    AppEvent,
    AppMode,
    AsrReadyChanged,
    EventBus,
    HotkeyPressed,
    HotkeyReleased,
    ShutdownRequested,
    StateChanged,
    TranscriptReady,
    WakeWordDetected,
)
from dcent_voice.inject.base import Injector
from dcent_voice.state import AppState, InvalidTransition, ModeStateMachine
from dcent_voice.util import paths
from dcent_voice.util.timing import StageTimer

logger = logging.getLogger(APP_NAME).getChild("pipeline")


class _PipelineCancelled(RuntimeError):
    """Internal control flow for a terminally cancelled utterance."""


def _asr_is_loaded(asr: Any) -> bool:
    checker = getattr(asr, "is_loaded", None)
    if callable(checker):
        return bool(checker())
    return True


# Voice content must not land in the rotating file log for a privacy-first
# product. The per-utterance line logs metadata only unless the user explicitly
# opts in for debugging via DCENT_VOICE_LOG_TRANSCRIPTS=1.
_LOG_TRANSCRIPTS = os.environ.get("DCENT_VOICE_LOG_TRANSCRIPTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@dataclass
class PipelineConfig:
    samplerate: int = 16000
    min_utterance_s: float = 0.30
    # Slightly above pure digital silence so fan noise / bias does not always
    # reach Whisper (which then hallucinates). Real speech is typically >>0.01.
    silence_rms_threshold: float = 0.005
    cleanup_enabled: bool = False
    dictionary: tuple[VocabEntry, ...] = ()
    snippets: tuple[SnippetEntry, ...] = ()
    # Offline post-ASR transforms (no network / no LLM). On by default so
    # dictation is readable without requiring optional cleanup.
    local_polish: bool = True
    spoken_edits: bool = True
    developer_terms: bool = True
    cleanup_level: str = "medium"
    focus_guard_enabled: bool = True
    # Streaming cadence. First peek is earlier than the steady interval so the
    # overlay can show a first partial without waiting a full second. Injected
    # words still go through IncrementalCommitter (not every ASR flicker).
    stream_interval_s: float = 0.45
    stream_first_peek_s: float = 0.32
    stream_min_audio_s: float = 0.32
    stream_agreement_passes: int = 3
    stream_first_agreement_passes: int = 2
    # Soft max hold: synthesize release after this many seconds of recording.
    max_utterance_s: float = 60.0
    # After wake-word start, end the utterance once this much trailing silence
    # is observed (seconds). 0 disables silence end (max_utterance_s only).
    wake_end_silence_s: float = 1.15
    # Optional local personalization store. Never holds audio.
    personalization: Any = None
    personalization_prose_context: bool = False
    style_default: str = "plain"
    style_per_app: dict[str, str] = field(default_factory=dict)
    language_mode: str = "english"
    language: str = "en"

    def __post_init__(self) -> None:
        if type(self.personalization_prose_context) is not bool:
            raise TypeError("personalization_prose_context must be a boolean")


@dataclass(frozen=True)
class HoldReleaseScore:
    """One hold-release inject result on real speech (not FakeASR)."""

    id: str
    reference: str
    hypothesis: str
    raw: str
    injected: bool
    discarded: bool
    reason: str
    wer: float
    cer: float
    timings: dict[str, float]
    rms: float
    duration_s: float
    kind: str = "hold_release"


def stream_pass_wait_s(cfg: PipelineConfig, *, first: bool) -> float:
    """Seconds to wait before the next streaming ASR peek.

    Tests that shrink ``stream_interval_s`` also shrink the first wait so they
    stay fast. Production first peek is ``min(first, interval)`` (0.32 s at
    shipped defaults) instead of a full 1.0 s of dead air.
    """
    first_s = max(0.0, float(cfg.stream_first_peek_s))
    interval_s = max(0.0, float(cfg.stream_interval_s))
    return min(first_s, interval_s) if first else interval_s


class IncrementalCommitter:
    """LocalAgreement-style word committer for streaming dictation.

    Each streaming pass re-transcribes the growing audio window; a word is only
    "committed" (and therefore injected) once consecutive transcripts agree on
    it. Growing prefixes still need ``agreement_passes`` (default 3). The first
    emit may happen after ``first_agreement_passes`` identical transcripts so a
    short stable utterance is not held hostage by the growing-prefix rule.
    update() returns just the newly committed words.
    """

    def __init__(
        self,
        *,
        agreement_passes: int = 3,
        first_agreement_passes: int = 2,
    ) -> None:
        # Default 3 (was 2): fewer mid-stream mis-hears flash into the document
        # before finalize can retract them. Finalize still flushes the tail.
        self.agreement_passes = max(2, int(agreement_passes))
        self.first_agreement_passes = max(
            1, min(int(first_agreement_passes), self.agreement_passes)
        )
        self._history: list[list[str]] = []
        self._committed = 0

    def update(self, transcript: str) -> str:
        words = transcript.split()
        self._history.append(words)
        if len(self._history) > self.agreement_passes:
            self._history = self._history[-self.agreement_passes :]
        if len(self._history) >= self.agreement_passes:
            # Full LocalAgreement wins over the identical-repeat shortcut so a
            # later stable window cannot skip the growing-prefix rule.
            stable = 0
            for column in zip(*self._history, strict=False):
                if len(set(column)) != 1:
                    break
                stable += 1
            if stable > self._committed:
                delta = self._history[-1][self._committed : stable]
                self._committed = stable
                return " ".join(delta)
            return ""
        if (
            self._committed == 0
            and self.first_agreement_passes < self.agreement_passes
            and len(self._history) >= self.first_agreement_passes
        ):
            recent = self._history[-self.first_agreement_passes :]
            if recent[0] and all(item == recent[0] for item in recent):
                self._committed = len(recent[0])
                return " ".join(recent[0])
        return ""

    def finalize(self, transcript: str) -> str:
        words = transcript.split()
        if len(words) > self._committed:
            delta = words[self._committed :]
            self._committed = len(words)
            return " ".join(delta)
        return ""


class PipelineWorker:
    """Background worker that runs the dictation pipeline."""

    def __init__(
        self,
        *,
        bus: EventBus,
        capture: AudioCapture,
        asr: ASRProvider,
        injector: Injector,
        config: PipelineConfig | None = None,
        cleanup: Any | None = None,
        overlay: Any | None = None,
        command_router: CommandRouter | None = None,
        command_executor: CommandExecutor | None = None,
        ade_dispatcher: ADEDispatcher | None = None,
        selection_getter: Any | None = None,
        notify: Any | None = None,
        recovery_store: Any | None = None,
    ) -> None:
        self.bus = bus
        self.capture = capture
        self.asr = asr
        self.injector = injector
        self.config = config or PipelineConfig()
        self.cleanup = cleanup
        self.overlay = overlay
        self.command_router = command_router
        self.command_executor = command_executor
        self.ade_dispatcher = ade_dispatcher or ADEDispatcher()
        self.selection_getter = selection_getter
        # Optional callable(title, body) for tray toasts — rate-limited inside.
        self.notify = notify
        self.recovery_store = recovery_store
        self.state = ModeStateMachine()
        self._events: queue.SimpleQueue[AppEvent | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="PipelineWorker", daemon=True)
        self._stop = threading.Event()
        # Serialize terminal cancellation with externally visible effects. If
        # an injection is already running, stop() waits for it to finish; once
        # stop() returns, every later effect observes the cancelled generation.
        self._terminal_lock = threading.RLock()
        self._cancel_generation = 0
        self._active_generation = 0
        self._unsubscribe = self.bus.subscribe(self._on_event)
        self._foreground_hwnd: int | None = None
        self._focus_target_at_press: object | None = None
        self._foreground_process = ""
        self._foreground_title = ""
        self._selection_at_press = ""
        self._runtime_lock = threading.RLock()
        self._stream_stop: threading.Event | None = None
        self._stream_thread: threading.Thread | None = None
        self._stream_committer: IncrementalCommitter | None = None
        self._inject_gate = threading.Lock()
        self._stream_injected = False
        self._stream_injected_text = ""
        self._stream_stashed = False
        self._last_injected = ""
        self._last_notify_at = 0.0
        self._last_notify_key = ""
        self._auto_stop_timer: threading.Timer | None = None
        self._auto_stopped = False
        self._wake_silence_stop: threading.Event | None = None
        self._wake_silence_thread: threading.Thread | None = None
        self._failover_stop: threading.Event | None = None
        self._failover_thread: threading.Thread | None = None
        self._no_audio_message = ""

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def status_snapshot(self) -> dict[str, Any]:
        alive = self._thread.is_alive() and not self._stop.is_set()
        return {
            "ok": alive,
            "alive": alive,
            "state": self.state.state.value,
            "critical": True,
        }

    def stop(self, timeout: float = 5.0) -> None:
        with self._terminal_lock:
            self._cancel_generation += 1
            self._stop.set()
            self.state.cancel()
        self._cancel_auto_stop()
        self._cancel_dead_default_failover()
        if self._stream_stop is not None:
            self._stream_stop.set()
        self._events.put(None)
        self._thread.join(timeout)
        # A live streaming pass holds the ASR model; join it so shutdown does
        # not free resources out from under an in-flight decode (RT-ASR-4).
        stream_thread = self._stream_thread
        if stream_thread is not None and stream_thread.is_alive():
            stream_thread.join(timeout=3.0)
        self.capture.stop()
        self._unsubscribe()
        with self._terminal_lock:
            self._foreground_hwnd = None
            self._focus_target_at_press = None
            self._foreground_process = ""
            self._foreground_title = ""
            self._selection_at_press = ""
            self._auto_stopped = False

    def _generation_snapshot(self) -> int:
        with self._terminal_lock:
            return self._cancel_generation

    def _cancelled(self, generation: int) -> bool:
        with self._terminal_lock:
            return self._stop.is_set() or generation != self._cancel_generation

    def _checkpoint(self, generation: int) -> None:
        with self._terminal_lock:
            if self._stop.is_set() or generation != self._cancel_generation:
                raise _PipelineCancelled

    def _publish_event(self, event: AppEvent, *, generation: int | None = None) -> bool:
        """Publish only while this worker/generation remains externally active."""

        with self._terminal_lock:
            expected = self._active_generation if generation is None else generation
            if self._stop.is_set() or expected != self._cancel_generation:
                return False
            self.bus.publish(event)
            return True

    def update_runtime(
        self,
        *,
        asr: ASRProvider | None = None,
        injector: Injector | None = None,
        cleanup: Any | None = None,
        config: PipelineConfig | None = None,
        command_router: CommandRouter | None = None,
        command_executor: CommandExecutor | None = None,
    ) -> None:
        """Apply provider/config changes for the next utterance."""
        with self._runtime_lock:
            old_asr = self.asr
            self.asr = asr or self.asr
            self.injector = injector or self.injector
            self.cleanup = cleanup
            self.config = config or self.config
            self.command_router = command_router or self.command_router
            self.command_executor = command_executor or self.command_executor
        if asr is not None and asr is not old_asr:
            with contextlib.suppress(Exception):
                old_asr.unload()

    def _on_event(self, ev: AppEvent) -> None:
        if isinstance(ev, AsrReadyChanged) and ev.ready and self.state.is_recording:
            mode = self.state.active_mode
            live = _overlay_live_state(mode)
            self._overlay_call("set_state", live)
            if live != "streaming":
                self._overlay_call("set_message", "")
            return
        if isinstance(ev, (HotkeyPressed, HotkeyReleased, ShutdownRequested, WakeWordDetected)):
            self._events.put(ev)

    def _run(self) -> None:
        while not self._stop.is_set():
            ev = self._events.get()
            if ev is None:
                return
            if isinstance(ev, ShutdownRequested):
                self._stop.set()
                return
            try:
                if isinstance(ev, WakeWordDetected):
                    self._handle_wake_word(ev)
                elif isinstance(ev, HotkeyPressed):
                    self._handle_press(ev.mode, focus_target=ev.focus_target)
                elif isinstance(ev, HotkeyReleased):
                    self._handle_release(ev.mode)
            except _PipelineCancelled:
                # stop() invalidated an in-flight capture/load/decode. The
                # terminal path deliberately emits no UI, event, or injection.
                continue
            except Exception as exc:
                # A capture/device error (mic busy, unplugged, rejected rate)
                # must NOT kill the worker thread — that would deaden the hotkey
                # for the rest of the session. Recover to IDLE and keep serving.
                logger.exception("pipeline event handling failed; recovering to idle")
                if self._stream_stop is not None:
                    self._stream_stop.set()
                with contextlib.suppress(Exception):
                    self.capture.stop()
                self.state.cancel()
                self._overlay_call("show")
                self._overlay_call("set_state", "permission")
                self._overlay_call("set_message", "Check the microphone")
                self._overlay_call("hide_later", 1.4)
                self._notify_user(
                    "DCENT_Voice microphone",
                    "Could not start or finish audio capture — check the selected microphone",
                    key=f"capture:{type(exc).__name__}",
                    min_interval_s=15.0,
                )
                with contextlib.suppress(Exception):
                    self._publish_event(StateChanged(self.state.state.value, None))

    def _handle_wake_word(self, ev: WakeWordDetected) -> None:
        """Start dictation from a local wake-word hit (hands-free activation).

        Ends on trailing silence (``wake_end_silence_s``) or the same max hold as
        push-to-talk. A second wake while already recording is ignored so we do
        not stack utterances.
        """
        if self.state.is_recording or self.state.state is AppState.PROCESSING:
            logger.debug("wake-word ignored while busy phrase=%s", ev.phrase)
            return
        logger.info("wake-word activating dictation phrase=%s", ev.phrase)
        self._handle_press(AppMode.DICTATION, from_wake=True)

    def _handle_press(
        self,
        mode: AppMode,
        *,
        from_wake: bool = False,
        focus_target: object | None = None,
    ) -> None:
        if mode not in {AppMode.DICTATION, AppMode.COMMAND, AppMode.STREAMING}:
            return
        generation = self._generation_snapshot()
        with self._terminal_lock:
            self._checkpoint(generation)
            self._active_generation = generation
            try:
                state = self.state.press(mode)
            except InvalidTransition:
                if self.state.state is AppState.PROCESSING:
                    self._overlay_call("set_message", "Finishing previous dictation…")
                    self._notify_user(
                        "DCENT_Voice",
                        "Finishing the previous dictation — try again in a moment",
                        key="processing_busy",
                        min_interval_s=3.0,
                    )
                return
        self._cancel_auto_stop()
        self._cancel_wake_silence_watch()
        self._auto_stopped = False
        self._focus_target_at_press = focus_target or _capture_windows_focus_target()
        captured_top = getattr(self._focus_target_at_press, "top_hwnd", None)
        self._foreground_hwnd = int(captured_top) if captured_top else get_foreground_window()
        self._foreground_process = _foreground_process_name()
        self._foreground_title = window_title(self._foreground_hwnd)
        self._selection_at_press = ""
        if mode is AppMode.COMMAND and self.selection_getter is not None:
            try:
                self._selection_at_press = self.selection_getter()
            except Exception:
                self._selection_at_press = ""
        self.capture.begin_utterance()
        self._checkpoint(generation)
        self._arm_dead_default_failover()
        self._overlay_call("show")
        self._overlay_call("set_language", _overlay_language_label(self.config))
        self._overlay_call(
            "set_style",
            _overlay_style_label(self.config, self._foreground_process, self._foreground_title),
        )
        self._overlay_call("set_cleanup", _overlay_cleanup_label(self.config))
        self._overlay_call("set_priority", _overlay_priority_label(self.config, ""))
        model_loaded = _asr_is_loaded(self.asr)
        if not model_loaded:
            self._overlay_call("set_state", "loading")
            self._overlay_call("set_message", "Loading model…")
            self._notify_user(
                "DCENT_Voice",
                "The speech model will load when this recording ends…",
                key="asr-loading",
                min_interval_s=2.0,
            )
        else:
            self._overlay_call("set_state", _overlay_live_state(mode))
            self._overlay_call("set_language", _overlay_language_label(self.config))
            if from_wake:
                self._overlay_call("set_message", "Listening…")
            else:
                self._overlay_call("set_message", "")
        # Loading a cold local model concurrently with PortAudio can starve the
        # Python input callback long enough to lose most of a short first
        # utterance. Preserve the recording first; the final ASR pass loads
        # synchronously after capture is quiesced. Warm sessions retain live
        # streaming behavior.
        if mode is AppMode.STREAMING and model_loaded:
            self._start_streaming(generation)
        self._arm_auto_stop(mode)
        if from_wake and mode is AppMode.DICTATION:
            self._arm_wake_silence_end(mode)
        self._publish_event(StateChanged(state.value, mode), generation=generation)

    def _handle_release(self, mode: AppMode) -> None:
        generation = self._active_generation
        self._cancel_auto_stop()
        self._cancel_wake_silence_watch()
        self._cancel_dead_default_failover()
        with self._terminal_lock:
            self._checkpoint(generation)
            try:
                state = self.state.release(mode)
            except InvalidTransition:
                return
        if state is not AppState.PROCESSING:
            return
        self._publish_event(StateChanged(state.value, mode), generation=generation)
        self._overlay_call("set_state", "processing")
        if mode is AppMode.STREAMING:
            self._finish_streaming(generation)
        else:
            self._process_current_utterance(mode, generation)

    def _arm_auto_stop(self, mode: AppMode) -> None:
        limit = float(self.config.max_utterance_s or 0.0)
        if limit <= 0:
            return
        generation = self._active_generation

        def _fire() -> None:
            # Publish on the bus so release runs on the pipeline worker thread
            # (same path as a real hotkey release) without blocking the timer.
            if not self.state.is_recording or self.state.active_mode is not mode:
                return
            logger.info("auto-stopping utterance after %.1fs (mode=%s)", limit, mode.value)
            self._auto_stopped = True
            self._publish_event(HotkeyReleased(mode), generation=generation)

        timer = threading.Timer(limit, _fire)
        timer.daemon = True
        self._auto_stop_timer = timer
        timer.start()

    def _cancel_auto_stop(self) -> None:
        timer = self._auto_stop_timer
        self._auto_stop_timer = None
        if timer is not None:
            timer.cancel()

    def _arm_wake_silence_end(self, mode: AppMode) -> None:
        """Poll capture levels and end wake-started dictation after trailing silence."""
        silence_s = float(self.config.wake_end_silence_s or 0.0)
        if silence_s <= 0:
            return
        stop = threading.Event()
        self._wake_silence_stop = stop
        generation = self._active_generation

        def _watch() -> None:
            heard_speech = False
            silent_for = 0.0
            tick = 0.08
            threshold = float(self.config.silence_rms_threshold)
            while not stop.wait(tick):
                if not self.state.is_recording or self.state.active_mode is not mode:
                    return
                try:
                    audio = self.capture.peek_utterance()
                except Exception:
                    continue
                if audio is None or len(audio) == 0:
                    continue
                # Tail window ~200 ms.
                tail = audio[-int(0.2 * self.config.samplerate) :]
                level = _rms(tail)
                if level >= threshold:
                    heard_speech = True
                    silent_for = 0.0
                    continue
                if not heard_speech:
                    # Still waiting for the user to start speaking after wake.
                    continue
                silent_for += tick
                if silent_for >= silence_s:
                    logger.info(
                        "wake-word silence end after %.2fs quiet (mode=%s)",
                        silent_for,
                        mode.value,
                    )
                    self._publish_event(HotkeyReleased(mode), generation=generation)
                    return

        thread = threading.Thread(target=_watch, name="WakeSilenceEnd", daemon=True)
        self._wake_silence_thread = thread
        thread.start()

    def _cancel_wake_silence_watch(self) -> None:
        stop = getattr(self, "_wake_silence_stop", None)
        self._wake_silence_stop = None
        if stop is not None:
            stop.set()
        thread = getattr(self, "_wake_silence_thread", None)
        self._wake_silence_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def _arm_dead_default_failover(self) -> None:
        """Watch a silent OS-default mic and switch to a live input mid-hold."""
        self._cancel_dead_default_failover()
        failover = getattr(self.capture, "maybe_failover_dead_default", None)
        if not callable(failover):
            return
        stop = threading.Event()
        self._failover_stop = stop

        def _watch() -> None:
            if stop.wait(0.18):
                return
            while not stop.wait(0.22):
                if not self.state.is_recording:
                    return
                try:
                    resolved = failover()
                except Exception:
                    logger.exception("dead-default failover failed")
                    continue
                if resolved is None:
                    continue
                if resolved.auto_selected:
                    self._overlay_call(
                        "set_message",
                        f"Using {resolved.name} — default mic has no signal",
                    )
                    return
                if not resolved.default_was_dead:
                    return

        thread = threading.Thread(target=_watch, name="DeadDefaultFailover", daemon=True)
        thread.start()
        self._failover_thread = thread

    def _cancel_dead_default_failover(self) -> None:
        stop = getattr(self, "_failover_stop", None)
        self._failover_stop = None
        if stop is not None:
            stop.set()
        thread = getattr(self, "_failover_thread", None)
        self._failover_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def _process_current_utterance(self, mode: AppMode, generation: int) -> None:
        timer = StageTimer()
        injected = False
        reason = ""
        raw = ""
        cleaned = ""
        command_result = ""
        discarded = False
        duration_s = 0.0
        rms = 0.0
        user_error = ""
        insertion_expected = False
        stashed = False
        enter_skipped_after_insert = False

        try:
            self._checkpoint(generation)
            with self._runtime_lock:
                asr = self.asr
                cleanup = self.cleanup
                pipeline_config = self.config
                command_router = self.command_router
                command_executor = self.command_executor

            with timer.stage("capture"):
                audio = self.capture.end_utterance()
            self._checkpoint(generation)

            duration_s = len(audio) / float(pipeline_config.samplerate)
            rms = _rms(audio)
            if duration_s < pipeline_config.min_utterance_s:
                discarded = True
                reason = "too_short"
                return
            if rms < pipeline_config.silence_rms_threshold:
                discarded = True
                failover = getattr(self.capture, "maybe_failover_dead_default", None)
                if callable(failover):
                    with contextlib.suppress(Exception):
                        failover()
                snap: dict[str, Any] = {}
                getter = getattr(self.capture, "status_snapshot", None)
                if callable(getter):
                    with contextlib.suppress(Exception):
                        snap = getter() or {}
                if snap.get("default_was_dead"):
                    reason = "no_audio"
                    name = str(snap.get("resolved_name") or "selected microphone")
                    self._no_audio_message = f"No audio from {name}"
                else:
                    reason = "silence"
                    self._no_audio_message = ""
                return

            if self._auto_stopped:
                reason = "auto_stop"
                self._overlay_call("set_message", "Max recording length reached")
                self._notify_user(
                    "DCENT_Voice",
                    "Max recording length reached — finalizing",
                    key="auto_stop",
                    min_interval_s=10.0,
                )

            self._overlay_call(
                "set_message",
                f"Transcribing {duration_s:.0f}s…"
                if not self._auto_stopped
                else "Max length — transcribing…",
            )
            with timer.stage("asr"):
                if not _asr_is_loaded(asr):
                    self._checkpoint(generation)
                    self._overlay_call("set_message", "Loading speech model…")
                    self._checkpoint(generation)
                    asr.load()
                    self._checkpoint(generation)
                app_context = self._foreground_process
                style_name = _resolved_style_name(
                    pipeline_config, app_context, self._foreground_title
                )
                dictionary = _merged_dictionary(
                    pipeline_config,
                    style=style_name,
                    app=app_context,
                    asr=asr,
                )
                self._checkpoint(generation)
                transcript = asr.transcribe(
                    audio,
                    samplerate=pipeline_config.samplerate,
                    initial_prompt=build_initial_prompt(dictionary, pipeline_config.snippets),
                    hotwords=build_hotwords(dictionary, pipeline_config.snippets),
                )
                self._checkpoint(generation)
            raw = transcript.text
            if getattr(transcript, "rejected_reason", None):
                discarded = True
                reason = transcript.rejected_reason or "asr_hallucination"
                logger.info(
                    "asr rejected reason=%s dur=%.2fs rate=%.1f",
                    reason,
                    duration_s,
                    getattr(transcript, "chars_per_s", 0.0) or 0.0,
                )
                return
            if not (raw or "").strip():
                discarded = True
                reason = "asr_empty"
                return
            raw, next_style = _take_spoken_style(raw, style_name)
            if next_style != style_name:
                style_name = next_style
                self._overlay_call("set_style", _overlay_chip_label(style_name))
            cleanup_level = pipeline_config.cleanup_level
            raw, next_cleanup = _take_spoken_cleanup(raw, cleanup_level)
            if next_cleanup != cleanup_level:
                cleanup_level = next_cleanup
                self._overlay_call("set_cleanup", _overlay_chip_label(cleanup_level))
            if mode is AppMode.DICTATION and is_undo_last_command(raw):
                with self._terminal_lock:
                    self._checkpoint(generation)
                    reason = self._undo_last_injection(pipeline_config)
                discarded = reason != "undone"
                return
            submit_enter = False
            if mode is AppMode.DICTATION:
                submit_enter, raw = peel_spoken_press_enter(raw)
            if mode is AppMode.DICTATION and submit_enter and not (raw or "").strip():
                with self._terminal_lock:
                    self._checkpoint(generation)
                    restored = self._ensure_target_foreground(pipeline_config.focus_guard_enabled)
                    if restored:
                        with timer.stage("inject"):
                            self._press_enter()
                        reason = "enter"
                    else:
                        discarded = True
                        reason = "focus_changed"
                return
            dictionary = _merged_dictionary(
                pipeline_config,
                style=style_name,
                app=app_context,
                asr=asr,
            )
            corrections = extract_spoken_corrections(raw) if pipeline_config.spoken_edits else ()
            raw = _apply_personalization(pipeline_config, raw, style=style_name, app=app_context)
            applied_dictionary = _learned_post_dictionary(
                pipeline_config, style=style_name, app=app_context
            )
            raw = apply_dictionary(raw, applied_dictionary)
            last_fix = extract_last_correction(raw)
            if last_fix:
                with self._terminal_lock:
                    self._checkpoint(generation)
                    _learn_last(pipeline_config, last_fix, style=style_name, app=app_context)
                raw = last_fix
            raw = compose_dictation(
                raw,
                style=style_name,
                snippets=pipeline_config.snippets,
                dictionary=applied_dictionary,
                polish=pipeline_config.local_polish,
                spoken_edits=pipeline_config.spoken_edits,
                developer_terms=pipeline_config.developer_terms,
                cleanup_level=cleanup_level,
            )
            self._overlay_call("set_priority", _overlay_priority_label(pipeline_config, raw))
            if not (raw or "").strip():
                # Spoken "scratch that" can clear the whole utterance on purpose.
                discarded = True
                reason = "edited_empty"
                return
            cleaned = raw

            if cleanup is not None and pipeline_config.cleanup_enabled:
                self._checkpoint(generation)
                self._overlay_call("set_message", "Cleaning up…")
                with timer.stage("cleanup"):
                    cleaned = cleanup.clean(raw, style=style_name)
                self._checkpoint(generation)

            if mode is AppMode.DICTATION:
                insertion_expected = True
                with self._terminal_lock:
                    self._checkpoint(generation)
                    restored = self._ensure_target_foreground(pipeline_config.focus_guard_enabled)
                    target_bound = self._can_inject_into_press_target()
                    if not restored and not target_bound:
                        # A global injector is safe only while the captured
                        # window is foreground. Failed restoration with no
                        # explicitly bound destination must perform zero input.
                        reason = "focus_changed"
                        stashed = bool(self._stash_transcript(cleaned)) or stashed
                        return
                    try:
                        with timer.stage("inject"):
                            self._inject_text(cleaned)
                        injected = True
                        self._last_injected = cleaned
                        if submit_enter:
                            if restored or self._can_press_enter_into_press_target():
                                self._press_enter()
                                reason = "sent"
                            else:
                                enter_skipped_after_insert = True
                                reason = "focus_changed"
                        if corrections:
                            _record_corrections(
                                pipeline_config,
                                corrections,
                                style=style_name,
                                app=app_context,
                            )
                        _note_utterance(
                            pipeline_config,
                            raw,
                            cleaned,
                            style=style_name,
                            app=app_context,
                        )
                        self._overlay_call("set_state", "active")
                    except Exception as exc:
                        logger.exception(
                            "dictation inject failed restored=%s has_press_target=%s",
                            restored,
                            self._focus_target_at_press is not None,
                        )
                        reason = "focus_changed" if not restored else f"error:{type(exc).__name__}"
                        stashed = bool(self._stash_transcript(cleaned)) or stashed
                        if restored:
                            raise
            else:
                with timer.stage("command"):
                    router = command_router or CommandRouter()
                    intent = router.route(cleaned or raw, self._selection_at_press)
                    command_result = intent.text or ""
                reason = intent.reason
                if intent.action in {"rewrite_selection", "insert_text"}:
                    insertion_expected = True
                    with self._terminal_lock:
                        self._checkpoint(generation)
                        # Command injection must land in the captured target too.
                        if self._ensure_target_foreground(pipeline_config.focus_guard_enabled):
                            executor = command_executor or CommandExecutor(self.injector)
                            with timer.stage("inject"):
                                targeted_execute = getattr(executor, "execute_into_target", None)
                                if self._focus_target_at_press is not None and callable(
                                    targeted_execute
                                ):
                                    injected = targeted_execute(intent, self._focus_target_at_press)
                                else:
                                    injected = executor.execute(intent)
                        else:
                            reason = "focus_changed"
                            # Same as dictation: don't lose the result on a focus steal.
                            stashed = (
                                bool(self._stash_transcript(command_result or cleaned or raw))
                                or stashed
                            )
                elif intent.action == "tool_call" and intent.tool_call is not None:
                    with self._terminal_lock:
                        self._checkpoint(generation)
                        self.ade_dispatcher.dispatch(
                            intent.tool_call,
                            selection_present=bool(self._selection_at_press),
                        )
                    injected = False
                else:
                    injected = False
        except _PipelineCancelled:
            reason = "cancelled"
            discarded = True
        except Exception as exc:
            reason = f"error:{type(exc).__name__}"
            if isinstance(exc, ModelUnavailableError):
                user_error = str(exc)
            logger.exception("utterance processing failed")
            # Don't drop the words on the floor: if we transcribed anything but
            # never injected it, leave it on the clipboard so it can be pasted.
            fallback_text = command_result if mode is AppMode.COMMAND else (cleaned or raw)
            if not self._cancelled(generation) and not injected and fallback_text:
                stashed = bool(self._stash_transcript(fallback_text)) or stashed
        finally:
            preview = repr(raw[:120]) if _LOG_TRANSCRIPTS else f"<{len(raw)} chars>"
            logger.info(
                "utterance mode=%s dur=%.2fs rms=%.5f discarded=%s reason=%s "
                "injected=%s text=%s timings=%s",
                mode.value,
                duration_s,
                rms,
                discarded,
                reason or "ok",
                injected,
                preview,
                timer.as_dict(),
            )
            if self._cancelled(generation):
                self._foreground_hwnd = None
                self._focus_target_at_press = None
                self._foreground_process = ""
                self._foreground_title = ""
                self._selection_at_press = ""
                self._auto_stopped = False
                raise _PipelineCancelled
            fallback_text = command_result if mode is AppMode.COMMAND else (cleaned or raw)
            recovered = False
            if insertion_expected and not injected and fallback_text:
                recovered = self._record_failed_result(
                    fallback_text, reason=reason, mode=mode.value
                )
            if discarded:
                # Brief "no speech" visual so silence never feels like a dead hotkey.
                overlay_message = (
                    self._no_audio_message
                    if reason == "no_audio" and self._no_audio_message
                    else _discard_message(reason)
                )
                self._overlay_call("set_state", "discarded")
                self._overlay_call("set_message", overlay_message)
                self._overlay_call("hide_later", 0.9)
                self._notify_discard(reason)
            else:
                if reason == "focus_changed":
                    copied_label = "Command result" if mode is AppMode.COMMAND else "Dictation"
                    if enter_skipped_after_insert:
                        focus_message = "Text inserted; Enter skipped because focus changed"
                        overlay_message = "Text inserted — Enter skipped"
                    elif stashed:
                        focus_message = (
                            f"{copied_label} copied to clipboard — paste with {_paste_shortcut()}"
                        )
                        overlay_message = f"Copied — paste with {_paste_shortcut()}"
                    elif recovered:
                        focus_message = (
                            f"{copied_label} saved in Settings → Privacy → "
                            "Failed-dictation recovery"
                        )
                        overlay_message = "Saved in failed-dictation recovery"
                    else:
                        focus_message = f"{copied_label} not inserted because focus changed"
                        overlay_message = "Not inserted — focus changed"
                    self._notify_user(
                        "DCENT_Voice",
                        focus_message,
                        key="focus_changed",
                        min_interval_s=5.0,
                    )
                    self._overlay_call("set_state", "discarded")
                    self._overlay_call("set_message", overlay_message)
                elif reason.startswith("error:"):
                    self._notify_user(
                        "DCENT_Voice",
                        user_error or "Microphone or processing error — check Settings",
                        key=reason,
                        min_interval_s=15.0,
                    )
                    if user_error:
                        self._overlay_call(
                            "set_message",
                            "Speech model unavailable — open Settings",
                        )
                elif reason == "undone":
                    self._overlay_call("set_state", "discarded")
                    self._overlay_call("set_message", "Undone")
                elif reason == "enter":
                    self._overlay_call("set_state", "active")
                    self._overlay_call("set_message", "Enter")
                elif reason == "sent":
                    self._overlay_call("set_message", "Sent")
                elif reason == "auto_stop" and injected:
                    self._overlay_call("set_message", "Max length — done")
                self._overlay_call("hide_later", 0.4 if reason != "focus_changed" else 1.2)
            self._publish_event(
                TranscriptReady(
                    raw=raw,
                    cleaned=cleaned,
                    mode=mode,
                    timings=timer.as_dict(),
                    injected=injected,
                    discarded=discarded,
                    reason=reason or "ok",
                ),
                generation=generation,
            )
            with self._terminal_lock:
                self._checkpoint(generation)
                self.state.finish_processing()
                self._publish_event(
                    StateChanged(self.state.state.value, None), generation=generation
                )
            self._foreground_hwnd = None
            self._focus_target_at_press = None
            self._foreground_process = ""
            self._foreground_title = ""
            self._selection_at_press = ""
            self._auto_stopped = False

    def _overlay_call(self, method: str, *args: Any) -> None:
        with self._terminal_lock:
            if self._stop.is_set():
                return
            target = self.overlay
            if target is None:
                return
            fn = getattr(target, method, None)
            if callable(fn):
                fn(*args)

    def _notify_discard(self, reason: str) -> None:
        if reason == "silence":
            self._notify_user(
                "DCENT_Voice",
                "No speech detected — check mic mute and input device",
                key="silence",
                min_interval_s=30.0,
            )
        elif reason == "no_audio":
            self._notify_user(
                "DCENT_Voice",
                self._no_audio_message or "No audio from selected microphone",
                key="no_audio",
                min_interval_s=15.0,
            )
        elif reason == "too_short":
            self._notify_user(
                "DCENT_Voice",
                "Hold the hotkey a bit longer while speaking",
                key="too_short",
                min_interval_s=30.0,
            )

    def _notify_user(
        self,
        title: str,
        body: str,
        *,
        key: str,
        min_interval_s: float = 30.0,
    ) -> None:
        with self._terminal_lock:
            if self._stop.is_set() or self.notify is None:
                return
            now = time.monotonic()
            if key == self._last_notify_key and now - self._last_notify_at < min_interval_s:
                return
            self._last_notify_key = key
            self._last_notify_at = now
            with contextlib.suppress(Exception):
                self.notify(title, body)

    def _ensure_target_foreground(self, focus_guard_enabled: bool) -> bool:
        """Make sure the window captured at press is foreground before injecting.

        The overlay (or the Start menu, on a Win chord) can grab focus while an
        utterance is transcribed. Returns True if the target is (or was restored
        to) the foreground; False means injection should be skipped.
        """
        if not focus_guard_enabled:
            return True
        restored = True
        stolen = False
        if focus_changed(self._foreground_hwnd):
            stolen = True
            stealer = window_title(get_foreground_window())
            restored = restore_foreground(self._foreground_hwnd)
            logger.info(
                "focus left target during processing: target=%r stealer=%r restored=%s",
                window_title(self._foreground_hwnd),
                stealer,
                restored,
            )
        if restored:
            self._refocus_browser_page_field(steal_recovered=stolen)
        return restored

    def _refocus_browser_page_field(self, *, steal_recovered: bool = False) -> None:
        """Keep live search/composer focus after overlay or SPA blur.

        Never click into Notepad/VS Code; a search-box click would break
        an already-selected editor baseline.
        """
        if platform.system() != "Windows":
            return
        process = (self._foreground_process or "").casefold()
        target = self._focus_target_at_press
        if target is not None:
            process = process or str(getattr(target, "process_name", "") or "").casefold()
        if not any(
            name in process
            for name in ("msedge", "chrome", "firefox", "brave", "opera", "iexplore")
        ):
            return
        hwnd = self._foreground_hwnd
        if hwnd is None and target is not None:
            hwnd = getattr(target, "top_hwnd", None)
        with contextlib.suppress(Exception):
            from dcent_voice.inject.windows_uia import refocus_page_field

            refocus_page_field(
                hwnd=int(hwnd) if hwnd else None,
                timeout_s=1.2,
                steal_recovered=steal_recovered,
            )

    def _stash_transcript(self, text: str) -> bool:
        if not text:
            return False
        with self._terminal_lock:
            if self._stop.is_set():
                return False
            try:
                from dcent_voice.inject.clipboard import set_clipboard_text

                set_clipboard_text(text)
            except Exception:
                logger.warning("failed to copy uninserted transcript to clipboard", exc_info=True)
                return False
            return True

    def _record_failed_result(self, text: str, *, reason: str, mode: str) -> bool:
        """Best-effort opt-in retention; vault failures never break dictation."""

        store = self.recovery_store
        if store is None or not text:
            return False
        try:
            return bool(store.record(text, reason=reason or "insertion_failed", mode=mode))
        except Exception:
            return False

    def _can_inject_into_press_target(self) -> bool:
        return self._focus_target_at_press is not None and callable(
            getattr(self.injector, "inject_into_target", None)
        )

    def _can_press_enter_into_press_target(self) -> bool:
        return self._focus_target_at_press is not None and callable(
            getattr(self.injector, "press_enter_into_target", None)
        )

    def _inject_text(self, text: str) -> None:
        with self._terminal_lock:
            self._checkpoint(self._active_generation)
            targeted = getattr(self.injector, "inject_into_target", None)
            if self._focus_target_at_press is not None and callable(targeted):
                targeted(text, self._focus_target_at_press)
            else:
                self.injector.inject(text)

    def _retract_text(self, char_count: int) -> None:
        if char_count <= 0:
            return
        with self._terminal_lock:
            self._checkpoint(self._active_generation)
            self.injector.retract(char_count)

    def _press_enter(self) -> None:
        with self._terminal_lock:
            self._checkpoint(self._active_generation)
            targeted = getattr(self.injector, "press_enter_into_target", None)
            if self._focus_target_at_press is not None and callable(targeted):
                targeted(self._focus_target_at_press)
                return
            self.injector.press_enter()

    def _undo_last_injection(self, pipeline_config: PipelineConfig) -> str:
        previous = self._last_injected
        if not previous:
            return "nothing_to_undo"
        if not self._ensure_target_foreground(pipeline_config.focus_guard_enabled):
            return "focus_changed"
        try:
            self._retract_text(len(previous))
        except Exception:
            logger.exception("undo-last retract failed")
            return "error:Retract"
        self._last_injected = ""
        return "undone"

    def _start_streaming(self, generation: int | None = None) -> None:
        if self._stream_thread is not None and self._stream_thread.is_alive():
            # A repeat press must never orphan a live stream thread — replacing
            # its stop event would leave the old loop running with no way to
            # stop it.
            return
        self._stream_stop = threading.Event()
        self._stream_committer = IncrementalCommitter(
            agreement_passes=self.config.stream_agreement_passes,
            first_agreement_passes=self.config.stream_first_agreement_passes,
        )
        self._stream_injected = False
        self._stream_injected_text = ""
        self._stream_stashed = False
        expected = self._active_generation if generation is None else generation
        self._stream_thread = threading.Thread(
            target=self._streaming_loop,
            args=(expected,),
            name="PipelineStreaming",
            daemon=True,
        )
        self._stream_thread.start()

    def _streaming_loop(self, generation: int | None = None) -> None:
        expected = self._active_generation if generation is None else generation
        with self._runtime_lock:
            asr = self.asr
            cfg = self.config
        stop = self._stream_stop
        committer = self._stream_committer
        if stop is None or committer is None:
            return
        first_pass = True
        while True:
            if stop.wait(stream_pass_wait_s(cfg, first=first_pass)) or self._cancelled(expected):
                break
            first_pass = False
            try:
                audio = self.capture.peek_utterance()
                min_audio_s = max(0.0, float(cfg.stream_min_audio_s))
                if len(audio) / float(cfg.samplerate) < min_audio_s:
                    continue
                app_context = self._foreground_process
                style_name = _resolved_style_name(cfg, app_context, self._foreground_title)
                dictionary = _merged_dictionary(cfg, style=style_name, app=app_context, asr=asr)
                self._checkpoint(expected)
                transcript = asr.transcribe(
                    audio,
                    samplerate=cfg.samplerate,
                    initial_prompt=build_initial_prompt(dictionary, cfg.snippets),
                    hotwords=build_hotwords(dictionary, cfg.snippets),
                )
                self._checkpoint(expected)
                # Once finalize has signalled stop, this pass must not touch the
                # committer: advancing it without injecting would make finalize
                # skip those words, silently dropping the tail of the utterance.
                if stop.is_set():
                    break
                if getattr(transcript, "rejected_reason", None) or not transcript.text:
                    continue
                # Streaming: structure tokens + snippets only. Full polish (caps,
                # terminal periods, scratch-that) waits for finalize so already-
                # committed words do not rewrite under the user.
                partial = _apply_personalization(
                    cfg, transcript.text, style=style_name, app=app_context
                )
                partial = apply_dictionary(
                    partial,
                    _learned_post_dictionary(cfg, style=style_name, app=app_context),
                )
                partial, next_style = _take_spoken_style(partial, style_name)
                if next_style != style_name:
                    style_name = next_style
                    self._overlay_call("set_style", _overlay_chip_label(style_name))
                if cfg.spoken_edits or cfg.developer_terms:
                    partial = apply_spoken_tokens(partial, include_dev=cfg.developer_terms)
                if cfg.snippets:
                    partial = apply_snippets(partial, cfg.snippets)
                with self._inject_gate, self._terminal_lock:
                    self._checkpoint(expected)
                    if stop.is_set():
                        break
                    # Overlay shows the latest ASR partial immediately. Inject
                    # still waits for IncrementalCommitter so the document is
                    # not rewritten on every flicker.
                    self._overlay_call("set_message", partial)
                    self._overlay_call("set_priority", _overlay_priority_label(cfg, partial))
                    delta = committer.update(partial)
                    if delta:
                        self._inject_stream_delta(delta, expected)
            except _PipelineCancelled:
                break
            except Exception:
                logger.exception("streaming pass failed")

    def _finish_streaming(self, generation: int | None = None) -> None:
        expected = self._active_generation if generation is None else generation
        timer = StageTimer()
        injected = False
        raw = ""
        reason = "ok"
        insertion_expected = False
        recovery_needed = False
        corrections: tuple[tuple[str, str], ...] = ()
        app_context = self._foreground_process
        style_name = "plain"
        if self._stream_stop is not None:
            self._stream_stop.set()
        thread = self._stream_thread
        if thread is not None:
            thread.join(timeout=3.0)
            if thread.is_alive():
                logger.warning("streaming pass did not stop before finalize timeout")
        try:
            self._checkpoint(expected)
            with self._runtime_lock:
                asr = self.asr
                cfg = self.config
            with timer.stage("capture"):
                audio = self.capture.end_utterance()
            self._checkpoint(expected)
            duration_s = len(audio) / float(cfg.samplerate)
            submit_enter = False
            if duration_s >= cfg.min_utterance_s and _rms(audio) >= cfg.silence_rms_threshold:
                with timer.stage("asr"):
                    if not _asr_is_loaded(asr):
                        self._checkpoint(expected)
                        self._overlay_call("set_message", "Loading speech model…")
                        self._checkpoint(expected)
                        asr.load()
                        self._checkpoint(expected)
                    app_context = self._foreground_process
                    style_name = _resolved_style_name(cfg, app_context, self._foreground_title)
                    dictionary = _merged_dictionary(cfg, style=style_name, app=app_context, asr=asr)
                    self._checkpoint(expected)
                    transcript = asr.transcribe(
                        audio,
                        samplerate=cfg.samplerate,
                        initial_prompt=build_initial_prompt(dictionary, cfg.snippets),
                        hotwords=build_hotwords(dictionary, cfg.snippets),
                    )
                    self._checkpoint(expected)
                if getattr(transcript, "rejected_reason", None):
                    reason = transcript.rejected_reason
                    raw = ""
                else:
                    decoded = transcript.text or ""
                    decoded, next_style = _take_spoken_style(decoded, style_name)
                    if next_style != style_name:
                        style_name = next_style
                        self._overlay_call("set_style", _overlay_chip_label(style_name))
                    cleanup_level = cfg.cleanup_level
                    decoded, next_cleanup = _take_spoken_cleanup(decoded, cleanup_level)
                    if next_cleanup != cleanup_level:
                        cleanup_level = next_cleanup
                        self._overlay_call("set_cleanup", _overlay_chip_label(cleanup_level))
                    submit_enter, decoded = peel_spoken_press_enter(decoded)
                    corrections = extract_spoken_corrections(decoded) if cfg.spoken_edits else ()
                    raw = _apply_personalization(cfg, decoded, style=style_name, app=app_context)
                    raw = apply_dictionary(
                        raw,
                        _learned_post_dictionary(cfg, style=style_name, app=app_context),
                    )
                    last_fix = extract_last_correction(raw)
                    if last_fix:
                        with self._terminal_lock:
                            self._checkpoint(expected)
                            _learn_last(cfg, last_fix, style=style_name, app=app_context)
                        raw = last_fix
                    raw = compose_dictation(
                        raw,
                        style=style_name,
                        snippets=cfg.snippets,
                        dictionary=_learned_post_dictionary(cfg, style=style_name, app=app_context),
                        polish=cfg.local_polish,
                        spoken_edits=cfg.spoken_edits,
                        developer_terms=cfg.developer_terms,
                        cleanup_level=cleanup_level,
                    )
                    self._overlay_call("set_priority", _overlay_priority_label(cfg, raw))
                with self._inject_gate, self._terminal_lock, timer.stage("inject"):
                    self._checkpoint(expected)
                    insertion_expected = bool((raw or "").strip())
                    reconciled = self._reconcile_stream_injection(raw, expected)
                    if not reconciled and insertion_expected:
                        recovery_needed = True
                        reason = "focus_changed"
                    if submit_enter:
                        # Re-check even when the final text already matches the
                        # streamed prefix (or is empty). Reconciliation can be a
                        # no-op, but Enter is still a new global side effect and
                        # must never land in a focus-stealing application.
                        enter_target_ready = reconciled and self._ensure_target_foreground(
                            cfg.focus_guard_enabled
                        )
                        if enter_target_ready:
                            self._press_enter()
                            reason = "sent" if (raw or "").strip() else "enter"
                        else:
                            reason = "focus_changed"
                            if reconciled and (raw or "").strip():
                                self._notify_user(
                                    "DCENT_Voice",
                                    "Text inserted; Enter skipped because focus changed",
                                    key="enter_focus_changed",
                                    min_interval_s=5.0,
                                )
                if (raw or "").strip():
                    if corrections:
                        _record_corrections(
                            cfg,
                            corrections,
                            style=style_name,
                            app=app_context,
                        )
                    _note_utterance(
                        cfg,
                        decoded,
                        raw,
                        style=style_name,
                        app=app_context,
                    )
            injected = (
                self._stream_injected
                or bool(self._stream_injected_text)
                or reason
                in {
                    "sent",
                    "enter",
                }
            )
            if injected and (raw or "").strip():
                self._last_injected = self._stream_injected_text or raw
            if (
                not (raw or "").strip()
                and not self._stream_injected_text
                and reason not in {"sent", "enter"}
            ):
                reason = reason if reason != "ok" else "edited_empty"
            self._overlay_call(
                "set_state",
                "active" if (raw or "").strip() or reason in {"sent", "enter"} else "discarded",
            )
            if reason == "edited_empty":
                self._overlay_call("set_message", _discard_message("edited_empty"))
            elif reason == "enter":
                self._overlay_call("set_message", "Enter")
            elif reason == "sent":
                self._overlay_call("set_message", "Sent")
        except _PipelineCancelled:
            reason = "cancelled"
        except Exception as exc:
            reason = f"error:{type(exc).__name__}"
            logger.exception("streaming finalize failed")
        finally:
            preview = repr(raw[:120]) if _LOG_TRANSCRIPTS else f"<{len(raw)} chars>"
            logger.info(
                "utterance mode=streaming reason=%s injected=%s text=%s timings=%s",
                reason,
                injected,
                preview,
                timer.as_dict(),
            )
            if self._cancelled(expected):
                self._stream_stop = None
                self._stream_thread = None
                self._stream_committer = None
                self._stream_injected = False
                self._stream_injected_text = ""
                self._stream_stashed = False
                self._foreground_hwnd = None
                self._focus_target_at_press = None
                self._foreground_process = ""
                self._foreground_title = ""
                raise _PipelineCancelled
            recovered = False
            if (recovery_needed or (insertion_expected and reason.startswith("error:"))) and raw:
                recovered = self._record_failed_result(
                    raw, reason=reason, mode=AppMode.STREAMING.value
                )
            if recovery_needed:
                if self._stream_stashed:
                    message = f"Dictation copied to clipboard — paste with {_paste_shortcut()}"
                elif recovered:
                    message = "Dictation saved in Settings → Privacy → Failed-dictation recovery"
                else:
                    message = "Dictation not inserted because focus changed"
                self._notify_user(
                    "DCENT_Voice",
                    message,
                    key="focus_changed",
                    min_interval_s=5.0,
                )
            self._overlay_call("hide_later", 0.4)
            self._publish_event(
                TranscriptReady(
                    raw=raw,
                    cleaned=raw,
                    mode=AppMode.STREAMING,
                    timings=timer.as_dict(),
                    injected=injected and bool((raw or "").strip() or self._stream_injected),
                    discarded=reason == "edited_empty"
                    or (not (raw or "").strip() and reason not in {"sent", "enter"}),
                    reason=reason,
                ),
                generation=expected,
            )
            with self._terminal_lock:
                self._checkpoint(expected)
                self.state.finish_processing()
                self._publish_event(StateChanged(self.state.state.value, None), generation=expected)
            self._stream_stop = None
            self._stream_thread = None
            self._stream_committer = None
            self._stream_injected = False
            self._stream_injected_text = ""
            self._stream_stashed = False
            self._foreground_hwnd = None
            self._focus_target_at_press = None
            self._foreground_process = ""
            self._foreground_title = ""

    def _reconcile_stream_injection(self, desired: str, generation: int | None = None) -> bool:
        """Bring the focused field in line with the final postprocessed transcript.

        Streaming may already have typed a committed prefix. Spoken edits such as
        "scratch that" can shorten or clear that prefix; we retract surplus
        characters (Backspace) then inject any residual. When the final text
        diverges from the prefix (not a pure extension), replace entirely.
        """
        expected = self._active_generation if generation is None else generation
        self._checkpoint(expected)
        desired = desired or ""
        previous = self._stream_injected_text or ""
        if desired == previous:
            return True
        with self._runtime_lock:
            focus_guard = self.config.focus_guard_enabled
        if not self._ensure_target_foreground(focus_guard):
            self._stream_stashed = bool(self._stash_transcript(desired)) or self._stream_stashed
            return False

        if not previous:
            if desired:
                self._inject_text(desired)
                self._stream_injected_text = desired
                self._stream_injected = True
            return True

        if desired.startswith(previous):
            residual = desired[len(previous) :]
            if residual:
                self._inject_text(residual)
                self._stream_injected_text = desired
                self._stream_injected = True
            return True

        if previous.startswith(desired):
            # Shortened in place (trailing delete / partial scratch).
            extra = len(previous) - len(desired)
            if extra > 0:
                self._retract_text(extra)
            self._stream_injected_text = desired
            self._stream_injected = bool(desired)
            return True

        # Divergent rewrite (e.g. mid-utterance "scratch that" rewrites clause).
        self._retract_text(len(previous))
        if desired:
            self._inject_text(desired)
            self._stream_injected_text = desired
            self._stream_injected = True
        else:
            self._stream_injected_text = ""
            self._stream_injected = False
        return True

    def _inject_stream_delta(self, delta: str, generation: int | None = None) -> None:
        expected = self._active_generation if generation is None else generation
        self._checkpoint(expected)
        text = (" " + delta) if self._stream_injected else delta
        with self._runtime_lock:
            focus_guard = self.config.focus_guard_enabled
        if not self._ensure_target_foreground(focus_guard):
            copied = self._stash_transcript(text)
            self._stream_stashed = bool(copied) or self._stream_stashed
            self._notify_user(
                "DCENT_Voice",
                (
                    f"Dictation copied to clipboard — paste with {_paste_shortcut()}"
                    if copied
                    else "Dictation not inserted because focus changed"
                ),
                key="focus_changed",
                min_interval_s=5.0,
            )
            return
        self._inject_text(text)
        self._stream_injected = True
        self._stream_injected_text = (self._stream_injected_text or "") + text


def _dedupe_vocab(items: list[VocabEntry]) -> tuple[VocabEntry, ...]:
    seen: set[tuple[str, str]] = set()
    out: list[VocabEntry] = []
    for entry in items:
        key = ((entry.spoken or "").casefold(), entry.written)
        if not entry.spoken or key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return tuple(out)


def _is_local_asr(asr: object | None) -> bool:
    from dcent_voice.asr.base import Locality

    return getattr(asr, "locality", None) is Locality.LOCAL


def postprocess_dictionary(dictionary: tuple[VocabEntry, ...]) -> tuple[VocabEntry, ...]:
    """User dictionary plus shipped domain terms. Never includes learned names."""
    from dcent_voice.dictation.vocab import shipped_domain_vocab

    return _dedupe_vocab([*dictionary, *shipped_domain_vocab()])


def merge_asr_hint_dictionary(
    dictionary: tuple[VocabEntry, ...],
    *,
    asr: object | None = None,
    personalization: object | None = None,
    style: str | None = None,
    app: str | None = None,
) -> tuple[VocabEntry, ...]:
    """ASR hints. Learned + shipped terms stay on local providers only."""
    from dcent_voice.dictation.vocab import shipped_domain_vocab

    items = list(dictionary)
    if not _is_local_asr(asr):
        return tuple(items)
    items.extend(shipped_domain_vocab())
    if personalization is not None:
        try:
            as_vocab = getattr(personalization, "as_vocab", None)
            if callable(as_vocab):
                items.extend(as_vocab(style=style, app=app))
        except Exception:
            logger.exception("personalization as_vocab failed")
    return _dedupe_vocab(items)


def _merged_dictionary(
    pipeline_config: PipelineConfig,
    *,
    style: str | None = None,
    app: str | None = None,
    asr: object | None = None,
) -> tuple[VocabEntry, ...]:
    # Learned terms and shipped domain vocab boost local ASR only. Cloud
    # providers still receive the user's explicit [dictionary] table and
    # nothing from personalization.json.
    return merge_asr_hint_dictionary(
        pipeline_config.dictionary,
        asr=asr,
        personalization=pipeline_config.personalization,
        style=style,
        app=app,
    )


def _foreground_process_name() -> str:
    from dcent_voice.inject.router import get_foreground_process_name

    try:
        return get_foreground_process_name() or ""
    except Exception:
        return ""


def _capture_windows_focus_target() -> object | None:
    if platform.system() != "Windows":
        return None
    try:
        from dcent_voice.inject.windows_focus import capture_foreground_target

        return capture_foreground_target()
    except Exception:
        logger.debug("could not capture focused child control at press", exc_info=True)
        return None


def _resolved_style_name(
    pipeline_config: PipelineConfig,
    process: str | None = None,
    title: str | None = None,
) -> str:
    if process is None:
        process = _foreground_process_name()
    learned: dict[str, str] = {}
    store = pipeline_config.personalization
    getter = getattr(store, "learned_app_styles", None) if store is not None else None
    if callable(getter):
        try:
            learned = dict(getter() or {})
        except Exception:
            logger.exception("learned_app_styles failed")
    return resolve_style(
        pipeline_config.style_default,
        process,
        pipeline_config.style_per_app,
        window_title=title,
        learned_per_app=learned,
    )


def _take_spoken_style(text: str, style_name: str) -> tuple[str, str]:
    spoken, remainder = peel_spoken_style(text)
    if spoken:
        return remainder, spoken
    return text, style_name


def _take_spoken_cleanup(text: str, cleanup_level: str) -> tuple[str, str]:
    spoken, remainder = peel_spoken_cleanup(text)
    if spoken:
        return remainder, spoken
    return text, cleanup_level


def _learned_post_dictionary(
    pipeline_config: PipelineConfig,
    *,
    style: str | None,
    app: str | None,
) -> tuple:
    items = list(pipeline_config.dictionary)
    as_vocab = getattr(pipeline_config.personalization, "as_vocab", None)
    if callable(as_vocab):
        try:
            items.extend(as_vocab(style=style, app=app))
        except Exception:
            logger.exception("personalization as_vocab failed")
    return postprocess_dictionary(tuple(items))


def _apply_personalization(
    pipeline_config: PipelineConfig,
    text: str,
    *,
    style: str,
    app: str,
) -> str:
    store = pipeline_config.personalization
    if store is None:
        return text
    prose_context = pipeline_config.personalization_prose_context
    if type(prose_context) is not bool:
        logger.error("personalization_prose_context must be a boolean; refusing rewrite")
        return text
    try:
        return store.apply(
            text,
            style=style,
            app=app,
            prose_context=prose_context,
        )
    except Exception:
        logger.exception("personalization apply failed")
        return text


def _learn_last(
    pipeline_config: PipelineConfig,
    correction: str,
    *,
    style: str,
    app: str,
) -> None:
    store = pipeline_config.personalization
    if store is None:
        return
    with contextlib.suppress(Exception):
        store.learn_last(correction, source="spoken_last", style=style, app=app)


def _note_utterance(
    pipeline_config: PipelineConfig,
    raw: str,
    cleaned: str,
    *,
    style: str,
    app: str,
) -> None:
    store = pipeline_config.personalization
    if store is None:
        return
    with contextlib.suppress(Exception):
        store.note_utterance(raw, cleaned, style=style, app=app)


def _record_corrections(
    pipeline_config: PipelineConfig,
    pairs: tuple[tuple[str, str], ...],
    *,
    style: str,
    app: str,
) -> None:
    store = pipeline_config.personalization
    if store is None:
        return
    with contextlib.suppress(Exception):
        store.record_pairs(pairs, style=style, app=app)


def _starred_first(dictionary: tuple[VocabEntry, ...]) -> tuple[VocabEntry, ...]:
    """Starred terms lead ASR hints. Display sort does not change this order."""

    return starred_first(dictionary)


def _starred_first_snippets(
    snippets: tuple[SnippetEntry, ...] | tuple[object, ...] = (),
) -> tuple[object, ...]:
    """Starred snippet cues lead ASR hints. Display sort does not change this order."""
    return tuple(
        sorted(
            snippets or (),
            key=lambda entry: 0 if getattr(entry, "starred", False) else 1,
        )
    )


def build_initial_prompt(
    dictionary: tuple[VocabEntry, ...],
    snippets: tuple[SnippetEntry, ...] | tuple[object, ...] = (),
) -> str | None:
    """Natural prior-text prompt (written forms and snippet cues).

    Meta syntax like ``spoken -> written`` is a known Whisper pollution source
    under low confidence; use short natural terms instead. Starred dictionary
    terms and starred snippet cues come first so they survive the length cap —
    local dictation priority, not cloud starring. Expansions stay out of
    the prompt so they are not echoed.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        text = str(token or "").strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(text)

    for vocab in _starred_first(dictionary):
        if vocab.starred:
            _add(vocab.written)
    for snippet in _starred_first_snippets(snippets):
        if getattr(snippet, "starred", False):
            _add(getattr(snippet, "spoken", ""))
    for vocab in _starred_first(dictionary):
        if not vocab.starred:
            _add(vocab.written)
    for snippet in _starred_first_snippets(snippets):
        if not getattr(snippet, "starred", False):
            _add(getattr(snippet, "spoken", ""))
    if not terms:
        return None
    joined = ", ".join(terms[:24])
    return f"{joined}."


def build_hotwords(
    dictionary: tuple[VocabEntry, ...],
    snippets: tuple[SnippetEntry, ...] | tuple[object, ...] = (),
) -> str | None:
    """Space-joined terms for faster-whisper ``hotwords=``. Starred snippet cues lead."""
    parts: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        text = str(token or "").strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        parts.append(text)

    for vocab in _starred_first(dictionary):
        if vocab.starred:
            _add(vocab.written)
            _add(vocab.spoken)
    for snippet in _starred_first_snippets(snippets):
        if getattr(snippet, "starred", False):
            _add(getattr(snippet, "spoken", ""))
    for vocab in _starred_first(dictionary):
        if not vocab.starred:
            _add(vocab.written)
            _add(vocab.spoken)
    for snippet in _starred_first_snippets(snippets):
        if not getattr(snippet, "starred", False):
            _add(getattr(snippet, "spoken", ""))
    return " ".join(parts[:48]) if parts else None


def apply_dictionary(text: str, dictionary: tuple[VocabLike, ...]) -> str:
    """Deterministic spoken→written replacements after ASR (case-insensitive)."""
    if not text or not dictionary:
        return text
    import re

    result = text
    # Longer spoken phrases first to avoid partial overwrites. Same length:
    # starred wins (local dictation priority when terms conflict).
    ordered = sorted(
        dictionary,
        key=lambda entry: (-len(entry.spoken or ""), 0 if entry.starred else 1),
    )
    for entry in ordered:
        spoken = (entry.spoken or "").strip()
        written = (entry.written or "").strip()
        if not spoken or not written:
            continue
        # Do not rewrite substrings inside larger words. Lookarounds also work
        # for multi-word terms and avoid the ASCII-only edge cases of \b.
        pattern = re.compile(rf"(?<!\w){re.escape(spoken)}(?!\w)", re.IGNORECASE)
        result = pattern.sub(written, result)
    return result


def _overlay_language_label(config: PipelineConfig) -> str:
    from dcent_voice.asr.language import resolve_language_policy

    return resolve_language_policy(config.language_mode, config.language).overlay_label


def _overlay_chip_label(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    return text[:1].upper() + text[1:]


def _overlay_style_label(
    pipeline_config: PipelineConfig,
    process: str | None = None,
    title: str | None = None,
) -> str:
    return _overlay_chip_label(_resolved_style_name(pipeline_config, process, title))


def _overlay_cleanup_label(pipeline_config: PipelineConfig) -> str:
    return _overlay_chip_label(pipeline_config.cleanup_level or "medium")


def _overlay_priority_label(pipeline_config: PipelineConfig, text: str = "") -> str:
    """Name a starred form present in the transcript, or hide the empty chip."""
    blob = str(text or "")
    if not blob.strip():
        return ""
    import re

    def _hit(cue: str) -> bool:
        needle = str(cue or "").strip()
        if not needle:
            return False
        return bool(re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", blob, re.IGNORECASE))

    def _chip(*parts: str) -> str:
        for part in parts:
            label = str(part or "").strip()
            if label:
                return label if len(label) <= 24 else f"{label[:23]}…"
        return "Priority"

    for vocab in pipeline_config.dictionary:
        if vocab.starred and (_hit(vocab.spoken) or _hit(vocab.written)):
            return _chip(vocab.written, vocab.spoken)
    for snippet in pipeline_config.snippets:
        if snippet.starred and (_hit(snippet.spoken) or _hit(snippet.expansion)):
            return _chip(snippet.expansion, snippet.spoken)
    return ""


def _overlay_live_state(mode: AppMode | None) -> str:
    if mode is AppMode.COMMAND:
        return "command"
    if mode is AppMode.STREAMING:
        return "streaming"
    return "listening"


def _rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float32)))))


def _discard_message(reason: str) -> str:
    if reason == "silence":
        return "No speech detected"
    if reason == "no_audio":
        return "No audio from selected microphone"
    if reason == "too_short":
        return "Too short"
    if reason == "edited_empty":
        # Spoken "scratch that" cleared the whole utterance on purpose.
        return "Cleared — scratch that"
    if reason == "nothing_to_undo":
        return "Nothing to undo"
    if reason in {"asr_hallucination", "asr_phantom", "asr_hint_echo"}:
        return "Could not understand speech"
    if reason == "asr_empty":
        return "No speech recognized"
    return "Discarded"


def _paste_shortcut() -> str:
    return "Command+V" if platform.system() == "Darwin" else "Ctrl+V"


def get_foreground_window() -> int | None:
    if platform.system() != "Windows":
        return None
    handle = _user32.GetForegroundWindow()
    return int(handle or 0)


def focus_changed(original_hwnd: int | None) -> bool:
    if not original_hwnd:
        return False
    current = get_foreground_window()
    return bool(current and current != original_hwnd)


def window_title(hwnd: int | None) -> str:
    if not hwnd or _user32 is None:
        return ""
    try:
        length = int(_user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def restore_foreground(hwnd: int | None) -> bool:
    """Bring `hwnd` back to the foreground so injected input lands in it.

    The overlay (or the Start menu, if Win is part of the chord) can grab focus
    while an utterance is transcribed. SetForegroundWindow is normally refused
    for a background thread, so attach to both the current-foreground and target
    input queues first — the standard workaround.
    """
    if not hwnd or _user32 is None:
        return False
    from dcent_voice.inject.windows_focus import restore_foreground as restore_hwnd

    return bool(restore_hwnd(int(hwnd)))


_user32: Any
_kernel32: Any
if platform.system() == "Windows":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    _user32.keybd_event.restype = None
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
else:  # pragma: no cover - Windows-only implementation guard
    _user32 = None
    _kernel32 = None


def _hold_release_fixture() -> Path:
    """Source-checkout WAV used by the hold-release scoring harness.

    Development-only: the frozen application never ships ``tests/``, so callers
    that reach this in a packaged build get a clear error instead of a path that
    silently does not exist.
    """
    if paths.is_frozen():
        raise RuntimeError(
            "The hold-release scoring harness needs tests/fixtures/audio/hello.wav, which is "
            "not part of the packaged DCENT_Voice application; run it from a source checkout "
            "or pass audio_path explicitly."
        )
    return paths.source_root() / "tests" / "fixtures" / "audio" / "hello.wav"


_HOLD_RELEASE_REFERENCE = "Hello world"


class _HoldReleaseCapture:
    """WAV-backed capture for shipped hold-release scoring. Not a live microphone."""

    def __init__(self, audio: np.ndarray, samplerate: int) -> None:
        self.audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        self.samplerate = int(samplerate)
        self.started = False
        self.stopped = False

    def begin_utterance(self) -> None:
        self.started = True

    def end_utterance(self) -> np.ndarray:
        return self.audio

    def peek_utterance(self) -> np.ndarray:
        return self.audio

    def stop(self) -> None:
        self.stopped = True


def _load_hold_release_wav(path: Path) -> tuple[np.ndarray, int]:
    import wave

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError("Only 16-bit PCM WAV files are supported.")
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if sample_rate != 16000:
        duration_s = len(data) / float(sample_rate)
        old_x = np.linspace(0.0, duration_s, num=len(data), endpoint=False)
        new_len = int(duration_s * 16000)
        new_x = np.linspace(0.0, duration_s, num=new_len, endpoint=False)
        data = np.interp(new_x, old_x, data).astype(np.float32)
        sample_rate = 16000
    return data, sample_rate


def score_shipped_default_hold_release(
    asr: Any,
    injector: Any,
    *,
    audio_path: Path | str | None = None,
    timeout_s: float = 30.0,
) -> HoldReleaseScore:
    """Score hold-release real speech through the configured injector."""
    from dcent_voice.eval_corpus import char_error_rate, word_error_rate

    wav = Path(audio_path) if audio_path is not None else _hold_release_fixture()
    if not wav.is_file():
        raise FileNotFoundError(f"missing hold-release audio: {wav}")
    audio, samplerate = _load_hold_release_wav(wav)
    rms = _rms(audio)
    if rms < 0.01:
        raise ValueError(f"hold-release audio is silence: {wav}")
    capture = _HoldReleaseCapture(audio, samplerate)
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []

    def _on_event(ev: AppEvent) -> None:
        if isinstance(ev, TranscriptReady):
            ready.append(ev)
            done.set()

    bus.subscribe(_on_event)
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=capture,  # type: ignore[arg-type]
        asr=asr,
        injector=injector,
        config=PipelineConfig(focus_guard_enabled=False, samplerate=samplerate),
    )
    worker.start()
    try:
        bus.publish(HotkeyPressed(AppMode.DICTATION))
        bus.publish(HotkeyReleased(AppMode.DICTATION))
        if not done.wait(timeout_s):
            raise TimeoutError("hold-release did not finish")
    finally:
        worker.stop()
        bus.stop()
    event = ready[-1]
    hypothesis = event.cleaned or event.raw
    injected_payloads = getattr(injector, "injected", None)
    if event.injected and isinstance(injected_payloads, list) and injected_payloads:
        hypothesis = str(injected_payloads[-1])
    if not capture.started:
        raise RuntimeError("hold-release capture never began")
    return HoldReleaseScore(
        id=wav.stem,
        reference=_HOLD_RELEASE_REFERENCE,
        hypothesis=hypothesis,
        raw=event.raw,
        injected=bool(event.injected),
        discarded=bool(event.discarded),
        reason=event.reason,
        wer=word_error_rate(_HOLD_RELEASE_REFERENCE, hypothesis),
        cer=char_error_rate(_HOLD_RELEASE_REFERENCE, hypothesis),
        timings=dict(event.timings),
        rms=rms,
        duration_s=len(audio) / float(samplerate),
        kind="hold_release",
    )
