# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Expose the authenticated local ADE service API."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import math
import secrets
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    field_validator,
    model_validator,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dcent_voice import __version__
from dcent_voice.asr.base import (
    ASRProvider,
    UnsupportedLanguageError,
    normalize_language_hint,
)
from dcent_voice.attach.contract import (
    API_VERSION,
    error_payload,
    headless_surface,
    model_loaded,
    probe_hardware,
)
from dcent_voice.commands.router import CommandRouter
from dcent_voice.llm.cleanup import CleanupPipeline
from dcent_voice.privacy import ConsentRequired, PrivacyMonitor
from dcent_voice.util.timing import StageTimer

StatusProvider = Callable[[], dict[str, Any]]


# Bound authenticated payloads so a buggy/hostile local client cannot feed the
# ASR an hour of "audio" or an absurd samplerate and pin the CPU (RT-SEC-1).
# HTTP accepts at most one minute of 16 kHz mono samples. Higher-rate clients
# should resample before upload; the streaming endpoints use much smaller chunks.
MAX_AUDIO_SECONDS = 60
MAX_AUDIO_SAMPLES = 16_000 * MAX_AUDIO_SECONDS
MIN_SAMPLERATE = 8_000
MAX_SAMPLERATE = 48_000
MAX_PROMPT_CHARS = 2_000
MAX_STREAM_CHUNK_SECONDS = 5
MAX_STREAM_CHUNK_SAMPLES = MAX_SAMPLERATE * MAX_STREAM_CHUNK_SECONDS
MAX_FLOAT32_SAMPLE = float(np.finfo(np.float32).max)
MAX_SERVICE_WORKERS = 4
# 960,000 valid JSON float samples can occupy about 16 MB when written in
# scientific notation. Leave practical framing headroom while rejecting payloads
# that can never be a useful one-minute mono request before JSON parsing begins.
MAX_HTTP_BODY_BYTES = 32 * 1024 * 1024
MAX_COMMAND_CHARS = 20_000
MAX_SELECTION_CHARS = 100_000


class AudioValidationError(ValueError):
    """A stream audio frame is malformed or exceeds its allocation budget."""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


class ServiceBusyError(RuntimeError):
    """Raised when all bounded background service-worker slots are occupied."""


class RequestBodyTooLargeError(HTTPException):
    """Raised before JSON parsing when an HTTP request exceeds the body budget."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body exceeds the service limit.")


class RequestBodyLimitMiddleware:
    """Reject over-budget HTTP bodies before FastAPI can buffer or decode them."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            response = JSONResponse(
                status_code=413,
                content=error_payload(
                    413,
                    "Request body exceeds the service limit.",
                    code="payload_too_large",
                    retryable=False,
                ),
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestBodyTooLargeError()
            return message

        await self.app(scope, limited_receive, send)


def _content_length(scope: Scope) -> int | None:
    """Return a non-negative Content-Length when the request declares one."""
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def decode_transcribe_audio(request: TranscribeRequest) -> tuple[np.ndarray, int]:
    """Accept either JSON float samples or a base64 16-bit PCM WAV."""
    if request.wav_b64:
        try:
            payload = base64.b64decode(request.wav_b64, validate=True)
        except Exception as exc:
            raise AudioValidationError("wav_b64 must be valid base64") from exc
        try:
            with wave.open(BytesIO(payload), "rb") as wav:
                channels = wav.getnchannels()
                width = wav.getsampwidth()
                rate = wav.getframerate()
                frames = wav.readframes(wav.getnframes())
        except Exception as exc:
            raise AudioValidationError("wav_b64 must be a 16-bit PCM WAV") from exc
        if width != 2:
            raise AudioValidationError("wav_b64 must be 16-bit PCM")
        if rate < MIN_SAMPLERATE or rate > MAX_SAMPLERATE:
            raise AudioValidationError("wav_b64 samplerate is out of range")
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if len(samples) > rate * MAX_AUDIO_SECONDS:
            raise AudioValidationError(
                f"audio duration must not exceed {MAX_AUDIO_SECONDS} seconds"
            )
        return samples, int(rate)
    return np.asarray(request.audio, dtype=np.float32), int(request.samplerate)
    return None


def validate_audio_payload(
    audio: Any,
    samplerate: Any,
    *,
    max_samples: int = MAX_STREAM_CHUNK_SAMPLES,
    max_seconds: float = MAX_STREAM_CHUNK_SECONDS,
) -> int:
    """Validate a JSON/array audio frame before NumPy allocates or copies it.

    Returns the normalized samplerate. Empty audio is valid for a final marker.
    """

    if isinstance(samplerate, bool) or not isinstance(samplerate, int):
        raise AudioValidationError("samplerate must be an integer")
    rate = samplerate
    if not MIN_SAMPLERATE <= rate <= MAX_SAMPLERATE:
        raise AudioValidationError(
            f"samplerate must be between {MIN_SAMPLERATE} and {MAX_SAMPLERATE}"
        )
    if isinstance(audio, (str, bytes, bytearray, dict)) or audio is None:
        raise AudioValidationError("audio must be an array of finite numbers")
    try:
        count = len(audio)
    except (TypeError, OverflowError) as exc:
        raise AudioValidationError("audio must be a sized array") from exc
    duration_limit = int(rate * max_seconds)
    if count > max_samples or count > duration_limit:
        raise AudioValidationError("audio chunk exceeds the stream limit", too_large=True)
    try:
        iterator = iter(audio)
    except TypeError as exc:
        raise AudioValidationError("audio must be iterable") from exc
    for sample in iterator:
        if isinstance(sample, (bool, str, bytes, bytearray)):
            raise AudioValidationError("audio must contain only finite numbers")
        try:
            value = float(sample)
        except (TypeError, ValueError, OverflowError) as exc:
            raise AudioValidationError("audio must contain only finite numbers") from exc
        if not math.isfinite(value) or abs(value) > MAX_FLOAT32_SAMPLE:
            raise AudioValidationError("audio must contain only finite float32 numbers")
    return rate


class ConnectionLimiter:
    """Small thread-safe admission counter shared by WebSocket endpoints."""

    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("connection limit must be positive")
        self.limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


class TranscribeRequest(BaseModel):
    audio: list[FiniteFloat] = Field(default_factory=list, max_length=MAX_AUDIO_SAMPLES)
    samplerate: int = Field(default=16000, ge=MIN_SAMPLERATE, le=MAX_SAMPLERATE)
    initial_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    language: str | None = Field(default=None, max_length=16)
    wav_b64: str | None = Field(default=None, max_length=MAX_HTTP_BODY_BYTES)
    cleanup: bool = True
    polish: bool = True
    style: str | None = Field(default=None, max_length=32)
    app_context: str | None = Field(default=None, max_length=128)
    prose_context: StrictBool = False
    cleanup_level: str | None = Field(default=None, max_length=16)

    @field_validator("audio")
    @classmethod
    def validate_float32_samples(cls, audio: list[float]) -> list[float]:
        if any(abs(sample) > MAX_FLOAT32_SAMPLE for sample in audio):
            raise ValueError("audio samples must fit within finite float32 range")
        return audio

    @field_validator("cleanup_level")
    @classmethod
    def validate_cleanup_level(cls, value: str | None) -> str | None:
        if value is None:
            return value
        name = value.strip().lower()
        if name not in {"none", "light", "medium", "high"}:
            raise ValueError("cleanup_level must be none, light, medium, or high")
        return name

    @model_validator(mode="after")
    def validate_duration(self) -> TranscribeRequest:
        if self.wav_b64:
            return self
        if not self.audio:
            raise ValueError("audio samples or wav_b64 is required")
        if len(self.audio) > self.samplerate * MAX_AUDIO_SECONDS:
            raise ValueError(f"audio duration must not exceed {MAX_AUDIO_SECONDS} seconds")
        return self


class CommandRequest(BaseModel):
    """A bounded text command and optional selected-text context."""

    transcript: str = Field(max_length=MAX_COMMAND_CHARS)
    selection: str = Field(default="", max_length=MAX_SELECTION_CHARS)


class ComposeRequest(BaseModel):
    """Headless ADE text compose. No audio, tray, or hotkeys."""

    text: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    polish: bool = True
    style: str | None = Field(default=None, max_length=32)
    cleanup_level: str | None = Field(default=None, max_length=16)
    app_context: str | None = Field(default=None, max_length=128)

    @field_validator("cleanup_level")
    @classmethod
    def validate_cleanup_level(cls, value: str | None) -> str | None:
        if value is None:
            return value
        name = value.strip().lower()
        if name not in {"none", "light", "medium", "high"}:
            raise ValueError("cleanup_level must be none, light, medium, or high")
        return name


class LearnRequest(BaseModel):
    """Stateless typed correction. Never includes audio or shared provenance."""

    model_config = ConfigDict(extra="forbid")
    spoken: str = Field(min_length=1, max_length=500)
    written: str = Field(min_length=1, max_length=500)
    style: str | None = Field(default=None, max_length=32)
    app_context: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_non_whitespace_pair(self) -> LearnRequest:
        if not self.spoken.strip() or not self.written.strip():
            raise ValueError("spoken and written must both contain text")
        return self


@dataclass
class ServiceEngine:
    """Coordinates authenticated local ADE API operations."""

    asr: ASRProvider
    cleanup: CleanupPipeline | None = None
    router: CommandRouter | None = None
    privacy: PrivacyMonitor | None = None
    personalization: Any = None
    snippets: tuple[Any, ...] = field(default_factory=tuple)
    dictionary: tuple[Any, ...] = field(default_factory=tuple)
    lock: threading.Lock = field(default_factory=threading.Lock)
    worker_limit: int = MAX_SERVICE_WORKERS
    # Optional subsystem probes for truthful /health (hotkeys, pipeline, …).
    status_providers: dict[str, StatusProvider] = field(default_factory=dict)
    _worker_slots: threading.BoundedSemaphore = field(init=False, repr=False)
    _cancel: threading.Event = field(init=False, repr=False)
    _sticky_transcribe_style: str | None = field(default=None, init=False, repr=False)
    _sticky_transcribe_polish: bool | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.worker_limit <= 0:
            raise ValueError("worker_limit must be positive")
        self._worker_slots = threading.BoundedSemaphore(self.worker_limit)
        self._cancel = threading.Event()
        self._sticky_transcribe_style = None
        self._sticky_transcribe_polish = None

    def cancel(self) -> None:
        """Abort the next one-shot transcribe. Does not require tray or hotkeys."""
        self._cancel.set()

    def compose(self, request: ComposeRequest) -> dict[str, Any]:
        """Compose text through the desktop's local dictation path."""
        from dcent_voice.dictation.postprocess import compose_dictation
        from dcent_voice.dictation.style import normalize_style

        style_name = normalize_style(request.style)
        level = request.cleanup_level or "medium"
        dictionary = tuple(self.dictionary)
        as_vocab = getattr(self.personalization, "as_vocab", None)
        if callable(as_vocab):
            with contextlib.suppress(Exception):
                dictionary = dictionary + tuple(as_vocab(style=style_name, app=request.app_context))
        if request.polish:
            text = compose_dictation(
                request.text,
                style=style_name,
                snippets=self.snippets,
                dictionary=dictionary,
                polish=True,
                cleanup_level=level,
            )
        else:
            text = compose_dictation(
                request.text,
                style=style_name,
                snippets=self.snippets,
                dictionary=dictionary,
                polish=False,
                spoken_edits=False,
                developer_terms=False,
                cleanup_level=level,
            )
        return {"text": text, "style": style_name}

    def try_acquire_worker_slot(self) -> bool:
        """Reserve an ASR/stream worker slot until its underlying thread returns."""
        return self._worker_slots.acquire(blocking=False)

    def release_worker_slot(self) -> None:
        """Release a previously reserved ASR/stream worker slot."""
        self._worker_slots.release()

    def transcribe(self, request: TranscribeRequest) -> dict[str, Any]:
        """Run one-shot ADE transcription with local postprocessing."""
        if self._cancel.is_set():
            self._cancel.clear()
            return {
                "raw": "",
                "cleaned": "",
                "language": "",
                "duration_s": 0.0,
                "asr_latency_s": 0.0,
                "timings": {"cancelled": 0.0},
                "rejected_reason": "cancelled",
            }
        timer = StageTimer()
        samples, samplerate = decode_transcribe_audio(request)
        # Provider language is mutable on current local/cloud adapters. Keep
        # set -> transcribe -> restore within the same serialization boundary
        # so concurrent ADE clients cannot observe each other's request state.
        with self.lock:
            provider = self.asr
            has_language = hasattr(provider, "language")
            previous_language = getattr(provider, "language", None)
            validator = getattr(provider, "validate_language_hint", None)
            requested_language = (
                validator(request.language)
                if callable(validator)
                else normalize_language_hint(request.language)
            )
            per_call_language = bool(getattr(provider, "supports_per_call_language", False))
            if request.language is not None and not per_call_language and not has_language:
                raise UnsupportedLanguageError(
                    f"{type(provider).__name__} cannot honor a language hint"
                )
            language_provider: Any = provider
            try:
                if request.language is not None and not per_call_language and has_language:
                    language_provider.language = requested_language or "auto"
                transcribe_kwargs: dict[str, Any] = {
                    "samplerate": samplerate,
                    "initial_prompt": request.initial_prompt,
                }
                if per_call_language:
                    transcribe_kwargs["language"] = requested_language
                with timer.stage("asr"):
                    result = provider.transcribe(samples, **transcribe_kwargs)
            finally:
                if request.language is not None and not per_call_language and has_language:
                    language_provider.language = previous_language
        # ADE returns ASR output unchanged in `raw`; `cleaned` follows the same
        # local composition path as desktop hold-to-talk, then optional cleanup.
        # Pass polish=false alone for raw STT. With style, compose still runs
        # and honors polish=false (no local filler strip). style/polish set on
        # an earlier oneshot remain for a later call that omits both.
        provided = request.model_fields_set
        if "style" in provided:
            self._sticky_transcribe_style = request.style
        if "polish" in provided:
            self._sticky_transcribe_polish = request.polish
        style = request.style if "style" in provided else self._sticky_transcribe_style
        polish = (
            request.polish
            if "polish" in provided
            else (
                self._sticky_transcribe_polish
                if self._sticky_transcribe_polish is not None
                else request.polish
            )
        )
        style_name = style or "plain"
        cleaned = result.text
        if self.personalization is not None:
            cleaned = self.personalization.apply(
                cleaned,
                style=style_name,
                app=request.app_context,
                prose_context=request.prose_context,
            )
        dictionary = tuple(self.dictionary)
        # Learned corrections have already passed through PersonalizationStore.apply
        # above.  Feeding them through the generic vocabulary pass a second time
        # would bypass its explicit prose_context and literal-safety boundaries.
        # Protect only learned written forms that were actually applied; do not
        # reintroduce declined spoken forms through the generic dictionary pass.
        as_vocab = getattr(self.personalization, "as_vocab", None)
        if callable(as_vocab):
            with contextlib.suppress(Exception):
                learned = as_vocab(style=style_name, app=request.app_context)
                dictionary = dictionary + tuple(
                    entry for entry in learned if entry.written and entry.written in cleaned
                )
        if style or polish:
            from dcent_voice.dictation.postprocess import compose_dictation

            with timer.stage("postprocess"):
                cleaned = compose_dictation(
                    cleaned,
                    style=style_name,
                    snippets=self.snippets,
                    dictionary=dictionary,
                    polish=polish,
                    cleanup_level=request.cleanup_level or "medium",
                )
        if request.cleanup and self.cleanup is not None:
            with timer.stage("cleanup"):
                cleaned = self.cleanup.clean(cleaned, style=style)
        return {
            "raw": result.text,
            "cleaned": cleaned,
            "language": result.language,
            "duration_s": result.duration_s,
            "asr_latency_s": result.asr_latency_s,
            "timings": timer.as_dict(),
        }

    def capabilities(self) -> dict[str, Any]:
        asr_details: Any = self.asr
        spec = getattr(self.asr, "spec", None)
        language_hint = bool(
            getattr(self.asr, "supports_per_call_language", False) or hasattr(self.asr, "language")
        )
        supported_languages = getattr(self.asr, "supported_language_codes", None)
        features = [
            "oneshot",
            "streaming",
            "vocabulary",
            "wav_b64",
            "cancel",
            "learn",
            "personalization_scope",
            "style",
            "polish",
            "prose_context",
            "cleanup_level",
            "compose",
        ]
        if language_hint:
            features.insert(3, "language_hint")
        features.extend(["ready", "hardware_auto"])
        return {
            "name": "dcent-voice",
            "version": __version__,
            **headless_surface(),
            "modes": ["oneshot", "streaming"],
            "provider": getattr(spec, "provider", "unknown"),
            "model": getattr(spec, "model", ""),
            "hardware": probe_hardware(self.asr),
            "features": features,
            "language_hint": {
                "supported": language_hint,
                "codes": (
                    sorted(supported_languages) if supported_languages is not None else "iso-639-1"
                ),
                "auto": bool(getattr(self.asr, "supports_language_auto_detection", False)),
                **(
                    {
                        "effect": asr_details.language_hint_effect,
                        "reports_detected_language": bool(
                            getattr(self.asr, "reports_detected_language", False)
                        ),
                    }
                    if getattr(asr_details, "language_hint_effect", None) is not None
                    else {}
                ),
            },
            "privacy": self.privacy.snapshot() if self.privacy is not None else None,
        }

    def command(self, request: CommandRequest) -> dict[str, Any]:
        router = self.router or CommandRouter()
        intent = router.route(request.transcript, request.selection)
        return intent.model_dump()

    def learn(self, request: LearnRequest) -> dict[str, Any]:
        """Store an app-scoped typed correction without audio."""
        store = self.personalization
        if store is None:
            raise RuntimeError("personalization is not available")
        term = store.record_correction(
            request.spoken,
            request.written,
            source="typed",
            style=request.style,
            app=request.app_context,
        )
        return {
            "ok": term is not None,
            "term": None
            if term is None
            else {
                "spoken": term.spoken,
                "written": term.written,
                "count": term.count,
                "source": term.source,
                "style": term.style,
                "app": term.app,
            },
            "snapshot": store.snapshot(),
        }

    def health(self) -> dict[str, Any]:
        subsystems: dict[str, Any] = {"service": {"ok": True}}
        critical_ok = True
        for name, provider in self.status_providers.items():
            try:
                snapshot = provider()
            except Exception as exc:  # pragma: no cover - defensive
                snapshot = {"ok": False, "status": "error", "detail": type(exc).__name__}
            subsystems[name] = snapshot
            if snapshot.get("critical") and not snapshot.get("ok", True):
                critical_ok = False
            # Hotkeys / pipeline without explicit critical flag still fail overall ok
            # when they report status "dead" or ok False.
            status = snapshot.get("status")
            if snapshot.get("enabled") and status == "dead":
                critical_ok = False
            if snapshot.get("ok") is False and name in {"hotkeys", "pipeline"}:
                critical_ok = False
        return {
            "ok": critical_ok,
            "ready": True,
            "model_loaded": model_loaded(self.asr),
            "api_version": API_VERSION,
            "requires_tray": False,
            "requires_hotkeys": False,
            "cancelled": self._cancel.is_set(),
            "hardware": probe_hardware(self.asr),
            "privacy": self.privacy.snapshot() if self.privacy is not None else None,
            "subsystems": subsystems,
        }

    def readiness(self) -> dict[str, Any]:
        """Headless STT readiness. Dead hotkeys/tray do not block attach."""
        snapshot = self.health()
        return {
            "ok": True,
            "ready": True,
            "model_loaded": bool(snapshot.get("model_loaded")),
            "api_version": API_VERSION,
            "requires_tray": False,
            "requires_hotkeys": False,
            "cancelled": bool(snapshot.get("cancelled")),
            "hardware": snapshot.get("hardware"),
            "privacy": snapshot.get("privacy"),
            "desktop_ok": bool(snapshot.get("ok")),
        }


async def run_bounded_worker(
    engine: ServiceEngine, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run service work in a bounded thread slot that survives coroutine cancellation."""
    if not engine.try_acquire_worker_slot():
        raise ServiceBusyError("service worker capacity is full")

    invocation = functools.partial(fn, *args, **kwargs)
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(None, invocation)
    except BaseException:
        engine.release_worker_slot()
        raise

    # ``shield`` keeps the executor future alive if a WebSocket disconnect
    # cancels its coroutine before the worker starts. The completion callback
    # then releases the slot exactly when the actual background work is done.
    future.add_done_callback(lambda _future: engine.release_worker_slot())
    return await asyncio.shield(future)


def create_app(
    engine: ServiceEngine,
    token: str | None = None,
    *,
    max_body_bytes: int = MAX_HTTP_BODY_BYTES,
) -> FastAPI:
    # No interactive docs on a token-secured local voice service: /docs,
    # /redoc, and /openapi.json would enumerate the API to any local process
    # without auth (RT-SEC-14).
    app = FastAPI(
        title="DCENT_Voice",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # This must be outside FastAPI's request parsing. Model limits validate
    # after the framework has received the complete JSON payload, whereas this
    # middleware covers both declared Content-Length and chunked requests.
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=max_body_bytes)

    @app.exception_handler(RequestBodyTooLargeError)
    async def request_body_too_large(
        _request: Request, _exc: RequestBodyTooLargeError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content=error_payload(
                413,
                "Request body exceeds the service limit.",
                code="payload_too_large",
                retryable=False,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI includes the rejected value in its default detail. A JSON NaN
        # is accepted by Python's parser but cannot be emitted by the strict JSON
        # response encoder, turning a correct finite-number rejection into a 500.
        detail = [
            {
                "type": error.get("type", "value_error"),
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", "Invalid request"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_payload(422, detail, code="invalid_request", retryable=False),
        )

    @app.exception_handler(HTTPException)
    async def structured_http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        code = getattr(exc, "error_code", None)
        retryable = getattr(exc, "retryable", None)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                exc.status_code,
                exc.detail,
                code=code,
                retryable=retryable,
            ),
        )

    def _authorized(authorization: str | None) -> bool:
        if token is None:
            return True
        expected = f"Bearer {token}"
        # compare_digest raises if lengths differ; treat that as unauthorized.
        if authorization is None or len(authorization) != len(expected):
            return False
        return secrets.compare_digest(authorization, expected)

    def _http_error(
        status: int,
        detail: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> HTTPException:
        exc = HTTPException(status_code=status, detail=detail)
        structured_exc: Any = exc
        if code is not None:
            structured_exc.error_code = code
        if retryable is not None:
            structured_exc.retryable = retryable
        return exc

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        # When a session token is configured, transcript/command endpoints require
        # `Authorization: Bearer <token>` so a random local process (or a web page
        # hitting 127.0.0.1) can't drive dictation or read voice content.
        if not _authorized(authorization):
            raise _http_error(401, "Unauthorized", code="unauthorized", retryable=False)

    @app.get("/health")
    async def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        full = engine.health()
        if _authorized(authorization):
            return full
        # Unauthenticated callers (including the startup identity probe) get
        # liveness only — no privacy posture or subsystem detail for arbitrary
        # local processes (RT-SEC-4).
        return {"ok": bool(full.get("ok")), "subsystems": {"service": {"ok": True}}}

    @app.get("/capabilities")
    async def capabilities(_: None = Depends(require_token)) -> dict[str, Any]:
        return engine.capabilities()

    @app.get("/ready")
    async def ready(_: None = Depends(require_token)) -> dict[str, Any]:
        return engine.readiness()

    @app.post("/cancel")
    async def cancel(_: None = Depends(require_token)) -> dict[str, Any]:
        engine.cancel()
        return {"ok": True, "cancelled": True, "api_version": API_VERSION}

    @app.post("/transcribe")
    async def transcribe(
        request: TranscribeRequest, _: None = Depends(require_token)
    ) -> dict[str, Any]:
        try:
            return await run_bounded_worker(engine, engine.transcribe, request)
        except AudioValidationError as exc:
            code = "payload_too_large" if exc.too_large else "invalid_audio"
            status = 413 if exc.too_large else 422
            raise _http_error(status, str(exc), code=code, retryable=False) from exc
        except UnsupportedLanguageError as exc:
            raise _http_error(422, str(exc), code="unsupported_language", retryable=False) from exc
        except ConsentRequired as exc:
            # Consent is checked again by the cloud provider immediately before
            # egress. A grant can therefore be revoked (or its ledger corrupted)
            # after the service was built; expose that policy result rather than
            # leaking it through FastAPI as an internal-server error.
            raise _http_error(
                403,
                str(exc),
                code="consent_required",
                retryable=False,
            ) from exc
        except ServiceBusyError as exc:
            raise _http_error(
                503,
                "Speech service is busy; retry shortly.",
                code="busy",
                retryable=True,
            ) from exc

    @app.post("/command")
    async def command(request: CommandRequest, _: None = Depends(require_token)) -> dict[str, Any]:
        return engine.command(request)

    @app.post("/compose")
    async def compose(request: ComposeRequest, _: None = Depends(require_token)) -> dict[str, Any]:
        try:
            return engine.compose(request)
        except ValueError as exc:
            raise _http_error(422, str(exc), code="invalid_request", retryable=False) from exc

    @app.get("/personalization")
    async def personalization(_: None = Depends(require_token)) -> dict[str, Any]:
        store = engine.personalization
        if store is None:
            return {"enabled": False, "stores_audio": False, "terms": []}
        return store.snapshot()

    @app.post("/learn")
    async def learn(request: LearnRequest, _: None = Depends(require_token)) -> dict[str, Any]:
        try:
            return engine.learn(request)
        except TimeoutError as exc:
            raise _http_error(503, str(exc), code="unavailable", retryable=True) from exc
        except ValueError as exc:
            raise _http_error(422, str(exc), code="invalid_request", retryable=False) from exc
        except RuntimeError as exc:
            raise _http_error(503, str(exc), code="unavailable", retryable=True) from exc

    return app
