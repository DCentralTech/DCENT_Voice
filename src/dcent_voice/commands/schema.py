# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Define structured voice-command request and intent models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CommandAction = Literal["rewrite_selection", "insert_text", "tool_call", "noop"]


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CommandIntent(BaseModel):
    action: CommandAction = "noop"
    text: str = ""
    tool_call: ToolCall | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


COMMAND_INTENT_SCHEMA = CommandIntent.model_json_schema()
