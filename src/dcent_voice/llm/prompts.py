# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Build prompts used by the transcription cleanup pipeline."""

from __future__ import annotations

from dcent_voice.config import SnippetEntry, VocabEntry, starred_first

CLEANUP_SYSTEM_PROMPT = """You clean up voice dictation only.
Do NOT add facts, answers, lists, explanations, or new ideas.
Do NOT invent content if the input is gibberish — return lightly punctuated input
or a short empty string.
Preserve the speaker's meaning and technical terms exactly.
Remove filler words, false starts, and exact repeated fragments.
Add punctuation and capitalization.
Return ONLY the cleaned dictation text, with no quotes or commentary."""

_STYLE_ADDENDA = {
    "email": (
        "Destination: email. Keep meaning. Use short paragraphs and email prose. "
        "Expand yeah/gonna/wanna. If the text starts with a greeting, put it on "
        "its own line. If it ends with thanks or regards, put that on its own line. "
        "Do not invent a subject line or signature block."
    ),
    "chat": (
        "Destination: chat message. Keep meaning. Casual, concise. "
        "Drop spoken lead-ins like 'I just wanted to say'. "
        "Drop the final period on a short statement. Do not add a greeting."
    ),
    "formal": (
        "Destination: formal prose. Keep meaning. Expand contractions. "
        "Drop hedges such as 'to be honest', 'basically', 'I guess'. "
        "Use complete sentences. Do not add titles or letterhead."
    ),
    "code": (
        "Destination: code or terminal. Preserve identifiers, paths, and CLI "
        "tokens exactly. Do not wrap in markdown fences. Do not add commentary."
    ),
    "notes": (
        "Destination: structured notes. Keep meaning. Prefer short sections "
        "headed Decision, Action items, or Next steps when the speaker used "
        "those cues. Use markdown bullets for tasks. Do not invent agenda "
        "items, attendees, or meeting metadata that was not spoken."
    ),
}


def cleanup_system_prompt(style: str | None = None) -> str:
    """System prompt for optional cleanup. Style names the destination, not a persona."""
    name = (style or "plain").strip().lower()
    addendum = _STYLE_ADDENDA.get(name)
    if not addendum:
        return CLEANUP_SYSTEM_PROMPT
    return f"{CLEANUP_SYSTEM_PROMPT}\n{addendum}"


def cleanup_user_prompt(
    raw_text: str,
    dictionary: tuple[VocabEntry, ...] = (),
    snippets: tuple[SnippetEntry, ...] | tuple[object, ...] = (),
) -> str:
    """Starred snippet cues lead cleanup. Expansions stay in the prompt so polish keeps them."""
    vocab_lines: list[str] = []

    def _add_dictionary(*, starred: bool) -> None:
        for entry in starred_first(dictionary):
            if bool(entry.starred) != starred:
                continue
            spoken = (entry.spoken or "").strip()
            written = (entry.written or "").strip()
            if spoken and written:
                vocab_lines.append(f"- {spoken} => {written}")

    def _add_snippets(*, starred: bool) -> None:
        ordered = sorted(
            snippets or (),
            key=lambda entry: 0 if getattr(entry, "starred", False) else 1,
        )
        for entry in ordered:
            if bool(getattr(entry, "starred", False)) != starred:
                continue
            cue = str(getattr(entry, "spoken", "") or "").strip()
            expansion = str(getattr(entry, "expansion", "") or "").strip()
            if not cue or not expansion:
                continue
            if len(expansion) > 80:
                expansion = f"{expansion[:79]}…"
            vocab_lines.append(f"- {cue} => {expansion}")

    _add_dictionary(starred=True)
    _add_snippets(starred=True)
    _add_dictionary(starred=False)
    _add_snippets(starred=False)
    lines: list[str] = []
    if vocab_lines:
        lines.append("Use this custom vocabulary exactly when relevant:")
        lines.extend(vocab_lines)
        lines.append("")
    lines.append("Dictation:")
    lines.append(raw_text)
    return "\n".join(lines)
