# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Quote a Python string as an AppleScript string literal.

macOS is the one platform where DCENT_Voice's user-visible dialogs are built by
*generating source code* — an ``osascript -e`` fragment — rather than by passing
text to an API. That makes quoting a correctness problem, and the obvious
shortcut is wrong in a way that only shows up on a Mac:

``json.dumps`` produces a JSON string literal, not an AppleScript one. With the
default ``ensure_ascii=True`` every non-ASCII character becomes ``\\uXXXX`` —
and **AppleScript has no ``\\u`` escape**. It reads ``\\u`` as a plain ``u``, so
``DCENT_Voice — could not start`` reached the user as ``DCENT_Voice u2014 could
not start``. Every fatal dialog we show on macOS contains that em dash.

Turning ``ensure_ascii`` off is not the fix either, because the other half of
what ``json.dumps`` was doing is still needed: an AppleScript string literal
cannot span lines, so a real newline in an ``-e`` argument is a syntax error, not
a line break. Our dialog bodies are paragraphs separated by blank lines.

So: escape exactly what AppleScript defines — backslash, double quote, and the
newline/return/tab control characters — and pass every other character through
unchanged, including Unicode. ``osascript`` reads its argument as UTF-8.
"""

from __future__ import annotations

#: The only escapes AppleScript string literals define. Backslash MUST be first:
#: escaping it after the others would double the backslashes they introduce.
_ESCAPES = (
    ("\\", "\\\\"),
    ('"', '\\"'),
    ("\r\n", "\\n"),
    ("\r", "\\n"),
    ("\n", "\\n"),
    ("\t", "\\t"),
)


def escape(text: str) -> str:
    """Escape ``text`` for the inside of an AppleScript string literal."""
    result = str(text)
    for character, replacement in _ESCAPES:
        result = result.replace(character, replacement)
    # Any remaining C0 control character has no AppleScript escape and would
    # corrupt the literal. None of them belongs in a dialog anyway.
    return "".join(character if character >= " " else " " for character in result)


def quote(text: str) -> str:
    """Render ``text`` as a complete AppleScript string literal, quotes included.

    ``quote('say "hi"')`` returns the 12 characters ``"say \\"hi\\""`` — ready to
    interpolate straight into an ``osascript -e`` fragment.
    """
    return f'"{escape(text)}"'
