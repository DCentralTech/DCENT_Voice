# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from dcent_voice.pipeline import IncrementalCommitter


def test_committer_only_emits_words_agreed_across_three_passes() -> None:
    c = IncrementalCommitter(agreement_passes=3)
    assert c.update("the quick") == ""
    assert c.update("the quick brown") == ""
    # Longest common prefix of the three windows is "the quick".
    assert c.update("the quick brown") == "the quick"
    # Last three windows now share "the quick brown" → commit "brown".
    assert c.update("the quick brown fox") == "brown"
    assert c.update("the quick brown fox") == ""
    # Three windows share through "fox".
    assert c.update("the quick brown fox jumps") == "fox"
    assert c.finalize("the quick brown fox jumps high") == "jumps high"


def test_committer_does_not_emit_words_that_get_revised() -> None:
    c = IncrementalCommitter(agreement_passes=3)
    c.update("i scream")
    # Growing/revised openings are not identical repeats, so first-emit
    # shortcut does not fire. Nothing is safe until 3-pass LCP agrees.
    assert c.update("ice cream") == ""
    assert c.update("ice cream") == ""
    assert c.update("ice cream cone") == "ice cream"


def test_committer_identical_repeat_emits_first_words_on_second_pass() -> None:
    c = IncrementalCommitter(agreement_passes=3, first_agreement_passes=2)
    assert c.update("hello world") == ""
    assert c.update("hello world") == "hello world"
    assert c.update("hello world friend") == ""
    assert c.update("hello world friend") == ""
    assert c.update("hello world friend") == "friend"


def test_committer_never_double_emits() -> None:
    c = IncrementalCommitter(agreement_passes=3)
    assert c.update("hello there") == ""
    assert c.update("hello there friend") == ""
    assert c.update("hello there friend") == "hello there"
    assert c.update("hello there friend") == "friend"
    assert c.finalize("hello there friend") == ""


def test_committer_two_pass_mode_still_available() -> None:
    c = IncrementalCommitter(agreement_passes=2)
    assert c.update("the quick") == ""
    assert c.update("the quick brown") == "the quick"
