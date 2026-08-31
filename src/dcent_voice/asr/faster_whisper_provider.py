# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Transcribe local audio with Faster Whisper."""

from __future__ import annotations

import contextlib
import logging
import platform
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice.asr.base import (
    WHISPER_LANGUAGE_CODES,
    ASRProvider,
    Locality,
    TranscriptResult,
    UnsupportedLanguageError,
    normalize_language_hint,
)
from dcent_voice.asr.lifecycle import DEFAULT_IDLE_UNLOAD_S
from dcent_voice.asr.model_registry import (
    canonical_model_id,
    resolve_faster_whisper_model,
    verified_snapshot_lock,
)
from dcent_voice.asr.quality import sanitize_transcript, segment_text_keep
from dcent_voice.config import APP_NAME, ASRSpec

logger = logging.getLogger(APP_NAME).getChild("asr")


class FasterWhisperASRProvider(ASRProvider):
    """Local faster-whisper ASR provider with lazy model lifecycle management."""

    locality = Locality.LOCAL
    supports_per_call_language = True

    @property
    def supported_language_codes(self) -> frozenset[str]:
        if self.spec.model.strip().lower().endswith(".en"):
            return frozenset({"en"})
        return WHISPER_LANGUAGE_CODES

    @property
    def supports_language_auto_detection(self) -> bool:
        return not self.spec.model.strip().lower().endswith(".en")

    def validate_language_hint(self, language: str | None) -> str | None:
        normalized = normalize_language_hint(language)
        if self.spec.model.strip().lower().endswith(".en"):
            if normalized == "":
                raise UnsupportedLanguageError(
                    "the selected faster-whisper .en model does not support "
                    "automatic language detection"
                )
            if normalized not in {None, "en"}:
                raise UnsupportedLanguageError(
                    "the selected faster-whisper .en model supports only English"
                )
        return super().validate_language_hint(normalized)

    def __init__(
        self,
        spec: ASRSpec,
        *,
        language: str = "en",
        vad_filter: bool = True,
        beam_size: int = 1,
        idle_unload_s: float = DEFAULT_IDLE_UNLOAD_S,
        condition_on_previous_text: bool = False,
        temperature: float = 0.0,
        no_speech_threshold: float = 0.6,
        compression_ratio_threshold: float = 2.4,
        log_prob_threshold: float = -1.0,
        no_repeat_ngram_size: int = 3,
        without_timestamps: bool = True,
        max_chars_per_s: float = 28.0,
    ) -> None:
        if spec.provider != "faster-whisper":
            raise ValueError(f"Unsupported ASR provider for FasterWhisperASRProvider: {spec.raw}")
        self.spec = spec
        configured_language = self.validate_language_hint(language)
        self.language = "auto" if configured_language == "" else configured_language or "en"
        self.vad_filter = vad_filter
        self.beam_size = beam_size
        # Anti-hallucination defaults: never cascade previous-window text into
        # the next (classic "should be able to be able…" loops), and never climb
        # the temperature ladder into random sampling.
        self.condition_on_previous_text = condition_on_previous_text
        self.temperature = temperature
        self.no_speech_threshold = no_speech_threshold
        self.compression_ratio_threshold = compression_ratio_threshold
        self.log_prob_threshold = log_prob_threshold
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.without_timestamps = without_timestamps
        self.max_chars_per_s = max_chars_per_s
        # Release the loaded model after this many seconds with no utterance so an
        # idle session doesn't hold 0.5-3 GB of weights resident; it reloads
        # lazily on the next transcribe. 0 disables. transcribe() binds a strong
        # local reference to the model under the lock, so even a timer that has
        # already fired (cancel() is a no-op then) can only null self._model —
        # never free the model out from under an in-flight decode.
        self.idle_unload_s = idle_unload_s
        self._model: Any | None = None
        self._device, self._compute_type = resolve_device_compute(spec)
        self._requested_device = self._device
        self._last_load_s: float | None = None
        self._last_decode_s: float | None = None
        self._fallback_reason: str | None = None
        self._resolved_model = spec.model
        self._lock = threading.RLock()
        # Serializes decodes: CTranslate2 models are not safe for concurrent
        # transcribe() calls (e.g. a straggling streaming pass overlapping the
        # finalize pass). Separate from _lock so a queued decode never blocks
        # load()/unload() bookkeeping.
        self._infer_lock = threading.Lock()
        self._idle_timer: threading.Timer | None = None
        self._lifecycle_listener = None

    def is_loaded(self) -> bool:
        return self._model is not None

    def set_lifecycle_listener(self, listener) -> None:
        self._lifecycle_listener = listener

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            started = time.perf_counter()
            try:
                self._model = self._load_model(self._device, self._compute_type)
            except Exception as exc:
                if self._device != "cpu" and looks_like_cuda_failure(exc):
                    self._fallback_reason = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "ASR accelerator unavailable; falling back to CPU int8 (%s)",
                        type(exc).__name__,
                    )
                    self._device = "cpu"
                    self._compute_type = "int8"
                    self._model = self._load_model(self._device, self._compute_type)
                else:
                    raise
            self._last_load_s = time.perf_counter() - started
            listener = getattr(self, "_lifecycle_listener", None)
            if callable(listener):
                with contextlib.suppress(Exception):
                    listener(True, "loaded")
            logger.info(
                "ASR model ready model=%s requested_device=%s runtime=%s/%s load_s=%.3f",
                self.spec.model,
                self._requested_device,
                self._actual_device(),
                self._compute_type,
                self._last_load_s,
            )

    def unload(self) -> None:
        with self._lock:
            self._cancel_idle_timer()
            was_loaded = self._model is not None
            self._model = None
        listener = self._lifecycle_listener
        if was_loaded and callable(listener):
            with contextlib.suppress(Exception):
                listener(False, "idle_unload")

    def _cancel_idle_timer(self) -> None:
        timer = self._idle_timer
        self._idle_timer = None
        if timer is not None:
            timer.cancel()

    def _arm_idle_timer(self) -> None:
        if not self.idle_unload_s or self.idle_unload_s <= 0:
            return
        with self._lock:
            self._cancel_idle_timer()
            self._idle_timer = threading.Timer(self.idle_unload_s, self.unload)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        requested_language = self.validate_language_hint(language)
        # Hold off the idle-unload while we (re)load and decode this utterance.
        # The local `model` reference keeps the weights alive even if an
        # already-fired idle timer nulls self._model while we decode.
        with self._lock:
            self._cancel_idle_timer()
            self.load()
            model = self._model

        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samplerate != 16000:
            samples = resample_to_16k(samples, samplerate)
        try:
            with self._infer_lock:
                try:
                    return self._run_transcription(
                        model,
                        samples,
                        audio,
                        samplerate,
                        initial_prompt,
                        hotwords,
                        requested_language,
                    )
                except Exception as exc:
                    # CUDA/cuDNN/cuBLAS libraries can be missing at inference time
                    # even when the model loaded (e.g. cublas64_12.dll not on
                    # PATH), so the load-time guard never sees it. Degrade to CPU
                    # int8 and retry once.
                    if self._device != "cpu" and looks_like_cuda_failure(exc):
                        with self._lock:
                            self._cancel_idle_timer()
                            self._device = "cpu"
                            self._compute_type = "int8"
                            self._fallback_reason = f"{type(exc).__name__}: {exc}"
                            logger.warning(
                                "ASR inference accelerator failure; retrying on CPU int8 (%s)",
                                type(exc).__name__,
                            )
                            self._model = None
                            self.load()
                            model = self._model
                        return self._run_transcription(
                            model,
                            samples,
                            audio,
                            samplerate,
                            initial_prompt,
                            hotwords,
                            requested_language,
                        )
                    raise
        finally:
            self._arm_idle_timer()

    def _run_transcription(
        self,
        model: Any,
        samples: np.ndarray,
        audio: Any,
        samplerate: int,
        initial_prompt: str | None,
        hotwords: str | None,
        language: str | None,
    ) -> TranscriptResult:
        assert model is not None
        start = time.perf_counter()
        # "auto" / empty → let Whisper detect language (multilingual models).
        effective_language = self.language if language is None else language
        lang = (effective_language or "").strip().lower()
        language_arg = None if lang in {"", "auto", "detect"} else effective_language
        kwargs: dict[str, Any] = {
            "language": language_arg,
            "vad_filter": self.vad_filter,
            "beam_size": self.beam_size,
            "temperature": self.temperature,
            "condition_on_previous_text": self.condition_on_previous_text,
            "compression_ratio_threshold": self.compression_ratio_threshold,
            "log_prob_threshold": self.log_prob_threshold,
            "no_speech_threshold": self.no_speech_threshold,
            "without_timestamps": self.without_timestamps,
            "no_repeat_ngram_size": self.no_repeat_ngram_size,
            "initial_prompt": initial_prompt,
        }
        if hotwords:
            kwargs["hotwords"] = hotwords

        def _decode(options: dict[str, Any]) -> tuple[str, int, Any]:
            segments, info = model.transcribe(samples, **options)
            kept: list[str] = []
            dropped = 0
            for segment in segments:
                text = getattr(segment, "text", "") or ""
                if segment_text_keep(
                    text,
                    no_speech_prob=getattr(segment, "no_speech_prob", None),
                    avg_logprob=getattr(segment, "avg_logprob", None),
                    compression_ratio=getattr(segment, "compression_ratio", None),
                    no_speech_threshold=self.no_speech_threshold,
                    log_prob_threshold=self.log_prob_threshold,
                    compression_ratio_threshold=self.compression_ratio_threshold,
                ):
                    kept.append(text.strip())
                else:
                    dropped += 1
            return " ".join(kept).strip(), dropped, info

        raw_joined, dropped, info = _decode(kwargs)
        duration_s = len(np.asarray(audio).reshape(-1)) / float(samplerate)
        quality = sanitize_transcript(
            raw_joined,
            duration_s=duration_s,
            max_chars_per_s=self.max_chars_per_s,
            hint_text=hotwords,
        )
        if quality.rejected_reason == "asr_hint_echo" and hotwords:
            logger.warning("ASR hotword echo detected; retrying once without hotwords")
            retry_kwargs = dict(kwargs)
            retry_kwargs.pop("hotwords", None)
            raw_joined, retry_dropped, info = _decode(retry_kwargs)
            dropped += retry_dropped
            quality = sanitize_transcript(
                raw_joined,
                duration_s=duration_s,
                max_chars_per_s=self.max_chars_per_s,
                hint_text=initial_prompt,
            )
        latency = time.perf_counter() - start
        self._last_decode_s = latency
        language = getattr(info, "language", None) or effective_language or ""

        if quality.rejected_reason:
            logger.warning(
                "asr quality reject reason=%s chars=%s dur=%.2fs rate=%.1f dropped_segments=%s",
                quality.rejected_reason,
                len(raw_joined),
                duration_s,
                quality.chars_per_s,
                dropped,
            )
        elif quality.collapsed or dropped:
            logger.info(
                "asr quality ok chars=%s dur=%.2fs rate=%.1f collapsed=%s dropped_segments=%s",
                len(quality.text),
                duration_s,
                quality.chars_per_s,
                quality.collapsed,
                dropped,
            )

        return TranscriptResult(
            text=quality.text if not quality.rejected_reason else "",
            language=language,
            duration_s=duration_s,
            asr_latency_s=latency,
            rejected_reason=quality.rejected_reason,
            chars_per_s=quality.chars_per_s,
        )

    def _load_model(self, device: str, compute_type: str) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - environment specific
            raise RuntimeError(
                "faster-whisper is required for local ASR. Install with "
                'python -m pip install -e ".[cuda]".'
            ) from exc
        self._resolved_model = resolve_faster_whisper_model(self.spec.model)
        model_id = canonical_model_id(self.spec.model)
        with verified_snapshot_lock(Path(self._resolved_model), model_id):
            return WhisperModel(
                self._resolved_model,
                device=device,
                compute_type=compute_type,
                # Runtime transcription is strictly offline. Alias resolution has
                # already selected a verified local directory; this is defense in
                # depth against a future resolver regression.
                local_files_only=True,
            )

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def runtime(self) -> tuple[str, str]:
        return self._device, self._compute_type

    def runtime_status(self) -> dict[str, Any]:
        """Diagnostic status for authenticated health/UI surfaces."""
        with self._lock:
            return {
                "ok": True,
                "status": "ready" if self._model is not None else "not_loaded",
                "provider": "faster-whisper",
                "model": self.spec.model,
                "resolved_model": self._resolved_model,
                "requested_device": self._requested_device,
                "actual_device": self._actual_device(),
                "compute_type": self._compute_type,
                "model_loaded": self._model is not None,
                "last_load_s": self._last_load_s,
                "last_decode_s": self._last_decode_s,
                "fallback_reason": self._fallback_reason,
            }

    def _actual_device(self) -> str:
        model = self._model
        engine = getattr(model, "model", None)
        actual = getattr(engine, "device", None) or getattr(model, "device", None)
        return str(actual or self._device)


def resolve_device_compute(spec: ASRSpec) -> tuple[str, str]:
    """Map an ASR compute suffix to a (device, compute_type) pair.

    Unsuffixed / ``auto`` / ``default`` profiles prefer a **working** CUDA stack
    when present, otherwise **CPU int8** immediately. That keeps machines without
    a high-end GPU (or with a GPU but missing CUDA/cuDNN DLLs) on a fast local
    path instead of spending the first dictation on a failed CUDA load.
    """
    compute = (spec.compute_type or "default").strip() or "default"
    if compute.startswith("cuda-"):
        return "cuda", compute.removeprefix("cuda-") or "float16"
    if compute.startswith("cpu-"):
        return "cpu", compute.removeprefix("cpu-") or "int8"
    if compute in {"int8", "int16", "float32"}:
        return "cpu", compute
    if compute in {"default", "auto"}:
        if cuda_runtime_ready():
            # float16 is the usual CTranslate2 CUDA default for Whisper.
            return "cuda", "float16"
        return "cpu", "int8"
    # Unknown explicit suffix: leave to CTranslate2 "auto" device selection.
    return "auto", compute


def cuda_runtime_ready() -> bool:
    """Whether CUDA ASR is likely to load without a multi-second failure.

    Requires at least one CUDA device *and* (on Windows) the cuBLAS + cuDNN 8
    DLLs CTranslate2 needs on PATH. Device count alone is not enough: many PCs
    show an NVIDIA GPU while the Whisper CUDA stack is incomplete.
    """
    try:
        import ctranslate2
    except Exception:
        return False
    try:
        if int(ctranslate2.get_cuda_device_count()) <= 0:
            return False
    except Exception:
        return False
    return not (platform.system() == "Windows" and not _windows_cuda_dlls_present())


def _windows_cuda_dlls_present() -> bool:
    """True when cuBLAS and cuDNN 8 inference DLLs are discoverable on PATH."""
    import os
    from pathlib import Path

    path_dirs = [Path(part) for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    cudnn_names = {
        "cudnn64_8.dll",
        "cudnn_ops_infer64_8.dll",
        "cudnn_cnn_infer64_8.dll",
    }
    found_cudnn = False
    found_cublas = False
    for directory in path_dirs:
        if not directory.is_dir():
            continue
        try:
            # Do not stat every file in large PATH entries such as System32.
            # DirEntry already carries the directory-enumeration metadata; only
            # ask the filesystem about names that could satisfy this probe.
            with os.scandir(directory) as entries:
                for entry in entries:
                    name = entry.name.lower()
                    is_cudnn = name in cudnn_names
                    is_cublas = name.startswith("cublas64_") and name.endswith(".dll")
                    if not (is_cudnn or is_cublas) or not entry.is_file():
                        continue
                    found_cudnn = found_cudnn or is_cudnn
                    found_cublas = found_cublas or is_cublas
                    if found_cudnn and found_cublas:
                        return True
        except OSError:
            continue
    return False


def looks_like_cuda_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("cuda", "cudnn", "cublas", "cudart", "ctranslate2"))


def resample_to_16k(audio: np.ndarray, samplerate: int) -> np.ndarray:
    if samplerate <= 0:
        raise ValueError("samplerate must be positive")
    if audio.size == 0:
        return audio.astype(np.float32)
    duration_s = audio.size / float(samplerate)
    new_len = max(1, int(duration_s * 16000))
    old_x = np.linspace(0.0, duration_s, num=audio.size, endpoint=False)
    new_x = np.linspace(0.0, duration_s, num=new_len, endpoint=False)
    return np.interp(new_x, old_x, audio).astype(np.float32)
