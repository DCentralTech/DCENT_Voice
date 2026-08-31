# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import errno
import os
import sys
import types

import pytest

from dcent_voice.attach import single_instance
from dcent_voice.attach.single_instance import (
    AlreadyRunningError,
    LockUnavailableError,
    SingleInstanceLock,
    force_clear_stale_lock,
)


class _FakeFcntl(types.ModuleType):
    LOCK_EX = 0x02
    LOCK_NB = 0x04
    LOCK_UN = 0x08

    def __init__(self) -> None:
        super().__init__("fcntl")
        self.locked: set[tuple[int, int]] = set()

    def flock(self, fd: int, operation: int) -> None:
        stat = os.fstat(fd)
        key = (stat.st_dev, stat.st_ino)
        if operation & self.LOCK_UN:
            self.locked.discard(key)
            return
        if key in self.locked:
            raise BlockingIOError(errno.EWOULDBLOCK, "lock is already held")
        self.locked.add(key)


@pytest.fixture
def mocked_posix(monkeypatch):
    fake = _FakeFcntl()
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    monkeypatch.setattr(single_instance.sys, "platform", "linux")
    return fake


def _lock(path, *, pid: int | None = None) -> SingleInstanceLock:
    # Unique mutex per path so pytest-xdist / parallel tests never collide.
    mutex = f"Local\\DCENT_Voice_Test_{abs(hash(str(path))) % (10**12)}"
    return SingleInstanceLock(
        path=path,
        pid=pid if pid is not None else os.getpid(),
        mutex_name=mutex if sys.platform == "win32" else None,
    )


def test_single_instance_lock_rejects_live_owner(tmp_path) -> None:
    path = tmp_path / "dcent-voice.lock"
    first = _lock(path, pid=os.getpid())
    first.acquire()
    try:
        second = _lock(path, pid=os.getpid() + 1)
        # Share the same mutex name as first so second sees the conflict.
        second.mutex_name = first.mutex_name
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()


def test_single_instance_lock_reclaims_stale_owner(tmp_path) -> None:
    path = tmp_path / "dcent-voice.lock"
    path.write_text("999999999", encoding="utf-8")

    lock = _lock(path, pid=os.getpid())
    lock.acquire()
    try:
        assert path.read_text(encoding="utf-8") == str(os.getpid())
    finally:
        lock.release()

    assert not path.exists()


def test_force_clear_stale_lock(tmp_path) -> None:
    path = tmp_path / "dcent-voice.lock"
    path.write_text("999999999", encoding="utf-8")
    assert force_clear_stale_lock(path) is True
    assert not path.exists()
    assert force_clear_stale_lock(path) is False


def test_force_clear_does_not_kill_live_lock(tmp_path) -> None:
    path = tmp_path / "dcent-voice.lock"
    lock = _lock(path, pid=os.getpid())
    lock.acquire()
    try:
        assert force_clear_stale_lock(path) is False
        assert path.exists()
    finally:
        lock.release()


def test_posix_advisory_lock_blocks_pid_reuse_and_releases(tmp_path, mocked_posix) -> None:
    path = tmp_path / "dcent-voice.lock"
    first = SingleInstanceLock(path=path, pid=1001)
    first.acquire()
    assert first._posix_fd is not None
    assert path.read_text(encoding="utf-8") == "1001"

    # The diagnostic PID may look stale or reused; the held advisory lock is the
    # authoritative ownership signal.
    second = SingleInstanceLock(path=path, pid=1002)
    with pytest.raises(AlreadyRunningError, match="POSIX file lock held"):
        second.acquire()

    first.refresh()
    assert path.read_text(encoding="utf-8") == "1001"
    first.release()
    first.release()  # idempotent
    if os.name != "nt":
        assert not path.exists()

    second.acquire()
    try:
        assert path.read_text(encoding="utf-8") == "1002"
    finally:
        second.release()


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot replace an open POSIX-style lock")
def test_posix_release_does_not_unlink_replaced_path(tmp_path, mocked_posix) -> None:
    path = tmp_path / "dcent-voice.lock"
    lock = SingleInstanceLock(path=path, pid=1001)
    lock.acquire()

    path.unlink()
    path.write_text("replacement", encoding="utf-8")
    lock.release()

    assert path.read_text(encoding="utf-8") == "replacement"


def test_posix_force_clear_respects_held_lock(tmp_path, mocked_posix) -> None:
    path = tmp_path / "dcent-voice.lock"
    lock = SingleInstanceLock(path=path, pid=1001)
    lock.acquire()
    try:
        assert force_clear_stale_lock(path) is False
    finally:
        lock.release()


def test_posix_lock_rejects_hard_link_without_overwriting_target(tmp_path, mocked_posix) -> None:
    target = tmp_path / "sentinel.txt"
    target.write_text("DO NOT OVERWRITE", encoding="utf-8")
    path = tmp_path / "dcent-voice.lock"
    os.link(target, path)

    with pytest.raises(AlreadyRunningError, match="unsafe"):
        SingleInstanceLock(path=path, pid=4242).acquire(retries=1)

    assert target.read_text(encoding="utf-8") == "DO NOT OVERWRITE"
    assert force_clear_stale_lock(path) is False


def test_posix_lock_rejects_symlink_without_overwriting_target(tmp_path, mocked_posix) -> None:
    target = tmp_path / "sentinel.txt"
    target.write_text("DO NOT OVERWRITE", encoding="utf-8")
    path = tmp_path / "dcent-voice.lock"
    try:
        path.symlink_to(target)
    except OSError as exc:  # pragma: no cover - Windows privilege policy varies
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(AlreadyRunningError, match="unsafe"):
        SingleInstanceLock(path=path, pid=4242).acquire(retries=1)

    assert target.read_text(encoding="utf-8") == "DO NOT OVERWRITE"
    assert force_clear_stale_lock(path) is False


def test_windows_mutex_blocks_second_instance(tmp_path) -> None:
    """Named mutex is primary on Windows; second acquire must fail even with no file."""
    if sys.platform != "win32":
        pytest.skip("Windows mutex only")

    path_a = tmp_path / "a.lock"
    path_b = tmp_path / "b.lock"
    shared = f"Local\\DCENT_Voice_Test_Shared_{abs(hash(str(tmp_path))) % (10**12)}"
    first = SingleInstanceLock(path=path_a, pid=os.getpid(), mutex_name=shared)
    first.acquire()
    try:
        second = SingleInstanceLock(path=path_b, pid=os.getpid() + 1, mutex_name=shared)
        with pytest.raises(AlreadyRunningError, match="mutex|already running"):
            second.acquire()
    finally:
        first.release()


def test_windows_smoke_mutex_override_is_scoped(tmp_path, monkeypatch) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows mutex only")

    smoke_mutex = f"Local\\DCENT_Voice_Smoke_{abs(hash(str(tmp_path))) % (10**12)}"
    monkeypatch.setenv("DCENT_VOICE_SMOKE_MUTEX", smoke_mutex)

    lock = SingleInstanceLock(path=tmp_path / "smoke.lock")

    assert lock.mutex_name == smoke_mutex


def test_windows_mutex_allows_reclaim_when_lock_pid_reused(tmp_path) -> None:
    """Mutex owner can reclaim a lock file whose PID still looks live (PID reuse)."""
    if sys.platform != "win32":
        pytest.skip("Windows mutex only")

    path = tmp_path / "dcent-voice.lock"
    path.write_text(str(os.getpid()), encoding="utf-8")
    lock = _lock(path, pid=os.getpid())
    lock.acquire()
    try:
        assert path.read_text(encoding="utf-8") == str(os.getpid())
        assert lock.acquired is True
    finally:
        lock.release()
    assert not path.exists()


def test_windows_mutex_null_handle_fail_closed(tmp_path, monkeypatch) -> None:
    """W3-F2: CreateMutexW returning NULL must raise, not fall back to file-only.

    Drives the real ``_acquire_windows_mutex`` path with a kernel32 stub that
    returns a null HANDLE so dual-instance PID-reuse cannot reappear silently.
    """
    if sys.platform != "win32":
        pytest.skip("Windows mutex only")

    import ctypes

    path = tmp_path / "dcent-voice.lock"
    # Pre-seed a lock file so a silent file-only fallback would look "successful".
    path.write_text("999999999", encoding="utf-8")
    lock = SingleInstanceLock(
        path=path,
        pid=os.getpid(),
        mutex_name=f"Local\\DCENT_Voice_Test_Null_{abs(hash(str(path))) % (10**12)}",
    )

    real_windll = ctypes.WinDLL

    def patched_windll(name, *args, **kwargs):
        dll = real_windll(name, *args, **kwargs)
        if str(name).lower() in {"kernel32", "kernel32.dll"}:

            class _NullCreateMutex:
                argtypes = None
                restype = None

                def __call__(self, *_a, **_k):
                    # ACCESS_DENIED — any non-zero winerror is fine for the message.
                    ctypes.set_last_error(5)
                    return 0  # NULL HANDLE → fail-closed

            dll.CreateMutexW = _NullCreateMutex()
        return dll

    monkeypatch.setattr(ctypes, "WinDLL", patched_windll)

    # Distinct type: this is "the OS refused us a mutex", not "already running".
    with pytest.raises(LockUnavailableError, match="CreateMutexW failed") as excinfo:
        lock.acquire()
    assert excinfo.value.winerror == 5
    assert not isinstance(excinfo.value, AlreadyRunningError)

    # Fail-closed: no in-process ownership and no rewrite of the lock file as us.
    assert lock.acquired is False
    assert lock._mutex_handle is None
    assert path.read_text(encoding="utf-8") == "999999999"


def test_handle_already_running_retries_after_stale_clear(tmp_path, monkeypatch) -> None:
    from dcent_voice.app import handle_already_running
    from dcent_voice.attach.single_instance import AlreadyRunningError
    from dcent_voice.config import load_config

    lock_path = tmp_path / "dcent-voice.lock"
    lock_path.write_text("999999999", encoding="utf-8")
    monkeypatch.setattr(
        "dcent_voice.app.force_clear_stale_lock",
        lambda path=None: force_clear_stale_lock(lock_path),
    )
    calls = {"n": 0}

    def fake_run_app(config, **kwargs):
        calls["n"] += 1
        return 42

    monkeypatch.setattr("dcent_voice.app.run_app", fake_run_app)
    config = load_config(create=True)
    code = handle_already_running(
        AlreadyRunningError("already"),
        config=config,
        run_kwargs={"no_tray": True},
    )
    assert code == 42
    assert calls["n"] == 1
    assert not lock_path.exists()
