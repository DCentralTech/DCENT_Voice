# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Model dictation mode transitions and application state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from dcent_voice.events import AppMode


class AppState(Enum):
    IDLE = "idle"
    RECORDING_DICTATION = "recording_dictation"
    RECORDING_COMMAND = "recording_command"
    RECORDING_STREAMING = "recording_streaming"
    PROCESSING = "processing"


class InvalidTransition(RuntimeError):
    pass


@dataclass
class ModeStateMachine:
    """Validates and publishes dictation mode transitions."""

    state: AppState = AppState.IDLE
    active_mode: AppMode | None = None

    def press(self, mode: AppMode) -> AppState:
        if self.state is AppState.IDLE:
            self.active_mode = mode
            self.state = _recording_state_for(mode)
            return self.state
        if self.is_recording and self.active_mode is mode:
            return self.state
        raise InvalidTransition(f"Cannot press {mode.value} while {self.state.value}.")

    def release(self, mode: AppMode) -> AppState:
        if self.is_recording and self.active_mode is mode:
            self.state = AppState.PROCESSING
            return self.state
        if self.state is AppState.IDLE:
            return self.state
        raise InvalidTransition(f"Cannot release {mode.value} while {self.state.value}.")

    def finish_processing(self) -> AppState:
        if self.state is not AppState.PROCESSING:
            raise InvalidTransition(f"Cannot finish processing while {self.state.value}.")
        self.state = AppState.IDLE
        self.active_mode = None
        return self.state

    def cancel(self) -> AppState:
        self.state = AppState.IDLE
        self.active_mode = None
        return self.state

    @property
    def is_recording(self) -> bool:
        return self.state in {
            AppState.RECORDING_DICTATION,
            AppState.RECORDING_COMMAND,
            AppState.RECORDING_STREAMING,
        }


def _recording_state_for(mode: AppMode) -> AppState:
    if mode is AppMode.DICTATION:
        return AppState.RECORDING_DICTATION
    if mode is AppMode.COMMAND:
        return AppState.RECORDING_COMMAND
    if mode is AppMode.STREAMING:
        return AppState.RECORDING_STREAMING
    raise InvalidTransition(f"{mode.value} is not supported by the state machine.")
