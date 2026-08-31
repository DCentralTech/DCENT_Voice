# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcent_voice import app
from dcent_voice.config import load_config
from dcent_voice.inject import clipboard, windows_self_test
from dcent_voice.inject.keystroke import WindowsSendInputInjector, _utf16_units
from dcent_voice.inject.windows_self_test import run_windows_injection_self_test
from scripts import bench_injection_windows


def test_windows_unicode_injector_preserves_utf16_surrogate_pairs() -> None:
    assert _utf16_units("A🔥日") == [0x0041, 0xD83D, 0xDD25, 0x65E5]


def test_injection_self_test_dispatch_is_config_independent(monkeypatch, tmp_path) -> None:
    calls: list[tuple[Path | None, int]] = []

    def fake_command(*, output_json: Path | None, runs: int) -> int:
        calls.append((output_json, runs))
        return 0

    monkeypatch.setattr(
        "dcent_voice.inject.windows_self_test.run_self_test_command",
        fake_command,
    )
    monkeypatch.setattr(
        app,
        "load_config",
        lambda *_args, **_kwargs: pytest.fail("self-test must not read user configuration"),
    )
    output = tmp_path / "report.json"
    assert app.main(["injection-self-test", "--runs", "7", "--output-json", str(output)]) == 0
    assert calls == [(output, 7)]


def test_child_environment_preserves_venv_prefix_across_base_interpreter_generations(
    monkeypatch, tmp_path
) -> None:
    venv = tmp_path / "venv"
    dlls = venv / "Lib" / "site-packages" / "pywin32_system32"
    dlls.mkdir(parents=True)
    base = tmp_path / "base"
    (base / "Lib" / "site-packages" / "pywin32_system32").mkdir(parents=True)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv(windows_self_test._SOURCE_ENV_PREFIX, raising=False)
    monkeypatch.setattr(windows_self_test.sys, "prefix", str(venv))

    parent_environment = windows_self_test._child_environment()

    assert parent_environment[windows_self_test._SOURCE_ENV_PREFIX] == str(venv)
    assert parent_environment["PATH"].split(windows_self_test.os.pathsep)[0] == str(dlls)

    monkeypatch.setenv(
        windows_self_test._SOURCE_ENV_PREFIX,
        parent_environment[windows_self_test._SOURCE_ENV_PREFIX],
    )
    monkeypatch.setattr(windows_self_test.sys, "prefix", str(base))

    child_environment = windows_self_test._child_environment()

    assert child_environment[windows_self_test._SOURCE_ENV_PREFIX] == str(venv)
    assert child_environment["PATH"].split(windows_self_test.os.pathsep)[0] == str(dlls)


def test_self_test_error_json_always_identifies_exact_stage(monkeypatch, tmp_path) -> None:
    output = tmp_path / "error.json"

    def fail(*, runs):
        del runs
        raise windows_self_test.InjectionSelfTestStageError(
            "verified_native_paste", RuntimeError("deliberate failure")
        )

    monkeypatch.setattr(windows_self_test, "run_windows_injection_self_test", fail)

    assert windows_self_test.run_self_test_command(output_json=output, runs=1) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert report["diagnostic"]["stage"] == "verified_native_paste"
    assert "deliberate failure" in report["diagnostic"]["error"]


def test_windows_desktop_build_uses_layout_independent_sendinput(monkeypatch) -> None:
    monkeypatch.setattr(app.platform, "system", lambda: "Windows")
    config = load_config(Path("config.example.toml"), create=False)
    router = app.build_injector(config)
    assert isinstance(router.injectors["keystroke"], WindowsSendInputInjector)


def test_benchmark_executable_mode_uses_child_json_contract(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "dcent-voice.exe"
    executable.write_bytes(b"frozen-test-binary")
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append(command)
        report_path = Path(command[command.index("--output-json") + 1])
        report_path.write_text('{"status":"pass","routes":[]}\n', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bench_injection_windows.subprocess, "run", fake_run)
    code, report = bench_injection_windows.run_benchmark(
        executable=executable,
        runs=3,
        timeout_s=5.0,
    )

    assert code == 0
    assert observed[0][0] == str(executable.resolve())
    assert observed[0][1:4] == ["injection-self-test", "--runs", "3"]
    assert report["benchmark_process"]["mode"] == "frozen-executable"
    assert (
        report["benchmark_process"]["executable_sha256"]
        == sha256(b"frozen-test-binary").hexdigest()
    )


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="requires a real interactive Win32 desktop")
def test_real_windows_injection_into_private_native_edit_control() -> None:
    original = clipboard.snapshot_clipboard()
    if original is None:
        pytest.skip("current clipboard cannot be restored without losing an unsupported format")
    report = run_windows_injection_self_test(runs=1)
    assert report["status"] == "pass", report
    assert {route["route"] for route in report["routes"]} == {
        "native_paste",
        "native_replace",
    }
    assert {route["native_api"] for route in report["routes"]} == {
        "verified EM_REPLACESEL (target-bound)",
        "clipboard transaction + verified WM_PASTE (target-bound)",
    }
    assert all(route["success_count"] == route["runs"] for route in report["routes"])
    assert report["clipboard_restore"]["registered_html_format_restored"] is True
    assert report["clipboard_restore"]["arbitrary_registered_format_restored"] is True
    assert report["focus_guard"]["native_replace_selection_race_target_bound"] is True
    assert report["focus_guard"]["native_replace_distractor_received_nothing"] is True
    assert report["focus_guard"]["native_paste_selection_race_target_bound"] is True
    assert report["focus_guard"]["native_paste_distractor_received_nothing"] is True
    assert report["private_clipboard_fail_closed"]["all_rejected_before_mutation"] is False
    assert report["private_clipboard_fail_closed"]["all_native_fallback_exact"] is True
    assert report["private_clipboard_fail_closed"]["probe_global_allocations_outstanding"] == 0
    assert report["private_clipboard_fail_closed"]["gdi_handle_delta"] == 0
    assert report["unicode_sendinput_probe"]["success"] is True
    assert report["unicode_sendinput_probe"]["native_api"] == (
        "global SendInput with KEYEVENTF_UNICODE"
    )
    assert report["clipboard_contention"]["rejected"] is False
    assert report["clipboard_contention"]["clipboard_held"] is True
    assert report["clipboard_contention"]["native_fallback_exact"] is True
    assert report["cross_process_serialization"]["both_payloads_exact_once"] is True
    assert report["parent_binding"]["forged_parent_rejected"] is True
    assert report["parent_binding"]["orphan_exited"] is True
