# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Ownership-bound subprocess launch and teardown.

Windows PID-tree discovery is not a safe deletion primitive: a discovered
window can belong to a shared browser/terminal host, and a PID can be reused
between discovery and termination.  Processes launched here are assigned to a
private Windows Job Object.  Closing that job can terminate only processes the
kernel proved were assigned to it; it can never walk upward into the caller.
POSIX launches use a fresh session and terminate only that owned process group.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from ctypes import wintypes
from typing import Any, cast

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_CREATE_SUSPENDED = 0x00000004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class OwnedProcessError(RuntimeError):
    """Raised when a child cannot be bound to an ownership boundary."""


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class _WindowsJob:
    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = int(handle)
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(self._handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process: subprocess.Popen[Any]) -> None:
        process_handle = int(getattr(process, "_handle", 0) or 0)
        if not process_handle:
            raise OwnedProcessError("spawned Windows process has no native process handle")
        if not self._kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle), wintypes.HANDLE(process_handle)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def contains_pid(self, pid: int) -> bool:
        if self._handle == 0 or pid <= 0:
            return False
        process_handle = self._kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not process_handle:
            return False
        result = wintypes.BOOL(False)
        try:
            ok = self._kernel32.IsProcessInJob(
                process_handle,
                wintypes.HANDLE(self._handle),
                ctypes.byref(result),
            )
            return bool(ok and result.value)
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(
                wintypes.HANDLE(self._handle), wintypes.UINT(exit_code)
            )

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = 0


def _resume_suspended_process(process: subprocess.Popen[Any]) -> None:
    """Resume only the threads of a just-created, still-suspended child."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    snapshot_value = int(snapshot) if snapshot else 0
    if not snapshot_value or snapshot_value == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    thread_ids: list[int] = []
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if int(entry.th32OwnerProcessID) == process.pid:
                thread_ids.append(int(entry.th32ThreadID))
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    if not thread_ids:
        raise OwnedProcessError(f"spawned Windows process {process.pid} has no resumable thread")

    thread_handles: list[int] = []
    try:
        for thread_id in thread_ids:
            handle = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            thread_handles.append(int(handle))
        for handle in thread_handles:
            previous_count = kernel32.ResumeThread(wintypes.HANDLE(handle))
            if previous_count == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        for handle in thread_handles:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def start_owned_process(
    command: Sequence[str | os.PathLike[str]] | str | os.PathLike[str],
    **kwargs: Any,
) -> subprocess.Popen[Any]:
    """Start a child inside a kernel-enforced, caller-owned process boundary."""

    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
        process = subprocess.Popen(command, **kwargs)
        cast(Any, process)._dcent_owned_session = bool(kwargs["start_new_session"])
        return process

    job = _WindowsJob()
    win_process: subprocess.Popen[Any] | None = None
    assigned = False
    try:
        creationflags = int(kwargs.pop("creationflags", 0)) | _CREATE_SUSPENDED
        win_process = subprocess.Popen(command, creationflags=creationflags, **kwargs)
        job.assign(win_process)
        assigned = True
        _resume_suspended_process(win_process)
    except Exception:
        if win_process is not None and win_process.poll() is None:
            if assigned:
                job.terminate()
            else:
                # The child has never executed because CREATE_SUSPENDED stays
                # in effect until after successful Job Object assignment.
                win_process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                win_process.wait(timeout=2.0)
        if win_process is not None and win_process.poll() is None:
            win_process.kill()
            win_process.wait(timeout=2.0)
        job.close()
        raise
    assert win_process is not None
    cast(Any, win_process)._dcent_owned_job = job
    return win_process


def owned_process_contains_pid(process: subprocess.Popen[Any], pid: int) -> bool:
    """Return whether ``pid`` is kernel-proven to belong to ``process``'s boundary."""

    if os.name == "nt":
        job = getattr(process, "_dcent_owned_job", None)
        return bool(job is not None and job.contains_pid(pid))
    if not getattr(process, "_dcent_owned_session", False) or pid <= 0:
        return pid == process.pid
    get_process_group = getattr(os, "getpgid", None)
    if get_process_group is None:
        return False
    try:
        return bool(get_process_group(pid) == process.pid)
    except ProcessLookupError:
        return False


def terminate_owned_process(
    process: subprocess.Popen[Any],
    *,
    grace_s: float = 5.0,
    kill_s: float = 5.0,
) -> None:
    """Terminate a launched child and every process in only its owned boundary."""

    job = getattr(process, "_dcent_owned_job", None)
    owned_session = bool(getattr(process, "_dcent_owned_session", False))
    try:
        if owned_session:
            # The session leader may accept SIGTERM and exit while a worker
            # ignores it. Signal and observe the entire kernel-owned group from
            # the outset; waiting on only the root would falsely report clean
            # teardown and orphan that worker.
            kill_process_group = getattr(os, "killpg", None)
            if kill_process_group is None:
                raise OwnedProcessError("owned process-group termination is unavailable")
            with contextlib.suppress(ProcessLookupError):
                kill_process_group(process.pid, int(getattr(signal, "SIGTERM", 15)))
            if not _wait_for_process_group_exit(process, grace_s):
                with contextlib.suppress(ProcessLookupError):
                    kill_process_group(process.pid, int(getattr(signal, "SIGKILL", 9)))
                if not _wait_for_process_group_exit(process, kill_s):
                    raise subprocess.TimeoutExpired(cmd=process.args, timeout=max(0.0, kill_s))
            if process.poll() is None:
                process.wait(timeout=max(0.1, kill_s))
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(0.0, grace_s))
            except subprocess.TimeoutExpired:
                if job is not None:
                    job.terminate()
                else:
                    process.kill()
                process.wait(timeout=max(0.1, kill_s))
    finally:
        if job is not None:
            # KILL_ON_JOB_CLOSE also catches descendants that outlived a root
            # which accepted graceful termination.
            job.close()
            cast(Any, process)._dcent_owned_job = None


def _wait_for_process_group_exit(process: subprocess.Popen[Any], timeout_s: float) -> bool:
    """Boundedly wait until one exact POSIX process group no longer exists."""

    kill_process_group = getattr(os, "killpg", None)
    if kill_process_group is None:
        return False
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        # Reap an exited session leader so its zombie does not make the group
        # look alive for the entire grace/kill window.
        process.poll()
        try:
            kill_process_group(process.pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
