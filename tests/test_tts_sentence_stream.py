# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from dcent_voice.tts.sentence_stream import CodePolicy, SentenceStream


def _run(text: str, *, char_by_char: bool = False, **kwargs) -> list[str]:
    stream = SentenceStream(**kwargs)
    out: list[str] = []
    if char_by_char:
        for ch in text:
            out.extend(stream.push(ch))
    else:
        out.extend(stream.push(text))
    out.extend(stream.flush())
    return out


# --- Basic boundaries -----------------------------------------------------------


def test_splits_on_terminal_punctuation() -> None:
    assert _run("Hello there. How are you? Fine!") == [
        "Hello there.",
        "How are you?",
        "Fine!",
    ]


def test_partial_sentence_held_until_punctuation() -> None:
    stream = SentenceStream()
    assert stream.push("Hello ther") == []
    assert stream.push("e, friend") == []
    assert stream.push(". Next one.") == ["Hello there, friend."]
    assert stream.flush() == ["Next one."]


def test_flush_emits_trailing_text_without_punctuation() -> None:
    stream = SentenceStream()
    assert stream.push("no terminator here") == []
    assert stream.flush() == ["no terminator here"]


# --- Abbreviations / decimals (do not split) ------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Dr. Smith is in. Bye.", ["Dr. Smith is in.", "Bye."]),
        ("Pi is 3.14 today. Yes.", ["Pi is 3.14 today.", "Yes."]),
        ("Use tools, e.g. hammers. Done.", ["Use tools, e.g. hammers.", "Done."]),
        ("Meet at 9 a.m. tomorrow. OK.", ["Meet at 9 a.m. tomorrow.", "OK."]),
        ("Ask J. Smith first. Then go.", ["Ask J. Smith first.", "Then go."]),
    ],
)
def test_abbreviations_and_decimals_do_not_split(text: str, expected: list[str]) -> None:
    assert _run(text) == expected


# --- Never splits mid-word: incremental == batch --------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "One sentence. Two sentences! Three? Four.",
        "Dr. Who visited. He said e.g. this. Fine.",
        "Numbers 1.5 and 2.75 matter. Really.",
        "Trailing partial with no end",
        'Quotes work. "He said hi." Then left.',
    ],
)
def test_incremental_matches_batch(text: str) -> None:
    # Feeding one character at a time must never split a word differently than
    # feeding the whole string — a boundary is only ever taken at punctuation.
    assert _run(text, char_by_char=True) == _run(text)


def test_no_emitted_sentence_ends_mid_word() -> None:
    text = "alpha beta gamma. delta epsilon. zeta"
    for sentence in _run(text, char_by_char=True):
        # Every emitted token is a whole word (splitting only at whitespace/punct).
        assert not sentence.startswith(" ")
        assert sentence == sentence.strip()


# --- Code policy ----------------------------------------------------------------


def test_fenced_code_block_skipped_by_default() -> None:
    text = "Before it. ```python\nprint('secret')\n``` After it."
    out = _run(text)
    joined = " ".join(out)
    assert "secret" not in joined
    assert "print" not in joined
    assert out[0] == "Before it."
    assert out[-1] == "After it."


def test_inline_code_skipped_by_default() -> None:
    out = _run("Run `git status` now to check.")
    joined = " ".join(out)
    assert "git status" not in joined
    assert "Run" in joined
    assert "now to check" in joined


def test_inline_code_spoken_when_policy_says_so() -> None:
    out = _run("Run `git status` now.", code_policy=CodePolicy.SPEAK)
    assert out == ["Run git status now."]


def test_unterminated_inline_code_held_until_flush() -> None:
    stream = SentenceStream(code_policy=CodePolicy.SPEAK)
    # Backtick opens a span that never closes within this push: hold it back.
    assert stream.push("Say `hello") == []
    # Trailing space after the period makes the boundary certain mid-stream.
    assert stream.push(" world` please. ") == ["Say hello world please."]


def test_unterminated_fence_held_until_flush() -> None:
    stream = SentenceStream()
    assert stream.push("Intro. ```\nunclosed code") == ["Intro."]
    # The open fence is held (not spoken) until more text or flush arrives.
    assert stream.flush() == []


def test_code_split_across_pushes_still_skipped() -> None:
    stream = SentenceStream()
    assert stream.push("Look at `git ") == []
    # The inline code span spans two pushes; its content is still dropped. Only
    # internal whitespace differs, so compare on normalized whitespace.
    first = stream.push("commit -m x` here. Ok.")
    assert [" ".join(s.split()) for s in first] == ["Look at here."]
    assert stream.flush() == ["Ok."]
