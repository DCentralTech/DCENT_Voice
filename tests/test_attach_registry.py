# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dcent_voice.attach.registry import (
    build_launch_descriptor,
    create_registry_entry,
    is_pid_running,
    read_registry_entry,
    remove_registry_entry,
    remove_stale_registry_entries,
    restrict_private_file,
    write_install_manifest,
    write_registry_entry,
    write_text_atomic,
)


def test_is_pid_running_detects_live_and_dead_pids() -> None:
    # Regression: on Windows os.kill(pid, 0) is not a liveness check and raised
    # an uncatchable SystemError, crashing single-instance startup on a stale
    # lock. is_pid_running must answer cleanly on every platform.
    assert is_pid_running(os.getpid()) is True
    assert is_pid_running(999_999_999) is False
    assert is_pid_running(0) is False
    assert is_pid_running(-1) is False


def test_registry_entry_writes_atomically_with_token_ref(tmp_path) -> None:
    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        pid=os.getpid(),
        token="test-token",
    )

    path = write_registry_entry(entry, registry_dir=tmp_path)
    parsed = read_registry_entry(path)

    assert parsed.moduleId == "dcent-voice"
    assert parsed.sovereigntyClass == "LOCAL"
    assert "stt.final" in parsed.capabilities
    assert parsed.tokenRef.endswith("dcent-voice.token")
    assert (tmp_path / "dcent-voice.token").read_text(encoding="utf-8") == "test-token"
    assert not list(tmp_path.glob("*.tmp"))


def test_registry_entry_marks_wildcard_bind_as_lan_without_changing_capability_class(
    tmp_path,
) -> None:
    entry = create_registry_entry(
        endpoint="http://0.0.0.0:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        token="test-token",
    )

    assert entry.sovereigntyClass == "LAN"
    stt = next(block for block in entry.capabilitySovereignty if block["capability"] == "stt.final")
    assert stt["sovereigntyClass"] == "LOCAL"


def test_persistent_install_manifest_has_only_manifest_owned_launch_data(tmp_path) -> None:
    path = write_install_manifest(registry_dir=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload == {"moduleId": "dcent-voice", "launch": build_launch_descriptor()}
    assert "tokenRef" not in payload
    assert "pid" not in payload


def test_registry_removes_stale_pid_entries(tmp_path) -> None:
    stale = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        pid=999_999_999,
        token="stale-token",
    )
    path = write_registry_entry(stale, registry_dir=tmp_path)
    token_path = tmp_path / "dcent-voice.token"
    assert token_path.exists()

    removed = remove_stale_registry_entries(registry_dir=tmp_path)

    assert removed == [path]
    assert not path.exists()
    assert not token_path.exists()


def test_registry_remove_deletes_json_and_token(tmp_path) -> None:
    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        token="remove-token",
    )
    path = write_registry_entry(entry, registry_dir=tmp_path)

    remove_registry_entry(entry, registry_dir=tmp_path)

    assert not path.exists()
    assert not (tmp_path / "dcent-voice.token").exists()


def test_token_write_restricts_private_permissions(tmp_path) -> None:
    """W1-F6: create_registry_entry → write_text_atomic → restrict_private_file.

    Asserts the shipped path applies owner-only style permissions on the token
    file (POSIX mode 0o600; Windows icacls user grant after inheritance strip).
    """
    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        pid=os.getpid(),
        token="acl-test-token",
    )
    token_path = tmp_path / "dcent-voice.token"
    assert token_path.is_file()
    assert token_path.read_text(encoding="utf-8") == "acl-test-token"
    assert entry.tokenRef == str(token_path)

    if sys.platform != "win32":
        mode = stat.S_IMODE(token_path.stat().st_mode)
        assert mode == 0o600, f"expected owner-only mode 0o600, got {oct(mode)}"
        return

    # Windows: restrict_private_file runs icacls /inheritance:r /grant:r USER:(R,W).
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    assert user, "USERNAME required to verify Windows ACL grant"
    listed = subprocess.run(
        ["icacls", str(token_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = listed.stdout
    assert user.lower() in out.lower(), f"token ACL missing user grant:\n{out}"
    # After inheritance:r, ACEs are explicit (not inherited). icacls marks
    # inherited entries with (I); none should remain for a locked-down secret.
    for line in out.splitlines():
        if token_path.name in line:
            continue
        if line.strip().startswith(str(token_path)):
            continue
        assert "(I)" not in line, f"inherited ACE still present after restrict:\n{out}"


def test_write_text_atomic_calls_restrict_private_file(tmp_path) -> None:
    """Direct unit of the atomic-write → ACL seam used for tokens and registry."""
    path = tmp_path / "session.token"
    write_text_atomic(path, "top-secret")
    assert path.read_text(encoding="utf-8") == "top-secret"
    # Re-apply explicitly to prove the public helper is the same shipped function.
    restrict_private_file(path)
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        listed = subprocess.run(
            ["icacls", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert user and user.lower() in listed.stdout.lower()


def test_token_publication_fails_closed_on_posix_chmod_error(tmp_path, monkeypatch) -> None:
    from dcent_voice.attach import registry as registry_module

    monkeypatch.setattr(registry_module.sys, "platform", "linux")

    def deny_chmod(_path, _mode) -> None:
        raise PermissionError("simulated chmod denial")

    monkeypatch.setattr(registry_module.os, "chmod", deny_chmod)

    with pytest.raises(PermissionError, match="simulated chmod denial"):
        create_registry_entry(
            endpoint="http://127.0.0.1:8765",
            version="0.1.0",
            registry_dir=tmp_path,
            token="must-not-publish",
        )

    assert not (tmp_path / "dcent-voice.token").exists()
    assert not list(tmp_path.glob(".dcent-voice.token.*.tmp"))


def test_private_atomic_write_restricts_empty_temp_before_writing_contents(
    tmp_path, monkeypatch
) -> None:
    from dcent_voice.attach import registry as registry_module

    observed_contents: list[bytes] = []

    def reject_acl(path: Path) -> None:
        observed_contents.append(path.read_bytes())
        raise PermissionError("simulated pre-write ACL failure")

    monkeypatch.setattr(registry_module, "restrict_private_file", reject_acl)

    with pytest.raises(PermissionError, match="pre-write ACL failure"):
        write_text_atomic(tmp_path / "secret.txt", "must-never-touch-an-unsecured-file")

    assert observed_contents == [b""]
    assert not (tmp_path / "secret.txt").exists()
    assert not list(tmp_path.glob(".secret.txt.*.tmp"))


def test_token_publication_fails_closed_on_windows_icacls_error(tmp_path, monkeypatch) -> None:
    from dcent_voice.attach import registry as registry_module

    monkeypatch.setattr(registry_module.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "acl-test-user")

    def denied_icacls(args, **_kwargs):
        assert _kwargs["creationflags"] == registry_module._CREATE_NO_WINDOW
        return subprocess.CompletedProcess(
            args=args,
            returncode=5,
            stdout="",
            stderr="Access is denied.",
        )

    monkeypatch.setattr(registry_module.subprocess, "run", denied_icacls)

    with pytest.raises(PermissionError, match="Access is denied"):
        create_registry_entry(
            endpoint="http://127.0.0.1:8765",
            version="0.1.0",
            registry_dir=tmp_path,
            token="must-not-publish",
        )

    assert not (tmp_path / "dcent-voice.token").exists()
    assert not list(tmp_path.glob(".dcent-voice.token.*.tmp"))


def test_windows_private_acl_commands_are_hidden_for_write_and_verify(
    tmp_path, monkeypatch
) -> None:
    from dcent_voice.attach import registry as registry_module

    monkeypatch.setattr(registry_module.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "acl-test-user")
    calls: list[dict[str, object]] = []

    def successful_icacls(args, **kwargs):
        calls.append(kwargs)
        stdout = (
            f"{args[1]} acl-test-user:(R,W)\n" if len(args) == 2 else "processed file successfully"
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(registry_module.subprocess, "run", successful_icacls)

    path = tmp_path / "secret.txt"
    write_text_atomic(path, "private")

    assert path.read_text(encoding="utf-8") == "private"
    assert len(calls) == 3  # restrict verification plus final-name verification
    assert all(call["creationflags"] == registry_module._CREATE_NO_WINDOW for call in calls)


def test_non_secret_install_manifest_remains_best_effort_on_acl_failure(
    tmp_path, monkeypatch
) -> None:
    from dcent_voice.attach import registry as registry_module

    def unavailable_acl(_path) -> None:
        raise PermissionError("simulated ACL tool failure")

    monkeypatch.setattr(registry_module, "restrict_private_file", unavailable_acl)

    path = write_install_manifest(registry_dir=tmp_path)

    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["moduleId"] == "dcent-voice"


def test_live_registry_publication_removes_file_when_final_acl_verification_fails(
    tmp_path, monkeypatch
) -> None:
    from dcent_voice.attach import registry as registry_module

    entry = create_registry_entry(
        endpoint="http://127.0.0.1:8765",
        version="0.1.0",
        registry_dir=tmp_path,
        token="secure-token",
    )
    monkeypatch.setattr(registry_module, "restrict_private_file", lambda _path: None)

    def fail_verification(_path) -> None:
        raise PermissionError("simulated final ACL verification failure")

    monkeypatch.setattr(registry_module, "_verify_private_file", fail_verification)

    with pytest.raises(PermissionError, match="final ACL verification failure"):
        write_registry_entry(entry, registry_dir=tmp_path)

    assert not (tmp_path / "dcent-voice.json").exists()
    assert not list(tmp_path.glob(".dcent-voice.json.*.tmp"))
