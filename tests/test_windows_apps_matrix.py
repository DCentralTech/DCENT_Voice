# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import threading
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcent_voice import app
from dcent_voice.config import load_bundled_default_config
from dcent_voice.inject import windows_apps_test
from scripts import bench_injection_apps_windows
from tests.win32_native import requires_win32_native


def test_real_app_routes_are_production_build_decisions(monkeypatch) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Windows")
    router = app.build_injector(load_bundled_default_config())
    native = SimpleNamespace(supports_edit_messages=True)
    global_target = SimpleNamespace(supports_edit_messages=False)

    router.process_name_fn = lambda: "Code.exe"
    assert router.resolve_decision("短🔥e\u0301", native).delivery == "native_replace"
    assert router.resolve_decision("short", global_target).delivery == "unicode_sendinput_checked"
    assert router.resolve_decision("line one\r\nline two", native).delivery == "native_paste"
    assert (
        router.resolve_decision("line one\r\nline two", global_target).delivery
        == "clipboard_ctrl_v_checked"
    )
    assert router.resolve_decision("x" * 65, native).delivery == "native_paste"

    router.process_name_fn = lambda: "cmd.exe"
    terminal = router.resolve_decision("x" * 65, global_target)
    assert terminal.configured_injector == "keystroke"
    assert terminal.resolved_injector == "keystroke"
    assert terminal.delivery == "unicode_sendinput_checked"


def test_percentiles_interpolate_without_cherry_picking() -> None:
    summary = windows_apps_test._latencies([1.0, 2.0, 3.0, 100.0])
    assert summary["p50"] == 2.5
    assert summary["p95"] == pytest.approx(85.45)
    assert summary["p99"] == pytest.approx(97.09)


def test_app_matrix_dispatch_is_config_independent(monkeypatch, tmp_path) -> None:
    calls: list[tuple[Path | None, list[str], int]] = []

    def fake_command(*, output_json: Path | None, apps: list[str], runs: int) -> int:
        calls.append((output_json, apps, runs))
        return 0

    monkeypatch.setattr(windows_apps_test, "run_apps_test_command", fake_command)
    monkeypatch.setattr(
        app,
        "load_config",
        lambda *_args, **_kwargs: pytest.fail("matrix must not read user configuration"),
    )
    output = tmp_path / "matrix.json"
    assert (
        app.main(
            [
                "injection-app-matrix",
                "--apps",
                "notepad,chrome",
                "--runs",
                "4",
                "--output-json",
                str(output),
            ]
        )
        == 0
    )
    assert calls == [(output, ["notepad", "chrome"], 4)]


@requires_win32_native
def test_matrix_fails_if_clipboard_is_not_restored(monkeypatch, tmp_path) -> None:
    snapshots = iter([[(13, b"before")], [(13, b"after")]])
    monkeypatch.setattr(windows_apps_test.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        windows_apps_test, "discover_apps", lambda: {"notepad": tmp_path / "notepad.exe"}
    )
    monkeypatch.setattr(windows_apps_test, "snapshot_clipboard", lambda **_kw: next(snapshots))
    monkeypatch.setattr(
        windows_apps_test,
        "_notepad",
        lambda *_args: {"app": "notepad", "status": "pass"},
    )

    report = windows_apps_test.run_apps_matrix(apps=["notepad"], runs=1)

    assert report["status"] == "fail"
    assert report["clipboard_restoration"]["exact_after_matrix"] is False


@requires_win32_native
def test_matrix_builds_the_exact_bundled_production_injector(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "notepad.exe"
    executable.write_bytes(b"fixture")
    built = []
    real_builder = app.build_injector

    def spy_builder(config):
        router = real_builder(config)
        built.append((config, router))
        return router

    def fake_notepad(_executable, _root, runs, router):
        assert router is built[0][1]
        decision = router.resolve_decision("x" * 65, SimpleNamespace(supports_edit_messages=False))
        return {
            "app": "notepad",
            "status": "pass",
            "cases": [{"exact": True, "runs": runs, "route": decision.delivery}],
        }

    monkeypatch.setattr(windows_apps_test.platform, "system", lambda: "Windows")
    monkeypatch.setattr(windows_apps_test, "discover_apps", lambda: {"notepad": executable})
    monkeypatch.setattr(windows_apps_test, "snapshot_clipboard", lambda **_kw: [])
    monkeypatch.setattr(windows_apps_test, "_windows", lambda: [])
    monkeypatch.setattr(windows_apps_test, "_notepad", fake_notepad)
    monkeypatch.setattr(app, "build_injector", spy_builder)

    report = windows_apps_test.run_apps_matrix(apps=["notepad"], runs=1)

    assert report["status"] == "pass"
    assert len(built) == 1
    assert built[0][0].source_path == load_bundled_default_config().source_path
    assert report["production_injector"]["builder"] == "dcent_voice.app.build_injector"
    assert report["production_injector"]["short_text_keystroke_chars"] == 48


@pytest.mark.parametrize("app_name", ["notepad", "vscode", "console_terminal", "edge", "chrome"])
@pytest.mark.parametrize("failure", ["no_op", "partial", "stale"])
def test_every_app_rejects_noop_partial_and_stale_readback(app_name, failure) -> None:
    expected, sentinel = windows_apps_test._sentinel_payload("short")
    if failure == "no_op":
        observed = ""
    elif failure == "partial":
        observed = expected[:-1]
    else:
        observed, _old_sentinel = windows_apps_test._sentinel_payload("short")

    with pytest.raises(AssertionError):
        windows_apps_test._verify_delivery(
            app=app_name,
            run=0,
            expected=expected,
            observed=observed,
            sentinel=sentinel,
        )


@pytest.mark.parametrize("app_name", ["notepad", "vscode", "console_terminal", "edge", "chrome"])
def test_every_app_rejects_unverified_baseline(app_name) -> None:
    with pytest.raises(AssertionError, match="baseline mismatch"):
        windows_apps_test._verify_baseline(
            app=app_name, run=0, expected="", observed="stale previous content"
        )


def test_sentinel_payloads_are_unique_and_high_entropy() -> None:
    generated = [windows_apps_test._sentinel_payload("short") for _ in range(100)]
    sentinels = [sentinel for _payload, sentinel in generated]
    assert len(set(sentinels)) == 100
    assert all(len(sentinel) == 33 and sentinel.startswith("D") for sentinel in sentinels)
    assert all(sentinel in payload for payload, sentinel in generated)


def test_browser_content_click_stays_below_chromium_chrome() -> None:
    x, y = windows_apps_test._browser_content_click_offset(1280, 800)
    assert x == 640
    assert y >= 220
    assert y > 160


def test_browser_search_click_hits_typical_search_box_not_omnibox() -> None:
    x, y = windows_apps_test._browser_search_click_offset(1280, 800)
    body_y = windows_apps_test._browser_content_click_offset(1280, 800)[1]
    assert x == 640
    assert y == 368
    assert y >= 200
    assert y > 160
    assert y < body_y


def test_google_live_target_is_classic_page_search_not_omnibox() -> None:
    url = windows_apps_test.LIVE_BROWSER_TARGETS["edge-google"][3]
    assert "google.com/webhp" in url
    assert "igu=1" in url


def test_non_github_search_preserves_autofocus() -> None:
    assert windows_apps_test._search_prepare_preserves_autofocus("edge-google")
    assert windows_apps_test._search_prepare_preserves_autofocus("edge-wiki")
    assert windows_apps_test._search_prepare_preserves_autofocus("edge-ddg")
    assert not windows_apps_test._search_prepare_preserves_autofocus("edge-github")


def test_page_search_field_prefers_named_edit_then_first_page_edit() -> None:
    from types import SimpleNamespace

    named = SimpleNamespace(
        Name="Search Google",
        AutomationId="q",
        ControlTypeName="Edit",
        BoundingRectangle=SimpleNamespace(width=400, height=40),
        GetChildren=lambda: [],
    )
    unnamed = SimpleNamespace(
        Name="",
        AutomationId="APjFqb",
        ControlTypeName="ComboBox",
        BoundingRectangle=SimpleNamespace(width=500, height=44),
        GetChildren=lambda: [],
    )
    web = SimpleNamespace(
        Name="",
        AutomationId="RootWebArea",
        ControlTypeName="Document",
        BoundingRectangle=SimpleNamespace(width=1200, height=700),
        GetChildren=lambda: [unnamed],
    )
    root = SimpleNamespace(GetChildren=lambda: [web, named])
    window = SimpleNamespace(hwnd=1)
    previous_hwnd = windows_apps_test._control_from_hwnd
    previous_focused = windows_apps_test.focused_page_search_field
    windows_apps_test._control_from_hwnd = lambda _hwnd: root
    windows_apps_test.focused_page_search_field = lambda _names: None
    try:
        assert windows_apps_test._page_search_field(window, ("Search Google", "q")) is named
        assert windows_apps_test._page_search_field(window, ("unused-alias",)) is unnamed
    finally:
        windows_apps_test._control_from_hwnd = previous_hwnd
        windows_apps_test.focused_page_search_field = previous_focused


def test_wait_browser_page_ready_sees_first_heartbeat() -> None:
    state = windows_apps_test._PageState("isolated")
    assert state.page_ready is False

    def fill() -> None:
        time.sleep(0.02)
        with state.lock:
            state.page_ready = True
            state.value = "prefix selection suffix"

    worker = threading.Thread(target=fill)
    worker.start()
    windows_apps_test._wait_browser_page_ready(state, timeout_s=1.0)
    worker.join(timeout=1)
    assert state.page_ready is True
    assert state.value == "prefix selection suffix"


def test_render_uia_browser_page_keeps_non_t_dom() -> None:
    from dcent_voice.inject.windows_apps_test import (
        _render_uia_browser_page,
        existing_document_paths,
    )

    paths = existing_document_paths()
    html = _render_uia_browser_page(
        paths["edge-ce"],
        title="DCENT-edge-ce-token",
        baseline="BASE-unique",
    ).decode("utf-8")
    assert "DCENT-edge-ce-token" in html
    assert "BASE-unique" in html
    assert 'contenteditable="true"' in html
    assert 'id="t"' not in html


def test_chromium_hold_args_keep_a_normal_window_and_skip_first_run() -> None:
    args = windows_apps_test._chromium_hold_args(
        Path("msedge.exe"),
        Path("profile"),
        "http://127.0.0.1:9/",
    )
    assert "--new-window" in args
    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    assert "--window-size=1280,800" in args
    assert "--force-renderer-accessibility" in args
    assert "--disable-background-networking" in args
    assert args[-1] == "http://127.0.0.1:9/"
    live = windows_apps_test._chromium_hold_args(
        Path("msedge.exe"),
        Path("profile"),
        "https://duckduckgo.com/",
        live=True,
    )
    assert "--disable-background-networking" not in live
    assert live[-1] == "https://duckduckgo.com/"


def test_browser_control_transaction_retries_a_missed_clear_ack() -> None:
    state = windows_apps_test._PageState("isolated")
    records = []
    stop = threading.Event()

    def renderer() -> None:
        while not stop.is_set():
            with state.lock:
                generation = state.control_generation
                value = state.control_value
                select_all = state.control_select_all
                if generation >= 2:
                    state.ack_generation = generation
                    state.ack_value = value
                    state.ack_active_id = "t"
                    state.ack_selection_start = 0 if select_all else len(value)
                    state.ack_selection_end = len(value)
                    state.ack_count += 1
                    state.value = value
            time.sleep(0.001)

    worker = threading.Thread(target=renderer)
    worker.start()
    try:
        windows_apps_test._browser_control_transaction(
            state,
            expected="",
            select_all=False,
            previous_sentinel="D" + "a" * 32,
            stage="chrome.clear.run_5",
            records=records,
            attempt_timeout_s=0.08,
        )
    finally:
        stop.set()
        worker.join(timeout=1)

    assert records[0]["success"] is True
    assert records[0]["attempts"] == 2
    assert records[0]["previous_sentinel_absent"] is True


def test_browser_control_transaction_reports_bounded_clear_failure() -> None:
    state = windows_apps_test._PageState("isolated")
    state.value = "D" + "b" * 32
    records = []

    with pytest.raises(AssertionError, match="control transaction failed"):
        windows_apps_test._browser_control_transaction(
            state,
            expected="",
            select_all=False,
            previous_sentinel=state.value,
            stage="chrome.clear.run_5",
            records=records,
            attempts_limit=2,
            attempt_timeout_s=0.005,
        )

    assert records[0]["success"] is False
    assert records[0]["attempts"] == 2
    assert records[0]["failure_stage"] == "chrome.clear.run_5.verification"


def test_wait_for_page_value_observes_async_delivery_and_stability() -> None:
    state = windows_apps_test._PageState("isolated")

    def deliver() -> None:
        time.sleep(0.02)
        with state.lock:
            state.value = "recovered"

    worker = threading.Thread(target=deliver)
    worker.start()
    observed = windows_apps_test._wait_for_page_value(
        state, "recovered", timeout_s=0.5, stable_s=0.02
    )
    worker.join(timeout=1)

    assert observed == "recovered"


@pytest.mark.parametrize(
    ("refused", "browser_after", "distractor_after", "expected"),
    [
        (False, "basepayload", "", "recovered"),
        (True, "base", "", "refused"),
        (False, "base", "payload", "wrong_target"),
        (True, "changed", "", "mutated_after_refusal"),
        (False, "base", "", "not_delivered"),
        (False, "partial", "", "unexpected_target_state"),
    ],
)
def test_focus_theft_outcome_distinguishes_recovery_from_wrong_target(
    refused, browser_after, distractor_after, expected
) -> None:
    assert (
        windows_apps_test._focus_theft_outcome(
            refused=refused,
            before="base",
            expected_recovery="basepayload",
            browser_after=browser_after,
            distractor_after=distractor_after,
        )
        == expected
    )


@pytest.mark.parametrize("app_name", ["notepad", "vscode", "console_terminal", "edge", "chrome"])
def test_forced_noop_fails_every_matrix_app(app_name, tmp_path) -> None:
    class NoOpRouter:
        def resolve_decision(self, _text, _target):
            return SimpleNamespace(
                process_name="forced-noop.exe",
                configured_injector="clipboard",
                resolved_injector="clipboard",
                delivery="clipboard_ctrl_v_checked",
            )

        def inject_into_target_with_decision(self, _text, _target):
            return self.resolve_decision(_text, _target)

    result = windows_apps_test._inject_repeated(
        app=windows_apps_test._OwnedApp(app_name, tmp_path / "target.exe"),
        router=NoOpRouter(),
        payload_kind="short",
        case_name="forced_noop",
        runs=2,
        prepare=lambda _index, _payload, _previous, _records: SimpleNamespace(
            supports_edit_messages=False
        ),
        readback=lambda _index, _payload: "known empty baseline",
        clear_after=lambda _index, _target, _sentinel, _records: None,
    )

    assert result["exact"] is False
    assert result["success_count"] == 0
    assert result["runs"] == 2
    assert all("unique sentinel was not delivered" in error for error in result["errors"])


def test_app_report_includes_diagnostic_failures(tmp_path) -> None:
    report = windows_apps_test._app_report(
        windows_apps_test._OwnedApp("chrome", tmp_path / "chrome.exe"),
        [{"exact": True}],
        diagnostics={"focus_theft": {"success": False}},
    )
    assert report["status"] == "fail"


def test_benchmark_binds_exact_frozen_executable(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "dcent-voice.exe"
    executable.write_bytes(b"frozen-app-matrix")
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append(command)
        report_path = Path(command[command.index("--output-json") + 1])
        report_path.write_text('{"status":"pass","apps":[]}\n', encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(bench_injection_apps_windows.subprocess, "run", fake_run)
    code, report = bench_injection_apps_windows.run_benchmark(
        executable=executable,
        apps="notepad,chrome",
        runs=3,
        timeout_s=5.0,
    )

    assert code == 0
    assert observed[0][0] == str(executable.resolve())
    assert observed[0][1:6] == ["injection-app-matrix", "--apps", "notepad,chrome", "--runs", "3"]
    assert report["benchmark_process"]["mode"] == "frozen-executable"
    assert (
        report["benchmark_process"]["executable_sha256"] == sha256(b"frozen-app-matrix").hexdigest()
    )
