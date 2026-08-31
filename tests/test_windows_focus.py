# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from dcent_voice.inject import windows_focus


def _target() -> windows_focus.WindowsFocusTarget:
    return windows_focus.WindowsFocusTarget(1, 2, 3, "Edit")


@pytest.mark.parametrize(
    "unchanged_polls", [0, 2, 24], ids=["grace_start", "grace_mid", "grace_end"]
)
def test_verified_paste_acknowledges_delayed_consumption_without_resend(
    monkeypatch, unchanged_polls
) -> None:
    before = windows_focus.TargetEditState("prefix", 6, 6)
    expected = windows_focus.TargetEditState("prefix🔥", 8, 8)
    states = iter((before, *([before] * unchanged_polls), expected))
    sends: list[int] = []
    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", lambda *_a, **_k: next(states))
    monkeypatch.setattr(
        windows_focus,
        "_send_target_message",
        lambda _target, message, **_kwargs: sends.append(message) or 0,
    )
    monkeypatch.setattr(windows_focus.time, "sleep", lambda _delay: None)

    windows_focus.send_targeted_paste(_target(), "🔥")

    assert sends == [0x0302]


def test_w24d_exact_backoff_schedule_yields_one_copy_and_one_send(monkeypatch) -> None:
    before = windows_focus.TargetEditState("A", 1, 1)
    expected = windows_focus.TargetEditState("AX", 2, 2)
    current = {"state": before}
    reads = 0
    sends: list[int] = []

    def read(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return current["state"]

    def send(_target, message, **_kwargs):
        sends.append(message)
        if len(sends) > 1:
            current["state"] = windows_focus.TargetEditState("AXX", 3, 3)
        return 0

    def consume_during_grace(_delay):
        current["state"] = expected

    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", read)
    monkeypatch.setattr(windows_focus, "_send_target_message", send)
    monkeypatch.setattr(windows_focus.time, "sleep", consume_during_grace)

    windows_focus.send_targeted_paste(_target(), "X")

    assert reads >= 3
    assert sends == [0x0302]
    assert current["state"] == expected


def test_verified_paste_partial_result_fails_without_retry(monkeypatch) -> None:
    before = windows_focus.TargetEditState("prefix", 6, 6)
    states = iter((before, before, windows_focus.TargetEditState("prefixx", 7, 7)))
    sends: list[int] = []
    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", lambda *_a, **_k: next(states))
    monkeypatch.setattr(
        windows_focus,
        "_send_target_message",
        lambda _target, message, **_kwargs: sends.append(message) or 0,
    )

    with pytest.raises(windows_focus.TargetedInjectionVerificationError, match="partial"):
        windows_focus.send_targeted_paste(_target(), "expected")

    assert sends == [0x0302]


def test_verified_paste_bounded_noop_never_resends_or_reports_success(monkeypatch) -> None:
    before = windows_focus.TargetEditState("prefix", 6, 6)
    sends: list[int] = []
    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", lambda *_a, **_k: before)
    monkeypatch.setattr(
        windows_focus,
        "_send_target_message",
        lambda _target, message, **_kwargs: sends.append(message) or 0,
    )
    ticks = iter((0.0, 0.0, 0.050))
    monkeypatch.setattr(windows_focus.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(windows_focus.time, "sleep", lambda _delay: None)

    with pytest.raises(windows_focus.TargetedInjectionVerificationError, match="single.*no-op"):
        windows_focus.send_targeted_paste(_target(), "expected")

    assert sends == [0x0302]


@pytest.mark.parametrize(
    "error",
    [
        windows_focus.TargetedInjectionVerificationError("target died"),
        windows_focus.TargetedInjectionVerificationError("identity changed"),
    ],
)
def test_verified_paste_target_failure_during_grace_never_resends(monkeypatch, error) -> None:
    before = windows_focus.TargetEditState("A", 1, 1)
    reads: list[int] = []
    sends: list[int] = []

    def read(*_args, **_kwargs):
        reads.append(1)
        if len(reads) >= 3:
            raise error
        return before

    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", read)
    monkeypatch.setattr(
        windows_focus,
        "_send_target_message",
        lambda _target, message, **_kwargs: sends.append(message) or 0,
    )
    monkeypatch.setattr(windows_focus.time, "sleep", lambda _delay: None)

    with pytest.raises(windows_focus.TargetedInjectionVerificationError, match=str(error)):
        windows_focus.send_targeted_paste(_target(), "X")

    assert sends == [0x0302]


def test_verified_replace_requires_exact_utf16_text_and_selection(monkeypatch) -> None:
    states = iter(
        (
            windows_focus.TargetEditState("A🔥B", 1, 3),
            windows_focus.TargetEditState("A日本B", 3, 3),
        )
    )
    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", lambda *_a, **_k: next(states))
    monkeypatch.setattr(windows_focus, "_send_target_message", lambda *_a, **_k: 0)

    windows_focus.send_targeted_edit_text(_target(), "日本")


def test_chromium_renderer_hwnd_churn_allows_same_window_page_search(
    monkeypatch,
) -> None:
    press = windows_focus.WindowsFocusTarget(10, 20, 3, "Chrome_RenderWidgetHostHWND")
    current = windows_focus.WindowsFocusTarget(10, 99, 3, "Chrome_RenderWidgetHostHWND")
    monkeypatch.setattr(windows_focus, "capture_foreground_target", lambda: current)
    monkeypatch.setattr(
        "dcent_voice.inject.windows_uia.focused_page_search_field",
        lambda _names: object(),
    )
    assert windows_focus.focus_is_unchanged(press) is True
    monkeypatch.setattr(
        "dcent_voice.inject.windows_uia.focused_page_search_field",
        lambda _names: None,
    )
    assert windows_focus.focus_is_unchanged(press) is False
    notepad_press = windows_focus.WindowsFocusTarget(10, 20, 3, "Edit")
    notepad_now = windows_focus.WindowsFocusTarget(10, 99, 3, "Edit")
    monkeypatch.setattr(windows_focus, "capture_foreground_target", lambda: notepad_now)
    assert windows_focus.focus_is_unchanged(notepad_press) is False


def test_capture_target_from_hwnd_rejects_empty() -> None:
    assert windows_focus.capture_target_from_hwnd(0) is None


def test_verified_replace_rejects_unexpected_result(monkeypatch) -> None:
    states = iter(
        (
            windows_focus.TargetEditState("old", 0, 3),
            windows_focus.TargetEditState("", 0, 0),
        )
    )
    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", lambda *_a, **_k: next(states))
    monkeypatch.setattr(windows_focus, "_send_target_message", lambda *_a, **_k: 0)

    with pytest.raises(windows_focus.TargetedInjectionVerificationError, match="exact expected"):
        windows_focus.send_targeted_edit_text(_target(), "new")


def test_verified_paste_accepts_windows_crlf_for_lf_paragraphs(monkeypatch) -> None:
    before = windows_focus.TargetEditState("DCENT-hold-em", 0, 13)
    inserted = "Hey,\n\nCould you send a report?\n\nThanks,"
    crlf = inserted.replace("\n", "\r\n")
    observed = windows_focus.TargetEditState(crlf, len(crlf), len(crlf))
    states = iter((before, observed))
    sends: list[int] = []
    monkeypatch.setattr(windows_focus, "read_targeted_edit_state", lambda *_a, **_k: next(states))
    monkeypatch.setattr(
        windows_focus,
        "_send_target_message",
        lambda _target, message, **_kwargs: sends.append(message) or 0,
    )

    windows_focus.send_targeted_paste(_target(), inserted)

    assert sends == [0x0302]


def test_edit_states_match_rejects_different_paragraphs() -> None:
    expected = windows_focus.TargetEditState("Hey,\n\nThanks,", 13, 13)
    observed = windows_focus.TargetEditState("Hey,\r\n\r\nLater,", 15, 15)
    assert windows_focus._edit_states_match(expected, expected) is True
    assert windows_focus._edit_states_match(observed, expected) is False
