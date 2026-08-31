# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Incremental text → sentence-sized synthesis units.

ADE streams assistant tokens as they are generated; a TTS backend wants whole
sentences (synthesizing a half-word is audible and wrong). :class:`SentenceStream`
buffers incremental text and emits complete sentences as soon as a boundary is
certain, so playback starts on sentence 1 while sentence 2 is still arriving.

Guarantees exercised by the property tests:
- never splits inside a word — a boundary is only taken after sentence-ending
  punctuation followed by whitespace (or end-of-input on ``flush``);
- abbreviations (``Dr.``, ``e.g.``) and decimals (``3.14``) do not end a sentence;
- code is handled per :class:`CodePolicy` — fenced ```` ``` ```` blocks and inline
  ```code``` spans are skipped by default (spoken code is noise), or kept
  verbatim when the policy says to speak it. Unterminated spans are held back
  until they close or ``flush`` forces them.
"""

from __future__ import annotations

from enum import Enum

_SENTENCE_END = ".!?…"  # . ! ? …
_CLOSERS = "\"')]}”’"  # closing quotes/brackets that ride along after end punctuation
_FENCE = "```"

# Lowercased tokens (the run of letters/dots before a period) that do NOT end a
# sentence. Dotted forms like "e.g" are matched after stripping the trailing dot.
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "sr",
        "jr",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "u.s",
        "u.k",
        "a.m",
        "p.m",
        "no",
        "fig",
        "al",
        "inc",
        "ltd",
        "co",
        "dept",
        "approx",
        "gen",
        "sen",
        "rep",
        "gov",
        "capt",
        "sgt",
        "lt",
        "cmdr",
        "messrs",
        "mt",
        "ave",
        "blvd",
    }
)


class CodePolicy(Enum):
    """What to do with code content while chunking text for speech."""

    SKIP = "skip"  # drop fenced blocks and inline code spans entirely (default)
    SPEAK = "speak"  # keep the code text (backticks/fences removed) and speak it


class SentenceStream:
    """Stateful incremental sentence splitter.

    Feed text with :meth:`push` (returns any sentences completed so far) and call
    :meth:`flush` at end-of-utterance to drain the trailing partial sentence.
    """

    def __init__(self, *, code_policy: CodePolicy = CodePolicy.SKIP) -> None:
        self.code_policy = code_policy
        self._raw = ""  # unscanned tail (may hold an unterminated code span)
        self._pending = ""  # clean text accumulated but not yet a full sentence

    def push(self, text: str) -> list[str]:
        """Add incremental text; return sentences that are now complete."""
        if not text:
            return []
        self._raw += text
        clean, consumed = _scan_code(self._raw, self.code_policy, final=False)
        self._raw = self._raw[consumed:]
        self._pending += clean
        sentences, self._pending = _split_sentences(self._pending, final=False)
        return sentences

    def flush(self) -> list[str]:
        """Drain everything buffered, forcing any open code span and trailing text."""
        clean, consumed = _scan_code(self._raw, self.code_policy, final=True)
        self._raw = self._raw[consumed:]
        self._pending += clean
        sentences, self._pending = _split_sentences(self._pending, final=True)
        self._raw = ""
        return sentences


def _scan_code(buf: str, policy: CodePolicy, *, final: bool) -> tuple[str, int]:
    """Strip/keep code spans from ``buf``.

    Returns ``(clean_text, consumed)`` where ``consumed`` is how many characters of
    ``buf`` were fully processed. When an unterminated code span is found and
    ``final`` is ``False``, scanning stops at its start so the caller can wait for
    more input; on ``final`` the span is closed at end-of-buffer.
    """

    out: list[str] = []
    i = 0
    n = len(buf)
    while i < n:
        if buf.startswith(_FENCE, i) and _at_line_start(buf, i):
            close = buf.find(_FENCE, i + len(_FENCE))
            if close == -1:
                if not final:
                    return "".join(out), i
                out.append(_fenced_text(buf[i + len(_FENCE) :], policy))
                return "".join(out), n
            inner = buf[i + len(_FENCE) : close]
            out.append(_fenced_text(inner, policy))
            i = close + len(_FENCE)
            continue
        ch = buf[i]
        if ch == "`":
            run = _run_length(buf, i, "`")
            close = _find_run(buf, i + run, run)
            if close == -1:
                if not final:
                    return "".join(out), i
                if policy is CodePolicy.SPEAK:
                    out.append(buf[i + run :])
                else:
                    out.append(" ")
                return "".join(out), n
            if policy is CodePolicy.SPEAK:
                out.append(buf[i + run : close])
            else:
                out.append(" ")
            i = close + run
            continue
        out.append(ch)
        i += 1
    return "".join(out), n


def _fenced_text(inner: str, policy: CodePolicy) -> str:
    if policy is not CodePolicy.SPEAK:
        return " "
    # Drop the info string on the opening fence line (e.g. ```python).
    body = inner.split("\n", 1)[1] if "\n" in inner else ""
    return " " + body + " "


def _at_line_start(buf: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and buf[j] in " \t":
        j -= 1
    return j < 0 or buf[j] == "\n"


def _run_length(buf: str, i: int, ch: str) -> int:
    n = len(buf)
    j = i
    while j < n and buf[j] == ch:
        j += 1
    return j - i


def _find_run(buf: str, start: int, run: int) -> int:
    """Index of the next backtick run of exactly ``run`` length at/after ``start``."""
    i = start
    n = len(buf)
    while i < n:
        if buf[i] == "`":
            length = _run_length(buf, i, "`")
            if length == run:
                return i
            i += length
        else:
            i += 1
    return -1


def _split_sentences(text: str, *, final: bool) -> tuple[list[str], str]:
    """Split ``text`` into complete sentences plus a trailing remainder."""

    sentences: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] not in _SENTENCE_END:
            i += 1
            continue
        j = i + 1
        while j < n and text[j] in _SENTENCE_END:
            j += 1
        while j < n and text[j] in _CLOSERS:
            j += 1
        nxt = text[j] if j < n else ""
        at_boundary = nxt.isspace() or (nxt == "" and final)
        if at_boundary and not _is_non_boundary(text, i):
            sentence = text[start:j].strip()
            if sentence:
                sentences.append(sentence)
            while j < n and text[j].isspace():
                j += 1
            start = j
            i = j
            continue
        i = j
    remainder = text[start:]
    if final:
        tail = remainder.strip()
        if tail:
            sentences.append(tail)
        remainder = ""
    return sentences, remainder


def _is_non_boundary(text: str, dot: int) -> bool:
    """True if the period at ``dot`` is an abbreviation/decimal, not a sentence end."""

    if text[dot] != ".":
        return False
    # Decimal number: digit . digit (the digit-after case only arises mid-stream).
    if dot > 0 and text[dot - 1].isdigit():
        nxt = text[dot + 1] if dot + 1 < len(text) else ""
        if nxt.isdigit():
            return True
    token = _preceding_token(text, dot)
    if not token:
        return False
    if token in _ABBREVIATIONS:
        return True
    # A single letter before the dot is an initial (e.g. "J. Smith").
    return len(token) == 1 and token.isalpha()


def _preceding_token(text: str, dot: int) -> str:
    j = dot - 1
    while j >= 0 and (text[j].isalpha() or text[j] == "."):
        j -= 1
    return text[j + 1 : dot].strip(".").lower()
