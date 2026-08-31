# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dcent_voice.app import _wait_for_gui_or_shutdown, _wait_for_lazy_overlay_host
from dcent_voice.util.wait import wait_any
from scripts import bench_idle


def test_wait_any_returns_immediately_when_already_set() -> None:
    ready = threading.Event()
    ready.set()
    start = time.perf_counter()
    assert wait_any(ready, threading.Event()) is True
    assert time.perf_counter() - start < 0.05


def test_wait_any_wakes_when_later_event_sets() -> None:
    first = threading.Event()
    second = threading.Event()

    def _set() -> None:
        time.sleep(0.05)
        second.set()

    threading.Thread(target=_set, daemon=True).start()
    start = time.perf_counter()
    assert wait_any(first, second, timeout=1.0) is True
    assert second.is_set()
    assert time.perf_counter() - start < 0.5


def test_wait_any_timeout_does_not_busy_spin() -> None:
    start = time.perf_counter()
    assert wait_any(threading.Event(), timeout=0.2) is False
    elapsed = time.perf_counter() - start
    assert 0.15 <= elapsed < 0.6


def test_lazy_overlay_host_wait_is_event_driven() -> None:
    shutdown = threading.Event()
    gui = threading.Event()
    overlay = threading.Event()

    def _request() -> None:
        time.sleep(0.05)
        overlay.set()

    threading.Thread(target=_request, daemon=True).start()
    assert _wait_for_lazy_overlay_host(shutdown, gui, overlay) is True
    shutdown.set()
    assert _wait_for_lazy_overlay_host(shutdown, gui, overlay) is False


def test_gui_or_shutdown_wait_prefers_shutdown() -> None:
    shutdown = threading.Event()
    gui = threading.Event()
    gui.set()
    shutdown.set()
    assert _wait_for_gui_or_shutdown(shutdown, gui) is False


def test_app_idle_loops_do_not_poll() -> None:
    source = Path("src/dcent_voice/app.py").read_text(encoding="utf-8")
    assert "wait_for_start_request(0.25)" not in source
    assert "shutdown.wait(0.1)" not in source
    assert "shutdown.wait(0.25)" not in source
    assert "_ready.wait(0.05)" not in source
    assert "wait_any" in source
    assert "_wait_for_lazy_overlay_host" in source


def test_bench_idle_self_test_emits_shell_vs_model_schema(tmp_path: Path) -> None:
    output = tmp_path / "idle.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/bench_idle.py",
            "--self-test",
            "--seconds",
            "0.5",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["scope"] == "self-test"
    assert report["model_resident_at_idle"] is False
    assert report["ready_to_dictate"] is True
    assert "shell_rss_mb" in report
    assert "cpu_percent" in report
    assert "mean" in report["shell_rss_mb"]
    assert "mean" in report["cpu_percent"]
    assert report["samples"]
    assert "self-test" in completed.stdout


def test_bench_idle_split_reports_unload_rss_and_ready_times() -> None:
    source = Path("scripts/bench_idle.py").read_text(encoding="utf-8")
    for needle in (
        "after_unload_rss_mb",
        "first_load_s",
        "reload_s",
        "warm_transcribe_s",
        "default_idle_unload_s",
    ):
        assert needle in source


def test_bench_idle_is_headless_unless_the_exact_interactive_opt_in_is_set() -> None:
    source = Path("scripts/bench_idle.py").read_text(encoding="utf-8")
    assert '("--no-tray", "--no-hotkeys", "--no-overlay")' in source
    assert 'os.environ.get("DCENT_VOICE_ALLOW_INTERACTIVE_TESTS") != "1"' in source
    assert '"idle_headless_service_no_dictation"' in source


def test_bench_idle_tears_down_owned_process_when_sampling_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "bench"
    temporary.mkdir()

    class FakeProcess:
        pid = 43210
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert timeout == 10
            return 0

    process = FakeProcess()
    killed: list[FakeProcess] = []
    monkeypatch.setattr(bench_idle.tempfile, "mkdtemp", lambda **_kwargs: str(temporary))
    monkeypatch.setattr(bench_idle, "_write_idle_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bench_idle, "_launch_idle", lambda **_kwargs: process)
    monkeypatch.setattr(bench_idle.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        bench_idle,
        "collect_samples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("sample fault")),
    )
    monkeypatch.setattr(bench_idle, "_kill_tree", killed.append)

    with pytest.raises(RuntimeError, match="sample fault"):
        bench_idle.main(["--seconds", "0.1", "--settle-s", "0"])

    assert killed == [process]
    assert temporary.exists() is False
