# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Headless speech engine. No tray, overlay, or global hotkeys required."""

from __future__ import annotations

import threading
import wave
from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from dcent_voice import __version__
from dcent_voice.asr.base import (
    ASRProvider,
    Locality,
    UnsupportedLanguageError,
    normalize_language_hint,
)
from dcent_voice.asr.factory import build_asr_provider, describe_active_asr
from dcent_voice.asr.language import LanguagePolicy, resolve_language_policy
from dcent_voice.attach.contract import (
    API_VERSION,
    headless_surface,
    model_loaded,
    probe_hardware,
)
from dcent_voice.audio.vad import EnergyVAD
from dcent_voice.config import AppConfig, VocabEntry, effective_snippets, load_config
from dcent_voice.dictation.postprocess import (
    compose_dictation,
    extract_last_correction,
    extract_spoken_corrections,
)
from dcent_voice.dictation.style import normalize_style, resolve_style
from dcent_voice.personalization import (
    PersonalizationStore,
    default_personalization_path,
    infer_correction_pair,
)
from dcent_voice.pipeline import (
    apply_dictionary,
    build_hotwords,
    build_initial_prompt,
    merge_asr_hint_dictionary,
    postprocess_dictionary,
)
from dcent_voice.privacy import PrivacyMonitor
from dcent_voice.util.owned_process import start_owned_process, terminate_owned_process
from dcent_voice.util.timing import StageTimer


@dataclass(frozen=True)
class EngineResult:
    """One-shot transcription result with honest stage timings."""

    text: str
    raw: str
    language: str
    duration_s: float
    asr_latency_s: float
    timings: dict[str, float]
    provider: str
    model: str
    language_mode: str
    rejected_reason: str = ""
    corrections: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "raw": self.raw,
            "language": self.language,
            "duration_s": self.duration_s,
            "asr_latency_s": self.asr_latency_s,
            "timings": dict(self.timings),
            "provider": self.provider,
            "model": self.model,
            "language_mode": self.language_mode,
            "rejected_reason": self.rejected_reason,
            "corrections": [list(pair) for pair in self.corrections],
        }


@dataclass(frozen=True)
class StreamEvent:
    """One incremental headless stream update. No tray or hotkeys required."""

    type: str
    text: str
    raw: str = ""
    committed: str = ""
    partial: str = ""
    speech: bool = False
    rejected_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "raw": self.raw,
            "committed": self.committed,
            "partial": self.partial,
            "speech": self.speech,
            "rejected_reason": self.rejected_reason,
        }


@dataclass
class EngineStreamSession:
    """Accumulate audio chunks and emit partial and final engine events."""

    engine: VoiceEngine
    samplerate: int = 16000
    partial_interval_s: float = 0.5
    language: str | None = None
    vocabulary: Any = None
    polish: bool | None = None
    style: str | None = None
    app_context: str | None = None
    prose_context: bool | None = None
    vad: EnergyVAD = field(default_factory=EnergyVAD)
    committed: str = ""
    last_partial: str = ""
    last_raw: str = ""
    _buffer: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _last_partial_samples: int = 0

    def push(
        self,
        audio: Any,
        *,
        final: bool = False,
        samplerate: int | None = None,
        style: str | None = None,
        polish: bool | None = None,
        app_context: str | None = None,
    ) -> StreamEvent:
        # style/polish set on an earlier push remain for later final=True.
        controls_changed = style is not None or polish is not None or app_context is not None
        if style is not None:
            self.style = style
        if polish is not None:
            self.polish = polish
        if app_context is not None:
            self.app_context = app_context
        if self.engine._cancel.is_set():
            self.engine._cancel.clear()
            return StreamEvent(type="cancelled", text="", rejected_reason="cancelled")
        rate = int(samplerate or self.samplerate)
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size:
            self._buffer = np.concatenate((self._buffer, samples))
        speech = bool(self.vad.is_speech(samples if samples.size else self._buffer, rate).speech)
        if not speech and not final:
            return StreamEvent(
                type="silence",
                text=self.committed,
                committed=self.committed,
                partial=self.last_partial,
                speech=False,
            )
        if not self._buffer.size and final:
            return StreamEvent(type="final", text=self.committed, committed=self.committed)
        partial_stride = int(max(0.0, float(self.partial_interval_s)) * rate)
        if (
            not final
            and not controls_changed
            and self._last_partial_samples
            and partial_stride
            and self._buffer.size - self._last_partial_samples < partial_stride
        ):
            # Preserve the latest honest hypothesis while enough new audio
            # accumulates for the next decode. Parakeet is not stateful: asking
            # it to re-run the same zero-padded 3 s window every 250 ms doubles
            # CPU cost without improving first-partial or final latency.
            return StreamEvent(
                type="partial",
                text=self.committed or self.last_partial,
                raw=self.last_raw,
                committed=self.committed,
                partial=self.last_partial,
                speech=True,
            )
        result = self.engine.transcribe(
            self._buffer,
            samplerate=rate,
            language=self.language,
            vocabulary=self.vocabulary,
            polish=self.polish,
            style=self.style,
            app_context=self.app_context,
            prose_context=self.prose_context,
        )
        if result.rejected_reason == "cancelled":
            return StreamEvent(type="cancelled", text="", rejected_reason="cancelled")
        text = result.text
        raw = result.raw
        if final:
            self._buffer = np.zeros(0, dtype=np.float32)
            self._last_partial_samples = 0
            self.committed = ""
            self.last_partial = ""
            self.last_raw = ""
            return StreamEvent(
                type="final",
                text=text,
                raw=raw,
                committed=text,
                partial=raw,
                speech=speech,
            )
        self._last_partial_samples = int(self._buffer.size)
        self.last_raw = raw
        self.last_partial = raw or text
        if len(text) > len(self.committed):
            self.committed = text
        return StreamEvent(
            type="partial",
            text=self.committed or text,
            raw=raw,
            committed=self.committed,
            partial=self.last_partial,
            speech=speech,
        )


@dataclass(frozen=True)
class _EngineUtterance:
    generation: int
    raw: str
    cleaned: str
    style: str
    app: str


class VoiceEngine:
    """Embeddable local (or opt-in cloud) transcription engine.

    This is the surface ADE and other D-Central projects should use. It does not
    start a tray icon, overlay, or hotkey hook.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        asr: ASRProvider | None = None,
        personalization: PersonalizationStore | None = None,
        privacy: PrivacyMonitor | None = None,
        polish: bool = True,
    ) -> None:
        self.config = config if config is not None else load_config()
        self.privacy = privacy or PrivacyMonitor.from_config(self.config)
        if asr is None and self.config.current_profile.asr.locality is Locality.CLOUD:
            # Headless/CLI construction is a first-class runtime path. It must
            # enforce the same consent boundary as desktop startup before an
            # environment credential can instantiate a cloud provider.
            self.privacy.validate_cloud_consent()
        self.policy: LanguagePolicy = resolve_language_policy(
            getattr(self.config, "language_mode", None),
            self.config.current_profile.language or self.config.language,
        )
        enabled, learn, _prose_context = self._configured_personalization_policy()
        self.personalization = personalization
        if self.personalization is None:
            self.personalization = PersonalizationStore(
                enabled=enabled,
                learn=learn,
            )
        self.polish = polish
        self._asr = asr
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._utterance_lock = threading.RLock()
        self._utterance_generation = 0
        self._last_engine_utterance: _EngineUtterance | None = None
        self._consumed_utterance_generation = 0
        self._claimed_utterance_generations: set[int] = set()
        self._sticky_transcribe_style: str | None = None
        self._sticky_transcribe_polish: bool | None = None

    @classmethod
    def from_config(cls, path: Path | str | None = None, **kwargs: Any) -> VoiceEngine:
        return cls(load_config(path) if path is not None else load_config(), **kwargs)

    @property
    def asr(self) -> ASRProvider:
        with self._lock:
            if self._asr is None:
                profile = self.config.current_profile
                if profile.asr.locality is Locality.CLOUD:
                    self.privacy.validate_cloud_consent()
                self._asr = build_asr_provider(
                    profile.asr,
                    language=profile.language or self.config.language,
                    language_mode=getattr(self.config, "language_mode", None),
                    egress_logger=(
                        self._record_egress if profile.asr.locality is Locality.CLOUD else None
                    ),
                    policy=self.policy,
                )
            return self._asr

    def _record_egress(self, provider_key: str, payload_type: str, byte_count: int) -> None:
        """Validate live consent, then persist metadata only before egress."""

        self.privacy.record_egress(
            provider_key,
            payload_type=payload_type,
            byte_count=byte_count,
        )

    def load(self) -> None:
        self.asr.load()

    def unload(self) -> None:
        with self._lock:
            if self._asr is not None:
                self._asr.unload()

    def cancel(self) -> None:
        self._cancel.set()

    def capabilities(self) -> dict[str, Any]:
        profile = self.config.current_profile
        asr_info = describe_active_asr(
            self.asr,
            self.policy,
            requested=profile.asr.raw,
        )
        return {
            "name": "dcent-voice",
            "version": __version__,
            **headless_surface(),
            "modes": ["oneshot", "streaming"],
            "local_default": bool(asr_info["local"]),
            "language_mode": self.policy.mode,
            "language_label": self.policy.label,
            "asr": asr_info,
            "hardware": probe_hardware(self.asr),
            "features": [
                "oneshot",
                "streaming",
                "cancel",
                "vocabulary",
                "language_hint",
                "personalization",
                "personalization_scope",
                "offline_polish",
                "learn",
                "style",
                "compose",
                "ready",
                "hardware_auto",
            ],
            "privacy": {
                "stores_audio": False,
                "personalization_local": True,
                "cloud_required": False,
            },
        }

    def health(self) -> dict[str, Any]:
        asr = self.asr
        loaded = model_loaded(asr)
        return {
            "ok": True,
            "ready": loaded,
            "model_loaded": loaded,
            "api_version": API_VERSION,
            "requires_tray": False,
            "requires_hotkeys": False,
            "provider": getattr(getattr(asr, "spec", None), "provider", "unknown"),
            "model": getattr(getattr(asr, "spec", None), "model", ""),
            "language_mode": self.policy.mode,
            "cancelled": self._cancel.is_set(),
            "hardware": probe_hardware(asr),
            "privacy": {
                "stores_audio": False,
                "personalization_local": True,
                "cloud_required": False,
            },
        }

    def ready(self) -> dict[str, Any]:
        return self.health()

    def open_stream(self, samplerate: int = 16000, **kwargs: Any) -> EngineStreamSession:
        """Open a headless stream session with optional dictation controls."""
        return EngineStreamSession(self, samplerate=samplerate, **kwargs)

    def transcribe_stream(
        self,
        chunks: Iterable[Any],
        samplerate: int = 16000,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield incremental headless results from audio chunks."""
        session = self.open_stream(samplerate=samplerate, **kwargs)
        iterator = iter(chunks)
        try:
            chunk = next(iterator)
        except StopIteration:
            yield session.push(np.zeros(0, dtype=np.float32), final=True).to_dict()
            return
        while True:
            try:
                nxt = next(iterator)
            except StopIteration:
                yield session.push(chunk, final=True, samplerate=samplerate).to_dict()
                return
            yield session.push(chunk, final=False, samplerate=samplerate).to_dict()
            chunk = nxt

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        *,
        language: str | None = None,
        vocabulary: tuple[VocabEntry, ...] | None = None,
        polish: bool | None = None,
        style: str | None = None,
        app_context: str | None = None,
        prose_context: bool | None = None,
    ) -> EngineResult:
        """Transcribe audio without requiring the desktop UI or hotkeys."""
        if prose_context is not None and type(prose_context) is not bool:
            raise TypeError("prose_context must be a boolean or None")
        enabled, learn, configured_prose_context = self._personalization_policy()
        use_prose_context = configured_prose_context if prose_context is None else prose_context

        if self._cancel.is_set():
            self._cancel.clear()
            return EngineResult(
                text="",
                raw="",
                language=self.policy.whisper_language or "",
                duration_s=0.0,
                asr_latency_s=0.0,
                timings={"cancelled": 0.0},
                provider=self.config.current_profile.asr.provider,
                model=self.config.current_profile.asr.model,
                language_mode=self.policy.mode,
                rejected_reason="cancelled",
            )

        generation, prior_utterance = self._begin_transcription()

        timer = StageTimer()
        style_cfg = getattr(self.config, "style", None)
        learned_styles = (
            self.personalization.learned_app_styles()
            if enabled and self.personalization is not None
            else {}
        )
        if style is not None:
            self._sticky_transcribe_style = style
        if polish is not None:
            self._sticky_transcribe_polish = polish
        # style/polish set on an earlier call remain for a later call that omits both
        # (VoiceEngine.from_config / attach_engine default polish=True).
        style_name = (
            style
            or self._sticky_transcribe_style
            or resolve_style(
                style_cfg.default if style_cfg is not None else "plain",
                app_context,
                style_cfg.per_app if style_cfg is not None else None,
                learned_per_app=learned_styles,
            )
        )
        dictionary = self._merged_dictionary(
            vocabulary,
            style=style_name,
            app=app_context,
            personalization_enabled=enabled,
        )
        use_polish = (
            polish
            if polish is not None
            else (
                self._sticky_transcribe_polish
                if self._sticky_transcribe_polish is not None
                else self.polish
            )
        )
        asr = self.asr
        validator = getattr(asr, "validate_language_hint", None)
        requested_language = (
            validator(language) if callable(validator) else normalize_language_hint(language)
        )
        per_call_language = bool(getattr(asr, "supports_per_call_language", False))
        has_mutable_language = hasattr(asr, "language")
        if language is not None and not per_call_language and not has_mutable_language:
            raise UnsupportedLanguageError(f"{type(asr).__name__} cannot honor a language hint")

        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        duration_s = len(samples) / float(samplerate or 16000)

        transcribe_kwargs: dict[str, Any] = {
            "samplerate": samplerate,
            "initial_prompt": build_initial_prompt(
                dictionary, effective_snippets(self.config.snippets)
            ),
            "hotwords": build_hotwords(dictionary, effective_snippets(self.config.snippets)),
        }
        if per_call_language:
            transcribe_kwargs["language"] = requested_language
        with timer.stage("asr"):
            if language is not None and not per_call_language:
                with self._lock:
                    language_provider: Any = asr
                    previous_language = language_provider.language
                    try:
                        language_provider.language = requested_language or "auto"
                        result = asr.transcribe(samples, **transcribe_kwargs)
                    finally:
                        language_provider.language = previous_language
            else:
                result = asr.transcribe(samples, **transcribe_kwargs)
        raw = result.text or ""
        rejected = getattr(result, "rejected_reason", "") or ""
        if rejected:
            return EngineResult(
                text="",
                raw=raw,
                language=result.language,
                duration_s=duration_s,
                asr_latency_s=result.asr_latency_s,
                timings=timer.as_dict(),
                provider=self.config.current_profile.asr.provider,
                model=getattr(asr, "spec", self.config.current_profile.asr).model,
                language_mode=self.policy.mode,
                rejected_reason=rejected,
            )

        corrections = extract_spoken_corrections(raw) if self.config.dictation.spoken_edits else ()
        with timer.stage("postprocess"):
            text = raw
            if enabled and self.personalization is not None:
                text = self.personalization.apply(
                    text,
                    style=style_name,
                    app=app_context,
                    prose_context=use_prose_context,
                    policy_enabled=enabled,
                )
            post_dictionary = self._post_dictionary(
                vocabulary,
                style=style_name,
                app=app_context,
                personalized_text=text if enabled else None,
                personalization_enabled=enabled,
            )
            text = apply_dictionary(text, post_dictionary)
            last_fix = extract_last_correction(text)
            if last_fix and enabled and learn and self.personalization is not None:
                inferred = self._claim_correction_source(last_fix, prior_utterance)
                if inferred is not None:
                    claim, pair, correction_style, correction_app = inferred
                    try:
                        term = self.personalization.record_correction(
                            pair[0],
                            pair[1],
                            source="spoken_last",
                            style=correction_style,
                            app=correction_app,
                            policy_enabled=enabled,
                            policy_learn=learn,
                        )
                    except Exception:
                        self._finish_correction_claim(claim, success=False)
                        raise
                    self._finish_correction_claim(claim, success=term is not None)
                text = last_fix
            if use_polish:
                text = compose_dictation(
                    text,
                    style=style_name,
                    snippets=effective_snippets(self.config.snippets),
                    dictionary=post_dictionary,
                    polish=self.config.dictation.local_polish,
                    spoken_edits=self.config.dictation.spoken_edits,
                    developer_terms=self.config.dictation.developer_terms,
                    cleanup_level=self.config.dictation.cleanup_level,
                )
            else:
                text = compose_dictation(
                    text,
                    style=style_name,
                    snippets=effective_snippets(self.config.snippets),
                    dictionary=post_dictionary,
                    polish=False,
                    spoken_edits=False,
                    developer_terms=False,
                )
        if enabled and learn and self.personalization is not None and (raw.strip() or text.strip()):
            self._commit_utterance(
                generation,
                raw=raw,
                cleaned=text,
                style=style_name,
                app=app_context,
            )
        if corrections and enabled and learn and self.personalization is not None:
            self.personalization.record_pairs(
                corrections,
                style=style_name,
                app=app_context,
                policy_enabled=enabled,
                policy_learn=learn,
            )

        return EngineResult(
            text=text,
            raw=raw,
            language=result.language,
            duration_s=duration_s,
            asr_latency_s=result.asr_latency_s,
            timings=timer.as_dict(),
            provider=getattr(
                getattr(asr, "spec", None),
                "provider",
                self.config.current_profile.asr.provider,
            ),
            model=getattr(
                getattr(asr, "spec", None),
                "model",
                self.config.current_profile.asr.model,
            ),
            language_mode=self.policy.mode,
            corrections=corrections,
        )

    def learn(
        self,
        spoken: str,
        written: str,
        *,
        source: str = "typed",
        style: str | None = None,
        app_context: str | None = None,
    ) -> dict[str, Any]:
        """Record an app-scoped typed correction without audio."""
        if self.personalization is None:
            raise RuntimeError("personalization is not available")
        enabled, learn, _prose_context = self._personalization_policy()
        term = None
        if enabled and learn:
            term = self.personalization.record_correction(
                spoken,
                written,
                source=source,
                style=style,
                app=app_context,
                policy_enabled=enabled,
                policy_learn=learn,
            )
        return self.personalization.snapshot() | {
            "enabled": enabled,
            "learn": learn,
            "ok": term is not None,
            "spoken": None if term is None else term.spoken,
            "written": None if term is None else term.written,
        }

    def learn_last(
        self,
        correction: str,
        *,
        source: str = "typed",
        style: str | None = None,
        app_context: str | None = None,
    ) -> dict[str, Any]:
        if self.personalization is None:
            raise RuntimeError("personalization is not available")
        enabled, learn, _prose_context = self._personalization_policy()
        term = None
        if enabled and learn:
            inferred = self._claim_correction_source(correction)
            if inferred is not None:
                claim, pair, remembered_style, remembered_app = inferred
                try:
                    term = self.personalization.record_correction(
                        pair[0],
                        pair[1],
                        source=source,
                        style=style or remembered_style,
                        app=app_context or remembered_app,
                        policy_enabled=enabled,
                        policy_learn=learn,
                    )
                except Exception:
                    self._finish_correction_claim(claim, success=False)
                    raise
                self._finish_correction_claim(claim, success=term is not None)
        return self.personalization.snapshot() | {
            "enabled": enabled,
            "learn": learn,
            "ok": term is not None,
            "spoken": None if term is None else term.spoken,
            "written": None if term is None else term.written,
        }

    def remember_app_style(self, app: str, style: str) -> dict[str, Any]:
        if self.personalization is None:
            raise RuntimeError("personalization is not available")
        enabled, learn, _prose_context = self._personalization_policy()
        item = None
        if enabled and learn:
            item = self.personalization.remember_app_style(
                app,
                style,
                source="typed",
                immediate=True,
                policy_enabled=enabled,
                policy_learn=learn,
            )
        return self.personalization.snapshot() | {
            "enabled": enabled,
            "learn": learn,
            "ok": item is not None,
            "app": None if item is None else item.app,
            "style": None if item is None else item.style,
        }

    def compose(
        self,
        text: str,
        *,
        style: str | None = None,
        polish: bool | None = None,
        cleanup_level: str | None = None,
        app_context: str | None = None,
    ) -> str:
        """Apply the same local dictation composition used by the desktop."""
        style_name = normalize_style(style)
        use_polish = self.polish if polish is None else polish
        dictation = getattr(self.config, "dictation", None)
        level = cleanup_level or (dictation.cleanup_level if dictation is not None else "medium")
        snippets = effective_snippets(self.config.snippets)
        dictionary = tuple(self.config.dictionary)
        as_vocab = getattr(self.personalization, "as_vocab", None)
        enabled, _learn, _prose_context = self._personalization_policy()
        if enabled and callable(as_vocab):
            with suppress(Exception):
                dictionary = dictionary + tuple(
                    as_vocab(
                        style=style_name,
                        app=app_context,
                        policy_enabled=enabled,
                    )
                )
        if use_polish:
            return compose_dictation(
                text,
                style=style_name,
                snippets=snippets,
                dictionary=dictionary,
                polish=True if dictation is None else dictation.local_polish,
                spoken_edits=True if dictation is None else dictation.spoken_edits,
                developer_terms=True if dictation is None else dictation.developer_terms,
                cleanup_level=level,
            )
        return compose_dictation(
            text,
            style=style_name,
            snippets=snippets,
            dictionary=dictionary,
            polish=False,
            spoken_edits=False,
            developer_terms=False,
            cleanup_level=level,
        )

    def transcribe_file(self, path: Path | str, **kwargs: Any) -> EngineResult:
        """Transcribe a WAV with the configured headless pipeline."""
        audio, samplerate = load_wav_mono(Path(path))
        return self.transcribe(audio, samplerate=samplerate, **kwargs)

    def _configured_personalization_policy(self) -> tuple[bool, bool, bool]:
        policy = getattr(self.config, "personalization", None)
        values = (
            ("enabled", True if policy is None else policy.enabled),
            ("learn", True if policy is None else policy.learn),
            ("prose_context", False if policy is None else policy.prose_context),
        )
        for name, value in values:
            if type(value) is not bool:
                raise TypeError(f"configured personalization.{name} must be a boolean")
        return values[0][1], values[1][1], values[2][1]

    def _personalization_policy(self) -> tuple[bool, bool, bool]:
        return self._configured_personalization_policy()

    def _begin_transcription(self) -> tuple[int, _EngineUtterance | None]:
        with self._utterance_lock:
            self._utterance_generation += 1
            prior = self._last_engine_utterance
            if prior is not None and prior.generation <= self._consumed_utterance_generation:
                prior = None
            return self._utterance_generation, prior

    def _commit_utterance(
        self,
        generation: int,
        *,
        raw: str,
        cleaned: str,
        style: str,
        app: str | None,
    ) -> None:
        utterance = _EngineUtterance(
            generation=generation,
            raw=(raw or "").strip(),
            cleaned=(cleaned or "").strip(),
            style=(style or "").strip(),
            app=(app or "").strip(),
        )
        with self._utterance_lock:
            current = self._last_engine_utterance
            if current is None or generation > current.generation:
                self._last_engine_utterance = utterance

    def _claim_correction_source(
        self,
        correction: str,
        candidate: _EngineUtterance | None = None,
    ) -> tuple[int, tuple[str, str], str, str] | None:
        with self._utterance_lock:
            utterance = candidate or self._last_engine_utterance
            if (
                utterance is None
                or utterance.generation <= self._consumed_utterance_generation
                or utterance.generation in self._claimed_utterance_generations
            ):
                return None
            pair = infer_correction_pair(
                utterance.cleaned or utterance.raw,
                correction,
            )
            if pair is None:
                return None
            self._claimed_utterance_generations.add(utterance.generation)
            return utterance.generation, pair, utterance.style, utterance.app

    def _finish_correction_claim(self, generation: int, *, success: bool) -> None:
        with self._utterance_lock:
            self._claimed_utterance_generations.discard(generation)
            if success:
                self._consumed_utterance_generation = max(
                    self._consumed_utterance_generation, generation
                )

    def _merged_dictionary(
        self,
        extra: tuple[VocabEntry, ...] | None,
        *,
        style: str | None = None,
        app: str | None = None,
        personalization_enabled: bool = True,
    ) -> tuple[VocabEntry, ...]:
        items = list(self.config.dictionary)
        if extra:
            items.extend(extra)
        return merge_asr_hint_dictionary(
            tuple(items),
            asr=self.asr,
            personalization=self.personalization if personalization_enabled else None,
            style=style,
            app=app,
        )

    def _post_dictionary(
        self,
        extra: tuple[VocabEntry, ...] | None,
        *,
        style: str | None = None,
        app: str | None = None,
        personalized_text: str | None = None,
        personalization_enabled: bool = True,
    ) -> tuple[VocabEntry, ...]:
        items = list(self.config.dictionary)
        if extra:
            items.extend(extra)
        as_vocab = getattr(self.personalization, "as_vocab", None)
        if personalization_enabled and personalized_text is not None and callable(as_vocab):
            with suppress(Exception):
                learned = as_vocab(
                    style=style,
                    app=app,
                    policy_enabled=personalization_enabled,
                )
                items.extend(
                    entry
                    for entry in learned
                    if entry.written and entry.written in personalized_text
                )
        return postprocess_dictionary(tuple(items))


@dataclass(frozen=True)
class FirstDictationScore:
    """Load-then-first-transcribe timings on the shipped default."""

    text: str
    load_s: float
    transcribe_s: float
    asr_s: float
    wer: float
    model_loaded_before: bool
    model_loaded_after: bool
    provider: str
    model: str
    kind: str = "first_dictation"


def score_shipped_default_first_dictation(
    engine: VoiceEngine,
    path: Path | str,
) -> FirstDictationScore:
    """Load the engine and score its first transcription."""
    import time

    from dcent_voice.eval_corpus import word_error_rate

    wav = Path(path)
    if not wav.is_file():
        raise FileNotFoundError(f"missing first-dictation audio: {wav}")
    before = bool(engine.ready().get("model_loaded"))
    load_started = time.perf_counter()
    engine.load()
    load_s = time.perf_counter() - load_started
    after = bool(engine.ready().get("model_loaded"))
    if not after:
        raise RuntimeError("first dictation: model did not load")
    transcribe_started = time.perf_counter()
    result = engine.transcribe_file(wav)
    transcribe_s = time.perf_counter() - transcribe_started
    return FirstDictationScore(
        text=result.text,
        load_s=load_s,
        transcribe_s=transcribe_s,
        asr_s=float(result.asr_latency_s or result.timings.get("asr") or 0.0),
        wer=word_error_rate("Hello world", result.text),
        model_loaded_before=before,
        model_loaded_after=after,
        provider=result.provider,
        model=result.model,
        kind="first_dictation",
    )


@dataclass(frozen=True)
class IdleCpuScore:
    """Idle CPU of a loaded shipped-default engine. No dictation, no tray."""

    samples: tuple[float, ...]
    cpu_mean: float
    cpu_max: float
    rss_bytes: int
    model_loaded: bool
    provider: str
    model: str
    kind: str = "idle_cpu"


def _process_cpu_seconds() -> float:
    import sys

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        created = FILETIME()
        exited = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError("GetProcessTimes failed")
        hundred_ns = (kernel.dwHighDateTime << 32 | kernel.dwLowDateTime) + (
            user.dwHighDateTime << 32 | user.dwLowDateTime
        )
        return hundred_ns / 10_000_000.0
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return float(usage.ru_utime + usage.ru_stime)


def _process_rss_bytes() -> int:
    import sys

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            return 0
        return int(counters.WorkingSetSize)
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = int(getattr(usage, "ru_maxrss", 0) or 0)
    return rss * 1024 if rss < 10_000_000 else rss


def score_shipped_default_idle_cpu(
    engine: VoiceEngine,
    *,
    settle_s: float = 1.0,
    seconds: float = 3.0,
    interval_s: float = 0.5,
) -> IdleCpuScore:
    """Sample idle CPU use after model load."""
    import time

    engine.load()
    ready = engine.ready()
    if not ready.get("model_loaded"):
        raise RuntimeError("idle CPU: model did not load")
    time.sleep(max(0.0, settle_s))
    previous = _process_cpu_seconds()
    previous_t = time.perf_counter()
    samples: list[float] = []
    deadline = time.perf_counter() + max(interval_s, seconds)
    while time.perf_counter() < deadline:
        time.sleep(max(0.05, interval_s))
        now = time.perf_counter()
        current = _process_cpu_seconds()
        elapsed = max(1e-6, now - previous_t)
        samples.append(max(0.0, (current - previous) / elapsed * 100.0))
        previous = current
        previous_t = now
    if not samples:
        raise RuntimeError("idle CPU: no samples")
    spec = getattr(engine.asr, "spec", None)
    return IdleCpuScore(
        samples=tuple(samples),
        cpu_mean=sum(samples) / len(samples),
        cpu_max=max(samples),
        rss_bytes=_process_rss_bytes(),
        model_loaded=True,
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="idle_cpu",
    )


@dataclass(frozen=True)
class StreamDictationScore:
    """Chunked transcribe_stream on shipped-default real speech. No tray."""

    text: str
    events: tuple[str, ...]
    partials: int
    finals: int
    chunks: int
    wer: float
    wall_s: float
    provider: str
    model: str
    kind: str = "stream_dictation"


def score_shipped_default_stream_dictation(
    engine: VoiceEngine,
    path: Path | str,
    *,
    chunk_s: float = 0.25,
) -> StreamDictationScore:
    """Measure shipped-default streaming dictation responsiveness."""
    import time

    from dcent_voice.eval_corpus import word_error_rate

    wav = Path(path)
    if not wav.is_file():
        raise FileNotFoundError(f"missing stream audio: {wav}")
    audio, rate = load_wav_mono(wav)
    if audio.size < int(rate):
        raise ValueError("stream audio shorter than 1 s")
    width = max(1, int(rate * max(0.05, chunk_s)))
    chunks = [audio[i : i + width] for i in range(0, int(audio.size), width)]
    if len(chunks) < 4:
        raise ValueError("stream audio produced too few chunks")
    engine.load()
    started = time.perf_counter()
    events = list(engine.transcribe_stream(chunks, samplerate=rate))
    wall_s = time.perf_counter() - started
    if not events:
        raise RuntimeError("stream dictation: no events")
    types = tuple(str(ev.get("type") or "") for ev in events)
    finals = [ev for ev in events if ev.get("type") == "final"]
    text = str((finals[-1] if finals else events[-1]).get("text") or "")
    spec = getattr(engine.asr, "spec", None)
    return StreamDictationScore(
        text=text,
        events=types,
        partials=sum(1 for item in types if item == "partial"),
        finals=sum(1 for item in types if item == "final"),
        chunks=len(chunks),
        wer=word_error_rate("Hello world", text),
        wall_s=wall_s,
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="stream_dictation",
    )


SHIPPED_DEFAULT_TAIL_AUDIO_IDS: tuple[str, ...] = (
    "hello",
    "ls-tc-wait",
    "ls-short-twenties",
    "punctuation",
    "short-command",
)


def _percentile(values: list[float], p: float) -> float:
    xs = sorted(float(item) for item in values)
    if not xs:
        raise ValueError("percentile of empty sample")
    if len(xs) == 1:
        return xs[0]
    idx = (len(xs) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(xs) - 1)
    frac = idx - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


@dataclass(frozen=True)
class TranscribeTailScore:
    """p50/p95 transcribe_file wall after load. No tray."""

    ids: tuple[str, ...]
    walls_s: tuple[float, ...]
    wers: tuple[float, ...]
    p50_s: float
    p95_s: float
    max_s: float
    provider: str
    model: str
    kind: str = "transcribe_tail"


def score_shipped_default_transcribe_tail(
    engine: VoiceEngine,
) -> TranscribeTailScore:
    """Measure post-load file-transcription latency."""
    import time

    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    catalog = {item.id: item for item in load_corpus()}
    engine.load()
    walls: list[float] = []
    wers: list[float] = []
    for item_id in SHIPPED_DEFAULT_TAIL_AUDIO_IDS:
        item = catalog[item_id]
        if item.audio is None or not item.audio.is_file():
            raise FileNotFoundError(f"missing tail audio: {item_id}")
        started = time.perf_counter()
        result = engine.transcribe_file(item.audio)
        walls.append(time.perf_counter() - started)
        wers.append(word_error_rate(item.reference, result.text))
    spec = getattr(engine.asr, "spec", None)
    return TranscribeTailScore(
        ids=SHIPPED_DEFAULT_TAIL_AUDIO_IDS,
        walls_s=tuple(walls),
        wers=tuple(wers),
        p50_s=_percentile(walls, 50.0),
        p95_s=_percentile(walls, 95.0),
        max_s=max(walls),
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="transcribe_tail",
    )


@dataclass(frozen=True)
class StreamTailScore:
    """p50/p95 transcribe_stream wall after load. No tray."""

    ids: tuple[str, ...]
    walls_s: tuple[float, ...]
    wers: tuple[float, ...]
    p50_s: float
    p95_s: float
    max_s: float
    partials: tuple[int, ...]
    finals: tuple[int, ...]
    provider: str
    model: str
    kind: str = "stream_tail"


def score_shipped_default_stream_tail(
    engine: VoiceEngine,
    *,
    chunk_s: float = 0.25,
) -> StreamTailScore:
    """Measure post-load latency for short streaming clips."""
    import time

    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    catalog = {item.id: item for item in load_corpus()}
    engine.load()
    walls: list[float] = []
    wers: list[float] = []
    partials: list[int] = []
    finals: list[int] = []
    width_s = max(0.05, chunk_s)
    for item_id in SHIPPED_DEFAULT_TAIL_AUDIO_IDS:
        item = catalog[item_id]
        if item.audio is None or not item.audio.is_file():
            raise FileNotFoundError(f"missing stream-tail audio: {item_id}")
        audio, rate = load_wav_mono(item.audio)
        width = max(1, int(rate * width_s))
        chunks = [audio[i : i + width] for i in range(0, int(audio.size), width)]
        if len(chunks) < 4:
            raise ValueError(f"stream-tail too few chunks: {item_id}")
        started = time.perf_counter()
        events = list(engine.transcribe_stream(chunks, samplerate=rate))
        walls.append(time.perf_counter() - started)
        if not events:
            raise RuntimeError(f"stream-tail no events: {item_id}")
        types = tuple(str(ev.get("type") or "") for ev in events)
        last = [ev for ev in events if ev.get("type") == "final"]
        text = str((last[-1] if last else events[-1]).get("text") or "")
        wers.append(word_error_rate(item.reference, text))
        partials.append(sum(1 for item in types if item == "partial"))
        finals.append(sum(1 for item in types if item == "final"))
    spec = getattr(engine.asr, "spec", None)
    return StreamTailScore(
        ids=SHIPPED_DEFAULT_TAIL_AUDIO_IDS,
        walls_s=tuple(walls),
        wers=tuple(wers),
        p50_s=_percentile(walls, 50.0),
        p95_s=_percentile(walls, 95.0),
        max_s=max(walls),
        partials=tuple(partials),
        finals=tuple(finals),
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="stream_tail",
    )


@dataclass(frozen=True)
class ModelLoadScore:
    """Cold load-to-ready on shipped-default Parakeet. No dictation."""

    load_s: float
    model_loaded_before: bool
    model_loaded_after: bool
    provider: str
    model: str
    kind: str = "model_load"


def score_shipped_default_model_load(engine: VoiceEngine) -> ModelLoadScore:
    """Measure time from unloaded to ready."""
    import time

    before = bool(engine.ready().get("model_loaded"))
    started = time.perf_counter()
    engine.load()
    load_s = time.perf_counter() - started
    after = bool(engine.ready().get("model_loaded"))
    if not after:
        raise RuntimeError("model load: model did not load")
    spec = getattr(engine.asr, "spec", None)
    return ModelLoadScore(
        load_s=load_s,
        model_loaded_before=before,
        model_loaded_after=after,
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="model_load",
    )


@dataclass(frozen=True)
class LoadedRamScore:
    """Working-set RSS after shipped-default load. No dictation."""

    rss_bytes: int
    model_loaded: bool
    provider: str
    model: str
    kind: str = "loaded_ram"


def score_shipped_default_loaded_ram(engine: VoiceEngine) -> LoadedRamScore:
    """Measure process RSS after model load."""
    engine.load()
    ready = engine.ready()
    if not ready.get("model_loaded"):
        raise RuntimeError("loaded RAM: model did not load")
    rss_bytes = _process_rss_bytes()
    if rss_bytes <= 0:
        raise RuntimeError("loaded RAM: RSS was not measured")
    spec = getattr(engine.asr, "spec", None)
    return LoadedRamScore(
        rss_bytes=int(rss_bytes),
        model_loaded=True,
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="loaded_ram",
    )


@dataclass(frozen=True)
class UtteranceCpuScore:
    """CPU-seconds of one shipped-default transcribe after load."""

    text: str
    cpu_s: float
    wall_s: float
    audio_s: float
    wer: float
    provider: str
    model: str
    kind: str = "utterance_cpu"


def score_shipped_default_utterance_cpu(
    engine: VoiceEngine,
    path: Path | str,
) -> UtteranceCpuScore:
    """Measure CPU use for a post-load transcription."""
    import time

    from dcent_voice.eval_corpus import word_error_rate

    wav = Path(path)
    if not wav.is_file():
        raise FileNotFoundError(f"missing utterance-cpu audio: {wav}")
    audio, rate = load_wav_mono(wav)
    audio_s = float(audio.size) / float(rate)
    if audio_s < 1.0:
        raise ValueError("utterance-cpu audio shorter than 1 s")
    engine.load()
    before = _process_cpu_seconds()
    started = time.perf_counter()
    result = engine.transcribe_file(wav)
    wall_s = time.perf_counter() - started
    cpu_s = max(0.0, _process_cpu_seconds() - before)
    return UtteranceCpuScore(
        text=result.text,
        cpu_s=cpu_s,
        wall_s=wall_s,
        audio_s=audio_s,
        wer=word_error_rate("Hello world", result.text),
        provider=result.provider,
        model=result.model,
        kind="utterance_cpu",
    )


@dataclass(frozen=True)
class LearnedVocabScore:
    """Learn a product name, then transcribe the same real-speech WAV."""

    before: str
    after: str
    before_wer: float
    after_wer: float
    spoken: str
    written: str
    provider: str
    model: str
    kind: str = "learned_vocab"


def score_shipped_default_learned_vocab(
    engine: VoiceEngine,
) -> LearnedVocabScore:
    """Learn product vocabulary, then score the fixed trusted-prose corpus WAV."""
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing learned-vocab audio: dcentral-terms")
    engine.load()
    # This scorer owns the bundled prose WAV and can honestly opt into contextual
    # learned rewrites. Product callers remain fail-closed unless they do the same.
    before = engine.transcribe_file(item.audio, prose_context=True)
    learned = engine.learn("sent voice", "DCENT_Voice")
    if not learned.get("ok"):
        raise RuntimeError("learned vocab: learn failed")
    after = engine.transcribe_file(item.audio, prose_context=True)
    return LearnedVocabScore(
        before=before.text,
        after=after.text,
        before_wer=word_error_rate(item.reference, before.text),
        after_wer=word_error_rate(item.reference, after.text),
        spoken="sent voice",
        written="DCENT_Voice",
        provider=after.provider,
        model=after.model,
        kind="learned_vocab",
    )


@dataclass(frozen=True)
class AppLearnedVocabScore:
    """Learn a product name for one app, then transcribe under other/none/same app."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    spoken: str
    written: str
    app: str
    other_app: str
    provider: str
    model: str
    kind: str = "app_learned_vocab"


def score_shipped_default_app_learned_vocab(
    engine: VoiceEngine,
) -> AppLearnedVocabScore:
    """Verify learned vocabulary remains scoped in the fixed trusted-prose WAV."""
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing app-learned-vocab audio: dcentral-terms")
    engine.load()
    learned = engine.learn("sent voice", "DCENT_Voice", app_context="notepad.exe")
    if not learned.get("ok"):
        raise RuntimeError("app-learned vocab: learn failed")
    other = engine.transcribe_file(item.audio, app_context="chrome.exe", prose_context=True)
    none = engine.transcribe_file(item.audio, prose_context=True)
    same = engine.transcribe_file(item.audio, app_context="notepad.exe", prose_context=True)
    return AppLearnedVocabScore(
        other=other.text,
        none=none.text,
        same=same.text,
        other_wer=word_error_rate(item.reference, other.text),
        none_wer=word_error_rate(item.reference, none.text),
        same_wer=word_error_rate(item.reference, same.text),
        spoken="sent voice",
        written="DCENT_Voice",
        app="notepad.exe",
        other_app="chrome.exe",
        provider=same.provider,
        model=same.model,
        kind="app_learned_vocab",
    )


@dataclass(frozen=True)
class AppLearnedStreamScore:
    """Learn a product name for one app, then stream under other/none/same app."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    other_partials: int
    none_partials: int
    same_partials: int
    other_finals: int
    none_finals: int
    same_finals: int
    spoken: str
    written: str
    app: str
    other_app: str
    provider: str
    model: str
    kind: str = "app_learned_stream"


def score_shipped_default_app_learned_stream(
    engine: VoiceEngine,
    *,
    chunk_s: float = 0.25,
) -> AppLearnedStreamScore:
    """Measure fail-closed app-scoped learning during streaming."""
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing app-learned-stream audio: dcentral-terms")
    audio, rate = load_wav_mono(item.audio)
    width = max(1, int(rate * max(0.05, chunk_s)))
    chunks = [audio[i : i + width] for i in range(0, int(audio.size), width)]
    if len(chunks) < 4:
        raise ValueError("app-learned-stream too few chunks")
    engine.load()
    learned = engine.learn("sent voice", "DCENT_Voice", app_context="notepad.exe")
    if not learned.get("ok"):
        raise RuntimeError("app-learned stream: learn failed")

    def _final(events: list[dict[str, Any]]) -> tuple[str, int, int]:
        if not events:
            raise RuntimeError("app-learned stream: no events")
        types = tuple(str(ev.get("type") or "") for ev in events)
        last = [ev for ev in events if ev.get("type") == "final"]
        text = str((last[-1] if last else events[-1]).get("text") or "")
        return (
            text,
            sum(1 for item in types if item == "partial"),
            sum(1 for item in types if item == "final"),
        )

    other_text, other_partials, other_finals = _final(
        list(
            engine.transcribe_stream(
                chunks,
                samplerate=rate,
                app_context="chrome.exe",
                prose_context=True,
            )
        )
    )
    none_text, none_partials, none_finals = _final(
        list(engine.transcribe_stream(chunks, samplerate=rate, prose_context=True))
    )
    same_text, same_partials, same_finals = _final(
        list(
            engine.transcribe_stream(
                chunks,
                samplerate=rate,
                app_context="notepad.exe",
                prose_context=True,
            )
        )
    )
    spec = getattr(engine.asr, "spec", None)
    return AppLearnedStreamScore(
        other=other_text,
        none=none_text,
        same=same_text,
        other_wer=word_error_rate(item.reference, other_text),
        none_wer=word_error_rate(item.reference, none_text),
        same_wer=word_error_rate(item.reference, same_text),
        other_partials=other_partials,
        none_partials=none_partials,
        same_partials=same_partials,
        other_finals=other_finals,
        none_finals=none_finals,
        same_finals=same_finals,
        spoken="sent voice",
        written="DCENT_Voice",
        app="notepad.exe",
        other_app="chrome.exe",
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="app_learned_stream",
    )


@dataclass(frozen=True)
class AppLearnedStreamReloadScore:
    """Learn, unload, reload from disk, then stream under other/none/same app."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    other_partials: int
    none_partials: int
    same_partials: int
    other_finals: int
    none_finals: int
    same_finals: int
    spoken: str
    written: str
    app: str
    other_app: str
    store_exists: bool
    distinct_engine: bool
    provider: str
    model: str
    kind: str = "app_learned_stream_reload"


def score_shipped_default_app_learned_stream_reload(
    engine: VoiceEngine,
    *,
    chunk_s: float = 0.25,
) -> AppLearnedStreamReloadScore:
    """Verify app-scoped streaming vocabulary across unload and reload."""
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    store = engine.personalization
    if store is None:
        raise RuntimeError("app-learned stream reload: no store")
    path = Path(store.path)
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing app-learned-stream-reload audio: dcentral-terms")
    audio, rate = load_wav_mono(item.audio)
    width = max(1, int(rate * max(0.05, chunk_s)))
    chunks = [audio[i : i + width] for i in range(0, int(audio.size), width)]
    if len(chunks) < 4:
        raise ValueError("app-learned-stream-reload too few chunks")
    engine.load()
    learned = engine.learn("sent voice", "DCENT_Voice", app_context="notepad.exe")
    if not learned.get("ok"):
        raise RuntimeError("app-learned stream reload: learn failed")
    engine.unload()
    if not path.is_file():
        raise RuntimeError("app-learned stream reload: store missing")
    fresh_store = PersonalizationStore(path, enabled=True, learn=True)
    fresh = VoiceEngine(engine.config, personalization=fresh_store)
    distinct = fresh is not engine

    def _final(events: list[dict[str, Any]]) -> tuple[str, int, int]:
        if not events:
            raise RuntimeError("app-learned stream reload: no events")
        types = tuple(str(ev.get("type") or "") for ev in events)
        last = [ev for ev in events if ev.get("type") == "final"]
        text = str((last[-1] if last else events[-1]).get("text") or "")
        return (
            text,
            sum(1 for row in types if row == "partial"),
            sum(1 for row in types if row == "final"),
        )

    try:
        fresh.load()
        other_text, other_partials, other_finals = _final(
            list(
                fresh.transcribe_stream(
                    chunks,
                    samplerate=rate,
                    app_context="chrome.exe",
                    prose_context=True,
                )
            )
        )
        none_text, none_partials, none_finals = _final(
            list(fresh.transcribe_stream(chunks, samplerate=rate, prose_context=True))
        )
        same_text, same_partials, same_finals = _final(
            list(
                fresh.transcribe_stream(
                    chunks,
                    samplerate=rate,
                    app_context="notepad.exe",
                    prose_context=True,
                )
            )
        )
        spec = getattr(fresh.asr, "spec", None)
    finally:
        fresh.unload()
    return AppLearnedStreamReloadScore(
        other=other_text,
        none=none_text,
        same=same_text,
        other_wer=word_error_rate(item.reference, other_text),
        none_wer=word_error_rate(item.reference, none_text),
        same_wer=word_error_rate(item.reference, same_text),
        other_partials=other_partials,
        none_partials=none_partials,
        same_partials=same_partials,
        other_finals=other_finals,
        none_finals=none_finals,
        same_finals=same_finals,
        spoken="sent voice",
        written="DCENT_Voice",
        app="notepad.exe",
        other_app="chrome.exe",
        store_exists=True,
        distinct_engine=distinct,
        provider=str(getattr(spec, "provider", "") or ""),
        model=str(getattr(spec, "model", "") or ""),
        kind="app_learned_stream_reload",
    )


def run_app_learned_stream_restart_worker(
    store_path: str,
    audio_path: str,
    chunk_s: float,
    out_path: str,
    config_path: str | None = None,
    reference: str | None = None,
) -> None:
    """Child PID: load durable store and stream under other/none/same app."""
    import json
    import os
    import sys

    from dcent_voice.config import load_config
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    config = load_config(
        Path(config_path) if config_path else Path("config.example.toml"),
        create=False,
    )
    store = PersonalizationStore(
        Path(store_path) if store_path else None,
        enabled=True,
        learn=True,
    )
    engine = VoiceEngine(config, personalization=store)
    wav = Path(audio_path)
    if not wav.is_file():
        raise FileNotFoundError("missing app-learned-stream-restart audio")
    if reference:
        ref = reference
    else:
        catalog = {item.id: item for item in load_corpus()}
        ref = catalog["dcentral-terms"].reference
    audio, rate = load_wav_mono(wav)
    width = max(1, int(rate * max(0.05, chunk_s)))
    chunks = [audio[i : i + width] for i in range(0, int(audio.size), width)]
    if len(chunks) < 4:
        raise ValueError("app-learned-stream-restart too few chunks")

    def _final(events: list[dict[str, Any]]) -> tuple[str, int, int]:
        if not events:
            raise RuntimeError("app-learned stream restart: no events")
        types = tuple(str(ev.get("type") or "") for ev in events)
        last = [ev for ev in events if ev.get("type") == "final"]
        text = str((last[-1] if last else events[-1]).get("text") or "")
        return (
            text,
            sum(1 for row in types if row == "partial"),
            sum(1 for row in types if row == "final"),
        )

    try:
        engine.load()
        other_text, other_partials, other_finals = _final(
            list(
                engine.transcribe_stream(
                    chunks,
                    samplerate=rate,
                    app_context="chrome.exe",
                    prose_context=True,
                )
            )
        )
        none_text, none_partials, none_finals = _final(
            list(engine.transcribe_stream(chunks, samplerate=rate, prose_context=True))
        )
        same_text, same_partials, same_finals = _final(
            list(
                engine.transcribe_stream(
                    chunks,
                    samplerate=rate,
                    app_context="notepad.exe",
                    prose_context=True,
                )
            )
        )
        spec = getattr(engine.asr, "spec", None)
    finally:
        engine.unload()
    Path(out_path).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "other": other_text,
                "none": none_text,
                "same": same_text,
                "other_wer": word_error_rate(ref, other_text),
                "none_wer": word_error_rate(ref, none_text),
                "same_wer": word_error_rate(ref, same_text),
                "other_partials": other_partials,
                "none_partials": none_partials,
                "same_partials": same_partials,
                "other_finals": other_finals,
                "none_finals": none_finals,
                "same_finals": same_finals,
                "provider": str(getattr(spec, "provider", "") or ""),
                "model": str(getattr(spec, "model", "") or ""),
                "frozen": bool(getattr(sys, "frozen", False)),
                "exe": str(sys.executable),
                "store": str(store.path),
            }
        ),
        encoding="utf-8",
    )


@dataclass(frozen=True)
class AppLearnedStreamRestartScore:
    """Learn, spawn a new PID, stream under other/none/same app from disk."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    other_partials: int
    none_partials: int
    same_partials: int
    other_finals: int
    none_finals: int
    same_finals: int
    spoken: str
    written: str
    app: str
    other_app: str
    store_exists: bool
    parent_pid: int
    child_pid: int
    distinct_pid: bool
    provider: str
    model: str
    kind: str = "app_learned_stream_restart"


def score_shipped_default_app_learned_stream_restart(
    engine: VoiceEngine,
    *,
    chunk_s: float = 0.25,
) -> AppLearnedStreamRestartScore:
    """Verify app-scoped streaming vocabulary in a restarted process."""
    import json
    import os
    import subprocess
    import sys

    from dcent_voice.eval_corpus import load_corpus

    store = engine.personalization
    if store is None:
        raise RuntimeError("app-learned stream restart: no store")
    path = Path(store.path)
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing app-learned-stream-restart audio: dcentral-terms")
    engine.load()
    learned = engine.learn("sent voice", "DCENT_Voice", app_context="notepad.exe")
    if not learned.get("ok"):
        raise RuntimeError("app-learned stream restart: learn failed")
    engine.unload()
    if not path.is_file():
        raise RuntimeError("app-learned stream restart: store missing")
    out_path = path.with_name(path.name + ".restart.json")
    parent_pid = os.getpid()
    worker_name = run_app_learned_stream_restart_worker.__name__
    script = (
        f"from dcent_voice.engine import {worker_name} as w; "
        f"w({str(path)!r}, {str(item.audio)!r}, {chunk_s!r}, {str(out_path)!r})"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "app-learned stream restart worker failed: "
            + (proc.stderr or proc.stdout or str(proc.returncode))
        )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    child_pid = int(payload["pid"])
    if child_pid == parent_pid:
        raise RuntimeError("app-learned stream restart: child reused parent pid")
    return AppLearnedStreamRestartScore(
        other=str(payload["other"]),
        none=str(payload["none"]),
        same=str(payload["same"]),
        other_wer=float(payload["other_wer"]),
        none_wer=float(payload["none_wer"]),
        same_wer=float(payload["same_wer"]),
        other_partials=int(payload["other_partials"]),
        none_partials=int(payload["none_partials"]),
        same_partials=int(payload["same_partials"]),
        other_finals=int(payload["other_finals"]),
        none_finals=int(payload["none_finals"]),
        same_finals=int(payload["same_finals"]),
        spoken="sent voice",
        written="DCENT_Voice",
        app="notepad.exe",
        other_app="chrome.exe",
        store_exists=True,
        parent_pid=parent_pid,
        child_pid=child_pid,
        distinct_pid=True,
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        kind="app_learned_stream_restart",
    )


@dataclass(frozen=True)
class AppLearnedStreamFrozenScore:
    """Learn, spawn frozen dcent-voice.exe, stream under other/none/same app from disk."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    other_partials: int
    none_partials: int
    same_partials: int
    other_finals: int
    none_finals: int
    same_finals: int
    spoken: str
    written: str
    app: str
    other_app: str
    store_exists: bool
    parent_pid: int
    child_pid: int
    distinct_pid: bool
    frozen: bool
    child_exe: str
    provider: str
    model: str
    kind: str = "app_learned_stream_frozen"


def score_shipped_default_frozen_stream_restart(
    engine: VoiceEngine,
    frozen_exe: Path | str,
    *,
    chunk_s: float = 0.25,
) -> AppLearnedStreamFrozenScore:
    """Verify app-scoped streaming vocabulary in a frozen process."""
    import json
    import os
    import subprocess

    from dcent_voice.eval_corpus import load_corpus

    exe = Path(frozen_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"missing frozen exe: {exe}")
    store = engine.personalization
    if store is None:
        raise RuntimeError("frozen stream restart: no store")
    path = Path(store.path)
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing frozen-stream-restart audio: dcentral-terms")
    engine.load()
    learned = engine.learn("sent voice", "DCENT_Voice", app_context="notepad.exe")
    if not learned.get("ok"):
        raise RuntimeError("frozen stream restart: learn failed")
    engine.unload()
    if not path.is_file():
        raise RuntimeError("frozen stream restart: store missing")
    out_path = path.with_name(path.name + ".frozen.json")
    config_file = Path("config.example.toml").resolve()
    parent_pid = os.getpid()
    proc = subprocess.run(
        [
            str(exe),
            "app-learned-stream-restart",
            "--store",
            str(path),
            "--audio",
            str(item.audio),
            "--out",
            str(out_path),
            "--chunk-s",
            str(chunk_s),
            "--config-file",
            str(config_file),
            "--reference",
            item.reference,
        ],
        capture_output=True,
        text=True,
        cwd=str(exe.parent),
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "frozen stream restart worker failed: "
            + (proc.stderr or proc.stdout or str(proc.returncode))
        )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    child_pid = int(payload["pid"])
    if child_pid == parent_pid:
        raise RuntimeError("frozen stream restart: child reused parent pid")
    child_exe = str(payload.get("exe") or "")
    if "dcent-voice" not in Path(child_exe).name.lower():
        raise RuntimeError(f"frozen stream restart: child was not freeze exe: {child_exe}")
    if not bool(payload.get("frozen")):
        raise RuntimeError("frozen stream restart: child was not a frozen process")
    return AppLearnedStreamFrozenScore(
        other=str(payload["other"]),
        none=str(payload["none"]),
        same=str(payload["same"]),
        other_wer=float(payload["other_wer"]),
        none_wer=float(payload["none_wer"]),
        same_wer=float(payload["same_wer"]),
        other_partials=int(payload["other_partials"]),
        none_partials=int(payload["none_partials"]),
        same_partials=int(payload["same_partials"]),
        other_finals=int(payload["other_finals"]),
        none_finals=int(payload["none_finals"]),
        same_finals=int(payload["same_finals"]),
        spoken="sent voice",
        written="DCENT_Voice",
        app="notepad.exe",
        other_app="chrome.exe",
        store_exists=True,
        parent_pid=parent_pid,
        child_pid=child_pid,
        distinct_pid=True,
        frozen=True,
        child_exe=child_exe,
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        kind="app_learned_stream_frozen",
    )


@dataclass(frozen=True)
class AppLearnedStreamAppdataScore:
    """Quit-relaunch frozen exe against the product APPDATA personalization store."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    other_partials: int
    none_partials: int
    same_partials: int
    other_finals: int
    none_finals: int
    same_finals: int
    spoken: str
    written: str
    app: str
    other_app: str
    store_path: str
    store_is_appdata: bool
    restored: bool
    learn_pid: int
    stream_pid: int
    parent_pid: int
    distinct_pid: bool
    frozen: bool
    child_exe: str
    provider: str
    model: str
    kind: str = "app_learned_stream_appdata"


def score_shipped_default_appdata_stream_relaunch(
    frozen_exe: Path | str,
    *,
    chunk_s: float = 0.25,
) -> AppLearnedStreamAppdataScore:
    """Measure fail-closed APPDATA learning across a frozen-app relaunch."""
    import json
    import os
    import subprocess

    from dcent_voice.eval_corpus import load_corpus

    exe = Path(frozen_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"missing frozen exe: {exe}")
    appdata_store = default_personalization_path()
    backup = appdata_store.read_bytes() if appdata_store.is_file() else None
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing appdata-stream-relaunch audio: dcentral-terms")
    config_file = Path("config.example.toml").resolve()
    out_dir = Path(os.environ.get("TEMP") or ".") / "dcent-appdata-stream"
    out_dir.mkdir(parents=True, exist_ok=True)
    learn_json = out_dir / "learn.json"
    stream_json = out_dir / "stream.json"
    parent_pid = os.getpid()
    learn_proc: subprocess.Popen[Any] | None = None
    score: AppLearnedStreamAppdataScore | None = None
    try:
        learn_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "learn",
                "--from",
                "sent voice",
                "--to",
                "DCENT_Voice",
                "--app",
                "notepad.exe",
                "--output-json",
                str(learn_json),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(exe.parent),
        )
        learn_pid = int(learn_proc.pid)
        learn_stdout, learn_stderr = learn_proc.communicate(timeout=60)
        if learn_proc.returncode != 0:
            raise RuntimeError(
                "appdata stream relaunch learn failed: "
                + (learn_stderr or learn_stdout or str(learn_proc.returncode))
            )
        if not appdata_store.is_file():
            raise RuntimeError("appdata stream relaunch: product store missing after learn")
        stream_proc = subprocess.run(
            [
                str(exe),
                "app-learned-stream-restart",
                "--audio",
                str(item.audio),
                "--out",
                str(stream_json),
                "--chunk-s",
                str(chunk_s),
                "--config-file",
                str(config_file),
                "--reference",
                item.reference,
            ],
            capture_output=True,
            text=True,
            cwd=str(exe.parent),
            timeout=180,
        )
        if stream_proc.returncode != 0:
            raise RuntimeError(
                "appdata stream relaunch stream failed: "
                + (stream_proc.stderr or stream_proc.stdout or str(stream_proc.returncode))
            )
        payload = json.loads(stream_json.read_text(encoding="utf-8"))
        child_exe = str(payload.get("exe") or "")
        if "dcent-voice" not in Path(child_exe).name.lower():
            raise RuntimeError(f"appdata stream relaunch: child was not freeze exe: {child_exe}")
        if not bool(payload.get("frozen")):
            raise RuntimeError("appdata stream relaunch: child was not a frozen process")
        store_path = str(payload.get("store") or "")
        expected = str(appdata_store)
        if Path(store_path).resolve() != appdata_store.resolve():
            raise RuntimeError(
                f"appdata stream relaunch: store was not APPDATA: {store_path} != {expected}"
            )
        stream_pid = int(payload["pid"])
        if stream_pid == parent_pid:
            raise RuntimeError("appdata stream relaunch: stream reused parent pid")
        learn_payload = json.loads(learn_json.read_text(encoding="utf-8"))
        if not learn_payload.get("ok"):
            raise RuntimeError("appdata stream relaunch: learn not ok")
        score = AppLearnedStreamAppdataScore(
            other=str(payload["other"]),
            none=str(payload["none"]),
            same=str(payload["same"]),
            other_wer=float(payload["other_wer"]),
            none_wer=float(payload["none_wer"]),
            same_wer=float(payload["same_wer"]),
            other_partials=int(payload["other_partials"]),
            none_partials=int(payload["none_partials"]),
            same_partials=int(payload["same_partials"]),
            other_finals=int(payload["other_finals"]),
            none_finals=int(payload["none_finals"]),
            same_finals=int(payload["same_finals"]),
            spoken="sent voice",
            written="DCENT_Voice",
            app="notepad.exe",
            other_app="chrome.exe",
            store_path=store_path,
            store_is_appdata=True,
            restored=False,
            learn_pid=learn_pid,
            stream_pid=stream_pid,
            parent_pid=parent_pid,
            distinct_pid=True,
            frozen=True,
            child_exe=child_exe,
            provider=str(payload["provider"]),
            model=str(payload["model"]),
            kind="app_learned_stream_appdata",
        )
    finally:
        if learn_proc is not None:
            terminate_owned_process(learn_proc, grace_s=1.0, kill_s=5.0)
        if backup is None:
            if appdata_store.is_file():
                appdata_store.unlink()
        else:
            appdata_store.write_bytes(backup)
    if score is None:
        raise RuntimeError("appdata stream relaunch: no score")
    return replace(score, restored=True)


@dataclass(frozen=True)
class AppLearnedStreamDesktopScore:
    """Quit-relaunch frozen tray/desktop app against the product APPDATA store."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    other_partials: int
    none_partials: int
    same_partials: int
    other_finals: int
    none_finals: int
    same_finals: int
    spoken: str
    written: str
    app: str
    other_app: str
    store_path: str
    store_is_appdata: bool
    restored: bool
    desktop: bool
    learn_pid: int
    stream_pid: int
    parent_pid: int
    distinct_pid: bool
    frozen: bool
    child_exe: str
    provider: str
    model: str
    kind: str = "app_learned_stream_desktop"


def score_shipped_default_desktop_stream_relaunch(
    frozen_exe: Path | str,
    *,
    chunk_s: float = 0.25,
) -> AppLearnedStreamDesktopScore:
    """Verify app-scoped streaming vocabulary across a frozen desktop relaunch."""
    import os
    import socket
    import subprocess
    import time

    from dcent_voice.attach.client import VoiceAttachClient
    from dcent_voice.attach.registry import default_registry_dir, read_registry_entry
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    exe = Path(frozen_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"missing frozen exe: {exe}")
    appdata_store = default_personalization_path()
    backup = appdata_store.read_bytes() if appdata_store.is_file() else None
    registry_dir = default_registry_dir()
    registry_names = (
        "dcent-voice.json",
        "dcent-voice.token",
        "dcent-voice.lock",
        "dcent-voice.install.json",
    )
    registry_backup = {
        name: (registry_dir / name).read_bytes() if (registry_dir / name).is_file() else None
        for name in registry_names
    }
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing desktop-stream-relaunch audio: dcentral-terms")
    audio, rate = load_wav_mono(item.audio)
    width = max(1, int(rate * max(0.05, chunk_s)))
    chunks = [audio[i : i + width] for i in range(0, int(audio.size), width)]
    if len(chunks) < 4:
        raise ValueError("desktop stream relaunch too few chunks")
    out_dir = Path(os.environ.get("TEMP") or ".") / "dcent-desktop-stream"
    out_dir.mkdir(parents=True, exist_ok=True)
    example = Path("config.example.toml").read_text(encoding="utf-8")
    finder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    finder.bind(("127.0.0.1", 0))
    port = int(finder.getsockname()[1])
    finder.close()
    config_text = example.replace("port = 8765", f"port = {port}", 1)
    config_text = config_text.replace(
        "first_run_education_shown = false",
        "first_run_education_shown = true",
        1,
    )
    config_file = out_dir / "config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    child_env = os.environ.copy()
    child_env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    child_env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_w280_{os.getpid()}"
    flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    parent_pid = os.getpid()
    learn_proc: subprocess.Popen[bytes] | None = None
    stream_proc: subprocess.Popen[bytes] | None = None
    score: AppLearnedStreamDesktopScore | None = None

    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is not None:
            terminate_owned_process(proc, grace_s=20.0, kill_s=10.0)

    def _wait_client(proc: subprocess.Popen[bytes]) -> VoiceAttachClient:
        deadline = time.monotonic() + 180
        last = "not_running"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "desktop stream relaunch: desktop exited " + str(proc.returncode)
                )
            try:
                client = VoiceAttachClient.discover(timeout=180.0)
                body = client.ready()
                if body.get("ready") and body.get("model_loaded"):
                    entry = read_registry_entry(registry_dir / "dcent-voice.json")
                    if str(port) not in str(entry.endpoint):
                        client.close()
                        last = f"endpoint {entry.endpoint}"
                        time.sleep(0.4)
                        continue
                    if int(entry.pid or 0) != int(proc.pid):
                        client.close()
                        last = f"pid {entry.pid} != {proc.pid}"
                        time.sleep(0.4)
                        continue
                    return client
                client.close()
                last = str(body)
            except Exception as exc:
                last = str(exc)
            time.sleep(0.4)
        raise RuntimeError("desktop stream relaunch: ADE not ready: " + last)

    def _restore_registry() -> None:
        registry_dir.mkdir(parents=True, exist_ok=True)
        for name, data in registry_backup.items():
            path = registry_dir / name
            if data is None:
                if path.is_file():
                    path.unlink()
            else:
                path.write_bytes(data)

    def _restore_store() -> None:
        if backup is None:
            if appdata_store.is_file():
                appdata_store.unlink()
        else:
            appdata_store.write_bytes(backup)

    def _final(events: list[dict[str, Any]]) -> tuple[str, int, int]:
        if not events:
            raise RuntimeError("desktop stream relaunch: no stream events")
        types = tuple(str(ev.get("type") or "") for ev in events)
        last = [ev for ev in events if ev.get("type") == "final"]
        text = str((last[-1] if last else events[-1]).get("text") or "")
        return (
            text,
            sum(1 for row in types if row == "partial"),
            sum(1 for row in types if row == "final"),
        )

    try:
        learn_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        learn_pid = int(learn_proc.pid)
        learn_client = _wait_client(learn_proc)
        try:
            learned = learn_client.learn(
                "sent voice",
                "DCENT_Voice",
                app_context="notepad.exe",
            )
        finally:
            learn_client.close()
        if not learned.get("ok"):
            raise RuntimeError("desktop stream relaunch: learn not ok")
        if not appdata_store.is_file():
            raise RuntimeError("desktop stream relaunch: product store missing after learn")
        _stop(learn_proc)
        time.sleep(1)
        stream_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        stream_pid = int(stream_proc.pid)
        if stream_pid in (parent_pid, learn_pid):
            raise RuntimeError("desktop stream relaunch: stream reused pid")
        stream_client = _wait_client(stream_proc)
        try:
            caps = stream_client.capabilities()
            other_text, other_partials, other_finals = _final(
                stream_client.stream_session(
                    chunks,
                    samplerate=rate,
                    app_context="chrome.exe",
                    prose_context=True,
                )
            )
            none_text, none_partials, none_finals = _final(
                stream_client.stream_session(chunks, samplerate=rate, prose_context=True)
            )
            same_text, same_partials, same_finals = _final(
                stream_client.stream_session(
                    chunks,
                    samplerate=rate,
                    app_context="notepad.exe",
                    prose_context=True,
                )
            )
        finally:
            stream_client.close()
        _stop(stream_proc)
        score = AppLearnedStreamDesktopScore(
            other=other_text,
            none=none_text,
            same=same_text,
            other_wer=word_error_rate(item.reference, other_text),
            none_wer=word_error_rate(item.reference, none_text),
            same_wer=word_error_rate(item.reference, same_text),
            other_partials=other_partials,
            none_partials=none_partials,
            same_partials=same_partials,
            other_finals=other_finals,
            none_finals=none_finals,
            same_finals=same_finals,
            spoken="sent voice",
            written="DCENT_Voice",
            app="notepad.exe",
            other_app="chrome.exe",
            store_path=str(appdata_store),
            store_is_appdata=True,
            restored=False,
            desktop=True,
            learn_pid=learn_pid,
            stream_pid=stream_pid,
            parent_pid=parent_pid,
            distinct_pid=True,
            frozen=True,
            child_exe=str(exe),
            provider=str(caps.get("provider") or ""),
            model=str(caps.get("model") or ""),
            kind="app_learned_stream_desktop",
        )
    finally:
        _stop(learn_proc)
        _stop(stream_proc)
        _restore_store()
        _restore_registry()
    if score is None:
        raise RuntimeError("desktop stream relaunch: no score")
    return replace(score, restored=True)


@dataclass(frozen=True)
class AppLearnedOneshotDesktopScore:
    """Quit-relaunch frozen tray/desktop app ADE oneshot against APPDATA."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    spoken: str
    written: str
    app: str
    other_app: str
    store_path: str
    store_is_appdata: bool
    restored: bool
    desktop: bool
    oneshot: bool
    learn_pid: int
    oneshot_pid: int
    parent_pid: int
    distinct_pid: bool
    frozen: bool
    child_exe: str
    provider: str
    model: str
    kind: str = "app_learned_oneshot_desktop"


def score_shipped_default_desktop_oneshot_relaunch(
    frozen_exe: Path | str,
) -> AppLearnedOneshotDesktopScore:
    """Verify app-scoped one-shot vocabulary across a frozen desktop relaunch."""
    import os
    import socket
    import subprocess
    import time

    from dcent_voice.attach.client import VoiceAttachClient
    from dcent_voice.attach.registry import default_registry_dir, read_registry_entry
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    exe = Path(frozen_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"missing frozen exe: {exe}")
    appdata_store = default_personalization_path()
    backup = appdata_store.read_bytes() if appdata_store.is_file() else None
    registry_dir = default_registry_dir()
    registry_names = (
        "dcent-voice.json",
        "dcent-voice.token",
        "dcent-voice.lock",
        "dcent-voice.install.json",
    )
    registry_backup = {
        name: (registry_dir / name).read_bytes() if (registry_dir / name).is_file() else None
        for name in registry_names
    }
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing desktop-oneshot-relaunch audio: dcentral-terms")
    out_dir = Path(os.environ.get("TEMP") or ".") / "dcent-desktop-oneshot"
    out_dir.mkdir(parents=True, exist_ok=True)
    example = Path("config.example.toml").read_text(encoding="utf-8")
    finder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    finder.bind(("127.0.0.1", 0))
    port = int(finder.getsockname()[1])
    finder.close()
    config_text = example.replace("port = 8765", f"port = {port}", 1)
    config_text = config_text.replace(
        "first_run_education_shown = false",
        "first_run_education_shown = true",
        1,
    )
    config_file = out_dir / "config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    child_env = os.environ.copy()
    child_env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    child_env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_w281_{os.getpid()}"
    flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    parent_pid = os.getpid()
    learn_proc: subprocess.Popen[bytes] | None = None
    oneshot_proc: subprocess.Popen[bytes] | None = None
    score: AppLearnedOneshotDesktopScore | None = None

    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is not None:
            terminate_owned_process(proc, grace_s=20.0, kill_s=10.0)

    def _wait_client(proc: subprocess.Popen[bytes]) -> VoiceAttachClient:
        deadline = time.monotonic() + 180
        last = "not_running"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "desktop oneshot relaunch: desktop exited " + str(proc.returncode)
                )
            try:
                client = VoiceAttachClient.discover(timeout=180.0)
                body = client.ready()
                if body.get("ready") and body.get("model_loaded"):
                    entry = read_registry_entry(registry_dir / "dcent-voice.json")
                    if str(port) not in str(entry.endpoint):
                        client.close()
                        last = f"endpoint {entry.endpoint}"
                        time.sleep(0.4)
                        continue
                    if int(entry.pid or 0) != int(proc.pid):
                        client.close()
                        last = f"pid {entry.pid} != {proc.pid}"
                        time.sleep(0.4)
                        continue
                    return client
                client.close()
                last = str(body)
            except Exception as exc:
                last = str(exc)
            time.sleep(0.4)
        raise RuntimeError("desktop oneshot relaunch: ADE not ready: " + last)

    def _restore_registry() -> None:
        registry_dir.mkdir(parents=True, exist_ok=True)
        for name, data in registry_backup.items():
            path = registry_dir / name
            if data is None:
                if path.is_file():
                    path.unlink()
            else:
                path.write_bytes(data)

    def _restore_store() -> None:
        if backup is None:
            if appdata_store.is_file():
                appdata_store.unlink()
        else:
            appdata_store.write_bytes(backup)

    def _text(body: dict[str, Any]) -> str:
        return str(body.get("cleaned") or body.get("raw") or body.get("text") or "")

    try:
        learn_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        learn_pid = int(learn_proc.pid)
        learn_client = _wait_client(learn_proc)
        try:
            learned = learn_client.learn(
                "sent voice",
                "DCENT_Voice",
                app_context="notepad.exe",
            )
        finally:
            learn_client.close()
        if not learned.get("ok"):
            raise RuntimeError("desktop oneshot relaunch: learn not ok")
        if not appdata_store.is_file():
            raise RuntimeError("desktop oneshot relaunch: product store missing after learn")
        _stop(learn_proc)
        time.sleep(1)
        oneshot_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        oneshot_pid = int(oneshot_proc.pid)
        if oneshot_pid in (parent_pid, learn_pid):
            raise RuntimeError("desktop oneshot relaunch: oneshot reused pid")
        oneshot_client = _wait_client(oneshot_proc)
        try:
            caps = oneshot_client.capabilities()
            other_text = _text(
                oneshot_client.transcribe_file(
                    item.audio,
                    app_context="chrome.exe",
                    prose_context=True,
                )
            )
            none_text = _text(oneshot_client.transcribe_file(item.audio, prose_context=True))
            same_text = _text(
                oneshot_client.transcribe_file(
                    item.audio,
                    app_context="notepad.exe",
                    prose_context=True,
                )
            )
        finally:
            oneshot_client.close()
        _stop(oneshot_proc)
        score = AppLearnedOneshotDesktopScore(
            other=other_text,
            none=none_text,
            same=same_text,
            other_wer=word_error_rate(item.reference, other_text),
            none_wer=word_error_rate(item.reference, none_text),
            same_wer=word_error_rate(item.reference, same_text),
            spoken="sent voice",
            written="DCENT_Voice",
            app="notepad.exe",
            other_app="chrome.exe",
            store_path=str(appdata_store),
            store_is_appdata=True,
            restored=False,
            desktop=True,
            oneshot=True,
            learn_pid=learn_pid,
            oneshot_pid=oneshot_pid,
            parent_pid=parent_pid,
            distinct_pid=True,
            frozen=True,
            child_exe=str(exe),
            provider=str(caps.get("provider") or ""),
            model=str(caps.get("model") or ""),
            kind="app_learned_oneshot_desktop",
        )
    finally:
        _stop(learn_proc)
        _stop(oneshot_proc)
        _restore_store()
        _restore_registry()
    if score is None:
        raise RuntimeError("desktop oneshot relaunch: no score")
    return replace(score, restored=True)


@dataclass(frozen=True)
class AppLearnedComposeDesktopScore:
    """Quit-relaunch frozen tray/desktop app ADE compose against APPDATA."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    spoken: str
    written: str
    app: str
    other_app: str
    store_path: str
    store_is_appdata: bool
    restored: bool
    desktop: bool
    compose: bool
    learn_pid: int
    compose_pid: int
    parent_pid: int
    distinct_pid: bool
    frozen: bool
    child_exe: str
    provider: str
    model: str
    kind: str = "app_learned_compose_desktop"


def score_shipped_default_desktop_compose_relaunch(
    frozen_exe: Path | str,
) -> AppLearnedComposeDesktopScore:
    """Verify app-scoped composition vocabulary across a frozen desktop relaunch."""
    import os
    import socket
    import subprocess
    import time

    from dcent_voice.attach.client import VoiceAttachClient
    from dcent_voice.attach.registry import default_registry_dir, read_registry_entry
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    exe = Path(frozen_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"missing frozen exe: {exe}")
    appdata_store = default_personalization_path()
    backup = appdata_store.read_bytes() if appdata_store.is_file() else None
    registry_dir = default_registry_dir()
    registry_names = (
        "dcent-voice.json",
        "dcent-voice.token",
        "dcent-voice.lock",
        "dcent-voice.install.json",
    )
    registry_backup = {
        name: (registry_dir / name).read_bytes() if (registry_dir / name).is_file() else None
        for name in registry_names
    }
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    cue = item.reference.replace("DCENT_Voice", "sent voice")
    out_dir = Path(os.environ.get("TEMP") or ".") / "dcent-desktop-compose"
    out_dir.mkdir(parents=True, exist_ok=True)
    example = Path("config.example.toml").read_text(encoding="utf-8")
    finder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    finder.bind(("127.0.0.1", 0))
    port = int(finder.getsockname()[1])
    finder.close()
    config_text = example.replace("port = 8765", f"port = {port}", 1)
    config_text = config_text.replace(
        "first_run_education_shown = false",
        "first_run_education_shown = true",
        1,
    )
    config_file = out_dir / "config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    child_env = os.environ.copy()
    child_env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    child_env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_w282_{os.getpid()}"
    flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    parent_pid = os.getpid()
    learn_proc: subprocess.Popen[bytes] | None = None
    compose_proc: subprocess.Popen[bytes] | None = None
    score: AppLearnedComposeDesktopScore | None = None

    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is not None:
            terminate_owned_process(proc, grace_s=20.0, kill_s=10.0)

    def _wait_client(proc: subprocess.Popen[bytes]) -> VoiceAttachClient:
        deadline = time.monotonic() + 180
        last = "not_running"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "desktop compose relaunch: desktop exited " + str(proc.returncode)
                )
            try:
                client = VoiceAttachClient.discover(timeout=180.0)
                body = client.ready()
                if body.get("ready") and body.get("model_loaded"):
                    entry = read_registry_entry(registry_dir / "dcent-voice.json")
                    if str(port) not in str(entry.endpoint):
                        client.close()
                        last = f"endpoint {entry.endpoint}"
                        time.sleep(0.4)
                        continue
                    if int(entry.pid or 0) != int(proc.pid):
                        client.close()
                        last = f"pid {entry.pid} != {proc.pid}"
                        time.sleep(0.4)
                        continue
                    return client
                client.close()
                last = str(body)
            except Exception as exc:
                last = str(exc)
            time.sleep(0.4)
        raise RuntimeError("desktop compose relaunch: ADE not ready: " + last)

    def _restore_registry() -> None:
        registry_dir.mkdir(parents=True, exist_ok=True)
        for name, data in registry_backup.items():
            path = registry_dir / name
            if data is None:
                if path.is_file():
                    path.unlink()
            else:
                path.write_bytes(data)

    def _restore_store() -> None:
        if backup is None:
            if appdata_store.is_file():
                appdata_store.unlink()
        else:
            appdata_store.write_bytes(backup)

    try:
        learn_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        learn_pid = int(learn_proc.pid)
        learn_client = _wait_client(learn_proc)
        try:
            learned = learn_client.learn(
                "sent voice",
                "DCENT_Voice",
                app_context="notepad.exe",
            )
        finally:
            learn_client.close()
        if not learned.get("ok"):
            raise RuntimeError("desktop compose relaunch: learn not ok")
        if not appdata_store.is_file():
            raise RuntimeError("desktop compose relaunch: product store missing after learn")
        _stop(learn_proc)
        time.sleep(1)
        compose_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        compose_pid = int(compose_proc.pid)
        if compose_pid in (parent_pid, learn_pid):
            raise RuntimeError("desktop compose relaunch: compose reused pid")
        compose_client = _wait_client(compose_proc)
        try:
            caps = compose_client.capabilities()
            other_text = str(
                compose_client.compose(cue, app_context="chrome.exe").get("text") or ""
            )
            none_text = str(compose_client.compose(cue).get("text") or "")
            same_text = str(
                compose_client.compose(cue, app_context="notepad.exe").get("text") or ""
            )
        finally:
            compose_client.close()
        _stop(compose_proc)
        score = AppLearnedComposeDesktopScore(
            other=other_text,
            none=none_text,
            same=same_text,
            other_wer=word_error_rate(item.reference, other_text),
            none_wer=word_error_rate(item.reference, none_text),
            same_wer=word_error_rate(item.reference, same_text),
            spoken="sent voice",
            written="DCENT_Voice",
            app="notepad.exe",
            other_app="chrome.exe",
            store_path=str(appdata_store),
            store_is_appdata=True,
            restored=False,
            desktop=True,
            compose=True,
            learn_pid=learn_pid,
            compose_pid=compose_pid,
            parent_pid=parent_pid,
            distinct_pid=True,
            frozen=True,
            child_exe=str(exe),
            provider=str(caps.get("provider") or ""),
            model=str(caps.get("model") or ""),
            kind="app_learned_compose_desktop",
        )
    finally:
        _stop(learn_proc)
        _stop(compose_proc)
        _restore_store()
        _restore_registry()
    if score is None:
        raise RuntimeError("desktop compose relaunch: no score")
    return replace(score, restored=True)


@dataclass(frozen=True)
class AppLearnedPersonalizationDesktopScore:
    """Quit-relaunch frozen tray/desktop app ADE personalization against APPDATA."""

    spoken: str
    written: str
    app: str
    other_app: str
    term_count: int
    term_app: str
    stores_audio: bool
    other_app_absent: bool
    store_path: str
    store_is_appdata: bool
    restored: bool
    desktop: bool
    inspect: bool
    learn_pid: int
    inspect_pid: int
    parent_pid: int
    distinct_pid: bool
    frozen: bool
    child_exe: str
    provider: str
    model: str
    kind: str = "app_learned_personalization_desktop"


def score_shipped_default_desktop_personalization_relaunch(
    frozen_exe: Path | str,
) -> AppLearnedPersonalizationDesktopScore:
    """Verify scoped personalization across a frozen desktop relaunch."""
    import os
    import socket
    import subprocess
    import time

    from dcent_voice.attach.client import VoiceAttachClient
    from dcent_voice.attach.registry import default_registry_dir, read_registry_entry

    exe = Path(frozen_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"missing frozen exe: {exe}")
    appdata_store = default_personalization_path()
    backup = appdata_store.read_bytes() if appdata_store.is_file() else None
    registry_dir = default_registry_dir()
    registry_names = (
        "dcent-voice.json",
        "dcent-voice.token",
        "dcent-voice.lock",
        "dcent-voice.install.json",
    )
    registry_backup = {
        name: (registry_dir / name).read_bytes() if (registry_dir / name).is_file() else None
        for name in registry_names
    }
    out_dir = Path(os.environ.get("TEMP") or ".") / "dcent-desktop-personalization"
    out_dir.mkdir(parents=True, exist_ok=True)
    example = Path("config.example.toml").read_text(encoding="utf-8")
    finder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    finder.bind(("127.0.0.1", 0))
    port = int(finder.getsockname()[1])
    finder.close()
    config_text = example.replace("port = 8765", f"port = {port}", 1)
    config_text = config_text.replace(
        "first_run_education_shown = false",
        "first_run_education_shown = true",
        1,
    )
    config_file = out_dir / "config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    child_env = os.environ.copy()
    child_env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    child_env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_w283_{os.getpid()}"
    flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    parent_pid = os.getpid()
    learn_proc: subprocess.Popen[bytes] | None = None
    inspect_proc: subprocess.Popen[bytes] | None = None
    score: AppLearnedPersonalizationDesktopScore | None = None

    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is not None:
            terminate_owned_process(proc, grace_s=20.0, kill_s=10.0)

    def _wait_client(proc: subprocess.Popen[bytes]) -> VoiceAttachClient:
        deadline = time.monotonic() + 180
        last = "not_running"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "desktop personalization relaunch: desktop exited " + str(proc.returncode)
                )
            try:
                client = VoiceAttachClient.discover(timeout=180.0)
                body = client.ready()
                if body.get("ready") and body.get("model_loaded"):
                    entry = read_registry_entry(registry_dir / "dcent-voice.json")
                    if str(port) not in str(entry.endpoint):
                        client.close()
                        last = f"endpoint {entry.endpoint}"
                        time.sleep(0.4)
                        continue
                    if int(entry.pid or 0) != int(proc.pid):
                        client.close()
                        last = f"pid {entry.pid} != {proc.pid}"
                        time.sleep(0.4)
                        continue
                    return client
                client.close()
                last = str(body)
            except Exception as exc:
                last = str(exc)
            time.sleep(0.4)
        raise RuntimeError("desktop personalization relaunch: ADE not ready: " + last)

    def _restore_registry() -> None:
        registry_dir.mkdir(parents=True, exist_ok=True)
        for name, data in registry_backup.items():
            path = registry_dir / name
            if data is None:
                if path.is_file():
                    path.unlink()
            else:
                path.write_bytes(data)

    def _restore_store() -> None:
        if backup is None:
            if appdata_store.is_file():
                appdata_store.unlink()
        else:
            appdata_store.write_bytes(backup)

    try:
        learn_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        learn_pid = int(learn_proc.pid)
        learn_client = _wait_client(learn_proc)
        try:
            learned = learn_client.learn(
                "sent voice",
                "DCENT_Voice",
                app_context="notepad.exe",
            )
        finally:
            learn_client.close()
        if not learned.get("ok"):
            raise RuntimeError("desktop personalization relaunch: learn not ok")
        if not appdata_store.is_file():
            raise RuntimeError(
                "desktop personalization relaunch: product store missing after learn"
            )
        _stop(learn_proc)
        time.sleep(1)
        inspect_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        inspect_pid = int(inspect_proc.pid)
        if inspect_pid in (parent_pid, learn_pid):
            raise RuntimeError("desktop personalization relaunch: inspect reused pid")
        inspect_client = _wait_client(inspect_proc)
        try:
            caps = inspect_client.capabilities()
            snap = inspect_client.personalization()
        finally:
            inspect_client.close()
        _stop(inspect_proc)
        terms = list(snap.get("terms") or [])
        apps = [str(term.get("app") or "").lower() for term in terms]
        writtens = [str(term.get("written") or "") for term in terms]
        spokens = [str(term.get("spoken") or "") for term in terms]
        if "DCENT_Voice" not in writtens:
            raise RuntimeError(
                "desktop personalization relaunch: written form missing after relaunch"
            )
        if "sent voice" not in spokens:
            raise RuntimeError(
                "desktop personalization relaunch: spoken form missing after relaunch"
            )
        if "notepad.exe" not in apps:
            raise RuntimeError(
                "desktop personalization relaunch: notepad term missing after relaunch"
            )
        other_app_absent = "chrome.exe" not in apps
        if not other_app_absent:
            raise RuntimeError(
                "desktop personalization relaunch: chrome term leaked after relaunch"
            )
        if bool(snap.get("stores_audio")):
            raise RuntimeError("desktop personalization relaunch: snapshot stores audio")
        score = AppLearnedPersonalizationDesktopScore(
            spoken="sent voice",
            written="DCENT_Voice",
            app="notepad.exe",
            other_app="chrome.exe",
            term_count=len(terms),
            term_app="notepad.exe",
            stores_audio=False,
            other_app_absent=True,
            store_path=str(appdata_store),
            store_is_appdata=True,
            restored=False,
            desktop=True,
            inspect=True,
            learn_pid=learn_pid,
            inspect_pid=inspect_pid,
            parent_pid=parent_pid,
            distinct_pid=True,
            frozen=True,
            child_exe=str(exe),
            provider=str(caps.get("provider") or ""),
            model=str(caps.get("model") or ""),
            kind="app_learned_personalization_desktop",
        )
    finally:
        _stop(learn_proc)
        _stop(inspect_proc)
        _restore_store()
        _restore_registry()
    if score is None:
        raise RuntimeError("desktop personalization relaunch: no score")
    return replace(score, restored=True)


@dataclass(frozen=True)
class AppLearnedJsonDesktopScore:
    """Quit-relaunch frozen tray/desktop app ADE JSON transcribe against APPDATA."""

    other: str
    none: str
    same: str
    other_wer: float
    none_wer: float
    same_wer: float
    spoken: str
    written: str
    app: str
    other_app: str
    store_path: str
    store_is_appdata: bool
    restored: bool
    desktop: bool
    json_audio: bool
    learn_pid: int
    transcribe_pid: int
    parent_pid: int
    distinct_pid: bool
    frozen: bool
    child_exe: str
    provider: str
    model: str
    kind: str = "app_learned_json_desktop"


def score_shipped_default_desktop_json_relaunch(
    frozen_exe: Path | str,
) -> AppLearnedJsonDesktopScore:
    """Verify scoped JSON transcription across a frozen desktop relaunch."""
    import os
    import socket
    import subprocess
    import time

    from dcent_voice.attach.client import VoiceAttachClient
    from dcent_voice.attach.registry import default_registry_dir, read_registry_entry
    from dcent_voice.eval_corpus import load_corpus, word_error_rate

    exe = Path(frozen_exe)
    if not exe.is_file():
        raise FileNotFoundError(f"missing frozen exe: {exe}")
    appdata_store = default_personalization_path()
    backup = appdata_store.read_bytes() if appdata_store.is_file() else None
    registry_dir = default_registry_dir()
    registry_names = (
        "dcent-voice.json",
        "dcent-voice.token",
        "dcent-voice.lock",
        "dcent-voice.install.json",
    )
    registry_backup = {
        name: (registry_dir / name).read_bytes() if (registry_dir / name).is_file() else None
        for name in registry_names
    }
    catalog = {item.id: item for item in load_corpus()}
    item = catalog["dcentral-terms"]
    if item.audio is None or not item.audio.is_file():
        raise FileNotFoundError("missing desktop-json-relaunch audio: dcentral-terms")
    audio, rate = load_wav_mono(item.audio)
    samples = [float(sample) for sample in audio]
    out_dir = Path(os.environ.get("TEMP") or ".") / "dcent-desktop-json"
    out_dir.mkdir(parents=True, exist_ok=True)
    example = Path("config.example.toml").read_text(encoding="utf-8")
    finder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    finder.bind(("127.0.0.1", 0))
    port = int(finder.getsockname()[1])
    finder.close()
    config_text = example.replace("port = 8765", f"port = {port}", 1)
    config_text = config_text.replace(
        "first_run_education_shown = false",
        "first_run_education_shown = true",
        1,
    )
    config_file = out_dir / "config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    child_env = os.environ.copy()
    child_env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
    child_env["DCENT_VOICE_SMOKE_MUTEX"] = f"Local\\DCENT_Voice_Smoke_w284_{os.getpid()}"
    flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    parent_pid = os.getpid()
    learn_proc: subprocess.Popen[bytes] | None = None
    transcribe_proc: subprocess.Popen[bytes] | None = None
    score: AppLearnedJsonDesktopScore | None = None

    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is not None:
            terminate_owned_process(proc, grace_s=20.0, kill_s=10.0)

    def _wait_client(proc: subprocess.Popen[bytes]) -> VoiceAttachClient:
        deadline = time.monotonic() + 180
        last = "not_running"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("desktop JSON relaunch: desktop exited " + str(proc.returncode))
            try:
                client = VoiceAttachClient.discover(timeout=180.0)
                body = client.ready()
                if body.get("ready") and body.get("model_loaded"):
                    entry = read_registry_entry(registry_dir / "dcent-voice.json")
                    if str(port) not in str(entry.endpoint):
                        client.close()
                        last = f"endpoint {entry.endpoint}"
                        time.sleep(0.4)
                        continue
                    if int(entry.pid or 0) != int(proc.pid):
                        client.close()
                        last = f"pid {entry.pid} != {proc.pid}"
                        time.sleep(0.4)
                        continue
                    return client
                client.close()
                last = str(body)
            except Exception as exc:
                last = str(exc)
            time.sleep(0.4)
        raise RuntimeError("desktop JSON relaunch: ADE not ready: " + last)

    def _restore_registry() -> None:
        registry_dir.mkdir(parents=True, exist_ok=True)
        for name, data in registry_backup.items():
            path = registry_dir / name
            if data is None:
                if path.is_file():
                    path.unlink()
            else:
                path.write_bytes(data)

    def _restore_store() -> None:
        if backup is None:
            if appdata_store.is_file():
                appdata_store.unlink()
        else:
            appdata_store.write_bytes(backup)

    def _text(body: dict[str, Any]) -> str:
        return str(body.get("cleaned") or body.get("raw") or body.get("text") or "")

    try:
        learn_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        learn_pid = int(learn_proc.pid)
        learn_client = _wait_client(learn_proc)
        try:
            learned = learn_client.learn(
                "sent voice",
                "DCENT_Voice",
                app_context="notepad.exe",
            )
        finally:
            learn_client.close()
        if not learned.get("ok"):
            raise RuntimeError("desktop JSON relaunch: learn not ok")
        if not appdata_store.is_file():
            raise RuntimeError("desktop JSON relaunch: product store missing after learn")
        _stop(learn_proc)
        time.sleep(1)
        transcribe_proc = start_owned_process(
            [
                str(exe),
                "--config",
                str(config_file),
                "--no-hotkeys",
                "--no-overlay",
            ],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=flags,
        )
        transcribe_pid = int(transcribe_proc.pid)
        if transcribe_pid in (parent_pid, learn_pid):
            raise RuntimeError("desktop JSON relaunch: transcribe reused pid")
        json_client = _wait_client(transcribe_proc)
        try:
            caps = json_client.capabilities()
            other_text = _text(
                json_client.transcribe(
                    {
                        "audio": samples,
                        "samplerate": rate,
                        "app_context": "chrome.exe",
                        "prose_context": True,
                    }
                )
            )
            none_text = _text(
                json_client.transcribe(
                    {"audio": samples, "samplerate": rate, "prose_context": True}
                )
            )
            same_text = _text(
                json_client.transcribe(
                    {
                        "audio": samples,
                        "samplerate": rate,
                        "app_context": "notepad.exe",
                        "prose_context": True,
                    }
                )
            )
        finally:
            json_client.close()
        _stop(transcribe_proc)
        score = AppLearnedJsonDesktopScore(
            other=other_text,
            none=none_text,
            same=same_text,
            other_wer=word_error_rate(item.reference, other_text),
            none_wer=word_error_rate(item.reference, none_text),
            same_wer=word_error_rate(item.reference, same_text),
            spoken="sent voice",
            written="DCENT_Voice",
            app="notepad.exe",
            other_app="chrome.exe",
            store_path=str(appdata_store),
            store_is_appdata=True,
            restored=False,
            desktop=True,
            json_audio=True,
            learn_pid=learn_pid,
            transcribe_pid=transcribe_pid,
            parent_pid=parent_pid,
            distinct_pid=True,
            frozen=True,
            child_exe=str(exe),
            provider=str(caps.get("provider") or ""),
            model=str(caps.get("model") or ""),
            kind="app_learned_json_desktop",
        )
    finally:
        _stop(learn_proc)
        _stop(transcribe_proc)
        _restore_store()
        _restore_registry()
    if score is None:
        raise RuntimeError("desktop JSON relaunch: no score")
    return replace(score, restored=True)


def load_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """Load a 16-bit PCM WAV as mono float32. Stdlib only — no ffmpeg."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if sample_width != 2:
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
