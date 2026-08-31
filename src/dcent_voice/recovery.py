# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Bounded, opt-in local recovery for dictation that could not be inserted."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dcent_voice.attach.registry import verify_private_file, write_text_atomic
from dcent_voice.config import AppConfig, RecoveryConfig, user_config_dir

logger = logging.getLogger("DCENT_Voice").getChild("recovery")

SCHEMA_VERSION = 1
MAX_TEXT_CHARS = 20_000
MAX_FILE_BYTES = 1_000_000
STORE_FILENAME = "failed_dictations.json"

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


@dataclass(frozen=True)
class RecoveryEntry:
    id: str
    created_at: str
    text: str
    reason: str
    mode: str


def default_recovery_path(config: AppConfig | None = None) -> Path:
    """Keep custom/test configs self-contained; use the native config dir normally."""

    source = config.source_path if config is not None else None
    base = source.parent if source is not None else user_config_dir()
    return base / "recovery" / STORE_FILENAME


class RecoveryStore:
    """Owner-only JSON vault containing only failed, usable transcript text.

    The store is inert unless ``enabled`` is exactly ``True``. Successful
    utterances and microphone audio never enter it. Every read verifies that
    the public file is a regular, owner-only file; every write publishes a
    completed private temporary file atomically.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        enabled: bool = False,
        max_items: int = 10,
        max_age_hours: int = 24,
    ) -> None:
        self.path = path or default_recovery_path()
        self._lock = _path_lock(self.path)
        self._enabled = enabled is True
        self._max_items = max_items
        self._max_age_hours = max_age_hours
        self._last_error = ""

    @classmethod
    def from_config(cls, config: AppConfig) -> RecoveryStore:
        policy = config.recovery
        return cls(
            path=default_recovery_path(config),
            enabled=policy.enabled,
            max_items=policy.max_items,
            max_age_hours=policy.max_age_hours,
        )

    def update_policy(self, policy: RecoveryConfig) -> bool:
        """Apply live policy and report whether its retention action succeeded."""

        try:
            with self._lock:
                self._enabled = policy.enabled is True
                self._max_items = policy.max_items
                self._max_age_hours = policy.max_age_hours
                if not self._enabled:
                    self._clear_locked()
                else:
                    entries = self._load_locked()
                    pruned = self._prune(entries)
                    if pruned != entries:
                        self._write_locked(pruned)
            self._last_error = ""
            return True
        except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
            self._last_error = _purge_error_label(exc) if not self._enabled else _error_label(exc)
            logger.warning("failed-dictation recovery policy refused: %s", type(exc).__name__)
            return False

    def record(self, text: str, *, reason: str, mode: str) -> bool:
        """Retain one failed result, returning false when disabled or unavailable."""

        normalized = str(text or "").strip()
        if not normalized:
            return False
        normalized = normalized[:MAX_TEXT_CHARS]
        entry = RecoveryEntry(
            id=uuid.uuid4().hex,
            created_at=datetime.now(UTC).isoformat(),
            text=normalized,
            reason=_bounded_label(reason, fallback="insertion_failed"),
            mode=_bounded_label(mode, fallback="dictation"),
        )
        try:
            with self._lock:
                # Policy and content share one serialization boundary. A record
                # already queued behind update_policy(False) must observe the
                # disabled state after the purge, never recreate the vault.
                if not self._enabled:
                    return False
                entries = self._load_locked()
                entries.append(entry)
                self._write_locked(self._prune(entries))
            self._last_error = ""
            return True
        except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
            self._last_error = _error_label(exc)
            logger.warning("failed-dictation recovery write refused: %s", type(exc).__name__)
            return False

    def snapshot(self) -> dict[str, Any]:
        """Return UI-safe state. Transcript text is exposed only while opted in."""

        try:
            with self._lock:
                if not self._enabled:
                    return {
                        "enabled": False,
                        "stores_audio": False,
                        "stores_successes": False,
                        "entry_count": 0,
                        "entries": [],
                        "retention": self._retention(),
                        "path": str(self.path),
                        "integrity_ok": not bool(self._last_error),
                        "detail": self._last_error,
                    }
                loaded = self._load_locked()
                entries = self._prune(loaded)
                if entries != loaded:
                    self._write_locked(entries)
            self._last_error = ""
        except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
            self._last_error = _error_label(exc)
            entries = []
        return {
            "enabled": True,
            "stores_audio": False,
            "stores_successes": False,
            "entry_count": len(entries),
            "entries": [asdict(entry) for entry in reversed(entries)],
            "retention": self._retention(),
            "path": str(self.path),
            "integrity_ok": not bool(self._last_error),
            "detail": self._last_error,
        }

    def get(self, entry_id: str) -> RecoveryEntry | None:
        with self._lock:
            if not self._enabled:
                return None
            for entry in self._load_locked():
                if entry.id == entry_id:
                    return entry
        return None

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            entries = self._load_locked() if self.path.exists() else []
            kept = [entry for entry in entries if entry.id != entry_id]
            if len(kept) == len(entries):
                return False
            self._write_locked(kept)
            return True

    def clear(self) -> bool:
        """Remove every retained entry, returning false if bytes may remain."""

        try:
            with self._lock:
                self._clear_locked()
            self._last_error = ""
            return True
        except (OSError, PermissionError) as exc:
            self._last_error = _purge_error_label(exc)
            logger.warning("failed-dictation recovery purge refused: %s", type(exc).__name__)
            return False

    def _retention(self) -> dict[str, int]:
        return {"max_items": self._max_items, "max_age_hours": self._max_age_hours}

    def _load_locked(self) -> list[RecoveryEntry]:
        if not self.path.exists():
            return []
        info = os.lstat(self.path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("recovery path must be a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError("recovery file exceeds its bounded size")
        verify_private_file(self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported recovery schema")
        values = raw.get("entries")
        if not isinstance(values, list) or len(values) > 50:
            raise ValueError("invalid recovery entries")
        entries: list[RecoveryEntry] = []
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("invalid recovery entry")
            entry = RecoveryEntry(
                id=_validated_id(item.get("id")),
                created_at=_validated_timestamp(item.get("created_at")),
                text=_validated_text(item.get("text")),
                reason=_bounded_label(item.get("reason"), fallback="insertion_failed"),
                mode=_bounded_label(item.get("mode"), fallback="dictation"),
            )
            entries.append(entry)
        return entries

    def _write_locked(self, entries: list[RecoveryEntry]) -> None:
        if not entries:
            self._clear_locked()
            return
        payload = json.dumps(
            {"schema_version": SCHEMA_VERSION, "entries": [asdict(entry) for entry in entries]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("recovery payload exceeds its bounded size")
        if self.path.is_symlink():
            raise ValueError("recovery path symlinks are forbidden")
        write_text_atomic(self.path, payload)

    def _clear_locked(self) -> None:
        self.path.unlink(missing_ok=True)

    def _prune(self, entries: list[RecoveryEntry]) -> list[RecoveryEntry]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=self._max_age_hours)
        current = [
            entry
            for entry in entries
            if cutoff <= datetime.fromisoformat(entry.created_at).astimezone(UTC) <= now
        ]
        return current[-self._max_items :]


def _path_lock(path: Path) -> threading.RLock:
    key = path.resolve(strict=False)
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _validated_id(value: Any) -> str:
    label = str(value or "")
    if len(label) != 32 or any(char not in "0123456789abcdef" for char in label):
        raise ValueError("invalid recovery id")
    return label


def _validated_timestamp(value: Any) -> str:
    label = str(value or "")
    parsed = datetime.fromisoformat(label)
    if parsed.tzinfo is None:
        raise ValueError("recovery timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _validated_text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_CHARS:
        raise ValueError("invalid recovery text")
    return value


def _bounded_label(value: Any, *, fallback: str) -> str:
    label = str(value or fallback).strip()
    return (label or fallback)[:80]


def _error_label(exc: BaseException) -> str:
    return f"Vault unavailable ({type(exc).__name__}); no new text was retained."


def _purge_error_label(exc: BaseException) -> str:
    return (
        "Recovery is off, but retained text could not be purged "
        f"({type(exc).__name__}). Close programs using the vault and retry Clear."
    )
