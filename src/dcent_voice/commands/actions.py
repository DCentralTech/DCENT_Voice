# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Execute validated voice-command actions."""

from __future__ import annotations

from dcent_voice.commands.schema import CommandIntent
from dcent_voice.inject.base import Injector


class CommandExecutor:
    """Executes validated local voice-command actions."""

    def __init__(self, injector: Injector) -> None:
        self.injector = injector

    def execute(self, intent: CommandIntent) -> bool:
        if intent.action in {"rewrite_selection", "insert_text"} and intent.text:
            self.injector.inject(intent.text)
            return True
        return False

    def execute_into_target(self, intent: CommandIntent, target: object) -> bool:
        """Execute text against the focus target captured when the command began."""
        if intent.action not in {"rewrite_selection", "insert_text"} or not intent.text:
            return False
        targeted = getattr(self.injector, "inject_into_target", None)
        if callable(targeted):
            targeted(intent.text, target)
        else:
            self.injector.inject(intent.text)
        return True
