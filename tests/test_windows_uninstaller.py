# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dcent_voice.util.owned_process import start_owned_process, terminate_owned_process

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows uninstaller"),
    pytest.mark.interactive,
]

_OWNED_PROCESSES: list[subprocess.Popen[Any]] = []
_REGISTRY_KEYS: list[str] = []


def _start_test_process(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    process = start_owned_process(command, **kwargs)
    _OWNED_PROCESSES.append(process)
    return process


@pytest.fixture(autouse=True)
def _reap_test_owned_processes() -> Iterator[None]:
    """Close every private process boundary even when setup/assertions fail."""

    assert _OWNED_PROCESSES == []
    try:
        yield
    finally:
        for process in reversed(_OWNED_PROCESSES):
            terminate_owned_process(process, grace_s=0.0, kill_s=5.0)
        _OWNED_PROCESSES.clear()
        for relative in reversed(_REGISTRY_KEYS):
            _delete_registry_tree(relative)
        _REGISTRY_KEYS.clear()


SCRIPT = Path(
    os.environ.get(
        "DCENT_VOICE_UNINSTALL_SCRIPT",
        "packaging/windows/setup-stub/uninstall.ps1",
    )
).resolve()


def _fixture(root: Path) -> None:
    (root / "_internal").mkdir(parents=True)
    (root / "_internal" / "base_library.zip").write_bytes(b"payload")
    (root / "dcent-voice-offline-bundle.json").write_text("{}", encoding="utf-8")


def _registry_fixture() -> tuple[str, str]:
    import winreg

    relative = rf"Software\DCENT_Voice\Tests\Uninstall\{uuid.uuid4().hex}"
    _REGISTRY_KEYS.append(relative)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, relative) as key:
        winreg.SetValueEx(key, "Sentinel", 0, winreg.REG_SZ, "keep-until-success")
    return rf"HKCU:\{relative}", relative


def _delete_registry_tree(relative: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            relative,
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            children: list[str] = []
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                    index += 1
                except OSError:
                    break
        for child in children:
            _delete_registry_tree(relative + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, relative)
    except FileNotFoundError:
        pass


def _registry_exists(relative: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative):
            return True
    except FileNotFoundError:
        return False


def _registry_value(relative: str, name: str) -> str:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative) as key:
        value, _ = winreg.QueryValueEx(key, name)
    return str(value)


def _invoke(
    root: Path,
    programs: Path,
    registry_path: str,
    *,
    grace_ms: int = 200,
    terminate_ms: int = 3000,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-InstallRoot",
            str(root),
            "-ProgramsRoot",
            str(programs),
            "-ModelDataRoot",
            str(root.parent / "DCENT_Voice.Models"),
            "-RegistryPath",
            registry_path,
            "-GraceTimeoutMs",
            str(grace_ms),
            "-TerminateTimeoutMs",
            str(terminate_ms),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _invoke_recovery(relative: str) -> subprocess.CompletedProcess[str]:
    command = _registry_value(relative, "DCENTRecoveryUninstaller")
    return subprocess.run(
        [command, "/S"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _exclusive_holder(path: Path, *, directory: bool = False) -> subprocess.Popen[str]:
    holder_code = r"""
import ctypes, sys, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                          wintypes.HANDLE]
k.CreateFileW.restype = wintypes.HANDLE
flags = 0x02000000 if sys.argv[2] == 'directory' else 0x80
h = k.CreateFileW(sys.argv[1], 0x80000000, 3, None, 3, flags, None)
if h == wintypes.HANDLE(-1).value:
    raise ctypes.WinError(ctypes.get_last_error())
print('READY', flush=True)
time.sleep(30)
"""
    holder = _start_test_process(
        [sys.executable, "-c", holder_code, str(path), "directory" if directory else "file"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "READY"
    return holder


def _exclusive_open_once(path: Path) -> subprocess.CompletedProcess[str]:
    probe_code = r"""
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                          wintypes.HANDLE]
k.CreateFileW.restype = wintypes.HANDLE
k.CloseHandle.argtypes = [wintypes.HANDLE]
h = k.CreateFileW(sys.argv[1], 0x80000000, 0, None, 3, 0x80, None)
if h == wintypes.HANDLE(-1).value:
    print(ctypes.get_last_error())
    raise SystemExit(32)
k.CloseHandle(h)
"""
    return subprocess.run(
        [sys.executable, "-c", probe_code, str(path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def test_uninstaller_stops_only_exact_root_process_and_removes_registration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install root with spaces & apostrophe's"
    programs = tmp_path / "Start Menu" / "DCENT_Voice"
    _fixture(root)
    programs.mkdir(parents=True)
    (programs / "DCENT_Voice.lnk").write_bytes(b"shortcut")
    registry_path, registry_relative = _registry_fixture()

    shutil.copy2(os.environ["COMSPEC"], root / "dcent-voice.exe")
    owned = _start_test_process(
        [str(root / "dcent-voice.exe"), "/d", "/c", "ping", "-t", "127.0.0.1"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    unrelated = _start_test_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        time.sleep(0.2)
        assert owned.poll() is None
        assert unrelated.poll() is None
        result = _invoke(root, programs, registry_path)
        assert result.returncode == 0, result.stderr
        owned.wait(timeout=5)
        assert unrelated.poll() is None
        assert not root.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_uninstaller_foreign_lock_fails_and_preserves_registration(tmp_path: Path) -> None:
    root = tmp_path / "install root held by foreign process"
    programs = tmp_path / "Start Menu retained"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    locked = root / "foreign-lock.dll"
    locked.write_bytes(b"locked")
    programs.mkdir(parents=True)
    (programs / "DCENT_Voice.lnk").write_bytes(b"shortcut")
    registry_path, registry_relative = _registry_fixture()

    holder_code = r"""
import ctypes, sys, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                          wintypes.HANDLE]
k.CreateFileW.restype = wintypes.HANDLE
h = k.CreateFileW(sys.argv[1], 0x80000000, 0, None, 3, 0x80, None)
if h == wintypes.HANDLE(-1).value:
    raise ctypes.WinError(ctypes.get_last_error())
print('READY', flush=True)
time.sleep(30)
"""
    holder = _start_test_process(
        [sys.executable, "-c", holder_code, str(locked)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"
        result = _invoke(root, programs, registry_path)
        assert result.returncode == 30
        assert "installed file is still in use" in result.stderr
        assert holder.poll() is None
        assert root.exists()
        assert (root / "dcent-voice.exe").exists()
        assert (root / "_internal" / "base_library.zip").exists()
        assert locked.exists()
        assert programs.exists()
        assert _registry_exists(registry_relative)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_uninstaller_removes_autostart_but_retains_user_records_by_default(
    tmp_path: Path,
) -> None:
    import winreg

    import keyring

    root = tmp_path / "default retention install"
    programs = tmp_path / "Start Menu default retention"
    user_data = tmp_path / "profile" / "DCENT_Voice"
    model_data = tmp_path / "DCENT_Voice.Models"
    ade_modules = tmp_path / "local" / "DCENT" / "modules"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    programs.mkdir(parents=True)
    user_data.mkdir(parents=True)
    (user_data / "config.toml").write_text("private settings", encoding="utf-8")
    model_data.mkdir()
    (model_data / "custom-model.bin").write_bytes(b"retain custom model")
    ade_modules.mkdir(parents=True)
    (ade_modules / "dcent-voice.install.json").write_text("{}", encoding="utf-8")
    registry_path, registry_relative = _registry_fixture()
    run_relative = rf"Software\DCENT_Voice\Tests\Run\{uuid.uuid4().hex}"
    _REGISTRY_KEYS.append(run_relative)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_relative) as key:
        winreg.SetValueEx(key, "DCENT_Voice", 0, winreg.REG_SZ, str(root / "dcent-voice.exe"))
        winreg.SetValueEx(key, "Foreign", 0, winreg.REG_SZ, "preserve")
    service = "DCENT_Voice-Test-" + uuid.uuid4().hex
    try:
        keyring.set_password(service, "openai:api_key", "retain-secret")
        result = _invoke(
            root,
            programs,
            registry_path,
            extra=(
                "-RunRegistryPath",
                rf"HKCU:\{run_relative}",
                "-UserDataRoot",
                str(user_data),
                "-AdeModulesRoot",
                str(ade_modules),
                "-CredentialService",
                service,
            ),
        )
        assert result.returncode == 0, result.stderr
        assert (user_data / "config.toml").read_text(encoding="utf-8") == "private settings"
        assert (model_data / "custom-model.bin").read_bytes() == b"retain custom model"
        assert keyring.get_password(service, "openai:api_key") == "retain-secret"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_relative) as key:
            assert winreg.QueryValueEx(key, "Foreign")[0] == "preserve"
            with pytest.raises(FileNotFoundError):
                winreg.QueryValueEx(key, "DCENT_Voice")
    finally:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(service, "openai:api_key")
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, run_relative)


def test_uninstaller_removes_both_start_menu_shortcuts_and_the_single_autostart(
    tmp_path: Path,
) -> None:
    """WS5: Setup writes two shortcuts and exactly one autostart; both must go.

    The app entry and the diagnostics entry live in the same Programs folder, so
    removing the folder removes both. Autostart is the HKCU Run value and nothing
    else — the retired Inno script's {userstartup} shortcut is gone, so there is
    no second place a stale launch-at-login entry can hide.
    """
    import winreg

    root = tmp_path / "two shortcut install"
    programs = tmp_path / "Start Menu two shortcuts"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    programs.mkdir(parents=True)
    app_shortcut = programs / "DCENT_Voice.lnk"
    diagnostics_shortcut = programs / "DCENT_Voice Diagnostics.lnk"
    app_shortcut.write_bytes(b"lnk")
    diagnostics_shortcut.write_bytes(b"lnk")
    registry_path, _registry_relative = _registry_fixture()
    run_relative = rf"Software\DCENT_Voice\Tests\Run\{uuid.uuid4().hex}"
    _REGISTRY_KEYS.append(run_relative)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_relative) as key:
        winreg.SetValueEx(key, "DCENT_Voice", 0, winreg.REG_SZ, str(root / "dcent-voice.exe"))
    try:
        result = _invoke(
            root,
            programs,
            registry_path,
            extra=("-RunRegistryPath", rf"HKCU:\{run_relative}"),
        )
        assert result.returncode == 0, result.stderr
        assert not app_shortcut.exists()
        assert not diagnostics_shortcut.exists()
        assert not programs.exists()
        with (
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_relative) as key,
            pytest.raises(FileNotFoundError),
        ):
            winreg.QueryValueEx(key, "DCENT_Voice")
    finally:
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, run_relative)


def test_uninstaller_explicit_purge_removes_user_data_ade_and_credentials(
    tmp_path: Path,
) -> None:
    import keyring

    root = tmp_path / "purge install"
    programs = tmp_path / "Start Menu purge"
    user_data = tmp_path / "profile" / "DCENT_Voice"
    model_data = tmp_path / "DCENT_Voice.Models"
    ade_modules = tmp_path / "local" / "DCENT" / "modules"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    programs.mkdir(parents=True)
    user_data.mkdir(parents=True)
    (user_data / "consent.jsonl").write_text("private consent", encoding="utf-8")
    model_data.mkdir()
    (model_data / "custom-model.bin").write_bytes(b"purge custom model")
    ade_modules.mkdir(parents=True)
    for name in ("dcent-voice.json", "dcent-voice.token", "dcent-voice.install.json"):
        (ade_modules / name).write_text("secret" if name.endswith(".token") else "{}")
    registry_path, _registry_relative = _registry_fixture()
    service = "DCENT_Voice-Test-" + uuid.uuid4().hex
    try:
        keyring.set_password(service, "openai:api_key", "purge-secret")
        result = _invoke(
            root,
            programs,
            registry_path,
            extra=(
                "-PurgeUserData",
                "-UserDataRoot",
                str(user_data),
                "-AdeModulesRoot",
                str(ade_modules),
                "-CredentialService",
                service,
            ),
        )
        assert result.returncode == 0, result.stderr
        assert not user_data.exists()
        assert not model_data.exists()
        assert not ade_modules.exists()
        assert keyring.get_password(service, "openai:api_key") is None
    finally:
        if keyring.get_password(service, "openai:api_key") is not None:
            keyring.delete_password(service, "openai:api_key")


def test_retained_tree_blocks_post_snapshot_lock_before_any_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "transactional install root"
    programs = tmp_path / "Start Menu retained until payload deletion"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    marker = root / "__dcent_uninstall_test_delete_first__"
    marker.write_bytes(b"delete-first")
    late = root / "zz-late-lock.bin"
    late.write_bytes(b"locked-after-deletion-started")
    early = root / "early"
    early.mkdir()
    for index in range(200):
        (early / f"{index:04d}.bin").write_bytes(b"payload")
    programs.mkdir(parents=True)
    (programs / "DCENT_Voice.lnk").write_bytes(b"shortcut")
    registry_path, registry_relative = _registry_fixture()
    started = tmp_path / "deletion-started.signal"
    proceed = tmp_path / "continue.signal"

    command = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-InstallRoot",
        str(root),
        "-ProgramsRoot",
        str(programs),
        "-RegistryPath",
        registry_path,
        "-GraceTimeoutMs",
        "200",
        "-TerminateTimeoutMs",
        "3000",
        "-TestDeleteStartedSignal",
        str(started),
        "-TestDeleteContinueSignal",
        str(proceed),
    ]
    uninstall = _start_test_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 15
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists(), "uninstaller never reached deletion-started barrier"
        state_path = Path(_registry_value(registry_relative, "DCENTRecoveryState"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        tombstone = Path(state["TombstonePath"])
        assert not root.exists()
        assert tombstone.exists()
        assert (tombstone / marker.name).exists()
        assert (tombstone / late.name).exists()
        blocked = _exclusive_open_once(tombstone / late.name)
        assert blocked.returncode == 32, (blocked.stdout, blocked.stderr)
        assert blocked.stdout.strip() in {"5", "32"}
        proceed.write_text("continue", encoding="utf-8")
        stdout, stderr = uninstall.communicate(timeout=20)
        assert uninstall.returncode == 0, (stdout, stderr)
        assert not _registry_exists(registry_relative)
        assert not programs.exists()
        assert not tombstone.exists()
        assert not root.exists()
    finally:
        if uninstall.poll() is None:
            uninstall.kill()
        uninstall.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_entire_child_subtree_is_pinned_at_deletion_barrier(tmp_path: Path) -> None:
    root = tmp_path / "child pin source"
    programs = tmp_path / "Start Menu child pin"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    (root / "__dcent_uninstall_test_delete_first__").write_bytes(b"barrier")
    victim = root / "victim" / "nested"
    victim.mkdir(parents=True)
    for index in range(200):
        (victim / f"{index:04d}.bin").write_bytes(b"original")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    started = tmp_path / "child-pinned.signal"
    proceed = tmp_path / "child-pinned-continue.signal"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-InstallRoot",
        str(root),
        "-ProgramsRoot",
        str(programs),
        "-RegistryPath",
        registry_path,
        "-TestDeleteStartedSignal",
        str(started),
        "-TestDeleteContinueSignal",
        str(proceed),
    ]
    uninstall = _start_test_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    escaped = tmp_path / "escaped-original-child"
    attacker_stop = threading.Event()
    attacker_success: list[bool] = []
    attacker: threading.Thread | None = None
    try:
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        state = json.loads(
            Path(_registry_value(registry_relative, "DCENTRecoveryState")).read_text(
                encoding="utf-8"
            )
        )
        tombstone = Path(state["TombstonePath"])
        pinned_victim = tombstone / "victim"
        with pytest.raises(OSError) as rename_error:
            pinned_victim.rename(escaped)
        assert rename_error.value.winerror in {5, 32}
        assert pinned_victim.exists()
        assert not escaped.exists()

        def attack_child_rename() -> None:
            while not attacker_stop.is_set():
                try:
                    pinned_victim.rename(escaped)
                    attacker_success.append(True)
                    return
                except OSError:
                    time.sleep(0.001)

        attacker = threading.Thread(target=attack_child_rename, daemon=True)
        attacker.start()
        time.sleep(0.2)
        attacker_stop.set()
        attacker.join(timeout=2)
        assert not attacker_success
        assert pinned_victim.exists()
        assert not escaped.exists()
        proceed.write_text("continue", encoding="utf-8")
        stdout, stderr = uninstall.communicate(timeout=30)
        assert uninstall.returncode == 0, (stdout, stderr)
        assert not root.exists()
        assert not tombstone.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        attacker_stop.set()
        if attacker is not None:
            attacker.join(timeout=2)
        if uninstall.poll() is None:
            uninstall.kill()
        uninstall.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(escaped, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


@pytest.mark.parametrize(
    ("relative", "replacement"),
    [
        (Path("victim"), True),
        (Path("outer/middle/victim"), True),
        (Path("removed-victim"), False),
    ],
)
def test_child_swap_between_native_enumeration_and_open_fails_closed(
    tmp_path: Path,
    relative: Path,
    replacement: bool,
) -> None:
    case = f"{len(relative.parts)}-{int(replacement)}"
    root = tmp_path / f"enumeration race {case}"
    programs = tmp_path / f"Start Menu enumeration race {case}"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    victim = root / relative / "nested"
    victim.mkdir(parents=True)
    for index in range(200):
        (victim / f"{index:04d}.bin").write_bytes(b"original")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    enumerated = tmp_path / f"enumerated-{case}.signal"
    proceed = tmp_path / f"enumerated-{case}-continue.signal"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-InstallRoot",
        str(root),
        "-ProgramsRoot",
        str(programs),
        "-RegistryPath",
        registry_path,
        "-TestEntryEnumeratedRelativePath",
        str(relative),
        "-TestEntryEnumeratedPhase",
        "tombstone",
        "-TestEntryEnumeratedSignal",
        str(enumerated),
        "-TestEntryEnumeratedContinueSignal",
        str(proceed),
    ]
    uninstall = _start_test_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    escaped = tmp_path / f"escaped-original-{case}"
    attacker_saved = tmp_path / f"attacker-replacement-{case}"
    try:
        deadline = time.monotonic() + 20
        while not enumerated.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert enumerated.exists(), "uninstaller never reached enum/open race barrier"
        state_path = Path(_registry_value(registry_relative, "DCENTRecoveryState"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        tombstone = Path(state["TombstonePath"])
        original = tombstone / relative
        original.rename(escaped)
        if replacement:
            original.mkdir(parents=True)
            (original / "attacker-replacement.txt").write_bytes(b"replacement")
        proceed.write_text("continue", encoding="utf-8")
        stdout, stderr = uninstall.communicate(timeout=30)
        assert uninstall.returncode == 30, (stdout, stderr)
        assert (
            "identity changed before handle acquisition" in stderr
            if replacement
            else "cannot find" in stderr.lower()
        )
        assert sum(1 for _ in escaped.rglob("*")) == 201
        if replacement:
            assert (original / "attacker-replacement.txt").read_bytes() == b"replacement"
        assert tombstone.exists()
        assert programs.exists()
        assert _registry_exists(registry_relative)

        if replacement:
            original.rename(attacker_saved)
        escaped.rename(original)
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 0, retry.stderr
        if replacement:
            assert (attacker_saved / "attacker-replacement.txt").read_bytes() == b"replacement"
        assert not tombstone.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        if uninstall.poll() is None:
            uninstall.kill()
        uninstall.wait(timeout=5)
        for candidate in (root, escaped, attacker_saved, programs):
            shutil.rmtree(candidate, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_new_child_after_closed_world_validation_fails_with_recovery_retained(
    tmp_path: Path,
) -> None:
    root = tmp_path / "post validation insertion"
    programs = tmp_path / "Start Menu post validation insertion"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    (root / "__dcent_uninstall_test_delete_first__").write_bytes(b"barrier")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    started = tmp_path / "validated.signal"
    proceed = tmp_path / "validated-continue.signal"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-InstallRoot",
        str(root),
        "-ProgramsRoot",
        str(programs),
        "-RegistryPath",
        registry_path,
        "-TestDeleteStartedSignal",
        str(started),
        "-TestDeleteContinueSignal",
        str(proceed),
    ]
    uninstall = _start_test_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        state = json.loads(
            Path(_registry_value(registry_relative, "DCENTRecoveryState")).read_text(
                encoding="utf-8"
            )
        )
        tombstone = Path(state["TombstonePath"])
        inserted = tombstone / "attacker-added-after-validation.bin"
        inserted.write_bytes(b"must-not-be-deleted")
        proceed.write_text("continue", encoding="utf-8")
        stdout, stderr = uninstall.communicate(timeout=30)
        assert uninstall.returncode == 30, (stdout, stderr)
        assert "remained non-empty" in stderr
        assert inserted.read_bytes() == b"must-not-be-deleted"
        assert tombstone.exists()
        assert programs.exists()
        assert _registry_exists(registry_relative)
        inserted.unlink()
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 0, retry.stderr
        assert not tombstone.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        if uninstall.poll() is None:
            uninstall.kill()
        uninstall.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("-TestMaxPinnedEntries", "4"), "entry count exceeds"),
        (("-TestMaxPinnedDepth", "2"), "depth exceeds"),
    ],
)
def test_retained_tree_resource_bounds_fail_before_quarantine(
    tmp_path: Path,
    extra: tuple[str, str],
    message: str,
) -> None:
    root = tmp_path / f"resource bound {extra[0]}"
    programs = tmp_path / f"Start Menu resource bound {extra[0]}"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    deep = root / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "payload.bin").write_bytes(b"payload")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    try:
        result = _invoke(root, programs, registry_path, extra=extra)
        assert result.returncode == 30
        assert message in result.stderr
        assert root.exists()
        assert programs.exists()
        assert _registry_exists(registry_relative)
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 0, retry.stderr
        assert not root.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_pinned_tombstone_cannot_be_swapped_at_deletion_barrier(tmp_path: Path) -> None:
    root = tmp_path / "pinned tombstone source"
    programs = tmp_path / "Start Menu pinned tombstone"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    (root / "__dcent_uninstall_test_delete_first__").write_bytes(b"barrier")
    (root / "payload.bin").write_bytes(b"original-payload")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    started = tmp_path / "pinned-deletion.signal"
    proceed = tmp_path / "pinned-continue.signal"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-InstallRoot",
        str(root),
        "-ProgramsRoot",
        str(programs),
        "-RegistryPath",
        registry_path,
        "-TestDeleteStartedSignal",
        str(started),
        "-TestDeleteContinueSignal",
        str(proceed),
    ]
    uninstall = _start_test_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    moved = tmp_path / "attacker-moved-original"
    attacker_stop = threading.Event()
    attacker_success: list[bool] = []
    attacker_errors: list[int | None] = []
    attacker: threading.Thread | None = None
    try:
        deadline = time.monotonic() + 15
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        state_path = Path(_registry_value(registry_relative, "DCENTRecoveryState"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        tombstone = Path(state["TombstonePath"])
        assert tombstone.exists()
        with pytest.raises(OSError) as rename_error:
            tombstone.rename(moved)
        assert rename_error.value.winerror in {5, 32}
        assert tombstone.exists()
        assert not moved.exists()
        with pytest.raises(FileExistsError):
            tombstone.mkdir()

        def attack_rename() -> None:
            while not attacker_stop.is_set():
                try:
                    tombstone.rename(moved)
                    attacker_success.append(True)
                    return
                except OSError as exc:
                    attacker_errors.append(exc.winerror)
                    time.sleep(0.001)

        attacker = threading.Thread(target=attack_rename, daemon=True)
        attacker.start()
        proceed.write_text("continue", encoding="utf-8")
        stdout, stderr = uninstall.communicate(timeout=20)
        attacker_stop.set()
        attacker.join(timeout=2)
        assert uninstall.returncode == 0, (stdout, stderr)
        assert not attacker_success
        assert attacker_errors
        assert set(attacker_errors) <= {2, 3, 5, 32}
        assert not root.exists()
        assert not tombstone.exists()
        assert not moved.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        attacker_stop.set()
        if attacker is not None:
            attacker.join(timeout=2)
        if uninstall.poll() is None:
            uninstall.kill()
        uninstall.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(moved, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


@pytest.mark.parametrize("disposition_mode", ["auto", "basic"])
def test_final_root_disposition_is_object_bound(
    tmp_path: Path,
    disposition_mode: str,
) -> None:
    root = tmp_path / f"final root {disposition_mode}"
    programs = tmp_path / f"Start Menu final {disposition_mode}"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    disposed = tmp_path / f"disposed-{disposition_mode}.signal"
    proceed = tmp_path / f"continue-{disposition_mode}.signal"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-InstallRoot",
        str(root),
        "-ProgramsRoot",
        str(programs),
        "-RegistryPath",
        registry_path,
        "-TestDispositionMode",
        disposition_mode,
        "-TestFinalDispositionSignal",
        str(disposed),
        "-TestFinalDispositionContinueSignal",
        str(proceed),
    ]
    uninstall = _start_test_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    replacement_created = False
    try:
        deadline = time.monotonic() + 15
        while not disposed.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert disposed.exists()
        state_path = Path(_registry_value(registry_relative, "DCENTRecoveryState"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        tombstone = Path(state["TombstonePath"])
        if tombstone.exists():
            moved = tmp_path / f"final-move-{disposition_mode}"
            with pytest.raises(OSError) as rename_error:
                tombstone.rename(moved)
            assert rename_error.value.winerror in {5, 32}
            assert not moved.exists()
        else:
            tombstone.mkdir()
            (tombstone / "attacker-replacement.txt").write_text(
                "must not be deleted",
                encoding="utf-8",
            )
            replacement_created = True
        proceed.write_text("continue", encoding="utf-8")
        stdout, stderr = uninstall.communicate(timeout=20)
        assert uninstall.returncode == 0, (stdout, stderr)
        assert not root.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
        if replacement_created:
            assert (tombstone / "attacker-replacement.txt").read_text(
                encoding="utf-8"
            ) == "must not be deleted"
        else:
            assert not tombstone.exists()
    finally:
        if uninstall.poll() is None:
            uninstall.kill()
        uninstall.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_unsupported_handle_disposition_retains_recovery(tmp_path: Path) -> None:
    root = tmp_path / "unsupported disposition"
    programs = tmp_path / "Start Menu unsupported disposition"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    try:
        result = _invoke(
            root,
            programs,
            registry_path,
            extra=("-TestDispositionMode", "unsupported"),
        )
        assert result.returncode == 30
        assert "handle-pinned quarantined payload" in result.stderr
        state_path = Path(_registry_value(registry_relative, "DCENTRecoveryState"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        tombstone = Path(state["TombstonePath"])
        recovery = Path(state["RecoveryRoot"])
        assert not root.exists()
        assert tombstone.exists()
        assert recovery.exists()
        assert programs.exists()
        assert _registry_exists(registry_relative)
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 0, retry.stderr
        assert not tombstone.exists()
        assert not recovery.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_reparse_and_hardlink_children_do_not_escape_pinned_root(tmp_path: Path) -> None:
    root = tmp_path / "hostile children"
    programs = tmp_path / "Start Menu hostile children"
    external = tmp_path / "external must survive"
    external.mkdir()
    external_sentinel = external / "sentinel.txt"
    external_sentinel.write_bytes(b"external-content")
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    linked_source = root / "hardlinked-payload.bin"
    linked_source.write_bytes(b"hardlink-content")
    outside_hardlink = tmp_path / "outside-hardlink.bin"
    os.link(linked_source, outside_hardlink)
    junction = root / "junction-outside"
    junction_result = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert junction_result.returncode == 0, junction_result.stderr
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    try:
        result = _invoke(root, programs, registry_path)
        assert result.returncode == 0, result.stderr
        assert not root.exists()
        assert external_sentinel.read_bytes() == b"external-content"
        assert outside_hardlink.read_bytes() == b"hardlink-content"
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        shutil.rmtree(external, ignore_errors=True)
        outside_hardlink.unlink(missing_ok=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_symlink_children_do_not_escape_pinned_root(tmp_path: Path) -> None:
    root = tmp_path / "symlink children"
    programs = tmp_path / "Start Menu symlink children"
    external = tmp_path / "external symlink target"
    external.mkdir()
    external_file = external / "sentinel.txt"
    external_file.write_bytes(b"symlink-target")
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    try:
        os.symlink(external_file, root / "file-symlink")
        os.symlink(external, root / "directory-symlink", target_is_directory=True)
    except OSError as exc:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(external, ignore_errors=True)
        if exc.winerror == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    try:
        result = _invoke(root, programs, registry_path)
        assert result.returncode == 0, result.stderr
        assert not root.exists()
        assert external_file.read_bytes() == b"symlink-target"
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        shutil.rmtree(external, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_uninstaller_rename_refusal_preserves_complete_original_and_recovers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "rename locked root & user's app"
    programs = tmp_path / "Start Menu rename refusal"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    for index in range(50):
        (root / f"payload-{index:03d}.bin").write_bytes(bytes([index]))
    before = {
        p.relative_to(root).as_posix(): (p.is_dir(), p.stat().st_size if p.is_file() else 0)
        for p in root.rglob("*")
    }
    programs.mkdir(parents=True)
    (programs / "DCENT_Voice.lnk").write_bytes(b"shortcut")
    registry_path, registry_relative = _registry_fixture()
    holder = _exclusive_holder(root, directory=True)
    try:
        result = _invoke(root, programs, registry_path)
        assert result.returncode == 30
        assert "closed-world retained install tree" in result.stderr
        after = {
            p.relative_to(root).as_posix(): (p.is_dir(), p.stat().st_size if p.is_file() else 0)
            for p in root.rglob("*")
        }
        assert after == before
        assert programs.exists()
        assert _registry_exists(registry_relative)
        assert Path(_registry_value(registry_relative, "DCENTRecoveryUninstaller")).exists()
        assert holder.poll() is None
        holder.kill()
        holder.wait(timeout=5)
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 0, retry.stderr
        assert not root.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


@pytest.mark.parametrize("stop_after", ["registered", "renamed"])
def test_uninstaller_crash_state_is_recoverable(
    tmp_path: Path,
    stop_after: str,
) -> None:
    root = tmp_path / f"crash recovery {stop_after}"
    programs = tmp_path / f"Start Menu {stop_after}"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    programs.mkdir(parents=True)
    (programs / "DCENT_Voice.lnk").write_bytes(b"shortcut")
    registry_path, registry_relative = _registry_fixture()
    try:
        stopped = _invoke(
            root,
            programs,
            registry_path,
            extra=("-TestStopAfter", stop_after),
        )
        assert stopped.returncode == (70 if stop_after == "registered" else 71)
        state_path = Path(_registry_value(registry_relative, "DCENTRecoveryState"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        tombstone = Path(state["TombstonePath"])
        recovery = Path(state["RecoveryRoot"])
        assert recovery.exists()
        if stop_after == "registered":
            assert root.exists()
            assert not tombstone.exists()
        else:
            assert not root.exists()
            assert tombstone.exists()
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 0, retry.stderr
        assert not root.exists()
        assert not tombstone.exists()
        assert not recovery.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_uninstaller_recovery_rejects_replaced_root_identity(tmp_path: Path) -> None:
    root = tmp_path / "identity-bound install"
    original = tmp_path / "original moved aside"
    programs = tmp_path / "Start Menu identity"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"original")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    try:
        stopped = _invoke(
            root,
            programs,
            registry_path,
            extra=("-TestStopAfter", "registered"),
        )
        assert stopped.returncode == 70
        root.rename(original)
        _fixture(root)
        replacement = root / "dcent-voice.exe"
        replacement.write_bytes(b"replacement-must-survive")
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 31
        assert "identity differs" in retry.stderr
        assert replacement.read_bytes() == b"replacement-must-survive"
        assert original.exists()
        assert programs.exists()
        assert _registry_exists(registry_relative)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(original, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_uninstaller_stale_tombstone_does_not_delete_new_install(tmp_path: Path) -> None:
    root = tmp_path / "install with stale tombstone"
    programs = tmp_path / "Start Menu stale tombstone"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"old-install")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    try:
        stopped = _invoke(
            root,
            programs,
            registry_path,
            extra=("-TestStopAfter", "renamed"),
        )
        assert stopped.returncode == 71
        state_path = Path(_registry_value(registry_relative, "DCENTRecoveryState"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        tombstone = Path(state["TombstonePath"])
        assert tombstone.exists()
        _fixture(root)
        replacement = root / "dcent-voice.exe"
        replacement.write_bytes(b"new-install-must-survive")
        retry = _invoke_recovery(registry_relative)
        assert retry.returncode == 31
        assert "both install root and recovery tombstone exist" in retry.stderr
        assert replacement.read_bytes() == b"new-install-must-survive"
        assert tombstone.exists()
        assert _registry_exists(registry_relative)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_concurrent_uninstallers_serialize_without_losing_recovery(tmp_path: Path) -> None:
    root = tmp_path / "concurrent install root"
    programs = tmp_path / "Start Menu concurrent"
    _fixture(root)
    (root / "dcent-voice.exe").write_bytes(b"sentinel")
    programs.mkdir(parents=True)
    registry_path, registry_relative = _registry_fixture()
    ready = tmp_path / "before-rename.signal"
    proceed = tmp_path / "continue-rename.signal"

    base = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-InstallRoot",
        str(root),
        "-ProgramsRoot",
        str(programs),
        "-RegistryPath",
        registry_path,
        "-GraceTimeoutMs",
        "200",
        "-TerminateTimeoutMs",
        "3000",
    ]
    first = _start_test_process(
        [
            *base,
            "-TestBeforeRenameSignal",
            str(ready),
            "-TestBeforeRenameContinueSignal",
            str(proceed),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        second = _start_test_process(
            base,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        time.sleep(0.25)
        assert second.poll() is None
        proceed.write_text("continue", encoding="utf-8")
        first_out, first_err = first.communicate(timeout=20)
        second_out, second_err = second.communicate(timeout=20)
        assert first.returncode == 0, (first_out, first_err)
        assert second.returncode == 0, (second_out, second_err)
        assert not root.exists()
        assert not programs.exists()
        assert not _registry_exists(registry_relative)
        assert not list(tmp_path.glob(".*.uninstall-*"))
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(programs, ignore_errors=True)
        for candidate in tmp_path.glob(".*.uninstall-*"):
            shutil.rmtree(candidate, ignore_errors=True)
        if _registry_exists(registry_relative):
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_uninstaller_rejects_invalid_root_without_mutating_registration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not an install"
    programs = tmp_path / "Start Menu retained"
    root.mkdir()
    programs.mkdir()
    registry_path, registry_relative = _registry_fixture()
    try:
        result = _invoke(root, programs, registry_path)
        assert result.returncode == 10
        assert root.exists()
        assert programs.exists()
        assert _registry_exists(registry_relative)
    finally:
        import winreg

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


def test_unregistered_custom_uninstall_removes_only_its_payload(
    tmp_path: Path,
) -> None:
    import winreg

    custom = tmp_path / "custom portable install"
    _fixture(custom)
    (custom / "dcent-voice.exe").write_bytes(b"custom app")
    normal_programs = tmp_path / "normal Start Menu" / "DCENT_Voice"
    normal_programs.mkdir(parents=True)
    shortcut = normal_programs / "DCENT_Voice.lnk"
    shortcut.write_bytes(b"normal shortcut")
    user_data = tmp_path / "normal profile" / "DCENT_Voice"
    user_data.mkdir(parents=True)
    config = user_data / "config.toml"
    config.write_text("normal config", encoding="utf-8")
    model_data = tmp_path / "normal data" / "DCENT_Voice.Models"
    model_data.mkdir(parents=True)
    model = model_data / "custom-model.bin"
    model.write_bytes(b"normal durable model")
    modules = tmp_path / "normal local" / "DCENT" / "modules"
    modules.mkdir(parents=True)
    discovery = modules / "dcent-voice.json"
    discovery.write_text("{}", encoding="utf-8")
    registry_path, registry_relative = _registry_fixture()
    run_relative = rf"Software\DCENT_Voice\Tests\Run\{uuid.uuid4().hex}"
    _REGISTRY_KEYS.append(run_relative)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_relative) as key:
        winreg.SetValueEx(key, "DCENT_Voice", 0, winreg.REG_SZ, "normal app")

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-InstallRoot",
                str(custom),
                "-Unregistered",
                "-ProgramsRoot",
                str(custom),
                "-RegistryPath",
                registry_path,
                "-RunRegistryPath",
                rf"HKCU:\{run_relative}",
                "-UserDataRoot",
                str(user_data),
                "-ModelDataRoot",
                str(model_data),
                "-AdeModulesRoot",
                str(modules),
                "-GraceTimeoutMs",
                "200",
                "-TerminateTimeoutMs",
                "3000",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not custom.exists()
        assert shortcut.read_bytes() == b"normal shortcut"
        assert config.read_text(encoding="utf-8") == "normal config"
        assert model.read_bytes() == b"normal durable model"
        assert discovery.read_text(encoding="utf-8") == "{}"
        assert _registry_exists(registry_relative)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_relative) as key:
            assert winreg.QueryValueEx(key, "DCENT_Voice")[0] == "normal app"
    finally:
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, run_relative)
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_relative)


@pytest.mark.parametrize(
    ("field", "dangerous_value"),
    [
        ("ProgramsRoot", "foreign programs"),
        ("UserDataRoot", "foreign/DCENT_Voice"),
        ("ModelDataRoot", "foreign/DCENT_Voice.Models"),
        ("AdeModulesRoot", "foreign/DCENT/modules"),
        ("RunRegistryPath", r"HKCU:\Software\DCENT_Voice\ForeignRun"),
        ("CredentialService", "DCENT_Voice-Test-redirected"),
    ],
)
def test_production_recovery_state_refuses_redirected_cleanup_targets(
    tmp_path: Path,
    field: str,
    dangerous_value: str,
) -> None:
    install = tmp_path / "DCENT_Voice"
    transaction = uuid.uuid4().hex
    recovery = tmp_path / f".DCENT_Voice.uninstall-{transaction}.recovery"
    recovery.mkdir()
    state_path = recovery / "transaction.json"
    programs_result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Environment]::GetFolderPath('Programs')",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert programs_result.returncode == 0
    programs = Path(programs_result.stdout.strip()) / "DCENT_Voice"
    state: dict[str, object] = {
        "SchemaVersion": 4,
        "TransactionId": transaction,
        "InstallRoot": str(install),
        "TombstonePath": str(tmp_path / f".DCENT_Voice.uninstall-{transaction}.payload"),
        "RecoveryRoot": str(recovery),
        "RecoveryCommand": str(recovery / "Uninstall.cmd"),
        "StatePath": str(state_path),
        "ProgramsRoot": str(programs),
        "RegistryPath": (r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DCENT_Voice"),
        "Registered": False,
        "RunRegistryPath": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
        "PurgeUserData": True,
        "Unregistered": False,
        "UserDataRoot": str(Path(os.environ["APPDATA"]) / "DCENT_Voice"),
        "ModelDataRoot": str(Path(os.environ["LOCALAPPDATA"]) / "DCENT_Voice.Models"),
        "AdeModulesRoot": str(Path(os.environ["LOCALAPPDATA"]) / "DCENT" / "modules"),
        "CredentialService": "DCENT_Voice",
        "IdentityVolume": 1,
        "IdentityIndexHigh": 1,
        "IdentityIndexLow": 1,
        "Inventory": [],
    }
    if field.endswith("Root") and not dangerous_value.startswith("HKCU"):
        dangerous = tmp_path / dangerous_value
        dangerous.mkdir(parents=True)
        sentinel = dangerous / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        state[field] = str(dangerous)
    else:
        sentinel = tmp_path / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        state[field] = dangerous_value
    state_path.write_text(json.dumps(state), encoding="utf-8")

    refused = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-StatePath",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert refused.returncode == 10
    assert "production cleanup targets" in refused.stderr
    assert sentinel.read_text(encoding="utf-8") == "preserve"
