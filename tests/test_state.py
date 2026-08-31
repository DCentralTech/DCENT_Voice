# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from dcent_voice.events import AppMode
from dcent_voice.state import AppState, InvalidTransition, ModeStateMachine


def test_dictation_press_release_finish() -> None:
    machine = ModeStateMachine()

    assert machine.press(AppMode.DICTATION) is AppState.RECORDING_DICTATION
    assert machine.release(AppMode.DICTATION) is AppState.PROCESSING
    assert machine.finish_processing() is AppState.IDLE
    assert machine.active_mode is None


def test_command_press_release_finish() -> None:
    machine = ModeStateMachine()

    assert machine.press(AppMode.COMMAND) is AppState.RECORDING_COMMAND
    assert machine.release(AppMode.COMMAND) is AppState.PROCESSING
    assert machine.finish_processing() is AppState.IDLE


def test_cannot_start_second_mode_while_recording() -> None:
    machine = ModeStateMachine()
    machine.press(AppMode.DICTATION)

    with pytest.raises(InvalidTransition):
        machine.press(AppMode.COMMAND)


def test_idle_release_is_ignored() -> None:
    machine = ModeStateMachine()

    assert machine.release(AppMode.DICTATION) is AppState.IDLE
