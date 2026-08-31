# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Describe provider authentication and consent capabilities."""

from __future__ import annotations

from dataclasses import dataclass

from dcent_voice.auth.base import AuthMode


@dataclass(frozen=True)
class ConsentDescriptor:
    provider_key: str
    payload_type: str


@dataclass(frozen=True)
class ProviderCapability:
    name: str
    display_name: str
    locality: str
    auth_modes: tuple[AuthMode, ...]
    consent_descriptors: tuple[ConsentDescriptor, ...] = ()
    policy_url: str = ""
    docs_url: str = ""
    data_note: str = ""

    @property
    def supports_oauth(self) -> bool:
        return AuthMode.OAUTH_PKCE in self.auth_modes or AuthMode.DEVICE_CODE in self.auth_modes

    @property
    def supports_api_key(self) -> bool:
        return AuthMode.API_KEY in self.auth_modes


# Verified against official provider docs on 2026-07-06. Current public API
# access for these cloud providers is API-key based for this app's use case.
PROVIDERS: dict[str, ProviderCapability] = {
    "ollama": ProviderCapability(
        name="ollama",
        display_name="Ollama",
        locality="local",
        auth_modes=(AuthMode.NONE,),
        docs_url="https://github.com/ollama/ollama/blob/main/docs/api.md",
        data_note="Local HTTP service; prompts stay on this machine.",
    ),
    "lmstudio": ProviderCapability(
        name="lmstudio",
        display_name="LM Studio",
        locality="local",
        auth_modes=(AuthMode.NONE,),
        docs_url="https://lmstudio.ai/docs",
        data_note="Local OpenAI-compatible service; prompts stay on this machine.",
    ),
    "openai": ProviderCapability(
        name="openai",
        display_name="OpenAI",
        locality="cloud",
        auth_modes=(AuthMode.API_KEY,),
        consent_descriptors=(
            ConsentDescriptor("llm:openai", "text"),
            ConsentDescriptor("asr:openai", "audio"),
        ),
        policy_url="https://openai.com/policies/row-privacy-policy/",
        docs_url="https://platform.openai.com/docs/api-reference",
        data_note="Text sent to OpenAI API endpoints when selected.",
    ),
    "anthropic": ProviderCapability(
        name="anthropic",
        display_name="Anthropic",
        locality="cloud",
        auth_modes=(AuthMode.API_KEY,),
        consent_descriptors=(ConsentDescriptor("llm:anthropic", "text"),),
        policy_url="https://www.anthropic.com/legal/privacy",
        docs_url="https://docs.anthropic.com/en/docs/get-started",
        data_note="Text sent to Anthropic API endpoints when selected.",
    ),
    "groq": ProviderCapability(
        name="groq",
        display_name="Groq",
        locality="cloud",
        auth_modes=(AuthMode.API_KEY,),
        consent_descriptors=(
            ConsentDescriptor("llm:groq", "text"),
            ConsentDescriptor("asr:groq", "audio"),
        ),
        policy_url="https://groq.com/privacy-policy/",
        docs_url="https://console.groq.com/keys",
        data_note="Text or audio sent to Groq API endpoints when selected.",
    ),
    "xai": ProviderCapability(
        name="xai",
        display_name="Grok (xAI)",
        locality="cloud",
        # OpenAI-compatible API; xAI also offers OAuth device-code sign-in tied to
        # a SuperGrok / X Premium+ subscription (accounts.x.ai).
        auth_modes=(AuthMode.DEVICE_CODE, AuthMode.API_KEY),
        consent_descriptors=(
            ConsentDescriptor("llm:xai", "text"),
            ConsentDescriptor("asr:xai", "audio"),
        ),
        policy_url="https://x.ai/legal/privacy-policy",
        docs_url="https://docs.x.ai/developers/model-capabilities/audio/speech-to-text",
        data_note="Text or audio sent to xAI (Grok) API endpoints when selected.",
    ),
    "deepgram": ProviderCapability(
        name="deepgram",
        display_name="Deepgram",
        locality="cloud",
        auth_modes=(AuthMode.API_KEY,),
        consent_descriptors=(ConsentDescriptor("asr:deepgram", "audio"),),
        policy_url="https://deepgram.com/privacy",
        docs_url="https://developers.deepgram.com/guides/fundamentals/authenticating",
        data_note="Audio sent to Deepgram when selected for ASR.",
    ),
}


def get_provider_capability(name: str) -> ProviderCapability | None:
    return PROVIDERS.get(name.lower())


def list_provider_capabilities() -> list[ProviderCapability]:
    return list(PROVIDERS.values())
