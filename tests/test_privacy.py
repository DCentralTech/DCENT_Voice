# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
from pathlib import Path

import pytest

from dcent_voice.config import load_config
from dcent_voice.privacy import (
    EGRESS_TAIL_MAX_SCAN_BYTES,
    ConsentLedger,
    ConsentRequired,
    EgressLog,
    PrivacyMonitor,
    PrivacyStatus,
)


def _grant_consent_worker(path: str, provider_key: str, start) -> None:
    start.wait()
    ConsentLedger(Path(path)).grant(provider_key, payload_type="text")


def _revoke_consent_worker(path: str, provider_key: str, start) -> None:
    start.wait()
    ConsentLedger(Path(path)).revoke(provider_key)


def _record_egress_worker(path: str, worker: int, count: int, start) -> None:
    start.wait()
    log = EgressLog(Path(path))
    for sequence in range(count):
        log.record(f"llm:worker-{worker}", payload_type="text", byte_count=sequence)


def test_local_profile_is_sovereign() -> None:
    config = load_config(Path("config.example.toml"), create=False)

    monitor = PrivacyMonitor.from_config(config)

    assert monitor.status is PrivacyStatus.SOVEREIGN
    assert monitor.missing_consents() == ()


def test_cloud_profile_requires_consent(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "cloud"

[privacy]
consent_ledger_path = ""
egress_log_path = ""

[profile.cloud]
asr = "deepgram:nova-3"
llm = "none"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    ledger = ConsentLedger(tmp_path / "consent.json")
    monitor = PrivacyMonitor.from_config(config)
    monitor.ledger = ledger

    assert monitor.status is PrivacyStatus.CLOUD
    with pytest.raises(ConsentRequired):
        monitor.validate_cloud_consent()

    ledger.grant("asr:deepgram", payload_type="audio")
    monitor.validate_cloud_consent()


def test_cloud_llm_builder_requires_consent_and_rechecks_on_each_egress(
    tmp_path: Path, monkeypatch
) -> None:
    from dcent_voice import app

    consent = tmp_path / "consent.json"
    egress = tmp_path / "egress.jsonl"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''\
active_profile = "cloud"

[privacy]
consent_ledger_path = "{consent.as_posix()}"
egress_log_path = "{egress.as_posix()}"

[profile.cloud]
asr = "faster-whisper:tiny.en:cpu-int8"
llm = "openai:gpt-test"
cleanup_enabled = true
''',
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    monitor = PrivacyMonitor.from_config(config)
    provider_builds = 0
    captured_logger = None

    class FakeCloudLlm:
        def __init__(self, _spec, *, api_key=None, egress_logger=None) -> None:
            nonlocal provider_builds, captured_logger
            provider_builds += 1
            captured_logger = egress_logger

    monkeypatch.setattr(app, "OpenAICompatProvider", FakeCloudLlm)

    with pytest.raises(ConsentRequired, match="llm:openai"):
        app.build_llm_provider(config, monitor)
    assert provider_builds == 0
    assert not egress.exists()

    monitor.ledger.grant("llm:openai", payload_type="text")
    app.build_llm_provider(config, monitor)
    assert provider_builds == 1
    assert callable(captured_logger)
    captured_logger("llm:openai", "text", 123)
    before = egress.read_text(encoding="utf-8")
    assert "transcript" not in before

    monitor.ledger.revoke("llm:openai")
    with pytest.raises(ConsentRequired, match="llm:openai"):
        captured_logger("llm:openai", "text", 456)
    assert egress.read_text(encoding="utf-8") == before


def test_consent_ledger_persists(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ConsentLedger(path)

    ledger.grant("llm:openai", payload_type="text", policy_url="https://example.test")

    assert ConsentLedger(path).has_consent("llm:openai")


def test_concurrent_thread_grants_do_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    thread_count = 12
    start = threading.Barrier(thread_count)

    def grant(index: int) -> None:
        start.wait()
        ConsentLedger(path).grant(f"llm:thread-{index}", payload_type="text")

    threads = [threading.Thread(target=grant, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert set(ConsentLedger(path).entries()) == {
        f"llm:thread-{index}" for index in range(thread_count)
    }


def test_concurrent_grants_and_revoke_preserve_every_update(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ConsentLedger(path)
    ledger.grant("llm:keep", payload_type="text")
    ledger.grant("llm:remove", payload_type="text")
    grant_count = 8
    start = threading.Barrier(grant_count + 1)

    def grant(index: int) -> None:
        start.wait()
        ConsentLedger(path).grant(f"llm:new-{index}", payload_type="text")

    def revoke() -> None:
        start.wait()
        ConsentLedger(path).revoke("llm:remove")

    threads = [threading.Thread(target=grant, args=(index,)) for index in range(grant_count)]
    threads.append(threading.Thread(target=revoke))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    entries = ConsentLedger(path).entries()
    assert "llm:keep" in entries
    assert "llm:remove" not in entries
    assert {f"llm:new-{index}" for index in range(grant_count)} <= set(entries)


def test_concurrent_process_grants_and_revoke_do_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ConsentLedger(path)
    ledger.grant("llm:keep", payload_type="text")
    ledger.grant("llm:remove", payload_type="text")
    grant_count = 3
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_grant_consent_worker,
            args=(str(path), f"llm:process-{index}", start),
        )
        for index in range(grant_count)
    ]
    processes.append(
        context.Process(
            target=_revoke_consent_worker,
            args=(str(path), "llm:remove", start),
        )
    )
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)

    try:
        assert not any(process.is_alive() for process in processes)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    entries = ConsentLedger(path).entries()
    assert "llm:keep" in entries
    assert "llm:remove" not in entries
    assert {f"llm:process-{index}" for index in range(grant_count)} <= set(entries)


def test_failed_atomic_replace_preserves_previous_consent_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "ledger.json"
    ledger = ConsentLedger(path)
    ledger.grant("llm:existing", payload_type="text")
    before = path.read_bytes()
    original_replace = Path.replace

    def fail_publish(self: Path, target: Path) -> Path:
        if target == path and self.name.endswith(".tmp"):
            raise OSError("simulated atomic publish failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_publish)

    with pytest.raises(OSError, match="simulated atomic publish failure"):
        ledger.grant("llm:new", payload_type="text")

    assert path.read_bytes() == before
    assert ConsentLedger(path).has_consent("llm:existing", payload_type="text")
    assert not ConsentLedger(path).has_consent("llm:new", payload_type="text")
    assert list(tmp_path.glob(".ledger.json.*.tmp")) == []


def test_consent_publish_fsyncs_temp_before_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "ledger.json"
    ledger = ConsentLedger(path)
    ledger.grant("llm:existing", payload_type="text")
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = Path.replace

    def tracked_fsync(fd: int) -> None:
        events.append("fsync")
        original_fsync(fd)

    def tracked_replace(self: Path, target: Path) -> Path:
        if target == path and self.name.endswith(".tmp"):
            assert events and events[-1] == "fsync"
            events.append("replace")
        return original_replace(self, target)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(Path, "replace", tracked_replace)

    ledger.grant("llm:new", payload_type="text")

    assert "replace" in events


def test_cloud_consent_is_bound_to_the_approved_payload_type(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "cloud"

[privacy]
consent_ledger_path = ""
egress_log_path = ""

[profile.cloud]
asr = "deepgram:nova-3"
llm = "none"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant("asr:deepgram", payload_type="text")
    monitor = PrivacyMonitor.from_config(config)
    monitor.ledger = ledger

    assert ledger.has_consent("asr:deepgram", payload_type="text") is True
    assert ledger.has_consent("asr:deepgram", payload_type="audio") is False
    with pytest.raises(ConsentRequired):
        monitor.validate_cloud_consent()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {
            "asr:deepgram": {
                "provider_key": "llm:deepgram",
                "accepted_at": 1,
                "payload_type": "audio",
            }
        },
        {
            "asr:deepgram": {
                "provider_key": "asr:deepgram",
                "accepted_at": float("nan"),
                "payload_type": "audio",
            }
        },
        {
            "asr:deepgram": {
                "provider_key": "asr:deepgram",
                "accepted_at": 1,
                "payload_type": "",
            }
        },
    ],
)
def test_malformed_consent_records_fail_closed(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert ConsentLedger(path).has_consent("asr:deepgram", payload_type="audio") is False


def test_egress_log_records_without_content(tmp_path: Path) -> None:
    log = EgressLog(tmp_path / "egress.jsonl")

    log.record("llm:openai", payload_type="text", byte_count=123)
    entries = log.tail()

    assert entries[0].provider_key == "llm:openai"
    assert entries[0].payload_type == "text"
    assert entries[0].byte_count == 123
    assert "content" not in (tmp_path / "egress.jsonl").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not available")
def test_egress_log_is_owner_only_and_repairs_legacy_mode(tmp_path: Path) -> None:
    path = tmp_path / "egress.jsonl"
    log = EgressLog(path)
    previous_umask = os.umask(0o022)
    try:
        log.record("llm:openai", payload_type="text", byte_count=1)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o644)
    log.record("llm:openai", payload_type="text", byte_count=2)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.byte_count for entry in log.tail()] == [1, 2]


def test_concurrent_process_egress_records_are_complete_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "egress.jsonl"
    process_count = 4
    records_per_process = 20
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_record_egress_worker,
            args=(str(path), index, records_per_process, start),
        )
        for index in range(process_count)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)

    try:
        assert not any(process.is_alive() for process in processes)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == process_count * records_per_process
    assert all(isinstance(json.loads(line), dict) for line in raw_lines)
    assert len(EgressLog(path).tail(limit=1000)) == process_count * records_per_process


def test_egress_tail_scans_only_bounded_suffix(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "egress.jsonl"
    path.write_bytes(b"x" * (EGRESS_TAIL_MAX_SCAN_BYTES + 4096) + b"\n")
    log = EgressLog(path)
    for index in range(5):
        log.record("llm:openai", payload_type="text", byte_count=index)

    def reject_unbounded_read(*_args, **_kwargs):
        raise AssertionError("tail must not load the full log with Path.read_text")

    monkeypatch.setattr(Path, "read_text", reject_unbounded_read)

    assert [entry.byte_count for entry in log.tail(limit=3)] == [2, 3, 4]
    assert log.tail(limit=0) == []


def test_egress_tail_skips_partial_crash_record(tmp_path: Path) -> None:
    path = tmp_path / "egress.jsonl"
    log = EgressLog(path)
    log.record("llm:openai", payload_type="text", byte_count=7)
    with path.open("ab") as handle:
        handle.write(b'{"timestamp":')

    entries = log.tail()

    assert len(entries) == 1
    assert entries[0].byte_count == 7

    log.record("llm:openai", payload_type="text", byte_count=8)

    assert [entry.byte_count for entry in log.tail()] == [7, 8]
