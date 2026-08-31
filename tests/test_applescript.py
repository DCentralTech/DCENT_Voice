# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""AppleScript string-literal quoting (review MINOR-1).

Both macOS dialogs used ``json.dumps`` to build an ``osascript -e`` fragment.
JSON and AppleScript agree on ``\\\\``, ``\\"``, ``\\n``, ``\\r`` and ``\\t`` —
and disagree on everything else. With ``ensure_ascii=True`` (the default) JSON
emits ``\\uXXXX``, which AppleScript does not define, so it drops the backslash
and shows the escape as text. Every fatal dialog title we produce contains an em
dash, so this was visible on literally every macOS failure surface.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from dcent_voice.util.applescript import escape, quote


def test_plain_text_is_returned_between_quotes() -> None:
    assert quote("hello") == '"hello"'


def test_unicode_survives_verbatim() -> None:
    """The actual bug: json.dumps turned this em dash into the text u2014."""
    assert quote("DCENT_Voice — could not start") == '"DCENT_Voice — could not start"'
    assert "\\u" not in quote("DCENT_Voice — could not start")


@pytest.mark.parametrize("text", ["•", "…", "café", "→", "日本語", "emoji 🎤"])
def test_every_non_ascii_character_passes_through(text: str) -> None:
    assert escape(text) == text


def test_double_quotes_are_escaped() -> None:
    assert quote('say "hi"') == '"say \\"hi\\""'


def test_backslashes_are_escaped() -> None:
    assert quote("C:\\path") == '"C:\\\\path"'


def test_backslash_is_escaped_before_the_quote_it_precedes() -> None:
    r"""A trailing backslash must not escape the literal's own closing quote."""
    assert quote("ends with\\") == '"ends with\\\\"'


def test_a_literal_backslash_n_is_not_turned_into_a_newline() -> None:
    r"""The two characters \n must stay two characters, not become a line break."""
    assert quote("literal \\n here") == '"literal \\\\n here"'


def test_newlines_become_applescript_escapes() -> None:
    """AppleScript literals cannot span lines: a raw newline is a syntax error."""
    assert quote("one\ntwo") == '"one\\ntwo"'
    assert "\n" not in quote("one\ntwo")


def test_carriage_returns_and_tabs_are_escaped() -> None:
    assert quote("a\r\nb") == '"a\\nb"'
    assert quote("a\rb") == '"a\\nb"'
    assert quote("a\tb") == '"a\\tb"'


def test_control_characters_become_spaces() -> None:
    """A NUL or bell has no AppleScript escape and would corrupt the literal."""
    assert quote("a\x00b\x07c") == '"a b c"'


def test_the_real_dialog_text_produces_a_single_line_literal() -> None:
    """The paragraphs the first-run dialog shows must not break the -e argument."""
    from dcent_voice.ui import first_run

    class _Hotkeys:
        dictation = "ctrl+win"
        mode = "hold"

    class _Config:
        hotkeys = _Hotkeys()

    text = first_run.dialog_text(_Config(), gui_missing=True, platform="darwin")
    assert "\n" in text, "the fixture must actually contain newlines"
    literal = quote(text)
    assert "\n" not in literal
    assert "\\u" not in literal
    # The bullets in the education copy are the characters json.dumps mangled.
    assert "•" in literal


def test_both_call_sites_use_the_shared_helper() -> None:
    """Neither macOS dialog may drift back to json.dumps."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "dcent_voice"
    for relative in ("util/fatal.py", "ui/first_run.py"):
        source = (root / relative).read_text(encoding="utf-8")
        start = source.index("def _show_macos_dialog")
        body = source[start : start + 900]
        assert "from dcent_voice.util.applescript import quote" in body, relative
        # Ignore comments: both call sites deliberately *name* json.dumps to
        # explain why it is wrong here.
        code = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
        assert "json.dumps(" not in code, relative


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript only exists on macOS")
def test_osascript_round_trips_the_escaped_text() -> None:
    """The only test that proves the grammar rather than asserting our own rules."""
    text = 'DCENT_Voice — "quoted" \\ back\nsecond • line'
    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/osascript", "-e", f"return {quote(text)}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    # osascript prints a returned string with real newlines collapsed to \n.
    assert completed.stdout.strip() == text.replace("\n", "\\n")
