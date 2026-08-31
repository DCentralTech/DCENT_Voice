# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Apply user-editable runtime settings safely."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceRuntimeSettings(BaseSettings):
    """Environment layer for service bootstrap and tests.

    The TOML config remains the user-facing source, while these settings cover
    process-level overrides such as fake-audio mode and registry location.
    """

    model_config = SettingsConfigDict(env_prefix="DCENT_VOICE_", extra="ignore")

    fake_audio: bool = False
    registry_dir: str | None = None
    service_host: str = Field(default="127.0.0.1")
    service_port: int = Field(default=8765, ge=1, le=65535)
