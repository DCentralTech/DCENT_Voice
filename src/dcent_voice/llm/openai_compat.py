# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Provide OpenAI-compatible cleanup API integration."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx

from dcent_voice.asr.base import Locality
from dcent_voice.config import LLMSpec
from dcent_voice.llm.base import LLMProvider

DEFAULT_BASE_URLS = {
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
}

DEFAULT_API_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
}


class OpenAICompatProvider(LLMProvider):
    """OpenAI-compatible cleanup provider for local or opted-in endpoints."""

    def __init__(
        self,
        spec: LLMSpec,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 15.0,
        keep_alive: str = "2m",
        transport: httpx.BaseTransport | None = None,
        egress_logger: Callable[[str, str, int], None] | None = None,
    ) -> None:
        if not spec.enabled:
            raise ValueError("OpenAICompatProvider requires an enabled LLM spec.")
        self.spec = spec
        self.provider_name = spec.provider
        self.model = spec.model or ""
        self.base_url = (base_url or DEFAULT_BASE_URLS.get(spec.provider) or "").rstrip("/")
        if not self.base_url:
            raise ValueError(f"No default base URL for LLM provider {spec.provider!r}.")
        if spec.locality is Locality.LOCAL:
            _validate_loopback_base_url(self.base_url)
        self.api_key = api_key or _api_key_from_env(spec.provider)
        self.timeout_s = timeout_s
        self.keep_alive = keep_alive
        self.locality = spec.locality
        self.provider_key = f"llm:{spec.provider}"
        self.egress_logger = egress_logger
        # Local servers (LM Studio / Ollama) often are simply not running; fail
        # connect fast so cleanup can fall back to the raw transcript quickly.
        local = spec.provider in {"ollama", "lmstudio"}
        timeout = (
            httpx.Timeout(timeout_s, connect=1.2 if local else min(5.0, timeout_s))
            if transport is None
            else timeout_s
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=self._headers(),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        payload = self._chat_payload(system, user, temperature=temperature, max_tokens=max_tokens)
        data = self._post_chat(payload)
        return _message_content(data).strip()

    def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        payload = self._chat_payload(system, user, temperature=temperature, max_tokens=1024)
        if self.provider_name == "ollama":
            payload["format"] = schema
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "dcent_response", "schema": schema, "strict": True},
            }
        data = self._post_chat(payload)
        content = _message_content(data)
        return json.loads(content)

    def complete_tools(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
    ) -> list[Any]:
        payload = self._chat_payload(system, user, temperature=0.0, max_tokens=1024)
        payload["tools"] = tools
        data = self._post_chat(payload)
        # `[{}]` default only guards a MISSING key; a present-but-empty
        # `"choices": []` (content-filtered / edge responses) would still
        # IndexError, so coalesce the empty list too.
        message = (data.get("choices") or [{}])[0].get("message", {})
        return list(message.get("tool_calls") or [])

    def health(self) -> bool:
        if self.locality is Locality.CLOUD:
            # The logger is also the live-consent enforcement boundary.  Keep
            # the probe metadata-only: no transcript or credential content is
            # sent in the request body, so record a zero-byte egress attempt.
            # A manually constructed cloud provider without that boundary must
            # fail closed rather than making an unaudited request.
            if self.egress_logger is None:
                return False
            self.egress_logger(self.provider_key, "text", 0)
        try:
            # Health probes must never inherit the much longer generation read
            # timeout, including for cloud providers with a responsive connect
            # but a stalled response body.
            response = self._client.get("/models", timeout=httpx.Timeout(2.0, connect=2.0))
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()

    def _chat_payload(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.provider_name == "ollama":
            payload["keep_alive"] = self.keep_alive
        return payload

    def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.locality is Locality.CLOUD:
            # The logger is the live-consent and durable audit boundary, not an
            # optional observer.  Directly constructed cloud providers must
            # fail closed before touching the network when that boundary is
            # absent, revoked, corrupt, or otherwise unable to record.
            if self.egress_logger is None:
                raise RuntimeError("Cloud LLM egress audit boundary is required.")
            byte_count = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            self.egress_logger(self.provider_key, "text", byte_count)
        response = self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def _message_content(data: dict[str, Any]) -> str:
    return str((data.get("choices") or [{}])[0].get("message", {}).get("content", ""))


def _api_key_from_env(provider: str) -> str | None:
    env = DEFAULT_API_KEY_ENVS.get(provider)
    return os.environ.get(env) if env else None


def _validate_loopback_base_url(base_url: str) -> None:
    """Reject local-provider endpoints that could send transcripts off-device."""

    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Local LLM base URL must be a valid loopback HTTP(S) URL.") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise ValueError("Local LLM base URL must be a loopback HTTP(S) URL with a port.")
    if hostname == "localhost":
        return
    try:
        address = ip_address(hostname)
    except ValueError as exc:
        raise ValueError("Local LLM base URL must use a loopback host.") from exc
    if not address.is_loopback:
        raise ValueError("Local LLM base URL must use a loopback host.")
