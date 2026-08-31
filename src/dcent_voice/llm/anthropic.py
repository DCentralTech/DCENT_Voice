# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Provide the Anthropic-compatible cleanup provider."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import httpx

from dcent_voice.asr.base import Locality
from dcent_voice.config import LLMSpec
from dcent_voice.llm.base import LLMProvider

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    """Anthropic-backed opt-in cloud cleanup provider."""

    locality = Locality.CLOUD

    def __init__(
        self,
        spec: LLMSpec,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        egress_logger: Callable[[str, str, int], None] | None = None,
    ) -> None:
        if spec.provider != "anthropic" or not spec.enabled:
            raise ValueError("AnthropicProvider requires an anthropic:<model> spec.")
        self.spec = spec
        self.model = spec.model or ""
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY or stored Anthropic credential is required.")
        self.provider_key = "llm:anthropic"
        self.egress_logger = egress_logger
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        payload = self._payload(system, user, temperature=temperature, max_tokens=max_tokens)
        data = self._post_messages(payload)
        return _content_text(data).strip()

    def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        structured_system = (
            system
            + "\nReturn only strict JSON matching this JSON Schema. Do not include markdown.\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        text = self.complete(structured_system, user, temperature=temperature, max_tokens=1024)
        return json.loads(text)

    def complete_tools(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
    ) -> list[Any]:
        payload = self._payload(system, user, temperature=0.0, max_tokens=1024)
        payload["tools"] = tools
        data = self._post_messages(payload)
        calls = []
        for item in data.get("content", []):
            if item.get("type") == "tool_use":
                calls.append(
                    {
                        "id": item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": json.dumps(item.get("input", {})),
                        },
                    }
                )
        return calls

    def health(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        self._client.close()

    def _payload(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    def _post_messages(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Anthropic is always a cloud provider.  The callback is therefore a
        # mandatory live-consent and durable audit boundary, including for
        # direct construction outside the application factory.
        if self.egress_logger is None:
            raise RuntimeError("Cloud LLM egress audit boundary is required.")
        byte_count = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.egress_logger(self.provider_key, "text", byte_count)
        response = self._client.post("/messages", json=payload)
        response.raise_for_status()
        return response.json()


def _content_text(data: dict[str, Any]) -> str:
    chunks = []
    for item in data.get("content", []):
        if item.get("type") == "text":
            chunks.append(str(item.get("text", "")))
    return "".join(chunks)
