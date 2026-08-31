# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Load, validate, and serialize application configuration."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import math
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dcent_voice.asr.base import (
    Locality,
    UnsupportedLanguageError,
    normalize_language_hint,
)

try:  # pragma: no cover - Python 3.11+ uses stdlib; fallback helps local 3.10 test runners.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

APP_NAME = "DCENT_Voice"
CONFIG_FILENAME = "config.toml"
CURRENT_CONFIG_VERSION = 2

LOCAL_ASR_PROVIDERS = {"faster-whisper", "whisper-cpp", "parakeet"}
LOCAL_LLM_PROVIDERS = {"ollama", "lmstudio", "none"}
# ``piper`` stays valid for existing config files but is deferred from this
# public beta pending compatible voice licensing. ``auto`` selects Kokoro only.
TTS_BACKENDS = {"kokoro", "piper", "auto"}
TTS_MIC_POLICIES = {"pause", "duck", "off"}

logger = logging.getLogger(APP_NAME).getChild("config")

_KNOWN_TOP_LEVEL = {
    "config_version",
    "active_profile",
    "language",
    "language_mode",
    "cleanup_enabled",
    "launch_at_startup",
    "idle_unload_s",
    "hotkeys",
    "overlay",
    "service",
    "audio",
    "injector",
    "privacy",
    "recovery",
    "commands",
    "profile",
    "dictionary",
    "snippets",
    "dictation",
    "tts",
    "personalization",
    "style",
    "lists",
}

# Previous shipped desktop pins. Distil always moves to the Whisper fallback.
# When Parakeet ONNX weights are already on disk (Setup.exe bundle), both
# distil and the prior base.en default move to the current shipped engine.
# Never download 640 MB just to migrate.
_STALE_DISTIL_ASR = frozenset(
    {
        "faster-whisper:distil-small.en:cpu-int8",
        "faster-whisper:distil-small.en:int8",
    }
)
_WHISPER_FALLBACK_ASR = "faster-whisper:base.en:cpu-int8"
_SHIPPED_DESKTOP_ASR = "parakeet:tdt-0.6b-v3:int8"
_STALE_WHEN_PARAKEET = _STALE_DISTIL_ASR | {_WHISPER_FALLBACK_ASR}


def migrate_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Bring an older on-disk config forward to the current schema.

    A single hook so future key renames/moves live in one place instead of being
    scattered through the parser.
    """
    version = raw.get("config_version", 1)
    if not isinstance(version, int):
        version = 1
    migrated = dict(raw)
    if "language_mode" not in migrated:
        language = str(migrated.get("language", "en")).strip().lower()
        if language in {"auto", "detect"}:
            migrated["language_mode"] = "auto"
        elif language in {"", "en", "eng", "en-us", "en-gb"}:
            migrated["language_mode"] = "english"
        else:
            migrated["language_mode"] = "multilingual"
    return migrated


class ConfigError(ValueError):
    """Raised when config.toml cannot be parsed into a valid AppConfig."""


class ConfigUnreadableError(ConfigError):
    """Raised when config.toml could not be *read* — an I/O failure, not bad content.

    The distinction decides whether we are allowed to quarantine the user's
    file. Invalid TOML or a bad value means the file is genuinely unusable and
    resetting it is the recoverable choice. A ``PermissionError`` means nothing
    about the content: the file may be perfectly good and merely locked for a
    moment by an antivirus scan, an open editor, or a roaming-profile sync.
    Resetting on that would destroy working settings over a transient error, so
    this subclass is deliberately excluded from the recovery path.
    """


@dataclass(frozen=True)
class ASRSpec:
    raw: str
    provider: str
    model: str
    compute_type: str | None = None

    @property
    def locality(self) -> Locality:
        return Locality.LOCAL if self.provider in LOCAL_ASR_PROVIDERS else Locality.CLOUD

    @classmethod
    def parse(cls, raw: str) -> ASRSpec:
        value = _clean_spec(raw, "ASR")
        parts = value.split(":")
        if len(parts) < 2:
            raise ConfigError(
                f"Invalid ASR spec {raw!r}. Expected '<provider>:<model>[:compute_type]'."
            )
        provider = parts[0].strip().lower()
        model = parts[1].strip()
        compute_type = ":".join(parts[2:]).strip() or None
        if not provider or not model:
            raise ConfigError(f"Invalid ASR spec {raw!r}. Provider and model are required.")
        return cls(raw=value, provider=provider, model=model, compute_type=compute_type)


@dataclass(frozen=True)
class LLMSpec:
    raw: str
    provider: str
    model: str | None = None

    @property
    def locality(self) -> Locality:
        return Locality.LOCAL if self.provider in LOCAL_LLM_PROVIDERS else Locality.CLOUD

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    @classmethod
    def parse(cls, raw: str) -> LLMSpec:
        value = _clean_spec(raw, "LLM")
        if value.lower() in {"none", "off", "disabled"}:
            return cls(raw="none", provider="none", model=None)

        provider, sep, model = value.partition(":")
        provider = provider.strip().lower()
        model = model.strip()
        if not sep or not provider or not model:
            raise ConfigError(f"Invalid LLM spec {raw!r}. Expected '<provider>:<model>' or 'none'.")
        return cls(raw=value, provider=provider, model=model)


@dataclass(frozen=True)
class VocabEntry:
    spoken: str
    written: str
    starred: bool = False
    added_at: str = ""


def starred_first(dictionary: tuple[VocabEntry, ...]) -> tuple[VocabEntry, ...]:
    """Starred terms lead ASR/cleanup hints. Display sort does not change this."""

    return tuple(sorted(dictionary, key=lambda entry: 0 if entry.starred else 1))


@dataclass(frozen=True)
class SnippetEntry:
    """Voice shortcut: spoken cue expands to ``expansion`` text."""

    spoken: str
    expansion: str
    starred: bool = False
    added_at: str = ""


def _now_added_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_added_at(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


STARTER_SNIPPETS: tuple[SnippetEntry, ...] = (
    SnippetEntry(spoken="my email", expansion=""),
    SnippetEntry(spoken="my calendar", expansion=""),
    SnippetEntry(spoken="my signature", expansion=""),
)


def effective_snippets(snippets: tuple[SnippetEntry, ...] | None) -> tuple[SnippetEntry, ...]:
    """Show starter cues only when snippets were never saved.

    ``None`` (missing ``[snippets]``) → first-run starters.
    ``()`` (saved ``items = []``) → stay empty; deleted cues do not come back.
    Empty expansions do not rewrite speech. Does not write ``config.toml``.
    """
    if snippets is None:
        return STARTER_SNIPPETS
    return snippets


MAX_SNIPPET_SPOKEN = 60
MAX_SNIPPET_EXPANSION = 4000
MAX_SNIPPET_IMPORT_ITEMS = 1000
MAX_SNIPPET_IMPORT_BYTES = 3_000_000
MAX_DICTIONARY_SPOKEN = 60
MAX_DICTIONARY_WRITTEN = 4000
MAX_DICTIONARY_IMPORT_ITEMS = 1000
MAX_DICTIONARY_IMPORT_BYTES = 3_000_000


def parse_snippet_import(payload: Any) -> tuple[SnippetEntry, ...]:
    """Parse a local snippet JSON document. Not a cloud sync payload.

    Accepts DCENT ``spoken``/``expansion`` and generic ``name``/``text``.
    """
    entries, _stats = load_snippet_import(payload)
    return entries


def load_snippet_import(payload: Any) -> tuple[tuple[SnippetEntry, ...], dict[str, Any]]:
    """Parse snippet JSON. Bad rows are skipped so valid rows still import."""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise ConfigError("Snippet import is empty.")
        if len(text.encode("utf-8")) > MAX_SNIPPET_IMPORT_BYTES:
            raise ConfigError("Snippet import is larger than 3 MB.")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError("Snippet import must be JSON.") from exc
    if isinstance(payload, dict):
        items = payload.get("items", payload.get("snippets"))
        if items is None:
            raise ConfigError("Snippet import needs an items array.")
    elif isinstance(payload, list):
        items = payload
    else:
        raise ConfigError("Snippet import must be a JSON object or array.")
    if not isinstance(items, list):
        raise ConfigError("Snippet import items must be an array.")
    parsed: list[SnippetEntry] = []
    skip_changes: list[dict[str, Any]] = []
    entry_indexes: list[int] = []
    skipped_empty = 0
    skipped_malformed = 0
    skipped_overflow = 0
    if len(items) > MAX_SNIPPET_IMPORT_ITEMS:
        raise ConfigError("Cannot import more than 1,000 entries.")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            skipped_malformed += 1
            skip_changes.append(_snippet_skip_row("", "", "malformed", index=index))
            continue
        spoken = _snippet_import_cue(item)
        expansion = _snippet_import_expansion(item)
        if not spoken:
            skipped_empty += 1
            skip_changes.append(_snippet_skip_row("", expansion or "", "empty", index=index))
            continue
        if expansion is None:
            skipped_malformed += 1
            skip_changes.append(_snippet_skip_row(spoken, "", "malformed", index=index))
            continue
        if not expansion.strip():
            skipped_empty += 1
            skip_changes.append(_snippet_skip_row(spoken, expansion, "empty", index=index))
            continue
        if len(spoken) > MAX_SNIPPET_SPOKEN or len(expansion) > MAX_SNIPPET_EXPANSION:
            skipped_malformed += 1
            skip_changes.append(_snippet_skip_row(spoken, expansion, "malformed", index=index))
            continue
        parsed.append(
            SnippetEntry(
                spoken=spoken,
                expansion=expansion,
                starred=_snippet_import_starred(item),
                added_at=_parse_added_at(item.get("added_at")),
            )
        )
        entry_indexes.append(index)
    return tuple(parsed), {
        "read": len(parsed) + skipped_empty + skipped_malformed + skipped_overflow,
        "skipped_empty": skipped_empty,
        "skipped_malformed": skipped_malformed,
        "skipped_overflow": skipped_overflow,
        "skip_changes": skip_changes,
        "entry_indexes": entry_indexes,
    }


def _snippet_import_cue(item: dict[str, Any]) -> str:
    for key in ("spoken", "name", "trigger"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _snippet_import_expansion(item: dict[str, Any]) -> str | None:
    found = False
    result = ""
    for key in ("expansion", "text", "content"):
        if key not in item:
            continue
        value = item[key]
        if not isinstance(value, str):
            return None
        if not found:
            result = value
            found = True
    return result


def _snippet_import_starred(item: dict[str, Any]) -> bool:
    return item.get("starred") is True


def _snippet_skip_row(
    spoken: str,
    expansion: str,
    reason: str,
    *,
    index: int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "spoken": spoken,
        "expansion": expansion,
        "action": "skip",
        "reason": reason,
    }
    if index is not None:
        row["index"] = index
    return row


def _interleave_snippet_skip_rows(
    plan_changes: list[dict[str, Any]],
    *,
    skip_changes: Sequence[dict[str, Any]] = (),
    entry_indexes: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """Put parse skip rows back in file order among plan add/skip rows."""
    if not skip_changes:
        return plan_changes
    by_index: dict[int, dict[str, Any]] = {}
    indexes = list(entry_indexes) if entry_indexes is not None else list(range(len(plan_changes)))
    for idx, change in zip(indexes, plan_changes, strict=True):
        by_index[int(idx)] = change
    for row in skip_changes:
        by_index[int(row["index"])] = _snippet_skip_row(
            row.get("spoken") or "",
            row.get("expansion") or "",
            str(row.get("reason") or "malformed"),
        )
    return [by_index[idx] for idx in sorted(by_index)]


def merge_snippet_entries(
    existing: tuple[SnippetEntry, ...] | None,
    incoming: tuple[SnippetEntry, ...],
) -> tuple[SnippetEntry, ...]:
    """Merge imported cues into saved snippets. Same cue (case-insensitive) is skipped."""
    by_key: dict[str, SnippetEntry] = {}
    order: list[str] = []
    for entry in existing or ():
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = entry
    for entry in incoming:
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key in by_key:
            continue
        order.append(key)
        by_key[key] = (
            entry
            if entry.added_at
            else SnippetEntry(
                spoken=entry.spoken,
                expansion=entry.expansion,
                starred=entry.starred,
                added_at=_now_added_at(),
            )
        )
    return tuple(by_key[key] for key in order)


def _apply_snippet_import_entries(
    existing: tuple[SnippetEntry, ...] | None,
    incoming: tuple[SnippetEntry, ...],
) -> tuple[SnippetEntry, ...]:
    """Overlay imported cues, replacing matching spoken cues and applying stars."""
    by_key: dict[str, SnippetEntry] = {}
    order: list[str] = []
    for entry in existing or ():
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = entry
    for entry in incoming:
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key in by_key:
            old = by_key[key]
            by_key[key] = SnippetEntry(
                spoken=entry.spoken,
                expansion=entry.expansion,
                starred=old.starred or entry.starred,
                added_at=old.added_at or entry.added_at or _now_added_at(),
            )
            continue
        order.append(key)
        by_key[key] = (
            entry
            if entry.added_at
            else SnippetEntry(
                spoken=entry.spoken,
                expansion=entry.expansion,
                starred=entry.starred,
                added_at=_now_added_at(),
            )
        )
    return tuple(by_key[key] for key in order)


def starred_import_detail(starred_added: int) -> str:
    """Import preview and apply toast both append this star count. Empty when none."""
    n = int(starred_added or 0)
    return f", starred {n}" if n > 0 else ""


def snippet_star_aria(starred: bool) -> str:
    """Snippet star control names dictation priority on this machine."""
    return (
        "Starred — dictation priority on this machine"
        if starred
        else "Star snippet for dictation priority"
    )


def starred_count_detail(starred: int) -> str:
    """List count appends how many visible rows are starred. Empty when none."""
    n = int(starred or 0)
    return f", {n} starred" if n > 0 else ""


def starred_only_empty_label(unit: str) -> str:
    """Starred-only filter names when no visible starred rows remain."""
    return "No starred terms" if unit == "terms" else "No starred snippets"


def starred_only_entries(entries, starred_only: bool = False):
    """Starred-only export keeps starred rows. Full list when the filter is off."""
    rows = tuple(entries or ())
    if not starred_only:
        return rows
    return tuple(entry for entry in rows if entry.starred)


def matches_list_query(*parts: str, query: str = "") -> bool:
    """Export and list search match spoken/written/expansion on this machine."""
    needle = str(query or "").strip().lower()
    if not needle:
        return True
    return any(needle in str(part or "").lower() for part in parts)


def export_empty_toast(starred_only: bool, query: str = "") -> str:
    """Empty export toast names a search miss before Starred only."""
    if str(query or "").strip():
        return "Nothing visible to export"
    return "Nothing starred to export" if starred_only else "Nothing to export"


def export_done_toast(kind: str, starred_only: bool, query: str = "") -> str:
    """Successful export toast names visible when a search filter is on."""
    if str(query or "").strip():
        return (
            "Dictionary exported — visible"
            if kind == "dictionary"
            else "Snippets exported — visible"
        )
    if kind == "dictionary":
        return (
            "Dictionary exported — starred only"
            if starred_only
            else "Dictionary exported — stars included"
        )
    return "Snippets exported — starred only" if starred_only else "Snippets exported"


def export_download_name(kind: str, query: str = "", starred_only: bool = False) -> str:
    """Name an export for its visible, starred-only, or full contents."""
    if str(query or "").strip():
        return (
            "dcent-dictionary-visible.csv"
            if kind == "dictionary"
            else "dcent-snippets-visible.json"
        )
    if starred_only:
        return (
            "dcent-dictionary-starred.csv"
            if kind == "dictionary"
            else "dcent-snippets-starred.json"
        )
    return "dcent-dictionary.csv" if kind == "dictionary" else "dcent-snippets.json"


def import_cancelled_toast(kind: str) -> str:
    """Cancel names dictionary or snippets. Not a generic Import cancelled."""
    return "Dictionary import cancelled" if kind == "dictionary" else "Snippet import cancelled"


def import_applied_toast(
    kind: str, added: int, replaced: int, skipped: int, extra: str = ""
) -> str:
    """Apply names dictionary or snippets. Not a generic Imported count."""
    lead = "Dictionary imported" if kind == "dictionary" else "Snippets imported"
    return f"{lead} {int(added)}, replaced {int(replaced)}, skipped {int(skipped)}{extra}"


def import_review_summary(
    kind: str, added: int, replaced: int, skipped: int, extra: str = ""
) -> str:
    """Review names dictionary or snippets. Not a generic Import count."""
    lead = "Dictionary" if kind == "dictionary" else "Snippets"
    return (
        f"{lead}: import {int(added)}, replace {int(replaced)}, skip {int(skipped)}"
        f"{extra}. Nothing is saved until you apply."
    )


def import_empty_review_summary(kind: str, skipped: int, extra: str = "") -> str:
    """Empty review names dictionary or snippets. Not a generic nothing-imported line."""
    lead = "Dictionary" if kind == "dictionary" else "Snippets"
    return f"{lead}: Nothing will be imported. Skip {int(skipped)}{extra}."


def import_undone_toast(kind: str = "snippets") -> str:
    """Undo names dictionary or snippets. Not a generic Import undone."""
    return "Dictionary import undone" if kind == "dictionary" else "Snippet import undone"


def import_undo_button_label(kind: str = "snippets") -> str:
    """The undo button names dictionary or snippets. Not a generic Undo import."""
    return "Undo dictionary import" if kind == "dictionary" else "Undo snippet import"


def import_undo_help_label(kind: str = "snippets") -> str:
    """Undo help names dictionary or snippets. Not a generic Undo import."""
    return "Undo dictionary import" if kind == "dictionary" else "Undo snippet import"


def dictionary_undo_steps_help() -> str:
    """Dictionary undo steps through each apply. Not a cloud team import."""
    return "Dictionary undo steps through each apply"


def snippet_undo_steps_help() -> str:
    """Snippet undo steps through each apply. Not a cloud team import."""
    return "Snippet undo steps through each apply"


def import_apply_button_label(kind: str) -> str:
    """The apply button names dictionary or snippets. Not a generic Apply import."""
    return "Apply dictionary import" if kind == "dictionary" else "Apply snippet import"


def import_cancel_button_label(kind: str) -> str:
    """The cancel button names dictionary or snippets. Not a generic Cancel."""
    return "Cancel dictionary import" if kind == "dictionary" else "Cancel snippet import"


def import_done_button_label(kind: str) -> str:
    """The done button names dictionary or snippets. Not a generic Done."""
    return "Done dictionary import" if kind == "dictionary" else "Done snippet import"


def import_done_help_label(kind: str) -> str:
    """Done help names dictionary or snippets. Not a generic Done."""
    return "Done dictionary import" if kind == "dictionary" else "Done snippet import"


def remove_visible_needs_search(query: str, starred_only: bool) -> bool:
    """Remove visible requires a search unless Starred only is on."""
    return not str(query or "").strip() and not bool(starred_only)


def remove_visible_noun(kind: str, count: int) -> str:
    """Remove visible names dictionary or snippets. Not a generic row."""
    if kind == "dictionary":
        return "term" if int(count) == 1 else "terms"
    return "snippet" if int(count) == 1 else "snippets"


def remove_visible_confirm(visible: int, starred: int, noun: str) -> str:
    """Remove-visible confirm names how many of the visible rows are starred."""
    return (
        f"Remove {int(visible)} visible {noun}{starred_count_detail(starred)}? "
        "This is on this machine — not a cloud team bulk delete."
    )


def remove_visible_toast(visible: int, starred: int, noun: str) -> str:
    """Remove-visible toast names how many of the removed rows were starred."""
    return f"Removed {int(visible)} visible {noun}{starred_count_detail(starred)} — save to keep"


def remove_visible_empty_toast(starred_only: bool, kind: str = "snippets") -> str:
    """Empty remove names dictionary or snippets. Starred empty names kind too."""
    if starred_only:
        return (
            "Nothing starred terms to remove"
            if kind == "dictionary"
            else "Nothing starred snippets to remove"
        )
    return "No terms to remove" if kind == "dictionary" else "No snippets to remove"


def remove_visible_search_toast() -> str:
    """Remove visible without a filter names both search and Starred only."""
    return "Search or Starred only, then remove visible"


def overlay_priority_title(text: str) -> str:
    """Hover and aria-label for the overlay chip. Empty hides it."""
    form = str(text or "").strip()
    return f"Starred: {form}" if form else ""


def snippet_import_row_starred(row: dict | None) -> bool:
    """Import review names Starred only on add or replace, never on skip."""
    if not isinstance(row, dict):
        return False
    if row.get("action") not in {"add", "replace"}:
        return False
    return row.get("starred") is True


def plan_snippet_import(
    existing: tuple[SnippetEntry, ...] | None,
    incoming: tuple[SnippetEntry, ...],
    *,
    dictionary: tuple[VocabEntry, ...] = (),
    skipped_empty: int = 0,
    skipped_malformed: int = 0,
    skipped_overflow: int = 0,
    read: int | None = None,
    skip_changes: Sequence[dict[str, Any]] = (),
    entry_indexes: Sequence[int] | None = None,
) -> tuple[tuple[SnippetEntry, ...], dict[str, Any]]:
    """Plan snippet additions, replacements, and skips.

    Existing snippets are replaced when the expansion changes. Snippet JSON
    applies stars on replacement. Unchanged existing snippets are skipped.

    Duplicate cues in the same file keep the first occurrence (first-wins).
    Later copies count as skipped and are listed as skip in the review, including
    later copies of a cue already saved or colliding with dictionary.
    Dictionary cues are skipped if new.
    Empty and invalid parse rows are listed as skip in file order.
    Each skip row includes a reason (empty, malformed, duplicate, existing,
    or dictionary).
    """
    existing_by_key: dict[str, SnippetEntry] = {}
    for entry in existing or ():
        key = (entry.spoken or "").strip().lower()
        if key and key not in existing_by_key:
            existing_by_key[key] = entry
    existing_keys = set(existing_by_key)
    dict_keys = {
        (entry.spoken or "").strip().lower() for entry in dictionary if (entry.spoken or "").strip()
    }
    added = replaced = skipped_dictionary = skipped_duplicate = skipped_existing = 0
    starred_added = 0
    kept: list[SnippetEntry] = []
    seen: set[str] = set()
    changes: list[dict[str, Any]] = []
    for entry in incoming:
        key = (entry.spoken or "").strip().lower()
        if not key:
            skipped_empty += 1
            continue
        if key in seen:
            skipped_duplicate += 1
            changes.append(_snippet_skip_row(entry.spoken, entry.expansion, "duplicate"))
            continue
        seen.add(key)
        if key in dict_keys and key not in existing_keys:
            skipped_dictionary += 1
            changes.append(_snippet_skip_row(entry.spoken, entry.expansion, "dictionary"))
            continue
        if key in existing_keys:
            current = existing_by_key[key]
            if (entry.expansion or "").strip() == (current.expansion or "").strip():
                skipped_existing += 1
                changes.append(_snippet_skip_row(entry.spoken, entry.expansion, "existing"))
                continue
            replaced += 1
            starred = current.starred or entry.starred
            kept.append(
                SnippetEntry(
                    spoken=entry.spoken,
                    expansion=entry.expansion,
                    starred=starred,
                    added_at=current.added_at,
                )
            )
            changes.append(
                {
                    "spoken": entry.spoken,
                    "expansion": entry.expansion,
                    "action": "replace",
                    **({"starred": True} if starred else {}),
                }
            )
            continue
        added += 1
        if entry.starred:
            starred_added += 1
        kept.append(entry)
        changes.append(
            {
                "spoken": entry.spoken,
                "expansion": entry.expansion,
                "action": "add",
                **({"starred": True} if entry.starred else {}),
            }
        )
    merged = _apply_snippet_import_entries(existing, tuple(kept))
    skipped = (
        skipped_empty
        + skipped_dictionary
        + skipped_duplicate
        + skipped_existing
        + skipped_malformed
        + skipped_overflow
    )
    return merged, {
        "added": added,
        "replaced": replaced,
        "skipped": skipped,
        "skipped_empty": skipped_empty,
        "skipped_dictionary": skipped_dictionary,
        "skipped_duplicate": skipped_duplicate,
        "skipped_existing": skipped_existing,
        "skipped_malformed": skipped_malformed,
        "skipped_overflow": skipped_overflow,
        "read": (
            len(incoming) + skipped_empty + skipped_malformed + skipped_overflow
            if read is None
            else read
        ),
        "applied": added + replaced,
        "starred_added": starred_added,
        "starred_detail": starred_import_detail(starred_added),
        "changes": _interleave_snippet_skip_rows(
            changes,
            skip_changes=skip_changes,
            entry_indexes=entry_indexes,
        ),
    }


def snippet_export_payload(
    snippets: tuple[SnippetEntry, ...] | None,
    *,
    starred_only: bool = False,
    query: str = "",
) -> dict[str, Any]:
    """Serialize saved snippets for a local JSON file. Omits first-run starters."""
    items = [
        {
            "spoken": entry.spoken,
            "name": entry.spoken,
            "expansion": entry.expansion,
            "text": entry.expansion,
            "starred": entry.starred,
            "added_at": entry.added_at,
        }
        for entry in starred_only_entries(snippets, starred_only)
        if (entry.spoken or "").strip()
        and matches_list_query(entry.spoken, entry.expansion, query=query)
    ]
    return {"schema": "dcent-snippets-v1", "items": items}


def dictionary_export_payload(
    terms: tuple[VocabEntry, ...] | None,
    *,
    starred_only: bool = False,
    query: str = "",
) -> str:
    """Serialize saved dictionary terms as spoken, written, starred CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for entry in starred_only_entries(terms, starred_only):
        spoken = (entry.spoken or "").strip()
        written = (entry.written or "").strip() or spoken
        if not spoken:
            continue
        if not matches_list_query(spoken, written, query=query):
            continue
        writer.writerow([spoken, written, "true" if entry.starred else "false"])
    return buf.getvalue()


def _csv_starred(cell: str) -> bool:
    return str(cell or "").strip().lower() in {"1", "true", "yes", "starred", "star"}


def load_dictionary_import(payload: Any) -> tuple[tuple[VocabEntry, ...], dict[str, Any]]:
    """Parse a local dictionary CSV with optional written and starred columns."""
    if not isinstance(payload, str):
        raise ConfigError("Dictionary import must be CSV text.")
    text = payload.strip()
    if not text:
        raise ConfigError("Dictionary import is empty.")
    if len(text.encode("utf-8")) > MAX_DICTIONARY_IMPORT_BYTES:
        raise ConfigError("Dictionary import is larger than 3 MB.")
    try:
        rows = list(csv.reader(io.StringIO(payload)))
    except csv.Error as exc:
        raise ConfigError("Dictionary import must be CSV.") from exc
    if len(rows) > MAX_DICTIONARY_IMPORT_ITEMS:
        raise ConfigError("Cannot import more than 1,000 entries.")
    parsed: list[VocabEntry] = []
    skip_changes: list[dict[str, Any]] = []
    entry_indexes: list[int] = []
    skipped_empty = 0
    skipped_malformed = 0
    skipped_overflow = 0
    for index, row in enumerate(rows):
        cells = [str(cell) if cell is not None else "" for cell in row]
        while cells and not str(cells[-1]).strip():
            cells.pop()
        if not cells or not any(str(cell).strip() for cell in cells):
            skipped_empty += 1
            skip_changes.append(_snippet_skip_row("", "", "empty", index=index))
            continue
        if len(cells) > 3:
            skipped_malformed += 1
            skip_changes.append(
                _snippet_skip_row(cells[0].strip(), cells[1].strip(), "malformed", index=index)
            )
            continue
        spoken = cells[0].strip()
        written = cells[1].strip() if len(cells) >= 2 else spoken
        starred = _csv_starred(cells[2]) if len(cells) >= 3 else False
        if not spoken or not written:
            skipped_empty += 1
            skip_changes.append(_snippet_skip_row(spoken, written, "empty", index=index))
            continue
        if len(spoken) > MAX_DICTIONARY_SPOKEN or len(written) > MAX_DICTIONARY_WRITTEN:
            skipped_malformed += 1
            skip_changes.append(_snippet_skip_row(spoken, written, "malformed", index=index))
            continue
        parsed.append(VocabEntry(spoken=spoken, written=written, starred=starred))
        entry_indexes.append(index)
    return tuple(parsed), {
        "read": len(parsed) + skipped_empty + skipped_malformed + skipped_overflow,
        "skipped_empty": skipped_empty,
        "skipped_malformed": skipped_malformed,
        "skipped_overflow": skipped_overflow,
        "skip_changes": skip_changes,
        "entry_indexes": entry_indexes,
    }


def merge_vocab_entries(
    existing: tuple[VocabEntry, ...] | None,
    incoming: tuple[VocabEntry, ...],
) -> tuple[VocabEntry, ...]:
    """Merge imported dictionary terms. Same spoken cue is skipped."""
    by_key: dict[str, VocabEntry] = {}
    order: list[str] = []
    for entry in existing or ():
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = entry
    for entry in incoming:
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key in by_key:
            continue
        order.append(key)
        by_key[key] = (
            entry
            if entry.added_at
            else VocabEntry(
                spoken=entry.spoken,
                written=entry.written,
                starred=entry.starred,
                added_at=_now_added_at(),
            )
        )
    return tuple(by_key[key] for key in order)


def _apply_dictionary_import_entries(
    existing: tuple[VocabEntry, ...] | None,
    incoming: tuple[VocabEntry, ...],
) -> tuple[VocabEntry, ...]:
    """Overlay imported terms, replacing cues and applying three-column stars."""
    by_key: dict[str, VocabEntry] = {}
    order: list[str] = []
    for entry in existing or ():
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = entry
    for entry in incoming:
        key = (entry.spoken or "").strip().lower()
        if not key:
            continue
        if key in by_key:
            old = by_key[key]
            by_key[key] = VocabEntry(
                spoken=entry.spoken,
                written=entry.written,
                starred=old.starred or entry.starred,
                added_at=old.added_at or entry.added_at or _now_added_at(),
            )
            continue
        order.append(key)
        by_key[key] = (
            entry
            if entry.added_at
            else VocabEntry(
                spoken=entry.spoken,
                written=entry.written,
                starred=entry.starred,
                added_at=_now_added_at(),
            )
        )
    return tuple(by_key[key] for key in order)


def plan_dictionary_import(
    existing: tuple[VocabEntry, ...] | None,
    incoming: tuple[VocabEntry, ...],
    *,
    snippets: tuple[SnippetEntry, ...] | None = (),
    skipped_empty: int = 0,
    skipped_malformed: int = 0,
    skipped_overflow: int = 0,
    read: int | None = None,
    skip_changes: Sequence[dict[str, Any]] = (),
    entry_indexes: Sequence[int] | None = None,
) -> tuple[tuple[VocabEntry, ...], dict[str, Any]]:
    """Plan dictionary additions, replacements, and skips.

    Existing terms are replaced when the written form changes. Three-column
    CSV imports apply stars on replacement. Snippet cues are skipped.
    """
    existing_by_key: dict[str, VocabEntry] = {}
    for entry in existing or ():
        key = (entry.spoken or "").strip().lower()
        if key and key not in existing_by_key:
            existing_by_key[key] = entry
    existing_keys = set(existing_by_key)
    snippet_keys = {
        (entry.spoken or "").strip().lower()
        for entry in snippets or ()
        if (entry.spoken or "").strip()
    }
    added = replaced = skipped_snippet = skipped_duplicate = skipped_existing = 0
    starred_added = 0
    kept: list[VocabEntry] = []
    seen: set[str] = set()
    changes: list[dict[str, Any]] = []
    for entry in incoming:
        key = (entry.spoken or "").strip().lower()
        if not key:
            skipped_empty += 1
            continue
        if key in seen:
            skipped_duplicate += 1
            changes.append(_snippet_skip_row(entry.spoken, entry.written, "duplicate"))
            continue
        seen.add(key)
        if key in snippet_keys:
            skipped_snippet += 1
            changes.append(_snippet_skip_row(entry.spoken, entry.written, "snippet"))
            continue
        if key in existing_keys:
            current = existing_by_key[key]
            if (entry.written or "").strip() == (current.written or "").strip():
                skipped_existing += 1
                changes.append(_snippet_skip_row(entry.spoken, entry.written, "existing"))
                continue
            replaced += 1
            starred = current.starred or entry.starred
            kept.append(
                VocabEntry(
                    spoken=entry.spoken,
                    written=entry.written,
                    starred=starred,
                    added_at=current.added_at,
                )
            )
            changes.append(
                {
                    "spoken": entry.spoken,
                    "expansion": entry.written,
                    "action": "replace",
                    **({"starred": True} if starred else {}),
                }
            )
            continue
        added += 1
        if entry.starred:
            starred_added += 1
        kept.append(entry)
        changes.append(
            {
                "spoken": entry.spoken,
                "expansion": entry.written,
                "action": "add",
                **({"starred": True} if entry.starred else {}),
            }
        )
    merged = _apply_dictionary_import_entries(existing, tuple(kept))
    skipped = (
        skipped_empty
        + skipped_snippet
        + skipped_duplicate
        + skipped_existing
        + skipped_malformed
        + skipped_overflow
    )
    return merged, {
        "added": added,
        "replaced": replaced,
        "skipped": skipped,
        "skipped_empty": skipped_empty,
        "skipped_snippet": skipped_snippet,
        "skipped_duplicate": skipped_duplicate,
        "skipped_existing": skipped_existing,
        "skipped_malformed": skipped_malformed,
        "skipped_overflow": skipped_overflow,
        "skipped_dictionary": 0,
        "read": (
            len(incoming) + skipped_empty + skipped_malformed + skipped_overflow
            if read is None
            else read
        ),
        "applied": added + replaced,
        "starred_added": starred_added,
        "starred_detail": starred_import_detail(starred_added),
        "changes": _interleave_snippet_skip_rows(
            changes,
            skip_changes=skip_changes,
            entry_indexes=entry_indexes,
        ),
    }


@dataclass(frozen=True)
class DictationConfig:
    """Offline post-ASR transforms (no network, no LLM)."""

    # Deterministic filler removal, capitalization, end punctuation.
    local_polish: bool = True
    # "scratch that", "new line", spoken punctuation, etc.
    spoken_edits: bool = True
    # .py / git / path-oriented spoken forms.
    developer_terms: bool = True
    # Local Auto Cleanup level. none / light / medium. Default medium.
    cleanup_level: str = "medium"


@dataclass(frozen=True)
class HotkeyConfig:
    mode: str = "hold"
    dictation: str = "ctrl+win"
    # Optional power-user chords. "off" keeps the default surface to one key.
    command: str = "off"
    streaming: str = "off"


@dataclass(frozen=True)
class StyleConfig:
    """Local writing style. Deterministic. Never leaves the machine."""

    default: str = "plain"
    per_app: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ListPrefs:
    """Local dictionary/snippet Order and Starred only. Display only. Not a cloud library."""

    dictionary_sort: str = "saved"
    snippet_sort: str = "saved"
    dictionary_starred_only: bool = False
    snippet_starred_only: bool = False


LIST_SORT_MODES = frozenset({"saved", "az", "za", "starred", "newest", "oldest"})


@dataclass(frozen=True)
class OverlayConfig:
    enabled: bool = True
    lazy: bool = True
    position: str = "bottom-center"
    reduced_motion: bool = False


@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    # When False (default), only loopback hosts are accepted so the local
    # service cannot be accidentally exposed on the LAN.
    allow_lan: bool = False


@dataclass(frozen=True)
class AudioConfig:
    # None => system default input device. Otherwise a sounddevice device index
    # (int) or a name substring (str) selecting the capture device.
    input_device: int | str | None = None
    # Ring-buffer capacity (seconds). Must be >= auto_stop_seconds so a full
    # hold never silently wraps the oldest audio.
    max_seconds: float = 90.0
    # Soft stop while PTT is held: synthesize release and finalize. Prevents
    # unbounded holds and pairs with the hotkey stuck-safety net.
    auto_stop_seconds: float = 60.0


@dataclass(frozen=True)
class InjectorConfig:
    default: str = "clipboard"
    restore_clipboard: bool = True
    # Maximum wait after Ctrl+V before clipboard restore. 100 ms is enough for
    # local apps; slow remotes can raise this per-app or globally.
    paste_delay_s: float = 0.10
    paste_min_delay_s: float = 0.04
    # Short utterances skip the clipboard tax and type as keystrokes instead.
    # 0 disables. Per-app overrides still win.
    short_text_keystroke_chars: int = 48
    per_app: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PersonalizationConfig:
    """Local correction memory. Never stores microphone audio."""

    enabled: bool = True
    learn: bool = True
    # Explicit trust signal for longer learned rewrites. False is sovereign,
    # fail-closed default; whole-utterance corrections remain available.
    prose_context: bool = False

    def __post_init__(self) -> None:
        for name in ("enabled", "learn", "prose_context"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"personalization.{name} must be a boolean")


@dataclass(frozen=True)
class TtsConfig:
    # TTS is off by default: model assets are not bundled and are fetched only
    # after explicit consent, so a fresh install advertises no TTS over DVAP.
    enabled: bool = False
    backend: str = "kokoro"  # kokoro | piper (legacy/deferred) | auto (Kokoro)
    voice: str = "af_heart"
    # Half-duplex barge-in: while TTS plays on the speakers, "pause" suspends mic
    # capture and "duck" keeps it open at reduced gain; "off" disables coupling.
    mic_policy: str = "pause"
    duck_gain: float = 0.2
    # Skip fenced/inline code when speaking assistant text (spoken code is noise).
    skip_code: bool = True


@dataclass(frozen=True)
class PrivacyConfig:
    first_run_education_shown: bool = False
    consent_ledger_path: Path | None = None
    egress_log_path: Path | None = None


@dataclass(frozen=True)
class RecoveryConfig:
    """Explicit, bounded retention for text that could not be inserted."""

    enabled: bool = False
    max_items: int = 10
    max_age_hours: int = 24

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("recovery.enabled must be a boolean")
        if type(self.max_items) is not int or not 1 <= self.max_items <= 50:
            raise ValueError("recovery.max_items must be an integer between 1 and 50")
        if type(self.max_age_hours) is not int or not 1 <= self.max_age_hours <= 168:
            raise ValueError("recovery.max_age_hours must be an integer between 1 and 168")


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    asr: ASRSpec
    llm: LLMSpec
    cleanup_enabled: bool = True
    language: str = "en"

    @property
    def locality(self) -> Locality:
        if self.asr.locality is Locality.CLOUD or self.llm.locality is Locality.CLOUD:
            return Locality.CLOUD
        return Locality.LOCAL


@dataclass(frozen=True)
class AppConfig:
    active_profile: str
    profiles: dict[str, ProfileConfig]
    language: str = "en"
    language_mode: str = "english"
    cleanup_enabled: bool = True
    launch_at_startup: bool = False
    # Seconds after last use before releasing local ASR RAM. 0 = keep warm.
    idle_unload_s: float = 600.0
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    injector: InjectorConfig = field(default_factory=InjectorConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    dictation: DictationConfig = field(default_factory=DictationConfig)
    personalization: PersonalizationConfig = field(default_factory=PersonalizationConfig)
    style: StyleConfig = field(default_factory=StyleConfig)
    lists: ListPrefs = field(default_factory=ListPrefs)
    dictionary: tuple[VocabEntry, ...] = ()
    snippets: tuple[SnippetEntry, ...] | None = None
    source_path: Path | None = None

    @property
    def current_profile(self) -> ProfileConfig:
        return self.profiles[self.active_profile]

    @property
    def session_locality(self) -> Locality:
        return self.current_profile.locality


def user_config_dir() -> Path:
    # Delegated to util.paths so the platform location and the
    # DCENT_VOICE_PROFILE_ROOT test/automation override have one definition.
    # Everything derived from this (logs, privacy ledger, recovery journal,
    # personalization store, TTS assets) follows the override automatically.
    from dcent_voice.util.paths import user_config_dir as _user_config_dir

    return _user_config_dir()


def default_config_path() -> Path:
    return user_config_dir() / CONFIG_FILENAME


def ensure_user_config(path: Path | None = None, source: Path | None = None) -> Path:
    """Seed the user config from the shipped example, atomically.

    An existing destination is returned untouched even when it is unreadable or
    corrupt: classifying (and recovering from) a bad config is ``load_config``'s
    job, not this layer's.
    """
    destination = path or default_config_path()
    if destination.exists():
        return destination

    source_path = source or find_config_example()
    if not source_path.is_file():
        raise ConfigError(
            f"Cannot create config at {destination}; the bundled example file is missing at "
            f"{source_path}. Reinstall DCENT_Voice or copy config.example.toml there manually."
        )

    # tmp + os.replace so a crash or a second process can never observe a
    # half-written config.toml, and so a failure names both endpoints.
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(source_path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except OSError as exc:
        with contextlib.suppress(OSError, NameError, UnboundLocalError):
            temp_path.unlink()
        raise ConfigError(
            f"Cannot create config at {destination} from {source_path}: {exc}"
        ) from exc
    return destination


def find_config_example() -> Path:
    """Locate the shipped ``config.example.toml``.

    Order matters: the bundle wins, then the source checkout, and the current
    working directory is the *last* resort so a stray example file in an
    unrelated directory can never seed a user's configuration.
    """
    from dcent_voice.util import paths

    candidates = [
        paths.resource("config.example.toml"),
        # PyInstaller one-dir also ships a human-findable copy beside the exe.
        paths.app_dir() / "config.example.toml",
        Path(__file__).resolve().with_name("config.example.toml"),
        Path.cwd() / "config.example.toml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def bundled_default_config_path() -> Path:
    """Return the shipped example config, never a mutable user config."""

    from dcent_voice.util import paths

    return paths.resource("config.example.toml")


def load_bundled_default_config() -> AppConfig:
    """Load the immutable source/frozen default used to construct a fresh install."""

    path = bundled_default_config_path()
    return load_config(path, create=False)


@dataclass(frozen=True)
class ConfigRecovery:
    """Record of a user config that was invalid and had to be reset."""

    config_path: Path
    broken_path: Path
    reason: str

    def message(self) -> str:
        return (
            "Your settings file was invalid and has been reset to the defaults; "
            f"the previous file was kept at {self.broken_path}."
        )


#: Set by :func:`load_config` when it had to reset a corrupt user config. The
#: desktop runtime reads it once, after logging is configured, to tell the user
#: what happened (a reset that nobody mentions is indistinguishable from
#: "the app lost my settings for no reason").
recovery_notice: ConfigRecovery | None = None


def take_recovery_notice() -> ConfigRecovery | None:
    """Return and clear the pending corrupt-config notice."""
    global recovery_notice
    notice = recovery_notice
    recovery_notice = None
    return notice


def load_config(path: Path | str | None = None, *, create: bool = True) -> AppConfig:
    explicit = path is not None
    config_path = default_config_path() if path is None else Path(path)
    if create and not explicit:
        config_path = ensure_user_config(config_path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")

    try:
        return _read_config_file(config_path, create=create)
    except ConfigUnreadableError:
        # The file may be perfectly valid and merely locked for a moment (an
        # antivirus scan, an editor holding it, a roaming-profile hiccup).
        # Quarantining it here would destroy good settings over a transient I/O
        # error, so this propagates to report_fatal with nothing changed.
        raise
    except ConfigError as exc:
        # Recovery is only ever applied to the profile's own config file. An
        # explicit ``--config`` path belongs to the caller: silently renaming
        # and replacing a file somebody pointed us at would be data loss.
        if explicit or not create:
            raise
        return _recover_broken_config(config_path, exc)


def _read_config_file(config_path: Path, *, create: bool) -> AppConfig:
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigUnreadableError(
            f"Cannot read {config_path}: {exc}. Your settings were left unchanged."
        ) from exc

    if (
        create
        and _should_persist_desktop_asr_migration(config_path, raw)
        and persist_stale_desktop_asr(config_path)
    ):
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))

    return parse_config(raw, source_path=config_path)


def _recover_broken_config(config_path: Path, cause: ConfigError) -> AppConfig:
    """Quarantine an unusable config, reseed from the example, and carry on.

    A single bad key used to be a hard, silent exit for a user who had no way to
    find or edit the file. Resetting is recoverable and the original is kept, so
    the worst case is "my tweaks are in the file next to it".
    """
    global recovery_notice

    broken_path = _quarantine_path(config_path)
    try:
        os.replace(config_path, broken_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            broken_path.unlink()  # release the name we reserved but never used
        raise ConfigError(
            f"{cause} — and the invalid file could not be moved aside ({broken_path}): {exc}"
        ) from exc

    try:
        ensure_user_config(config_path)
        config = _read_config_file(config_path, create=True)
    except ConfigError as exc:
        raise ConfigError(
            f"{cause} — the invalid file was moved to {broken_path}, but a fresh "
            f"configuration could not be created: {exc}"
        ) from exc

    recovery_notice = ConfigRecovery(
        config_path=config_path,
        broken_path=broken_path,
        reason=str(cause),
    )
    logger.warning(
        "invalid configuration reset: %s (previous file kept at %s)",
        cause,
        broken_path,
    )
    _prune_broken_configs(config_path)
    return config


#: How many quarantined copies to keep. Enough to see a repeating pattern,
#: few enough that a crash-looping launch cannot fill the user's profile.
MAX_BROKEN_CONFIG_COPIES = 5


def _quarantine_path(config_path: Path) -> Path:
    """Reserve an unused ``config.toml.broken-<stamp>`` name, atomically.

    ``while candidate.exists()`` was a time-of-check/time-of-use race: two
    launches in the same second (autostart plus a double-click, say) could both
    see the same name free and the second would silently overwrite the first
    one's evidence. ``O_CREAT | O_EXCL`` lets the filesystem arbitrate instead.
    The reserved file is empty; the caller replaces it with ``os.replace``.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    for counter in range(100):
        suffix = f".broken-{stamp}" if counter == 0 else f".broken-{stamp}-{counter}"
        candidate = config_path.with_name(f"{config_path.name}{suffix}")
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        return candidate
    raise ConfigError(f"Could not reserve a quarantine name beside {config_path}")


def _prune_broken_configs(config_path: Path) -> None:
    """Keep only the newest ``MAX_BROKEN_CONFIG_COPIES`` quarantined configs.

    The timestamp is fixed-width, so lexical order is chronological order.
    Never raises: losing an old copy is not worth failing a startup that has
    otherwise just recovered.
    """
    with contextlib.suppress(OSError):
        existing = sorted(config_path.parent.glob(f"{config_path.name}.broken-*"))
        for stale in existing[:-MAX_BROKEN_CONFIG_COPIES]:
            with contextlib.suppress(OSError):
                stale.unlink()


def parse_config(raw: dict[str, Any], *, source_path: Path | None = None) -> AppConfig:
    raw = migrate_raw(raw)
    version = raw.get("config_version", 1)
    if isinstance(version, int) and version > CURRENT_CONFIG_VERSION:
        logger.warning(
            "config_version %s is newer than this build supports (%s); "
            "unrecognized keys will be ignored.",
            version,
            CURRENT_CONFIG_VERSION,
        )
    unknown = set(raw) - _KNOWN_TOP_LEVEL
    if unknown:
        # Catches typos like [hotkey] vs [hotkeys] that used to silently default.
        logger.warning("Ignoring unrecognized config keys: %s", ", ".join(sorted(unknown)))

    active_profile = _string(raw.get("active_profile", "desktop"), "active_profile")
    language = _language(raw.get("language", "en"), "language")
    language_mode = _parse_language_mode(raw.get("language_mode"), language)
    cleanup_enabled = _bool(raw.get("cleanup_enabled", True), "cleanup_enabled")
    launch_at_startup = _bool(raw.get("launch_at_startup", False), "launch_at_startup")
    idle_unload_s = _idle_unload_s(raw.get("idle_unload_s", 600.0))

    profiles = _parse_profiles(
        raw.get("profile"),
        global_cleanup_enabled=cleanup_enabled,
        global_language=language,
    )
    if active_profile not in profiles:
        names = ", ".join(sorted(profiles)) or "<none>"
        raise ConfigError(f"active_profile {active_profile!r} is not defined. Profiles: {names}.")

    return AppConfig(
        active_profile=active_profile,
        profiles=profiles,
        language=language,
        language_mode=language_mode,
        cleanup_enabled=cleanup_enabled,
        launch_at_startup=launch_at_startup,
        idle_unload_s=idle_unload_s,
        hotkeys=_parse_hotkeys(raw.get("hotkeys", {})),
        overlay=_parse_overlay(raw.get("overlay", {})),
        service=_parse_service(raw.get("service", {})),
        audio=_parse_audio(raw.get("audio", {})),
        injector=_parse_injector(raw.get("injector", {})),
        privacy=_parse_privacy(raw.get("privacy", {})),
        recovery=_parse_recovery(raw.get("recovery", {})),
        tts=_parse_tts(raw.get("tts", {})),
        dictation=_parse_dictation(raw.get("dictation", {})),
        personalization=_parse_personalization(raw.get("personalization", {})),
        style=_parse_style(raw.get("style", {})),
        lists=_parse_lists(raw.get("lists", {})),
        dictionary=_parse_dictionary(raw.get("dictionary", {})),
        snippets=_parse_snippets(raw.get("snippets", None)),
        source_path=source_path,
    )


def _parse_profiles(
    table: Any,
    *,
    global_cleanup_enabled: bool,
    global_language: str,
) -> dict[str, ProfileConfig]:
    if not isinstance(table, dict) or not table:
        raise ConfigError("At least one [profile.<name>] section is required.")

    profiles: dict[str, ProfileConfig] = {}
    for name, value in table.items():
        if not isinstance(value, dict):
            raise ConfigError(f"profile.{name} must be a table.")
        asr_raw = _string(value.get("asr"), f"profile.{name}.asr")
        llm_raw = _string(value.get("llm", "none"), f"profile.{name}.llm")
        cleanup_enabled = _bool(
            value.get("cleanup_enabled", global_cleanup_enabled),
            f"profile.{name}.cleanup_enabled",
        )
        language = _language(
            value.get("language", global_language),
            f"profile.{name}.language",
        )
        profiles[name] = ProfileConfig(
            name=name,
            asr=ASRSpec.parse(asr_raw),
            llm=LLMSpec.parse(llm_raw),
            cleanup_enabled=cleanup_enabled,
            language=language,
        )
    return profiles


def _parse_hotkeys(table: Any) -> HotkeyConfig:
    if table is None:
        return HotkeyConfig()
    if not isinstance(table, dict):
        raise ConfigError("[hotkeys] must be a table.")
    return HotkeyConfig(
        mode=_string(table.get("mode", "hold"), "hotkeys.mode"),
        dictation=_hotkey_spec(
            table.get("dictation", "ctrl+win"), "hotkeys.dictation", required=True
        ),
        command=_hotkey_spec(table.get("command", "off"), "hotkeys.command"),
        streaming=_hotkey_spec(table.get("streaming", "off"), "hotkeys.streaming"),
    )


def _hotkey_spec(value: Any, key: str, *, required: bool = False) -> str:
    if value is None:
        value = "off"
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string.")
    cleaned = value.strip()
    if cleaned.lower() in {"", "off", "none", "disabled"}:
        if required:
            raise ConfigError(f"{key} must be a key combination.")
        return "off"
    return cleaned


def _parse_style(table: Any) -> StyleConfig:
    if table is None or table == {}:
        return StyleConfig()
    if not isinstance(table, dict):
        raise ConfigError("[style] must be a table.")
    default = _string(table.get("default", "plain"), "style.default").lower()
    if default in {"off", "none", "default"}:
        default = "plain"
    if default not in {"plain", "email", "chat", "code", "formal", "notes"}:
        raise ConfigError("style.default must be plain, email, chat, code, formal, or notes.")
    per_app_raw = table.get("per_app", {})
    if per_app_raw is None:
        per_app_raw = {}
    if not isinstance(per_app_raw, dict):
        raise ConfigError("[style.per_app] must be a table.")
    per_app: dict[str, str] = {}
    for process, style in per_app_raw.items():
        name = _string(style, f"style.per_app.{process}").lower()
        if name not in {"plain", "email", "chat", "code", "formal", "notes"}:
            raise ConfigError("style.per_app values must be a known style.")
        per_app[_string(process, "style.per_app key")] = name
    return StyleConfig(default=default, per_app=per_app)


def _parse_lists(table: Any) -> ListPrefs:
    if table is None or table == {}:
        return ListPrefs()
    if not isinstance(table, dict):
        raise ConfigError("[lists] must be a table.")
    dictionary_sort = _string(
        table.get("dictionary_sort", "saved"), "lists.dictionary_sort"
    ).lower()
    snippet_sort = _string(table.get("snippet_sort", "saved"), "lists.snippet_sort").lower()
    if dictionary_sort not in LIST_SORT_MODES:
        raise ConfigError(
            "lists.dictionary_sort must be saved, az, za, starred, newest, or oldest."
        )
    if snippet_sort not in LIST_SORT_MODES:
        raise ConfigError("lists.snippet_sort must be saved, az, za, starred, newest, or oldest.")
    return ListPrefs(
        dictionary_sort=dictionary_sort,
        snippet_sort=snippet_sort,
        dictionary_starred_only=_bool(
            table.get("dictionary_starred_only", False), "lists.dictionary_starred_only"
        ),
        snippet_starred_only=_bool(
            table.get("snippet_starred_only", False), "lists.snippet_starred_only"
        ),
    )


def _parse_overlay(table: Any) -> OverlayConfig:
    if table is None:
        return OverlayConfig()
    if not isinstance(table, dict):
        raise ConfigError("[overlay] must be a table.")
    return OverlayConfig(
        enabled=_bool(table.get("enabled", True), "overlay.enabled"),
        lazy=_bool(table.get("lazy", True), "overlay.lazy"),
        position=_string(table.get("position", "bottom-center"), "overlay.position"),
        reduced_motion=_bool(table.get("reduced_motion", False), "overlay.reduced_motion"),
    )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _parse_service(table: Any) -> ServiceConfig:
    if table is None:
        return ServiceConfig()
    if not isinstance(table, dict):
        raise ConfigError("[service] must be a table.")
    port = table.get("port", 8765)
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("service.port must be an integer from 1 to 65535.")
    host = _string(table.get("host", "127.0.0.1"), "service.host")
    allow_lan = _bool(table.get("allow_lan", False), "service.allow_lan")
    if not allow_lan and host.lower() not in _LOOPBACK_HOSTS:
        raise ConfigError(
            "service.host must be a loopback address (127.0.0.1, ::1, localhost) "
            "unless service.allow_lan = true "
            f"(got host={host!r})."
        )
    return ServiceConfig(
        enabled=_bool(table.get("enabled", True), "service.enabled"),
        host=host,
        port=port,
        allow_lan=allow_lan,
    )


def _parse_audio(table: Any) -> AudioConfig:
    if not table:
        return AudioConfig()
    if not isinstance(table, dict):
        raise ConfigError("[audio] must be a table.")
    device = table.get("input_device")
    if device in (None, ""):
        device = None
    elif not isinstance(device, (int, str)):
        raise ConfigError("audio.input_device must be a device index (int) or name (string).")
    try:
        max_seconds = float(table.get("max_seconds", 90.0))
        auto_stop = float(table.get("auto_stop_seconds", 60.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("audio duration limits must be finite numbers.") from exc
    if not math.isfinite(max_seconds) or not math.isfinite(auto_stop):
        raise ConfigError("audio duration limits must be finite numbers.")
    if max_seconds <= 0:
        raise ConfigError("audio.max_seconds must be positive.")
    if auto_stop <= 0:
        raise ConfigError("audio.auto_stop_seconds must be positive.")
    # AudioCapture allocates max_seconds * the device-native sample rate. Keep a
    # bad config from allocating hundreds of MB (or more) before the first PTT.
    if max_seconds > 300:
        raise ConfigError("audio.max_seconds must be at most 300 seconds.")
    if auto_stop > 240:
        raise ConfigError("audio.auto_stop_seconds must be at most 240 seconds.")
    # Keep headroom so queue lag after auto-stop cannot wrap the ring and drop
    # the start of a long hold (auto_stop == max_seconds is unsafe).
    _AUTO_STOP_MARGIN_S = 2.0
    if auto_stop + _AUTO_STOP_MARGIN_S > max_seconds:
        raise ConfigError(
            "audio.auto_stop_seconds must leave at least "
            f"{_AUTO_STOP_MARGIN_S:g}s headroom under audio.max_seconds "
            f"(got auto_stop={auto_stop}, max={max_seconds})."
        )
    return AudioConfig(
        input_device=device,
        max_seconds=max_seconds,
        auto_stop_seconds=auto_stop,
    )


def _parse_injector(table: Any) -> InjectorConfig:
    if table is None:
        return InjectorConfig()
    if not isinstance(table, dict):
        raise ConfigError("[injector] must be a table.")
    default = _string(table.get("default", "clipboard"), "injector.default").lower()
    if default not in {"clipboard", "keystroke"}:
        raise ConfigError("injector.default must be 'clipboard' or 'keystroke'.")
    restore_clipboard = _bool(table.get("restore_clipboard", True), "injector.restore_clipboard")
    try:
        paste_delay_s = float(table.get("paste_delay_s", 0.10))
        paste_min_delay_s = float(table.get("paste_min_delay_s", 0.04))
        short_text_keystroke_chars = int(table.get("short_text_keystroke_chars", 48))
    except (TypeError, ValueError) as exc:
        raise ConfigError("injector timing fields must be numbers.") from exc
    if not math.isfinite(paste_delay_s) or paste_delay_s < 0:
        raise ConfigError("injector.paste_delay_s must be a finite non-negative number.")
    if not math.isfinite(paste_min_delay_s) or paste_min_delay_s < 0:
        raise ConfigError("injector.paste_min_delay_s must be a finite non-negative number.")
    if paste_min_delay_s > paste_delay_s:
        raise ConfigError("injector.paste_min_delay_s cannot exceed paste_delay_s.")
    if short_text_keystroke_chars < 0:
        raise ConfigError("injector.short_text_keystroke_chars must be >= 0.")
    per_app_raw = table.get("per_app", {})
    if not isinstance(per_app_raw, dict):
        raise ConfigError("[injector.per_app] must be a table.")
    per_app: dict[str, str] = {}
    for process, injector_name in per_app_raw.items():
        value = _string(injector_name, f"injector.per_app.{process}").lower()
        if value not in {"clipboard", "keystroke"}:
            raise ConfigError("injector.per_app values must be 'clipboard' or 'keystroke'.")
        per_app[_string(process, "injector.per_app key")] = value
    return InjectorConfig(
        default=default,
        restore_clipboard=restore_clipboard,
        paste_delay_s=paste_delay_s,
        paste_min_delay_s=paste_min_delay_s,
        short_text_keystroke_chars=short_text_keystroke_chars,
        per_app=per_app,
    )


def _parse_privacy(table: Any) -> PrivacyConfig:
    if table is None:
        return PrivacyConfig()
    if not isinstance(table, dict):
        raise ConfigError("[privacy] must be a table.")
    return PrivacyConfig(
        first_run_education_shown=_bool(
            table.get("first_run_education_shown", False),
            "privacy.first_run_education_shown",
        ),
        consent_ledger_path=_optional_path(table.get("consent_ledger_path", "")),
        egress_log_path=_optional_path(table.get("egress_log_path", "")),
    )


def _parse_recovery(table: Any) -> RecoveryConfig:
    if table is None or table == {}:
        return RecoveryConfig()
    if not isinstance(table, dict):
        raise ConfigError("[recovery] must be a table.")
    max_items = table.get("max_items", 10)
    max_age_hours = table.get("max_age_hours", 24)
    if type(max_items) is not int or not 1 <= max_items <= 50:
        raise ConfigError("recovery.max_items must be an integer between 1 and 50.")
    if type(max_age_hours) is not int or not 1 <= max_age_hours <= 168:
        raise ConfigError("recovery.max_age_hours must be an integer between 1 and 168.")
    return RecoveryConfig(
        enabled=_bool(table.get("enabled", False), "recovery.enabled"),
        max_items=max_items,
        max_age_hours=max_age_hours,
    )


def _parse_tts(table: Any) -> TtsConfig:
    if not table:
        return TtsConfig()
    if not isinstance(table, dict):
        raise ConfigError("[tts] must be a table.")
    backend = _string(table.get("backend", "kokoro"), "tts.backend").lower()
    if backend not in TTS_BACKENDS:
        raise ConfigError(f"tts.backend must be one of {sorted(TTS_BACKENDS)}.")
    mic_policy = _string(table.get("mic_policy", "pause"), "tts.mic_policy").lower()
    if mic_policy not in TTS_MIC_POLICIES:
        raise ConfigError(f"tts.mic_policy must be one of {sorted(TTS_MIC_POLICIES)}.")
    duck_gain = table.get("duck_gain", 0.2)
    if not isinstance(duck_gain, (int, float)) or isinstance(duck_gain, bool):
        raise ConfigError("tts.duck_gain must be a number between 0 and 1.")
    if not 0.0 <= float(duck_gain) <= 1.0:
        raise ConfigError("tts.duck_gain must be between 0 and 1.")
    return TtsConfig(
        enabled=_bool(table.get("enabled", False), "tts.enabled"),
        backend=backend,
        voice=_string(table.get("voice", "af_heart"), "tts.voice"),
        mic_policy=mic_policy,
        duck_gain=float(duck_gain),
        skip_code=_bool(table.get("skip_code", True), "tts.skip_code"),
    )


def _parse_dictionary(table: Any) -> tuple[VocabEntry, ...]:
    if table is None:
        return ()
    if not isinstance(table, dict):
        raise ConfigError("[dictionary] must be a table.")
    terms = table.get("terms", [])
    if terms is None:
        return ()
    if not isinstance(terms, list):
        raise ConfigError("dictionary.terms must be an array of tables.")

    parsed: list[VocabEntry] = []
    for index, item in enumerate(terms):
        if not isinstance(item, dict):
            raise ConfigError(f"dictionary.terms[{index}] must be a table.")
        spoken = _string(item.get("spoken"), f"dictionary.terms[{index}].spoken")
        written = _string(item.get("written"), f"dictionary.terms[{index}].written")
        starred = _bool(item.get("starred", False), f"dictionary.terms[{index}].starred")
        added_at = _parse_added_at(item.get("added_at"))
        parsed.append(
            VocabEntry(spoken=spoken, written=written, starred=starred, added_at=added_at)
        )
    return tuple(parsed)


def _parse_snippets(table: Any) -> tuple[SnippetEntry, ...] | None:
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[snippets] must be a table.")
    items = table.get("items", None)
    if items is None:
        return None
    if not isinstance(items, list):
        raise ConfigError("snippets.items must be an array of tables.")
    if not items:
        return ()

    parsed: list[SnippetEntry] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"snippets.items[{index}] must be a table.")
        spoken = _string(item.get("spoken"), f"snippets.items[{index}].spoken")
        # Expansion may be multi-line; empty is allowed (cue is inert until filled).
        expansion = item.get("expansion", "")
        if not isinstance(expansion, str):
            raise ConfigError(f"snippets.items[{index}].expansion must be a string.")
        starred = _bool(item.get("starred", False), f"snippets.items[{index}].starred")
        added_at = _parse_added_at(item.get("added_at"))
        parsed.append(
            SnippetEntry(spoken=spoken, expansion=expansion, starred=starred, added_at=added_at)
        )
    return tuple(parsed)


def _parse_personalization(table: Any) -> PersonalizationConfig:
    if table is None or table == {}:
        return PersonalizationConfig()
    if not isinstance(table, dict):
        raise ConfigError("[personalization] must be a table.")
    return PersonalizationConfig(
        enabled=_bool(table.get("enabled", True), "personalization.enabled"),
        learn=_bool(table.get("learn", True), "personalization.learn"),
        prose_context=_bool(table.get("prose_context", False), "personalization.prose_context"),
    )


def _parse_language_mode(value: Any, language: str) -> str:
    aliases = {
        "en": "english",
        "eng": "english",
        "english-only": "english",
        "en-fast": "english",
        "multi": "multilingual",
        "many": "multilingual",
        "intl": "multilingual",
        "international": "multilingual",
        "detect": "auto",
        "auto-detect": "auto",
        "autodetect": "auto",
    }
    allowed = {"english", "multilingual", "auto"}
    if value is None or value == "":
        lang = (language or "en").strip().lower()
        if lang in {"", "auto", "detect"}:
            return "auto"
        if lang in {"en", "eng", "en-us", "en-gb"}:
            return "english"
        return "multilingual"
    if not isinstance(value, str):
        raise ConfigError("language_mode must be a string.")
    mode = value.strip().lower()
    mode = aliases.get(mode, mode)
    if mode not in allowed:
        raise ConfigError("language_mode must be english, multilingual, or auto.")
    return mode


def persist_stale_desktop_asr(path: Path) -> bool:
    """Rewrite a known-stale shipped desktop ASR line. Preserves the rest of the file."""
    import re

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    target = desktop_asr_migration_target()
    if target == _SHIPPED_DESKTOP_ASR:
        stale = (
            r"(?:faster-whisper:distil-small\.en:(?:cpu-)?int8|"
            r"faster-whisper:base\.en:cpu-int8)"
        )
    else:
        stale = r"faster-whisper:distil-small\.en:(?:cpu-)?int8"
    pattern = re.compile(
        rf'(?ms)^(\[profile\.desktop\][^\[]*?^asr\s*=\s*)"{stale}"',
    )
    updated, count = pattern.subn(rf'\1"{target}"', text, count=1)
    if not count or updated == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(path)
    return True


def desktop_asr_migration_target() -> str:
    """Parakeet when weights are already local. Otherwise Whisper base.en."""
    return _SHIPPED_DESKTOP_ASR if _parakeet_weights_present() else _WHISPER_FALLBACK_ASR


def _parakeet_weights_present() -> bool:
    try:
        from dcent_voice.asr.parakeet_provider import resolve_parakeet_model_dir
    except Exception:
        return False
    return resolve_parakeet_model_dir() is not None


def _should_persist_desktop_asr_migration(path: Path, raw: dict[str, Any]) -> bool:
    if path.name == "config.example.toml":
        return False
    profiles = raw.get("profile")
    if not isinstance(profiles, dict):
        return False
    desktop = profiles.get("desktop")
    if not isinstance(desktop, dict):
        return False
    asr = str(desktop.get("asr", "")).strip()
    if _parakeet_weights_present():
        return asr in _STALE_WHEN_PARAKEET
    return asr in _STALE_DISTIL_ASR


def _parse_dictation(table: Any) -> DictationConfig:
    if table is None:
        return DictationConfig()
    if not isinstance(table, dict):
        raise ConfigError("[dictation] must be a table.")
    return DictationConfig(
        local_polish=_bool(table.get("local_polish", True), "dictation.local_polish"),
        spoken_edits=_bool(table.get("spoken_edits", True), "dictation.spoken_edits"),
        developer_terms=_bool(table.get("developer_terms", True), "dictation.developer_terms"),
        cleanup_level=_parse_cleanup_level(table.get("cleanup_level", "medium")),
    )


def _parse_cleanup_level(value: Any) -> str:
    if value is None:
        return "medium"
    if not isinstance(value, str):
        raise ConfigError("dictation.cleanup_level must be a string.")
    name = value.strip().lower()
    aliases = {
        "off": "none",
        "false": "none",
        "on": "medium",
        "full": "high",
        "default": "medium",
    }
    name = aliases.get(name, name)
    if name not in {"none", "light", "medium", "high"}:
        raise ConfigError("dictation.cleanup_level must be none, light, medium, or high.")
    return name


def _clean_spec(raw: str, label: str) -> str:
    value = _string(raw, f"{label} spec").strip()
    if not value:
        raise ConfigError(f"{label} spec cannot be empty.")
    return value


def _string(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string.")
    return value.strip()


def _language(value: Any, key: str) -> str:
    language = _string(value, key)
    try:
        normalized = normalize_language_hint(language)
    except UnsupportedLanguageError as exc:
        raise ConfigError(f"{key}: {exc}") from exc
    return "auto" if normalized == "" else normalized or "en"


def _bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false.")
    return value


def _idle_unload_s(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("idle_unload_s must be a number of seconds (0 keeps the model warm).")
    if float(value) < 0:
        raise ConfigError("idle_unload_s must be >= 0.")
    return float(value)


def _optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError("Optional path values must be strings.")
    return Path(value).expanduser()
