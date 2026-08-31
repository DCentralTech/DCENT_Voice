# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Serve and control settings and setup-wizard windows."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

try:  # pragma: no cover - Python 3.11+ uses stdlib; fallback helps local 3.10 test runners.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from dcent_voice import __version__
from dcent_voice.asr.factory import describe_asr
from dcent_voice.asr.language import language_choices, resolve_language_policy
from dcent_voice.asr.model_registry import (
    MODEL_ALIASES,
    faster_whisper_model_status,
    faster_whisper_root,
    valid_faster_whisper_snapshot,
)
from dcent_voice.auth.base import ApiKeyAuth, AuthMode
from dcent_voice.auth.oauth import (
    OAUTH_CONFIGS,
    DeviceCodeGrant,
    OAuthAuth,
    poll_device_token,
    request_device_code,
)
from dcent_voice.auth.registry import get_provider_capability, list_provider_capabilities
from dcent_voice.auth.store import CredentialStore
from dcent_voice.auth.validate import (
    CREDENTIAL_EGRESS_PAYLOAD,
    credential_consent_key,
    validate_api_key,
)
from dcent_voice.config import (
    AppConfig,
    ASRSpec,
    ConfigError,
    default_config_path,
    effective_snippets,
    load_config,
    parse_config,
)
from dcent_voice.events import ConfigChanged, EventBus, PrivacyChanged
from dcent_voice.hotkeys import normalize_key
from dcent_voice.privacy import FIRST_RUN_EDUCATION, ConsentRequired, PrivacyMonitor
from dcent_voice.recovery import RecoveryStore
from dcent_voice.tts.assets import (
    ASSETS_BY_BACKEND,
    MODEL_DOWNLOAD_KEY,
    ChecksumError,
    backend_assets_present,
    install_backend_assets,
)

_CONFIG_LOCKS_GUARD = threading.Lock()
_CONFIG_LOCKS: dict[Path, threading.RLock] = {}
_LOGGER = logging.getLogger(__name__)
_TTS_RUNTIME_MODULES = {"kokoro": "kokoro_onnx"}
_MISSING_UNDO_FRAME = object()


def _snippet_row_payload(entry: Any) -> dict[str, Any]:
    row = {
        "spoken": entry.spoken,
        "expansion": entry.expansion,
    }
    if getattr(entry, "starred", False):
        row["starred"] = True
    added_at = str(getattr(entry, "added_at", "") or "")
    if added_at:
        row["added_at"] = added_at
    return row


def _dictionary_row_payload(entry: Any) -> dict[str, Any]:
    row = {
        "spoken": entry.spoken,
        "written": entry.written,
    }
    if getattr(entry, "starred", False):
        row["starred"] = True
    added_at = str(getattr(entry, "added_at", "") or "")
    if added_at:
        row["added_at"] = added_at
    return row


def _config_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _CONFIG_LOCKS_GUARD:
        return _CONFIG_LOCKS.setdefault(key, threading.RLock())


def _tts_runtime_available(backend: str) -> bool:
    """Return whether the local inference runtime for a TTS backend is installed."""

    module = _TTS_RUNTIME_MODULES.get(backend)
    return module is not None and importlib.util.find_spec(module) is not None


class SettingsApi:
    """Webview bridge that exposes settings operations to the UI."""

    def __init__(
        self,
        *,
        config: AppConfig,
        bus: EventBus,
        privacy: PrivacyMonitor,
        credential_store: CredentialStore | None = None,
        tts_fetch: Callable[[str], bytes] | None = None,
        tts_model_root: Path | None = None,
        personalization: Any = None,
        recovery_store: RecoveryStore | None = None,
    ) -> None:
        # pywebview recursively walks every public js_api attribute while it
        # builds the JavaScript bridge. Runtime objects such as AppConfig and
        # pathlib.Path are not bridge methods and can form effectively infinite
        # attribute chains (Path.parent at the filesystem root). Keep all
        # runtime state private so only the intentional public methods below are
        # exposed to JavaScript.
        self._config = config
        self._bus = bus
        self._privacy = privacy
        self._credential_store = credential_store
        self._device_grants: dict[str, DeviceCodeGrant] = {}
        self._tts_fetch = tts_fetch
        self._tts_model_root = tts_model_root
        self._tts_install_lock = threading.Lock()
        self._personalization = personalization
        self._recovery = recovery_store or RecoveryStore.from_config(config)
        self._snippet_import_undo: list[Any] = []
        self._dictionary_import_undo: list[Any] = []
        self._load_snippet_undo_stash()
        self._load_dictionary_undo_stash()

    def _update_runtime(self, config: AppConfig, privacy: PrivacyMonitor) -> None:
        """Refresh state shared with the live runtime without exposing it to JS."""

        self._config = config
        self._privacy = privacy
        self._recovery.update_policy(config.recovery)
        if self._personalization is not None:
            self._personalization.update_policy(
                enabled=config.personalization.enabled,
                learn=config.personalization.learn,
            )

    def get_config(self) -> dict[str, Any]:
        from dcent_voice.dictation.style import DEFAULT_APP_STYLES

        snapshot = config_snapshot(self._config)
        snapshot["hardware"] = self.hardware_status()
        snapshot["learned"] = self.personalization_snapshot()
        style = dict(snapshot.get("style") or {})
        style["built_in"] = dict(DEFAULT_APP_STYLES)
        snapshot["style"] = style
        if self._snippet_import_undo:
            snapshot["snippet_undo"] = True
        if self._dictionary_import_undo:
            snapshot["dictionary_undo"] = True
        return snapshot

    def _personalization_store(self) -> Any:
        from dcent_voice.personalization import PersonalizationStore

        if self._personalization is not None:
            return self._personalization
        return PersonalizationStore(
            enabled=self._config.personalization.enabled,
            learn=self._config.personalization.learn,
        )

    def personalization_snapshot(self) -> dict[str, Any]:
        return self._personalization_store().snapshot()

    def learn_last(self, correction: str) -> dict[str, Any]:
        store = self._personalization_store()
        store.learn_last(str(correction or ""), source="typed")
        return store.snapshot()

    def reset_personalization(self) -> dict[str, Any]:
        store = self._personalization_store()
        store.reset()
        return store.snapshot()

    def remember_app_style(self, app: str, style: str) -> dict[str, Any]:
        store = self._personalization_store()
        store.remember_app_style(str(app or ""), str(style or ""), source="typed", immediate=True)
        return store.snapshot()

    def reset_app_styles(self) -> dict[str, Any]:
        store = self._personalization_store()
        store.reset_app_styles()
        return store.snapshot()

    def hardware_status(self) -> dict[str, Any]:
        """Report whether local Whisper can use a working CUDA stack.

        Used by Settings / wizard so users without high-end GPUs (or with a GPU
        but incomplete CUDA/cuDNN) see that CPU int8 is the active safe path.
        Never phones home; pure local probes only.
        """
        from dcent_voice.asr.faster_whisper_provider import (
            cuda_runtime_ready,
            resolve_device_compute,
        )

        active = self._config.profiles.get(self._config.active_profile)
        asr_raw = active.asr.raw if active is not None else ""
        resolved_info: dict[str, Any] = {}
        resolved_asr = active.asr if active is not None else None
        if active is not None:
            policy = resolve_language_policy(
                getattr(self._config, "language_mode", None),
                active.language or self._config.language,
            )
            resolved_info = describe_asr(active.asr, policy)
            resolved_asr = ASRSpec.parse(str(resolved_info["resolved"]))
        device, compute = ("cpu", "int8")
        if resolved_asr is not None and resolved_asr.provider == "faster-whisper":
            device, compute = resolve_device_compute(resolved_asr)
        cuda_ready = bool(cuda_runtime_ready())
        if resolved_asr is not None and resolved_asr.provider == "parakeet":
            device, compute = "cpu", resolved_asr.compute_type or "int8"
        if device == "cpu":
            if resolved_asr is not None and resolved_asr.provider == "parakeet":
                summary = (
                    "Active path: local Parakeet ONNX (CPU). Works on any modern "
                    "laptop — no discrete GPU required."
                )
            else:
                summary = (
                    f"Active path: CPU {compute}. Works on any modern laptop — "
                    "no discrete GPU required."
                )
            model_name = (resolved_asr.model if resolved_asr is not None else "").lower()
            if any(token in model_name for token in ("distil", "large", "medium")):
                recommendation = (
                    "This model is heavier on pure CPU (short-utterance finalize may "
                    "exceed ~800 ms). For snappier feel without a GPU, switch to the "
                    "desktop profile (base.en:cpu-int8) or tiny; use quality/accurate "
                    "when you prefer accuracy over speed."
                )
            elif (
                resolved_asr is not None
                and resolved_asr.provider == "faster-whisper"
                and resolved_asr.model.lower().endswith("base.en")
            ):
                recommendation = (
                    "This profile is still Whisper base.en. The shipped default is "
                    "local Parakeet ONNX (desktop). Switch in Settings → Models for "
                    "the faster CPU path; existing configs are not auto-migrated."
                )
            elif not cuda_ready:
                recommendation = (
                    "Stay on the desktop or tiny profile for CPU-only machines. "
                    "Use auto/gpu only after scripts/check_env.py reports CUDA ready."
                )
            else:
                recommendation = "CUDA is available if you want the gpu profile for lower latency."
        elif device == "cuda":
            summary = f"Active path: NVIDIA CUDA ({compute})."
            recommendation = (
                "CUDA stack looks ready."
                if cuda_ready
                else "Config requests CUDA but the runtime is incomplete; "
                "the first dictation may fall back to CPU int8."
            )
        else:
            summary = f"Active path: {device}/{compute}."
            recommendation = "Prefer an explicit :cpu-int8 suffix on machines without a GPU."
        return {
            "cuda_ready": cuda_ready,
            "active_device": device,
            "active_compute": compute,
            "active_asr": asr_raw,
            "resolved_asr": resolved_info.get("resolved", asr_raw),
            "model_readiness": resolved_info.get("model_readiness"),
            "active_profile": self._config.active_profile,
            "summary": summary,
            "recommendation": recommendation,
            "cpu_default_profile": "desktop",
            "cpu_default_asr": "parakeet:tdt-0.6b-v3:int8",
        }

    def set_config(
        self,
        patch: dict[str, Any],
        *,
        clear_snippet_undo: bool = True,
        clear_dictionary_undo: bool = True,
    ) -> dict[str, Any]:
        snippets_changed = self._snippet_patch_changes_list(patch)
        dictionary_changed = self._dictionary_patch_changes_list(patch)
        target = self._config.source_path or default_config_path()
        with _config_lock(target):
            # Read the latest on-disk document while holding the shared lock, so
            # Settings, wizard, and tray disjoint patches cannot overwrite one
            # another from stale snapshots.
            try:
                table = tomllib.loads(target.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                table = config_to_table(self._config)
            deep_merge(table, patch)
            # Validate before touching the file; a bad patch can never brick the
            # last-known-good config.
            parse_config(table, source_path=target)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(dump_config_toml(table))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()
            self._config = load_config(target, create=False)
            self._privacy = PrivacyMonitor.from_config(self._config)
            self._recovery.update_policy(self._config.recovery)
        if clear_snippet_undo and snippets_changed:
            self._clear_snippet_undo_stash()
        if clear_dictionary_undo and dictionary_changed:
            self._clear_dictionary_undo_stash()
        self._bus.publish(ConfigChanged(patch))
        return self.get_config()

    def _snippet_patch_changes_list(self, patch: dict[str, Any]) -> bool:
        if "snippets" not in patch:
            return False
        incoming = patch.get("snippets")
        current = [(entry.spoken, entry.expansion) for entry in (self._config.snippets or ())]
        if incoming is None:
            return bool(current) or self._config.snippets is not None
        if not isinstance(incoming, dict) or "items" not in incoming:
            return False
        incoming_pairs = []
        for item in incoming.get("items") or ():
            if not isinstance(item, dict):
                continue
            spoken = str(item.get("spoken") or "").strip()
            if not spoken:
                continue
            incoming_pairs.append((spoken, str(item.get("expansion") or "")))
        return incoming_pairs != current

    def _dictionary_patch_changes_list(self, patch: dict[str, Any]) -> bool:
        if "dictionary" not in patch:
            return False
        incoming = patch.get("dictionary")
        current = [(entry.spoken, entry.written) for entry in (self._config.dictionary or ())]
        if incoming is None:
            return bool(current)
        if not isinstance(incoming, dict) or "terms" not in incoming:
            return False
        incoming_pairs = []
        for item in incoming.get("terms") or ():
            if not isinstance(item, dict):
                continue
            spoken = str(item.get("spoken") or "").strip()
            if not spoken:
                continue
            incoming_pairs.append((spoken, str(item.get("written") or spoken)))
        return incoming_pairs != current

    def _plan_snippet_import(self, payload: Any) -> tuple[tuple, dict[str, Any]]:
        from dcent_voice.config import load_snippet_import, plan_snippet_import

        incoming, parse_stats = load_snippet_import(payload)
        return plan_snippet_import(
            self._config.snippets,
            incoming,
            dictionary=self._config.dictionary,
            skipped_empty=parse_stats["skipped_empty"],
            skipped_malformed=parse_stats.get("skipped_malformed", 0),
            skipped_overflow=parse_stats.get("skipped_overflow", 0),
            read=parse_stats["read"],
            skip_changes=parse_stats.get("skip_changes") or (),
            entry_indexes=parse_stats.get("entry_indexes"),
        )

    def preview_snippets(self, payload: Any) -> dict[str, Any]:
        """Count a local snippet import without writing config. Includes the cue list."""
        _merged, stats = self._plan_snippet_import(payload)
        snapshot = self.get_config()
        snapshot["snippet_import"] = stats
        return snapshot

    def import_snippets(self, payload: Any) -> dict[str, Any]:
        """Merge a local JSON snippet list into saved config. Not a cloud sync."""
        merged, stats = self._plan_snippet_import(payload)
        if stats["applied"] == 0:
            snapshot = self.get_config()
            snapshot["snippet_import"] = stats
            return snapshot
        previous = self._config.snippets
        items = [_snippet_row_payload(entry) for entry in merged]
        snapshot = self.set_config({"snippets": {"items": items}}, clear_snippet_undo=False)
        self._stash_snippet_undo(previous)
        snapshot["snippet_import"] = stats
        snapshot["snippet_undo"] = True
        return snapshot

    def undo_snippet_import(self) -> dict[str, Any]:
        """Restore snippets from before the last applied import."""
        if not self._snippet_import_undo:
            self._load_snippet_undo_stash()
        if not self._snippet_import_undo:
            raise ConfigError("Nothing to undo.")
        previous = self._snippet_import_undo.pop()
        if previous is None:
            self._restore_absent_snippets(clear_snippet_undo=False)
        else:
            items = [_snippet_row_payload(entry) for entry in previous]
            self.set_config({"snippets": {"items": items}}, clear_snippet_undo=False)
        self._persist_snippet_undo()
        return self.get_config()

    def _snippet_undo_path(self) -> Path:
        target = self._config.source_path or default_config_path()
        return target.with_name(f"{target.name}.snippet-undo.json")

    def _stash_snippet_undo(self, previous: Any) -> None:
        self._snippet_import_undo.append(previous)
        self._persist_snippet_undo()

    def _snippet_undo_frame(self, previous: Any) -> dict[str, Any]:
        if previous is None:
            return {"absent": True}
        return {"items": [_snippet_row_payload(entry) for entry in previous]}

    def _parse_snippet_undo_frame(self, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return _MISSING_UNDO_FRAME
        if raw.get("absent"):
            return None
        items = raw.get("items")
        if not isinstance(items, list):
            return _MISSING_UNDO_FRAME
        from dcent_voice.config import SnippetEntry

        entries = []
        for item in items:
            if not isinstance(item, dict):
                continue
            spoken = str(item.get("spoken") or "").strip()
            if not spoken:
                continue
            entries.append(
                SnippetEntry(
                    spoken=spoken,
                    expansion=str(item.get("expansion") or ""),
                    starred=item.get("starred") is True,
                    added_at=str(item.get("added_at") or "").strip(),
                )
            )
        return tuple(entries)

    def _persist_snippet_undo(self) -> None:
        path = self._snippet_undo_path()
        if not self._snippet_import_undo:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return
        payload = {
            "stack": [self._snippet_undo_frame(frame) for frame in self._snippet_import_undo]
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _clear_snippet_undo_stash(self) -> None:
        self._snippet_import_undo = []
        with contextlib.suppress(OSError):
            self._snippet_undo_path().unlink(missing_ok=True)

    def _load_snippet_undo_stash(self) -> None:
        path = self._snippet_undo_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        frames: list[Any] = []
        if isinstance(payload.get("stack"), list):
            for raw in payload["stack"]:
                parsed = self._parse_snippet_undo_frame(raw)
                if parsed is _MISSING_UNDO_FRAME:
                    continue
                frames.append(parsed)
        elif payload.get("absent"):
            frames.append(None)
        else:
            parsed = self._parse_snippet_undo_frame(payload)
            if parsed is not _MISSING_UNDO_FRAME:
                frames.append(parsed)
        self._snippet_import_undo = frames

    def _restore_absent_snippets(self, *, clear_snippet_undo: bool = True) -> dict[str, Any]:
        target = self._config.source_path or default_config_path()
        with _config_lock(target):
            try:
                table = tomllib.loads(target.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                table = config_to_table(self._config)
            table.pop("snippets", None)
            parse_config(table, source_path=target)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(dump_config_toml(table))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()
            self._config = load_config(target, create=False)
            self._privacy = PrivacyMonitor.from_config(self._config)
        if clear_snippet_undo:
            self._clear_snippet_undo_stash()
        self._bus.publish(ConfigChanged({"snippets": None}))
        return self.get_config()

    def export_snippets(self, starred_only: bool = False, query: str = "") -> dict[str, Any]:
        from dcent_voice.config import snippet_export_payload

        return snippet_export_payload(
            self._config.snippets,
            starred_only=bool(starred_only),
            query=str(query or ""),
        )

    def export_dictionary(self, starred_only: bool = False, query: str = "") -> dict[str, Any]:
        from dcent_voice.config import dictionary_export_payload

        return {
            "csv": dictionary_export_payload(
                self._config.dictionary,
                starred_only=bool(starred_only),
                query=str(query or ""),
            )
        }

    def _plan_dictionary_import(self, payload: Any) -> tuple[tuple, dict[str, Any]]:
        from dcent_voice.config import (
            effective_snippets,
            load_dictionary_import,
            plan_dictionary_import,
        )

        incoming, parse_stats = load_dictionary_import(payload)
        return plan_dictionary_import(
            self._config.dictionary,
            incoming,
            snippets=effective_snippets(self._config.snippets),
            skipped_empty=parse_stats["skipped_empty"],
            skipped_malformed=parse_stats.get("skipped_malformed", 0),
            skipped_overflow=parse_stats.get("skipped_overflow", 0),
            read=parse_stats["read"],
            skip_changes=parse_stats.get("skip_changes") or (),
            entry_indexes=parse_stats.get("entry_indexes"),
        )

    def preview_dictionary(self, payload: Any) -> dict[str, Any]:
        """Count a local dictionary CSV import without writing config."""
        _merged, stats = self._plan_dictionary_import(payload)
        snapshot = self.get_config()
        snapshot["dictionary_import"] = stats
        return snapshot

    def import_dictionary(self, payload: Any) -> dict[str, Any]:
        """Merge a local CSV dictionary into saved config. Not a cloud sync."""
        merged, stats = self._plan_dictionary_import(payload)
        if stats["applied"] == 0:
            snapshot = self.get_config()
            snapshot["dictionary_import"] = stats
            return snapshot
        terms = [
            {
                "spoken": entry.spoken,
                "written": entry.written,
                **({"starred": True} if entry.starred else {}),
                **({"added_at": entry.added_at} if entry.added_at else {}),
            }
            for entry in merged
        ]
        previous = self._config.dictionary
        snapshot = self.set_config({"dictionary": {"terms": terms}}, clear_dictionary_undo=False)
        self._stash_dictionary_undo(previous)
        snapshot["dictionary_import"] = stats
        snapshot["dictionary_undo"] = True
        return snapshot

    def undo_dictionary_import(self) -> dict[str, Any]:
        """Restore dictionary from before the last applied import."""
        if not self._dictionary_import_undo:
            self._load_dictionary_undo_stash()
        if not self._dictionary_import_undo:
            raise ConfigError("Nothing to undo.")
        previous = self._dictionary_import_undo.pop()
        terms = [_dictionary_row_payload(entry) for entry in (previous or ())]
        self.set_config({"dictionary": {"terms": terms}}, clear_dictionary_undo=False)
        self._persist_dictionary_undo()
        return self.get_config()

    def _dictionary_undo_path(self) -> Path:
        target = self._config.source_path or default_config_path()
        return target.with_name(f"{target.name}.dictionary-undo.json")

    def _stash_dictionary_undo(self, previous: Any) -> None:
        self._dictionary_import_undo.append(previous)
        self._persist_dictionary_undo()

    def _dictionary_undo_frame(self, previous: Any) -> dict[str, Any]:
        return {"terms": [_dictionary_row_payload(entry) for entry in (previous or ())]}

    def _parse_dictionary_undo_frame(self, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return _MISSING_UNDO_FRAME
        terms = raw.get("terms")
        if not isinstance(terms, list):
            return _MISSING_UNDO_FRAME
        from dcent_voice.config import VocabEntry

        entries = []
        for item in terms:
            if not isinstance(item, dict):
                continue
            spoken = str(item.get("spoken") or "").strip()
            if not spoken:
                continue
            entries.append(
                VocabEntry(
                    spoken=spoken,
                    written=str(item.get("written") or spoken),
                    starred=item.get("starred") is True,
                    added_at=str(item.get("added_at") or "").strip(),
                )
            )
        return tuple(entries)

    def _persist_dictionary_undo(self) -> None:
        path = self._dictionary_undo_path()
        if not self._dictionary_import_undo:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return
        payload = {
            "stack": [self._dictionary_undo_frame(frame) for frame in self._dictionary_import_undo]
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _clear_dictionary_undo_stash(self) -> None:
        self._dictionary_import_undo = []
        with contextlib.suppress(OSError):
            self._dictionary_undo_path().unlink(missing_ok=True)

    def _load_dictionary_undo_stash(self) -> None:
        path = self._dictionary_undo_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        frames: list[Any] = []
        if isinstance(payload.get("stack"), list):
            for raw in payload["stack"]:
                parsed = self._parse_dictionary_undo_frame(raw)
                if parsed is _MISSING_UNDO_FRAME:
                    continue
                frames.append(parsed)
        else:
            parsed = self._parse_dictionary_undo_frame(payload)
            if parsed is not _MISSING_UNDO_FRAME:
                frames.append(parsed)
        self._dictionary_import_undo = frames

    def list_local_models(self) -> dict[str, Any]:
        return {
            "ollama": _get_json_names("http://127.0.0.1:11434/api/tags", "models", "name"),
            "lmstudio": _get_json_names("http://127.0.0.1:1234/v1/models", "data", "id"),
            "faster_whisper": scan_faster_whisper_cache(),
        }

    def local_cleanup_status(self) -> dict[str, Any]:
        """Inspect optional local-LLM cleanup. Never implies a cloud call."""
        from dcent_voice.config import LOCAL_LLM_PROVIDERS

        models = self.list_local_models()
        profile = self._config.current_profile
        provider = profile.llm.provider
        detected = "ollama" if models["ollama"] else "lmstudio" if models["lmstudio"] else None
        local = provider in LOCAL_LLM_PROVIDERS
        enabled = profile.cleanup_enabled is True and local and provider != "none"
        return {
            "enabled": enabled,
            "requested": profile.cleanup_enabled is True,
            "llm": profile.llm.raw,
            "provider": provider,
            "model": profile.llm.model,
            "local": local,
            "ollama": list(models["ollama"]),
            "lmstudio": list(models["lmstudio"]),
            "detected": detected,
            "fallback": "heuristics",
            "required": False,
            "cloud": False,
        }

    def set_local_cleanup(self, enabled: bool) -> dict[str, Any]:
        """Toggle optional local cleanup on the active profile. Never selects cloud."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        active = self._config.active_profile
        profile = self._config.current_profile
        patch: dict[str, Any] = {"profile": {active: {"cleanup_enabled": enabled}}}
        if enabled and profile.llm.provider not in {"ollama", "lmstudio"}:
            models = self.list_local_models()
            if models["ollama"]:
                patch["profile"][active]["llm"] = f"ollama:{models['ollama'][0]}"
            elif models["lmstudio"]:
                patch["profile"][active]["llm"] = f"lmstudio:{models['lmstudio'][0]}"
            else:
                patch["profile"][active]["llm"] = "none"
        snapshot = self.set_config(patch)
        snapshot["local_cleanup"] = self.local_cleanup_status()
        return snapshot

    def tts_model_status(self) -> dict[str, Any]:
        """Report the installed state of the pinned, optional local TTS assets."""

        return {
            "enabled": self._config.tts.enabled,
            "configured_backend": self._config.tts.backend,
            "backends": [
                {
                    "backend": backend,
                    "installed": backend_assets_present(backend, root=self._tts_model_root),
                    "runtime_ready": _tts_runtime_available(backend),
                    "license": assets[0].license,
                    "asset_count": len(assets),
                }
                for backend, assets in ASSETS_BY_BACKEND.items()
            ],
        }

    def install_tts_models(self, backend: str, accept_egress: bool = False) -> dict[str, Any]:
        """Install one trusted TTS backend after an explicit egress confirmation."""

        if accept_egress is not True:
            return {
                "ok": False,
                "detail": "Confirm model-download egress before installing local TTS assets.",
            }
        normalized = backend.strip().lower() if isinstance(backend, str) else ""
        if normalized not in ASSETS_BY_BACKEND:
            return {
                "ok": False,
                "detail": (
                    "Choose the Kokoro TTS backend. Piper is deferred pending compatible "
                    "voice licensing."
                ),
            }
        if not _tts_runtime_available(normalized):
            return {
                "ok": False,
                "detail": (
                    f"The {normalized} runtime is not installed. "
                    "This beta build does not include optional TTS runtimes."
                ),
            }

        assets = ASSETS_BY_BACKEND[normalized]
        with self._tts_install_lock:
            # The button click is the user's explicit consent to the listed,
            # pinned upstream downloads. Persist that consent before egress so
            # every later retry remains auditable in the same ledger.
            self._privacy.ledger.grant(
                MODEL_DOWNLOAD_KEY,
                payload_type="model",
                policy_url=assets[0].license_url,
            )
            try:
                paths = install_backend_assets(
                    normalized,
                    ledger=self._privacy.ledger,
                    egress_log=self._privacy.egress_log,
                    fetch=self._tts_fetch,
                    root=self._tts_model_root,
                )
            except (ChecksumError, ConsentRequired, OSError, httpx.HTTPError, ValueError) as exc:
                _LOGGER.warning("TTS model install failed for %s: %s", normalized, exc)
                return {"ok": False, "detail": str(exc)}

            # A complete, verified install is the only point where TTS becomes
            # enabled in config. The DVAP backend is created at app startup, so
            # callers must restart before it is advertised to connected clients.
            config = self.set_config({"tts": {"enabled": True, "backend": normalized}})
            self._announce_privacy()
        return {
            "ok": True,
            "backend": normalized,
            "files": [path.name for path in paths],
            "config": config,
            "restart_required": True,
        }

    def list_input_devices(self) -> list[dict[str, Any]]:
        try:
            import sounddevice as sd
        except ImportError:
            return []
        devices: list[dict[str, Any]] = []
        try:
            for index, info in enumerate(sd.query_devices()):
                if info.get("max_input_channels", 0) > 0:
                    devices.append({"index": index, "name": str(info.get("name", ""))})
        except Exception:
            return []
        return devices

    def set_input_device(self, index: int | None) -> dict[str, Any]:
        return self.set_config({"audio": {"input_device": index}})

    def setup_state(self) -> dict[str, Any]:
        """Everything the first-run wizard needs in one call."""
        models = self.list_local_models()
        hardware = self.hardware_status()
        primary_readiness = hardware.get("model_readiness")
        resolved = ASRSpec.parse(
            str(hardware.get("resolved_asr") or self._config.current_profile.asr.raw)
        )
        fallback_readiness = (
            faster_whisper_model_status("base") if resolved.provider == "parakeet" else None
        )
        has_asr = bool(
            (primary_readiness or {}).get("ready") or (fallback_readiness or {}).get("ready")
        )
        profiles = {
            name: {
                "asr": profile.asr.raw,
                "llm": profile.llm.raw,
                "cleanup_enabled": profile.cleanup_enabled,
            }
            for name, profile in self._config.profiles.items()
        }
        return {
            "version": __version__,
            "setup_complete": self._config.privacy.first_run_education_shown,
            # The wizard's first screen tells the user where the tray icon is;
            # that sentence differs per OS, so it must not be guessed from the
            # WebView user agent.
            "platform": sys.platform,
            "hotkey_mode": self._config.hotkeys.mode,
            "input_devices": self.list_input_devices(),
            "input_device": self._config.audio.input_device,
            "hotkeys": self._config.hotkeys.__dict__,
            "active_profile": self._config.active_profile,
            "active_profile_config": profiles[self._config.active_profile],
            "profiles": profiles,
            "has_local_asr_model": has_asr,
            "models": models,
            "hardware": hardware,
            "asr_readiness": {
                "runtime_downloads": False,
                "primary_provider": resolved.provider,
                "primary_model": resolved.model,
                "primary": primary_readiness,
                "fallback": fallback_readiness,
            },
        }

    def finish_setup(self) -> dict[str, Any]:
        return self.set_config({"privacy": {"first_run_education_shown": True}})

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "name": capability.name,
                "display_name": capability.display_name,
                "locality": capability.locality,
                "auth_modes": [mode.value for mode in capability.auth_modes],
                "supports_oauth": capability.supports_oauth,
                "supports_api_key": capability.supports_api_key,
                # Device-code sign-in needs a registered OAuth client; the UI
                # hides the "Sign in" button until one is configured.
                "device_login_available": bool(
                    OAUTH_CONFIGS.get(capability.name, {}).get("client_id")
                ),
                "policy_url": capability.policy_url,
                "docs_url": capability.docs_url,
                "data_note": capability.data_note,
                "consent_descriptors": [
                    descriptor.__dict__ for descriptor in capability.consent_descriptors
                ],
                "account": self.provider_account(capability.name),
            }
            for capability in list_provider_capabilities()
        ]

    def provider_account(self, name: str) -> dict[str, Any]:
        capability = get_provider_capability(name)
        if capability is None:
            return {"provider": name, "connected": False, "auth_mode": "unknown", "label": ""}
        if AuthMode.API_KEY not in capability.auth_modes and not capability.supports_oauth:
            running = _local_provider_running(name) if capability.locality == "local" else False
            return {
                "provider": name,
                "connected": AuthMode.NONE in capability.auth_modes and running,
                "auth_mode": capability.auth_modes[0].value,
                "label": "Running locally" if running else "Not running",
                "status": "ready" if running else "unavailable",
            }
        store = self._store()
        # An OAuth (account sign-in) connection takes precedence over a key.
        if capability.supports_oauth:
            oauth = OAuthAuth(store).status(name)
            if oauth.connected:
                return {
                    "provider": name,
                    "connected": True,
                    "auth_mode": oauth.auth_mode.value,
                    "label": oauth.label or "Signed in",
                }
        if AuthMode.API_KEY not in capability.auth_modes:
            return {
                "provider": name,
                "connected": False,
                "auth_mode": capability.auth_modes[0].value,
                "label": "Not connected",
                "status": "unavailable",
            }
        account = ApiKeyAuth(store).status(name)
        return {
            "provider": account.provider,
            "connected": account.connected,
            "auth_mode": account.auth_mode.value,
            "label": account.label,
        }

    def begin_device_login(self, name: str, grant_consent: bool = False) -> dict[str, Any]:
        oauth = OAUTH_CONFIGS.get(name.lower())
        if not oauth or not oauth.get("client_id"):
            return {
                "ok": False,
                "detail": "Account sign-in isn't set up for this provider yet — use an API key.",
            }
        try:
            grant = request_device_code(
                oauth["device_endpoint"],
                client_id=oauth["client_id"],
                scope=oauth.get("scope", ""),
                authorize_egress=lambda: self._authorize_credential_egress(
                    name,
                    grant_consent=grant_consent,
                ),
            )
        except ConsentRequired:
            return {
                "ok": False,
                "detail": "Confirm credential egress before starting provider sign-in.",
            }
        except OSError:
            return {
                "ok": False,
                "detail": "Could not record credential egress; sign-in was not started.",
            }
        except ValueError:
            return {
                "ok": False,
                "detail": "Provider sign-in configuration or response was invalid.",
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"Could not start sign-in: {type(exc).__name__}"}
        self._device_grants[name.lower()] = grant
        return {
            "ok": True,
            "user_code": grant.user_code,
            "verification_uri": grant.verification_uri_complete or grant.verification_uri,
            "interval": grant.interval,
        }

    def poll_device_login(self, name: str) -> dict[str, Any]:
        oauth = OAUTH_CONFIGS.get(name.lower())
        grant = self._device_grants.get(name.lower())
        if not oauth or grant is None:
            return {"status": "error", "detail": "No sign-in is in progress."}
        try:
            token = poll_device_token(
                oauth["token_endpoint"],
                client_id=oauth["client_id"],
                device_code=grant.device_code,
                authorize_egress=lambda: self._authorize_credential_egress(name),
            )
        except ConsentRequired:
            self._device_grants.pop(name.lower(), None)
            return {"status": "error", "detail": "Credential egress consent was revoked."}
        except OSError:
            return {
                "status": "error",
                "detail": "Could not record credential egress; no sign-in request was sent.",
            }
        except ValueError:
            return {
                "status": "error",
                "detail": "Provider sign-in configuration or response was invalid.",
            }
        except httpx.HTTPStatusError as exc:
            error = ""
            with contextlib.suppress(Exception):
                error = str(exc.response.json().get("error", ""))
            if error in {"authorization_pending", "slow_down"}:
                return {"status": "pending"}
            return {"status": "error", "detail": error or "Sign-in was declined."}
        except httpx.HTTPError as exc:
            return {"status": "error", "detail": f"Network error: {type(exc).__name__}"}
        try:
            OAuthAuth(self._store()).connect_token(
                name, token, label="Signed in", mode=AuthMode.DEVICE_CODE
            )
        except ValueError:
            # Some providers return errors under HTTP 200; an empty access_token
            # must surface as a sign-in failure, not crash the bridge call.
            self._device_grants.pop(name.lower(), None)
            return {"status": "error", "detail": "The provider returned no access token."}
        capability = get_provider_capability(name)
        if capability is not None and capability.locality == "cloud":
            self._grant_provider_consents(capability.name)
        self._device_grants.pop(name.lower(), None)
        return {"status": "connected"}

    def connect_provider(
        self,
        name: str,
        api_key: str = "",
        label: str = "",
        grant_consent: bool = False,
    ) -> dict[str, Any]:
        capability = get_provider_capability(name)
        if capability is None:
            raise ValueError(f"Unknown provider: {name}")
        if AuthMode.API_KEY not in capability.auth_modes:
            return {**self.provider_account(name), "ok": True, "detail": ""}
        # Verify the key against the provider before saving so the user learns it
        # is wrong here, not on the first failed dictation. Only a *rejected* key
        # is refused — if the provider is unreachable (offline, 5xx) the key is
        # saved unverified rather than thrown away over a network blip.
        try:
            result = validate_api_key(
                name,
                api_key,
                authorize_egress=lambda: self._authorize_credential_egress(
                    name,
                    grant_consent=grant_consent,
                ),
            )
        except ConsentRequired:
            return {
                "provider": name,
                "connected": False,
                "auth_mode": AuthMode.API_KEY.value,
                "label": "",
                "ok": False,
                "detail": "Confirm credential egress before verifying this key.",
            }
        except OSError:
            return {
                "provider": name,
                "connected": False,
                "auth_mode": AuthMode.API_KEY.value,
                "label": "",
                "ok": False,
                "detail": "Could not record credential egress; the key was not sent.",
            }
        if not result.ok and result.reachable:
            return {
                "provider": name,
                "connected": False,
                "auth_mode": AuthMode.API_KEY.value,
                "label": "",
                "ok": False,
                "detail": result.detail,
            }
        verified = result.ok
        default_label = "API key verified" if verified else "API key (unverified)"
        account = ApiKeyAuth(self._store()).connect(name, api_key, label=label or default_label)
        if grant_consent is True and capability.locality == "cloud":
            self._grant_provider_consents(capability.name)
        return {
            "provider": account.provider,
            "connected": account.connected,
            "auth_mode": account.auth_mode.value,
            "label": account.label,
            "ok": True,
            "detail": result.detail
            if verified
            else f"{result.detail} Saved unverified — it will be used as-is.",
        }

    def disconnect_provider(self, name: str) -> dict[str, Any]:
        capability = get_provider_capability(name)
        if capability is None:
            return {
                **self.provider_account(name),
                "ok": False,
                "detail": "Unknown provider; no saved credentials were changed.",
            }
        store = self._store()
        cleanup_failed = False
        if capability.supports_oauth:
            try:
                OAuthAuth(store).disconnect(name)
            except Exception:
                cleanup_failed = True
        if AuthMode.API_KEY in capability.auth_modes:
            try:
                ApiKeyAuth(store).disconnect(name)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            # Absence could not be verified, so retain the connected state in
            # the UI. In particular, a locked keychain must never look like it
            # deleted credentials that become readable again after unlock.
            return {
                "provider": name,
                "connected": True,
                "auth_mode": capability.auth_modes[0].value,
                "label": "Credential removal incomplete",
                "status": "error",
                "ok": False,
                "detail": (
                    "Could not remove every saved credential. Unlock or start the "
                    "operating-system credential store, then try again."
                ),
            }
        account = self.provider_account(name)
        if account["connected"]:
            return {
                **account,
                "ok": False,
                "status": "error",
                "detail": "A saved credential remains; the provider was not disconnected.",
            }
        return {**account, "ok": True, "detail": f"Disconnected {name}."}

    def open_url(self, url: str) -> bool:
        # Open provider docs/policy links in the system browser; pywebview would
        # otherwise trap target=_blank. Only http(s) so the bridge can't be used
        # to launch arbitrary local handlers.
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return False
        import webbrowser

        with contextlib.suppress(Exception):
            return bool(webbrowser.open(url))
        return False

    def check_for_update(self) -> dict[str, Any]:
        from dcent_voice.util.updates import check_for_update as _check

        info = _check(__version__)
        return {
            "ok": info.ok,
            "available": info.available,
            "current": info.current,
            "latest": info.latest,
            "url": info.url,
        }

    def get_privacy_status(self) -> dict[str, Any]:
        return self._privacy.snapshot()

    def get_recovery_status(self) -> dict[str, Any]:
        return self._recovery.snapshot()

    def set_recovery_policy(
        self, enabled: bool, max_items: int = 10, max_age_hours: int = 24
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("Recovery opt-in must be a boolean.")
        self.set_config(
            {
                "recovery": {
                    "enabled": enabled,
                    "max_items": max_items,
                    "max_age_hours": max_age_hours,
                }
            }
        )
        return self._recovery.snapshot()

    def delete_recovery_entry(self, entry_id: str) -> dict[str, Any]:
        self._recovery.delete(str(entry_id or ""))
        return self._recovery.snapshot()

    def clear_recovery_entries(self) -> dict[str, Any]:
        self._recovery.clear()
        return self._recovery.snapshot()

    def copy_recovery_entry(self, entry_id: str) -> dict[str, Any]:
        """Copy only after an explicit Settings click; never runs in routine tests."""

        entry = self._recovery.get(str(entry_id or ""))
        if entry is None:
            return {"ok": False, "detail": "That recovery item is no longer available."}
        try:
            from dcent_voice.inject.clipboard import set_clipboard_text

            set_clipboard_text(entry.text)
        except Exception as exc:
            _LOGGER.warning("explicit recovery copy failed: %s", type(exc).__name__)
            return {"ok": False, "detail": "Could not copy the recovery item."}
        return {"ok": True, "detail": "Failed dictation copied to the clipboard."}

    def get_egress_log(self, limit: int = 100) -> list[dict[str, Any]]:
        return [entry.__dict__ for entry in self._privacy.egress_log.tail(limit)]

    def get_first_run_education(self) -> dict[str, Any]:
        return {
            "shown": self._config.privacy.first_run_education_shown,
            "copy": FIRST_RUN_EDUCATION,
        }

    def mark_first_run_education_shown(self) -> dict[str, Any]:
        return self.set_config({"privacy": {"first_run_education_shown": True}})

    def get_consent_ledger(self) -> list[dict[str, Any]]:
        return [entry.__dict__ for entry in self._privacy.ledger.entries().values()]

    def grant_consent(self, provider_key: str) -> dict[str, Any]:
        provider = next(
            (item for item in self._privacy.providers if item.key == provider_key), None
        )
        if provider is not None:
            capability = get_provider_capability(provider.provider)
            self._privacy.ledger.grant(
                provider_key,
                payload_type=provider.payload_type,
                policy_url=capability.policy_url if capability else "",
            )
            self._announce_privacy(
                consent_state="granted",
                reason="user_granted_cloud_consent",
            )
            return self.get_privacy_status()

        capability = self._capability_for_provider_key(provider_key)
        descriptor = None
        if capability is not None:
            descriptor = next(
                (
                    item
                    for item in capability.consent_descriptors
                    if item.provider_key == provider_key
                ),
                None,
            )
        if descriptor is None:
            raise ValueError(f"Unknown provider key: {provider_key}")
        self._privacy.ledger.grant(
            provider_key,
            payload_type=descriptor.payload_type,
            policy_url=capability.policy_url,
        )
        self._announce_privacy(
            consent_state="granted",
            reason="user_granted_cloud_consent",
        )
        return self.get_privacy_status()

    def revoke_consent(self, provider_key: str) -> dict[str, Any]:
        self._privacy.ledger.revoke(provider_key)
        self._announce_privacy(
            consent_state="revoked",
            reason="user_revoked_cloud_consent",
        )
        return self.get_privacy_status()

    def _announce_privacy(self, *, consent_state: str = "", reason: str = "") -> None:
        # A consent grant/revoke changes the observed data-flow class. Publishing
        # PrivacyChanged refreshes the tray/overlay and drives the DVAP
        # module.sovereignty push (service.dvap.module_sovereignty_for_event).
        self._bus.publish(
            PrivacyChanged(
                self._privacy.status.value,
                consent_state=consent_state,
                reason=reason,
                missing_providers=self._privacy.missing_consents(),
            )
        )

    def test_microphone(self, duration_s: float = 1.0) -> dict[str, Any]:
        duration_s = max(0.1, min(float(duration_s), 3.0))
        try:
            import numpy as np
            import sounddevice as sd

            from dcent_voice.audio.levels import AmplitudeMeter
        except ImportError as exc:
            return {"ok": False, "error": f"missing_dependency:{exc.name}", "peak": 0.0}

        meter = AmplitudeMeter()
        peak = 0.0

        def callback(indata, frames, time_info, status) -> None:
            nonlocal peak
            samples = np.asarray(indata, dtype=np.float32)
            mono = samples[:, 0] if samples.ndim == 2 else samples
            level = meter.update(mono)
            peak = max(peak, level)

        try:
            with sd.InputStream(
                device=self._config.audio.input_device,
                samplerate=16000,
                channels=1,
                dtype="float32",
                callback=callback,
            ):
                time.sleep(duration_s)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "peak": peak}
        return {"ok": True, "peak": peak, "level": meter.read()}

    def record_hotkey(self, timeout_s: float = 5.0) -> dict[str, Any]:
        try:
            from pynput import keyboard
        except ImportError:
            return {"ok": False, "error": "missing_dependency:pynput", "chord": ""}

        pressed: set[str] = set()
        observed: set[str] = set()
        done = False

        def on_press(key) -> None:
            name = normalize_key(key)
            if name:
                pressed.add(name)
                observed.add(name)

        def on_release(key):
            nonlocal done
            name = normalize_key(key)
            pressed.discard(name)
            if observed and not pressed:
                done = True
                return False
            return None

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        deadline = time.monotonic() + max(1.0, min(float(timeout_s), 15.0))
        try:
            while not done and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            listener.stop()
        if not observed:
            return {"ok": False, "error": "timeout", "chord": ""}
        return {"ok": True, "chord": "+".join(sorted(observed))}

    def run_benchmark(self) -> dict[str, Any]:
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.extend(["-m", "dcent_voice"])
        if self._config.source_path is not None:
            command.extend(["--config", str(self._config.source_path)])
        command.append("benchmark")
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
            )
            stderr = (
                exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            )
            return {
                "returncode": 124,
                "stdout": stdout or "",
                "stderr": (stderr or "") + "\nBenchmark timed out after 120 seconds.",
            }
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def _store(self) -> CredentialStore:
        if self._credential_store is None:
            self._credential_store = CredentialStore()
        return self._credential_store

    def _authorize_credential_egress(
        self,
        provider_name: str,
        *,
        grant_consent: bool = False,
    ) -> None:
        """Bind auth consent and append metadata before a credential request."""

        capability = get_provider_capability(provider_name)
        key = credential_consent_key(provider_name)
        if grant_consent is True:
            self._privacy.ledger.grant(
                key,
                payload_type=CREDENTIAL_EGRESS_PAYLOAD,
                policy_url=capability.policy_url if capability else "",
            )
        if not self._privacy.ledger.has_consent(
            key,
            payload_type=CREDENTIAL_EGRESS_PAYLOAD,
        ):
            raise ConsentRequired((key,))
        # Zero deliberately avoids leaking credential/token length. This is an
        # attempted-auth audit record, written before the request reaches wire.
        self._privacy.egress_log.record(
            key,
            payload_type=CREDENTIAL_EGRESS_PAYLOAD,
            byte_count=0,
        )

    def _grant_provider_consents(self, provider_name: str) -> None:
        capability = get_provider_capability(provider_name)
        if capability is None:
            return
        consent_targets = [
            (descriptor.provider_key, descriptor.payload_type)
            for descriptor in capability.consent_descriptors
        ]
        if not consent_targets:
            consent_targets = [
                (provider.key, provider.payload_type)
                for provider in self._privacy.providers
                if provider.provider == capability.name
            ]
        for provider_key, payload_type in consent_targets:
            self._privacy.ledger.grant(
                provider_key,
                payload_type=payload_type,
                policy_url=capability.policy_url,
            )

    def _capability_for_provider_key(self, provider_key: str):
        return next(
            (
                capability
                for capability in list_provider_capabilities()
                if any(
                    descriptor.provider_key == provider_key
                    for descriptor in capability.consent_descriptors
                )
            ),
            None,
        )


class _WindowLifecycleController:
    """Thread-safe lifecycle shared by the Settings and setup windows.

    pywebview retains a ``Window`` Python object after its native window closes.
    Treating a non-None reference as proof of liveness therefore leaves tray
    actions calling ``show`` on a dead native window.  This helper makes the
    native ``closed`` event authoritative and serializes creation/replacement.
    """

    def _init_window_lifecycle(self, on_window_requested: Callable[[], None] | None) -> None:
        self.window: Any | None = None
        self._window_lock = threading.RLock()
        self._on_window_requested = on_window_requested
        # Set by the application when it needs to react to the native window
        # closing (the first-run wizard persists its "shown" flag there).
        self.on_closed: Callable[[], None] | None = None

    def _open_window(self, webview: Any, title: str, kwargs: dict[str, Any]) -> bool:
        # The application may use this synchronous callback as a two-phase GUI
        # handshake: request/start pywebview's master window, then return only
        # when it is safe for this user-facing window to be created.  Never call
        # external code while holding the lifecycle lock.
        if self._on_window_requested is not None:
            try:
                self._on_window_requested()
            except Exception:
                _LOGGER.exception("%s window host request failed", title)
                return False

        with self._window_lock:
            existing = self.window
            if existing is not None and not self._window_is_closed(existing):
                # A second tray click can arrive before webview.start.  Calling
                # show/restore at that point waits on pywebview's `shown` event
                # for many seconds.  The first request is already pending, so
                # return without touching the not-yet-native window.
                if not self._window_has_been_shown(existing):
                    return True
                if self._reveal_window(existing):
                    return True
                # A native backend can disappear without delivering `closed`.
                # A failed show is sufficient evidence to recreate it.
                if self.window is existing:
                    self.window = None
            elif existing is not None and self.window is existing:
                self.window = None

            try:
                window = webview.create_window(title, **kwargs)
            except Exception:
                _LOGGER.exception("failed to create %s window", title)
                return False
            if window is None:
                _LOGGER.error("pywebview returned no %s window", title)
                return False

            self.window = window
            self._bind_closed_event(window)
            # Accommodate unusual/test backends that close synchronously while
            # the handler is being attached.
            if self._window_is_closed(window):
                if self.window is window:
                    self.window = None
                return False
            return True

    def close(self) -> None:
        """Destroy the current native window, if any, without lifecycle races."""

        # Clear ownership before destroy: pywebview can synchronously dispatch
        # `closed`, whose callback also takes the lock.  Destroy outside the lock
        # avoids deadlocks even on backends with synchronous event delivery.
        with self._window_lock:
            window = self.window
            self.window = None
        if window is None or self._window_is_closed(window):
            return
        try:
            window.destroy()
        except Exception:
            _LOGGER.exception("failed to destroy pywebview window")

    def _bind_closed_event(self, window: Any) -> None:
        closed = getattr(getattr(window, "events", None), "closed", None)
        if closed is None:
            _LOGGER.warning("pywebview window has no closed lifecycle event")
            return

        def _on_closed(*_args: Any, **_kwargs: Any) -> None:
            with self._window_lock:
                # A delayed event from an old native window must never clear a
                # replacement created by a newer tray action.
                if self.window is window:
                    self.window = None
            hook = getattr(self, "on_closed", None)
            if hook is not None:
                try:
                    hook()
                except Exception:
                    _LOGGER.exception("window closed hook failed")

        try:
            closed += _on_closed
        except Exception:
            _LOGGER.exception("failed to subscribe to pywebview closed event")

    @staticmethod
    def _window_is_closed(window: Any) -> bool:
        closed = getattr(getattr(window, "events", None), "closed", None)
        is_set = getattr(closed, "is_set", None)
        if not callable(is_set):
            return False
        try:
            return is_set() is True
        except Exception:
            return False

    @staticmethod
    def _window_has_been_shown(window: Any) -> bool:
        shown = getattr(getattr(window, "events", None), "shown", None)
        is_set = getattr(shown, "is_set", None)
        if not callable(is_set):
            # Older/fake backends without a shown event have historically been
            # safe to call directly.
            return True
        try:
            return is_set() is not False
        except Exception:
            return True

    @staticmethod
    def _reveal_window(window: Any) -> bool:
        restore = getattr(window, "restore", None)
        if callable(restore):
            with contextlib.suppress(Exception):
                restore()

        show = getattr(window, "show", None)
        if not callable(show):
            return False
        try:
            show()
        except Exception:
            _LOGGER.exception("failed to show existing pywebview window")
            return False

        # pywebview has no cross-platform activate method today, but some
        # backends/future versions expose one.  Native WinForms offers Activate.
        candidates = (window, getattr(window, "native", None))
        for candidate in candidates:
            for name in ("activate", "Activate", "bring_to_front", "BringToFront"):
                activate = getattr(candidate, name, None)
                if callable(activate):
                    with contextlib.suppress(Exception):
                        activate()
                    return True
        return True


class SettingsController(_WindowLifecycleController):
    """Owns the settings window and its browser bridge."""

    def __init__(
        self,
        *,
        config: AppConfig,
        bus: EventBus,
        privacy: PrivacyMonitor,
        credential_store: CredentialStore | None = None,
        assets_dir: Path | None = None,
        on_window_requested: Callable[[], None] | None = None,
        personalization: Any = None,
        recovery_store: RecoveryStore | None = None,
    ) -> None:
        self.api = SettingsApi(
            config=config,
            bus=bus,
            privacy=privacy,
            credential_store=credential_store,
            personalization=personalization,
            recovery_store=recovery_store,
        )
        self.assets_dir = assets_dir or Path(__file__).resolve().parent / "web" / "settings"
        self._init_window_lifecycle(on_window_requested)

    def open(self) -> bool:
        if sys.platform == "win32":
            from dcent_voice.ui.webview_runtime import windows_webview2_runtime_present

            if not windows_webview2_runtime_present():
                _LOGGER.warning("WebView2 runtime is not registered; Settings cannot open")
                return False
        try:
            import webview
        except ImportError:
            return False
        kwargs: dict[str, Any] = {
            "url": (self.assets_dir / "index.html").as_uri(),
            "width": 1100,
            "height": 740,
            "min_size": (860, 560),
            "js_api": self.api,
        }
        return self._open_window(webview, "DCENT_Voice Settings", kwargs)


class WizardApi:
    """js_api for the first-run wizard: a thin subset of SettingsApi plus close."""

    def __init__(self, settings_api: SettingsApi, on_close: Any) -> None:
        self._settings = settings_api
        self._on_close = on_close

    def setup_state(self) -> dict[str, Any]:
        return self._settings.setup_state()

    def set_input_device(self, index: int | None) -> dict[str, Any]:
        return self._settings.set_input_device(index)

    def test_microphone(self, duration_s: float = 1.5) -> dict[str, Any]:
        return self._settings.test_microphone(duration_s)

    def record_hotkey(self, timeout_s: float = 5.0) -> dict[str, Any]:
        return self._settings.record_hotkey(timeout_s)

    def finish_setup(self) -> dict[str, Any]:
        return self._settings.finish_setup()

    def close_setup(self) -> None:
        self._on_close()


class WizardController(_WindowLifecycleController):
    """Owns the first-run setup wizard and its browser bridge."""

    def __init__(
        self,
        *,
        config: AppConfig,
        bus: EventBus,
        privacy: PrivacyMonitor,
        credential_store: CredentialStore | None = None,
        assets_dir: Path | None = None,
        on_window_requested: Callable[[], None] | None = None,
        recovery_store: RecoveryStore | None = None,
    ) -> None:
        self.settings_api = SettingsApi(
            config=config,
            bus=bus,
            privacy=privacy,
            credential_store=credential_store,
            recovery_store=recovery_store,
        )
        self.assets_dir = assets_dir or Path(__file__).resolve().parent / "web" / "wizard"
        self._init_window_lifecycle(on_window_requested)

    def open(self) -> bool:
        if sys.platform == "win32":
            from dcent_voice.ui.webview_runtime import windows_webview2_runtime_present

            if not windows_webview2_runtime_present():
                _LOGGER.warning("WebView2 runtime is not registered; setup wizard cannot open")
                return False
        try:
            import webview
        except ImportError:
            return False
        kwargs: dict[str, Any] = {
            "url": (self.assets_dir / "index.html").as_uri(),
            "width": 760,
            "height": 720,
            "min_size": (680, 560),
            "js_api": WizardApi(self.settings_api, self._close),
        }
        return self._open_window(webview, "Welcome to DCENT_Voice", kwargs)

    def _close(self) -> None:
        self.close()


def config_snapshot(config: AppConfig) -> dict[str, Any]:
    return {
        "version": __version__,
        "source_path": str(config.source_path or default_config_path()),
        "active_profile": config.active_profile,
        "language": config.language,
        "language_mode": config.language_mode,
        "language_choices": language_choices(),
        "cleanup_enabled": config.cleanup_enabled,
        "launch_at_startup": config.launch_at_startup,
        "idle_unload_s": config.idle_unload_s,
        "hotkeys": config.hotkeys.__dict__,
        "overlay": config.overlay.__dict__,
        "service": config.service.__dict__,
        "audio": {"input_device": config.audio.input_device},
        "injector": {
            "default": config.injector.default,
            "restore_clipboard": config.injector.restore_clipboard,
            "paste_delay_s": config.injector.paste_delay_s,
            "paste_min_delay_s": config.injector.paste_min_delay_s,
            "short_text_keystroke_chars": config.injector.short_text_keystroke_chars,
            "per_app": dict(config.injector.per_app),
        },
        "privacy": {
            "first_run_education_shown": config.privacy.first_run_education_shown,
            "consent_ledger_path": str(config.privacy.consent_ledger_path or ""),
            "egress_log_path": str(config.privacy.egress_log_path or ""),
        },
        "recovery": {
            "enabled": config.recovery.enabled is True,
            "max_items": config.recovery.max_items,
            "max_age_hours": config.recovery.max_age_hours,
        },
        "tts": config.tts.__dict__.copy(),
        "profiles": {
            name: {
                "asr": profile.asr.raw,
                "llm": profile.llm.raw,
                "cleanup_enabled": profile.cleanup_enabled,
                "language": profile.language,
            }
            for name, profile in config.profiles.items()
        },
        "dictionary": [entry.__dict__ for entry in config.dictionary],
        "snippets": [entry.__dict__ for entry in effective_snippets(config.snippets)],
        "dictation": {
            "local_polish": config.dictation.local_polish,
            "spoken_edits": config.dictation.spoken_edits,
            "developer_terms": config.dictation.developer_terms,
            "cleanup_level": config.dictation.cleanup_level,
        },
        "personalization": {
            "enabled": config.personalization.enabled is True,
            "learn": config.personalization.learn is True,
            # Consent-like opt-ins fail closed if a manually constructed
            # AppConfig bypasses the strict TOML parser.
            "prose_context": config.personalization.prose_context is True,
        },
        "style": {
            "default": config.style.default,
            "per_app": dict(config.style.per_app),
        },
        "lists": {
            "dictionary_sort": config.lists.dictionary_sort,
            "snippet_sort": config.lists.snippet_sort,
            "dictionary_starred_only": config.lists.dictionary_starred_only is True,
            "snippet_starred_only": config.lists.snippet_starred_only is True,
        },
    }


def config_to_table(config: AppConfig) -> dict[str, Any]:
    snapshot = config_snapshot(config)
    return {
        "active_profile": snapshot["active_profile"],
        "language": snapshot["language"],
        "language_mode": snapshot["language_mode"],
        "cleanup_enabled": snapshot["cleanup_enabled"],
        "launch_at_startup": snapshot["launch_at_startup"],
        "idle_unload_s": snapshot["idle_unload_s"],
        "hotkeys": deepcopy(snapshot["hotkeys"]),
        "overlay": deepcopy(snapshot["overlay"]),
        "service": deepcopy(snapshot["service"]),
        "audio": deepcopy(snapshot["audio"]),
        "injector": deepcopy(snapshot["injector"]),
        "privacy": deepcopy(snapshot["privacy"]),
        "recovery": deepcopy(snapshot["recovery"]),
        "tts": deepcopy(snapshot["tts"]),
        "profile": deepcopy(snapshot["profiles"]),
        "dictionary": {"terms": deepcopy(snapshot["dictionary"])},
        "snippets": {"items": deepcopy(snapshot["snippets"])},
        "dictation": deepcopy(snapshot["dictation"]),
        "personalization": deepcopy(snapshot["personalization"]),
        "style": deepcopy(snapshot["style"]),
        "lists": deepcopy(snapshot["lists"]),
    }


def deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        # App override maps are saved as a full replacement from Settings rows.
        # Merging would leave deleted process keys behind.
        if key == "per_app" and isinstance(value, dict):
            target[key] = dict(value)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = value


def dump_config_toml(table: dict[str, Any]) -> str:
    """Serialize a full config table to TOML without dropping anything.

    Generic over sections and keys (unlike the old fixed-section writer, which
    silently discarded [tts], config_version, and audio limits on every save).
    Comments are not preserved — tomllib does not expose them — but every key
    round-trips. None values are omitted so optional keys fall back to their
    defaults on reload.
    """
    lines: list[str] = []
    scalars = [
        (key, value)
        for key, value in table.items()
        if value is not None and not isinstance(value, dict) and not _is_table_array(value)
    ]
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    if scalars:
        lines.append("")
    for key, value in table.items():
        if isinstance(value, dict):
            _dump_toml_section(_toml_key(key), value, lines)
        elif _is_table_array(value):
            _dump_toml_table_array(_toml_key(key), value, lines)
    return "\n".join(lines).rstrip() + "\n"


def _dump_toml_section(name: str, table: dict[str, Any], lines: list[str]) -> None:
    scalars = [
        (key, value)
        for key, value in table.items()
        if value is not None and not isinstance(value, dict) and not _is_table_array(value)
    ]
    subtables = [(key, value) for key, value in table.items() if isinstance(value, dict)]
    table_arrays = [(key, value) for key, value in table.items() if _is_table_array(value)]
    # A bare header is only needed when the section has direct keys (or is
    # entirely empty); [profile] before [profile.desktop] would be noise.
    if scalars or not (subtables or table_arrays):
        lines.append(f"[{name}]")
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")
    for key, value in subtables:
        _dump_toml_section(f"{name}.{_toml_key(key)}", value, lines)
    for key, value in table_arrays:
        _dump_toml_table_array(f"{name}.{_toml_key(key)}", value, lines)


def _dump_toml_table_array(name: str, items: list[dict[str, Any]], lines: list[str]) -> None:
    for item in items:
        lines.append(f"[[{name}]]")
        for key, value in item.items():
            if value is None:
                continue
            if key == "starred" and value is False:
                continue
            if key == "added_at" and not value:
                continue
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return json.dumps(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value))


def _get_json_names(url: str, list_key: str, name_key: str) -> list[str]:
    try:
        response = _local_http_get(url, timeout=1.5)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return []
    return [str(item.get(name_key)) for item in data.get(list_key, []) if item.get(name_key)]


def _local_provider_running(name: str) -> bool:
    urls = {
        "ollama": "http://127.0.0.1:11434/api/tags",
        "lmstudio": "http://127.0.0.1:1234/v1/models",
    }
    url = urls.get(name)
    if url is None:
        return False
    try:
        response = _local_http_get(url, timeout=0.4)
        return 200 <= response.status_code < 300
    except httpx.HTTPError:
        return False


def _local_http_get(url: str, *, timeout: float) -> httpx.Response:
    """Probe a fixed local endpoint without ambient proxies or redirects."""

    with httpx.Client(
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        return client.get(url)


def scan_faster_whisper_cache() -> list[str]:
    canonical_to_alias = {model_id.lower(): alias for alias, model_id in MODEL_ALIASES.items()}
    found: set[str] = set()

    installed_root = faster_whisper_root()
    if installed_root.exists():
        for path in installed_root.iterdir():
            if not valid_faster_whisper_snapshot(path):
                continue
            model_id = path.name.replace("--", "/")
            found.add(canonical_to_alias.get(model_id.lower(), model_id))

    cache = Path.home() / ".cache" / "huggingface" / "hub"
    if cache.exists():
        for path in cache.glob("models--Systran--*whisper-*"):
            if not _has_complete_huggingface_snapshot(path, cache):
                continue
            model_id = path.name.removeprefix("models--").replace("--", "/")
            found.add(canonical_to_alias.get(model_id.lower(), model_id))
    return sorted(found)


def _has_complete_huggingface_snapshot(repository: Path, cache_root: Path) -> bool:
    """Return whether a Hugging Face cache repository has usable model files."""
    snapshots = repository / "snapshots"
    try:
        candidates = tuple(path for path in snapshots.iterdir() if path.is_dir())
    except OSError:
        return False
    return any(_is_complete_huggingface_snapshot(path, cache_root) for path in candidates)


def _is_complete_huggingface_snapshot(snapshot: Path, cache_root: Path) -> bool:
    """Validate config and weights while allowing Hugging Face's internal symlinks."""
    config = snapshot / "config.json"
    weights = snapshot / "model.bin"
    try:
        config_target = config.resolve(strict=True)
        weights_target = weights.resolve(strict=True)
        config_target.relative_to(cache_root.resolve())
        weights_target.relative_to(cache_root.resolve())
        payload = json.loads(config_target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        weights_target.is_file() and weights_target.stat().st_size > 0 and isinstance(payload, dict)
    )
