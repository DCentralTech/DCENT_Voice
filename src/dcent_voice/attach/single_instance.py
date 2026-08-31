# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Prevent multiple concurrent DCENT_Voice desktop instances."""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .registry import default_registry_dir, is_pid_running, write_text_atomic

# Global named mutex identity (Windows). Survives PID reuse of a dead lock file.
_WINDOWS_MUTEX_NAME = "Local\\DCENT_Voice_SingleInstance"
_ERROR_ALREADY_EXISTS = 183


class AlreadyRunningError(RuntimeError):
    """Raised when another DCENT_Voice process owns the instance lock."""


class LockUnavailableError(RuntimeError):
    """Raised when the OS refused to create the single-instance primitive.

    Deliberately *not* an :class:`AlreadyRunningError`: "another copy is running,
    look in your tray" and "this machine would not give us a mutex" need
    opposite remedies, and reporting the second as the first sent users hunting
    for a tray icon that was never there. Callers must surface the winerror.
    """

    def __init__(self, message: str, *, winerror: int | None = None) -> None:
        super().__init__(message)
        self.winerror = winerror


@dataclass
class SingleInstanceLock:
    """Cross-platform lock that enforces one desktop instance."""

    path: Path | None = None
    pid: int = os.getpid()
    acquired: bool = False
    # Override for tests so parallel suites do not share the production mutex.
    mutex_name: str | None = None
    _mutex_handle: Any = field(default=None, repr=False, compare=False)
    # POSIX locks are owned by an open file description, so the descriptor must
    # remain open for the full lifetime of the application instance.
    _posix_fd: int | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = default_registry_dir() / "dcent-voice.lock"
        if self.mutex_name is None and sys.platform == "win32":
            # The packaged smoke harness needs an isolated mutex so it can run
            # beside a real review build. Restrict this test-only override to
            # the harness's own naming pattern; normal launches retain the
            # production mutex unconditionally.
            smoke_mutex = os.environ.get("DCENT_VOICE_SMOKE_MUTEX", "")
            if smoke_mutex.startswith("Local\\DCENT_Voice_Smoke_"):
                self.mutex_name = smoke_mutex
            else:
                self.mutex_name = _WINDOWS_MUTEX_NAME

    def acquire(self, *, retries: int = 4, retry_delay_s: float = 0.05) -> None:
        """Take the lock, reclaiming a dead holder's file automatically.

        On Windows a named mutex is the primary single-instance primitive so a
        leftover lock file whose PID was reused by an unrelated process cannot
        permanently block restart. On POSIX, a non-blocking ``flock`` is held on
        the PID file for this object's lifetime. The PID remains diagnostic only.
        """
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32" and self.mutex_name:
            self._acquire_windows_mutex()

        if sys.platform != "win32":
            last_error: Exception | None = None
            for attempt in range(max(1, retries)):
                try:
                    self._acquire_posix_file_lock()
                    return
                except AlreadyRunningError:
                    raise
                except OSError as exc:
                    last_error = exc
                    time.sleep(retry_delay_s * (attempt + 1))
            raise AlreadyRunningError(
                "DCENT_Voice could not acquire the POSIX single-instance lock "
                "(the lock path kept changing or could not be opened)."
            ) from last_error

        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                self._try_acquire_once()
                return
            except AlreadyRunningError:
                self._release_windows_mutex()
                raise
            except FileExistsError as exc:
                last_error = exc
                self._reclaim_if_stale()
                time.sleep(retry_delay_s * (attempt + 1))
            except OSError as exc:
                last_error = exc
                self._reclaim_if_stale()
                time.sleep(retry_delay_s * (attempt + 1))

        self._release_windows_mutex()
        raise AlreadyRunningError(
            "DCENT_Voice could not acquire the single-instance lock "
            "(another start may be in progress, or a stale lock could not be cleared)."
        ) from last_error

    def _try_acquire_once(self) -> None:
        assert self.path is not None
        self._reclaim_if_stale()

        if self.path.exists():
            existing_pid = _read_pid(self.path)
            if existing_pid is not None and is_pid_running(existing_pid):
                # On Windows the mutex already proved we are the only instance
                # when it was acquired; a live PID in the file is stale identity
                # after crash+reuse only if mutex failed (non-Windows).
                if self._mutex_handle is None:
                    raise AlreadyRunningError(
                        f"DCENT_Voice is already running as PID {existing_pid}."
                    )
                # Mutex held → we own the instance; reclaim orphaned lock file.
                self.path.unlink(missing_ok=True)
            else:
                # Dead holder or corrupt lock file.
                self.path.unlink(missing_ok=True)

        fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(self.pid))
            handle.flush()
            os.fsync(handle.fileno())
        self.acquired = True

    def _acquire_posix_file_lock(self) -> None:
        """Hold an advisory lock so PID reuse cannot impersonate a live owner."""
        assert self.path is not None
        fcntl: Any = __import__("fcntl")

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow and os.name != "nt":
            raise AlreadyRunningError("safe POSIX locking requires O_NOFOLLOW support")
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR | nofollow, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AlreadyRunningError("unsafe symlink at POSIX lock path") from exc
            raise
        locked = False
        try:
            _validate_posix_lock_fd(self.path, fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise
                existing_pid = _read_pid(self.path)
                detail = f" as PID {existing_pid}" if existing_pid is not None else ""
                raise AlreadyRunningError(
                    f"DCENT_Voice is already running{detail} (POSIX file lock held)."
                ) from exc

            # A cleanup process could have unlinked/replaced the path between
            # open() and flock(). Revalidate regular-file ownership and identity
            # before truncating the PID file.
            _validate_posix_lock_fd(self.path, fd)

            _write_pid_fd(fd, self.pid)
            self._posix_fd = fd
            self.acquired = True
        except Exception:
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise

    def _reclaim_if_stale(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        existing_pid = _read_pid(self.path)
        if existing_pid is None or not is_pid_running(existing_pid):
            self.path.unlink(missing_ok=True)

    def release(self) -> None:
        posix_fd = self._posix_fd
        self._posix_fd = None
        if posix_fd is not None:
            try:
                # Unlink only the inode we locked. If another actor replaced the
                # path, leave its file alone. Keep our lock held through unlink so
                # a contender can never acquire this inode and then lose its path.
                if self.path is not None and _path_matches_fd(self.path, posix_fd):
                    # PermissionError is possible on unusual POSIX mounts that
                    # emulate Windows delete sharing. Leaving an unlocked PID
                    # file is safe: the next owner locks and overwrites it.
                    with contextlib.suppress(FileNotFoundError, PermissionError):
                        self.path.unlink(missing_ok=True)
            finally:
                try:
                    fcntl: Any = __import__("fcntl")

                    with contextlib.suppress(OSError):
                        fcntl.flock(posix_fd, fcntl.LOCK_UN)
                finally:
                    os.close(posix_fd)
                    self.acquired = False

        if self.acquired and self.path is not None:
            try:
                if _read_pid(self.path) == self.pid:
                    self.path.unlink()
            finally:
                self.acquired = False
        self._release_windows_mutex()

    def refresh(self) -> None:
        if not self.acquired or self.path is None:
            raise RuntimeError("Cannot refresh a lock that has not been acquired.")
        if self._posix_fd is not None:
            _write_pid_fd(self._posix_fd, self.pid)
            return
        write_text_atomic(self.path, str(self.pid))

    def _acquire_windows_mutex(self) -> None:
        if self._mutex_handle is not None:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        name = self.mutex_name or _WINDOWS_MUTEX_NAME
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            # Do not silently drop to file-only locking: that reintroduces
            # Windows PID-reuse dual-instance failures (W1-F2).
            err = ctypes.get_last_error()
            raise LockUnavailableError(
                "DCENT_Voice could not create the single-instance lock "
                f"(CreateMutexW failed, winerror={err}). This is a Windows "
                "session/permission problem, not another running copy.",
                winerror=err,
            )
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise AlreadyRunningError(
                "DCENT_Voice is already running (named mutex held by another process)."
            )
        self._mutex_handle = handle

    def _release_windows_mutex(self) -> None:
        handle = self._mutex_handle
        self._mutex_handle = None
        if handle is None or sys.platform != "win32":
            return
        import ctypes

        with contextlib.suppress(OSError):
            ctypes.windll.kernel32.CloseHandle(handle)

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def force_clear_stale_lock(path: Path | None = None) -> bool:
    """Delete a stale lock file. Returns True if it was cleared.

    Windows retains the PID-liveness check because the named mutex is the real
    owner identity. POSIX first takes the advisory lock, so PID reuse cannot make
    an unlocked stale file look live and a held lock can never be force-cleared.
    """
    lock_path = path or (default_registry_dir() / "dcent-voice.lock")
    if not lock_path.exists():
        return False
    if sys.platform != "win32":
        return _force_clear_posix_lock(lock_path)
    existing_pid = _read_pid(lock_path)
    if existing_pid is not None and is_pid_running(existing_pid):
        return False
    lock_path.unlink(missing_ok=True)
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pid_fd(fd: int, pid: int) -> None:
    payload = str(pid).encode("ascii")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)


def _path_matches_fd(path: Path, fd: int) -> bool:
    try:
        fd_stat = os.fstat(fd)
        path_stat = path.lstat()
        return (
            stat.S_ISREG(fd_stat.st_mode)
            and stat.S_ISREG(path_stat.st_mode)
            and fd_stat.st_nlink == 1
            and os.path.samestat(path_stat, fd_stat)
        )
    except (FileNotFoundError, OSError):
        return False


def _validate_posix_lock_fd(path: Path, fd: int) -> None:
    """Reject symlinked, hard-linked, replaced, or foreign POSIX lock files."""
    if not _path_matches_fd(path, fd):
        raise AlreadyRunningError("unsafe or replaced POSIX lock path")
    fd_stat = os.fstat(fd)
    if hasattr(os, "getuid") and fd_stat.st_uid != os.getuid():
        raise AlreadyRunningError("POSIX lock path is not owned by the current user")


def _force_clear_posix_lock(path: Path) -> bool:
    """Remove an unlocked POSIX lock file without racing a live owner."""
    fcntl: Any = __import__("fcntl")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and os.name != "nt":
        return False
    try:
        fd = os.open(str(path), os.O_RDWR | nofollow)
    except (FileNotFoundError, OSError):
        return False
    try:
        locked = False
        if not _path_matches_fd(path, fd):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return False
            raise
        if not _path_matches_fd(path, fd):
            return False
        path.unlink(missing_ok=True)
        return True
    finally:
        if locked:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
