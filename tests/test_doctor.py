# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Unit tests for `dcent-voice doctor` (WS4).

Every check is exercised against a fabricated environment rather than this
machine, so the suite fails when a check stops describing reality — not when the
developer's laptop happens to be missing a microphone.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import types
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dcent_voice import doctor
from dcent_voice.doctor import cli, probe, report
from dcent_voice.doctor.checks import config as config_checks
from dcent_voice.doctor.checks import egress, environment, history, instance, launch, native_libs
from dcent_voice.doctor.checks import payload as payload_checks
from dcent_voice.doctor.result import FAIL, PASS, WARN, CheckResult, exit_code_for, summarize

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "doctor.schema.json"
PROGRAM_CS = REPO_ROOT / "packaging" / "windows" / "setup-stub" / "Program.cs"


@pytest.fixture
def profile_root(tmp_path, monkeypatch) -> Path:
    """Point every per-user path at a throwaway directory."""
    root = tmp_path / "profile"
    root.mkdir()
    monkeypatch.setenv("DCENT_VOICE_PROFILE_ROOT", str(root))
    return root


# --- result plumbing ---------------------------------------------------------


def test_check_result_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError):
        CheckResult("x.y", "broken", "detail")


def test_summary_and_exit_code_follow_the_worst_status() -> None:
    passing = [CheckResult("a.b", PASS, "ok")]
    warned = [*passing, CheckResult("c.d", WARN, "hmm")]
    failed = [*warned, CheckResult("e.f", FAIL, "no")]

    assert summarize(passing)["status"] == PASS
    assert summarize(warned)["status"] == WARN
    assert summarize(failed) == {"pass": 1, "warn": 1, "fail": 1, "total": 3, "status": FAIL}
    assert exit_code_for(passing) == 0
    assert exit_code_for(warned) == 0, "warnings must not fail the build"
    assert exit_code_for(failed) == 1


def test_result_data_is_json_safe() -> None:
    result = CheckResult("a.b", PASS, "ok", data={"path": Path("/tmp"), "items": (1, 2)})

    assert json.dumps(result.to_json())


# --- environment -------------------------------------------------------------


def test_environment_reports_the_profile_override(profile_root) -> None:
    result = environment.check_profile_paths()

    assert result.status == PASS
    assert result.data["profileRootOverride"] == str(profile_root)
    assert str(profile_root) in result.data["config"]


def test_write_access_fails_when_a_profile_directory_cannot_be_created(
    profile_root, monkeypatch
) -> None:
    def refuse(self, *args, **kwargs):  # noqa: ANN001, ARG001
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "mkdir", refuse)

    result = environment.check_write_access()

    assert result.status == FAIL
    assert "Access is denied" in result.detail
    assert result.remediation


def test_install_check_fails_without_the_bundled_example_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(environment.paths, "bundle_root", lambda: tmp_path)
    monkeypatch.setattr(environment.paths, "resource", lambda *p: tmp_path.joinpath(*p))

    result = environment.check_install_layout()

    assert result.status == FAIL
    assert "config.example.toml" in result.detail
    assert "Reinstall" in result.remediation


def test_install_check_records_whether_there_was_a_console(monkeypatch) -> None:
    """The report must say what the run saw, not leave it to be inferred."""
    monkeypatch.setattr(environment.sys, "stdout", None)
    monkeypatch.setattr(environment.sys, "stderr", None)

    result = environment.check_install_layout()

    assert result.data["stdout"].startswith("none")
    assert result.data["stderr"].startswith("none")


def test_stream_kind_describes_an_unusable_stream_without_raising() -> None:
    class Detached:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    assert environment._stream_kind(None).startswith("none")
    assert "unusable" in environment._stream_kind(Detached())


def test_architecture_fails_on_a_32_bit_process(monkeypatch) -> None:
    monkeypatch.setattr(environment.sys, "maxsize", 2**31 - 1)

    assert environment.check_architecture().status == FAIL


def test_redirected_paths_warns_for_a_onedrive_profile(monkeypatch, tmp_path) -> None:
    redirected = tmp_path / "OneDrive" / "AppData"
    redirected.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(redirected))
    monkeypatch.setattr(environment.paths, "user_config_dir", lambda: redirected)
    monkeypatch.setattr(environment.paths, "user_data_dir", lambda: redirected)
    monkeypatch.setattr(environment.paths, "profile_root", lambda: None)

    result = environment.check_redirected_paths()

    assert result.status == WARN
    assert "OneDrive" in result.detail
    assert "non-synced" in result.remediation


def test_disk_space_fails_when_the_drive_is_nearly_full(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(environment.paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(environment.paths, "user_config_dir", lambda: tmp_path)
    usage = types.SimpleNamespace(total=1, used=1, free=1024)
    monkeypatch.setattr(environment.shutil, "disk_usage", lambda _path: usage)

    result = environment.check_disk_space()

    assert result.status == FAIL
    assert "free" in result.detail


# --- payload -----------------------------------------------------------------


def test_payload_checks_are_skipped_in_a_source_checkout(monkeypatch) -> None:
    monkeypatch.setattr(payload_checks.paths, "is_frozen", lambda: False)

    results = payload_checks.run()

    assert [item.id for item in results] == [
        "payload.runtime_files",
        "payload.models",
        "payload.alternate_data_streams",
    ]
    assert all(item.status == PASS for item in results)


def test_payload_runtime_files_reports_every_missing_file(tmp_path) -> None:
    result = payload_checks.check_runtime_files(tmp_path)

    assert result.status == FAIL
    assert "missing: dcent-voice.exe" in result.detail
    assert result.data["missing"]


def test_payload_runtime_file_list_matches_the_installer(monkeypatch) -> None:
    """The C# installer and doctor must require the same payload."""
    source = PROGRAM_CS.read_text(encoding="utf-8")
    body = source.split("private static void ValidatePayload", 1)[1].split("ValidateModels", 1)[0]
    from_installer = set()
    for match in re.finditer(r"RequireRuntimeFile\(Path\.Combine\(root,([^)]*)\)", body):
        parts = re.findall(r'"([^"]+)"', match.group(1))
        from_installer.add("/".join(parts))
    # The offline bundle manifest is required by name outside the loop above too.
    assert from_installer, "could not parse ValidatePayload; update this test with Program.cs"

    assert set(payload_checks.REQUIRED_WINDOWS_FILES) == from_installer


def test_payload_models_reports_instead_of_raising(monkeypatch, tmp_path) -> None:
    from dcent_voice.asr import model_registry

    def explode(_payload):
        raise model_registry.ModelUnavailableError("vocab.txt: missing snapshot files")

    monkeypatch.setattr(model_registry, "verify_shipped_payload", explode)

    result = payload_checks.check_models(tmp_path)

    assert result.status == FAIL
    assert "vocab.txt" in result.detail
    assert "Reinstall" in result.remediation


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams are Windows-only")
def test_zone_identifier_is_a_warning_and_a_foreign_stream_is_a_failure(tmp_path) -> None:
    payload_file = tmp_path / "dcent-voice.exe"
    payload_file.write_text("stub", encoding="utf-8")
    Path(f"{payload_file}:Zone.Identifier").write_text(
        "[ZoneTransfer]\nZoneId=3\n", encoding="utf-8"
    )

    warned = payload_checks.check_alternate_data_streams(tmp_path)
    assert warned.status == WARN
    assert "Unblock-File" in warned.remediation

    Path(f"{payload_file}:SmartScreen").write_text("Anaheim", encoding="utf-8")
    failed = payload_checks.check_alternate_data_streams(tmp_path)
    assert failed.status == FAIL


# --- config ------------------------------------------------------------------


def test_config_checks_survive_a_missing_config(profile_root) -> None:
    results = {item.id: item for item in config_checks.run()}

    assert results["config.file"].status == WARN
    assert "no configuration file" in results["config.file"].detail
    assert results["config.profile"].status == PASS


def test_config_checks_survive_a_corrupt_config(profile_root, tmp_path) -> None:
    broken = tmp_path / "config.toml"
    broken.write_text("this is not = = valid toml [[", encoding="utf-8")

    results = {item.id: item for item in config_checks.run(config_path=broken)}

    assert results["config.file"].status == PASS
    assert results["config.profile"].status == FAIL
    assert "could not be loaded" in results["config.profile"].detail
    assert str(broken) in results["config.profile"].remediation


def test_config_reports_the_active_profile_and_resolves_its_model(
    profile_root, monkeypatch
) -> None:
    from dcent_voice.config import ensure_user_config

    config_path = ensure_user_config()
    monkeypatch.setattr(
        config_checks,
        "resolve_local_model",
        lambda _p, _m: (Path("C:/models/parakeet"), "verified"),
    )

    results = {item.id: item for item in config_checks.run(config_path=config_path)}

    assert results["config.profile"].status == PASS
    assert results["config.asr_model"].status == PASS
    assert results["config.unbundled_models"].status == PASS


def test_unresolvable_profile_models_warn_but_the_active_one_fails(
    profile_root, monkeypatch
) -> None:
    from dcent_voice.config import ensure_user_config

    config_path = ensure_user_config()
    monkeypatch.setattr(
        config_checks, "resolve_local_model", lambda _p, _m: (None, "not installed")
    )

    results = {item.id: item for item in config_checks.run(config_path=config_path)}

    assert results["config.asr_model"].status == FAIL
    assert results["config.unbundled_models"].status == WARN
    assert "not installed" in results["config.asr_model"].detail


# --- native probes -----------------------------------------------------------


def test_probe_runs_in_a_child_process_and_reports_success() -> None:
    result = probe.probe("PIL", timeout_s=120.0)

    assert result["ok"] is True
    assert result["file"]


def test_probe_reports_an_import_failure_without_raising(monkeypatch) -> None:
    """The child side turns any import error into a verdict, never a traceback."""
    monkeypatch.setitem(probe.PROBES, "PIL", ("dcent_voice_no_such_module", "fake"))

    result = probe._import_probe("PIL")

    assert result["ok"] is False
    assert "ModuleNotFoundError" in result["detail"]


def test_probe_child_verdict_survives_a_chatty_native_import(capsys) -> None:
    """A native banner printed to stdout must not corrupt the JSON verdict."""
    print("some DLL was loaded from somewhere")
    probe.emit("PIL")
    captured = capsys.readouterr().out

    assert probe._extract(captured)["ok"] is True


def test_probe_reports_a_hard_crash_as_a_finding(monkeypatch) -> None:
    """A native import that aborts the process must become a report line."""

    def crashing_argv(_probe_id: str) -> list[str]:
        return [sys.executable, "-c", "import os; os._exit(3)"]

    monkeypatch.setattr(probe, "_child_argv", crashing_argv)

    result = probe.probe("onnxruntime", timeout_s=120.0)

    assert result["ok"] is False
    assert result["crashed"] is True
    assert result["returncode"] == 3


def test_probe_reports_a_hang_as_a_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        probe, "_child_argv", lambda _p: [sys.executable, "-c", "import time; time.sleep(30)"]
    )

    result = probe.probe("pynput", timeout_s=1.0)

    assert result["ok"] is False
    assert result["timeout"] is True


def test_frozen_probe_reenters_the_executable(monkeypatch) -> None:
    monkeypatch.setattr(probe.sys, "frozen", True, raising=False)
    monkeypatch.setattr(probe.sys, "executable", r"C:\App\dcent-voice.exe")

    assert probe._child_argv("clr") == [r"C:\App\dcent-voice.exe", "doctor-probe", "clr"]


def test_missing_audio_device_is_a_warning_not_a_failure() -> None:
    result = native_libs.check_audio_inputs({"ok": True, "inputDevices": [], "defaultInput": None})

    assert result.status == WARN, "a CI runner without a microphone must not fail the build"
    assert "microphone" in result.remediation


def test_a_failed_ui_import_warns_while_a_failed_asr_import_fails() -> None:
    broken = {"ok": False, "detail": "DLL load failed"}

    assert native_libs._result_for("webview", broken).status == WARN
    assert native_libs._result_for("onnxruntime", broken).status == FAIL


def test_onnx_providers_require_a_cpu_backend() -> None:
    assert (
        native_libs.check_onnx_providers({"ok": True, "providers": ["CPUExecutionProvider"]}).status
        == PASS
    )
    assert native_libs.check_onnx_providers({"ok": True, "providers": []}).status == FAIL


# --- instance ----------------------------------------------------------------


def test_service_port_is_reported_as_taken_when_something_binds_it(profile_root) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]

        result = instance.check_service_port(port)

    assert result.status == WARN
    assert str(port) in result.detail
    assert result.remediation


def test_a_free_port_passes(profile_root) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]

    assert instance.check_service_port(port).status == PASS


def test_a_stale_lock_file_is_a_warning(profile_root) -> None:
    from dcent_voice.attach.registry import default_registry_dir

    lock = default_registry_dir() / "dcent-voice.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999999", encoding="utf-8")

    result = instance.check_lock_file()

    assert result.status == WARN
    assert "stale" in result.detail


@pytest.mark.skipif(sys.platform != "win32", reason="HKCU Run is a Windows concept")
def test_autostart_warns_when_it_points_at_a_missing_exe(monkeypatch, tmp_path) -> None:
    """Self-healing: the app rewrites its login item on every launch."""
    missing = tmp_path / "gone" / "dcent-voice.exe"
    monkeypatch.setattr(instance, "_first_quoted_or_word", lambda _cmd: str(missing))

    import contextlib
    import winreg

    # Patch OpenKey too. check_autostart treats an unopenable HKCU Run key as
    # "start at login is off" and returns PASS before it ever calls
    # QueryValueEx, so a host where that key cannot be opened (a CI runner in a
    # service session) would silently test nothing.
    @contextlib.contextmanager
    def _open_run_key(root, sub_key, *args, **kwargs):
        yield object()

    monkeypatch.setattr(winreg, "OpenKey", _open_run_key)

    original = winreg.QueryValueEx
    monkeypatch.setattr(
        winreg,
        "QueryValueEx",
        lambda key, name: (f'"{missing}"', 1) if name == "DCENT_Voice" else original(key, name),
    )

    result = instance.check_autostart()

    assert result.status == WARN, "the installer's self-check must not fail on this"
    assert "does not exist" in result.detail
    assert "repairs its login item" in result.remediation


# --- egress ------------------------------------------------------------------


def test_socket_monitor_records_remote_attempts_and_ignores_loopback() -> None:
    monitor = egress.SocketMonitor()
    monitor.record(("127.0.0.1", 8765), "socket.connect")
    monitor.record(("huggingface.co", 443), "socket.create_connection")
    monitor.record("/run/user/1000/bus", "socket.connect")

    assert len(monitor.attempts) == 3
    assert [item["host"] for item in monitor.remote_attempts] == ["huggingface.co"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("", True),
        ("8.8.8.8", False),
        ("huggingface.co", False),
    ],
)
def test_loopback_classification(host: str, expected: bool) -> None:
    assert egress.is_loopback(host) is expected


def test_egress_check_fails_when_a_remote_connection_is_attempted(monkeypatch) -> None:
    spec = types.SimpleNamespace(raw="parakeet:tdt-0.6b-v3:int8")
    monkeypatch.setattr(egress, "_active_asr_spec", lambda: (spec, False))
    monkeypatch.setattr(egress, "_load_model_bounded", lambda _spec, _timeout: _connect_somewhere())

    result = egress.check_egress(load_timeout_s=5.0, idle_s=0.0)

    assert result.status == FAIL
    assert "example.invalid" in result.detail


def _connect_somewhere() -> str:
    """Attempt a connection that the monitor must notice (and that always fails)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.01)
    try:
        sock.connect(("example.invalid", 443))
    except OSError:
        pass
    finally:
        sock.close()
    return "simulated load"


def test_egress_check_passes_when_nothing_leaves_the_machine(monkeypatch) -> None:
    monkeypatch.setattr(
        egress, "_active_asr_spec", lambda: (types.SimpleNamespace(raw="parakeet:x"), False)
    )
    monkeypatch.setattr(egress, "_load_model_bounded", lambda _s, _t: "the model loaded")

    result = egress.check_egress(load_timeout_s=5.0, idle_s=0.0)

    assert result.status == PASS
    assert result.data["remoteAttempts"] == []


def test_socket_monitor_restores_the_original_functions() -> None:
    original = socket.socket.connect
    with egress.SocketMonitor():
        assert socket.socket.connect is not original
    assert socket.socket.connect is original


# --- history -----------------------------------------------------------------


def test_last_startup_failure_is_reported_as_a_failure(profile_root) -> None:
    logs = profile_root / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "last-startup-failure.json").write_text(
        json.dumps({"timestamp": "2026-08-29T10:00:00Z", "message": "Cannot create config"}),
        encoding="utf-8",
    )

    result = history.check_last_startup_failure()

    assert result.status == FAIL
    assert "Cannot create config" in result.detail


def test_no_logs_at_all_is_a_warning(profile_root, monkeypatch, tmp_path) -> None:
    # Resolving the boot log creates it, so point at a path nothing has touched:
    # "the app died before it could open a log" is the state under test.
    monkeypatch.setattr(history, "boot_log_path", lambda: tmp_path / "absent" / "startup.log")

    assert history.check_logs().status == WARN


def test_history_uses_the_writers_own_path_helpers(profile_root, monkeypatch, tmp_path) -> None:
    """When the profile is unwritable the writers fall back to %TEMP%; follow them."""
    from dcent_voice.util import bootlog, fatal

    fallback = tmp_path / "temp-logs"
    fallback.mkdir()
    monkeypatch.setattr(bootlog, "probe_boot_log_path", lambda: fallback / "startup.log")
    monkeypatch.setattr(
        fatal, "failure_record_path", lambda *_a, **_k: fallback / fatal.FAILURE_FILENAME
    )
    (fallback / "startup.log").write_text("boot line\n", encoding="utf-8")

    assert history.boot_log_path() == fallback / "startup.log"
    assert history.failure_record_path() == fallback / fatal.FAILURE_FILENAME
    result = history.check_logs()
    assert result.status == PASS
    assert result.data["files"]["startup.log"]["path"] == str(fallback / "startup.log")


def test_asking_where_the_startup_log_is_does_not_create_it(profile_root) -> None:
    """Doctor must be able to say "no log exists" without making one exist."""
    path = history.boot_log_path()

    assert not path.exists(), "resolving the boot log path must have no side effect"
    assert history.check_logs().status == WARN
    assert not path.exists()


def test_log_files_never_lists_the_same_file_twice(profile_root) -> None:
    listed = history.log_files()

    assert len(listed) == len({str(path).casefold() for path in listed})


def test_log_tail_returns_the_last_lines(profile_root) -> None:
    logs = profile_root / "config" / "logs"
    logs.mkdir(parents=True)
    path = logs / "dcent_voice.log"
    path.write_text("\n".join(f"line {index}" for index in range(500)), encoding="utf-8")

    result = history.check_logs()

    assert result.status == PASS
    tail = result.data["files"]["dcent_voice.log"]["tail"]
    assert tail[-1] == "line 499"
    assert len(tail) == history.TAIL_LINES


# --- launch ------------------------------------------------------------------


def test_launch_checks_are_skipped_on_request() -> None:
    (result,) = launch.run(enabled=False)

    assert result.status == PASS
    assert "skipped" in result.detail


def test_launch_env_isolates_the_profile_mutex_and_autostart(tmp_path) -> None:
    env = launch.launch_env(tmp_path)

    assert env["DCENT_VOICE_PROFILE_ROOT"] == str(tmp_path)
    assert env["DCENT_VOICE_DISABLE_AUTOSTART"] == "1"
    assert env["DCENT_VOICE_NO_DIALOGS"] == "1"
    assert env["DCENT_VOICE_SMOKE_MUTEX"].startswith("Local\\DCENT_Voice_Smoke_")


def test_launch_reports_a_failed_seed_as_the_fresh_machine_bug(monkeypatch) -> None:
    monkeypatch.setattr(
        launch,
        "_seed_config",
        lambda *_a, **_k: (False, "--print-config exited 2: example file is missing", None),
    )

    result = launch.check_fresh_profile_launch(timeout_s=1.0)

    assert result.status == FAIL
    assert "never run it" in result.detail
    assert "Reinstall" in result.remediation


def test_health_poll_never_trusts_proxy_environment(monkeypatch) -> None:
    """The trial instance's bearer token must not be sent to a proxy.

    httpx's module-level API has trust_env=True, so HTTP_PROXY/ALL_PROXY would
    route this loopback request — Authorization header and all — through
    whatever the environment names.
    """
    import httpx

    captured: dict[str, object] = {}

    class RecordingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            raise RuntimeError("stop the poll after one attempt")

    monkeypatch.setattr(httpx, "Client", RecordingClient)
    process = types.SimpleNamespace(poll=lambda: None)

    launch._wait_for_health(49999, "secret-token", timeout_s=0.05, process=process)

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert captured["url"].startswith("http://127.0.0.1:")
    assert captured["headers"]["Authorization"] == "Bearer secret-token"


def test_no_http_call_in_the_doctor_package_trusts_the_environment() -> None:
    """A future call site must not reintroduce the proxy leak."""
    package = Path(cli.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "``" in line:
                continue
            # Module-level httpx helpers default to trust_env=True.
            if re.search(r"httpx\.(get|post|put|patch|delete|head|request|stream)\(", line):
                offenders.append(f"{path.name}:{number}: {stripped}")
            if "httpx.Client(" in line and "trust_env=False" not in text:
                offenders.append(f"{path.name}:{number}: {stripped}")

    assert offenders == [], (
        "doctor must build HTTP clients with trust_env=False so a proxy cannot "
        "receive the loopback session token:\n" + "\n".join(offenders)
    )


def test_launch_argv_uses_the_frozen_executable(monkeypatch) -> None:
    monkeypatch.setattr(launch.paths, "is_frozen", lambda: True)
    monkeypatch.setattr(launch.sys, "executable", r"C:\App\dcent-voice.exe")

    assert launch.launch_argv() == [r"C:\App\dcent-voice.exe"]


# --- report ------------------------------------------------------------------


def _sample_report(results: list[CheckResult]) -> dict:
    now = datetime.now(UTC)
    return report.build_report(results, started_at=now, finished_at=now, launch_checks=False)


def test_report_validates_against_the_published_schema(profile_root, tmp_path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    payload = _sample_report(
        [
            CheckResult("env.os", PASS, "Windows 10 build 19045", data={"build": 19045}),
            CheckResult("payload.models", FAIL, "bad", "reinstall", {"path": str(tmp_path)}),
        ]
    )

    jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_every_produced_check_id_matches_the_schema_pattern(profile_root) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    pattern = re.compile(schema["properties"]["checks"]["items"]["properties"]["id"]["pattern"])
    ids = _collect_declared_check_ids()

    assert ids, "no check ids were discovered"
    for check_id in ids:
        assert pattern.match(check_id), check_id


def _collect_declared_check_ids() -> set[str]:
    """Every literal check id in the checks package, straight from the source."""
    root = Path(cli.__file__).parent / "checks"
    found: set[str] = set()
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        found.update(
            re.findall(
                r'"((?:env|payload|config|native|ui|instance|egress|history|launch)\.[a-z_]+)"',
                text,
            )
        )
    return found


def test_write_report_produces_json_text_and_a_zip(profile_root, tmp_path) -> None:
    logs = profile_root / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "dcent_voice.log").write_text("hello\n", encoding="utf-8")
    (profile_root / "config" / "config.toml").write_text(
        'active_profile = "desktop"\napi_key = "sk-abcdefghijklmnopqrstuvwx"\n', encoding="utf-8"
    )
    payload = _sample_report([CheckResult("env.os", PASS, "fine")])

    written = report.write_report(payload, output_dir=tmp_path, stamp="20260829-000000")

    assert written["json"].name == "doctor-20260829-000000.json"
    assert written["text"].read_text(encoding="utf-8").startswith("DCENT_Voice diagnostics")
    with zipfile.ZipFile(written["zip"]) as bundle:
        names = set(bundle.namelist())
        assert "logs/dcent_voice.log" in names
        assert "config.redacted.toml" in names
        redacted = bundle.read("config.redacted.toml").decode("utf-8")
    assert "sk-abcdefghijklmnopqrstuvwx" not in redacted
    assert report.REDACTED in redacted


def test_json_path_option_writes_a_second_copy(profile_root, tmp_path) -> None:
    target = tmp_path / "nested" / "out.json"
    payload = _sample_report([CheckResult("env.os", PASS, "fine")])

    written = report.write_report(
        payload, output_dir=tmp_path, stamp="s", json_path=target, make_zip=False
    )

    assert written["requestedJson"] == target
    assert json.loads(target.read_text(encoding="utf-8"))["summary"]["status"] == PASS
    assert "zip" not in written


@pytest.mark.parametrize(
    "line",
    [
        'api_key = "sk-live-1234567890abcdef"',
        'token="abcdefghijklmnopqrstuvwxyz0123456789"',
        "  client_secret = 'hunter2'",
        'password = "hunter2"',
        'Authorization = "Bearer abcdefghijklmnopqrstuvwxyz"',
    ],
)
def test_redaction_removes_credential_shaped_values(line: str) -> None:
    redacted = report.redact_config(line)

    assert report.REDACTED in redacted
    assert "hunter2" not in redacted
    assert "sk-live-1234567890abcdef" not in redacted


def test_redaction_keeps_ordinary_settings_readable() -> None:
    text = 'active_profile = "desktop"\n[service]\nport = 8765\n'

    assert report.redact_config(text) == text


def test_default_output_dir_follows_the_profile_root(profile_root) -> None:
    assert report.default_output_dir() == profile_root / "diagnostics"


# --- CLI ---------------------------------------------------------------------


class _Args:
    def __init__(self, **kwargs) -> None:
        self.json = kwargs.get("json")
        self.open = kwargs.get("open", False)
        self.no_launch_checks = kwargs.get("no_launch_checks", True)
        self.zip = kwargs.get("zip", False)


def test_cli_exits_zero_when_everything_passes_or_warns(profile_root, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "collect",
        lambda **_k: [CheckResult("env.os", PASS, "ok"), CheckResult("ui.webview2", WARN, "no")],
    )

    assert cli.main(_Args()) == 0


def test_cli_exits_one_on_a_failure(profile_root, monkeypatch) -> None:
    monkeypatch.setattr(cli, "collect", lambda **_k: [CheckResult("payload.models", FAIL, "bad")])

    assert cli.main(_Args()) == 1


def test_cli_exits_two_when_doctor_itself_cannot_run(profile_root, monkeypatch) -> None:
    def explode(**_kwargs):
        raise RuntimeError("the report directory vanished")

    monkeypatch.setattr(cli, "run_doctor", explode)

    assert cli.main(_Args()) == cli.EXIT_COULD_NOT_RUN


def test_a_broken_check_module_becomes_a_failure_not_a_crash() -> None:
    def explode():
        raise ZeroDivisionError("boom")

    (result,) = cli._guarded("payload", explode)

    assert result.status == FAIL
    assert result.id == "payload.internal_error"
    assert "ZeroDivisionError" in result.detail


def test_no_dialogs_env_suppresses_the_message_box(monkeypatch) -> None:
    monkeypatch.setenv(cli.NO_DIALOGS_ENV, "1")

    assert cli._show_dialog("title", "message") is False


def test_no_open_env_suppresses_the_file_manager(monkeypatch) -> None:
    """A test run must never make an Explorer window appear on someone's desktop."""
    launched: list[str] = []
    monkeypatch.setattr(cli.os, "startfile", lambda target: launched.append(target), raising=False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: launched.append(str(a)))
    monkeypatch.setenv(cli.NO_OPEN_ENV, "1")

    assert cli.open_diagnostics_folder() is False
    assert launched == []


def test_cli_open_flag_is_inert_when_opening_is_suppressed(profile_root, monkeypatch) -> None:
    opened: list[object] = []
    real_popen = cli.subprocess.Popen

    def recording_popen(argv, *args, **kwargs):
        # Only a file-manager launch counts; report.py legitimately shells out
        # to icacls while writing, and must keep working.
        first = str(argv[0]) if isinstance(argv, list | tuple) and argv else str(argv)
        if "open" in first:
            opened.append(first)
            return None
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(cli, "collect", lambda **_k: [CheckResult("env.os", PASS, "ok")])
    monkeypatch.setattr(cli.os, "startfile", lambda target: opened.append(target), raising=False)
    monkeypatch.setattr(cli.subprocess, "Popen", recording_popen)
    monkeypatch.setenv(cli.NO_OPEN_ENV, "1")

    assert cli.main(_Args(open=True)) == 0
    assert opened == []


def test_the_test_suite_itself_is_isolated_from_the_real_profile() -> None:
    """The conftest fixture is load-bearing: assert it is actually in effect."""
    from dcent_voice.util import paths

    root = paths.profile_root()
    assert root is not None, "tests must never run against the real user profile"
    assert str(paths.user_config_dir()).startswith(str(root))
    assert os.environ.get("DCENT_VOICE_NO_DIALOGS") == "1"
    assert os.environ.get("DCENT_VOICE_NO_OPEN") == "1"
    assert report.default_output_dir() == root / "diagnostics"


def test_public_hooks_have_the_documented_shape() -> None:
    assert doctor.start_menu_shortcut_args() == ["doctor", "--open"]
    assert callable(doctor.open_diagnostics_folder)
    assert callable(doctor.run_doctor_in_background)
    assert callable(doctor.run_doctor)


def test_run_doctor_in_background_notifies_and_opens_the_folder(profile_root, monkeypatch) -> None:
    notified: list[tuple[str, str]] = []
    opened: list[object] = []
    monkeypatch.setattr(
        cli, "collect", lambda **_k: [CheckResult("env.os", PASS, "ok")], raising=True
    )
    monkeypatch.setattr(
        cli, "open_diagnostics_folder", lambda path=None: opened.append(path) or True
    )

    thread = doctor.run_doctor_in_background(
        lambda title, message: notified.append((title, message))
    )
    thread.join(timeout=120)

    assert not thread.is_alive()
    assert opened, "the tray hook must open the diagnostics folder"
    assert notified and "Diagnostics" in notified[0][1]


def test_doctor_runs_before_the_config_is_loaded(profile_root, monkeypatch) -> None:
    """The whole point: a corrupt config must not stop the diagnosis."""
    from dcent_voice import app

    def refuse(*_args, **_kwargs):
        raise AssertionError("doctor must not load the configuration")

    monkeypatch.setattr(app, "load_config", refuse)
    monkeypatch.setattr(cli, "collect", lambda **_k: [CheckResult("env.os", PASS, "ok")])

    assert app.main(["doctor", "--no-launch-checks", "--no-zip"]) == 0


def test_doctor_probe_subcommand_is_reachable_from_the_cli(profile_root) -> None:
    from dcent_voice import app

    assert app.main(["doctor-probe", "PIL"]) == 0


def test_ci_marker_exists_so_the_doctor_gate_runs() -> None:
    assert (REPO_ROOT / "scripts" / "qa" / "doctor-enabled").is_file()


def test_doctor_cli_survives_having_no_stdout_or_stderr_at_all(profile_root, monkeypatch) -> None:
    """The Start Menu shortcut launches detached: there is no console to write to.

    A windowed PyInstaller build has sys.stdout is None, so any unguarded
    sys.stdout.write / .isatty in the doctor package would turn the shortcut
    into an AttributeError the user never sees.
    """
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "stdout", None)
    monkeypatch.setattr(cli.sys, "stderr", None)
    monkeypatch.setenv(cli.NO_DIALOGS_ENV, "1")
    monkeypatch.setattr(cli, "collect", lambda **_k: [CheckResult("env.os", PASS, "ok")])

    assert cli.main(_Args(open=True)) == 0
    assert list((profile_root / "diagnostics").glob("doctor-*.json"))


def test_doctor_cli_reports_its_own_failure_without_a_console(profile_root, monkeypatch) -> None:
    """Even the 'doctor could not run' path must not need a stream."""
    monkeypatch.setattr(cli.sys, "stdout", None)
    monkeypatch.setattr(cli.sys, "stderr", None)
    monkeypatch.setenv(cli.NO_DIALOGS_ENV, "1")

    def explode(**_kwargs):
        raise RuntimeError("the report directory vanished")

    monkeypatch.setattr(cli, "run_doctor", explode)

    assert cli.main(_Args()) == cli.EXIT_COULD_NOT_RUN


def test_is_windowed_treats_a_missing_stream_as_windowed(monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "stdout", None)

    assert cli._is_windowed() is True


def test_probe_emit_survives_a_missing_stdout(monkeypatch) -> None:
    """`doctor-probe` launched detached has no stdout; it must not crash."""
    monkeypatch.setattr(probe.sys, "stdout", None)

    assert probe.emit("PIL") == 0
    monkeypatch.setitem(probe.PROBES, "PIL", ("dcent_voice_no_such_module", "fake"))
    # The exit code still carries the verdict when the JSON has nowhere to go.
    assert probe.emit("PIL") == 1


def test_frozen_windowed_run_writes_files_without_a_console(monkeypatch, profile_root) -> None:
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "stdout", None)
    monkeypatch.setenv(cli.NO_DIALOGS_ENV, "1")
    monkeypatch.setattr(cli, "collect", lambda **_k: [CheckResult("env.os", PASS, "ok")])

    assert cli.main(_Args()) == 0
    assert list((profile_root / "diagnostics").glob("doctor-*.json"))


def test_smoke_run_end_to_end_without_launch_checks(profile_root) -> None:
    """A real run against this machine: it must always produce a valid report."""
    outcome = cli.run_doctor(launch_checks=False, make_zip=False, probe_timeout_s=180.0)

    assert outcome.report["summary"]["total"] == len(outcome.results)
    assert outcome.files["json"].is_file()
    assert outcome.exit_code in (0, 1)
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(
        json.loads(outcome.files["json"].read_text(encoding="utf-8")),
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
    )


def test_subprocess_cli_help_lists_doctor() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "dcent_voice", "doctor", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0
    assert "--no-launch-checks" in completed.stdout
    assert "--open" in completed.stdout


# --- tray runs doctor out of process (MAJOR-1) --------------------------------


def test_tray_runs_doctor_in_a_child_process_not_in_the_live_app(
    profile_root, monkeypatch, tmp_path
) -> None:
    """The tray must never load a second model or patch sockets in the live app."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        target = Path(argv[argv.index("--json") + 1])
        target.write_text(
            json.dumps(
                {
                    "summary": {"pass": 3, "warn": 1, "fail": 0, "total": 4, "status": "warn"},
                    "artifacts": {
                        "json": str(tmp_path / "doctor.json"),
                        "text": str(tmp_path / "doctor.txt"),
                        "zip": str(tmp_path / "bundle.zip"),
                    },
                }
            ),
            encoding="utf-8",
        )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    # _run_doctor_child imports subprocess locally, so patch the module itself.
    import subprocess as _subprocess

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(
        doctor, "run_doctor", lambda **_k: pytest.fail("the tray must not run checks in-process")
    )
    notified: list[tuple[str, str]] = []
    opened: list[object] = []
    monkeypatch.setattr(doctor, "open_diagnostics_folder", lambda path=None: opened.append(path))

    thread = doctor.run_doctor_in_background(lambda t, m: notified.append((t, m)))
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert calls, "no child process was spawned"
    argv = calls[0]
    assert "doctor" in argv and "--no-launch-checks" in argv
    assert notified and "1 warnings" in notified[0][1]
    assert str(tmp_path / "bundle.zip") in notified[0][1]
    assert opened, "the tray should reveal the diagnostics folder"


def test_tray_child_argv_reenters_the_frozen_executable(monkeypatch) -> None:
    import sys as _sys

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(_sys, "executable", r"C:\App\dcent-voice.exe")

    argv = doctor._child_argv(Path("out.json"), launch_checks=False)

    assert argv[0] == r"C:\App\dcent-voice.exe"
    assert argv[1] == "doctor"
    assert "--no-launch-checks" in argv


def test_tray_falls_back_in_process_with_egress_skipped(profile_root, monkeypatch) -> None:
    """If spawning is impossible, still report — but never patch the live app."""

    def refuse(*_a, **_k):
        raise OSError("CreateProcess failed")

    monkeypatch.setattr(doctor, "_run_doctor_child", refuse)
    captured: dict[str, object] = {}

    def fake_run_doctor(**kwargs):
        captured.update(kwargs)
        return cli.DoctorOutcome(
            results=[CheckResult("env.os", PASS, "ok")],
            report={"summary": {"pass": 1, "warn": 0, "fail": 0, "total": 1, "status": "pass"}},
            files={},
        )

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)
    monkeypatch.setattr(doctor, "open_diagnostics_folder", lambda path=None: True)
    notified: list[tuple[str, str]] = []

    thread = doctor.run_doctor_in_background(lambda t, m: notified.append((t, m)))
    thread.join(timeout=60)

    assert captured["include_egress"] is False, "must not patch sockets inside the live app"
    assert notified and "in-process fallback" in notified[0][1]


def test_collect_can_skip_egress_and_says_why(profile_root, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_guarded", lambda _label, _runner: [])

    results = cli.collect(launch_checks=False, include_egress=False)

    egress_results = [item for item in results if item.id == "egress.connections"]
    assert len(egress_results) == 1
    assert egress_results[0].status == PASS
    assert "Start Menu" in egress_results[0].remediation


# --- socket monitor re-entrancy (MAJOR-1) -------------------------------------


def test_a_nested_socket_monitor_is_a_no_op_returning_the_outer_one() -> None:
    original = socket.socket.connect
    outer = egress.SocketMonitor()
    with outer as owner:
        assert owner is outer
        patched = socket.socket.connect
        inner = egress.SocketMonitor()
        with inner as effective:
            assert effective is outer, "a nested install must hand back the outer monitor"
            assert socket.socket.connect is patched, "the patches must not be re-applied"
        # The inner monitor exiting must NOT unpatch the outer one's wrappers.
        assert socket.socket.connect is patched
    assert socket.socket.connect is original


def test_nested_monitor_records_into_the_owner() -> None:
    outer = egress.SocketMonitor()
    with outer:
        inner = egress.SocketMonitor()
        with inner as effective:
            effective.record(("example.invalid", 443), "socket.connect")
    assert [entry["host"] for entry in outer.remote_attempts] == ["example.invalid"]


# --- DNS observation (MINOR-5) ------------------------------------------------


def test_the_monitor_sees_a_remote_name_lookup() -> None:
    monitor = egress.SocketMonitor()
    with monitor, contextlib.suppress(OSError):
        socket.getaddrinfo("dcent-voice-test.invalid", 443)
    remote = monitor.remote_attempts
    assert any(entry["api"] == "socket.getaddrinfo" for entry in remote)
    assert any(entry["kind"] == "resolve" for entry in remote)


def test_loopback_name_lookups_are_not_reported() -> None:
    monitor = egress.SocketMonitor()
    with monitor, contextlib.suppress(OSError):
        socket.getaddrinfo("127.0.0.1", 80)
    assert monitor.remote_attempts == []


def test_getaddrinfo_is_restored_after_the_monitor_exits() -> None:
    original = socket.getaddrinfo
    with egress.SocketMonitor():
        assert socket.getaddrinfo is not original
    assert socket.getaddrinfo is original


# --- port retargeting (L14) and the bind race (L15) ---------------------------


def test_port_rewrite_is_anchored_to_the_service_table(tmp_path) -> None:
    """An earlier [overlay] port must not absorb the edit and strand [service]."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[overlay]\nport = 1111\n\n[service]\nenabled = true\nport = 8765\n"
        "\n[audio]\nport = 2222\n",
        encoding="utf-8",
    )

    assert launch._retarget_port(config, 49999) is True

    assert config.read_text(encoding="utf-8") == (
        "[overlay]\nport = 1111\n\n[service]\nenabled = true\nport = 49999\n"
        "\n[audio]\nport = 2222\n"
    )


def test_port_rewrite_refuses_a_config_without_a_service_table(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[overlay]\nport = 1111\n", encoding="utf-8")

    assert launch._retarget_port(config, 49999) is False
    assert "port = 1111" in config.read_text(encoding="utf-8")


def test_launch_retries_once_when_the_port_was_stolen(monkeypatch) -> None:
    collision = CheckResult(
        "launch.fresh_profile",
        FAIL,
        "never published its token",
        "",
        {"childLogs": {"startup.log": ["OSError: [Errno 48] Address already in use"]}},
    )
    success = CheckResult("launch.fresh_profile", PASS, "ready in 3.0 s")
    outcomes = [collision, success]
    monkeypatch.setattr(launch, "_launch", lambda *_a, **_k: outcomes.pop(0))

    result = launch.check_fresh_profile_launch(timeout_s=1.0)

    assert result.status == PASS
    assert outcomes == [], "the retry should have consumed the second outcome"


def test_launch_does_not_retry_an_unrelated_failure(monkeypatch) -> None:
    failure = CheckResult(
        "launch.fresh_profile", FAIL, "config could not be seeded", "", {"childLogs": {}}
    )
    calls: list[int] = []

    def once(*_a, **_k):
        calls.append(1)
        return failure

    monkeypatch.setattr(launch, "_launch", once)

    result = launch.check_fresh_profile_launch(timeout_s=1.0)

    assert result.status == FAIL
    assert len(calls) == 1, "only a port collision justifies a second launch"


# --- diagnostics permissions and self-description (L16) -----------------------


def test_the_report_names_its_own_artifacts(profile_root, tmp_path) -> None:
    payload = _sample_report([CheckResult("env.os", PASS, "ok")])

    written = report.write_report(payload, output_dir=tmp_path, stamp="20260830-000000")

    saved = json.loads(written["json"].read_text(encoding="utf-8"))
    assert saved["artifacts"]["json"] == str(written["json"])
    assert saved["artifacts"]["text"] == str(written["text"])
    assert saved["artifacts"]["zip"] == str(written["zip"])


def test_artifacts_block_validates_against_the_schema(profile_root, tmp_path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    payload = _sample_report([CheckResult("env.os", PASS, "ok")])
    written = report.write_report(payload, output_dir=tmp_path, stamp="s", make_zip=False)

    jsonschema.validate(
        json.loads(written["json"].read_text(encoding="utf-8")),
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_diagnostics_are_written_owner_only(profile_root, tmp_path) -> None:
    payload = _sample_report([CheckResult("env.os", PASS, "ok")])

    written = report.write_report(payload, output_dir=tmp_path / "diag", stamp="s")

    assert (tmp_path / "diag").stat().st_mode & 0o077 == 0
    for key in ("json", "text", "zip"):
        assert written[key].stat().st_mode & 0o077 == 0, f"{key} is readable by others"


def test_secure_directory_reports_failure_rather_than_raising(tmp_path) -> None:
    missing = tmp_path / "does-not-exist"

    assert report.secure_directory(missing) is False


def test_report_is_still_written_when_locking_down_fails(
    profile_root, tmp_path, monkeypatch
) -> None:
    """A report the user cannot obtain is worse than one at default permissions."""
    from dcent_voice.attach import registry

    def refuse(*_a, **_k):
        raise PermissionError("icacls unavailable")

    monkeypatch.setattr(registry, "write_text_atomic", refuse)
    monkeypatch.setattr(report, "secure_directory", lambda _p: False)
    payload = _sample_report([CheckResult("env.os", PASS, "ok")])

    written = report.write_report(payload, output_dir=tmp_path, stamp="s", make_zip=False)

    assert written["json"].is_file()
    assert json.loads(written["json"].read_text(encoding="utf-8"))["summary"]["status"] == PASS
