# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Turn spoken ramble into sendable writing. Local. Deterministic. No LLM.

This is the default-path rewrite DCENT_Voice applies before text lands:
fillers and false starts drop, mid-utterance self-corrections keep the last
intent, and clean read speech is left alone. Destination tone lives in style.py.
"""

from __future__ import annotations

import re

# Discourse that is almost never lexical content when used as a tag.
_DISCOURSE_PHRASES: tuple[str, ...] = (
    "if that makes sense",
    "to be honest",
    "to be fair",
    "at the end of the day",
    "for what it's worth",
)

# Leading stacks people use to start talking. Bare "so"/"well" stay — they are
# common in clean read speech (LibriSpeech) and in real writing.
_LEADING_STACK = re.compile(
    r"(?i)^(?:(?:okay|ok|alright|all right|anyway)(?:\s+so)?|um+|uh+|erm+|hmm+)\s+"
)

_TRAILING_YOU_KNOW = re.compile(r"(?i)(?:,\s*)?\byou know\b(?=\s*[,.!?]|$)")
_PARENTHETICAL_I_MEAN = re.compile(r"(?i),\s*I mean,")

_FALSE_START = re.compile(
    r"(?i)\b(?P<pron>I|we|they|he|she)\s+"
    r"(?:(?:just|really)\s+)?"
    r"(?:want to|wanted to|was going to|were going to|gonna|trying to|tried to|"
    r"need to|needed to|have to|had to|going to)\s+"
    r"(?P=pron)\b"
)

_NUMBER_WORDS = (
    "zero",
    "oh",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
    "hundred",
    "thousand",
)
_DAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
# May is too ambiguous ("I may actually go") — never treat it as a month here.

_NUM_TOKEN = rf"(?:\d+(?:\.\d+)?|{'|'.join(_NUMBER_WORDS)})"
_DAY_TOKEN = "|".join(_DAYS)
_MONTH_TOKEN = "|".join(_MONTHS)
_PARALLEL = rf"(?:{_NUM_TOKEN}|{_DAY_TOKEN}|{_MONTH_TOKEN})"
_CORRECTION_MARK = r"(?:actually|wait\s+no|no\s+wait|sorry|I\s+mean|or\s+rather|or\s+actually)"

_PARALLEL_CORRECTION = re.compile(rf"(?i)\b({_PARALLEL})\s+{_CORRECTION_MARK}\s+({_PARALLEL})\b")
# Lone "wait" is only a correction between numbers/days ("5 wait 6").
# Never fire on "could wait no longer".
_WAIT_PARALLEL = re.compile(
    rf"(?i)\b({_NUM_TOKEN}|{_DAY_TOKEN})\s+wait\s+({_NUM_TOKEN}|{_DAY_TOKEN})\b"
)

_TOKEN_CORRECTION = re.compile(rf"(?i)\b(\w+)\s+{_CORRECTION_MARK}\s+(\w+)\b")
_CMD_CORRECTION = re.compile(
    r"(?i)\b(git|npm|npx|pip|uv|cargo|pytest|docker|kubectl|pnpm|yarn|"
    r"gh|lncli|bitcoin-cli|lightning-cli)\s+\w+\s+"
    rf"{_CORRECTION_MARK}\s+\1\s+(\w+)\b"
)

# After "actually", these mean the word is a verb/content, not a replacement.
_CONTENT_VERBS = frozenset(
    {
        "like",
        "liked",
        "likes",
        "want",
        "wanted",
        "wants",
        "think",
        "thought",
        "thinks",
        "went",
        "go",
        "goes",
        "is",
        "was",
        "are",
        "were",
        "am",
        "be",
        "been",
        "do",
        "did",
        "does",
        "have",
        "had",
        "has",
        "need",
        "needed",
        "needs",
        "see",
        "saw",
        "sees",
        "know",
        "knew",
        "knows",
        "got",
        "get",
        "gets",
        "make",
        "made",
        "makes",
        "work",
        "works",
        "worked",
        "feel",
        "feels",
        "felt",
        "said",
        "say",
        "says",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
    }
)
_SUBJECT_PRONOUNS = frozenset({"i", "we", "they", "he", "she", "you", "it"})
_KEEP_DOUBLE = frozenset({"had", "that"})
# Generic token replacement only fires for uncommon words (names, brands).
# Function words and everyday nouns stay put so "status actually git" is not
# eaten and "could wait no" is never a correction.
_COMMON_ENGLISH = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "but",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "did",
        "does",
        "not",
        "no",
        "yes",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "my",
        "your",
        "our",
        "their",
        "his",
        "her",
        "with",
        "from",
        "by",
        "as",
        "if",
        "then",
        "than",
        "so",
        "just",
        "really",
        "very",
        "also",
        "only",
        "even",
        "still",
        "back",
        "over",
        "out",
        "up",
        "down",
        "off",
        "about",
        "into",
        "after",
        "before",
        "because",
        "when",
        "where",
        "which",
        "who",
        "what",
        "how",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "must",
        "run",
        "please",
        "send",
        "get",
        "got",
        "make",
        "made",
        "see",
        "know",
        "think",
        "want",
        "need",
        "like",
        "go",
        "going",
        "gone",
        "come",
        "came",
        "take",
        "took",
        "give",
        "gave",
        "use",
        "used",
        "work",
        "works",
        "status",
        "log",
        "file",
        "open",
        "save",
        "edit",
        "test",
        "look",
        "draft",
        "plan",
        "team",
        "build",
        "time",
        "meeting",
        "sort",
        "kind",
        "all",
        "except",
        "instead",
        "while",
        "poor",
        "said",
        "man",
        "sir",
        "exist",
        "here",
        "now",
        "one",
        "deck",
        "report",
        "wait",
        "longer",
    }
)

_STUTTER = re.compile(r"(?i)\b(\w+)\s+\1\b")
_PHRASE_STUTTER = re.compile(
    r"(?i)\b((?:I|we|they)\s+(?:think|guess|feel|know|said|believe))\s+\1\b"
)
# "the report I mean the deck" / "Tuesday I mean Thursday" — I-mean is a
# spoken correction even when both nouns are everyday words.
_I_MEAN_NOUN = re.compile(
    r"(?i)\b(?:the\s+)?(?P<old>\w+)\s+I mean\s+(?P<art>the\s+)?(?P<new>\w+)\b"
)
# "the report I mean the status update" — keep the last noun phrase.
_I_MEAN_THE_PHRASE = re.compile(
    r"(?i)\bthe\s+(?P<old>\w+)\s+I mean\s+the\s+(?P<new>\w+"
    r"(?:\s+(?!thanks\b|thank\b|please\b|because\b|and\b|but\b|so\b)\w+){0,2})\b"
)
_CLAUSE_RESTART = re.compile(
    r"(?i)(?<!\w)(?P<mark>"
    r"let me start over|"
    r"let me try that again|"
    r"let me try again|"
    r"better yet|"
    r"no wait|"
    r"wait no"
    r")(?!\s+longer)\s+"
)
_RESTART_VERBS = frozenset(
    {
        "send",
        "tell",
        "email",
        "write",
        "call",
        "ask",
        "make",
        "put",
        "use",
        "open",
        "run",
        "go",
        "meet",
        "ship",
        "update",
        "change",
        "replace",
        "draft",
        "move",
        "give",
        "set",
        "add",
        "drop",
        "keep",
        "try",
    }
)
# Left/right tokens that make "I mean" explanatory, not a last-noun swap.
# "what I mean by…", "when I mean business", "by that I mean the deck".
_I_MEAN_DISCOURSE_LEFT = frozenset(
    {
        "what",
        "when",
        "where",
        "why",
        "how",
        "if",
        "as",
        "because",
        "that",
        "this",
        "these",
        "those",
        "which",
        "who",
        "whom",
        "whose",
        "whether",
        "and",
        "or",
        "but",
        "so",
        "then",
        "just",
    }
)
_I_MEAN_DISCOURSE_RIGHT = frozenset(
    {
        "by",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "about",
        "as",
        "if",
        "that",
        "this",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "did",
        "does",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "must",
        "and",
        "or",
        "but",
        "so",
    }
)
_MULTI_SPACE = re.compile(r"[^\S\n\t]+")


def rewrite_speech(text: str) -> str:
    """Rewrite ramble into sendable writing. Conservative on clean prose."""
    if not text or not text.strip():
        return text

    result = text
    result = _drop_leading_stacks(result)
    result = _drop_discourse(result)
    result = _collapse_stutter(result)
    result = _PHRASE_STUTTER.sub(r"\1", result)
    result = _FALSE_START.sub(lambda match: match.group("pron"), result)
    result = _apply_corrections(result)
    result = _drop_discourse(result)
    result = _MULTI_SPACE.sub(" ", result)
    return result.strip()


# High is local sendable brevity, not a cloud rewrite. Drop hedges people
# use to start, interrupt, stack, or trail a thought. Leave content "I think"
# ("that's what I think", "What I think is important") and clean prose alone.
_THAT_CLAUSE = r"(?:I|we|they|he|she|you|it|the|this|these|those|my|our|their|a|an)"
_HIGH_LEAD = re.compile(
    r"(?i)^(?:"
    r"I just wanted to say that|I would like to say that|I would say that|"
    r"I was wondering if|it seems like|I feel like|"
    r"I think|I guess|I suppose"
    rf")(?:\s+that(?=\s+{_THAT_CLAUSE}\b))?(?:\s*,)?\s+"
)
_HIGH_MID = r"(?:I think|I guess|I suppose|I feel like|it seems(?: like)?)"
_HIGH_WORD = r"[A-Za-z]+(?:['’][A-Za-z]+)*"
_HIGH_PAREN = re.compile(rf"(?i),\s*{_HIGH_MID}(?:\s*,\s*{_HIGH_MID})*\s*,")
_HIGH_TRAIL = re.compile(
    rf"(?i)(?P<pre>{_HIGH_WORD})\s*(?P<comma>,\s*)?(?P<hedge>{_HIGH_MID})\s*(?P<end>[.!?])?$"
)
_HIGH_TRAIL_KEEP_PRE = frozenset(
    {
        "what",
        "that",
        "this",
        "these",
        "those",
        "which",
        "who",
        "whom",
        "whose",
        "how",
        "why",
        "when",
        "where",
        "if",
        "as",
        "than",
        "because",
        "whether",
    }
)
_HIGH_LEAD_KEEP_REMAINDER = frozenset({"so", "not", "yes", "no"})
_HIGH_INLINE = re.compile(
    rf"(?i)(?P<pre>{_HIGH_WORD})\s+(?P<hedge>{_HIGH_MID})\s+(?P<next>{_HIGH_WORD})"
)
_HIGH_INLINE_KEEP_PRE = _HIGH_TRAIL_KEEP_PRE | frozenset(
    {
        "do",
        "does",
        "did",
        "don't",
        "doesn't",
        "didn't",
        "dont",
        "doesnt",
        "didnt",
    }
)
_HIGH_INLINE_KEEP_NEXT = frozenset({"about", "of", "so", "not", "yes", "no"})
_HIGH_JUST = re.compile(r"(?i)\bjust\s+(?=wanted|need|needed|think|thought)\b")
_HIGH_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def rewrite_high(text: str) -> str:
    """Brevity pass after medium polish. Local. No LLM. Conservative on prose.

    High drops stacked leads, comma-wrapped and uncommaed mid-clause hedges,
    and trailing hedges. Content frames stay.
    """
    if not text or not text.strip():
        return text
    result = _HIGH_PAREN.sub(" ", text)
    result = " ".join(_rewrite_high_sentence(part) for part in _high_sentences(result))
    result = _HIGH_JUST.sub("", result)
    result = _MULTI_SPACE.sub(" ", result).strip()
    return result


def _high_sentences(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    return [part.strip() for part in _HIGH_SENT_SPLIT.split(stripped) if part.strip()]


def _rewrite_high_sentence(sentence: str) -> str:
    current = sentence
    while True:
        match = _HIGH_LEAD.match(current)
        if not match:
            break
        remainder = current[match.end() :].strip()
        core = remainder.rstrip(".!?").strip().lower()
        if not core or core in _HIGH_LEAD_KEEP_REMAINDER:
            break
        current = remainder
    current = _drop_high_inline(current)
    current = _drop_high_trailing(current)
    current = _MULTI_SPACE.sub(" ", current).strip()
    if current and current[0].isalpha() and current[0].islower():
        current = current[0].upper() + current[1:]
    return current


def _drop_high_inline(sentence: str) -> str:
    """Drop uncommaed mid-clause hedges. Spoken dictation rarely has commas."""

    def _keep_or_drop(match: re.Match[str]) -> str:
        hedge = match.group("hedge").lower()
        # Uncommaed "it seems" / "it seems like" is the verb, not a hedge.
        # ", it seems," still drops via _HIGH_PAREN.
        if hedge.startswith("it seems"):
            return match.group(0)
        pre = match.group("pre").lower().replace("’", "'")
        nxt = match.group("next").lower().replace("’", "'")
        if pre in _HIGH_INLINE_KEEP_PRE:
            return match.group(0)
        if nxt in _HIGH_INLINE_KEEP_NEXT:
            return match.group(0)
        return f"{match.group('pre')} {match.group('next')}"

    previous = None
    result = sentence
    while previous != result:
        previous = result
        result = _HIGH_INLINE.sub(_keep_or_drop, result)
    return result


def _drop_high_trailing(sentence: str) -> str:
    match = _HIGH_TRAIL.search(sentence)
    if not match:
        return sentence
    if match.group("pre").lower() in _HIGH_TRAIL_KEEP_PRE:
        return sentence
    end = match.group("end") or ""
    head = sentence[: match.start()] + match.group("pre")
    return (head.rstrip() + end).strip()


def _drop_leading_stacks(text: str) -> str:
    result = text
    previous = None
    while previous != result:
        previous = result
        result = _LEADING_STACK.sub("", result)
    return result


def _drop_discourse(text: str) -> str:
    result = text
    for phrase in _DISCOURSE_PHRASES:
        pattern = re.compile(rf"(?i)(?<!\w){re.escape(phrase)}(?!\w)[,.]?")
        result = pattern.sub(" ", result)
    result = _TRAILING_YOU_KNOW.sub("", result)
    result = _PARENTHETICAL_I_MEAN.sub(",", result)
    return result


def _collapse_stutter(text: str) -> str:
    result = text

    def _keep_or_drop(match: re.Match[str]) -> str:
        word = match.group(1)
        if word.lower() in _KEEP_DOUBLE:
            return match.group(0)
        return word

    previous = None
    while previous != result:
        previous = result
        result = _STUTTER.sub(_keep_or_drop, result)
    return result


def _apply_corrections(text: str) -> str:
    result = _CMD_CORRECTION.sub(r"\1 \2", text)
    result = _apply_i_mean_phrase(result)
    result = _apply_i_mean_noun(result)
    result = _PARALLEL_CORRECTION.sub(r"\2", result)
    result = _WAIT_PARALLEL.sub(r"\2", result)

    def _token(match: re.Match[str]) -> str:
        old = match.group(1)
        new = match.group(2)
        if new.lower() in _CONTENT_VERBS:
            return match.group(0)
        if old.lower() in _SUBJECT_PRONOUNS:
            return match.group(0)
        if old.lower() in _COMMON_ENGLISH or new.lower() in _COMMON_ENGLISH:
            return match.group(0)
        if new.isalpha() and new.islower():
            return new[:1].upper() + new[1:]
        return new

    result = _TOKEN_CORRECTION.sub(_token, result)
    return _apply_clause_restart(result)


def _apply_i_mean_phrase(text: str) -> str:
    def _swap(match: re.Match[str]) -> str:
        old = match.group("old")
        new = match.group("new")
        if old.lower() in _I_MEAN_DISCOURSE_LEFT:
            return match.group(0)
        first = new.split()[0].lower()
        if first in _I_MEAN_DISCOURSE_RIGHT or first in _CONTENT_VERBS:
            return match.group(0)
        return f"the {new}"

    return _I_MEAN_THE_PHRASE.sub(_swap, text)


def _apply_clause_restart(text: str) -> str:
    """Keep the last intent after 'no wait' / 'let me start over' / 'better yet'."""
    match = None
    for found in _CLAUSE_RESTART.finditer(text):
        match = found
    if match is None:
        return text
    lead = text[: match.start()].strip()
    rest = text[match.end() :].strip()
    if not lead or not rest:
        return text
    first = rest.split()[0].lower().rstrip(".,!?")
    words = rest.split()
    if first in _SUBJECT_PRONOUNS or first in _RESTART_VERBS or len(words) >= 3:
        return rest
    return text


def _apply_i_mean_noun(text: str) -> str:
    def _swap(match: re.Match[str]) -> str:
        old = match.group("old")
        new = match.group("new")
        old_key = old.lower()
        new_key = new.lower()
        if old_key in _SUBJECT_PRONOUNS or new_key in _SUBJECT_PRONOUNS:
            return match.group(0)
        if old_key in _I_MEAN_DISCOURSE_LEFT or new_key in _I_MEAN_DISCOURSE_RIGHT:
            return match.group(0)
        if new_key in _CONTENT_VERBS:
            return match.group(0)
        article = match.group("art") or ""
        written = new
        if written.isalpha() and written.islower() and old[:1].isupper():
            written = written[:1].upper() + written[1:]
        return f"{article}{written}"

    return _I_MEAN_NOUN.sub(_swap, text)
