# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""WS2 regression guards: nothing fails silently, and offline is enforced.

Every test here injects one of the failure classes that used to end a windowed
launch with exit code 2, no log line, no dialog and no window: the process
appeared for a moment and vanished. Each asserts the three things that must now
always happen together — a log line, a ``report_fatal`` surface naming the log
path, and a non-zero exit code — or, where the failure is recoverable, that the
app resets and keeps running while saying so.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import textwrap
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

import pytest

from dcent_voice import app
from dcent_voice import config as config_module
from dcent_voice.config import ConfigError, load_config
from dcent_voice.util import bootlog, fatal

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
EXAMPLE = REPO_ROOT / "config.example.toml"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """An isolated, empty user profile plus a suppressed dialog surface."""
    root = tmp_path / "profile"
    monkeypatch.setenv("DCENT_VOICE_PROFILE_ROOT", str(root))
    monkeypatch.setenv(fatal.NO_DIALOGS_ENV, "1")
    monkeypatch.setenv("DCENT_VOICE_DISABLE_AUTOSTART", "1")
    # bootlog memoises the resolved boot log path for the process; point it at
    # the fresh profile so the assertions below read this test's file.
    bootlog.reset_boot_log_path()
    fatal.reset_fatal_state()
    config_module.recovery_notice = None
    yield root
    bootlog.reset_boot_log_path()
    fatal.reset_fatal_state()
    config_module.recovery_notice = None


@pytest.fixture
def fatal_calls(monkeypatch):
    """Record every report_fatal call while keeping its real return contract."""
    calls: list[dict] = []
    real = fatal.report_fatal

    def spy(title, message, *, log_path=None, exit_code=1, exc=None):
        calls.append(
            {
                "title": title,
                "message": message,
                "log_path": log_path,
                "exit_code": exit_code,
                "exc": exc,
            }
        )
        return real(title, message, log_path=log_path, exit_code=exit_code, exc=exc)

    monkeypatch.setattr(fatal, "report_fatal", spy)
    monkeypatch.setattr(app, "report_fatal", spy)
    return calls


def _seed_config(root: Path, text: str) -> Path:
    target = root / "config" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet in a clean child interpreter.

    The offline keys are stripped from the inherited environment on purpose:
    ``scripts/download_models.py`` and ``scripts/qa/warm_model_cache.py`` set
    ``DCENT_VOICE_ALLOW_HUB`` at module scope, and any test that imports one of
    them leaks it into this process — which would silently turn these
    assertions into no-ops.
    """
    env = os.environ.copy()
    for key in ("DCENT_VOICE_ALLOW_HUB", *bootlog.OFFLINE_ENV):
        env.pop(key, None)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
        env=env,
    )


# --------------------------------------------------------------------------
# Offline enforcement (AC9)
# --------------------------------------------------------------------------


def test_offline_env_is_set_before_huggingface_hub_is_imported() -> None:
    """huggingface_hub reads HF_HUB_OFFLINE once, at import. Ordering is the guarantee."""
    result = _run_python(
        f"""
        import sys
        sys.path.insert(0, {str(SRC)!r})
        import os
        assert "huggingface_hub" not in sys.modules
        import dcent_voice.app  # noqa: F401
        assert "huggingface_hub" not in sys.modules, "app must not import the hub at all"
        for key in ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
                    "HF_HUB_DISABLE_IMPLICIT_TOKEN", "TRANSFORMERS_OFFLINE", "DO_NOT_TRACK"):
            assert os.environ.get(key) == "1", key
        import huggingface_hub.constants as constants
        assert constants.HF_HUB_OFFLINE is True
        print("OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_offline_env_is_overridden_even_when_the_parent_shell_disabled_it() -> None:
    """A stale HF_HUB_OFFLINE=0 in the environment must not re-enable downloads."""
    result = _run_python(
        f"""
        import os, sys
        os.environ["HF_HUB_OFFLINE"] = "0"
        sys.path.insert(0, {str(SRC)!r})
        from dcent_voice.util import bootlog  # noqa: F401
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        print("OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_allow_hub_opt_out_is_honoured() -> None:
    """The two sanctioned downloader scripts must still be able to reach the hub."""
    result = _run_python(
        f"""
        import os, sys
        os.environ["DCENT_VOICE_ALLOW_HUB"] = "1"
        os.environ.pop("HF_HUB_OFFLINE", None)
        sys.path.insert(0, {str(SRC)!r})
        from dcent_voice.util import bootlog
        assert bootlog.allow_hub() is True
        assert os.environ.get("HF_HUB_OFFLINE") is None
        print("OK")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_download_models_script_opts_into_hub_access() -> None:
    source = (REPO_ROOT / "scripts" / "download_models.py").read_text(encoding="utf-8")
    assert 'os.environ.setdefault("DCENT_VOICE_ALLOW_HUB", "1")' in source
    assert source.index("DCENT_VOICE_ALLOW_HUB") < source.index("from dcent_voice")


# --------------------------------------------------------------------------
# Import graph (WS2.4)
# --------------------------------------------------------------------------


def test_importing_app_does_not_load_native_runtimes() -> None:
    """--version / --print-config / doctor must survive a broken native library.

    They can only do that if importing ``dcent_voice.app`` never touches one.
    """
    forbidden = ("ctranslate2", "onnxruntime", "uvicorn", "webview", "pystray", "sounddevice")
    result = _run_python(
        f"""
        import sys
        sys.path.insert(0, {str(SRC)!r})
        import dcent_voice.app  # noqa: F401
        leaked = [name for name in {forbidden!r} if name in sys.modules]
        print("LEAKED:" + ",".join(leaked))
        """
    )
    assert result.returncode == 0, result.stderr
    assert "LEAKED:" in result.stdout
    leaked = result.stdout.split("LEAKED:")[1].strip()
    assert leaked == "", f"dcent_voice.app eagerly imported: {leaked}"


def test_version_survives_a_poisoned_heavy_import() -> None:
    """Poison onnxruntime/sounddevice at import time; --version must still work."""
    result = _run_python(
        f"""
        import sys
        sys.path.insert(0, {str(SRC)!r})

        class Finder:
            POISONED = {{"onnxruntime", "ctranslate2", "sounddevice", "pystray", "webview"}}

            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in self.POISONED:
                    raise ImportError("simulated broken native DLL: " + name)
                return None

        sys.meta_path.insert(0, Finder())
        from dcent_voice.app import main
        raise SystemExit(main(["--version"]))
        """
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_lazy_names_stay_monkeypatchable_as_module_attributes() -> None:
    """Tests patch ``app.<Name>``; the lazy table must not break that contract."""
    for name in app._LAZY_IMPORTS:
        assert hasattr(app, name), name
    assert "PipelineWorker" in dir(app)
    with pytest.raises(AttributeError):
        app.definitely_not_a_real_attribute  # noqa: B018


# --------------------------------------------------------------------------
# report_fatal (AC2)
# --------------------------------------------------------------------------


def test_report_fatal_logs_records_and_returns_exit_code(profile, caplog, capsys) -> None:
    bootlog.install()
    log_path = bootlog.boot_log_path()
    with caplog.at_level(logging.CRITICAL, logger="DCENT_Voice"):
        code = fatal.report_fatal(
            "Broken thing",
            "the detail",
            log_path=log_path,
            exit_code=7,
            exc=RuntimeError("boom"),
        )

    assert code == 7
    assert any("Broken thing" in record.getMessage() for record in caplog.records)

    record_path = fatal.failure_record_path(log_path)
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert payload["title"] == "Broken thing"
    assert payload["message"] == "the detail"
    assert payload["log_path"] == str(log_path)
    assert payload["exc_type"] == "RuntimeError"
    assert payload["argv"] and payload["version"]

    # The log path is the one thing a remote user has to be told.
    assert str(log_path) in capsys.readouterr().err


def test_report_fatal_never_raises_when_everything_is_broken(monkeypatch, capsys) -> None:
    monkeypatch.setattr(fatal, "_write_failure_record", _boom)
    monkeypatch.setattr(fatal, "_show_dialog", _boom)
    monkeypatch.delenv(fatal.NO_DIALOGS_ENV, raising=False)
    assert fatal.report_fatal("t", "m", log_path=None, exit_code=3) == 3


def _boom(*_args, **_kwargs):
    raise OSError("disk gone")


def test_dialog_is_suppressed_by_env(monkeypatch, profile) -> None:
    shown: list[tuple] = []
    monkeypatch.setattr(fatal, "_show_dialog", lambda *a: shown.append(a))
    fatal.report_fatal("t", "m")
    assert shown == []
    monkeypatch.delenv(fatal.NO_DIALOGS_ENV)
    fatal.report_fatal("t", "m")
    assert len(shown) == 1


# --------------------------------------------------------------------------
# Bootstrap logging (WS2.1)
# --------------------------------------------------------------------------


def test_bootstrap_handler_survives_configure_logging(profile) -> None:
    from dcent_voice.util.logging import configure_logging

    bootlog.install()
    logger = bootlog.logger()
    assert any(
        getattr(handler, bootlog.BOOTSTRAP_HANDLER_ATTR, False) for handler in logger.handlers
    )

    configure_logging()
    bootstrap = [
        handler
        for handler in logger.handlers
        if getattr(handler, bootlog.BOOTSTRAP_HANDLER_ATTR, False)
    ]
    assert len(bootstrap) == 1, "configure_logging() must keep exactly one bootstrap handler"
    # Demoted so the two files stop mirroring each other, but still armed for failures.
    assert bootstrap[0].level == logging.WARNING


def test_boot_log_falls_back_when_the_profile_root_is_unwritable(tmp_path, monkeypatch) -> None:
    """A read-only profile must not cost us the only record of why we died."""
    bootlog.reset_boot_log_path()

    def unwritable() -> Path:
        raise PermissionError("profile is read-only")

    monkeypatch.setattr("dcent_voice.util.paths.user_config_dir", unwritable)
    path = bootlog.boot_log_path()
    assert path is not None
    assert path.name == bootlog.BOOT_LOG_FILENAME
    assert "DCENT_Voice" in str(path)
    bootlog.reset_boot_log_path()


# --------------------------------------------------------------------------
# Failure injection through main() (AC2)
# --------------------------------------------------------------------------


def test_missing_example_config_reports_fatal_instead_of_parser_error(
    profile, monkeypatch, fatal_calls, capsys
) -> None:
    """The original root cause: seeding fails, argparse exits 2 into the void."""
    monkeypatch.setattr(
        "dcent_voice.config.find_config_example",
        lambda: profile / "nowhere" / "config.example.toml",
    )
    code = app.main(["--print-config"])

    assert code == 2
    assert len(fatal_calls) == 1
    assert "configuration" in fatal_calls[0]["title"].lower()
    assert str(fatal_calls[0]["log_path"]) in capsys.readouterr().err
    payload = json.loads(fatal.failure_record_path().read_text(encoding="utf-8"))
    assert "config.example.toml" in payload["message"]


def test_corrupt_toml_is_reset_and_the_app_keeps_running(profile, capsys) -> None:
    broken = _seed_config(profile, "this is not = = valid toml [[[")
    config = load_config()

    assert config.active_profile
    assert broken.read_text(encoding="utf-8") != "this is not = = valid toml [[["
    quarantined = sorted(broken.parent.glob("config.toml.broken-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "this is not = = valid toml [[["

    notice = config_module.recovery_notice
    assert notice is not None
    assert notice.broken_path == quarantined[0]
    assert "has been reset" in notice.message()

    app._print_config_recovery_notice()
    assert str(quarantined[0]) in capsys.readouterr().err


def test_unknown_active_profile_is_reset(profile) -> None:
    example = EXAMPLE.read_text(encoding="utf-8")
    assert 'active_profile = "desktop"' in example
    _seed_config(profile, example.replace('active_profile = "desktop"', 'active_profile = "nope"'))
    config = load_config()

    assert config.active_profile == "desktop"
    assert config_module.recovery_notice is not None
    assert "nope" in config_module.recovery_notice.reason
    assert sorted((profile / "config").glob("config.toml.broken-*"))


def test_explicit_config_path_is_never_quarantined(profile, tmp_path) -> None:
    """--config points at a file the caller owns. Resetting it would be data loss."""
    explicit = tmp_path / "mine.toml"
    explicit.write_text("not [[ valid", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(explicit)

    assert explicit.read_text(encoding="utf-8") == "not [[ valid"
    assert not sorted(tmp_path.glob("mine.toml.broken-*"))
    assert config_module.recovery_notice is None


def test_recovery_reports_fatal_when_reseeding_also_fails(profile, monkeypatch) -> None:
    _seed_config(profile, "not [[ valid")
    monkeypatch.setattr(
        "dcent_voice.config.find_config_example",
        lambda: profile / "nowhere" / "config.example.toml",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    # Both halves of the story: what was wrong, and where the old file went.
    assert "broken-" in str(excinfo.value)
    assert "could not be created" in str(excinfo.value)


def test_consent_required_reports_fatal(profile, monkeypatch, fatal_calls) -> None:
    from dcent_voice.privacy import ConsentRequired

    _seed_config(profile, EXAMPLE.read_text(encoding="utf-8"))

    def raise_consent(*_args, **_kwargs):
        raise ConsentRequired(("asr:openai",))

    monkeypatch.setattr(app, "run_app", raise_consent)
    code = app.main([])

    assert code == 2
    assert len(fatal_calls) == 1
    assert "consent" in fatal_calls[0]["title"].lower()
    assert "asr:openai" in fatal_calls[0]["message"]


def test_lock_unavailable_reports_fatal_and_is_not_already_running(
    profile, monkeypatch, fatal_calls
) -> None:
    """CreateMutexW failing used to masquerade as "already running in the tray"."""
    from dcent_voice.attach.single_instance import AlreadyRunningError, LockUnavailableError

    _seed_config(profile, EXAMPLE.read_text(encoding="utf-8"))

    def raise_lock(*_args, **_kwargs):
        raise LockUnavailableError("CreateMutexW failed, winerror=5", winerror=5)

    monkeypatch.setattr(app, "run_app", raise_lock)
    seen: list = []
    monkeypatch.setattr(app, "handle_already_running", lambda *a, **k: seen.append(a) or 0)

    code = app.main([])

    assert code == 1
    assert seen == [], "a lock the OS refused is not another running instance"
    assert len(fatal_calls) == 1
    assert "instance lock" in fatal_calls[0]["title"]
    assert not issubclass(LockUnavailableError, AlreadyRunningError)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex only")
def test_mutex_creation_failure_propagates_as_lock_unavailable(tmp_path, monkeypatch) -> None:
    import ctypes

    from dcent_voice.attach.single_instance import LockUnavailableError, SingleInstanceLock

    lock = SingleInstanceLock(
        path=tmp_path / "dcent-voice.lock",
        pid=1234,
        mutex_name="Local\\DCENT_Voice_Test_WS2",
    )
    real_windll = ctypes.WinDLL

    def patched(name, *args, **kwargs):
        dll = real_windll(name, *args, **kwargs)
        if str(name).lower().startswith("kernel32"):

            class _Null:
                argtypes = None
                restype = None

                def __call__(self, *_a, **_k):
                    ctypes.set_last_error(5)
                    return 0

            dll.CreateMutexW = _Null()
        return dll

    monkeypatch.setattr(ctypes, "WinDLL", patched)
    with pytest.raises(LockUnavailableError) as excinfo:
        lock.acquire()
    assert excinfo.value.winerror == 5
    assert "not another running copy" in str(excinfo.value)


def test_packaged_entry_point_reports_a_nonzero_systemexit(profile, monkeypatch) -> None:
    """The windowed build: SystemExit(2) with no stdout is the silent vanish."""
    from dcent_voice import _packaged

    monkeypatch.setattr(_packaged, "is_windowed", lambda: True)
    monkeypatch.setattr(app, "main", lambda *_a, **_k: (_ for _ in ()).throw(SystemExit(2)))
    fatal.reset_fatal_state()
    code = _packaged.run()

    assert code == 2
    assert fatal.fatal_reported()
    payload = json.loads(fatal.failure_record_path().read_text(encoding="utf-8"))
    assert payload["title"] == "DCENT_Voice stopped during startup"


def test_console_usage_errors_do_not_get_a_second_dialog(profile, monkeypatch, caplog) -> None:
    """`dcent-voice --nonsense` in a terminal: argparse already said it on stderr."""
    from dcent_voice import _packaged

    monkeypatch.setattr(_packaged, "is_windowed", lambda: False)
    monkeypatch.setattr(app, "main", lambda *_a, **_k: (_ for _ in ()).throw(SystemExit(2)))
    fatal.reset_fatal_state()
    with caplog.at_level(logging.ERROR, logger="DCENT_Voice"):
        code = _packaged.run()

    assert code == 2
    assert not fatal.fatal_reported()
    assert any("exited with code 2" in record.getMessage() for record in caplog.records)


def test_packaged_entry_point_reports_an_unhandled_exception(profile, monkeypatch) -> None:
    from dcent_voice import _packaged

    def explode(*_a, **_k):
        raise OSError("a native DLL is missing")

    monkeypatch.setattr(app, "main", explode)
    fatal.reset_fatal_state()
    code = _packaged.run()

    assert code == 1
    payload = json.loads(fatal.failure_record_path().read_text(encoding="utf-8"))
    assert payload["exc_type"] == "OSError"
    assert "a native DLL is missing" in payload["message"]


def test_packaged_entry_point_does_not_double_report(profile, monkeypatch) -> None:
    """main() already showed a dialog; the outer handler must not show a second."""
    from dcent_voice import _packaged

    def already_reported(*_a, **_k):
        fatal.report_fatal("first", "already surfaced", exit_code=2)
        raise SystemExit(2)

    monkeypatch.setattr(_packaged, "is_windowed", lambda: True)
    monkeypatch.setattr(app, "main", already_reported)
    fatal.reset_fatal_state()
    assert _packaged.run() == 2
    payload = json.loads(fatal.failure_record_path().read_text(encoding="utf-8"))
    assert payload["title"] == "first"


def test_packaged_entry_point_passes_success_through(profile, monkeypatch) -> None:
    from dcent_voice import _packaged

    monkeypatch.setattr(app, "main", lambda *_a, **_k: 0)
    fatal.reset_fatal_state()
    assert _packaged.run() == 0
    assert not fatal.fatal_reported()


# --------------------------------------------------------------------------
# GUI runtime (WS2.6)
# --------------------------------------------------------------------------


def test_missing_webview2_is_a_dialog_not_a_lost_print(monkeypatch, profile, fatal_calls) -> None:
    if sys.platform == "win32":
        monkeypatch.setattr(
            "dcent_voice.ui.webview_runtime.windows_webview2_runtime_present",
            lambda: False,
        )
    code = app._report_missing_gui_runtime("settings dashboard")

    assert code == 1
    assert len(fatal_calls) == 1
    if sys.platform == "win32":
        assert (
            "developer.microsoft.com" in fatal_calls[0]["message"]
            or "http" in (fatal_calls[0]["message"])
        )


# --------------------------------------------------------------------------
# Loopback service under a real windowed launch (WS2.6 fallout)
# --------------------------------------------------------------------------


def test_service_thread_starts_when_stdout_is_none(monkeypatch) -> None:
    """Explorer gives the windowed exe no stdout; uvicorn must not choke on it.

    ``uvicorn``'s default ``ColourizedFormatter`` calls ``sys.stdout.isatty()``
    while ``logging.dictConfig`` builds it. With ``sys.stdout is None`` that
    raised inside the service thread, and a real double-click silently lost the
    entire loopback API — /health, ADE attach, the lot — while the tray still
    came up looking healthy.
    """
    import uvicorn

    from dcent_voice.service.server import ServiceThread

    captured: dict[str, object] = {}
    real_config = uvicorn.Config

    class FakeServer:
        def __init__(self, config) -> None:
            captured["config"] = config
            self.started = False

        def run(self) -> None:
            return

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    thread = ServiceThread(app=object(), host="127.0.0.1", port=1)
    thread._run()

    assert thread.error is None
    assert isinstance(captured["config"], real_config)
    assert captured["config"].log_config is None


def test_configure_logging_survives_a_windowed_launch(profile, monkeypatch) -> None:
    """No usable stderr must mean no console handler, not a silently dead one.

    ``logging.StreamHandler()`` binds ``sys.stderr`` at construction. When that
    is ``None`` — the frozen ``console=False`` build started from Explorer — the
    handler swallows an AttributeError inside ``handleError`` for every record
    it is ever given, which is a handler that cannot write and will not say so.
    """
    import logging as logging_module

    from dcent_voice.util.logging import configure_logging

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    logger = configure_logging()
    try:
        streams = [
            handler for handler in logger.handlers if type(handler) is logging_module.StreamHandler
        ]
        assert streams == []
        # And logging still works: the file handlers are unaffected.
        logger.error("windowed launch smoke")
    finally:
        for handler in list(logger.handlers):
            with contextlib.suppress(Exception):
                handler.flush()

    log_file = profile / "config" / "logs" / "dcent_voice.log"
    assert "windowed launch smoke" in log_file.read_text(encoding="utf-8")


def test_boot_line_records_the_stdio_state(profile, monkeypatch, caplog) -> None:
    """The boot line must say whether the process had a console.

    ``scripts/fresh_profile_smoke.py`` asserts on this field to prove it really
    launched the app the way a double-click does, instead of trusting that
    ``DETACHED_PROCESS`` denied the child a console. Two shipped bugs lived
    behind exactly that difference.
    """
    # Exactly two values, as scripts/fresh_profile_smoke.py parses them.
    assert bootlog.stream_state("stdout") == "handle"

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert bootlog.stream_state("stdout") == "none"
    assert bootlog.stream_state("stderr") == "none"

    # install() is idempotent per process, so drive the record directly and
    # read it off the logger rather than off a file this process already owns.
    with caplog.at_level(logging.INFO, logger="DCENT_Voice"):
        bootlog._log_environment(["--no-hotkeys", "--no-overlay"])
    line = caplog.records[-1].getMessage()
    assert "boot " in line
    assert "stdout=none" in line
    assert "stderr=none" in line
    # The smoke matches this phase's argv on the same line as the stdio fields.
    assert "--no-hotkeys" in line


def test_report_fatal_survives_a_windowed_launch(profile, monkeypatch) -> None:
    """The failure surface itself must not need a stream to work."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    assert fatal.is_windowed() is True
    assert fatal.report_fatal("windowed", "no streams at all", exit_code=4) == 4
    payload = json.loads(fatal.failure_record_path().read_text(encoding="utf-8"))
    assert payload["title"] == "windowed"


# --------------------------------------------------------------------------
# Security review: argv redaction (M3)
# --------------------------------------------------------------------------


def test_compose_transcript_never_reaches_argv_logs() -> None:
    """`compose <text>` is a dictated sentence sitting in argv.

    SECURITY.md promises transcripts are not written to disk, and both the boot
    line and last-startup-failure.json end up in the diagnostics zip a user
    emails us.
    """
    argv = ["dcent-voice.exe", "compose", "--style", "email", "tell", "alice", "the", "password"]
    redacted = bootlog.redact_argv(argv)

    assert redacted[:2] == ["dcent-voice.exe", "compose"]
    # Flag names survive: how the app was invoked is what we need to debug.
    assert "--style" in redacted
    for word in ("tell", "alice", "the", "password"):
        assert word not in redacted
    assert redacted.count(bootlog.REDACTED) == 5  # 4 positionals + the --style value


def test_learn_correction_values_are_redacted() -> None:
    argv = ["dcent-voice", "learn", "--from", "wreck a nice beach", "--to", "recognize speech"]
    redacted = bootlog.redact_argv(argv)

    assert redacted == [
        "dcent-voice",
        "learn",
        "--from",
        bootlog.REDACTED,
        "--to",
        bootlog.REDACTED,
    ]


def test_redaction_handles_values_glued_to_their_flag() -> None:
    """`--from=hello there` hides content inside a flag-shaped token."""
    redacted = bootlog.redact_argv(["dcent-voice", "learn", "--from=wreck a nice beach"])
    assert redacted == ["dcent-voice", "learn", f"--from={bootlog.REDACTED}"]


def test_non_content_subcommands_are_left_intact() -> None:
    """Over-redacting would cost us the diagnostics the log exists for.

    ``transcribe`` only ever receives a WAV path, never text.
    """
    for argv in (
        ["dcent-voice", "--print-config"],
        ["dcent-voice", "transcribe", "hello.wav", "--output-json", "out.json"],
        ["dcent-voice", "doctor", "--json", "report.json"],
    ):
        assert bootlog.redact_argv(argv) == argv


def test_boot_line_and_failure_record_both_redact(profile, caplog) -> None:
    """Both on-disk writers must use the same rule, not just one of them."""
    secret = "seventeen-blue-parakeets"
    argv = ["dcent-voice.exe", "compose", secret]

    with caplog.at_level(logging.INFO, logger="DCENT_Voice"):
        bootlog._log_environment(argv)
    assert secret not in caplog.records[-1].getMessage()

    with mock.patch.object(sys, "argv", argv):
        fatal.report_fatal("boom", "something failed", exit_code=1)
    payload = json.loads(fatal.failure_record_path().read_text(encoding="utf-8"))
    assert secret not in json.dumps(payload)
    assert payload["argv"] == ["dcent-voice.exe", "compose", bootlog.REDACTED]


# --------------------------------------------------------------------------
# Security review: bootstrap log hardening (M5)
# --------------------------------------------------------------------------


def test_startup_log_rotates(profile) -> None:
    """startup.log is appended to on every launch and ships in the zip."""
    bootlog.install()
    handlers = [
        handler
        for handler in bootlog.logger().handlers
        if getattr(handler, bootlog.BOOTSTRAP_HANDLER_ATTR, False)
    ]
    assert handlers, "bootstrap handler is not attached"
    handler = handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == bootlog.BOOT_LOG_MAX_BYTES == 512 * 1024
    assert handler.backupCount == bootlog.BOOT_LOG_BACKUPS == 2


def test_secure_touch_refuses_to_follow_a_symlink(tmp_path) -> None:
    """A planted symlink named startup.log must not redirect our writes."""
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW is POSIX-only")
    target = tmp_path / "someone-elses-file"
    target.write_text("original", encoding="utf-8")
    link = tmp_path / "startup.log"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(OSError):
        bootlog._secure_touch(link)
    assert target.read_text(encoding="utf-8") == "original"


def test_shared_tmp_dir_owned_by_another_user_is_refused(tmp_path, monkeypatch) -> None:
    """In a world-writable /tmp another local user can pre-create our dir."""
    directory = tmp_path / "DCENT_Voice"
    directory.mkdir()
    monkeypatch.setattr(bootlog.os, "getuid", lambda: 999999, raising=False)

    with pytest.raises(PermissionError):
        bootlog._prepare_log_dir(directory, shared=True)


def test_tmp_fallback_is_only_treated_as_shared_on_posix() -> None:
    """Windows %TEMP% lives under %LOCALAPPDATA% and is already per-user."""
    candidates = bootlog._boot_log_candidates()
    tmp_candidate, shared = candidates[-1]
    assert tmp_candidate.name == bootlog.BOOT_LOG_FILENAME
    assert tmp_candidate.parent.name == bootlog.APP_DIR_NAME
    assert shared is (os.name == "posix")


# --------------------------------------------------------------------------
# Security review: config recovery must not destroy good settings (M4)
# --------------------------------------------------------------------------


def test_transient_read_error_never_quarantines_the_config(profile, monkeypatch) -> None:
    """A locked file says nothing about its contents.

    An antivirus scan, an open editor or a roaming-profile sync can make a
    perfectly valid config unreadable for a moment. Resetting on that would
    destroy working settings over an error that fixes itself.
    """
    from dcent_voice.config import ConfigUnreadableError

    original = EXAMPLE.read_text(encoding="utf-8")
    config_path = _seed_config(profile, original)

    def locked(*_args, **_kwargs):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(Path, "read_text", locked)

    with pytest.raises(ConfigUnreadableError) as excinfo:
        load_config()

    assert "left unchanged" in str(excinfo.value)
    monkeypatch.undo()
    assert config_path.read_text(encoding="utf-8") == original
    assert not sorted(config_path.parent.glob("config.toml.broken-*"))
    assert config_module.recovery_notice is None


def test_unreadable_config_is_a_fatal_not_a_silent_reset(profile, monkeypatch, fatal_calls):
    """main() must still surface it — fail loud, just without touching the file."""

    def locked(*_args, **_kwargs):
        raise PermissionError(13, "The process cannot access the file")

    _seed_config(profile, EXAMPLE.read_text(encoding="utf-8"))
    monkeypatch.setattr(Path, "read_text", locked)

    code = app.main(["--print-config"])

    assert code == 2
    assert len(fatal_calls) == 1
    assert "left unchanged" in fatal_calls[0]["message"]


def test_quarantine_names_cannot_collide(profile) -> None:
    """Two launches in the same second must not overwrite each other's evidence."""
    from dcent_voice.config import _quarantine_path

    config_path = profile / "config" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("broken", encoding="utf-8")

    first = _quarantine_path(config_path)
    second = _quarantine_path(config_path)

    assert first != second
    # Both names are reserved on disk, so a later os.replace cannot clobber.
    assert first.exists() and second.exists()


def test_broken_configs_are_pruned_to_five(profile) -> None:
    """A crash-looping launch must not fill the user's profile with copies."""
    from dcent_voice.config import MAX_BROKEN_CONFIG_COPIES

    config_dir = profile / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for index in range(9):
        (config_dir / f"config.toml.broken-2026010{index}-000000").write_text(
            f"old {index}", encoding="utf-8"
        )
    _seed_config(profile, "this is not = = valid toml [[[")

    load_config()

    remaining = sorted(config_dir.glob("config.toml.broken-*"))
    assert len(remaining) == MAX_BROKEN_CONFIG_COPIES == 5
    # The newest survive: the just-quarantined file must still be there.
    assert config_module.recovery_notice.broken_path in remaining
    assert not (config_dir / "config.toml.broken-20260100-000000").exists()


# --------------------------------------------------------------------------
# Security review: LockUnavailableError on the stale-lock retry (MAJOR-5)
# --------------------------------------------------------------------------


def test_lock_failure_during_stale_lock_retry_keeps_the_tailored_message(
    profile, monkeypatch, fatal_calls
) -> None:
    """The retry runs *inside* `except AlreadyRunningError`.

    Python does not consult sibling except clauses of the same try, so a
    LockUnavailableError raised by the retry used to escape main() entirely and
    reach _packaged.run's generic "unexpected error" dialog — telling a user
    with a broken Windows session to look for a tray icon that was never there.
    """
    from dcent_voice.attach.single_instance import AlreadyRunningError, LockUnavailableError

    _seed_config(profile, EXAMPLE.read_text(encoding="utf-8"))
    calls: list[int] = []

    def run_app(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise AlreadyRunningError("another instance holds the lock")
        raise LockUnavailableError("CreateMutexW failed, winerror=5", winerror=5)

    monkeypatch.setattr(app, "run_app", run_app)
    monkeypatch.setattr(app, "force_clear_stale_lock", lambda: True)

    code = app.main([])

    assert calls == [1, 1], "the stale-lock retry must actually have run"
    assert code == 1
    assert len(fatal_calls) == 1
    assert fatal_calls[0]["title"] == "DCENT_Voice could not create its instance lock"
    assert "winerror=5" in fatal_calls[0]["message"]


def test_probe_boot_log_path_returns_a_path_in_both_branches() -> None:
    """The un-memoised branch must honour the ``-> Path | None`` annotation.

    ``_boot_log_candidates()`` returns ``(path, shared_parent)`` pairs so
    ``_prepare_log_dir`` knows whether it is creating a directory in a
    world-writable ``/tmp``. When that flag was added, this helper kept
    returning ``candidates[0]`` and started handing every caller a tuple — the
    annotation stayed right while the body drifted away from it, which took out
    doctor's log-history check with ``'tuple' object has no attribute 'exists'``.
    Both branches are asserted because only the cold one regressed.
    """
    bootlog.reset_boot_log_path()
    try:
        probed = bootlog.probe_boot_log_path()  # cold: no memo yet
        assert isinstance(probed, Path), f"expected a Path, got {type(probed).__name__}"
        assert probed.name == bootlog.BOOT_LOG_FILENAME

        resolved = bootlog.boot_log_path()  # warm: memo populated
        assert isinstance(resolved, Path)
        assert bootlog.probe_boot_log_path() == resolved
    finally:
        bootlog.reset_boot_log_path()
