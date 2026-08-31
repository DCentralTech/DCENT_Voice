# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dcent_voice.config import parse_config
from scripts.smoke_app import _ISOLATED_SOURCE_BOOTSTRAP, build_command, build_parser, smoke_config


def test_smoke_config_is_valid() -> None:
    import tomllib

    config = parse_config(tomllib.loads(smoke_config(54321)))

    assert config.active_profile == "tiny"
    assert config.service.port == 54321
    assert config.overlay.enabled is False
    assert config.hotkeys.dictation == "ctrl+win"
    assert config.hotkeys.command == "off"
    assert config.hotkeys.streaming == "off"


def test_packaged_smoke_uses_the_bundled_offline_default() -> None:
    import tomllib

    config = parse_config(tomllib.loads(smoke_config(54321, packaged=True)))

    assert config.active_profile == "desktop"
    assert config.profiles["desktop"].asr.raw == "parakeet:tdt-0.6b-v3:int8"


def test_isolated_source_smoke_uses_a_unique_mutex_bootstrap(tmp_path) -> None:
    command = build_command(None, tmp_path / "config.toml", isolate_source=True)

    assert command[:3] == [sys.executable, "-c", _ISOLATED_SOURCE_BOOTSTRAP]
    assert "DCENT_VOICE_SMOKE_MUTEX" in _ISOLATED_SOURCE_BOOTSTRAP
    assert command[-3:] == ["--no-tray", "--no-hotkeys", "--no-overlay"]


def test_smoke_isolation_is_enabled_by_default() -> None:
    assert build_parser().parse_args([]).isolate is True


def test_packaged_smoke_command_remains_a_direct_executable_launch(tmp_path) -> None:
    executable = Path("C:/DCENT_Voice/dcent-voice.exe")

    command = build_command(executable, tmp_path / "config.toml", isolate_source=True)

    assert command[0] == str(executable)
    assert _ISOLATED_SOURCE_BOOTSTRAP not in command


# --- scripts/fresh_profile_smoke.py (WS1 fresh-machine proof harness) --------

ROOT = Path(__file__).resolve().parents[1]
FRESH_SCRIPT = ROOT / "scripts" / "fresh_profile_smoke.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("fresh_profile_smoke", FRESH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_fresh_profile_parser_requires_an_executable(smoke) -> None:
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args([])


def test_fresh_profile_parser_defaults(smoke) -> None:
    args = smoke.build_parser().parse_args(["--executable", "dist/DCENT_Voice/dcent-voice.exe"])

    assert args.executable == Path("dist/DCENT_Voice/dcent-voice.exe")
    assert args.cwd is None
    assert args.profile_root is None
    assert args.port == 0
    assert args.seed_only is False


def test_neutral_cwd_defaults_to_a_directory_with_no_project_files(smoke) -> None:
    default = smoke._neutral_cwd(None)

    assert default == (Path("C:\\") if os.name == "nt" else Path("/"))
    # The bug hid because CI ran from the repo root, where config.example.toml
    # happens to exist. The default cwd must never be such a directory.
    assert not (default / "config.example.toml").exists()


def test_neutral_cwd_honors_an_explicit_request(smoke, tmp_path) -> None:
    assert smoke._neutral_cwd(tmp_path) == tmp_path


def test_fresh_profile_env_isolates_profile_mutex_and_autostart(smoke, tmp_path) -> None:
    env = smoke.smoke_env(tmp_path)

    assert env["DCENT_VOICE_PROFILE_ROOT"] == str(tmp_path)
    assert env["DCENT_VOICE_DISABLE_AUTOSTART"] == "1"
    # single_instance.py only honours this exact prefix for the override.
    assert env["DCENT_VOICE_SMOKE_MUTEX"].startswith("Local\\DCENT_Voice_Smoke_")
    # WS2 turns startup failures into modal dialogs. Now that phase 2 launches
    # detached with no pipes, a MessageBox nobody can dismiss would hang an
    # unattended run until its timeout instead of failing with the real reason.
    assert env["DCENT_VOICE_NO_DIALOGS"] == "1"
    assert "DCENT_VOICE_MODEL_DIR" not in env


def test_fresh_profile_mutex_is_unique_per_run(smoke, tmp_path) -> None:
    assert (
        smoke.smoke_env(tmp_path)["DCENT_VOICE_SMOKE_MUTEX"]
        != smoke.smoke_env(tmp_path)["DCENT_VOICE_SMOKE_MUTEX"]
    )


def test_expected_config_path_lives_under_the_profile_root(smoke, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DCENT_VOICE_PROFILE_ROOT", raising=False)

    path = smoke.expected_config_path(tmp_path)

    assert tmp_path in path.parents
    assert path.name == "config.toml"
    # The probe must not leak its override into this process.
    assert "DCENT_VOICE_PROFILE_ROOT" not in os.environ


def test_registry_dir_lives_under_the_profile_root(smoke, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DCENT_VOICE_PROFILE_ROOT", raising=False)

    registry_dir = smoke.registry_dir_for(tmp_path)

    assert tmp_path in registry_dir.parents
    assert "DCENT_VOICE_PROFILE_ROOT" not in os.environ


def test_set_service_port_rewrites_the_seeded_example(smoke, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")

    smoke.set_service_port(config, 54321)

    text = config.read_text(encoding="utf-8")
    assert "port = 54321" in text
    assert "port = 8765" not in text


def test_set_service_port_fails_loudly_when_there_is_no_port(smoke, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('active_profile = "desktop"\n', encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure):
        smoke.set_service_port(config, 1234)


def test_seeding_phase_refuses_a_profile_root_that_is_not_fresh(smoke, tmp_path) -> None:
    config = smoke.expected_config_path(tmp_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('active_profile = "desktop"\n', encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure, match="not fresh"):
        smoke.phase_seed(
            Path(sys.executable), tmp_path, smoke.smoke_env(tmp_path), tmp_path, timeout_s=5.0
        )


def test_asr_readiness_rejects_a_non_parakeet_provider(smoke) -> None:
    """The identity guard runs before any HTTP call, so no server is needed."""
    with pytest.raises(smoke.SmokeFailure, match="Parakeet"):
        smoke.assert_asr_ready(
            0,
            token="t",
            health={"subsystems": {"asr": {"ok": True, "provider": "FasterWhisperASRProvider"}}},
            profile_root=Path("."),
        )


def test_asr_readiness_rejects_an_unhealthy_asr_subsystem(smoke) -> None:
    with pytest.raises(smoke.SmokeFailure, match="not ok"):
        smoke.assert_asr_ready(
            0,
            token="t",
            health={"subsystems": {"asr": {"ok": False, "provider": "ParakeetASRProvider"}}},
            profile_root=Path("."),
        )


def test_parakeet_load_assertion_reads_the_log(smoke, tmp_path) -> None:
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    log = logs / "dcent_voice.log"

    log.write_text("nothing interesting\n", encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="never recorded"):
        smoke.assert_parakeet_was_loaded(tmp_path)

    log.write_text(
        "loading verified Parakeet weights from X\nparakeet ready model=nemo\n", encoding="utf-8"
    )
    smoke.assert_parakeet_was_loaded(tmp_path)


def test_parakeet_load_assertion_rejects_a_silent_whisper_fallback(smoke, tmp_path) -> None:
    """A fallback would make the smoke pass while shipping the wrong engine."""
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "dcent_voice.log").write_text(
        "parakeet ready model=nemo\nverified Parakeet unavailable; using pinned "
        "local Faster Whisper base\n",
        encoding="utf-8",
    )

    with pytest.raises(smoke.SmokeFailure, match="fell back"):
        smoke.assert_parakeet_was_loaded(tmp_path)


def test_log_assertion_requires_a_written_log(smoke, tmp_path) -> None:
    with pytest.raises(smoke.SmokeFailure, match="no log file"):
        smoke.assert_logs_exist(tmp_path)

    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "dcent_voice.log").write_text("started\n", encoding="utf-8")
    smoke.assert_logs_exist(tmp_path)


def test_missing_executable_exits_two(smoke, tmp_path) -> None:
    assert smoke.main(["--executable", str(tmp_path / "nope.exe")]) == 2


def test_fresh_profile_script_never_passes_config_to_the_app() -> None:
    """--config bypasses seeding; using it is what hid the bug from the old smoke."""
    source = FRESH_SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert '"--config"' not in code
    assert "'--config'" not in code


def test_fresh_profile_script_help_runs() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(FRESH_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    assert "--executable" in completed.stdout
    assert "--profile-root" in completed.stdout


def test_fresh_profile_launch_never_redirects_the_apps_streams() -> None:
    """A piped stdout gives a windowed build a sys.stdout a double-click never has.

    That is what hid the uvicorn ColourizedFormatter crash (`'NoneType' object has
    no attribute 'isatty'`) from this smoke: with pipes the service thread starts
    fine, and on a real double-click it dies and the loopback API never binds.
    Phase 2 must therefore launch with no redirection at all.
    """
    source = FRESH_SCRIPT.read_text(encoding="utf-8")
    start = source.index("def phase_launch(")
    launch = source[start : source.index("def assert_registry_entry_published(")]

    assert "subprocess.PIPE" not in launch
    assert "subprocess.DEVNULL" not in launch
    assert "stdin=None" in launch
    assert "stdout=None" in launch
    assert "stderr=None" in launch
    assert "creationflags=_DETACHED_CREATION_FLAGS" in launch


def test_detached_creation_flags_deny_the_child_a_console(smoke) -> None:
    if os.name != "nt":
        assert smoke._DETACHED_CREATION_FLAGS == 0
        return
    assert smoke._DETACHED_CREATION_FLAGS & subprocess.DETACHED_PROCESS
    # Own process group: a Ctrl+C in this terminal must not reach the app.
    assert smoke._DETACHED_CREATION_FLAGS & subprocess.CREATE_NEW_PROCESS_GROUP


def test_phase_one_still_captures_output_because_it_is_a_cli_use(smoke) -> None:
    """--print-config is a console invocation; its stdout is the assertion."""
    source = FRESH_SCRIPT.read_text(encoding="utf-8")
    seed = source[source.index("def phase_seed(") : source.index("def expected_config_path(")]

    assert "capture_output=True" in seed


def test_registry_entry_assertion_requires_endpoint_token_and_containment(smoke, tmp_path) -> None:
    registry_dir = tmp_path / "state" / "DCENT" / "modules"
    registry_dir.mkdir(parents=True)
    entry = registry_dir / "dcent-voice.json"
    token = registry_dir / "dcent-voice.token"
    token.write_text("secret", encoding="utf-8")

    def write(**overrides) -> None:
        payload = {
            "moduleId": "dcent-voice",
            "endpoint": "http://127.0.0.1:8765",
            "tokenRef": str(token),
        }
        payload.update(overrides)
        entry.write_text(json.dumps(payload), encoding="utf-8")

    write()
    smoke.assert_registry_entry_published(registry_dir, port=8765, timeout_s=1.0)

    with pytest.raises(smoke.SmokeFailure, match="not the requested port"):
        smoke.assert_registry_entry_published(registry_dir, port=9999, timeout_s=1.0)

    write(tokenRef=str(registry_dir / "gone.token"))
    with pytest.raises(smoke.SmokeFailure, match="missing token file"):
        smoke.assert_registry_entry_published(registry_dir, port=8765, timeout_s=1.0)

    escaped = tmp_path / "elsewhere.token"
    escaped.write_text("secret", encoding="utf-8")
    write(tokenRef=str(escaped))
    with pytest.raises(smoke.SmokeFailure, match="escaped the profile root"):
        smoke.assert_registry_entry_published(registry_dir, port=8765, timeout_s=1.0)


def test_registry_entry_assertion_fails_when_the_service_never_bound(smoke, tmp_path) -> None:
    """The exact symptom of the uvicorn crash: token may exist, entry never does."""
    registry_dir = tmp_path / "modules"
    registry_dir.mkdir(parents=True)

    with pytest.raises(smoke.SmokeFailure, match="never published an ADE registry entry"):
        smoke.assert_registry_entry_published(registry_dir, port=8765, timeout_s=0.5)


def test_diagnostics_come_from_the_profile_logs_not_pipes(smoke, tmp_path, capsys) -> None:
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "dcent_voice.log").write_text(
        "uncaught thread exception in DCENTService", encoding="utf-8"
    )
    (logs / "startup.log").write_text("frozen=True bundle_root=X", encoding="utf-8")
    (tmp_path / "config" / "logs" / "last-startup-failure.json").write_text(
        '{"reason": "service"}', encoding="utf-8"
    )

    smoke.print_profile_diagnostics(tmp_path)

    err = capsys.readouterr().err
    assert "uncaught thread exception in DCENTService" in err
    assert "frozen=True bundle_root=X" in err
    assert '"reason": "service"' in err


def test_diagnostics_say_so_when_the_app_died_before_logging(smoke, tmp_path, capsys) -> None:
    smoke.print_profile_diagnostics(tmp_path)

    assert "died before it could write one" in capsys.readouterr().err


def test_stdio_assertion_requires_the_app_to_report_a_none_stdout(smoke, tmp_path) -> None:
    """DETACHED_PROCESS is a request, not a guarantee; the app's own record is the proof."""
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    startup = logs / "startup.log"

    startup.write_text(
        "boot argv=['--no-hotkeys', '--no-overlay'] stdout=none stderr=none", encoding="utf-8"
    )
    smoke.assert_stdio_was_detached(tmp_path, timeout_s=1.0)


def test_stdio_assertion_fails_when_the_child_inherited_a_console(smoke, tmp_path) -> None:
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "startup.log").write_text(
        "boot argv=['--no-hotkeys', '--no-overlay'] stdout=handle stderr=handle",
        encoding="utf-8",
    )

    with pytest.raises(smoke.SmokeFailure, match="usable stdout"):
        smoke.assert_stdio_was_detached(tmp_path, timeout_s=1.0)


def test_stdio_assertion_fails_when_bootlog_records_nothing(smoke, tmp_path) -> None:
    """A missing field must fail loudly, never pass by default."""
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "startup.log").write_text(
        "boot argv=['--no-hotkeys', '--no-overlay'] offline=True", encoding="utf-8"
    )

    with pytest.raises(smoke.SmokeFailure, match="cannot prove"):
        smoke.assert_stdio_was_detached(tmp_path, timeout_s=0.5)


def test_launch_phase_checks_stdio_before_anything_else() -> None:
    """An inherited stdout invalidates every later assertion, so it is checked first."""
    source = FRESH_SCRIPT.read_text(encoding="utf-8")
    start = source.index("def phase_launch(")
    launch = source[start : source.index("def assert_stdio_was_detached(")]

    assert launch.index("assert_stdio_was_detached(") < launch.index("wait_for_token(")


def test_stdio_assertion_reads_the_launch_boot_line_not_phase_ones(smoke, tmp_path) -> None:
    """startup.log holds both runs; phase 1 is piped and honestly says stdout=handle."""
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "startup.log").write_text(
        """INFO boot argv=['--print-config'] stdout=handle stderr=handle
INFO boot argv=['--no-hotkeys', '--no-overlay'] stdout=none stderr=none
""",
        encoding="utf-8",
    )

    smoke.assert_stdio_was_detached(tmp_path, timeout_s=1.0)


def test_stdio_assertion_still_fails_when_only_the_launch_line_is_wrong(smoke, tmp_path) -> None:
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    (logs / "startup.log").write_text(
        """INFO boot argv=['--print-config'] stdout=handle stderr=handle
INFO boot argv=['--no-hotkeys', '--no-overlay'] stdout=handle stderr=handle
""",
        encoding="utf-8",
    )

    with pytest.raises(smoke.SmokeFailure, match="usable stdout"):
        smoke.assert_stdio_was_detached(tmp_path, timeout_s=1.0)


def test_stdio_assertion_distinguishes_a_missing_field_from_a_missing_boot_line(
    smoke, tmp_path
) -> None:
    logs = tmp_path / "config" / "logs"
    logs.mkdir(parents=True)
    startup = logs / "startup.log"

    startup.write_text("nothing was logged", encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="no boot line was written at all"):
        smoke.assert_stdio_was_detached(tmp_path, timeout_s=0.3)

    startup.write_text(
        "12:00:01 INFO [DCENT_Voice] boot argv=['x', '--no-hotkeys', '--no-overlay'] offline=True",
        encoding="utf-8",
    )
    with pytest.raises(smoke.SmokeFailure, match="carries no 'stdout=' field"):
        smoke.assert_stdio_was_detached(tmp_path, timeout_s=0.3)


def test_launch_argv_is_defined_once_and_shared(smoke) -> None:
    """The assertion matches on the argv the launcher actually used."""
    assert smoke._LAUNCH_ARGS == ("--no-hotkeys", "--no-overlay")
