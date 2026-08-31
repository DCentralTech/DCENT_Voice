# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import dcent_voice.app as app
import dcent_voice.asr.model_registry as registry


def _forbid_desktop(monkeypatch) -> list[object]:
    calls: list[object] = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("payload commands must not initialize the desktop")

    monkeypatch.setattr(app, "run_app", forbidden)
    return calls


def _forbid_config(monkeypatch) -> list[object]:
    calls: list[object] = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("payload commands must not read or create app configuration")

    monkeypatch.setattr(app, "load_config", forbidden)
    return calls


def _isolate_profile_environment(monkeypatch, tmp_path: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for variable in (
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        root = tmp_path / "profile-state" / variable.lower()
        monkeypatch.setenv(variable, str(root))
        roots.append(root)
    return tuple(roots)


def test_main_dispatches_verify_payload_without_desktop(monkeypatch, tmp_path, capsys) -> None:
    desktop_calls = _forbid_desktop(monkeypatch)
    config_calls = _forbid_config(monkeypatch)
    verified: list[Path] = []
    monkeypatch.setattr(registry, "verify_shipped_payload", verified.append)

    missing_config = tmp_path / "does-not-exist.toml"
    code = app.main(["--config", str(missing_config), "verify-payload", str(tmp_path)])

    assert code == 0
    assert verified == [tmp_path.resolve()]
    assert desktop_calls == []
    assert config_calls == []
    assert not missing_config.exists()
    assert f"verified payload: {tmp_path.resolve()}" in capsys.readouterr().out


def test_main_dispatches_stage_payload_exact_roots_without_desktop(
    monkeypatch, tmp_path, capsys
) -> None:
    desktop_calls = _forbid_desktop(monkeypatch)
    config_calls = _forbid_config(monkeypatch)
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    staged: list[tuple[Path, Path]] = []

    def stage(src: Path, dst: Path) -> Path:
        staged.append((src, dst))
        return dst

    monkeypatch.setattr(registry, "stage_verified_payload", stage)
    code = app.main(
        [
            "--config",
            str(tmp_path / "missing.toml"),
            "stage-payload",
            str(source),
            str(destination),
        ]
    )

    assert code == 0
    assert staged == [(source.resolve(), destination.resolve())]
    assert desktop_calls == []
    assert config_calls == []
    assert "staged verified payload:" in capsys.readouterr().out


def test_payload_failure_is_actionable_nonzero_and_never_starts_desktop(
    monkeypatch, tmp_path, capsys
) -> None:
    desktop_calls = _forbid_desktop(monkeypatch)
    config_calls = _forbid_config(monkeypatch)

    def fail(_root: Path) -> None:
        raise registry.ModelUnavailableError("missing pinned model")

    monkeypatch.setattr(registry, "verify_shipped_payload", fail)
    code = app.main(["--config", str(tmp_path / "invalid.toml"), "verify-payload", str(tmp_path)])

    assert code == 1
    assert desktop_calls == []
    assert config_calls == []
    error = capsys.readouterr().err
    assert "verify-payload failed" in error
    assert "missing pinned model" in error


def test_frozen_dispatch_contract_does_not_enter_desktop(monkeypatch, tmp_path) -> None:
    desktop_calls = _forbid_desktop(monkeypatch)
    config_calls = _forbid_config(monkeypatch)
    profile_roots = _isolate_profile_environment(monkeypatch, tmp_path)
    verified: list[Path] = []
    monkeypatch.setattr(registry, "verify_shipped_payload", verified.append)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "dcent-voice.exe"))

    def forbidden_runtime_environment_restore() -> None:
        raise AssertionError("payload commands must not enter runtime environment setup")

    monkeypatch.setattr(
        app, "_restore_frozen_linux_library_path", forbidden_runtime_environment_restore
    )

    assert app.main(["verify-payload", str(tmp_path)]) == 0
    assert verified == [tmp_path.resolve()]
    assert desktop_calls == []
    assert config_calls == []
    assert all(not root.exists() for root in profile_roots)
    assert not (tmp_path / "profile-state").exists()


@pytest.mark.parametrize("config_kind", ["missing", "invalid", "forbidden"])
def test_payload_dispatch_ignores_unusable_config_without_profile_writes(
    monkeypatch, tmp_path, config_kind
) -> None:
    profile_roots = _isolate_profile_environment(monkeypatch, tmp_path)
    desktop_calls = _forbid_desktop(monkeypatch)
    config_calls = _forbid_config(monkeypatch)
    config_path = tmp_path / f"{config_kind}.toml"
    if config_kind == "invalid":
        config_path.write_text("this is = [invalid TOML", encoding="utf-8")

    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if config_kind == "forbidden" and path == config_path:
            raise PermissionError("config is deliberately unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    verified: list[Path] = []
    monkeypatch.setattr(registry, "verify_shipped_payload", verified.append)

    assert app.main(["--config", str(config_path), "verify-payload", str(tmp_path)]) == 0
    assert verified == [tmp_path.resolve()]
    assert config_calls == []
    assert desktop_calls == []
    assert all(not root.exists() for root in profile_roots)
    assert not (tmp_path / "profile-state").exists()


@pytest.mark.parametrize("config_kind", ["default", "missing", "invalid"])
def test_source_cli_invalid_payload_is_profile_inert(tmp_path, config_kind) -> None:
    config_path = tmp_path / f"{config_kind}.toml"
    if config_kind == "invalid":
        config_path.write_text("not valid = [toml", encoding="utf-8")
    profile_roots = tuple(
        tmp_path / "subprocess-profile" / name
        for name in (
            "home",
            "appdata",
            "localappdata",
            "xdg-config",
            "xdg-data",
            "xdg-cache",
            "xdg-runtime",
        )
    )
    environment = os.environ.copy()
    for variable, root in zip(
        (
            "HOME",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        ),
        profile_roots,
        strict=True,
    ):
        environment[variable] = str(root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    command = [sys.executable, "-m", "dcent_voice"]
    if config_kind != "default":
        command.extend(["--config", str(config_path)])
    command.extend(["verify-payload", str(tmp_path / "invalid-payload")])
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert "verify-payload failed" in result.stderr
    assert "Shipped payload path is unsafe" in result.stderr
    assert all(not root.exists() for root in profile_roots)
    assert not (tmp_path / "subprocess-profile").exists()


def test_fresh_frozen_cli_invalid_payload_exits_without_desktop_timeout(tmp_path) -> None:
    """Smoke the windowed entrypoint when a payload rebuilt from this source exists."""
    if sys.platform != "win32":
        pytest.skip("Windows frozen EXE")
    exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    source = Path(app.__file__).resolve()
    if not exe.is_file() or exe.stat().st_mtime < source.stat().st_mtime:
        pytest.skip("no frozen executable rebuilt from the current app dispatch")

    result = subprocess.run(
        [str(exe), "verify-payload", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
