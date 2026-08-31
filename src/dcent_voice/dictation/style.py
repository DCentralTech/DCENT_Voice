# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Local writing styles. Deterministic. No network. No audio."""

from __future__ import annotations

import re
from dataclasses import dataclass

STYLE_NAMES = ("plain", "email", "chat", "code", "formal", "notes")


@dataclass(frozen=True)
class Envelope:
    greeting: str
    who: str
    body: str
    closing: str


# Foreground process → style. Users can override in [style.per_app].
DEFAULT_APP_STYLES: dict[str, str] = {
    "code.exe": "code",
    "cursor.exe": "code",
    "devenv.exe": "code",
    "windowsterminal.exe": "code",
    "cmd.exe": "code",
    "powershell.exe": "code",
    "pwsh.exe": "code",
    "alacritty.exe": "code",
    "wezterm.exe": "code",
    "outlook.exe": "email",
    "olk.exe": "email",
    "ms-outlook.exe": "email",
    "hxoutlook.exe": "email",
    "thunderbird.exe": "email",
    "mail.exe": "email",
    "slack.exe": "chat",
    "teams.exe": "chat",
    "ms-teams.exe": "chat",
    "discord.exe": "chat",
    "whatsapp.exe": "chat",
    "telegram.exe": "chat",
    "signal.exe": "chat",
    "notion.exe": "notes",
    "obsidian.exe": "notes",
    "onenote.exe": "notes",
    "onenoteim.exe": "notes",
    "evernote.exe": "notes",
    "standard notes.exe": "notes",
}

_FORMAL_CONTRACTIONS: tuple[tuple[str, str], ...] = (
    ("don't", "do not"),
    ("doesn't", "does not"),
    ("didn't", "did not"),
    ("can't", "cannot"),
    ("couldn't", "could not"),
    ("won't", "will not"),
    ("wouldn't", "would not"),
    ("shouldn't", "should not"),
    ("isn't", "is not"),
    ("aren't", "are not"),
    ("wasn't", "was not"),
    ("weren't", "were not"),
    ("haven't", "have not"),
    ("hasn't", "has not"),
    ("hadn't", "had not"),
    ("i'm", "I am"),
    ("we're", "we are"),
    ("they're", "they are"),
    ("you're", "you are"),
    ("that's", "that is"),
    ("it's", "it is"),
    ("there's", "there is"),
)

_CLI_START = re.compile(
    r"^(git|npm|npx|pip|uv|cargo|pytest|python|py|node|go|rg|ssh|scp|curl|wget|"
    r"cd|ls|dir|make|docker|kubectl|pnpm|yarn|just|gh|lncli|bitcoin-cli|"
    r"lightning-cli|rustc)\b",
    re.IGNORECASE,
)

_EMAIL_CLOSINGS = (
    "thanks",
    "thank you",
    "best",
    "best regards",
    "kind regards",
    "regards",
    "cheers",
)

_INFORMAL = (
    (r"\bgonna\b", "going to"),
    (r"\bwanna\b", "want to"),
    (r"\bgotta\b", "have to"),
    (r"\byeah\b", "yes"),
    (r"\bok\b", "okay"),
)


def normalize_style(value: str | None) -> str:
    name = (value or "plain").strip().lower()
    if name in {"default", "off", "none", ""}:
        return "plain"
    if name not in STYLE_NAMES:
        raise ValueError(f"style must be one of {', '.join(STYLE_NAMES)}")
    return name


_STYLE_CUE_WORDS = {
    "email": "email",
    "mail": "email",
    "chat": "chat",
    "code": "code",
    "formal": "formal",
    "notes": "notes",
    "note": "notes",
    "plain": "plain",
}
_STYLE_CUE_ALT = "email|mail|chat|code|formal|notes|note|plain"
_LEADING_FILLER = re.compile(r"(?is)^(um+|uh+|erm+|hmm+|oh)\s+")
_LEADING_STYLE_CUE = re.compile(
    r"(?is)^\s*(?:please\s+|just\s+)?"
    r"(?:"
    r"(?:in|as|use|using)\s+(?:an?\s+)?(?P<framed>" + _STYLE_CUE_ALT + r")(?:\s+style)?"
    r"|"
    r"(?P<named>email|chat|code|formal|notes|plain)\s+style"
    r")"
    r"(?:\s*[:—,-]\s*|\s+)"
)
_TRAILING_STYLE_CUE = re.compile(
    r"(?is)\s+(?:"
    r"in\s+(?P<in_style>email|chat|code|formal|notes|plain)\s+style"
    r"|"
    r"as\s+(?:an?\s+)?(?P<as_style>" + _STYLE_CUE_ALT + r")(?:\s+style)?"
    r")\s*[.!]?\s*$"
)
_FALSE_STYLE_REMAINDER = re.compile(r"(?is)^(is|are|was|were|of|for|guide|guides|means|mean)\b")


def peel_spoken_style(text: str) -> tuple[str | None, str]:
    """Strip a leading/trailing spoken style cue. Does not rewrite the body.

    Overlay chips are read-only (click-through). Saying ``email style …`` or
    ``as notes …`` selects a local destination style for this utterance only.
    Not an overlay style picker. Content like ``code style is important`` is left
    alone.
    """
    source = text or ""
    working = source.strip()
    if not working:
        return None, source
    while True:
        nxt = _LEADING_FILLER.sub("", working).strip()
        if nxt == working:
            break
        working = nxt
    leading = _LEADING_STYLE_CUE.match(working)
    if leading:
        name = leading.group("framed") or leading.group("named")
        rest = working[leading.end() :].strip()
        style = _STYLE_CUE_WORDS.get((name or "").lower())
        if style and rest and not _FALSE_STYLE_REMAINDER.match(rest):
            return style, rest
    trailing = _TRAILING_STYLE_CUE.search(working)
    if trailing:
        name = trailing.group("in_style") or trailing.group("as_style")
        rest = working[: trailing.start()].strip()
        style = _STYLE_CUE_WORDS.get((name or "").lower())
        if style and rest:
            return style, rest
    return None, source


def resolve_style(
    default: str,
    process_name: str | None,
    per_app: dict[str, str] | None = None,
    window_title: str | None = None,
    learned_per_app: dict[str, str] | None = None,
) -> str:
    """Pick a style. Config, then title, then learned app, then built-in, then default."""
    built_in = {key.lower(): normalize_style(value) for key, value in DEFAULT_APP_STYLES.items()}
    overrides = {key.lower(): normalize_style(value) for key, value in (per_app or {}).items()}
    learned = {
        key.lower(): normalize_style(value) for key, value in (learned_per_app or {}).items()
    }
    process = (process_name or "").strip().lower()
    if process in overrides:
        return overrides[process]
    titled = _style_from_window_title(window_title)
    if titled is not None:
        return titled
    if process in learned:
        return learned[process]
    if process in built_in:
        return built_in[process]
    return normalize_style(default)


_TITLE_EMAIL = (
    "gmail",
    "mail.google",
    "inbox (",
    "outlook.live",
    "outlook.office",
    "outlook.office365",
    "docs.google",
    "google docs",
)
_TITLE_CHAT = (
    "slack |",
    " | slack",
    "slack.com",
    "discord |",
    " | discord",
    "microsoft teams",
    " | teams",
    "whatsapp",
    "telegram",
)
_TITLE_NOTES = (
    "notion",
    "obsidian",
    "onenote",
    "evernote",
    "standard notes",
)


def _style_from_window_title(title: str | None) -> str | None:
    """Map a browser tab title to a destination. Never treats 'Google' as email."""
    lowered = (title or "").strip().lower()
    if not lowered:
        return None
    if any(needle in lowered for needle in _TITLE_EMAIL):
        return "email"
    if any(needle in lowered for needle in _TITLE_CHAT):
        return "chat"
    if any(needle in lowered for needle in _TITLE_NOTES):
        return "notes"
    return None


def apply_style(text: str, style: str) -> str:
    """Apply a named local style. Never contacts a network."""
    if not text:
        return text
    name = normalize_style(style)
    if name == "plain":
        return text
    if name == "notes":
        return _style_notes(text)
    envelope = _peel_envelope(text)
    listed = extract_spoken_list(
        envelope.body,
        enveloped=bool(envelope.greeting or envelope.closing),
    )
    if listed is not None:
        body = format_destination_list(listed, name)
        return _wrap_destination_document(body, name, envelope)
    if name == "code":
        return _style_code(text)
    if name == "chat":
        return _style_chat(text)
    if name == "email":
        return _style_email(text)
    if name == "formal":
        return _style_formal(text)
    return text


_ORDINAL_LIST = re.compile(
    r"(?i)^(?:please\s+)?first[,:]?\s+(.+?)\s+second[,:]?\s+(.+?)"
    r"(?:\s+third[,:]?\s+(.+?))?(?:\s+fourth[,:]?\s+(.+?))?(?:\s+fifth[,:]?\s+(.+?))?$"
)
_AND_THEN = re.compile(r"(?i)\s+and then\s+")
# Greetings are not task cues. "Hey …" is peeled before list detection.
_TASKY_LEAD = re.compile(r"(?i)^(?:please|can you|could you|we need to|need to|first)\b")
_TASK_PREFIX = re.compile(r"(?i)^(?:please\s+)?(?:can you|could you)\s+")
_ENVELOPE_GREET = re.compile(
    r"(?i)^(?P<greet>hey|hi|hello|dear)(?:\s+(?P<who>[A-Za-z][\w.-]*))?[,\s]+"
)
_ENVELOPE_CLOSE = re.compile(
    r"(?i)\s+(?P<close>thanks|thank you|best regards|kind regards|best|regards|cheers)\s*$"
)


def extract_spoken_list(text: str, *, enveloped: bool = False) -> list[str] | None:
    """Pull first/second/third or 'and then' chains. None if not a list.

    Two-item ``and then`` is a list when the first clause is tasky, or when
    the caller already peeled a greeting/closing (``enveloped=True``). Never
    treat leftover envelope words on raw text as a task cue.
    """
    raw = (text or "").strip().rstrip(".!?")
    if not raw:
        return None
    match = _ORDINAL_LIST.match(raw)
    if match:
        items = [part.strip(" ,") for part in match.groups() if part and part.strip()]
        if len(items) >= 2:
            return [_strip_task_prefix(item) for item in items]
    parts = [part.strip(" ,") for part in _AND_THEN.split(raw) if part.strip()]
    if len(parts) >= 3:
        return [_strip_task_prefix(part) for part in parts]
    if len(parts) == 2 and (enveloped or _TASKY_LEAD.match(parts[0])):
        return [_strip_task_prefix(part) for part in parts]
    return None


def _strip_task_prefix(item: str) -> str:
    return _TASK_PREFIX.sub("", item).strip()


def _peel_envelope(text: str) -> Envelope:
    raw = (text or "").strip()
    greeting = ""
    who = ""
    closing = ""
    body = raw.rstrip(".!?")
    close = _ENVELOPE_CLOSE.search(body)
    if close:
        closing = close.group("close")
        body = body[: close.start()].rstrip(" ,")
    greet = _ENVELOPE_GREET.match(body)
    if greet:
        greeting = greet.group("greet")
        who = greet.group("who") or ""
        if who.lower() in _NOT_ADDRESSEE:
            body = f"{who} {body[greet.end() :]}".strip()
            who = ""
        else:
            body = body[greet.end() :].strip()
    return Envelope(greeting=greeting, who=who, body=body or raw, closing=closing)


def _wrap_destination_document(body: str, style: str, envelope: Envelope) -> str:
    """Greeting + list + closing when the speech had an envelope. Else list only."""
    if style == "chat" or not (envelope.greeting or envelope.closing):
        return body
    greet = envelope.greeting
    if style == "formal" and greet.lower() == "hey":
        greet = "Hello"
    head = ""
    if greet:
        greet_cap = greet[:1].upper() + greet[1:].lower()
        head = f"{greet_cap} {envelope.who}," if envelope.who else f"{greet_cap},"
    close = envelope.closing
    if close:
        if style == "formal" and close.lower() in {"thanks", "thank you"}:
            close = "Thank you"
        pretty = close[:1].upper() + close[1:] + ","
    else:
        pretty = ""
    parts = [part for part in (head, body, pretty) if part]
    return "\n\n".join(parts)


def format_destination_list(items: list[str], style: str) -> str:
    """Layout a spoken list for the destination. Local. Deterministic."""
    cleaned = [item[:1].upper() + item[1:] if item else item for item in items]
    if style in {"email", "formal"}:
        return "\n".join(f"{index}. {item}" for index, item in enumerate(cleaned, start=1))
    if style == "notes":
        return "## Action items\n" + "\n".join(f"- {item}" for item in cleaned)
    return "\n".join(f"- {item}" for item in cleaned)


_CAMEL_CASE = re.compile(r"(?i)\bcamel case\s+([A-Za-z0-9]+(?:\s+[A-Za-z0-9]+)+)")
_SNAKE_CASE = re.compile(r"(?i)\bsnake case\s+([A-Za-z0-9]+(?:\s+[A-Za-z0-9]+)+)")
_SUBJECT_BODY = re.compile(
    r"(?i)^subject\s+(.+?)(?:\s+body\s+|\.\s+)(.+)$",
)

# Words that look like a greeting addressee but are the start of the sentence.
_NOT_ADDRESSEE = frozenset(
    {
        "can",
        "could",
        "would",
        "will",
        "you",
        "i",
        "we",
        "they",
        "please",
        "just",
        "so",
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "it",
        "this",
        "that",
        "there",
        "here",
        "my",
        "your",
        "our",
    }
)
_GREETING_ADDRESSEE = re.compile(
    r"^(?P<greet>hi|hello|hey|dear)\s+(?P<who>[A-Za-z][\w.-]*)[,:]?\s+",
    re.IGNORECASE,
)
_POLITE_RUN = re.compile(
    r"(?i)^(?:run\s+|please\s+(?:just\s+)?(?:run\s+)?|"
    r"can you\s+(?:please\s+)?(?:run\s+)?|"
    r"could you\s+(?:please\s+)?(?:run\s+)?)"
)
_SPOKEN_PYTHON_FUNCTION = re.compile(
    r"(?is)^(?:define|create|write)(?:\s+a)?\s+function\s+"
    r"(?P<name>[A-Za-z][A-Za-z0-9 _-]*?)\s*"
    r"\((?P<args>[^)]*)\)\s*(?:colon|:)\s*[.!]?\s*"
    r"(?P<body>(?:\r?\n.*)*)$"
)
_SPOKEN_PYTHON_LOOP = re.compile(
    r"(?is)^for\s+each\s+(?P<item>[A-Za-z][A-Za-z0-9 _-]*?)\s+in\s+"
    r"(?P<items>[A-Za-z][A-Za-z0-9 _-]*?)\s*(?:colon|:)\s*[.!]?\s*"
    r"(?P<body>(?:\r?\n.*)*)$"
)
_SPOKEN_PYTHON_CLASS = re.compile(
    r"(?is)^(?:define|create|write)(?:\s+a)?\s+class\s+"
    r"(?P<name>[A-Za-z][A-Za-z0-9 _-]*?)\s*(?:colon|:)\s*[.!]?\s*"
    r"(?P<body>(?:\r?\n.*)*)$"
)
_SPOKEN_PYTHON_IF = re.compile(
    r"(?is)^if\s+(?P<condition>.+?)\s*(?:colon|:)\s*[.!]?\s*"
    r"(?P<body>(?:\r?\n.*)*)$"
)
_SPOKEN_ASSIGNMENT = re.compile(
    r"(?is)^(?P<declaration>const|let|var)\s+"
    r"(?P<name>[A-Za-z][A-Za-z0-9 _-]*?)\s+equals\s+"
    r"(?P<value>.+?)\s*(?:semicolon|;)\s*[.!]?$"
)
_SPOKEN_MEMBER = re.compile(
    r"(?is)^[A-Za-z][A-Za-z0-9 _()-]*(?:\s+dot\s+[A-Za-z][A-Za-z0-9 _()-]*)+[.!]?$"
)
_CODE_UNDERSCORE = re.compile(r"(?<=\w)\s*_\s*(?=\w)")


def _style_code(text: str) -> str:
    result = text.strip()
    result = _CODE_UNDERSCORE.sub("_", result)
    result = re.sub(
        r"(?i)\bquote\s+(.+?)\s+(?:(?:end|close)\s+)?quote\b",
        lambda match: f'"{match.group(1).strip()}"',
        result,
    )
    result = re.sub(r"(?i)\bdash\s+dash\s+([A-Za-z][\w-]*)", r"--\1", result)
    result = re.sub(r"(?i)\bdash\s+([A-Za-z][\w-]{1,})", r"--\1", result)
    result = re.sub(r"(?i)\bdash\s+([A-Za-z])\b", r"-\1", result)
    for transform in (
        _style_spoken_python_function,
        _style_spoken_python_loop,
        _style_spoken_python_class,
        _style_spoken_python_if,
        _style_spoken_assignment,
        _style_spoken_member,
    ):
        spoken = transform(result)
        if spoken is not None:
            return spoken
    result = _CAMEL_CASE.sub(lambda m: _join_case(m.group(1), "camel"), result)
    result = _SNAKE_CASE.sub(lambda m: _join_case(m.group(1), "snake"), result)
    stripped = _POLITE_RUN.sub("", result).lstrip()
    if stripped and _CLI_START.match(stripped):
        result = re.sub(r"^[A-Za-z-]+", lambda match: match.group(0).lower(), stripped, count=1)
    if _CLI_START.match(result) and result[-1:] in ".?!" and result.count(result[-1]) == 1:
        result = result[:-1]
    return result


def _style_spoken_python_function(text: str) -> str | None:
    """Render an explicit spoken Python function frame deterministically."""
    match = _SPOKEN_PYTHON_FUNCTION.fullmatch(text)
    if match is None:
        return None
    name = _spoken_identifier(match.group("name"))
    args = ", ".join(
        _spoken_identifier(part) for part in match.group("args").split(",") if part.strip()
    )
    return _python_block(f"def {name}({args}):", match.group("body"))


def _style_spoken_python_loop(text: str) -> str | None:
    match = _SPOKEN_PYTHON_LOOP.fullmatch(text)
    if match is None:
        return None
    item = _spoken_identifier(match.group("item"))
    items = _spoken_identifier(match.group("items"))
    return _python_block(f"for {item} in {items}:", match.group("body"))


def _style_spoken_python_class(text: str) -> str | None:
    match = _SPOKEN_PYTHON_CLASS.fullmatch(text)
    if match is None:
        return None
    name = _spoken_class_identifier(match.group("name"))
    return _python_block(f"class {name}:", match.group("body"))


def _style_spoken_python_if(text: str) -> str | None:
    match = _SPOKEN_PYTHON_IF.fullmatch(text)
    if match is None:
        return None
    condition = _spoken_expression(match.group("condition"))
    return _python_block(f"if {condition}:", match.group("body"))


def _style_spoken_assignment(text: str) -> str | None:
    match = _SPOKEN_ASSIGNMENT.fullmatch(text)
    if match is None:
        return None
    declaration = match.group("declaration").lower()
    name = _spoken_identifier(match.group("name"))
    value = _spoken_expression(match.group("value"))
    return f"{declaration} {name} = {value};"


def _style_spoken_member(text: str) -> str | None:
    member = _SPOKEN_MEMBER.fullmatch(text) is not None
    indexed = re.fullmatch(r"(?is).+?\s*\[\s*.+?\s*\][.!]?", text) is not None
    if not member and not indexed:
        return None
    return _spoken_expression(text)


def _python_block(header: str, spoken_body: str) -> str:
    sources = [source.strip() for source in spoken_body.splitlines() if source.strip()]
    if not sources:
        sources.append("pass")
    return header + "\n" + "\n".join(_python_body_lines(sources, indent=1))


def _python_body_lines(sources: list[str], *, indent: int) -> list[str]:
    rendered: list[str] = []
    index = 0
    while index < len(sources):
        source = sources[index]
        nested = re.fullmatch(r"(?is)if\s+(.+?)\s*(?:colon|:)\s*[.!]?", source)
        if nested:
            rendered.append("    " * indent + f"if {_spoken_expression(nested.group(1))}:")
            tail = sources[index + 1 :] or ["pass"]
            rendered.extend(_python_body_lines(tail, indent=indent + 1))
            break
        line = _normalize_python_statement(source)
        if line:
            rendered.append("    " * indent + line)
        index += 1
    return rendered


def _normalize_python_statement(value: str) -> str:
    line = _CODE_UNDERSCORE.sub("_", value.strip())
    if line.endswith(".") and not line.endswith("..."):
        line = line[:-1]
    if re.fullmatch(r"(?i)pass", line):
        return "pass"
    returned = re.fullmatch(r"(?is)return\s+(.+)", line)
    if returned:
        return "return " + _spoken_expression(returned.group(1))
    printed = re.fullmatch(r"(?is)print\s*(\(.*\))", line)
    if printed:
        return "print" + re.sub(r"\s+", "", printed.group(1))
    return line


def _spoken_expression(value: str) -> str:
    expression = _CODE_UNDERSCORE.sub("_", value.strip()).rstrip(".")
    awaited = re.fullmatch(r"(?is)await\s+(.+)", expression)
    if awaited:
        return "await " + _spoken_expression(awaited.group(1))
    comparisons = (
        (r"greater than or equal to", ">="),
        (r"less than or equal to", "<="),
        (r"not equal to", "!="),
        (r"equals equals", "=="),
        (r"greater than", ">"),
        (r"less than", "<"),
    )
    for spoken, operator in comparisons:
        compared = re.fullmatch(rf"(?is)(.+?)\s+{spoken}\s+(.+)", expression)
        if compared:
            left = _spoken_expression(compared.group(1))
            right = _spoken_expression(compared.group(2))
            return f"{left} {operator} {right}"
    dot_at = expression.lower().find(" dot ")
    call_at = expression.find("(")
    if dot_at >= 0 and (call_at < 0 or dot_at < call_at):
        return ".".join(
            _spoken_expression(part)
            for part in re.split(r"(?i)\s+dot\s+", expression)
            if part.strip()
        )
    indexed = re.fullmatch(r"(?is)(.+?)\s*\[\s*(.+?)\s*\]", expression)
    if indexed:
        return f"{_spoken_expression(indexed.group(1))}[{_spoken_expression(indexed.group(2))}]"
    call = re.fullmatch(r"(?is)([A-Za-z][A-Za-z0-9 _-]*?)\s*\(([^)]*)\)", expression)
    if call is not None:
        name = _spoken_identifier(call.group(1))
        args = ", ".join(
            _spoken_expression(part) for part in call.group(2).split(",") if part.strip()
        )
        return f"{name}({args})"
    if re.fullmatch(r"[A-Za-z0-9 _-]+", expression):
        return _spoken_identifier(expression)
    return expression


def _spoken_identifier(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    return "_".join(part.lower() for part in parts)


def _spoken_class_identifier(value: str) -> str:
    acronyms = {"api", "css", "gpu", "html", "http", "id", "json", "sql", "ui", "url"}
    parts = re.findall(r"[A-Za-z0-9]+", value)
    return "".join(
        part.upper() if part.lower() in acronyms else part[:1].upper() + part[1:].lower()
        for part in parts
    )


def _join_case(words: str, kind: str) -> str:
    parts = [part for part in re.split(r"\s+", words.strip()) if part]
    if not parts:
        return words
    if kind == "snake":
        return "_".join(part.lower() for part in parts)
    first, *rest = parts
    return first.lower() + "".join(part[:1].upper() + part[1:].lower() for part in rest)


_TRAILING_THANKS = re.compile(r"(?i)\s+(?:thanks|thank you)\s*[.!?]?$")
_CHAT_LEAD = re.compile(
    r"(?i)^(?:I just wanted to say(?: that)?|"
    r"just wanted to (?:say|let you know)(?: that)?|"
    r"just checking in[,.]?|"
    r"I was (?:just )?wondering if|"
    r"wanted to (?:quickly )?mention(?: that)?|"
    r"quick question[,:]?|"
    r"not sure if (?:this is )?(?:relevant|helpful) but)\s+"
)
_EMAIL_LEAD = re.compile(
    r"(?i)^(?:I just wanted to (?:say|let you know)(?: that)?|"
    r"just wanted to (?:say|let you know)(?: that)?|"
    r"just checking in[,.]?|"
    r"wanted to let you know(?: that)?)\s+"
)
_FORMAL_HEDGES = (
    (r"(?i)\bto be honest,?\s+", ""),
    (r"(?i)\bbasically,?\s+", ""),
    (r"(?i)\bI guess\s+", ""),
    (r"(?i)\bI suppose\s+", ""),
)
_TELL_ADDRESSEE = re.compile(
    r"(?i)^(?:(?:please|can you|could you)\s+)?(?:tell|email)\s+"
    r"(?P<who>[A-Za-z][\w.-]*)\s+(?P<body>.+)$"
)
_LET_KNOW = re.compile(
    r"(?i)^(?:(?:please|can you|could you)\s+)?let\s+"
    r"(?P<who>[A-Za-z][\w.-]*)\s+know\s+(?P<body>.+)$"
)
_NOT_NAMED_WHO = frozenset(
    {
        "me",
        "us",
        "him",
        "her",
        "it",
        "them",
        "someone",
        "everybody",
        "everyone",
        "people",
        "the",
        "this",
        "that",
        "a",
        "an",
    }
)


def _style_chat(text: str) -> str:
    result = text.strip()
    result = _CHAT_LEAD.sub("", result)
    result = _drop_tell_frame(result)
    result = _TRAILING_THANKS.sub("", result)
    result = re.sub(r"(?i)^please\s+", "", result)
    result = _cap_first(result) if result[:1].islower() else result
    if result[:4].lower() == "hey ":
        result = "hey " + result[4:]
    if (
        len(result) <= 80
        and result.endswith(".")
        and not result.endswith("...")
        and "?" not in result
        and "!" not in result
        and "\n" not in result
    ):
        result = result[:-1]
    return result


def _style_email(text: str) -> str:
    result = text.strip()
    framed = _email_from_spoken_frame(result)
    if framed is not None:
        return _apply_informal(framed)
    stripped = _EMAIL_LEAD.sub("", result)
    if stripped != result:
        result = _cap_first(stripped)
    result = _apply_informal(result)
    subject = _SUBJECT_BODY.match(result)
    if subject:
        result = f"Subject: {subject.group(1).strip()}\n\n{subject.group(2).strip()}"
    if "\n" not in result[:48]:
        greeted = _GREETING_ADDRESSEE.match(result)
        if greeted:
            who = greeted.group("who")
            greet = greeted.group("greet")
            rest = result[greeted.end() :].lstrip()
            greet_cap = greet[:1].upper() + greet[1:].lower()
            if rest and who.lower() not in _NOT_ADDRESSEE:
                rest = _cap_first(rest)
                result = f"{greet_cap} {who},\n\n{rest}"
            elif rest:
                rest = _cap_first(f"{who} {rest}")
                result = f"{greet_cap},\n\n{rest}"
        else:
            lowered = result.lower()
            for greeting in ("hi ", "hello ", "hey ", "dear "):
                if lowered.startswith(greeting):
                    head = result[: len(greeting)].rstrip()
                    rest = result[len(greeting) :].lstrip()
                    if rest:
                        result = f"{head},\n\n{_cap_first(rest)}"
                    break
    result = _email_closing(result)
    return _promote_email_request(result)


def _drop_tell_frame(text: str) -> str:
    """Chat keeps the payload of 'tell X' / 'let X know' without an envelope."""
    for pattern in (_TELL_ADDRESSEE, _LET_KNOW):
        match = pattern.match(text.rstrip(".!?"))
        if match:
            return _cap_first(match.group("body").strip())
    return text


def _email_from_spoken_frame(text: str) -> str | None:
    """'tell Alex the invoice is ready' becomes a sendable email."""
    raw = text.strip()
    match = _TELL_ADDRESSEE.match(raw.rstrip(".!?")) or _LET_KNOW.match(raw.rstrip(".!?"))
    if match is None:
        return None
    who = match.group("who")
    body = _cap_first(match.group("body").strip())
    if body and body[-1] not in ".!?":
        body = body + "."
    if who.lower() in _NOT_NAMED_WHO or who.lower() in _NOT_ADDRESSEE:
        return f"{body}\n\nThanks,"
    return f"Hi {who[:1].upper() + who[1:]},\n\n{body}\n\nThanks,"


def _promote_email_request(text: str) -> str:
    """Same speech, email tone: 'can you X' becomes a sendable request."""
    parts = text.split("\n\n")
    out: list[str] = []
    for part in parts:
        stripped = part.strip()
        if re.match(r"(?i)^can you\b", stripped) and not stripped.endswith("?"):
            stripped = re.sub(r"(?i)^can you\b", "Could you", stripped, count=1)
            stripped = stripped.rstrip(".") + "?"
        out.append(stripped)
    return "\n\n".join(out)


def _cap_first(text: str) -> str:
    if text and text[0].isalpha():
        return text[0].upper() + text[1:]
    return text


def _email_closing(text: str) -> str:
    stripped = text.rstrip().rstrip(".")
    lowered = stripped.lower()
    for closing in sorted(_EMAIL_CLOSINGS, key=len, reverse=True):
        pretty = closing[0].upper() + closing[1:] + ","
        if lowered == closing:
            return pretty
        if lowered.endswith(" " + closing):
            head = stripped[: -len(closing)].rstrip()
            return f"{head}\n\n{pretty}"
    return text


def _apply_informal(text: str) -> str:
    result = text
    for pattern, written in _INFORMAL:
        result = re.sub(pattern, written, result, flags=re.IGNORECASE)
    return result


def _style_formal(text: str) -> str:
    result = re.sub(r"(?i)^hey\b", "Hello", text.strip(), count=1)
    ordered = sorted(_FORMAL_CONTRACTIONS, key=lambda pair: len(pair[0]), reverse=True)
    for spoken, written in ordered:
        pattern = re.compile(rf"(?<!\w){re.escape(spoken)}(?!\w)", re.IGNORECASE)

        def _sub(match: re.Match[str], replacement: str = written) -> str:
            token = match.group(0)
            if token.isupper():
                return replacement.upper()
            if token[0].isupper():
                return replacement[0].upper() + replacement[1:]
            return replacement

        result = pattern.sub(_sub, result)
    result = _apply_informal(result)
    for hedge_pattern, written in _FORMAL_HEDGES:
        result = re.sub(hedge_pattern, written, result)
    result = re.sub(r"\s{2,}", " ", result).strip()
    return _cap_first(result)


# Spoken meeting / brain-dump cues → structured local notes. Not a meeting bot.
_NOTES_CUE = re.compile(
    r"(?i)(?P<label>"
    r"(?:we\s+)?decided(?:\s+to)?|"
    r"decision(?:\s+is|\s*:)?|"
    r"action\s+items?|"
    r"to[\s-]?dos?|"
    r"todos?|"
    r"next\s+steps?|"
    r"follow[\s-]?ups?"
    r")\b"
)
_NOTES_SECTION_TITLE = {
    "decision": "Decision",
    "decided": "Decision",
    "we decided": "Decision",
    "we decided to": "Decision",
    "decision is": "Decision",
    "decision:": "Decision",
    "action item": "Action items",
    "action items": "Action items",
    "todo": "Action items",
    "todos": "Action items",
    "to do": "Action items",
    "to-do": "Action items",
    "to dos": "Action items",
    "to-dos": "Action items",
    "next step": "Next steps",
    "next steps": "Next steps",
    "follow up": "Next steps",
    "follow-up": "Next steps",
    "followups": "Next steps",
    "follow ups": "Next steps",
}


def _notes_section_title(label: str) -> str:
    key = re.sub(r"\s+", " ", label.strip().lower())
    key = key.rstrip(":")
    return _NOTES_SECTION_TITLE.get(key, "Notes")


def _notes_bulletize(body: str) -> str:
    raw = body.strip(" ,.;")
    if not raw:
        return ""
    listed = extract_spoken_list(raw, enveloped=False)
    if listed is not None:
        return "\n".join(f"- {item[:1].upper() + item[1:] if item else item}" for item in listed)
    parts = [
        part.strip(" ,") for part in re.split(r"(?i)\s+and then\s+|, then\s+", raw) if part.strip()
    ]
    if len(parts) >= 2:
        return "\n".join(f"- {part[:1].upper() + part[1:] if part else part}" for part in parts)
    sentence = _cap_first(raw)
    starts_task = sentence.lower().startswith(("update ", "ping ", "send "))
    if sentence and sentence[-1] not in ".!?" and " " in sentence and not starts_task:
        sentence = sentence + "."
    if re.match(r"(?i)^(update|ping|send|fix|add|remove|ship|call|email)\b", sentence):
        return f"- {sentence.rstrip('.')}"
    return sentence


def _style_notes(text: str) -> str:
    """Structure brain-dumps into Decision / Action items / Next steps locally."""
    raw = (text or "").strip()
    if not raw:
        return raw
    # Notes never keep email greeting/closing envelopes.
    body = _peel_envelope(raw).body.strip()
    cues = list(_NOTES_CUE.finditer(body))
    if cues:
        blocks: list[tuple[str, str]] = []
        # Leading prose before the first cue (rare) becomes Notes.
        lead = body[: cues[0].start()].strip(" ,.;")
        if lead:
            blocks.append(("Notes", lead))
        for index, match in enumerate(cues):
            title = _notes_section_title(match.group("label"))
            end = cues[index + 1].start() if index + 1 < len(cues) else len(body)
            chunk = body[match.end() : end].strip(" ,.;:")
            # "we decided to ship Monday" — cue already ate "to"; keep payload.
            if match.group("label").lower().endswith(" to") and chunk:
                pass
            blocks.append((title, chunk))
        # Merge duplicate adjacent titles (action item … action item …).
        merged: list[tuple[str, str]] = []
        for title, chunk in blocks:
            if not chunk and title != "Decision":
                continue
            if merged and merged[-1][0] == title:
                prev = merged[-1][1]
                joined = f"{prev} and then {chunk}".strip() if prev and chunk else (prev or chunk)
                merged[-1] = (title, joined)
            else:
                merged.append((title, chunk))
        parts: list[str] = []
        for title, chunk in merged:
            content = (
                _notes_bulletize(chunk)
                if title != "Decision"
                else _cap_first(
                    chunk.rstrip(".") + ("." if chunk and chunk[-1] not in ".!?" else "")
                )
            )
            if title == "Decision" and not content:
                continue
            if title != "Decision" and not content:
                continue
            if title == "Decision":
                parts.append(f"## {title}\n{content}")
            else:
                parts.append(f"## {title}\n{content}")
        if parts:
            return "\n\n".join(parts)
    listed = extract_spoken_list(body, enveloped=False)
    if listed is not None:
        return format_destination_list(listed, "notes")
    # No structure cues: keep the speaker's punctuation; never invent sections.
    return _cap_first(raw.rstrip())
