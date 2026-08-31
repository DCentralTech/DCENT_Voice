# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from dcent_voice.attach.registry import verify_private_file, write_text_atomic
from dcent_voice.config import ConfigError, RecoveryConfig, load_config, parse_config
from dcent_voice.events import EventBus
from dcent_voice.privacy import PrivacyMonitor
from dcent_voice.recovery import RecoveryStore
from dcent_voice.ui.settings import SettingsApi, config_snapshot


def test_recovery_is_disabled_and_empty_by_default(tmp_path: Path) -> None:
    store = RecoveryStore(path=tmp_path / "vault.json")

    assert store.record("must not persist", reason="error:RuntimeError", mode="dictation") is False
    assert not store.path.exists()
    assert store.snapshot() == {
        "enabled": False,
        "stores_audio": False,
        "stores_successes": False,
        "entry_count": 0,
        "entries": [],
        "retention": {"max_items": 10, "max_age_hours": 24},
        "path": str(store.path),
        "integrity_ok": True,
        "detail": "",
    }


def test_recovery_retains_only_bounded_private_text_and_can_purge(tmp_path: Path) -> None:
    path = tmp_path / "recovery" / "vault.json"
    store = RecoveryStore(path=path, enabled=True, max_items=2, max_age_hours=24)

    assert store.record("first private phrase", reason="focus_changed", mode="dictation")
    assert store.record("second private phrase", reason="error:OSError", mode="dictation")
    assert store.record("third private phrase", reason="focus_changed", mode="command")

    snapshot = store.snapshot()
    assert snapshot["entry_count"] == 2
    assert [item["text"] for item in snapshot["entries"]] == [
        "third private phrase",
        "second private phrase",
    ]
    assert snapshot["stores_audio"] is False
    assert snapshot["stores_successes"] is False
    assert "first private phrase" not in path.read_text(encoding="utf-8")
    verify_private_file(path)

    store.update_policy(RecoveryConfig(enabled=False, max_items=2, max_age_hours=24))
    assert not path.exists()
    assert store.snapshot()["entries"] == []


def test_recovery_corruption_fails_closed_without_overwriting_content(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    write_text_atomic(path, "not-json-private-content")
    before = path.read_bytes()
    store = RecoveryStore(path=path, enabled=True)

    assert store.record("new secret", reason="focus_changed", mode="dictation") is False
    status = store.snapshot()

    assert status["integrity_ok"] is False
    assert status["entries"] == []
    assert path.read_bytes() == before
    assert "new secret" not in path.read_text(encoding="utf-8")

    store.update_policy(RecoveryConfig(enabled=False))
    assert not path.exists()
    assert store.snapshot()["integrity_ok"] is True


def test_recovery_prunes_expired_entries_before_accepting_new_text(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    store = RecoveryStore(path=path, enabled=True, max_items=10, max_age_hours=1)
    assert store.record("expired private phrase", reason="focus_changed", mode="dictation")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
    write_text_atomic(path, json.dumps(payload))

    assert store.record("current private phrase", reason="focus_changed", mode="dictation")

    status = store.snapshot()
    assert status["entry_count"] == 1
    assert status["entries"][0]["text"] == "current private phrase"
    assert "expired private phrase" not in path.read_text(encoding="utf-8")


def test_recovery_prunes_future_dated_entries_that_could_evade_retention(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vault.json"
    store = RecoveryStore(path=path, enabled=True, max_items=10, max_age_hours=1)
    assert store.record("future private phrase", reason="focus_changed", mode="dictation")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"][0]["created_at"] = "2999-01-01T00:00:00+00:00"
    write_text_atomic(path, json.dumps(payload))

    assert store.record("current private phrase", reason="focus_changed", mode="dictation")

    status = store.snapshot()
    assert status["entry_count"] == 1
    assert status["entries"][0]["text"] == "current private phrase"
    assert "future private phrase" not in path.read_text(encoding="utf-8")


def test_disable_and_clear_report_when_retained_bytes_cannot_be_purged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.json"
    store = RecoveryStore(path=path, enabled=True)
    assert store.record("still private", reason="focus_changed", mode="dictation")
    before = path.read_bytes()
    original_unlink = Path.unlink

    def deny_vault_unlink(target: Path, *args, **kwargs) -> None:
        if target == path:
            raise PermissionError("simulated sharing violation")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_vault_unlink)

    assert store.update_policy(RecoveryConfig(enabled=False)) is False
    status = store.snapshot()
    assert status["enabled"] is False
    assert status["integrity_ok"] is False
    assert "could not be purged" in status["detail"]
    assert path.read_bytes() == before
    assert store.clear() is False
    assert path.read_bytes() == before


def test_disable_wins_against_record_already_queued_for_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecoveryStore(path=tmp_path / "vault.json", enabled=True)
    reached_pre_lock = threading.Event()
    original_uuid4 = __import__("uuid").uuid4

    def synchronized_uuid4():
        reached_pre_lock.set()
        return original_uuid4()

    monkeypatch.setattr("dcent_voice.recovery.uuid.uuid4", synchronized_uuid4)
    results: list[bool] = []
    store._lock.acquire()
    try:
        writer = threading.Thread(
            target=lambda: results.append(
                store.record("must not return", reason="focus_changed", mode="dictation")
            )
        )
        writer.start()
        assert reached_pre_lock.wait(1.0)
        # RLock lets this thread apply the purge while the writer is queued.
        store.update_policy(RecoveryConfig(enabled=False))
    finally:
        store._lock.release()
    writer.join(timeout=1.0)

    assert not writer.is_alive()
    assert results == [False]
    assert not store.path.exists()
    assert store.snapshot()["enabled"] is False


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"enabled": "yes"}, "recovery.enabled"),
        ({"max_items": 0}, "recovery.max_items"),
        ({"max_items": 51}, "recovery.max_items"),
        ({"max_age_hours": 0}, "recovery.max_age_hours"),
        ({"max_age_hours": 169}, "recovery.max_age_hours"),
    ],
)
def test_recovery_config_validation_fails_closed(table: dict[str, object], message: str) -> None:
    raw = {
        "active_profile": "desktop",
        "profile": {"desktop": {"asr": "faster-whisper:tiny.en", "llm": "none"}},
        "recovery": table,
    }
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_config_snapshot_and_round_trip_include_recovery(tmp_path: Path) -> None:
    source = Path("config.example.toml")
    target = tmp_path / "config.toml"
    target.write_bytes(source.read_bytes())
    config = load_config(target, create=False)
    api = SettingsApi(
        config=config,
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
        recovery_store=RecoveryStore(path=tmp_path / "vault.json"),
    )

    updated = api.set_recovery_policy(True, 5, 72)
    reloaded = load_config(target, create=False)

    assert updated["enabled"] is True
    assert updated["retention"] == {"max_items": 5, "max_age_hours": 72}
    assert reloaded.recovery == RecoveryConfig(enabled=True, max_items=5, max_age_hours=72)
    assert config_snapshot(reloaded)["recovery"] == {
        "enabled": True,
        "max_items": 5,
        "max_age_hours": 72,
    }


def test_settings_recovery_copy_is_explicit_and_delete_is_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path("config.example.toml")
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(source.read_bytes())
    config = load_config(config_path, create=False)
    store = RecoveryStore(path=tmp_path / "vault.json", enabled=True)
    assert store.record("copy this only on click", reason="focus_changed", mode="dictation")
    entry_id = store.snapshot()["entries"][0]["id"]
    copied: list[str] = []
    monkeypatch.setattr("dcent_voice.inject.clipboard.set_clipboard_text", copied.append)
    api = SettingsApi(
        config=replace(config, recovery=RecoveryConfig(enabled=True)),
        bus=EventBus(),
        privacy=PrivacyMonitor.from_config(config),
        recovery_store=store,
    )

    assert copied == []
    assert api.copy_recovery_entry(entry_id)["ok"] is True
    assert copied == ["copy this only on click"]
    assert api.delete_recovery_entry(entry_id)["entry_count"] == 0


def test_settings_ui_discloses_opt_in_and_success_audio_exclusions() -> None:
    html = Path("src/dcent_voice/ui/web/settings/index.html").read_text(encoding="utf-8")
    script = Path("src/dcent_voice/ui/web/settings/settings.js").read_text(encoding="utf-8")

    assert "Off by default" in html
    assert "Successful dictation and microphone audio are never stored" in html
    assert '"set_recovery_policy"' in script
    assert "window.confirm" in script
    assert 'if (name === "get_recovery_status")' in script
    assert "Recovery disabled, but retained text could not be purged" in script
