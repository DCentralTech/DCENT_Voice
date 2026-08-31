# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from dcent_voice.inject import windows_apps_test
from dcent_voice.util import owned_process
from dcent_voice.util.owned_process import (
    OwnedProcessError,
    owned_process_contains_pid,
    start_owned_process,
    terminate_owned_process,
)
from tests.win32_native import requires_win32_native


@pytest.mark.skipif(os.name == "nt", reason="native POSIX process-group contract")
def test_posix_teardown_reaps_descendant_when_root_accepts_term(tmp_path) -> None:
    child_pid_path = tmp_path / "descendant.pid"
    child_ready_path = tmp_path / "descendant.ready"
    child_script = (
        "import pathlib,signal,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "pathlib.Path(sys.argv[1]).write_text('ready',encoding='ascii');"
        "time.sleep(60)"
    )
    root_script = (
        "import pathlib,signal,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[2]]);"
        "ready=pathlib.Path(sys.argv[2]);"
        "deadline=time.monotonic()+5;"
        "\nwhile not ready.exists() and time.monotonic()<deadline: time.sleep(.01);"
        "\npathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii');"
        "\nsignal.signal(signal.SIGTERM,lambda *_args:sys.exit(0));"
        "\nwhile True: time.sleep(1)"
    )
    process = start_owned_process(
        [
            sys.executable,
            "-c",
            root_script,
            str(child_pid_path),
            str(child_ready_path),
            child_script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not child_pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))

        terminate_owned_process(process, grace_s=0.1, kill_s=5.0)

        assert process.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        terminate_owned_process(process, grace_s=0.0, kill_s=5.0)


@requires_win32_native
def test_windows_job_contains_child_but_never_the_test_runner() -> None:
    process = start_owned_process([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert owned_process_contains_pid(process, process.pid) is True
        assert owned_process_contains_pid(process, os.getpid()) is False
    finally:
        terminate_owned_process(process, grace_s=0.0)
    assert process.poll() is not None


@requires_win32_native
def test_windows_job_teardown_reaps_an_owned_grandchild(tmp_path) -> None:
    child_pid_path = tmp_path / "grandchild.pid"
    child_script = (
        "import pathlib,subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "target=pathlib.Path(sys.argv[1]);temporary=target.with_suffix('.tmp');"
        "temporary.write_text(str(p.pid),encoding='ascii');temporary.replace(target);"
        "time.sleep(60)"
    )
    process = start_owned_process(
        [sys.executable, "-c", child_script, str(child_pid_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        deadline = time.monotonic() + 5.0
        while not child_pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        assert owned_process_contains_pid(process, child_pid) is True
        handle = kernel32.OpenProcess(0x00100000, False, child_pid)
        assert handle
        terminate_owned_process(process, grace_s=0.0)
        assert kernel32.WaitForSingleObject(handle, 5000) == 0
    finally:
        if handle:
            kernel32.CloseHandle(handle)
        terminate_owned_process(process, grace_s=0.0)


@requires_win32_native
def test_windows_job_assignment_failure_never_executes_the_child(monkeypatch, tmp_path) -> None:
    root_marker = tmp_path / "root-ran.txt"
    descendant_marker = tmp_path / "descendant-ran.txt"
    descendant_script = (
        "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ran',encoding='ascii')"
    )
    root_script = (
        "import pathlib,subprocess,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text('ran',encoding='ascii');"
        "subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[2]]);"
        "time.sleep(30)"
    )

    def reject_assignment(_job, _process) -> None:
        raise OwnedProcessError("simulated assignment failure")

    monkeypatch.setattr(owned_process._WindowsJob, "assign", reject_assignment)
    with pytest.raises(OwnedProcessError, match="simulated assignment failure"):
        start_owned_process(
            [
                sys.executable,
                "-c",
                root_script,
                str(root_marker),
                str(descendant_marker),
                descendant_script,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    time.sleep(0.1)
    assert root_marker.exists() is False
    assert descendant_marker.exists() is False


@requires_win32_native
def test_owned_app_never_closes_an_unproven_window_owner(monkeypatch) -> None:
    app = windows_apps_test._OwnedApp("unsafe-match", Path("target.exe"))
    app.start([sys.executable, "-c", "import time; time.sleep(60)"])
    app.window = windows_apps_test._Window(hwnd=1234, pid=os.getpid(), title="shared host")
    messages: list[tuple[int, int]] = []

    class User32:
        @staticmethod
        def PostMessageW(hwnd, message, _wparam, _lparam):
            messages.append((int(hwnd), int(message)))
            return True

    monkeypatch.setattr(windows_apps_test, "_is_window", lambda _hwnd: True)
    monkeypatch.setattr(windows_apps_test, "_user32", lambda: User32())
    app.close()

    assert messages == []
    assert os.getpid() > 0


def test_product_and_automation_have_no_broad_process_kill_patterns() -> None:
    root = Path(__file__).resolve().parents[1]
    banned = ("taskkill", "stop-process", "pkill", "killall")
    offenders: list[str] = []
    for source_root in (root / "src", root / "scripts", root / "packaging"):
        for path in source_root.rglob("*"):
            if path.suffix.casefold() not in {".py", ".ps1", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").casefold()
            if any(pattern in text for pattern in banned):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
