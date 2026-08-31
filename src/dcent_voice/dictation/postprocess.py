# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Offline dictation post-processing for natural speech edits and polish.

These transforms run entirely on-device after ASR and dictionary application.
They never contact a network and never require an LLM. Optional LLM cleanup
still runs after this stage when the user has enabled it.

Goals vs commercial references (behavioral parity, not copying):
- Spoken corrections / backtracking ("scratch that", "delete last word")
- Voice punctuation and structure ("new line", "bullet", "question mark")
- Snippet expansion (spoken cue → expanded text)
- Lightweight local polish (fillers, capitalization, end punctuation)
- Ramble rewrite (false starts, mid-utterance "5 actually 6")
- Developer-aware spoken forms (file extensions, common CLI tokens)
"""

from __future__ import annotations

import re
from typing import Protocol


class SnippetLike(Protocol):
    @property
    def spoken(self) -> str: ...

    @property
    def expansion(self) -> str: ...


class VocabLike(Protocol):
    @property
    def spoken(self) -> str: ...

    @property
    def written(self) -> str: ...

    @property
    def starred(self) -> bool: ...


# Fillers removed only as whole tokens (not substrings of real words).
# Only pure vocalized fillers — never content adverbs like "basically".
_FILLER_RE = re.compile(
    r"(?<!\w)(?:"
    r"um+|uh+|erm+|hmm+|mm+|"
    r"uh\s+huh|"
    r"oh\s+um"
    r")(?!\w)[,.]?",
    re.IGNORECASE,
)

# Trailing clause removers — longest first so multi-word phrases win.
_SCRATCH_PHRASES = (
    "scratch that",
    "delete that",
    "undo that",
    "forget that",
    "never mind",
    "nevermind",
    "disregard that",
    "cancel that",
    "ignore that",
)
_UNDO_LAST_COMMANDS = frozenset(
    (
        *_SCRATCH_PHRASES,
        "undo last",
        "undo last dictation",
        "scratch last",
        "delete last",
    )
)
_LEADING_COMMAND_FILLER = re.compile(r"(?is)^(um+|uh+|erm+|hmm+|oh|please|just)\s+")

_DELETE_LAST_WORD = re.compile(
    r"(?i)(?<!\w)(?:delete|remove|drop)\s+(?:the\s+)?last\s+word(?!\w)[.!?]?\s*$"
)
_DELETE_LAST_SENTENCE = re.compile(
    r"(?i)(?<!\w)(?:delete|remove|drop)\s+(?:the\s+)?last\s+sentence(?!\w)[.!?]?\s*$"
)
_DELETE_LAST_LINE = re.compile(
    r"(?i)(?<!\w)(?:delete|remove|drop)\s+(?:the\s+)?last\s+line(?!\w)[.!?]?\s*$"
)

# Spoken punctuation / structure → written forms (applied as whole phrases).
# Prefer multi-word cues so everyday English is not mangled
# (e.g. bare "period" would corrupt "period of growth").
_SPOKEN_TOKENS: tuple[tuple[str, str], ...] = (
    ("new paragraph", "\n\n"),
    ("next paragraph", "\n\n"),
    ("new line", "\n"),
    ("next line", "\n"),
    ("line break", "\n"),
    ("new sentence", ". "),
    ("next sentence", ". "),
    ("open parenthesis", "("),
    ("close parenthesis", ")"),
    ("open paren", "("),
    ("close paren", ")"),
    ("open bracket", "["),
    ("close bracket", "]"),
    ("open brace", "{"),
    ("close brace", "}"),
    ("open quote", '"'),
    ("close quote", '"'),
    ("open single quote", "'"),
    ("close single quote", "'"),
    ("question mark", "?"),
    ("exclamation mark", "!"),
    ("exclamation point", "!"),
    ("semi colon", ";"),
    ("semicolon", ";"),
    ("colon mark", ":"),
    ("ellipsis", "..."),
    ("dot dot dot", "..."),
    ("comma", ","),
    ("full stop", "."),
    ("period mark", "."),
    ("apostrophe", "'"),
    ("hyphen", "-"),
    ("em dash", "—"),
    ("forward slash", "/"),
    ("back slash", "\\"),
    ("backslash", "\\"),
    ("at sign", "@"),
    ("hash sign", "#"),
    ("percent sign", "%"),
    ("ampersand", "&"),
    ("asterisk", "*"),
    ("underscore", "_"),
    ("equals sign", "="),
    ("plus sign", "+"),
    ("bullet point", "\n- "),
    ("next bullet", "\n- "),
    ("insert bullet", "\n- "),
    ("number one", "\n1. "),
    ("number two", "\n2. "),
    ("number three", "\n3. "),
    ("number four", "\n4. "),
    ("number five", "\n5. "),
    ("insert tab", "\t"),
    ("press tab", "\t"),
    ("space bar", " "),
    ("double space", "  "),
)

# Developer-oriented spoken → written (applied after general tokens).
# Avoid bare words that are common English ("arrow", "pipe", "tilde").
_DEV_TOKENS: tuple[tuple[str, str], ...] = (
    ("dot py", ".py"),
    ("dot js", ".js"),
    ("dot ts", ".ts"),
    ("dot tsx", ".tsx"),
    ("dot jsx", ".jsx"),
    ("dot json", ".json"),
    ("dot md", ".md"),
    ("dot toml", ".toml"),
    ("example tom l", "example.toml"),
    ("tom l", "toml"),
    ("dot yaml", ".yaml"),
    ("dot yml", ".yml"),
    ("dot html", ".html"),
    ("dot css", ".css"),
    ("dot rs", ".rs"),
    ("dot go", ".go"),
    ("dot sh", ".sh"),
    ("dot env", ".env"),
    ("dot git", ".git"),
    ("slash bin slash", "/bin/"),
    ("double colon", "::"),
    ("fat arrow", "=>"),
    ("thin arrow", "->"),
    ("pipe symbol", "|"),
    ("tilde symbol", "~"),
    ("git status", "git status"),
    ("git commit", "git commit"),
    ("git push", "git push"),
    ("git pull", "git pull"),
    ("git add", "git add"),
    ("npm install", "npm install"),
    ("pip install", "pip install"),
    ("cargo build", "cargo build"),
    ("pytest", "pytest"),
    ("localhost", "localhost"),
    ("one twenty seven dot zero dot zero dot one", "127.0.0.1"),
    ("double u double u double u", "www"),
    ("h t t p s", "https"),
    ("h t t p", "http"),
    ("dot net", ".net"),
    ("dot com", ".com"),
    ("dot org", ".org"),
    ("dot io", ".io"),
    ("c plus plus", "C++"),
    ("c sharp", "C#"),
    ("type script", "TypeScript"),
    ("java script", "JavaScript"),
    ("vs code", "VS Code"),
    ("pull request", "pull request"),
    ("p r number", "PR #"),
    ("p test", "pytest"),
    ("pie test", "pytest"),
    ("p-test", "pytest"),
    ("ptest", "pytest"),
    ("bitcoin c l i", "bitcoin-cli"),
    ("bitcoin cli", "bitcoin-cli"),
    ("lightning c l i", "lightning-cli"),
    ("lightning cli", "lightning-cli"),
    ("l n c l i", "lncli"),
    ("ln c l i", "lncli"),
    ("lncli", "lncli"),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MULTI_SPACE = re.compile(r"[^\S\n\t]+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[{])\s+")
_SPACE_BEFORE_CLOSE = re.compile(r"\s+([)\]}])")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"\S+")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_dictation_postprocess(
    text: str,
    *,
    snippets: tuple[SnippetLike, ...] = (),
    dictionary: tuple[VocabLike, ...] = (),
    polish: bool = True,
    spoken_edits: bool = True,
    developer_terms: bool = True,
    cleanup_level: str = "medium",
) -> str:
    """Full offline post-ASR path used by the dictation pipeline.

    Order is intentional:
    1. Spoken structure/punctuation (so "period scratch that" works)
    2. Developer spoken forms
    3. User dictionary spoken→written
    4. Snippet expansion
    5. Backtracking / scratch-that edits
    6. Ramble rewrite (false starts, mid-utterance corrections)
    7. Local polish (fillers, capitalization, spacing)
    """
    if not text or not text.strip():
        return text

    result = text
    if spoken_edits:
        result = apply_spoken_tokens(result, include_dev=developer_terms)
    if developer_terms:
        result = _repair_cli_get_as_git(result)
    from dcent_voice.dictation.vocab import apply_shipped_vocab

    result = apply_shipped_vocab(result)
    if dictionary:
        from dcent_voice.pipeline import apply_dictionary

        result = apply_dictionary(result, dictionary)
    if snippets:
        result = apply_snippets(result, snippets)
    if spoken_edits:
        result = apply_spoken_edits(result)
    held: tuple[tuple[str, str], ...] = ()
    if snippets or dictionary:
        result, held = _hold_snippet_expansions(result, snippets, dictionary)
    level = _normalize_cleanup_level(cleanup_level, polish=polish)
    if level in {"medium", "high"}:
        from dcent_voice.dictation.rewrite import rewrite_high, rewrite_speech

        result = rewrite_speech(result)
        result = local_polish(result)
        if level == "high":
            result = rewrite_high(result)
    elif level == "light":
        result = local_polish(result)
    if held:
        result = _restore_snippet_expansions(result, held)
    return result


def _normalize_cleanup_level(value: str | None, *, polish: bool) -> str:
    if not polish:
        return "none"
    name = (value or "medium").strip().lower()
    aliases = {
        "off": "none",
        "on": "medium",
        "full": "high",
        "default": "medium",
    }
    name = aliases.get(name, name)
    if name not in {"none", "light", "medium", "high"}:
        return "medium"
    return name


def is_undo_last_command(text: str) -> bool:
    """True when the whole utterance is an undo-last command, not in-line scratch.

    Hold · say ``scratch that`` / ``undo that`` with nothing else · release
    retracts the previous insert. ``hello world scratch that`` still clears only
    this utterance.
    """
    working = (text or "").strip().lower()
    if not working:
        return False
    working = re.sub(r"[.!?]+$", "", working).strip()
    while True:
        nxt = _LEADING_COMMAND_FILLER.sub("", working).strip()
        if nxt == working:
            break
        working = nxt
    return working in _UNDO_LAST_COMMANDS


_CLEANUP_LEVEL_WORDS = {
    "none": "none",
    "off": "none",
    "light": "light",
    "medium": "medium",
    "high": "high",
    "full": "high",
}
_CLEANUP_LEVEL_ALT = "none|off|light|medium|high|full"
_LEADING_CLEANUP_CUE = re.compile(
    r"(?is)^\s*(?:please\s+|just\s+)?"
    r"(?:"
    r"(?:auto\s+)?cleanup(?:\s+level)?\s+(?P<named>" + _CLEANUP_LEVEL_ALT + r")"
    r"|"
    r"(?P<before>" + _CLEANUP_LEVEL_ALT + r")\s+cleanup"
    r"|"
    r"no\s+cleanup"
    r"|"
    r"without\s+cleanup"
    r")"
    r"(?:\s*[:—,-]\s*|\s+)"
)
_FALSE_CLEANUP_REMAINDER = re.compile(r"(?is)^(is|are|was|were|of|for|cost|costs|fee|fees|the)\b")


_TRAILING_PRESS_ENTER = re.compile(
    r"(?is)(?:^|\s+)"
    r"(?:please\s+|just\s+)?"
    r"(?:press|hit)\s+(?:the\s+)?(?:enter|return)(?:\s+key)?"
    r"(?:\s+please)?"
    r"[.!?]*\s*$"
)


def peel_spoken_press_enter(text: str) -> tuple[bool, str]:
    """Strip a trailing spoken Enter cue. Does not rewrite the body.

    Hold · say ``… press enter`` · release inserts the rest, then sends Enter.
    Said alone, only Enter is sent. Mid-utterance ``press enter to continue``
    is left alone. Not a cloud submit.
    """
    source = text or ""
    working = source.strip()
    if not working:
        return False, source
    while True:
        nxt = _LEADING_COMMAND_FILLER.sub("", working).strip()
        if nxt == working:
            break
        working = nxt
    match = _TRAILING_PRESS_ENTER.search(working)
    if not match:
        return False, source
    rest = working[: match.start()].rstrip(" \t,;:")
    return True, rest


def peel_spoken_cleanup(text: str) -> tuple[str | None, str]:
    """Strip a leading spoken Auto Cleanup cue. Does not rewrite the body.

    Overlay chips are read-only. Saying ``cleanup high …`` or ``no cleanup …``
    selects a local cleanup level for this utterance only. Not a cloud
    Auto Cleanup service. Content like ``cleanup the kitchen`` is left alone.
    """
    source = text or ""
    working = source.strip()
    if not working:
        return None, source
    while True:
        nxt = _LEADING_COMMAND_FILLER.sub("", working).strip()
        if nxt == working:
            break
        working = nxt
    match = _LEADING_CLEANUP_CUE.match(working)
    if not match:
        return None, source
    rest = working[match.end() :].strip()
    if not rest or _FALSE_CLEANUP_REMAINDER.match(rest):
        return None, source
    lowered = match.group(0).lower()
    if "no cleanup" in lowered or "without cleanup" in lowered:
        return "none", rest
    name = match.group("named") or match.group("before")
    level = _CLEANUP_LEVEL_WORDS.get((name or "").lower())
    if not level:
        return None, source
    return level, rest


def compose_dictation(
    text: str,
    *,
    style: str = "plain",
    snippets: tuple[SnippetLike, ...] = (),
    dictionary: tuple[VocabLike, ...] = (),
    polish: bool = True,
    spoken_edits: bool = True,
    developer_terms: bool = True,
    cleanup_level: str = "medium",
) -> str:
    """Postprocess locally, then apply the selected destination style."""
    from dcent_voice.dictation.style import apply_style, peel_spoken_style

    spoken_cleanup, text = peel_spoken_cleanup(text)
    if spoken_cleanup:
        cleanup_level = spoken_cleanup
    _, text = peel_spoken_press_enter(text)
    result = apply_dictation_postprocess(
        text,
        snippets=snippets,
        dictionary=dictionary,
        polish=polish,
        spoken_edits=spoken_edits,
        developer_terms=developer_terms,
        cleanup_level=cleanup_level,
    )
    spoken, peeled = peel_spoken_style(result)
    if spoken:
        style = spoken
        result = peeled
    held: tuple[tuple[str, str], ...] = ()
    if snippets or dictionary:
        result, held = _hold_snippet_expansions(result, snippets, dictionary)
    result = apply_style(result, style)
    if held:
        result = _restore_snippet_expansions(result, held)
    return result


def apply_spoken_tokens(text: str, *, include_dev: bool = True) -> str:
    """Replace spoken punctuation and structure phrases with written forms."""
    result = text
    tokens = _SPOKEN_TOKENS + (_DEV_TOKENS if include_dev else ())
    # Sort by spoken length descending so "new paragraph" beats "new".
    ordered = sorted(tokens, key=lambda pair: len(pair[0]), reverse=True)
    for spoken, written in ordered:
        pattern = re.compile(rf"(?<!\w){re.escape(spoken)}(?!\w)", re.IGNORECASE)

        # Function repl avoids re interpreting backslashes in written forms
        # (e.g. "\\" for spoken "backslash").
        def _replace_token(_match: re.Match[str], replacement: str = written) -> str:
            return replacement

        result = pattern.sub(_replace_token, result)
    return _glue_spoken_punctuation(result)


def _glue_spoken_punctuation(text: str) -> str:
    """Tighten spacing around tokens produced by spoken punctuation/dev forms."""
    result = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    # "app .py" → "app.py" for spoken file extensions.
    result = re.sub(r"(?<=\w)\s+(\.[A-Za-z0-9]+)\b", r"\1", result)
    # "word \n- item" → "word\n- item"
    result = re.sub(r"[^\S\n]+\n", "\n", result)
    result = re.sub(r"\n[^\S\n]+", "\n", result)
    # Space after comma/colon/semicolon when glued to the next word.
    # Do NOT insert space after "." — that would break file extensions (app.py).
    result = re.sub(r"([,;])(?=\S)", r"\1 ", result)
    # Do not break URL schemes: "https://example" must not become "https: //example".
    result = re.sub(r":(?!//)(?=\S)", ": ", result)
    result = re.sub(r"([!?])(?=[A-Za-z])", r"\1 ", result)
    result = _MULTI_SPACE.sub(" ", result)
    return result


def apply_snippets(text: str, snippets: tuple[SnippetLike, ...]) -> str:
    """Expand configured voice snippets. Longer cues first; starred wins when cues conflict."""
    if not text or not snippets:
        return text
    result = text
    ordered = sorted(
        snippets,
        key=lambda s: (
            -len((s.spoken or "").strip()),
            0 if getattr(s, "starred", False) else 1,
        ),
    )
    for entry in ordered:
        spoken = (entry.spoken or "").strip()
        expansion = entry.expansion if entry.expansion is not None else ""
        if not spoken or not expansion.strip():
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(spoken)}(?!\w)", re.IGNORECASE)

        def _replace_snippet(_match: re.Match[str], replacement: str = expansion) -> str:
            return replacement

        result = pattern.sub(_replace_snippet, result)
    return result


def _hold_snippet_expansions(
    text: str,
    snippets: tuple[SnippetLike, ...] = (),
    dictionary: tuple[VocabLike, ...] = (),
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Swap protected dictionary and snippet forms for polish-safe tokens."""
    if not text:
        return text, ()
    expansions: list[str] = []
    seen: set[str] = set()
    for snippet in snippets or ():
        expansion = (getattr(snippet, "expansion", None) or "").strip()
        if not expansion or expansion in seen:
            continue
        seen.add(expansion)
        expansions.append(expansion)
    for vocab in dictionary or ():
        written = (getattr(vocab, "written", None) or "").strip()
        if not written or written in seen:
            continue
        seen.add(written)
        expansions.append(written)
    if not expansions:
        return text, ()
    expansions.sort(key=len, reverse=True)
    held: list[tuple[str, str]] = []
    result = text
    for index, expansion in enumerate(expansions):
        if expansion not in result:
            continue
        token = f"\ue000{index}\ue001"
        result = result.replace(expansion, token)
        held.append((token, expansion))
    return result, tuple(held)


def _restore_snippet_expansions(text: str, held: tuple[tuple[str, str], ...]) -> str:
    result = text
    for token, expansion in held:
        result = result.replace(token, expansion)
    return result


_REPLACE_WITH = re.compile(
    r"(?i)(?<!\w)(?:replace|change)\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)\s*$"
)
_NO_I_MEANT = re.compile(r"(?i)(?<!\w)(?:(?:no|know)[, ]+)?i\s+meant\s+(?P<correction>.+?)\s*$")
_REMEMBER_AS = re.compile(
    r"(?i)(?<!\w)remember\s+(?P<old>.+?)\s+(?:as|is written)\s+(?P<new>.+?)\s*$"
)
_CORRECT_LAST = re.compile(
    r"(?i)^(?:correct that to|that should have been|make that)\s+(?P<fix>.+?)\s*$"
)


def extract_last_correction(text: str) -> str | None:
    """If this utterance corrects the previous one, return the intended text."""
    if not text or not text.strip():
        return None
    match = _CORRECT_LAST.match(text.strip())
    if not match:
        return None
    fix = match.group("fix").strip()
    return fix or None


def extract_spoken_corrections(text: str) -> tuple[tuple[str, str], ...]:
    """Return (spoken, written) pairs implied by in-utterance voice edits.

    Used to grow the local personalization store. Never includes audio.
    """
    if not text or not text.strip():
        return ()
    result = text.strip()
    pairs: list[tuple[str, str]] = []

    replace_match = _REPLACE_WITH.search(result)
    if replace_match:
        old = replace_match.group("old").strip()
        new = replace_match.group("new").strip()
        if old and new and old.lower() != new.lower():
            pairs.append((old, new))

    remember = _REMEMBER_AS.search(result)
    if remember:
        old = remember.group("old").strip()
        new = remember.group("new").strip()
        if old and new and old.lower() != new.lower():
            pairs.append((old, new))

    meant = _NO_I_MEANT.search(result)
    if meant:
        body = result[: meant.start()].rstrip(" ,;:-")
        correction = meant.group("correction").strip()
        words = _WORD_RE.findall(body)
        if correction and words:
            n = max(1, len(correction.split()))
            spoken = " ".join(words[-n:])
            if spoken and spoken.lower() != correction.lower():
                pairs.append((spoken, correction))
    return tuple(pairs)


def apply_spoken_edits(text: str) -> str:
    """Apply backtracking and deletion voice commands within one utterance."""
    if not text:
        return text
    result = text.strip()

    # "replace X with Y" / "change X with Y" at end of utterance.
    replace_match = _REPLACE_WITH.search(result)
    if replace_match:
        body = result[: replace_match.start()].rstrip(" ,;:-")
        old = replace_match.group("old").strip()
        new = replace_match.group("new").strip()
        if old:
            pattern = re.compile(re.escape(old), re.IGNORECASE)
            body = pattern.sub(new, body, count=1) if body else new
            return body.strip()

    # "no I meant CORRECTION" (ASR may hear "know I meant") — replace last token.
    meant = _NO_I_MEANT.search(result)
    if meant:
        body = result[: meant.start()].rstrip(" ,;:-")
        correction = meant.group("correction").strip()
        words = _WORD_RE.findall(body)
        body = " ".join(words[:-1]) if words else ""
        return f"{body} {correction}".strip() if body else correction

    # Delete last word/sentence/line commands at end of utterance.
    if _DELETE_LAST_WORD.search(result):
        result = _DELETE_LAST_WORD.sub("", result).rstrip()
        words = _WORD_RE.findall(result)
        result = " ".join(words[:-1]) if words else ""
        return result.strip()

    if _DELETE_LAST_SENTENCE.search(result):
        result = _DELETE_LAST_SENTENCE.sub("", result).rstrip()
        return _drop_last_sentence(result)

    if _DELETE_LAST_LINE.search(result):
        result = _DELETE_LAST_LINE.sub("", result).rstrip()
        lines = result.split("\n")
        return "\n".join(lines[:-1]).rstrip() if lines else ""

    # Repeated "scratch that" — process from the end, each removes prior clause.
    changed = True
    while changed:
        changed = False
        lower = result.lower()
        for phrase in _SCRATCH_PHRASES:
            idx = lower.rfind(phrase)
            if idx < 0:
                continue
            # Require phrase as whole words (rfind already found it; bound check).
            end = idx + len(phrase)
            if idx > 0 and lower[idx - 1].isalnum():
                continue
            if end < len(lower) and lower[end].isalnum():
                continue
            before = result[:idx].rstrip(" ,;:-")
            after = result[end:].lstrip(" ,;:-")
            # Scratch removes the immediately preceding sentence/clause.
            before = _drop_last_clause(before)
            result = f"{before} {after}".strip() if after else before
            changed = True
            break

    return result.strip()


def local_polish(text: str) -> str:
    """Deterministic offline cleanup: fillers, spacing, capitalization, periods."""
    if not text or not text.strip():
        return text

    result = text
    result = _FILLER_RE.sub(" ", result)
    result = _normalize_whitespace(result)
    result = _SPACE_BEFORE_PUNCT.sub(r"\1", result)
    result = _SPACE_AFTER_OPEN.sub(r"\1", result)
    result = _SPACE_BEFORE_CLOSE.sub(r"\1", result)
    result = _MULTI_NEWLINE.sub("\n\n", result)
    result = _collapse_grouped_digits(result)
    result = _spoken_email_at(result)
    result = _fix_lonely_i(result)
    result = _capitalize_sentences(result)
    result = _ensure_terminal_punctuation(result)
    return result.strip()


_CLI_GET_AS_GIT = re.compile(
    r"(?i)\b(run|then|please)\s+get\s+"
    r"(status|commit|push|pull|add|clone|diff|log|checkout|branch)\b"
)


def _repair_cli_get_as_git(text: str) -> str:
    """ASR often hears 'git' as 'get' in CLI dictation. Only after a cue."""
    return _CLI_GET_AS_GIT.sub(lambda m: f"{m.group(1)} git {m.group(2)}", text)


def _collapse_grouped_digits(text: str) -> str:
    """Join Whisper-style thousands groups: '8, 765' / '8,765' → '8765'."""
    return re.sub(r"\b(\d{1,3}),\s*(\d{3})\b", r"\1\2", text)


_SPOKEN_EMAIL_AT = re.compile(r"(?i)\b([A-Z0-9._%+\-]+)\s+at\s+([A-Z0-9.-]+\.[A-Z]{2,})\b")


def _spoken_email_at(text: str) -> str:
    """Turn 'ada at d-central.tech' into 'ada@d-central.tech'."""
    result = _SPOKEN_EMAIL_AT.sub(lambda m: f"{m.group(1)}@{m.group(2)}", text)
    return re.sub(r"(?i)dcentral\.tech", "d-central.tech", result)


def _fix_lonely_i(text: str) -> str:
    """Capitalize the English first-person pronoun without touching identifiers."""
    return re.sub(r"(?<!\w)i(?!\w)", "I", text)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_whitespace(text: str) -> str:
    # Preserve intentional newlines/tabs from spoken structure tokens.
    lines = text.split("\n")
    cleaned = [_MULTI_SPACE.sub(" ", line).strip() for line in lines]
    # Drop empty lines created by filler removal except paragraph breaks.
    out: list[str] = []
    blank_run = 0
    for line in cleaned:
        if not line:
            blank_run += 1
            if blank_run <= 1 and out:
                out.append("")
            continue
        blank_run = 0
        out.append(line)
    return "\n".join(out).strip()


def _capitalize_sentences(text: str) -> str:
    if not text:
        return text
    parts = re.split(r"(\n+)", text)
    rebuilt: list[str] = []
    for part in parts:
        if not part or part.startswith("\n"):
            rebuilt.append(part)
            continue
        sentences = _SENTENCE_SPLIT.split(part)
        capped: list[str] = []
        for sentence in sentences:
            s = sentence.strip()
            if not s:
                continue
            # Don't force-case code-ish tokens that start with lowercase symbols.
            if s[0].isalpha():
                s = s[0].upper() + s[1:]
            capped.append(s)
        # Rejoin with single space; original split consumed the separators.
        joined = " ".join(capped)
        # Restore terminal punctuation spacing inside the line.
        rebuilt.append(joined)
    return "".join(rebuilt)


_QUESTION_LEAD = re.compile(
    r"(?i)^(?:"
    r"what(?:'s| is| are| was| were| do| does| did| can| could| will| would| should| time| kind)|"
    r"when(?:'s| is| are| was| do| did| can| will| should)|"
    r"where(?:'s| is| are| was| do| did| can)|"
    r"why(?:'s| is| are| do| did| would| should)|"
    r"how(?:'s| is| are| do| did| can| much| many| long)|"
    r"who(?:'s| is| are| was| were| did)|"
    r"which(?: is| are| one| ones)|"
    r"(?:can|could|would|will|do|did|does) you|"
    r"(?:is|are) (?:there|it|this|that)"
    r")\b"
)


def _looks_like_question(text: str) -> bool:
    """True for spoken questions. False for 'what I mean…' statements."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    lower = stripped.lower()
    if lower.startswith("what i mean"):
        return False
    return _QUESTION_LEAD.match(stripped) is not None


def _ensure_terminal_punctuation(text: str) -> str:
    """Add a period to prose lines that clearly need end punctuation."""
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        # Leave bullets, numbered lists, code-ish lines alone.
        if stripped.startswith(("-", "*", "•")) or re.match(r"^\d+\.\s", stripped):
            out.append(line)
            continue
        bare = stripped.rstrip(".!")
        if bare and _looks_like_question(bare) and stripped[-1] != "?":
            out.append(bare + "?")
            continue
        if stripped[-1] in ".!?:;,\"')]}":
            out.append(line)
            continue
        # Single token / path / command: do not force a period.
        is_pathish = "/" in stripped or "\\" in stripped
        is_cmdish = stripped.startswith(("git", "npm", "pip", "http"))
        if " " not in stripped and (is_pathish or is_cmdish):
            out.append(line)
            continue
        if " " in stripped and stripped[0].isalpha():
            out.append(line.rstrip() + ".")
        else:
            out.append(line)
    return "\n".join(out)


def _drop_last_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    # Prefer punctuation-delimited sentences; fall back to last clause.
    matches = list(re.finditer(r"[.!?](?:\s+|$)", text))
    if len(matches) >= 1:
        cut = matches[-1].end()
        # If the last match is at the very end, drop that whole sentence.
        if cut >= len(text.rstrip()):
            if len(matches) >= 2:
                return text[: matches[-2].end()].rstrip()
            return ""
        return text[: matches[-1].start()].rstrip()
    return _drop_last_clause(text)


def _drop_last_clause(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    # Split on sentence enders or strong clause breaks.
    for sep in (". ", "! ", "? ", "; ", " — ", " - ", ", and ", ", "):
        if sep in text:
            head, _tail = text.rsplit(sep, 1)
            # Keep the separator's first character when it was a sentence ender.
            if sep[0] in ".!?":
                return (head + sep[0]).strip()
            return head.strip()
    # Unpunctuated speech:
    # - short thought (≤5 words) → clear entirely ("this was a mistake")
    # - longer thought → drop trailing ~3 words only
    words = _WORD_RE.findall(text)
    if len(words) <= 5:
        return ""
    return " ".join(words[:-3])
