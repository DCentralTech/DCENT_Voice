# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Define the language-model cleanup provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from dcent_voice.asr.base import Locality


class LLMProvider(ABC):
    """Protocol implemented by transcript-cleanup language-model providers."""

    locality: Locality

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        """Return a plain text completion."""

    @abstractmethod
    def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Return a dict matching the requested JSON schema."""

    @abstractmethod
    def complete_tools(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
    ) -> list[Any]:
        """Return tool calls. The concrete ToolCall schema lands in W3."""

    @abstractmethod
    def health(self) -> bool:
        """Return whether the provider is reachable."""
