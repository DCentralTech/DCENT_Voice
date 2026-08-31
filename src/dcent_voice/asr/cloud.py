# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Provide consent-gated cloud transcription providers."""

from __future__ import annotations

import io
import os
import time
import wave
from collections.abc import Callable
from typing import Any

import httpx
import numpy as np

from dcent_voice.asr.base import (
    WHISPER_LANGUAGE_CODES,
    ASRProvider,
    Locality,
    TranscriptResult,
)
from dcent_voice.config import ASRSpec


class CloudASRProvider(ASRProvider):
    """Base class for consent-gated cloud transcription providers."""

    locality = Locality.CLOUD
    supports_per_call_language = True
    supports_language_auto_detection = True

    def __init__(
        self,
        spec: ASRSpec,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        egress_logger: Callable[[str, str, int], None] | None = None,
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self.transport = transport
        self.egress_logger = egress_logger

    def load(self) -> None:
        return None

    def unload(self) -> None:
        return None

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        raise NotImplementedError

    def _record_egress(self, byte_count: int) -> None:
        if self.egress_logger is None:
            raise RuntimeError("Cloud ASR requires a consent-enforcing metadata egress logger.")
        self.egress_logger(f"asr:{self.spec.provider}", "audio", byte_count)


DEEPGRAM_NOVA3_LANGUAGE_CODES = frozenset(
    """ar hy be bn bs bg ca zh hr cs da nl en et fi fr de el gu he hi hu id it
    ja kn ko lv lt mk ms mr ne no fa pl pt pa ro ru sr sk sl es sv tl ta te th
    tr uk ur vi""".split()  # noqa: SIM905 - auditable provider table
)
DEEPGRAM_NOVA2_LANGUAGE_CODES = frozenset(
    """bg ca zh cs da nl en et fi fr de el hi hu id it ja ko lv lt ms no pl pt
    ro ru sk es sv th tr uk vi""".split()  # noqa: SIM905
)
XAI_LANGUAGE_CODES = frozenset(
    """ar cs da nl en fil fr de hi id it ja ko mk ms fa pl pt ro ru es sv th tr
    vi""".split()  # noqa: SIM905 - auditable provider table
)
DEEPGRAM_FLUX_MULTI_LANGUAGE_CODES = frozenset(
    {"de", "en", "es", "fr", "hi", "it", "ja", "nl", "pt", "ru"}
)


class DeepgramASRProvider(CloudASRProvider):
    """Deepgram-backed opt-in cloud transcription provider."""

    def __init__(self, spec: ASRSpec, **kwargs: Any) -> None:
        super().__init__(
            spec,
            api_key=kwargs.pop("api_key", None) or os.environ.get("DEEPGRAM_API_KEY"),
            **kwargs,
        )
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY or stored Deepgram credential is required.")

    @property
    def supported_language_codes(self) -> frozenset[str]:
        model = self.spec.model.strip().lower()
        if model == "nova-3-medical":
            return frozenset({"en"})
        if model == "nova-3" or model.startswith("nova-3-general"):
            return DEEPGRAM_NOVA3_LANGUAGE_CODES
        if model == "nova-2" or model.startswith("nova-2-general"):
            return DEEPGRAM_NOVA2_LANGUAGE_CODES
        if model == "flux-general-multi":
            return DEEPGRAM_FLUX_MULTI_LANGUAGE_CODES
        # Specialized/unknown models are not assumed multilingual. This is a
        # fail-closed hint contract; callers can select a documented model.
        return frozenset({"en"})

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        language_n = self.validate_language_hint(language)
        wav = float_audio_to_wav_bytes(audio, samplerate)
        start = time.perf_counter()
        url = f"https://api.deepgram.com/v1/listen?model={self.spec.model}&smart_format=true"
        if language_n:
            url += f"&language={language_n}"
        elif language is not None:
            # Deepgram omission defaults to English. Explicit auto/detect must
            # opt into its documented prerecorded language detector.
            url += "&detect_language=true"
        headers = {"Authorization": f"Token {self.api_key}", "Content-Type": "audio/wav"}
        with httpx.Client(
            transport=self.transport,
            timeout=30.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            self._record_egress(len(wav))
            response = client.post(url, content=wav, headers=headers)
            response.raise_for_status()
            data = response.json()
        text = (
            data.get("results", {})
            .get("channels", [{}])[0]
            .get("alternatives", [{}])[0]
            .get("transcript", "")
        )
        return TranscriptResult(
            text=text,
            language="",
            duration_s=len(np.asarray(audio).reshape(-1)) / float(samplerate),
            asr_latency_s=time.perf_counter() - start,
        )


class OpenAITranscriptionProvider(CloudASRProvider):
    """OpenAI-backed opt-in cloud transcription provider."""

    supported_language_codes = WHISPER_LANGUAGE_CODES

    def __init__(self, spec: ASRSpec, **kwargs: Any) -> None:
        super().__init__(
            spec,
            api_key=kwargs.pop("api_key", None) or os.environ.get("OPENAI_API_KEY"),
            **kwargs,
        )
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY or stored OpenAI credential is required.")

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        language_n = self.validate_language_hint(language)
        wav = float_audio_to_wav_bytes(audio, samplerate)
        start = time.perf_counter()
        files = {"file": ("audio.wav", wav, "audio/wav")}
        data = {"model": self.spec.model}
        if initial_prompt:
            data["prompt"] = initial_prompt
        if language_n:
            data["language"] = language_n
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(
            transport=self.transport,
            timeout=30.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            self._record_egress(len(wav))
            response = client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                files=files,
                data=data,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        return TranscriptResult(
            text=str(payload.get("text", "")),
            language=str(payload.get("language", "")),
            duration_s=len(np.asarray(audio).reshape(-1)) / float(samplerate),
            asr_latency_s=time.perf_counter() - start,
        )


class XaiTranscriptionProvider(CloudASRProvider):
    """xAI Grok Speech-to-Text. Opt-in cloud only; never the default."""

    supported_language_codes = XAI_LANGUAGE_CODES

    def __init__(self, spec: ASRSpec, **kwargs: Any) -> None:
        super().__init__(
            spec,
            api_key=kwargs.pop("api_key", None) or os.environ.get("XAI_API_KEY"),
            **kwargs,
        )
        if not self.api_key:
            raise RuntimeError("XAI_API_KEY or stored xAI credential is required.")

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        language_n = self.validate_language_hint(language)
        wav = float_audio_to_wav_bytes(audio, samplerate)
        start = time.perf_counter()
        files = {"file": ("audio.wav", wav, "audio/wav")}
        data: dict[str, str] = {}
        # With formatting enabled, xAI uses this explicit language for inverse
        # text normalization; the model name is not a language fallback.
        if language_n:
            data["language"] = language_n
            data["format"] = "true"
        params = httpx.QueryParams(
            [("keyterm", term) for term in _keyterms(initial_prompt, hotwords)]
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(
            transport=self.transport,
            timeout=30.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            self._record_egress(len(wav))
            response = client.post(
                "https://api.x.ai/v1/stt",
                files=files,
                data=data or None,
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        return TranscriptResult(
            text=str(payload.get("text", "")),
            language=str(payload.get("language", "")),
            duration_s=len(np.asarray(audio).reshape(-1)) / float(samplerate),
            asr_latency_s=time.perf_counter() - start,
        )


class GroqTranscriptionProvider(OpenAITranscriptionProvider):
    """Groq-backed opt-in cloud transcription provider."""

    def __init__(self, spec: ASRSpec, **kwargs: Any) -> None:
        CloudASRProvider.__init__(
            self,
            spec,
            api_key=kwargs.pop("api_key", None) or os.environ.get("GROQ_API_KEY"),
            **kwargs,
        )
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY or stored Groq credential is required.")
        self.supported_language_codes = (
            frozenset({"en"})
            if self.spec.model.strip().lower().endswith("-en")
            else WHISPER_LANGUAGE_CODES
        )

    def transcribe(
        self,
        audio: Any,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        language_n = self.validate_language_hint(language)
        wav = float_audio_to_wav_bytes(audio, samplerate)
        start = time.perf_counter()
        files = {"file": ("audio.wav", wav, "audio/wav")}
        data = {"model": self.spec.model}
        if initial_prompt:
            data["prompt"] = initial_prompt
        if language_n:
            data["language"] = language_n
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(
            transport=self.transport,
            timeout=30.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            self._record_egress(len(wav))
            response = client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                files=files,
                data=data,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        return TranscriptResult(
            text=str(payload.get("text", "")),
            language=str(payload.get("language", "")),
            duration_s=len(np.asarray(audio).reshape(-1)) / float(samplerate),
            asr_latency_s=time.perf_counter() - start,
        )


def build_cloud_asr_provider(
    spec: ASRSpec,
    *,
    api_key: str | None = None,
    egress_logger: Callable[[str, str, int], None] | None = None,
) -> CloudASRProvider:
    if spec.provider == "deepgram":
        return DeepgramASRProvider(spec, api_key=api_key, egress_logger=egress_logger)
    if spec.provider == "openai":
        return OpenAITranscriptionProvider(spec, api_key=api_key, egress_logger=egress_logger)
    if spec.provider == "groq":
        return GroqTranscriptionProvider(spec, api_key=api_key, egress_logger=egress_logger)
    if spec.provider == "xai":
        return XaiTranscriptionProvider(spec, api_key=api_key, egress_logger=egress_logger)
    raise ValueError(f"Unsupported cloud ASR provider: {spec.provider}")


def _keyterms(initial_prompt: str | None, hotwords: str | None) -> list[str]:
    """Turn dictionary/prompt hints into xAI ``keyterm`` values (max 100×50)."""
    seen: list[str] = []
    for blob in (hotwords, initial_prompt):
        if not blob:
            continue
        for part in blob.replace(",", " ").replace(".", " ").split():
            term = part.strip()
            if not term or term in seen or len(term) > 50:
                continue
            seen.append(term)
            if len(seen) >= 100:
                return seen
    return seen


def float_audio_to_wav_bytes(audio: Any, samplerate: int = 16000) -> bytes:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(pcm)
    return output.getvalue()
