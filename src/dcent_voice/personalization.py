# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Local, inspectable, resettable personalization. Never stores audio."""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dcent_voice.config import APP_NAME, VocabEntry, user_config_dir
from dcent_voice.dictation.style import STYLE_NAMES

logger = logging.getLogger(APP_NAME).getChild("personalization")

STORE_FILENAME = "personalization.json"
STORE_VERSION = 3
MAX_TERMS = 400
MAX_APP_STYLES = 64
MAX_PHRASE_CHARS = 256
MAX_SCOPE_CHARS = 128
MAX_SOURCE_CHARS = 64
MAX_CONFIRMATIONS = 1_000_000
MIN_GENERALIZATION_COUNT = 2
# Worst-case legacy JSON escaping for 400 maximum-sized Unicode terms is under
# 5 MiB. Six MiB leaves framing/indentation headroom without permitting an
# unbounded read before schema validation.
MAX_STORE_BYTES = 6 * 1024 * 1024
MAX_JSON_DEPTH = 16
MIN_TIMESTAMP_YEAR = 1970
MAX_TIMESTAMP_YEAR = 2199
STORE_LOCK_TIMEOUT_S = 1.0
STORE_LOCK_RETRY_S = 0.025
_POLICY_UNSET = object()
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


@dataclass(frozen=True)
class LearnedTerm:
    spoken: str
    written: str
    count: int
    source: str
    updated_at: str
    style: str = ""
    app: str = ""


@dataclass(frozen=True)
class LearnedAppStyle:
    """Destination writing style remembered from explicit corrections."""

    app: str
    style: str
    count: int
    source: str
    updated_at: str


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    written: str
    source_length: int
    exact: bool
    count: int


class PersistenceStateReconciledError(OSError):
    """A failed rollback was reconciled to the actual visible store state."""


class PersonalizationStore:
    """Correction memory kept next to the user config. Audio is never written."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        enabled: bool | object = _POLICY_UNSET,
        learn: bool | object = _POLICY_UNSET,
    ) -> None:
        """Load local corrections with explicit policy taking precedence.

        Omitted policy restores valid saved flags. Explicit boolean arguments
        remain authoritative across reloads. Legacy stores without policy use
        the backward-compatible enabled/learning defaults.
        """
        enabled_override = _policy_override(enabled, "enabled")
        learn_override = _policy_override(learn, "learn")
        self.path = path or default_personalization_path()
        self._lock = threading.RLock()
        self._enabled_override = enabled_override
        self._learn_override = learn_override
        self._enabled = True if enabled_override is None else enabled_override
        self._learn = True if learn_override is None else learn_override
        self._integrity_ok = True
        self._terms: list[LearnedTerm] = []
        self._app_styles: list[LearnedAppStyle] = []
        # Last utterance is process memory only — never written to disk.
        self._last_raw = ""
        self._last_cleaned = ""
        self._last_style = ""
        self._last_app = ""
        self._write_transaction_depth = 0
        self._policy_dirty = enabled_override is not None or learn_override is not None
        self._baseline_disk_policy = (self._enabled, self._learn)
        self._durable_persisted_state: tuple[Any, ...] | None = None
        self.load()
        self._durable_persisted_state = self._persisted_state_for_rollback()
        self._policy_dirty = enabled_override is not None or learn_override is not None
        self._cleanup_stale_artifacts_on_startup()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        enabled_n = _strict_policy_bool(value, "enabled")
        with self._lock:
            self._enabled = enabled_n
            self._enabled_override = enabled_n
            self._policy_dirty = True

    @property
    def learn(self) -> bool:
        return self._learn

    @learn.setter
    def learn(self, value: bool) -> None:
        learn_n = _strict_policy_bool(value, "learn")
        with self._lock:
            self._learn = learn_n
            self._learn_override = learn_n
            self._policy_dirty = True

    def load(self) -> None:
        with self._lock:
            self._terms = []
            self._app_styles = []
            try:
                persisted_enabled, persisted_learn, loaded, legacy, loaded_styles = (
                    _load_validated_store(self.path)
                )
            except FileNotFoundError:
                self._durable_persisted_state = self._persisted_state_for_rollback()
                return
            except (
                OSError,
                UnicodeDecodeError,
                ValueError,
                TypeError,
                RecursionError,
                MemoryError,
            ) as exc:
                self._fail_closed_policy()
                logger.warning(
                    "personalization store unreadable (%s); starting empty",
                    type(exc).__name__,
                )
                return
            # v3 stores terms oldest→newest. Older stores were sorted for
            # display, so recover the best available chronology from their
            # confirmation timestamps before applying the bound.
            self._integrity_ok = True
            self._enabled = (
                persisted_enabled if self._enabled_override is None else self._enabled_override
            )
            self._learn = persisted_learn if self._learn_override is None else self._learn_override
            if legacy:
                loaded.sort(key=lambda term: term.updated_at)
            self._terms = loaded[-MAX_TERMS:]
            self._app_styles = loaded_styles[-MAX_APP_STYLES:]
            self._baseline_disk_policy = (persisted_enabled, persisted_learn)
            self._durable_persisted_state = self._persisted_state_for_rollback()
            self._policy_dirty = False

    def update_policy(self, *, enabled: bool, learn: bool) -> None:
        """Apply live config flags atomically without reloading the store."""
        enabled_n = _strict_policy_bool(enabled, "enabled")
        learn_n = _strict_policy_bool(learn, "learn")
        with self._lock:
            self._enabled = enabled_n
            self._learn = learn_n
            self._enabled_override = enabled_n
            self._learn_override = learn_n
            self._policy_dirty = True

    def _fail_closed_policy(self) -> None:
        self._integrity_ok = False
        self._enabled = False
        self._learn = False

    def save(self) -> None:
        if self._write_transaction_depth:
            with self._lock:
                self._save_locked()
            return
        with _coordinated_store_write(self.path), self._lock:
            # Validate caller-owned policy before starting a rollback boundary;
            # malformed internal state stays visibly fail-closed for diagnosis.
            _strict_policy_bool(self._enabled, "enabled")
            _strict_policy_bool(self._learn, "learn")
            rollback = self._durable_persisted_state
            local_terms = list(self._terms)
            local_styles = list(self._app_styles)
            local_policy = (
                self._enabled,
                self._learn,
                self._enabled_override,
                self._learn_override,
            )
            baseline_terms = [] if rollback is None else list(rollback[0])
            baseline_styles = [] if rollback is None else list(rollback[1])
            try:
                try:
                    remote_enabled, remote_learn, remote_terms, _legacy, remote_styles = (
                        _load_validated_store(self.path)
                    )
                except FileNotFoundError:
                    remote_enabled, remote_learn = local_policy[:2]
                    remote_terms = []
                    remote_styles = []
                self._terms = _merge_terms(
                    remote_terms,
                    local_terms,
                    baseline_terms,
                )
                self._app_styles = _merge_app_styles(
                    remote_styles,
                    local_styles,
                    baseline_styles,
                )
                base_policy = self._baseline_disk_policy
                remote_policy = (remote_enabled, remote_learn)
                local_values = local_policy[:2]
                chosen_overrides: tuple[bool | None, bool | None]
                if not self._policy_dirty:
                    chosen_policy = remote_policy
                    chosen_overrides = (None, None)
                elif remote_policy in {base_policy, local_values}:
                    chosen_policy = local_values
                    chosen_overrides = local_policy[2:]
                else:
                    raise ValueError("personalization save contains a concurrent policy conflict")
                self._enabled, self._learn = chosen_policy
                self._enabled_override, self._learn_override = chosen_overrides
                self._save_locked()
            except PersistenceStateReconciledError:
                raise
            except Exception:
                if rollback is not None:
                    self._restore_persisted_state(rollback)
                raise

    def _save_locked(self) -> None:
        """Validate and atomically replace state while caller holds write locks."""
        enabled = _strict_policy_bool(self._enabled, "enabled")
        learn = _strict_policy_bool(self._learn, "learn")
        payload = {
            "version": STORE_VERSION,
            "enabled": enabled,
            "learn": learn,
            "terms": [asdict(term) for term in self._terms],
            "app_styles": [asdict(item) for item in self._app_styles],
        }
        # Validate the complete object before creating a temp file, including
        # against accidental internal-state corruption.
        _validate_store_payload(payload)
        # Cleanup is part of the transaction precondition. Performing it before
        # the durable replace avoids a post-commit cleanup error creating a
        # false failure and RAM/disk divergence.
        _cleanup_store_artifacts_locked(self.path)
        try:
            previous_bytes = _read_bounded_store(self.path)
        except FileNotFoundError:
            previous_bytes = None
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(self.path)
            replaced = True
            try:
                _sync_parent_directory(self.path.parent)
            except Exception as sync_exc:
                try:
                    _restore_after_failed_directory_sync(self.path, previous_bytes)
                except Exception as rollback_exc:
                    self._reconcile_visible_store_locked()
                    raise PersistenceStateReconciledError(
                        "personalization durability rollback failed; "
                        "state reconciled from visible disk"
                    ) from rollback_exc
                raise sync_exc
            self._integrity_ok = True
            self._policy_dirty = False
            self._baseline_disk_policy = (enabled, learn)
            self._durable_persisted_state = self._persisted_state_for_rollback()
        except Exception:
            if not replaced:
                Path(tmp_name).unlink(missing_ok=True)
            raise

    def _state_for_rollback(self) -> tuple[Any, ...]:
        return (
            list(self._terms),
            list(self._app_styles),
            self._enabled,
            self._learn,
            self._enabled_override,
            self._learn_override,
            self._integrity_ok,
            self._last_raw,
            self._last_cleaned,
            self._last_style,
            self._last_app,
            self._policy_dirty,
            self._baseline_disk_policy,
            self._durable_persisted_state,
        )

    def _restore_rollback_state(self, state: tuple[Any, ...]) -> None:
        (
            self._terms,
            self._app_styles,
            self._enabled,
            self._learn,
            self._enabled_override,
            self._learn_override,
            self._integrity_ok,
            self._last_raw,
            self._last_cleaned,
            self._last_style,
            self._last_app,
            self._policy_dirty,
            self._baseline_disk_policy,
            self._durable_persisted_state,
        ) = state

    def _persisted_state_for_rollback(self) -> tuple[Any, ...]:
        return (
            list(self._terms),
            list(self._app_styles),
            self._enabled,
            self._learn,
            self._enabled_override,
            self._learn_override,
            self._integrity_ok,
            self._baseline_disk_policy,
        )

    def _restore_persisted_state(self, state: tuple[Any, ...]) -> None:
        (
            terms,
            app_styles,
            self._enabled,
            self._learn,
            self._enabled_override,
            self._learn_override,
            self._integrity_ok,
            self._baseline_disk_policy,
        ) = state
        self._terms = list(terms)
        self._app_styles = list(app_styles)
        self._durable_persisted_state = (
            list(terms),
            list(app_styles),
            self._enabled,
            self._learn,
            self._enabled_override,
            self._learn_override,
            self._integrity_ok,
            self._baseline_disk_policy,
        )
        self._policy_dirty = False

    def _reconcile_visible_store_locked(self) -> None:
        try:
            enabled, learn, terms, legacy, app_styles = _load_validated_store(self.path)
            if legacy:
                terms.sort(key=lambda term: term.updated_at)
            self._terms = terms[-MAX_TERMS:]
            self._app_styles = app_styles[-MAX_APP_STYLES:]
            self._enabled = enabled
            self._learn = learn
            self._enabled_override = None
            self._learn_override = None
            self._integrity_ok = True
            self._baseline_disk_policy = (enabled, learn)
        except Exception:
            self._terms = []
            self._app_styles = []
            self._enabled_override = None
            self._learn_override = None
            self._fail_closed_policy()
            self._baseline_disk_policy = (False, False)
        self._policy_dirty = False
        self._durable_persisted_state = self._persisted_state_for_rollback()

    def _cleanup_stale_artifacts_on_startup(self) -> None:
        if not self.path.parent.is_dir() or not _store_artifacts(self.path):
            return
        try:
            with _coordinated_store_write(self.path):
                _cleanup_store_artifacts_locked(self.path)
        except (OSError, ValueError) as exc:
            logger.warning(
                "personalization crash artifacts could not be cleaned (%s)",
                type(exc).__name__,
            )

    def as_vocab(
        self,
        *,
        style: str | None = None,
        app: str | None = None,
        policy_enabled: bool | None = None,
    ) -> tuple[VocabEntry, ...]:
        """Return only unambiguous terms applicable to a destination.

        A call without context intentionally returns global terms only. This
        prevents an app-specific correction from leaking into a headless client
        that did not declare its destination.
        """
        with self._lock:
            if not self._integrity_ok or not _effective_policy(
                policy_enabled, self._enabled, "policy_enabled"
            ):
                return ()
            return tuple(
                VocabEntry(spoken=term.spoken, written=term.written)
                for term in _select_terms(self._terms, style=style, app=app)
            )

    def learned_app_styles(self, *, policy_enabled: bool | None = None) -> dict[str, str]:
        """Return destination styles confirmed enough to apply automatically."""
        _validate_policy_override(policy_enabled, "policy_enabled")
        with self._lock:
            if not self._integrity_ok or not _effective_policy(
                policy_enabled, self._enabled, "policy_enabled"
            ):
                return {}
            return {
                item.app: item.style
                for item in self._app_styles
                if item.count >= MIN_GENERALIZATION_COUNT
            }

    def remember_app_style(
        self,
        app: str,
        style: str,
        *,
        source: str = "typed",
        immediate: bool = False,
        policy_enabled: bool | None = None,
        policy_learn: bool | None = None,
    ) -> LearnedAppStyle | None:
        """Remember a destination style. Audio is never stored."""
        _validate_policy_override(policy_enabled, "policy_enabled")
        _validate_policy_override(policy_learn, "policy_learn")
        app_n = _normalize_app(app)
        style_n = _normalize_scope(style)
        if not app_n or style_n not in STYLE_NAMES:
            return None
        source_n = _normalize_phrase(source)[:MAX_SOURCE_CHARS] or "typed"
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with _coordinated_store_write(self.path), self._lock:
            if (
                not self._integrity_ok
                or not _effective_policy(policy_enabled, self._enabled, "policy_enabled")
                or not _effective_policy(policy_learn, self._learn, "policy_learn")
            ):
                return None
            rollback = self._state_for_rollback()
            self._write_transaction_depth += 1
            try:
                try:
                    os.lstat(self.path)
                except FileNotFoundError:
                    pass
                else:
                    self.load()
                if (
                    not self._integrity_ok
                    or not _effective_policy(policy_enabled, self._enabled, "policy_enabled")
                    or not _effective_policy(policy_learn, self._learn, "policy_learn")
                ):
                    return None
                updated = self._remember_app_style_locked(
                    app_n,
                    style_n,
                    source=source_n,
                    now=now,
                    immediate=immediate,
                )
                self.save()
                return updated
            except PersistenceStateReconciledError:
                raise
            except Exception:
                self._restore_rollback_state(rollback)
                raise
            finally:
                self._write_transaction_depth -= 1

    def reset_app_styles(self) -> None:
        """Clear learned destination styles only. Vocabulary terms stay."""
        with _coordinated_store_write(self.path), self._lock:
            _strict_policy_bool(self._enabled, "enabled")
            _strict_policy_bool(self._learn, "learn")
            rollback = self._state_for_rollback()
            self._write_transaction_depth += 1
            try:
                try:
                    os.lstat(self.path)
                except FileNotFoundError:
                    pass
                else:
                    self.load()
                self._app_styles = []
                self.save()
            except PersistenceStateReconciledError:
                raise
            except Exception:
                self._restore_rollback_state(rollback)
                raise
            finally:
                self._write_transaction_depth -= 1

    def _remember_app_style_locked(
        self,
        app: str,
        style: str,
        *,
        source: str,
        now: str,
        immediate: bool = False,
    ) -> LearnedAppStyle:
        existing_index = next(
            (index for index, item in enumerate(self._app_styles) if item.app == app),
            None,
        )
        existing = None if existing_index is None else self._app_styles[existing_index]
        same = existing is not None and existing.style == style
        if immediate:
            count = MIN_GENERALIZATION_COUNT
            if same and existing is not None:
                count = max(existing.count, MIN_GENERALIZATION_COUNT)
        elif same and existing is not None:
            count = min(existing.count + 1, MAX_CONFIRMATIONS)
        else:
            count = 1
        updated = LearnedAppStyle(
            app=app,
            style=style,
            count=count,
            source=source,
            updated_at=now,
        )
        if existing_index is not None:
            self._app_styles.pop(existing_index)
        self._app_styles.append(updated)
        if len(self._app_styles) > MAX_APP_STYLES:
            self._app_styles.pop(0)
        return updated

    def apply(
        self,
        text: str,
        *,
        style: str | None = None,
        app: str | None = None,
        prose_context: bool = False,
        policy_enabled: bool | None = None,
    ) -> str:
        """Apply scoped exact corrections and conservative learned variants.

        Exact corrections are honored after one explicit correction. Separator
        and possessive/plural variants need repeated evidence, a distinctive
        target, and an unambiguous destination mapping. No fuzzy edit-distance
        matching is used: ordinary neighboring words must never be rewritten.
        """
        if type(prose_context) is not bool:
            raise TypeError("prose_context must be a boolean")
        _validate_policy_override(policy_enabled, "policy_enabled")
        if not text:
            return text
        original_text = text
        with self._lock:
            if not self._integrity_ok or not _effective_policy(
                policy_enabled, self._enabled, "policy_enabled"
            ):
                return original_text
            terms = _select_terms(self._terms, style=style, app=app)
        text = unicodedata.normalize("NFC", original_text)
        whole_candidates: list[_Replacement] = []
        for term in terms:
            if _ambiguous_single_ascii(term.spoken):
                whole = _whole_utterance_token(text, term.spoken, style=style)
                if whole is not None:
                    start, end = whole
                    whole_candidates.append(
                        _Replacement(
                            start,
                            end,
                            _preserve_observed_case(term.written, text[start:end]),
                            len(term.spoken),
                            True,
                            term.count,
                        )
                    )
                continue
            whole = _whole_utterance_phrase(text, term.spoken)
            if whole is not None:
                start, end = whole
                whole_candidates.append(
                    _Replacement(
                        start,
                        end,
                        term.written,
                        len(term.spoken),
                        True,
                        term.count,
                    )
                )
            if term.count < MIN_GENERALIZATION_COUNT or not _distinctive_target(term):
                continue
            variant = _whole_natural_variant(text, term)
            if variant is not None:
                start, end, written = variant
                whole_candidates.append(
                    _Replacement(
                        start,
                        end,
                        written,
                        len(term.spoken),
                        False,
                        term.count,
                    )
                )
            separator = _whole_separator_variant(text, term)
            if separator is not None:
                start, end, suffix = separator
                whole_candidates.append(
                    _Replacement(
                        start,
                        end,
                        term.written + suffix,
                        len(term.spoken),
                        False,
                        term.count,
                    )
                )

        # Whole-utterance corrections are explicit enough to remain available
        # without prose-shape inference. Longer replacements require a positive
        # high-confidence prose decision, never a growing syntax blacklist.
        if whole_candidates:
            applied = _apply_non_overlapping(text, whole_candidates)
            return original_text if applied == text else applied
        if _normalize_scope(style) == "code" or not prose_context or _contains_clear_literal(text):
            return original_text

        candidates: list[_Replacement] = []
        for term in terms:
            if _ambiguous_single_ascii(term.spoken):
                continue
            exact = _exact_pattern(term)
            candidates.extend(
                _Replacement(
                    match.start(),
                    match.end(),
                    term.written,
                    len(term.spoken),
                    True,
                    term.count,
                )
                for match in exact.finditer(text)
            )
            if term.count < MIN_GENERALIZATION_COUNT or not _distinctive_target(term):
                continue
            variant_pattern = _variant_pattern(term)
            if variant_pattern is None:
                continue
            candidates.extend(
                _Replacement(
                    match.start(),
                    match.end(),
                    _variant_written(term, match),
                    len(term.spoken),
                    False,
                    term.count,
                )
                for match in variant_pattern.finditer(text)
            )
        applied = _apply_non_overlapping(text, candidates)
        return original_text if applied == text else applied

    def record_correction(
        self,
        spoken: str,
        written: str,
        *,
        source: str = "spoken_edit",
        style: str | None = None,
        app: str | None = None,
        policy_enabled: bool | None = None,
        policy_learn: bool | None = None,
    ) -> LearnedTerm | None:
        _validate_policy_override(policy_enabled, "policy_enabled")
        _validate_policy_override(policy_learn, "policy_learn")
        spoken_n = _normalize_phrase(spoken)
        written_n = unicodedata.normalize("NFC", (written or "").strip())
        if not spoken_n or not written_n:
            return None
        if len(spoken_n) > MAX_PHRASE_CHARS or len(written_n) > MAX_PHRASE_CHARS:
            return None
        if spoken_n == written_n:
            return None
        style_n = _normalize_scope(style)
        app_n = _normalize_app(app)
        source_n = _normalize_phrase(source)[:MAX_SOURCE_CHARS] or "spoken_edit"
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with _coordinated_store_write(self.path), self._lock:
            if (
                not self._integrity_ok
                or not _effective_policy(policy_enabled, self._enabled, "policy_enabled")
                or not _effective_policy(policy_learn, self._learn, "policy_learn")
            ):
                return None
            rollback = self._state_for_rollback()
            identity = _term_identity(spoken_n, style_n, app_n)
            known = next(
                (term for term in self._terms if _term_identity_of(term) == identity),
                None,
            )
            self._write_transaction_depth += 1
            try:
                # The OS/path lock makes this a reload-merge-replace transaction
                # across independent store objects and cooperating processes.
                # If no file exists, retain acknowledged in-memory state (also
                # useful to direct callers that deliberately stub persistence).
                try:
                    os.lstat(self.path)
                except FileNotFoundError:
                    pass
                else:
                    self.load()
                if (
                    not self._integrity_ok
                    or not _effective_policy(policy_enabled, self._enabled, "policy_enabled")
                    or not _effective_policy(policy_learn, self._learn, "policy_learn")
                ):
                    return None

                existing_index = next(
                    (
                        index
                        for index, term in enumerate(self._terms)
                        if _term_identity_of(term) == identity
                    ),
                    None,
                )
                existing = None if existing_index is None else self._terms[existing_index]
                # A stale writer may confirm an existing mapping but cannot
                # silently overwrite a different target learned since it loaded.
                if (
                    existing is not None
                    and existing.written != written_n
                    and (known is None or known.written != existing.written)
                ):
                    return None

                if existing is not None:
                    same_target = existing.written == written_n
                    updated = LearnedTerm(
                        spoken=spoken_n,
                        written=written_n,
                        count=(min(existing.count + 1, MAX_CONFIRMATIONS) if same_target else 1),
                        source=source_n,
                        updated_at=now,
                        style=style_n,
                        app=app_n,
                    )
                    assert existing_index is not None
                    self._terms.pop(existing_index)
                    self._terms.append(updated)
                else:
                    updated = LearnedTerm(
                        spoken=spoken_n,
                        written=written_n,
                        count=1,
                        source=source_n,
                        updated_at=now,
                        style=style_n,
                        app=app_n,
                    )
                    self._terms.append(updated)
                    if len(self._terms) > MAX_TERMS:
                        self._terms.pop(0)
                if style_n in STYLE_NAMES and app_n:
                    self._remember_app_style_locked(
                        app_n,
                        style_n,
                        source=source_n,
                        now=now,
                        immediate=source_n == "typed",
                    )
                self.save()
                return updated
            except PersistenceStateReconciledError:
                raise
            except Exception:
                self._restore_rollback_state(rollback)
                raise
            finally:
                self._write_transaction_depth -= 1

    def record_pairs(
        self,
        pairs: tuple[tuple[str, str], ...],
        *,
        source: str = "spoken_edit",
        style: str | None = None,
        app: str | None = None,
        policy_enabled: bool | None = None,
        policy_learn: bool | None = None,
    ) -> int:
        _validate_policy_override(policy_enabled, "policy_enabled")
        _validate_policy_override(policy_learn, "policy_learn")
        recorded = 0
        for spoken, written in pairs:
            if (
                self.record_correction(
                    spoken,
                    written,
                    source=source,
                    style=style,
                    app=app,
                    policy_enabled=policy_enabled,
                    policy_learn=policy_learn,
                )
                is not None
            ):
                recorded += 1
        return recorded

    def note_utterance(
        self,
        raw: str,
        cleaned: str,
        *,
        style: str | None = None,
        app: str | None = None,
        policy_enabled: bool | None = None,
        policy_learn: bool | None = None,
    ) -> None:
        """Remember the last dictation in RAM only. Audio is never stored."""
        _validate_policy_override(policy_enabled, "policy_enabled")
        _validate_policy_override(policy_learn, "policy_learn")
        with self._lock:
            if (
                not self._integrity_ok
                or not _effective_policy(policy_enabled, self._enabled, "policy_enabled")
                or not _effective_policy(policy_learn, self._learn, "policy_learn")
            ):
                return
            self._last_raw = (raw or "").strip()
            self._last_cleaned = (cleaned or "").strip()
            self._last_style = _normalize_scope(style)
            self._last_app = _normalize_app(app)

    def last_utterance(self) -> dict[str, str]:
        with self._lock:
            return {"raw": self._last_raw, "cleaned": self._last_cleaned}

    def learn_last(
        self,
        correction: str,
        *,
        source: str = "typed",
        style: str | None = None,
        app: str | None = None,
        policy_enabled: bool | None = None,
        policy_learn: bool | None = None,
    ) -> LearnedTerm | None:
        """Learn a typed correction against the last injected transcript."""
        _validate_policy_override(policy_enabled, "policy_enabled")
        _validate_policy_override(policy_learn, "policy_learn")
        correction_n = _normalize_phrase(correction)
        if not correction_n:
            return None
        with self._lock:
            previous = self._last_cleaned or self._last_raw
            style_n = _normalize_scope(style) or self._last_style
            app_n = _normalize_app(app) or self._last_app
        pair = infer_correction_pair(previous, correction_n)
        if pair is None:
            return None
        return self.record_correction(
            pair[0],
            pair[1],
            source=source,
            style=style_n,
            app=app_n,
            policy_enabled=policy_enabled,
            policy_learn=policy_learn,
        )

    def reset(self) -> None:
        with _coordinated_store_write(self.path), self._lock:
            _strict_policy_bool(self._enabled, "enabled")
            _strict_policy_bool(self._learn, "learn")
            rollback = self._state_for_rollback()
            self._write_transaction_depth += 1
            try:
                try:
                    os.lstat(self.path)
                except FileNotFoundError:
                    pass
                else:
                    self.load()
                self._terms = []
                self._app_styles = []
                self._last_raw = ""
                self._last_cleaned = ""
                self._last_style = ""
                self._last_app = ""
                self.save()
            except PersistenceStateReconciledError:
                raise
            except Exception:
                self._restore_rollback_state(rollback)
                raise
            finally:
                self._write_transaction_depth -= 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "path": str(self.path),
                "enabled": self._integrity_ok and self._enabled is True,
                "learn": self._integrity_ok and self._learn is True,
                "stores_audio": False,
                "stores_last_on_disk": False,
                "learning_requires_explicit_correction": True,
                "retention": {"max_terms": MAX_TERMS, "max_phrase_chars": MAX_PHRASE_CHARS},
                "has_last": bool(self._last_cleaned or self._last_raw),
                "last_preview": _preview(self._last_cleaned or self._last_raw),
                "term_count": len(self._terms),
                "terms": [asdict(term) for term in self._terms],
                "app_style_count": len(self._app_styles),
                "app_styles": [asdict(item) for item in self._app_styles],
            }


def default_personalization_path() -> Path:
    return user_config_dir() / STORE_FILENAME


def _path_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _coordinated_store_write(path: Path, *, timeout_s: float = STORE_LOCK_TIMEOUT_S):
    if timeout_s < 0:
        raise ValueError("personalization lock timeout must be non-negative")
    deadline = time.monotonic() + timeout_s
    thread_lock = _path_lock(path)
    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("timed out waiting for personalization path lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        if not hasattr(os, "O_NOFOLLOW") and lock_path.is_symlink():
            raise ValueError("personalization lock symlinks are forbidden")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("personalization lock must be a regular file")
            if os.name != "nt":
                platform_os: Any = os
                platform_os.fchmod(fd, 0o600)
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                _acquire_advisory_lock(fd, deadline, windows=True)
                try:
                    yield
                finally:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = __import__("fcntl")

                _acquire_advisory_lock(fd, deadline, windows=False)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


def _acquire_advisory_lock(fd: int, deadline: float, *, windows: bool) -> None:
    while True:
        try:
            if windows:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = __import__("fcntl")

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            retryable = exc.errno in {errno.EACCES, errno.EAGAIN}
            if not retryable:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for personalization advisory lock") from exc
            time.sleep(min(STORE_LOCK_RETRY_S, remaining))


def _strict_policy_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value


def _policy_override(value: Any, name: str) -> bool | None:
    if value is _POLICY_UNSET:
        return None
    return _strict_policy_bool(value, name)


def _validate_policy_override(value: Any, name: str) -> None:
    if value is not None:
        _strict_policy_bool(value, name)


def _effective_policy(override: bool | None, stored: Any, name: str) -> bool:
    _validate_policy_override(override, name)
    return stored is True if override is None else override


def infer_correction_pair(previous: str, correction: str) -> tuple[str, str] | None:
    """Infer a spoken→written pair from a typed replacement of the last utterance."""
    before = _normalize_phrase(previous)
    after = _normalize_phrase(correction)
    if not before or not after or before == after:
        return None
    prev_words = before.split()
    new_words = after.split()
    prefix = 0
    while (
        prefix < len(prev_words)
        and prefix < len(new_words)
        and prev_words[prefix] == new_words[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < (len(prev_words) - prefix)
        and suffix < (len(new_words) - prefix)
        and prev_words[-(suffix + 1)] == new_words[-(suffix + 1)]
    ):
        suffix += 1
    old_mid = prev_words[prefix : len(prev_words) - suffix if suffix else None]
    new_mid = new_words[prefix : len(new_words) - suffix if suffix else None]
    if old_mid and new_mid:
        return _trim_shared_edge_punctuation(" ".join(old_mid), " ".join(new_mid))
    if len(prev_words) <= 8 and len(new_words) <= 8:
        return (before, after)
    return None


def _preview(value: str, limit: int = 80) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _trim_shared_edge_punctuation(before: str, after: str) -> tuple[str, str]:
    """Keep sentence punctuation out of an otherwise token-level correction."""
    edge = frozenset(".,!?;:")
    while before and after and before[0] == after[0] and before[0] in edge:
        before, after = before[1:], after[1:]
    while before and after and before[-1] == after[-1] and before[-1] in edge:
        before, after = before[:-1], after[:-1]
    return before or "", after or ""


def _normalize_phrase(value: str) -> str:
    return unicodedata.normalize("NFC", " ".join((value or "").split()).strip())


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _load_validated_store(
    path: Path,
) -> tuple[bool, bool, list[LearnedTerm], bool, list[LearnedAppStyle]]:
    encoded = _read_bounded_store(path)
    _validate_json_depth(encoded)
    raw = json.loads(
        encoded.decode("utf-8"),
        parse_constant=_reject_json_constant,
        object_pairs_hook=_strict_json_object,
    )
    return _validate_store_payload(raw)


def _read_bounded_store(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fallback_identity: tuple[int, int] | None = None
    if not hasattr(os, "O_NOFOLLOW"):
        before_open = os.lstat(path)
        if stat.S_ISLNK(before_open.st_mode):
            raise ValueError("personalization store symlinks are forbidden")
        fallback_identity = (before_open.st_dev, before_open.st_ino)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("personalization store must be a regular file")
        if fallback_identity is not None:
            after_open = os.lstat(path)
            if (
                stat.S_ISLNK(after_open.st_mode)
                or fallback_identity
                != (
                    opened.st_dev,
                    opened.st_ino,
                )
                or fallback_identity != (after_open.st_dev, after_open.st_ino)
            ):
                raise ValueError("personalization store changed during safe open")
        if opened.st_size > MAX_STORE_BYTES:
            raise ValueError("personalization store exceeds byte limit")
        chunks: list[bytes] = []
        remaining = MAX_STORE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
    finally:
        os.close(fd)
    if len(encoded) > MAX_STORE_BYTES:
        raise ValueError("personalization store exceeds byte limit")
    return encoded


def _sync_parent_directory(path: Path) -> None:
    """Durably publish a completed atomic replace where directory fsync exists."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", -1)}:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {
                errno.EINVAL,
                errno.ENOSYS,
                getattr(errno, "ENOTSUP", -1),
            }:
                raise
    finally:
        os.close(fd)


def _restore_after_failed_directory_sync(path: Path, previous: bytes | None) -> None:
    """Best-effort logical rollback after a rename whose directory sync failed."""
    if previous is None:
        path.unlink(missing_ok=True)
        return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".rollback", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(previous)
            handle.flush()
            with suppress(OSError):
                os.fsync(handle.fileno())
            # The original durability failure may affect every sync call; the
            # prior visible state is still restored atomically below.
        Path(tmp_name).replace(path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _store_artifacts(path: Path) -> list[Path]:
    prefix = f".{path.name}."
    artifacts: list[Path] = []
    try:
        entries = path.parent.iterdir()
    except OSError:
        return artifacts
    for candidate in entries:
        if not candidate.name.startswith(prefix) or not candidate.name.endswith(
            (".tmp", ".rollback")
        ):
            continue
        try:
            metadata = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            artifacts.append(candidate)
    return artifacts


def _cleanup_store_artifacts_locked(path: Path) -> None:
    """Delete only this store writer's recognized regular crash artifacts."""
    for artifact in _store_artifacts(path):
        artifact.unlink(missing_ok=True)


def _validate_json_depth(encoded: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in encoded:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in {0x5B, 0x7B}:  # [ {
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("personalization store exceeds nesting limit")
        elif byte in {0x5D, 0x7D}:  # ] }
            depth -= 1
            if depth < 0:
                raise ValueError("personalization store nesting is invalid")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _validate_store_payload(
    raw: Any,
) -> tuple[bool, bool, list[LearnedTerm], bool, list[LearnedAppStyle]]:
    if type(raw) is not dict:
        raise ValueError("personalization store root must be an object")
    version_present = "version" in raw
    version = raw.get("version", 1)
    if type(version) is not int or version not in {1, 2, STORE_VERSION}:
        raise ValueError("unsupported personalization store version")
    legacy = not version_present or version in {1, 2}
    allowed_root = {"version", "enabled", "learn", "terms", "app_styles"}
    if not set(raw).issubset(allowed_root):
        raise ValueError("unexpected personalization store member")
    if "terms" not in raw or type(raw["terms"]) is not list:
        raise ValueError("personalization store terms must be an array")
    if "app_styles" in raw and type(raw["app_styles"]) is not list:
        raise ValueError("personalization store app_styles must be an array")

    if legacy:
        has_enabled = "enabled" in raw
        has_learn = "learn" in raw
        if has_enabled != has_learn:
            raise ValueError("legacy personalization policy must be complete")
        if has_enabled:
            enabled = _strict_policy_bool(raw["enabled"], "persisted enabled")
            learn = _strict_policy_bool(raw["learn"], "persisted learn")
        else:
            enabled, learn = True, True
    else:
        if "enabled" not in raw or "learn" not in raw:
            raise ValueError("current personalization policy must be complete")
        enabled = _strict_policy_bool(raw["enabled"], "persisted enabled")
        learn = _strict_policy_bool(raw["learn"], "persisted learn")
        if len(raw["terms"]) > MAX_TERMS:
            raise ValueError("current personalization store exceeds term limit")
        if len(raw.get("app_styles", [])) > MAX_APP_STYLES:
            raise ValueError("current personalization store exceeds app-style limit")

    terms = [_parse_persisted_term(item, legacy=legacy) for item in raw["terms"]]
    identities = [_term_identity_of(term) for term in terms]
    if len(set(identities)) != len(identities):
        raise ValueError("personalization store contains duplicate scoped terms")
    app_styles = [
        _parse_persisted_app_style(item, legacy=legacy) for item in raw.get("app_styles", [])
    ]
    apps = [item.app for item in app_styles]
    if len(set(apps)) != len(apps):
        raise ValueError("personalization store contains duplicate app styles")
    return enabled, learn, terms, legacy, app_styles


def _parse_persisted_term(item: Any, *, legacy: bool) -> LearnedTerm:
    if type(item) is not dict:
        raise ValueError("personalization term must be an object")
    current_keys = {
        "spoken",
        "written",
        "count",
        "source",
        "updated_at",
        "style",
        "app",
    }
    required = current_keys if not legacy else {"spoken", "written"}
    if not required.issubset(item) or not set(item).issubset(current_keys):
        raise ValueError("personalization term schema is invalid")

    spoken = item["spoken"]
    written = item["written"]
    count = item.get("count", 1)
    source = item.get("source", "legacy")
    updated_at = item.get("updated_at", "1970-01-01T00:00:00+00:00")
    style = item.get("style", "")
    app = item.get("app", "")
    string_fields = {
        "spoken": spoken,
        "written": written,
        "source": source,
        "updated_at": updated_at,
        "style": style,
        "app": app,
    }
    if any(type(value) is not str for value in string_fields.values()):
        raise ValueError("personalization term text fields must be strings")
    if legacy:
        spoken = _normalize_phrase(spoken)
        written = unicodedata.normalize("NFC", written.strip())
        source = _normalize_phrase(source)
        style = _normalize_scope(style)
        app = _normalize_app(app)
    if (
        not spoken
        or spoken != _normalize_phrase(spoken)
        or len(spoken) > MAX_PHRASE_CHARS
        or not written
        or written != written.strip()
        or written != unicodedata.normalize("NFC", written)
        or len(written) > MAX_PHRASE_CHARS
        or not source
        or source != _normalize_phrase(source)
        or len(source) > MAX_SOURCE_CHARS
        or style != _normalize_scope(style)
        or app != _normalize_app(app)
    ):
        raise ValueError("personalization term text is invalid")
    if type(count) is not int or not 1 <= count <= MAX_CONFIRMATIONS:
        raise ValueError("personalization term count is invalid")
    if not updated_at or len(updated_at) > MAX_SOURCE_CHARS:
        raise ValueError("personalization term timestamp is invalid")
    updated_at = _canonical_persisted_timestamp(updated_at, legacy=legacy)
    return LearnedTerm(
        spoken=spoken,
        written=written,
        count=count,
        source=source,
        updated_at=updated_at,
        style=style,
        app=app,
    )


def _canonical_persisted_timestamp(value: str, *, legacy: bool) -> str:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise ValueError("personalization term timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise ValueError("personalization term timestamp must include a timezone")
    utc_timestamp = timestamp.astimezone(UTC)
    if not MIN_TIMESTAMP_YEAR <= utc_timestamp.year <= MAX_TIMESTAMP_YEAR:
        raise ValueError("personalization term timestamp is outside supported dates")
    canonical = utc_timestamp.replace(microsecond=0).isoformat()
    if not legacy and value != canonical:
        raise ValueError("current personalization timestamp is not canonical UTC")
    return canonical


def _normalize_scope(value: Any) -> str:
    return _normalize_phrase(str(value or "")).casefold()[:MAX_SCOPE_CHARS]


def _normalize_app(value: Any) -> str:
    raw = _normalize_phrase(str(value or "")).replace("\\", "/")
    return raw.rsplit("/", 1)[-1].casefold()[:MAX_SCOPE_CHARS]


def _term_identity(spoken: str, style: str, app: str) -> tuple[str, str, str]:
    return (
        unicodedata.normalize("NFC", spoken).casefold(),
        unicodedata.normalize("NFC", style).casefold(),
        unicodedata.normalize("NFC", app).casefold(),
    )


def _term_identity_of(term: LearnedTerm) -> tuple[str, str, str]:
    return _term_identity(term.spoken, term.style, term.app)


def _parse_persisted_app_style(item: Any, *, legacy: bool) -> LearnedAppStyle:
    if type(item) is not dict:
        raise ValueError("personalization app style must be an object")
    required = {"app", "style", "count", "source", "updated_at"}
    if not required.issubset(item) or not set(item).issubset(required):
        raise ValueError("personalization app style schema is invalid")
    app = item["app"]
    style = item["style"]
    count = item["count"]
    source = item["source"]
    updated_at = item["updated_at"]
    if any(type(value) is not str for value in (app, style, source, updated_at)):
        raise ValueError("personalization app style text fields must be strings")
    if legacy:
        app = _normalize_app(app)
        style = _normalize_scope(style)
        source = _normalize_phrase(source)
    if (
        not app
        or app != _normalize_app(app)
        or style not in STYLE_NAMES
        or not source
        or source != _normalize_phrase(source)
        or len(source) > MAX_SOURCE_CHARS
    ):
        raise ValueError("personalization app style text is invalid")
    if type(count) is not int or not 1 <= count <= MAX_CONFIRMATIONS:
        raise ValueError("personalization app style count is invalid")
    if not updated_at or len(updated_at) > MAX_SOURCE_CHARS:
        raise ValueError("personalization app style timestamp is invalid")
    updated_at = _canonical_persisted_timestamp(updated_at, legacy=legacy)
    return LearnedAppStyle(
        app=app,
        style=style,
        count=count,
        source=source,
        updated_at=updated_at,
    )


def _merge_app_styles(
    remote: list[LearnedAppStyle],
    local: list[LearnedAppStyle],
    baseline: list[LearnedAppStyle],
) -> list[LearnedAppStyle]:
    """Three-way merge destination styles without resurrecting a reset."""
    remote_by = {item.app: item for item in remote}
    local_by = {item.app: item for item in local}
    baseline_by = {item.app: item for item in baseline}
    chosen: dict[str, LearnedAppStyle] = {}
    for app in remote_by.keys() | local_by.keys():
        remote_item = remote_by.get(app)
        local_item = local_by.get(app)
        baseline_item = baseline_by.get(app)
        if remote_item is None:
            if baseline_item is None:
                assert local_item is not None
                chosen[app] = local_item
            elif local_item != baseline_item:
                raise ValueError("personalization save conflicts with a remote style reset")
            continue
        if local_item is None:
            chosen[app] = remote_item
            continue
        if remote_item.style == local_item.style:
            chosen[app] = max(
                (remote_item, local_item),
                key=lambda item: (item.count, item.updated_at, item.source),
            )
            continue
        if baseline_item is not None and local_item.style == baseline_item.style:
            chosen[app] = remote_item
            continue
        if baseline_item is not None and remote_item.style == baseline_item.style:
            chosen[app] = local_item
            continue
        raise ValueError("personalization save contains a concurrent app-style conflict")

    ordered: list[LearnedAppStyle] = []
    for item in local:
        selected = chosen.get(item.app)
        if selected == item:
            ordered.append(item)
    present = {item.app for item in ordered}
    ordered.extend(
        item for item in remote if item.app not in present and chosen.get(item.app) == item
    )
    ordered.sort(key=lambda item: item.updated_at)
    return ordered[-MAX_APP_STYLES:]


def _merge_terms(
    remote: list[LearnedTerm],
    local: list[LearnedTerm],
    baseline: list[LearnedTerm],
) -> list[LearnedTerm]:
    """Three-way merge stale public saves without resurrecting reset terms."""
    remote_by = {_term_identity_of(term): term for term in remote}
    local_by = {_term_identity_of(term): term for term in local}
    baseline_by = {_term_identity_of(term): term for term in baseline}
    chosen: dict[tuple[str, str, str], LearnedTerm] = {}
    for identity in remote_by.keys() | local_by.keys():
        remote_term = remote_by.get(identity)
        local_term = local_by.get(identity)
        baseline_term = baseline_by.get(identity)
        if remote_term is None:
            if baseline_term is None:
                assert local_term is not None
                chosen[identity] = local_term
            elif local_term != baseline_term:
                raise ValueError("personalization save conflicts with a remote reset")
            # An unchanged stale local term must not resurrect an explicit reset.
            continue
        if local_term is None:
            chosen[identity] = remote_term
            continue
        if remote_term.written == local_term.written:
            chosen[identity] = max(
                (remote_term, local_term),
                key=lambda term: (term.count, term.updated_at, term.source),
            )
            continue
        if baseline_term is not None and local_term.written == baseline_term.written:
            chosen[identity] = remote_term
            continue
        if baseline_term is not None and remote_term.written == baseline_term.written:
            chosen[identity] = local_term
            continue
        raise ValueError("personalization save contains a concurrent mapping conflict")

    # Preserve each writer's oldest->newest order. Canonical timestamps merge
    # the two partial chronologies; on same-second ties, pending local entries
    # precede already-persisted remote entries so an acknowledged remote term
    # is not the first eviction merely because a stale save arrived later.
    ordered: list[LearnedTerm] = []
    for term in local:
        identity = _term_identity_of(term)
        selected = chosen.get(identity)
        if selected == term:
            ordered.append(term)
    present = {_term_identity_of(term) for term in ordered}
    ordered.extend(
        term
        for term in remote
        if _term_identity_of(term) not in present and chosen.get(_term_identity_of(term)) == term
    )
    ordered.sort(key=lambda term: term.updated_at)
    return ordered[-MAX_TERMS:]


def _select_terms(
    terms: list[LearnedTerm], *, style: str | None, app: str | None
) -> tuple[LearnedTerm, ...]:
    """Choose the most-specific, non-conflicting mapping for each phrase."""
    style_n = _normalize_scope(style)
    app_n = _normalize_app(app)
    grouped: dict[str, list[tuple[int, LearnedTerm]]] = {}
    for term in terms:
        if term.style and term.style != style_n:
            continue
        if term.app and term.app != app_n:
            continue
        specificity = int(bool(term.style)) + int(bool(term.app))
        grouped.setdefault(term.spoken.casefold(), []).append((specificity, term))

    selected: list[LearnedTerm] = []
    for candidates in grouped.values():
        best_specificity = max(score for score, _term in candidates)
        best = [term for score, term in candidates if score == best_specificity]
        # App-only and style-only rules can tie. Refuse to guess if they point
        # at different targets in the same destination.
        if len({term.written for term in best}) != 1:
            continue
        selected.append(max(best, key=lambda term: (term.count, term.updated_at)))
    return tuple(selected)


def _distinctive_target(term: LearnedTerm) -> bool:
    spoken_words = term.spoken.split()
    if len(spoken_words) > 1:
        return True
    written = term.written
    # Single-word morphology is limited to proper/uncommon technical forms.
    # A plain lower-case semantic replacement such as there->their stays exact.
    return any(char.isupper() or char.isdigit() or not char.isalnum() for char in written)


_CJK_RANGES = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"


def _ambiguous_single_ascii(spoken: str) -> bool:
    return re.fullmatch(r"[A-Za-z]+", spoken) is not None


def _whole_utterance_token(text: str, spoken: str, *, style: str | None) -> tuple[int, int] | None:
    """Return a lone word span after peeling only outer sentence punctuation."""
    del style
    span = _whole_utterance_core(text)
    if span is None:
        return None
    start, end = span
    if text[start:end].casefold() != spoken.casefold():
        return None
    return start, end


def _whole_utterance_phrase(text: str, spoken: str) -> tuple[int, int] | None:
    span = _whole_utterance_core(text)
    if span is None:
        return None
    start, end = span
    if text[start:end].casefold() != spoken.casefold():
        return None
    return start, end


def _whole_utterance_core(text: str) -> tuple[int, int] | None:
    start = 0
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    closing = frozenset(".!?,;:…！？，。；：")
    while end > start:
        if text[end - 1].isspace() or text[end - 1] in closing:
            end -= 1
            continue
        break
    if start >= end:
        return None
    return start, end


def _whole_separator_variant(text: str, term: LearnedTerm) -> tuple[int, int, str] | None:
    words = term.spoken.split()
    if len(words) < 2 or any(re.fullmatch(r"[\w]+", word) is None for word in words):
        return None
    span = _whole_utterance_core(text)
    if span is None:
        return None
    start, end = span
    core = text[start:end]
    joined = r"[-_]+".join(re.escape(word) for word in words)
    collapsed = re.escape("".join(words))
    match = re.fullmatch(
        rf"(?:{joined}|{collapsed})(?P<suffix>['’]s|s)?",
        core,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return start, end, match.group("suffix") or ""


def _whole_natural_variant(text: str, term: LearnedTerm) -> tuple[int, int, str] | None:
    pattern = _variant_pattern(term)
    span = _whole_utterance_core(text)
    if pattern is None or span is None:
        return None
    start, end = span
    match = pattern.fullmatch(text, start, end)
    if match is None:
        return None
    return start, end, _variant_written(term, match)


def _preserve_observed_case(written: str, observed: str) -> str:
    if observed.isupper():
        return written.upper()
    if observed[:1].isupper() and observed[1:].islower() and written.islower():
        return written[:1].upper() + written[1:]
    return written


def _exact_pattern(term: LearnedTerm) -> re.Pattern[str]:
    escaped = re.escape(term.spoken)
    cjk_count = sum(bool(re.fullmatch(f"[{_CJK_RANGES}]", char)) for char in term.spoken)
    if cjk_count >= 2:
        # CJK normally has no spaces between words. Permit an explicitly
        # learned multi-character phrase inside natural script, while refusing
        # to rewrite a fragment embedded in an ASCII identifier.
        pattern = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    else:
        # A single ideograph is too collision-prone inside a larger word.
        pattern = rf"(?<!\w){escaped}(?!\w|['’]s\b)"
    return re.compile(pattern, re.IGNORECASE)


def _apply_non_overlapping(
    text: str,
    candidates: list[_Replacement],
) -> str:
    """Choose replacements on the original text, then splice exactly once."""
    by_span: dict[tuple[int, int], list[_Replacement]] = {}
    for candidate in candidates:
        by_span.setdefault((candidate.start, candidate.end), []).append(candidate)

    resolved: list[_Replacement] = []
    for same_span in by_span.values():
        best_rank = max((int(item.exact), item.source_length, item.count) for item in same_span)
        best = [
            item
            for item in same_span
            if (int(item.exact), item.source_length, item.count) == best_rank
        ]
        if len({item.written for item in best}) == 1:
            resolved.append(best[0])

    chosen: list[_Replacement] = []
    for candidate in sorted(
        resolved,
        key=lambda item: (
            -(item.end - item.start),
            -int(item.exact),
            -item.source_length,
            -item.count,
            item.start,
        ),
    ):
        if any(
            candidate.start < existing.end and existing.start < candidate.end for existing in chosen
        ):
            continue
        chosen.append(candidate)

    if not chosen:
        return text
    parts: list[str] = []
    cursor = 0
    for candidate in sorted(chosen, key=lambda item: item.start):
        parts.append(text[cursor : candidate.start])
        parts.append(candidate.written)
        cursor = candidate.end
    parts.append(text[cursor:])
    return "".join(parts)


_CLEAR_URL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.)")
_CLEAR_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?!\w)")
_CLEAR_CODE = re.compile(r"[`{}\[\]<>=|&]|(?<!\w)--?[A-Za-z0-9]")
_CLEAR_CALL = re.compile(r"\b[A-Za-z_]\w*\(")


def _contains_clear_literal(text: str) -> bool:
    """Keep unmistakable literal data unchanged even in trusted prose mode."""
    if _CLEAR_URL.search(text) is not None or _CLEAR_EMAIL.search(text) is not None:
        return True
    if (
        "\\" in text
        or "/" in text
        or _CLEAR_CODE.search(text) is not None
        or _CLEAR_CALL.search(text) is not None
    ):
        return True
    for index, char in enumerate(text):
        if char != ".":
            continue
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if before.isdigit() and after.isdigit():
            continue
        if not after or after.isspace() or after in ".!?…。！？":
            continue
        return True
    return False


def _variant_pattern(term: LearnedTerm) -> re.Pattern[str] | None:
    words = term.spoken.split()
    if not words or any(re.fullmatch(r"[\w]+", word) is None for word in words):
        return None
    if len(words) == 1:
        # Possessives of explicitly learned names are safe; pluralizing an
        # arbitrary single token is much more collision-prone.
        return re.compile(rf"(?<!\w){re.escape(words[0])}(?P<suffix>['’]s)(?!\w)", re.IGNORECASE)
    # Natural-space morphology can generalize in prose. Hyphen/underscore/
    # collapsed spellings are handled separately as whole utterances only.
    joined = r"\s+".join(re.escape(word) for word in words)
    return re.compile(rf"(?<!\w){joined}(?P<suffix>['’]s|s)?(?!\w)", re.IGNORECASE)


def _variant_written(term: LearnedTerm, match: re.Match[str]) -> str:
    return term.written + (match.groupdict().get("suffix") or "")
