# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from keyring.errors import KeyringLocked

from dcent_voice.auth.oauth import DeviceCodeGrant, OAuthToken
from dcent_voice.auth.store import CredentialStore
from dcent_voice.auth.validate import ValidationResult
from dcent_voice.config import ConfigError, load_config, load_dictionary_import
from dcent_voice.events import EventBus
from dcent_voice.personalization import PersonalizationStore
from dcent_voice.privacy import PrivacyMonitor
from dcent_voice.tts.assets import MODEL_DOWNLOAD_KEY, TtsModelAsset
from dcent_voice.ui.settings import SettingsApi, config_snapshot, scan_faster_whisper_cache


def _stub_key_validation(monkeypatch) -> None:
    # connect_provider now pings the provider to verify the key; keep unit tests
    # offline by treating any key as valid.
    monkeypatch.setattr(
        "dcent_voice.ui.settings.validate_api_key",
        lambda *args, **kwargs: ValidationResult(True, "stubbed"),
    )


class FakeKeyring:
    def __init__(self) -> None:
        self.values = {}

    def get_password(self, service_name: str, username: str):
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


class LockableKeyring(FakeKeyring):
    def __init__(self) -> None:
        super().__init__()
        self.locked = False

    def get_password(self, service_name: str, username: str):
        if self.locked:
            raise KeyringLocked("credential store locked")
        return super().get_password(service_name, username)

    def delete_password(self, service_name: str, username: str) -> None:
        if self.locked:
            raise KeyringLocked("credential store locked")
        super().delete_password(service_name, username)


def _recording_get_server(
    *, status: int = 502, location: str | None = None
) -> tuple[ThreadingHTTPServer, threading.Thread, list[str]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(status)
            if location is not None:
                self.send_header("location", location)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"models": [{"name": "stolen"}]}')

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests


def test_config_snapshot_contains_profiles() -> None:
    config = load_config(Path("config.example.toml"), create=False)

    snapshot = config_snapshot(config)

    assert "desktop" in snapshot["profiles"]
    assert snapshot["profiles"]["tiny"]["llm"] == "none"
    assert snapshot["tts"]["backend"] == "kokoro"
    assert snapshot["idle_unload_s"] == 600.0
    spoken = [row["spoken"] for row in snapshot["snippets"]]
    assert spoken == ["my email", "my calendar", "my signature"]
    assert all(row["expansion"] == "" for row in snapshot["snippets"])


def test_cleared_snippets_stay_empty_in_settings_snapshot(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )

    updated = api.set_config({"snippets": {"items": []}})
    reloaded = load_config(config_path, create=False)

    assert updated["snippets"] == []
    assert reloaded.snippets == ()
    assert "items = []" in config_path.read_text(encoding="utf-8")


def test_settings_list_prefs_persist_on_this_machine(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    updated = api.set_config({"lists": {"dictionary_sort": "starred", "snippet_sort": "za"}})
    assert updated["lists"] == {
        "dictionary_sort": "starred",
        "snippet_sort": "za",
        "dictionary_starred_only": False,
        "snippet_starred_only": False,
    }
    reloaded = load_config(config_path, create=False)
    assert reloaded.lists.dictionary_sort == "starred"
    recency = api.set_config(
        {
            "lists": {
                "dictionary_sort": "newest",
                "snippet_sort": "oldest",
                "dictionary_starred_only": True,
                "snippet_starred_only": True,
            }
        }
    )
    assert recency["lists"] == {
        "dictionary_sort": "newest",
        "snippet_sort": "oldest",
        "dictionary_starred_only": True,
        "snippet_starred_only": True,
    }
    again = load_config(config_path, create=False)
    assert again.lists.dictionary_starred_only is True
    assert again.lists.snippet_starred_only is True


def test_settings_import_and_export_snippets_are_local(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )

    empty = api.export_snippets()
    assert empty["items"] == []
    updated = api.import_snippets(
        {"items": [{"spoken": "my calendar", "expansion": "https://cal.example/me"}]}
    )
    spoken = [row["spoken"] for row in updated["snippets"]]
    assert spoken == ["my calendar"]
    exported = api.export_snippets()
    assert exported["items"][0]["expansion"] == "https://cal.example/me"
    reloaded = load_config(config_path, create=False)
    assert reloaded.snippets is not None
    assert reloaded.snippets[0].spoken == "my calendar"


def test_settings_import_keeps_first_in_file_duplicate(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    preview = api.preview_snippets(
        [
            {"spoken": "dupe", "expansion": "first"},
            {"spoken": "dupe", "expansion": "second"},
        ]
    )
    assert preview["snippet_import"]["changes"] == [
        {"spoken": "dupe", "expansion": "first", "action": "add"},
        {"spoken": "dupe", "expansion": "second", "action": "skip", "reason": "duplicate"},
    ]
    assert preview["snippet_import"]["applied"] == 1
    assert preview["snippet_import"]["skipped_duplicate"] == 1
    assert preview["snippet_import"]["skipped"] == 1
    updated = api.import_snippets(
        [
            {"spoken": "dupe", "expansion": "first"},
            {"spoken": "dupe", "expansion": "second"},
        ]
    )
    saved = [row for row in updated["snippets"] if row["spoken"] == "dupe"]
    assert len(saved) == 1
    assert saved[0]["expansion"] == "first"
    assert saved[0]["starred"] is False
    assert saved[0]["added_at"]


def test_settings_name_text_format_import_reports_stats(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = config_path.read_text(encoding="utf-8")
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )

    empty = api.import_snippets([{"title": "not a snippet"}])
    assert empty["snippet_import"]["applied"] == 0
    assert empty["snippet_import"]["skipped"] == 1
    assert empty["snippet_import"]["skipped_empty"] == 1
    assert empty["snippet_import"]["changes"] == [
        {"spoken": "", "expansion": "", "action": "skip", "reason": "empty"}
    ]
    assert config_path.read_text(encoding="utf-8") == before

    updated = api.import_snippets([{"name": "my address", "text": "123 Main Street"}])
    assert updated["snippet_import"]["added"] == 1
    assert updated["snippet_import"]["replaced"] == 0
    assert "my address" in [row["spoken"] for row in updated["snippets"]]

    again = api.import_snippets([{"name": "my address", "text": "456 Oak"}])
    assert again["snippet_import"]["replaced"] == 1
    assert again["snippet_import"]["added"] == 0
    assert again["snippet_import"]["skipped_existing"] == 0
    assert again["snippet_import"]["applied"] == 1
    assert again["snippet_import"]["changes"] == [
        {"spoken": "my address", "expansion": "456 Oak", "action": "replace"}
    ]
    assert "456 Oak" in [
        row["expansion"] for row in again["snippets"] if row["spoken"] == "my address"
    ]
    same = api.import_snippets([{"name": "my address", "text": "456 Oak"}])
    assert same["snippet_import"]["applied"] == 0
    assert same["snippet_import"]["skipped_existing"] == 1

    blocked = api.import_snippets([{"spoken": "d central", "expansion": "nope"}])
    assert blocked["snippet_import"]["applied"] == 0
    assert blocked["snippet_import"]["skipped_dictionary"] == 1
    assert blocked["snippet_import"]["changes"] == [
        {"spoken": "d central", "expansion": "nope", "action": "skip", "reason": "dictionary"}
    ]
    assert "d central" not in [row["spoken"].lower() for row in blocked["snippets"]]


def test_settings_import_dictionary_csv_skips_existing_and_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = config_path.read_text(encoding="utf-8")
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    preview = api.preview_dictionary("Flow\n")
    assert preview["dictionary_import"]["added"] == 1
    assert config_path.read_text(encoding="utf-8") == before
    updated = api.import_dictionary("Flow\nkeep me,newer\n")
    spoken = [row["spoken"] for row in updated["dictionary"]]
    assert "Flow" in spoken
    assert updated["dictionary_import"]["added"] >= 1
    again = api.import_dictionary("Flow,other\n")
    assert again["dictionary_import"]["applied"] == 1
    assert again["dictionary_import"]["replaced"] == 1
    assert again["dictionary_import"]["skipped_existing"] == 0
    assert again["dictionary_import"]["changes"] == [
        {
            "spoken": "Flow",
            "expansion": "other",
            "action": "replace",
        }
    ]
    flow = [row for row in again["dictionary"] if row["spoken"] == "Flow"]
    assert len(flow) == 1
    assert flow[0]["written"] == "other"
    assert flow[0]["starred"] is False
    assert flow[0]["added_at"]
    same = api.import_dictionary("Flow,other\n")
    assert same["dictionary_import"]["applied"] == 0
    assert same["dictionary_import"]["skipped_existing"] == 1
    flow = [row for row in same["dictionary"] if row["spoken"] == "Flow"]
    assert flow[0]["written"] == "other"
    starred = api.set_config(
        {"dictionary": {"terms": [{"spoken": "Flow", "written": "Flow", "starred": True}]}}
    )
    assert any(row["spoken"] == "Flow" and row["starred"] is True for row in starred["dictionary"])


def test_settings_export_dictionary_csv_is_local_and_does_not_write(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = config_path.read_text(encoding="utf-8")
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    exported = api.export_dictionary()
    assert "csv" in exported
    assert "d central" in exported["csv"].lower()
    assert config_path.read_text(encoding="utf-8") == before
    api.import_dictionary("ExportMe,Export Me\n")
    again = api.export_dictionary()
    assert "ExportMe" in again["csv"]
    incoming, _stats = load_dictionary_import(again["csv"])
    spoken = [entry.spoken for entry in incoming]
    assert "ExportMe" in spoken


def test_settings_export_dictionary_starred_only(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.import_dictionary("plain,Plain,false\nvip,VIP,true\n")
    full = api.export_dictionary()
    starred = api.export_dictionary(True)
    assert "plain" in full["csv"].lower()
    assert "vip" in starred["csv"].lower()
    assert "plain" not in starred["csv"].lower()
    visible = api.export_dictionary(False, "vip")
    assert "vip" in visible["csv"].lower()
    assert "plain" not in visible["csv"].lower()
    miss = api.export_dictionary(False, "nope")
    assert miss["csv"].strip() == ""


def test_settings_preview_snippets_does_not_write(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = config_path.read_text(encoding="utf-8")
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )

    preview = api.preview_snippets(
        {"items": [{"spoken": "my calendar", "expansion": "https://cal.example/me"}]}
    )
    assert preview["snippet_import"]["added"] == 1
    assert preview["snippet_import"]["applied"] == 1
    assert preview["snippet_import"]["changes"] == [
        {
            "spoken": "my calendar",
            "expansion": "https://cal.example/me",
            "action": "add",
        }
    ]
    assert config_path.read_text(encoding="utf-8") == before
    assert api.get_config()["snippets"] == preview["snippets"]


def test_settings_preview_rejects_over_cap_without_writing(tmp_path: Path) -> None:
    from dcent_voice.config import ConfigError

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = config_path.read_text(encoding="utf-8")
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    with pytest.raises(ConfigError, match="1,000 entries"):
        api.preview_snippets([{"spoken": f"cue {i}", "expansion": "ok"} for i in range(1001)])
    assert config_path.read_text(encoding="utf-8") == before


def test_settings_undo_snippet_import_restores_previous_list(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    assert load_config(config_path, create=False).snippets is None

    updated = api.import_snippets(
        {"items": [{"spoken": "my calendar", "expansion": "https://cal.example/me"}]}
    )
    assert updated["snippet_undo"] is True
    assert "my calendar" in [row["spoken"] for row in updated["snippets"]]
    assert load_config(config_path, create=False).snippets is not None

    undone = api.undo_snippet_import()
    assert "snippet_undo" not in undone
    assert load_config(config_path, create=False).snippets is None
    assert "[snippets]" not in config_path.read_text(encoding="utf-8")
    restored = [row["spoken"] for row in undone["snippets"]]
    assert restored == ["my email", "my calendar", "my signature"]

    with pytest.raises(ConfigError, match="Nothing to undo"):
        api.undo_snippet_import()


def test_settings_undo_snippet_import_restores_saved_items(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "stays"}]}})
    saved = config_path.read_text(encoding="utf-8")

    api.import_snippets({"items": [{"spoken": "imported", "expansion": "new"}]})
    undone = api.undo_snippet_import()
    spoken = [row["spoken"] for row in undone["snippets"]]
    assert spoken == ["keep me"]
    assert config_path.read_text(encoding="utf-8") == saved

    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "stays"}]}})
    with pytest.raises(ConfigError, match="Nothing to undo"):
        api.undo_snippet_import()


def test_settings_undo_snippet_import_survives_settings_restart(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "stays"}]}})
    saved = config_path.read_text(encoding="utf-8")
    api.import_snippets({"items": [{"spoken": "imported", "expansion": "new"}]})
    stash = config_path.with_name("config.toml.snippet-undo.json")
    assert stash.is_file()

    restarted = SettingsApi(
        config=load_config(config_path, create=False),
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(load_config(config_path, create=False)),
    )
    assert restarted.get_config()["snippet_undo"] is True
    undone = restarted.undo_snippet_import()
    spoken = [row["spoken"] for row in undone["snippets"]]
    assert spoken == ["keep me"]
    assert config_path.read_text(encoding="utf-8") == saved
    assert not stash.exists()
    assert "snippet_undo" not in undone


def test_settings_undo_snippet_import_steps_through_each_apply(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "stays"}]}})
    original = config_path.read_text(encoding="utf-8")
    api.import_snippets({"items": [{"spoken": "first", "expansion": "one"}]})
    after_first = config_path.read_text(encoding="utf-8")
    api.import_snippets({"items": [{"spoken": "second", "expansion": "two"}]})
    stash = config_path.with_name("config.toml.snippet-undo.json")
    payload = json.loads(stash.read_text(encoding="utf-8"))
    assert len(payload["stack"]) == 2

    restarted = SettingsApi(
        config=load_config(config_path, create=False),
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(load_config(config_path, create=False)),
    )
    assert restarted.get_config()["snippet_undo"] is True
    first_undo = restarted.undo_snippet_import()
    assert first_undo["snippet_undo"] is True
    assert [row["spoken"] for row in first_undo["snippets"]] == ["keep me", "first"]
    assert config_path.read_text(encoding="utf-8") == after_first
    assert stash.is_file()

    second_undo = restarted.undo_snippet_import()
    assert "snippet_undo" not in second_undo
    assert [row["spoken"] for row in second_undo["snippets"]] == ["keep me"]
    assert config_path.read_text(encoding="utf-8") == original
    assert not stash.exists()
    with pytest.raises(ConfigError, match="Nothing to undo"):
        restarted.undo_snippet_import()


def test_settings_undo_snippet_import_survives_dictionary_save(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "stays"}]}})
    api.import_snippets({"items": [{"spoken": "imported", "expansion": "new"}]})
    stash = config_path.with_name("config.toml.snippet-undo.json")
    imported = [
        {"spoken": row["spoken"], "expansion": row["expansion"]}
        for row in api.get_config()["snippets"]
    ]
    api.set_config(
        {
            "dictionary": {"terms": [{"spoken": "btc", "written": "Bitcoin"}]},
            "snippets": {"items": imported},
        }
    )
    assert stash.is_file()
    assert api.get_config()["snippet_undo"] is True
    undone = api.undo_snippet_import()
    assert [row["spoken"] for row in undone["snippets"]] == ["keep me"]
    assert any(
        row["spoken"] == "btc" and row["written"] == "Bitcoin" for row in undone["dictionary"]
    )
    assert not stash.exists()
    assert "snippet_undo" not in undone


def test_settings_undo_snippet_import_clears_when_snippets_change(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "stays"}]}})
    api.import_snippets({"items": [{"spoken": "imported", "expansion": "new"}]})
    stash = config_path.with_name("config.toml.snippet-undo.json")
    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "edited"}]}})
    assert not stash.exists()
    assert "snippet_undo" not in api.get_config()
    with pytest.raises(ConfigError, match="Nothing to undo"):
        api.undo_snippet_import()


def test_settings_undo_snippet_import_loads_legacy_sidecar(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"snippets": {"items": [{"spoken": "keep me", "expansion": "stays"}]}})
    saved = config_path.read_text(encoding="utf-8")
    api.import_snippets({"items": [{"spoken": "imported", "expansion": "new"}]})
    stash = config_path.with_name("config.toml.snippet-undo.json")
    stash.write_text(
        json.dumps({"items": [{"spoken": "keep me", "expansion": "stays"}]}),
        encoding="utf-8",
    )

    restarted = SettingsApi(
        config=load_config(config_path, create=False),
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(load_config(config_path, create=False)),
    )
    undone = restarted.undo_snippet_import()
    assert [row["spoken"] for row in undone["snippets"]] == ["keep me"]
    assert config_path.read_text(encoding="utf-8") == saved
    assert not stash.exists()
    assert "snippet_undo" not in undone


def test_settings_undo_dictionary_import_restores_previous_list(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"dictionary": {"terms": [{"spoken": "keep me", "written": "Keep"}]}})
    saved = config_path.read_text(encoding="utf-8")

    updated = api.import_dictionary("imported,Imported\n")
    assert updated["dictionary_undo"] is True
    spoken = [row["spoken"] for row in updated["dictionary"]]
    assert "imported" in spoken
    assert "keep me" in spoken
    stash = config_path.with_name("config.toml.dictionary-undo.json")
    assert stash.is_file()

    undone = api.undo_dictionary_import()
    assert "dictionary_undo" not in undone
    assert [row["spoken"] for row in undone["dictionary"]] == ["keep me"]
    assert config_path.read_text(encoding="utf-8") == saved
    assert not stash.exists()

    with pytest.raises(ConfigError, match="Nothing to undo"):
        api.undo_dictionary_import()


def test_settings_undo_dictionary_import_survives_settings_restart(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"dictionary": {"terms": [{"spoken": "keep me", "written": "Keep"}]}})
    saved = config_path.read_text(encoding="utf-8")
    api.import_dictionary("imported,Imported\n")
    stash = config_path.with_name("config.toml.dictionary-undo.json")
    assert stash.is_file()

    restarted = SettingsApi(
        config=load_config(config_path, create=False),
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(load_config(config_path, create=False)),
    )
    assert restarted.get_config()["dictionary_undo"] is True
    undone = restarted.undo_dictionary_import()
    assert [row["spoken"] for row in undone["dictionary"]] == ["keep me"]
    assert config_path.read_text(encoding="utf-8") == saved
    assert not stash.exists()
    assert "dictionary_undo" not in undone


def test_settings_undo_dictionary_import_steps_through_each_apply(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"dictionary": {"terms": [{"spoken": "keep me", "written": "Keep"}]}})
    original = config_path.read_text(encoding="utf-8")
    api.import_dictionary("first,First\n")
    after_first = config_path.read_text(encoding="utf-8")
    api.import_dictionary("second,Second\n")
    stash = config_path.with_name("config.toml.dictionary-undo.json")
    payload = json.loads(stash.read_text(encoding="utf-8"))
    assert len(payload["stack"]) == 2

    restarted = SettingsApi(
        config=load_config(config_path, create=False),
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(load_config(config_path, create=False)),
    )
    assert restarted.get_config()["dictionary_undo"] is True
    first_undo = restarted.undo_dictionary_import()
    assert first_undo["dictionary_undo"] is True
    spoken = [row["spoken"] for row in first_undo["dictionary"]]
    assert spoken == ["keep me", "first"]
    assert config_path.read_text(encoding="utf-8") == after_first
    assert stash.is_file()

    second_undo = restarted.undo_dictionary_import()
    assert "dictionary_undo" not in second_undo
    assert [row["spoken"] for row in second_undo["dictionary"]] == ["keep me"]
    assert config_path.read_text(encoding="utf-8") == original
    assert not stash.exists()
    with pytest.raises(ConfigError, match="Nothing to undo"):
        restarted.undo_dictionary_import()


def test_settings_undo_dictionary_import_survives_snippet_save(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    api.set_config({"dictionary": {"terms": [{"spoken": "keep me", "written": "Keep"}]}})
    api.import_dictionary("imported,Imported\n")
    stash = config_path.with_name("config.toml.dictionary-undo.json")
    imported = [
        {"spoken": row["spoken"], "written": row["written"]}
        for row in api.get_config()["dictionary"]
    ]
    api.set_config(
        {
            "snippets": {"items": [{"spoken": "sig", "expansion": "Thanks"}]},
            "dictionary": {"terms": imported},
        }
    )
    assert stash.is_file()
    assert api.get_config()["dictionary_undo"] is True
    undone = api.undo_dictionary_import()
    assert [row["spoken"] for row in undone["dictionary"]] == ["keep me"]
    assert any(
        row["spoken"] == "sig" and row["expansion"] == "Thanks" for row in undone["snippets"]
    )
    assert not stash.exists()
    assert "dictionary_undo" not in undone


def test_config_snapshot_fails_closed_for_non_boolean_prose_context() -> None:
    config = load_config(Path("config.example.toml"), create=False)
    malformed_policy = replace(config.personalization)
    object.__setattr__(malformed_policy, "enabled", "false")
    object.__setattr__(malformed_policy, "learn", "false")
    object.__setattr__(malformed_policy, "prose_context", "true")
    malformed = replace(
        config,
        personalization=malformed_policy,
    )

    snapshot = config_snapshot(malformed)["personalization"]
    assert snapshot == {"enabled": False, "learn": False, "prose_context": False}


def test_runtime_config_syncs_shared_personalization_flags(tmp_path: Path) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.record_correction("d sent", "DCENT_Voice")
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor(config.privacy),
        personalization=store,
    )

    disabled = replace(
        config,
        personalization=replace(config.personalization, enabled=False, learn=True),
    )
    api._update_runtime(disabled, PrivacyMonitor(disabled.privacy))
    assert store.apply("d sent") == "d sent"

    no_learning = replace(
        config,
        personalization=replace(config.personalization, enabled=True, learn=False),
    )
    api._update_runtime(no_learning, PrivacyMonitor(no_learning.privacy))
    assert store.apply("d sent") == "DCENT_Voice"
    assert store.record_correction("bit coin", "Bitcoin") is None


def test_settings_can_inspect_and_reset_learned_app_styles(tmp_path: Path) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    store = PersonalizationStore(tmp_path / "personalization.json")
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor(config.privacy),
        personalization=store,
    )

    learned = api.remember_app_style("notepad.exe", "email")
    assert learned["stores_audio"] is False
    assert learned["app_styles"][0]["app"] == "notepad.exe"
    assert learned["app_styles"][0]["style"] == "email"
    assert store.learned_app_styles() == {"notepad.exe": "email"}

    cleared = api.reset_app_styles()
    assert cleared["app_styles"] == []
    assert store.learned_app_styles() == {}


def test_local_cleanup_toggle_autodetects_ollama_and_never_selects_cloud(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    monkeypatch.setattr(
        api,
        "list_local_models",
        lambda: {"ollama": ["qwen2.5:3b"], "lmstudio": [], "faster_whisper": []},
    )

    status = api.local_cleanup_status()
    assert status["required"] is False
    assert status["cloud"] is False
    assert status["fallback"] == "heuristics"
    assert status["requested"] is False

    enabled = api.set_local_cleanup(True)
    local = enabled["local_cleanup"]
    assert local["requested"] is True
    assert local["provider"] == "ollama"
    assert local["llm"] == "ollama:qwen2.5:3b"
    assert local["cloud"] is False
    assert "openai" not in local["llm"]
    assert "groq" not in local["llm"]
    assert load_config(config_path, create=False).current_profile.cleanup_enabled is True

    monkeypatch.setattr(
        api,
        "list_local_models",
        lambda: {"ollama": [], "lmstudio": [], "faster_whisper": []},
    )
    disabled = api.set_local_cleanup(False)
    assert disabled["local_cleanup"]["requested"] is False
    assert load_config(config_path, create=False).current_profile.cleanup_enabled is False


def test_local_model_probe_ignores_hostile_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dcent_voice.ui import settings as settings_module

    proxy, proxy_thread, proxy_requests = _recording_get_server()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        unused_port = reservation.getsockname()[1]
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("ALL_PROXY", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")

    try:
        models = settings_module._get_json_names(  # noqa: SLF001
            f"http://127.0.0.1:{unused_port}/api/tags",
            "models",
            "name",
        )
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2.0)

    assert models == []
    assert proxy_requests == []


def test_local_model_probe_does_not_follow_redirect() -> None:
    from dcent_voice.ui import settings as settings_module

    destination, destination_thread, destination_requests = _recording_get_server(status=200)
    destination_url = f"http://127.0.0.1:{destination.server_port}/collect"
    source, source_thread, source_requests = _recording_get_server(
        status=307,
        location=destination_url,
    )

    try:
        models = settings_module._get_json_names(  # noqa: SLF001
            f"http://127.0.0.1:{source.server_port}/api/tags",
            "models",
            "name",
        )
    finally:
        source.shutdown()
        source.server_close()
        source_thread.join(timeout=2.0)
        destination.shutdown()
        destination.server_close()
        destination_thread.join(timeout=2.0)

    assert models == []
    assert source_requests == ["/api/tags"]
    assert destination_requests == []


def test_hardware_status_reports_cpu_default_path(tmp_path: Path, monkeypatch) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    bus = EventBus()
    privacy = PrivacyMonitor(config.privacy)
    api = SettingsApi(config=config, bus=bus, privacy=privacy)

    monkeypatch.setattr(
        "dcent_voice.asr.faster_whisper_provider.cuda_runtime_ready",
        lambda: False,
    )
    status = api.hardware_status()
    snapshot = api.get_config()
    setup = api.setup_state()

    assert status["active_device"] == "cpu"
    assert status["active_compute"] == "int8"
    assert status["cuda_ready"] is False
    assert "CPU" in status["summary"]
    assert snapshot["hardware"]["active_device"] == "cpu"
    assert setup["hardware"]["cpu_default_asr"] == "parakeet:tdt-0.6b-v3:int8"
    assert "Parakeet" in status["summary"]


def test_setup_state_reports_verified_parakeet_and_offline_fallback(
    monkeypatch,
) -> None:
    from dcent_voice.ui import settings as settings_module

    config = load_config(Path("config.example.toml"), create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    primary = {
        "ready": True,
        "model_id": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "revision": "parakeet-revision",
        "path": "models/parakeet",
        "detail": "Verified local snapshot ready",
    }
    fallback = {
        "ready": True,
        "model_id": "Systran/faster-whisper-base",
        "revision": "whisper-revision",
        "path": "models/faster-whisper-base",
        "detail": "Verified local snapshot ready",
    }
    monkeypatch.setattr(
        api,
        "hardware_status",
        lambda: {
            "resolved_asr": "parakeet:tdt-0.6b-v3:int8",
            "model_readiness": primary,
        },
    )
    monkeypatch.setattr(
        api,
        "list_local_models",
        lambda: {"ollama": [], "lmstudio": [], "faster_whisper": []},
    )
    monkeypatch.setattr(
        settings_module,
        "faster_whisper_model_status",
        lambda model: fallback if model == "base" else None,
    )

    state = api.setup_state()

    assert state["has_local_asr_model"] is True
    assert state["asr_readiness"] == {
        "runtime_downloads": False,
        "primary_provider": "parakeet",
        "primary_model": "tdt-0.6b-v3",
        "primary": primary,
        "fallback": fallback,
    }


def test_hardware_status_reports_resolved_multilingual_model_and_readiness(
    monkeypatch,
) -> None:
    config = replace(
        load_config(Path("config.example.toml"), create=False),
        language_mode="multilingual",
        language="fr",
    )
    unavailable = {
        "ready": False,
        "model_id": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "revision": "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
        "path": None,
        "detail": "reinstall complete package",
    }
    monkeypatch.setattr(
        "dcent_voice.asr.parakeet_provider.parakeet_model_status",
        lambda: unavailable,
    )
    monkeypatch.setattr(
        "dcent_voice.asr.faster_whisper_provider.cuda_runtime_ready",
        lambda: False,
    )
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor(config.privacy),
    )
    status = api.hardware_status()
    assert status["active_asr"].startswith("parakeet:")
    assert status["resolved_asr"] == "parakeet:tdt-0.6b-v3:int8"
    assert status["model_readiness"] == unavailable
    assert "Parakeet" in status["summary"]


def test_hardware_status_nudges_stale_whisper_desktop(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"
[profile.desktop]
asr = "faster-whisper:base.en:cpu-int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(config=config, bus=EventBus(), privacy=PrivacyMonitor(config.privacy))
    monkeypatch.setattr(
        "dcent_voice.asr.faster_whisper_provider.cuda_runtime_ready",
        lambda: False,
    )
    rec = api.hardware_status()["recommendation"].lower()
    assert "parakeet" in rec
    assert "not auto-migrated" in rec


def test_hardware_status_warns_when_heavy_cpu_model_active(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"
[profile.desktop]
asr = "faster-whisper:distil-small.en:cpu-int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(config=config, bus=EventBus(), privacy=PrivacyMonitor(config.privacy))
    monkeypatch.setattr(
        "dcent_voice.asr.faster_whisper_provider.cuda_runtime_ready",
        lambda: False,
    )
    status = api.hardware_status()
    assert status["active_device"] == "cpu"
    rec = status["recommendation"].lower()
    assert "base.en" in rec or "desktop" in rec
    assert "800" in status["recommendation"]


def test_model_scan_includes_offline_registry_snapshots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DCENT_VOICE_MODEL_DIR", str(tmp_path / "models"))
    snapshot = tmp_path / "models" / "faster-whisper" / "Systran--faster-distil-whisper-small.en"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "model.bin").write_bytes(b"weights")

    assert "distil-small.en" in scan_faster_whisper_cache()


def test_model_scan_requires_complete_huggingface_snapshot(tmp_path: Path, monkeypatch) -> None:
    from dcent_voice.ui import settings as settings_module

    monkeypatch.setenv("DCENT_VOICE_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(settings_module.Path, "home", classmethod(lambda _cls: tmp_path))
    cache = tmp_path / ".cache" / "huggingface" / "hub"
    complete = cache / "models--Systran--faster-whisper-tiny" / "snapshots" / "revision"
    complete.mkdir(parents=True)
    (complete / "config.json").write_text("{}", encoding="utf-8")
    (complete / "model.bin").write_bytes(b"weights")
    incomplete = cache / "models--Systran--faster-whisper-tiny.en" / "snapshots" / "partial"
    incomplete.mkdir(parents=True)

    found = scan_faster_whisper_cache()

    assert "tiny" in found
    assert "tiny.en" not in found


def test_settings_microphone_check_uses_configured_input_device(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml")
        .read_text(encoding="utf-8")
        .replace('input_device = ""', 'input_device = "USB microphone"'),
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(config=config, bus=EventBus(), privacy=PrivacyMonitor.from_config(config))
    stream_args: dict[str, object] = {}

    class FakeInputStream:
        def __init__(self, **kwargs) -> None:
            stream_args.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=FakeInputStream))
    monkeypatch.setattr("dcent_voice.ui.settings.time.sleep", lambda _duration: None)

    result = api.test_microphone()

    assert result["ok"] is True
    assert stream_args["device"] == "USB microphone"


def test_settings_benchmark_uses_packaged_cli_entrypoint(tmp_path: Path, monkeypatch) -> None:
    from dcent_voice.ui import settings as settings_module

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(config=config, bus=EventBus(), privacy=PrivacyMonitor.from_config(config))
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="bench output", stderr="")

    monkeypatch.setattr(settings_module.subprocess, "run", fake_run)
    monkeypatch.setattr(settings_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(settings_module.sys, "executable", "C:/DCENT_Voice/dcent-voice.exe")

    result = api.run_benchmark()

    assert result == {"returncode": 0, "stdout": "bench output", "stderr": ""}
    assert commands == [
        [
            "C:/DCENT_Voice/dcent-voice.exe",
            "--config",
            str(config_path),
            "benchmark",
        ]
    ]


def test_settings_set_config_writes_valid_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )

    updated = api.set_config({"active_profile": "tiny", "hotkeys": {"dictation": "ctrl+alt"}})
    reloaded = load_config(config_path, create=False)

    assert updated["active_profile"] == "tiny"
    assert reloaded.active_profile == "tiny"
    assert reloaded.hotkeys.dictation == "ctrl+alt"
    assert reloaded.injector.per_app["WindowsTerminal.exe"] == "keystroke"


def test_local_provider_account_reports_real_availability(tmp_path: Path, monkeypatch) -> None:
    config = load_config(Path("config.example.toml"), create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )
    monkeypatch.setattr("dcent_voice.ui.settings._local_provider_running", lambda _name: False)

    account = api.provider_account("lmstudio")

    assert account["connected"] is False
    assert account["status"] == "unavailable"
    assert account["label"] == "Not running"


def test_settings_set_config_preserves_unedited_sections(tmp_path: Path) -> None:
    """RT-UX-1: a save must never wipe keys the settings UI does not edit.

    config_version, the [tts] section, audio limits, and unknown future
    sections all have to survive a round-trip through set_config.
    """
    import tomllib

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8")
        + '\n[experimental]\nfuture_knob = "keep-me"\n',
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )

    api.set_config({"active_profile": "tiny"})
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    reloaded = load_config(config_path, create=False)

    assert raw["config_version"] == 2
    assert raw["tts"]["backend"] == "kokoro"
    assert raw["tts"]["enabled"] is False
    assert raw["tts"]["duck_gain"] == 0.2
    assert raw["audio"]["max_seconds"] == 90
    assert raw["audio"]["auto_stop_seconds"] == 60
    assert raw["experimental"]["future_knob"] == "keep-me"
    assert reloaded.tts.backend == "kokoro"
    assert reloaded.audio.max_seconds == 90.0


def test_settings_set_config_rejects_invalid_patch_without_touching_disk(
    tmp_path: Path,
) -> None:
    """A patch that would produce an invalid config must raise and leave the
    on-disk file exactly as it was (validate-before-write)."""
    import pytest

    from dcent_voice.config import ConfigError

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = config_path.read_text(encoding="utf-8")
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
    )

    with pytest.raises(ConfigError):
        api.set_config({"active_profile": "does-not-exist"})
    with pytest.raises(ConfigError):
        api.set_config({"service": {"port": 999999}})
    for field in ("enabled", "learn", "prose_context"):
        for invalid in ("false", "true", 1, 0, None, [], {}):
            with pytest.raises(ConfigError, match=rf"personalization.{field}"):
                api.set_config({"personalization": {field: invalid}})

    assert config_path.read_text(encoding="utf-8") == before


def test_concurrent_settings_patches_preserve_disjoint_changes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    apis = [
        SettingsApi(
            config=config,
            bus=EventBus(),
            privacy=PrivacyMonitor.from_config(config),
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def save(api: SettingsApi, patch: dict) -> None:
        try:
            barrier.wait()
            api.set_config(patch)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=save, args=(apis[0], {"language": "fr"})),
        threading.Thread(
            target=save,
            args=(apis[1], {"hotkeys": {"dictation": "ctrl+alt"}}),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    reloaded = load_config(config_path, create=False)
    assert reloaded.language == "fr"
    assert reloaded.hotkeys.dictation == "ctrl+alt"
    assert list(tmp_path.glob(".config.toml.*.tmp")) == []


def test_settings_grants_cloud_consent_when_connecting_provider(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_key_validation(monkeypatch)
    config_path = tmp_path / "config.toml"
    consent_path = (tmp_path / "consent.json").as_posix()
    egress_path = (tmp_path / "egress.jsonl").as_posix()
    config_path.write_text(
        f"""
active_profile = "cloud"

[privacy]
consent_ledger_path = "{consent_path}"
egress_log_path = "{egress_path}"

[profile.cloud]
asr = "deepgram:nova-3"
llm = "none"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    monitor = PrivacyMonitor.from_config(config)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=monitor,
        credential_store=CredentialStore(FakeKeyring()),
    )

    assert "asr:deepgram" in api.get_privacy_status()["missing_consents"]
    account = api.connect_provider("deepgram", "dg-test", grant_consent=True)

    assert account["connected"] is True
    assert "asr:deepgram" not in api.get_privacy_status()["missing_consents"]


def test_settings_can_grant_inactive_provider_consent(tmp_path: Path, monkeypatch) -> None:
    _stub_key_validation(monkeypatch)
    config_path = tmp_path / "config.toml"
    consent_path = (tmp_path / "consent.json").as_posix()
    config_path.write_text(
        f"""
active_profile = "desktop"

[privacy]
consent_ledger_path = "{consent_path}"
egress_log_path = ""

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
        credential_store=CredentialStore(FakeKeyring()),
    )

    api.connect_provider("openai", "sk-test", grant_consent=True)
    ledger = api.get_consent_ledger()

    assert {entry["provider_key"] for entry in ledger} == {"llm:openai", "asr:openai"}


def _cloud_api(tmp_path: Path, *, credential_store: CredentialStore | None = None) -> SettingsApi:
    config_path = tmp_path / "config.toml"
    consent_path = (tmp_path / "consent.json").as_posix()
    egress_path = (tmp_path / "egress.jsonl").as_posix()
    config_path.write_text(
        f"""
active_profile = "desktop"

[privacy]
consent_ledger_path = "{consent_path}"
egress_log_path = "{egress_path}"

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    return SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
        credential_store=credential_store or CredentialStore(FakeKeyring()),
    )


def test_disconnect_provider_does_not_claim_success_while_keyring_is_locked(
    tmp_path: Path,
) -> None:
    backend = LockableKeyring()
    store = CredentialStore(backend)
    store.set_secret("xai", "api_key", "xai-api-retained")
    store.set_secret("xai", "oauth_access_token", "xai-oauth-retained")
    api = _cloud_api(tmp_path, credential_store=store)
    backend.locked = True

    result = api.disconnect_provider("xai")

    assert result["ok"] is False
    assert result["connected"] is True
    assert result["status"] == "error"
    assert "xai-api-retained" not in result["detail"]
    assert "xai-oauth-retained" not in result["detail"]

    backend.locked = False
    assert store.get_secret("xai", "api_key") == "xai-api-retained"
    assert store.get_secret("xai", "oauth_access_token") == "xai-oauth-retained"
    assert api.provider_account("xai")["connected"] is True


def test_disconnect_provider_reports_verified_success(tmp_path: Path) -> None:
    backend = FakeKeyring()
    store = CredentialStore(backend)
    store.set_secret("openai", "api_key", "sk-remove")
    api = _cloud_api(tmp_path, credential_store=store)

    result = api.disconnect_provider("openai")

    assert result["ok"] is True
    assert result["connected"] is False
    assert store.get_secret("openai", "api_key") is None


def _authorized_oauth_result(result):
    """Model an OAuth helper whose public boundary authorizes before wire."""

    def call(*args, **kwargs):
        kwargs["authorize_egress"]()
        return result

    return call


def test_connect_provider_saves_key_unverified_when_provider_unreachable(
    tmp_path: Path, monkeypatch
) -> None:
    # A network blip must not throw the user's (likely valid) key away — only a
    # real rejection from a reachable provider does.
    monkeypatch.setattr(
        "dcent_voice.ui.settings.validate_api_key",
        lambda *args, **kwargs: ValidationResult(False, "Could not reach openai", reachable=False),
    )
    api = _cloud_api(tmp_path)

    account = api.connect_provider("openai", "sk-good", grant_consent=True)

    assert account["ok"] is True
    assert account["connected"] is True
    assert "unverified" in account["detail"].lower()


def test_connect_provider_refuses_rejected_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "dcent_voice.ui.settings.validate_api_key",
        lambda *args, **kwargs: ValidationResult(False, "The provider rejected this key."),
    )
    api = _cloud_api(tmp_path)

    account = api.connect_provider("openai", "sk-bad", grant_consent=True)

    assert account["ok"] is False
    assert account["connected"] is False


def test_connect_provider_audits_auth_before_rejected_wire(tmp_path: Path, monkeypatch) -> None:
    from dcent_voice.auth import validate as validate_module

    api = _cloud_api(tmp_path)

    def rejected_probe(provider: str, api_key: str, timeout_s: float):
        assert provider == "openai"
        assert api_key == "sk-private-rejected"
        assert timeout_s > 0
        entries = api.get_egress_log()
        fields = [
            (entry["provider_key"], entry["payload_type"], entry["byte_count"]) for entry in entries
        ]
        assert fields == [("auth:openai", "credentials", 0)]
        return SimpleNamespace(status_code=401)

    monkeypatch.setattr(validate_module, "_probe", rejected_probe)

    account = api.connect_provider("openai", "sk-private-rejected", grant_consent=True)

    assert account["ok"] is False
    assert account["connected"] is False
    assert {entry["provider_key"] for entry in api.get_consent_ledger()} == {"auth:openai"}
    raw_log = (tmp_path / "egress.jsonl").read_text(encoding="utf-8")
    assert "sk-private-rejected" not in raw_log
    assert set(json.loads(raw_log).keys()) == {
        "byte_count",
        "payload_type",
        "provider_key",
        "timestamp",
    }


def test_connect_provider_audits_auth_before_network_failure(tmp_path: Path, monkeypatch) -> None:
    from dcent_voice.auth import validate as validate_module

    api = _cloud_api(tmp_path)

    def offline_probe(*args, **kwargs):
        assert api.get_egress_log()[0]["provider_key"] == "auth:openai"
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(validate_module, "_probe", offline_probe)

    account = api.connect_provider("openai", "sk-private-offline", grant_consent=True)

    assert account["ok"] is True
    assert account["connected"] is True
    assert api.get_egress_log() == [
        {
            "timestamp": api.get_egress_log()[0]["timestamp"],
            "provider_key": "auth:openai",
            "payload_type": "credentials",
            "byte_count": 0,
        }
    ]
    assert {entry["provider_key"] for entry in api.get_consent_ledger()} == {
        "auth:openai",
        "llm:openai",
        "asr:openai",
    }
    assert "sk-private-offline" not in (tmp_path / "egress.jsonl").read_text(encoding="utf-8")


def test_connect_provider_without_auth_consent_never_reaches_wire(
    tmp_path: Path, monkeypatch
) -> None:
    from dcent_voice.auth import validate as validate_module

    api = _cloud_api(tmp_path)
    called = False

    def probe(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(validate_module, "_probe", probe)

    account = api.connect_provider("openai", "sk-private", grant_consent=False)

    assert account["ok"] is False
    assert "credential egress" in account["detail"].lower()
    assert called is False
    assert api.get_consent_ledger() == []
    assert api.get_egress_log() == []


def test_begin_device_login_is_gated_without_client_id(tmp_path: Path) -> None:
    api = _cloud_api(tmp_path)

    # The shipped xai config has no registered client_id yet, so the bridge must
    # refuse cleanly and steer the user to an API key instead of erroring.
    result = api.begin_device_login("xai")

    assert result["ok"] is False
    assert "API key" in result["detail"]


def test_poll_device_login_without_begin_reports_error(tmp_path: Path) -> None:
    api = _cloud_api(tmp_path)

    result = api.poll_device_login("xai")

    assert result["status"] == "error"


def test_device_login_full_flow(tmp_path: Path, monkeypatch) -> None:
    api = _cloud_api(tmp_path)
    from dcent_voice.ui import settings as settings_module

    monkeypatch.setitem(
        settings_module.OAUTH_CONFIGS,
        "xai",
        {
            "device_endpoint": "https://example/device",
            "token_endpoint": "https://example/token",
            "client_id": "test-client",
            "scope": "api",
        },
    )
    grant = DeviceCodeGrant(
        device_code="dev-123",
        user_code="ABCD-EFGH",
        verification_uri="https://example/verify",
        interval=1,
    )
    monkeypatch.setattr(
        "dcent_voice.ui.settings.request_device_code", _authorized_oauth_result(grant)
    )

    started = api.begin_device_login("xai", grant_consent=True)
    assert started["ok"] is True
    assert started["user_code"] == "ABCD-EFGH"
    assert started["verification_uri"] == "https://example/verify"
    assert [
        (entry["provider_key"], entry["payload_type"], entry["byte_count"])
        for entry in api.get_egress_log()
    ] == [("auth:xai", "credentials", 0)]

    # First poll: RFC 8628 authorization_pending (HTTP 400) must map to
    # "pending", not an error.
    pending_request = httpx.Request("POST", "https://example/token")
    pending_response = httpx.Response(
        400, json={"error": "authorization_pending"}, request=pending_request
    )

    def raise_pending(*args, **kwargs):
        kwargs["authorize_egress"]()
        raise httpx.HTTPStatusError("pending", request=pending_request, response=pending_response)

    monkeypatch.setattr("dcent_voice.ui.settings.poll_device_token", raise_pending)
    assert api.poll_device_login("xai")["status"] == "pending"
    assert len(api.get_egress_log()) == 2

    # Second poll: token issued -> stored, consented, and reported connected.
    monkeypatch.setattr(
        "dcent_voice.ui.settings.poll_device_token",
        _authorized_oauth_result(OAuthToken(access_token="tok-xyz")),
    )
    assert api.poll_device_login("xai")["status"] == "connected"
    assert len(api.get_egress_log()) == 3
    assert all(entry["provider_key"] == "auth:xai" for entry in api.get_egress_log())
    assert all(entry["byte_count"] == 0 for entry in api.get_egress_log())
    raw_log = (tmp_path / "egress.jsonl").read_text(encoding="utf-8")
    assert "dev-123" not in raw_log
    assert "tok-xyz" not in raw_log

    account = api.provider_account("xai")
    assert account["connected"] is True
    # The grant is consumed; another poll without a new begin is an error.
    assert api.poll_device_login("xai")["status"] == "error"


def test_device_login_requires_auth_consent_before_wire(tmp_path: Path, monkeypatch) -> None:
    api = _cloud_api(tmp_path)
    from dcent_voice.ui import settings as settings_module

    monkeypatch.setitem(
        settings_module.OAUTH_CONFIGS,
        "xai",
        {
            "device_endpoint": "https://example/device",
            "token_endpoint": "https://example/token",
            "client_id": "test-client",
            "scope": "api",
        },
    )
    called = False

    def request(*args, **kwargs):
        nonlocal called
        kwargs["authorize_egress"]()
        called = True
        raise AssertionError("wire must remain blocked")

    monkeypatch.setattr("dcent_voice.ui.settings.request_device_code", request)

    result = api.begin_device_login("xai")

    assert result["ok"] is False
    assert "credential egress" in result["detail"].lower()
    assert called is False
    assert api.get_egress_log() == []


def test_device_login_rejects_insecure_endpoint_before_credential_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = _cloud_api(tmp_path)
    from dcent_voice.ui import settings as settings_module

    monkeypatch.setitem(
        settings_module.OAUTH_CONFIGS,
        "xai",
        {
            "device_endpoint": "http://accounts.x.ai/oauth2/device/code",
            "token_endpoint": "https://accounts.x.ai/oauth2/token",
            "client_id": "test-client",
            "scope": "api",
        },
    )

    result = api.begin_device_login("xai", grant_consent=True)

    assert result == {
        "ok": False,
        "detail": "Provider sign-in configuration or response was invalid.",
    }
    assert api.get_egress_log() == []


def test_device_poll_rechecks_revoked_auth_consent(tmp_path: Path, monkeypatch) -> None:
    api = _cloud_api(tmp_path)
    from dcent_voice.ui import settings as settings_module

    monkeypatch.setitem(
        settings_module.OAUTH_CONFIGS,
        "xai",
        {
            "device_endpoint": "https://example/device",
            "token_endpoint": "https://example/token",
            "client_id": "test-client",
            "scope": "api",
        },
    )
    monkeypatch.setattr(
        "dcent_voice.ui.settings.request_device_code",
        _authorized_oauth_result(
            DeviceCodeGrant(
                device_code="dev-secret",
                user_code="ABCD-EFGH",
                verification_uri="https://example/verify",
            )
        ),
    )
    assert api.begin_device_login("xai", grant_consent=True)["ok"] is True
    api.revoke_consent("auth:xai")
    called = False

    def poll(*args, **kwargs):
        nonlocal called
        kwargs["authorize_egress"]()
        called = True
        raise AssertionError("wire must remain blocked")

    monkeypatch.setattr("dcent_voice.ui.settings.poll_device_token", poll)

    result = api.poll_device_login("xai")

    assert result["status"] == "error"
    assert "revoked" in result["detail"].lower()
    assert called is False
    assert len(api.get_egress_log()) == 1
    assert "dev-secret" not in (tmp_path / "egress.jsonl").read_text(encoding="utf-8")


def test_device_poll_rejects_insecure_endpoint_before_credential_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = _cloud_api(tmp_path)
    from dcent_voice.ui import settings as settings_module

    monkeypatch.setitem(
        settings_module.OAUTH_CONFIGS,
        "xai",
        {
            "device_endpoint": "https://accounts.x.ai/oauth2/device/code",
            "token_endpoint": "http://accounts.x.ai/oauth2/token",
            "client_id": "test-client",
            "scope": "api",
        },
    )
    monkeypatch.setattr(
        "dcent_voice.ui.settings.request_device_code",
        _authorized_oauth_result(
            DeviceCodeGrant(
                device_code="dev-endpoint-secret",
                user_code="ABCD-EFGH",
                verification_uri="https://accounts.x.ai/device",
            )
        ),
    )
    assert api.begin_device_login("xai", grant_consent=True)["ok"] is True
    audit_count = len(api.get_egress_log())

    result = api.poll_device_login("xai")

    assert result == {
        "status": "error",
        "detail": "Provider sign-in configuration or response was invalid.",
    }
    assert len(api.get_egress_log()) == audit_count
    assert "dev-endpoint-secret" not in (tmp_path / "egress.jsonl").read_text(encoding="utf-8")


def test_device_poll_network_failure_keeps_pre_wire_audit(tmp_path: Path, monkeypatch) -> None:
    api = _cloud_api(tmp_path)
    from dcent_voice.ui import settings as settings_module

    monkeypatch.setitem(
        settings_module.OAUTH_CONFIGS,
        "xai",
        {
            "device_endpoint": "https://example/device",
            "token_endpoint": "https://example/token",
            "client_id": "test-client",
            "scope": "api",
        },
    )
    monkeypatch.setattr(
        "dcent_voice.ui.settings.request_device_code",
        _authorized_oauth_result(
            DeviceCodeGrant(
                device_code="dev-network-secret",
                user_code="ABCD-EFGH",
                verification_uri="https://example/verify",
            )
        ),
    )
    assert api.begin_device_login("xai", grant_consent=True)["ok"] is True

    def offline(*args, **kwargs):
        kwargs["authorize_egress"]()
        assert len(api.get_egress_log()) == 2
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("dcent_voice.ui.settings.poll_device_token", offline)

    result = api.poll_device_login("xai")

    assert result["status"] == "error"
    assert "network" in result["detail"].lower()
    assert len(api.get_egress_log()) == 2
    assert "dev-network-secret" not in (tmp_path / "egress.jsonl").read_text(encoding="utf-8")


def test_check_for_update_bridge_shape(tmp_path: Path, monkeypatch) -> None:
    from dcent_voice.util import updates

    api = _cloud_api(tmp_path)
    monkeypatch.setattr(
        updates,
        "check_for_update",
        lambda current, **kwargs: updates.UpdateInfo(
            available=True, current=current, latest="9.9.9", url="https://example/rel"
        ),
    )

    result = api.check_for_update()

    assert result["available"] is True
    assert result["latest"] == "9.9.9"
    assert result["url"] == "https://example/rel"


def test_open_url_rejects_non_http_schemes(tmp_path: Path) -> None:
    api = _cloud_api(tmp_path)

    assert api.open_url("file:///C:/Windows/System32/calc.exe") is False
    assert api.open_url("javascript:alert(1)") is False
    assert api.open_url(123) is False


def test_settings_first_run_education_methods(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(config=config, bus=EventBus(), privacy=PrivacyMonitor.from_config(config))

    assert api.get_first_run_education()["shown"] is False
    updated = api.mark_first_run_education_shown()

    assert updated["privacy"]["first_run_education_shown"] is True


def test_settings_tts_installer_requires_confirmation_then_enables_verified_backend(
    tmp_path: Path, monkeypatch
) -> None:
    from dcent_voice.tts import assets as assets_module
    from dcent_voice.ui import settings as settings_module

    payload = b"verified test tts model"
    asset = TtsModelAsset(
        key="test-kokoro.onnx",
        filename="test-kokoro.onnx",
        url="https://example.invalid/test-kokoro.onnx",
        sha256=hashlib.sha256(payload).hexdigest(),
        license="Apache-2.0",
        license_url="https://example.invalid/license",
    )
    monkeypatch.setitem(assets_module.ASSETS_BY_BACKEND, "kokoro", (asset,))
    monkeypatch.setattr(settings_module, "_tts_runtime_available", lambda _backend: True)
    config_path = tmp_path / "config.toml"
    consent_path = tmp_path / "consent.json"
    egress_path = tmp_path / "egress.jsonl"
    config_path.write_text(
        f'''\
active_profile = "desktop"

[privacy]
consent_ledger_path = "{consent_path.as_posix()}"
egress_log_path = "{egress_path.as_posix()}"

[tts]
enabled = false
backend = "kokoro"

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
''',
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
        tts_fetch=lambda _url: payload,
        tts_model_root=tmp_path / "tts-root",
    )

    refused = api.install_tts_models("kokoro")
    assert refused["ok"] is False
    assert "Confirm" in refused["detail"]
    assert not consent_path.exists()

    result = api.install_tts_models("kokoro", accept_egress=True)
    reloaded = load_config(config_path, create=False)

    assert result["ok"] is True
    assert result["backend"] == "kokoro"
    assert result["restart_required"] is True
    assert result["files"] == ["test-kokoro.onnx"]
    assert result["config"]["tts"]["enabled"] is True
    assert reloaded.tts.enabled is True
    assert reloaded.tts.backend == "kokoro"
    assert api.get_consent_ledger()[0]["provider_key"] == MODEL_DOWNLOAD_KEY
    assert api.get_egress_log()[-1]["provider_key"] == MODEL_DOWNLOAD_KEY
    assert (tmp_path / "tts-root" / "models" / "tts" / "kokoro" / asset.filename).exists()


def test_settings_tts_installer_does_not_enable_after_checksum_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from dcent_voice.tts import assets as assets_module
    from dcent_voice.ui import settings as settings_module

    asset = TtsModelAsset(
        key="bad-kokoro.onnx",
        filename="bad-kokoro.onnx",
        url="https://example.invalid/bad-kokoro.onnx",
        sha256="0" * 64,
        license="Apache-2.0",
    )
    monkeypatch.setitem(assets_module.ASSETS_BY_BACKEND, "kokoro", (asset,))
    monkeypatch.setattr(settings_module, "_tts_runtime_available", lambda _backend: True)
    config_path = tmp_path / "config.toml"
    consent_path = tmp_path / "consent.json"
    egress_path = tmp_path / "egress.jsonl"
    config_path.write_text(
        f'''\
active_profile = "desktop"

[privacy]
consent_ledger_path = "{consent_path.as_posix()}"
egress_log_path = "{egress_path.as_posix()}"

[tts]
enabled = false
backend = "kokoro"

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
''',
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
        tts_fetch=lambda _url: b"wrong bytes",
        tts_model_root=tmp_path / "tts-root",
    )

    result = api.install_tts_models("kokoro", accept_egress=True)

    assert result["ok"] is False
    assert load_config(config_path, create=False).tts.enabled is False


def test_settings_tts_installer_refuses_a_build_without_the_runtime(monkeypatch) -> None:
    from dcent_voice.ui import settings as settings_module

    config = load_config(Path("config.example.toml"), create=False)
    api = SettingsApi(config=config, bus=EventBus(), privacy=PrivacyMonitor.from_config(config))
    monkeypatch.setattr(settings_module, "_tts_runtime_available", lambda _backend: False)

    status = api.tts_model_status()
    result = api.install_tts_models("kokoro", accept_egress=True)

    assert [backend["backend"] for backend in status["backends"]] == ["kokoro"]
    assert all(not backend["runtime_ready"] for backend in status["backends"])
    assert result["ok"] is False
    assert "does not include optional TTS runtimes" in result["detail"]


def test_settings_tts_installer_defers_piper_before_granting_consent() -> None:
    config = load_config(Path("config.example.toml"), create=False)
    api = SettingsApi(config=config, bus=EventBus(), privacy=PrivacyMonitor.from_config(config))

    result = api.install_tts_models("piper", accept_egress=True)

    assert result["ok"] is False
    assert "Piper is deferred" in result["detail"]
