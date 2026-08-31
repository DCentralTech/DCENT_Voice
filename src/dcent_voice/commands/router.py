# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Route recognized command intents to handlers."""

from __future__ import annotations

import json
import re

from dcent_voice.commands.schema import COMMAND_INTENT_SCHEMA, CommandIntent, ToolCall
from dcent_voice.llm.base import LLMProvider

COMMAND_SYSTEM_PROMPT = """Route voice commands for a dictation app.
Return only JSON matching the schema.
Use rewrite_selection when the user asks to transform selected text.
Use insert_text when the user asks a direct question or asks to insert content.
Use tool_call only for explicit app/tool automation requests.
Use noop when no useful command is present."""


class CommandRouter:
    """Routes recognized command intents to registered handlers."""

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm

    def route(self, transcript: str, selection: str = "") -> CommandIntent:
        if self.llm is not None:
            routed = self._route_with_llm(transcript, selection)
            if routed is not None:
                return routed
        return rules_fallback(transcript, selection)

    def _route_with_llm(self, transcript: str, selection: str) -> CommandIntent | None:
        user = json.dumps({"transcript": transcript, "selection": selection}, ensure_ascii=False)
        llm = self.llm
        if llm is None:
            return None
        try:
            data = llm.complete_structured(
                COMMAND_SYSTEM_PROMPT,
                user,
                COMMAND_INTENT_SCHEMA,
                temperature=0.0,
            )
            return CommandIntent.model_validate(data)
        except Exception:
            return self._repair_with_llm(transcript, selection)

    def _repair_with_llm(self, transcript: str, selection: str) -> CommandIntent | None:
        if self.llm is None:
            return None
        repair_user = (
            "Return only valid JSON for this command intent schema.\n"
            f"Schema: {json.dumps(COMMAND_INTENT_SCHEMA, ensure_ascii=False)}\n"
            f"Transcript: {transcript}\n"
            f"Selection: {selection}"
        )
        try:
            text = self.llm.complete(
                COMMAND_SYSTEM_PROMPT, repair_user, temperature=0.0, max_tokens=512
            )
            return CommandIntent.model_validate(json.loads(text))
        except Exception:
            return None


def rules_fallback(transcript: str, selection: str = "") -> CommandIntent:
    text = transcript.strip()
    lower = text.lower()
    if not text:
        return CommandIntent(action="noop", confidence=1.0, reason="empty")

    arithmetic = _simple_arithmetic(lower)
    if arithmetic is not None:
        return CommandIntent(
            action="insert_text", text=arithmetic, confidence=0.95, reason="arithmetic"
        )

    if selection:
        rewritten = _rules_rewrite_selection(lower, selection)
        if rewritten is not None:
            return rewritten
        if re.match(
            r"^(?:please\s+)?(?:make\s+(?:this|it)|rewrite(?:\s+this)?|"
            r"improve(?:\s+this)?|fix\s+this|change\s+(?:this|the\s+tone)|"
            r"turn\s+this\s+into)\b",
            lower,
        ):
            return CommandIntent(
                action="noop",
                confidence=0.9,
                reason="unsupported_selection_transform_requires_local_llm",
            )

    if lower.startswith(("insert ", "write ", "type ")):
        return CommandIntent(
            action="insert_text",
            text=re.sub(r"^(insert|write|type)\s+", "", text, flags=re.I),
            confidence=0.7,
            reason="rules_insert",
        )

    tool_match = re.search(r"\b(open|search|create|summarize)\b\s+(?P<target>.+)", lower)
    if tool_match:
        return CommandIntent(
            action="tool_call",
            tool_call=ToolCall(
                name=tool_match.group(1),
                arguments={"target": tool_match.group("target")},
            ),
            confidence=0.55,
            reason="rules_tool",
        )

    return CommandIntent(action="insert_text", text=text, confidence=0.45, reason="default_insert")


def _simple_arithmetic(text: str) -> str | None:
    stripped = text.strip()
    expr = r"(-?\d+)\s*([+\-*/x])\s*(-?\d+)"
    # Require an explicit calc trigger at the start, OR the whole utterance to be
    # just the expression. Otherwise "call 555-1234" would evaluate to -679.
    match = re.match(rf"(?:what(?:'s| is)|calculate|compute)\s+{expr}\b", stripped) or re.fullmatch(
        rf"{expr}\s*[?.=]?", stripped
    )
    if not match:
        return None
    left = int(match.group(1))
    op = match.group(2)
    right = int(match.group(3))
    if op == "+":
        return str(left + right)
    if op == "-":
        return str(left - right)
    if op in {"*", "x"}:
        return str(left * right)
    if op == "/" and right != 0:
        result = left / right
        return str(int(result)) if result.is_integer() else str(result)
    return None


def _rules_rewrite_selection(lower: str, selection: str) -> CommandIntent | None:
    """Offline selection transforms that work without an LLM."""
    if re.search(r"\btranslate(?:\s+(?:this|the selection))?\s+(?:to|into)\s+\w+", lower):
        return CommandIntent(
            action="noop",
            confidence=0.95,
            reason="offline_translation_requires_local_llm",
        )
    if any(
        phrase in lower
        for phrase in (
            "fix grammar",
            "fix the grammar",
            "correct grammar",
            "correct the grammar",
            "proofread",
        )
    ):
        return CommandIntent(
            action="rewrite_selection",
            text=_fix_grammar(selection),
            confidence=0.78,
            reason="rules_fix_grammar",
        )
    if any(
        phrase in lower
        for phrase in (
            "make this warmer",
            "make it warmer",
            "make this friendlier",
            "make it friendlier",
            "friendlier tone",
            "less blunt",
        )
    ):
        return CommandIntent(
            action="rewrite_selection",
            text=_warmer(selection),
            confidence=0.7,
            reason="rules_warmer",
        )
    formal_phrases = ("more formal", "formalize", "make this formal")
    if any(phrase in lower for phrase in formal_phrases):
        return CommandIntent(
            action="rewrite_selection",
            text=_formalize(selection),
            confidence=0.72,
            reason="rules_formalize",
        )
    if any(p in lower for p in ("uppercase", "all caps", "make this uppercase")):
        return CommandIntent(
            action="rewrite_selection",
            text=selection.upper(),
            confidence=0.85,
            reason="rules_uppercase",
        )
    if any(p in lower for p in ("lowercase", "make this lowercase")):
        return CommandIntent(
            action="rewrite_selection",
            text=selection.lower(),
            confidence=0.85,
            reason="rules_lowercase",
        )
    if any(p in lower for p in ("title case", "capitalize")):
        return CommandIntent(
            action="rewrite_selection",
            text=selection.title(),
            confidence=0.8,
            reason="rules_title_case",
        )
    # Match removal before the generic "bullet" transform below.  Otherwise
    # "remove bullets" contains "bullet" and is routed back through
    # ``_bulletize``, leaving an already-bulleted selection unchanged.
    if any(
        p in lower
        for p in (
            "remove bullets",
            "remove bullet points",
            "unbullet",
            "unlist",
            "as plain text",
            "plain text",
        )
    ):
        return CommandIntent(
            action="rewrite_selection",
            text=_unbullet(selection),
            confidence=0.8,
            reason="rules_unbullet",
        )
    if any(p in lower for p in ("bullet", "make a list", "as a list", "bullet list")):
        return CommandIntent(
            action="rewrite_selection",
            text=_bulletize(selection),
            confidence=0.78,
            reason="rules_bulletize",
        )
    if any(p in lower for p in ("shorter", "make this shorter", "summarize this", "condense")):
        return CommandIntent(
            action="rewrite_selection",
            text=_shorten(selection),
            confidence=0.6,
            reason="rules_shorten",
        )
    if any(p in lower for p in ("snake case", "snake_case", "make this snake case")):
        return CommandIntent(
            action="rewrite_selection",
            text=_snake_case(selection),
            confidence=0.82,
            reason="rules_snake_case",
        )
    if any(p in lower for p in ("camel case", "camelcase", "make this camel case")):
        return CommandIntent(
            action="rewrite_selection",
            text=_camel_case(selection),
            confidence=0.82,
            reason="rules_camel_case",
        )
    if any(p in lower for p in ("kebab case", "kebab-case", "make this kebab case", "dash case")):
        return CommandIntent(
            action="rewrite_selection",
            text=_kebab_case(selection),
            confidence=0.82,
            reason="rules_kebab_case",
        )
    if any(
        p in lower
        for p in (
            "constant case",
            "screaming snake",
            "upper snake",
            "make this constant case",
        )
    ):
        return CommandIntent(
            action="rewrite_selection",
            text=_constant_case(selection),
            confidence=0.82,
            reason="rules_constant_case",
        )
    if any(p in lower for p in ("sentence case", "make this sentence case")):
        return CommandIntent(
            action="rewrite_selection",
            text=_sentence_case(selection),
            confidence=0.8,
            reason="rules_sentence_case",
        )
    if any(p in lower for p in ("number list", "numbered list", "as numbers", "number this")):
        return CommandIntent(
            action="rewrite_selection",
            text=_number_list(selection),
            confidence=0.78,
            reason="rules_number_list",
        )
    if any(p in lower for p in ("quote this", "add quotes", "surround with quotes")):
        return CommandIntent(
            action="rewrite_selection",
            text=_quote(selection),
            confidence=0.85,
            reason="rules_quote",
        )
    if any(
        p in lower
        for p in (
            "wrap in parentheses",
            "add parentheses",
            "surround with parentheses",
            "parenthesize",
        )
    ):
        return CommandIntent(
            action="rewrite_selection",
            text=_parenthesize(selection),
            confidence=0.85,
            reason="rules_parenthesize",
        )
    if any(p in lower for p in ("trim", "trim whitespace", "remove extra spaces", "fix spacing")):
        return CommandIntent(
            action="rewrite_selection",
            text=_fix_spacing(selection),
            confidence=0.8,
            reason="rules_fix_spacing",
        )
    if any(p in lower for p in ("reverse", "reverse this", "flip this")):
        return CommandIntent(
            action="rewrite_selection",
            text=_reverse_words(selection),
            confidence=0.75,
            reason="rules_reverse",
        )
    return None


def _formalize(selection: str) -> str:
    cleaned = _fix_grammar(selection)
    if not cleaned:
        return ""
    contractions = (
        (r"\bcan't\b", "cannot"),
        (r"\bdon't\b", "do not"),
        (r"\bwon't\b", "will not"),
        (r"\bisn't\b", "is not"),
        (r"\baren't\b", "are not"),
        (r"\bdoesn't\b", "does not"),
        (r"\bdidn't\b", "did not"),
        (r"\bcouldn't\b", "could not"),
        (r"\bshouldn't\b", "should not"),
        (r"\bwouldn't\b", "would not"),
        (r"\bI'm\b", "I am"),
        (r"\byou're\b", "you are"),
        (r"\bwe're\b", "we are"),
        (r"\bthey're\b", "they are"),
    )
    for pattern, replacement in contractions:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.I)
    return cleaned


def _fix_grammar(selection: str) -> str:
    cleaned = re.sub(r"\s+", " ", selection.strip())
    if not cleaned:
        return ""
    repairs = (
        (r"\bcant\b", "cannot"),
        (r"\bdont\b", "do not"),
        (r"\bwont\b", "will not"),
        (r"\bisnt\b", "is not"),
        (r"\barent\b", "are not"),
        (r"\bdoesnt\b", "does not"),
        (r"\bdidnt\b", "did not"),
        (r"\bcouldnt\b", "could not"),
        (r"\bshouldnt\b", "should not"),
        (r"\bwouldnt\b", "would not"),
        (r"\bim\b", "I'm"),
        (r"\bive\b", "I've"),
        (r"\byoure\b", "you're"),
        (r"\btheyre\b", "they're"),
    )
    for pattern, replacement in repairs:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.I)
    cleaned = re.sub(r"(?i)\bI\s+is\b", "I am", cleaned)
    cleaned = re.sub(r"(?i)\b(you|we|they)\s+is\b", r"\1 are", cleaned)
    cleaned = re.sub(r"(?i)\b(he|she|it)\s+are\b", r"\1 is", cleaned)
    cleaned = re.sub(r"(?i)\b(he|she|it)\s+do not\b", r"\1 does not", cleaned)
    cleaned = re.sub(r"(?i)\b(he|she|it)\s+have\b", r"\1 has", cleaned)
    cleaned = re.sub(r"(?i)\b(I|you|we|they)\s+does not\b", r"\1 do not", cleaned)
    cleaned = re.sub(r"(?i)\b(I|you|we|they)\s+has\b", r"\1 have", cleaned)
    cleaned = re.sub(r"(?i)\b(the\s+[A-Za-z]+s)\s+is\b", r"\1 are", cleaned)
    cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def _warmer(selection: str) -> str:
    cleaned = _fix_grammar(selection)
    if not cleaned:
        return ""
    body = cleaned.rstrip(".!?")
    if re.match(
        r"(?i)^(send|share|review|check|finish|update|call|email|reply|confirm|schedule)\b",
        body,
    ):
        return f"Could you please {body[:1].lower() + body[1:]}? Thank you."
    return f"Just a friendly note: {body[:1].lower() + body[1:]}. Thank you."


def _bulletize(selection: str) -> str:
    lines = [line.strip(" -\t") for line in re.split(r"[\n,;]+", selection) if line.strip()]
    if not lines:
        return selection
    if len(lines) == 1 and " " in lines[0]:
        # Split a single prose line into rough clauses.
        parts = re.split(r"\s+and\s+|;\s*", lines[0])
        lines = [p.strip() for p in parts if p.strip()] or lines
    return "\n".join(f"- {line}" for line in lines)


def _shorten(selection: str) -> str:
    cleaned = re.sub(r"\s+", " ", selection.strip())
    cleaned = re.sub(
        r"(?i)^(?:I\s+)?just wanted to (?:let you know|say)(?: that)?\s+",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\b(?:really|very|basically|actually|just)\b\s*", "", cleaned)
    cleaned = re.sub(r"(?i)\bin order to\b", "to", cleaned)
    cleaned = re.sub(r"(?i)\bdue to the fact that\b", "because", cleaned)
    cleaned = re.sub(r"(?i)\bat this point in time\b", "now", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= 120:
        return cleaned
    # Keep the first sentence when present; otherwise hard-trim with ellipsis.
    match = re.match(r"^(.+?[.!?])(?:\s|$)", cleaned)
    if match and len(match.group(1)) >= 20:
        return match.group(1)
    return cleaned[:117].rstrip() + "..."


def _snake_case(selection: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", selection.strip())
    cleaned = re.sub(r"[\s\-]+", "_", cleaned)
    return cleaned.lower().strip("_")


def _camel_case(selection: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", selection)
    if not words:
        return selection.strip()
    head, *tail = words
    return head.lower() + "".join(w[:1].upper() + w[1:].lower() for w in tail)


def _unbullet(selection: str) -> str:
    lines = []
    for line in selection.splitlines():
        stripped = re.sub(r"^\s*(?:[-*•]|\d+\.)\s+", "", line).strip()
        if stripped:
            lines.append(stripped)
    return " ".join(lines) if lines else selection.strip()


def _kebab_case(selection: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", selection.strip())
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    return cleaned.lower().strip("-")


def _constant_case(selection: str) -> str:
    return _snake_case(selection).upper()


def _sentence_case(selection: str) -> str:
    cleaned = re.sub(r"\s+", " ", selection.strip())
    if not cleaned:
        return ""
    lower = cleaned.lower()
    return lower[0].upper() + lower[1:]


def _number_list(selection: str) -> str:
    lines = [line.strip(" -\t") for line in re.split(r"[\n,;]+", selection) if line.strip()]
    if not lines:
        return selection
    if len(lines) == 1 and " " in lines[0]:
        parts = re.split(r"\s+and\s+|;\s*", lines[0])
        lines = [p.strip() for p in parts if p.strip()] or lines
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))


def _quote(selection: str) -> str:
    cleaned = selection.strip()
    if not cleaned:
        return '""'
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (
        cleaned.startswith("'") and cleaned.endswith("'")
    ):
        return cleaned
    return f'"{cleaned}"'


def _parenthesize(selection: str) -> str:
    cleaned = selection.strip()
    if not cleaned:
        return "()"
    if cleaned.startswith("(") and cleaned.endswith(")"):
        return cleaned
    return f"({cleaned})"


def _fix_spacing(selection: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in selection.splitlines()]
    # Collapse runs of blank lines to a single blank line.
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank and out:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(line)
    return "\n".join(out).strip()


def _reverse_words(selection: str) -> str:
    words = selection.split()
    if not words:
        return selection
    return " ".join(reversed(words))
