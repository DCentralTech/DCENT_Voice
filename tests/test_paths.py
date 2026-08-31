# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Frozen/source resource resolution and profile-root isolation (WS6)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from dcent_voice.util import paths

REPO_ROOT = Path(__file__).resolve().parents[1]


def _freeze(
    monkeypatch, *, executable: Path, meipass: Path | None, platform: str = "win32"
) -> None:
    """Simulate a PyInstaller frozen process without building one."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "platform", platform)
    if meipass is None:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    else:
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)


def _one_dir(root: Path, exe_name: str) -> tuple[Path, Path]:
    internal = root / "_internal"
    internal.mkdir(parents=True)
    (internal / "config.example.toml").write_text("# example\n", encoding="utf-8")
    exe = root / exe_name
    exe.write_bytes(b"MZ")
    return exe, internal


# --- source checkout ---------------------------------------------------------


def test_source_layout_resolves_repository_root() -> None:
    assert not paths.is_frozen()
    assert paths.source_root() == REPO_ROOT
    assert paths.bundle_root() == REPO_ROOT
    assert paths.app_dir() == REPO_ROOT
    assert paths.resource("config.example.toml") == REPO_ROOT / "config.example.toml"
    assert paths.resource("config.example.toml").is_file()


# --- frozen: Windows / Linux one-dir ----------------------------------------


@pytest.mark.parametrize(
    ("exe_name", "platform"),
    [("dcent-voice.exe", "win32"), ("dcent-voice", "linux")],
)
def test_frozen_one_dir_bundle_root_is_the_internal_directory(
    tmp_path: Path, monkeypatch, exe_name: str, platform: str
) -> None:
    app = tmp_path / "DCENT_Voice"
    exe, internal = _one_dir(app, exe_name)
    _freeze(monkeypatch, executable=exe, meipass=internal, platform=platform)

    assert paths.is_frozen()
    # PyInstaller 6 one-dir: _MEIPASS is <app>/_internal, not <app>.
    assert paths.bundle_root() == internal
    assert paths.app_dir() == app
    assert paths.resource("config.example.toml") == internal / "config.example.toml"
    assert paths.resource("config.example.toml").is_file()


def test_frozen_bundled_models_dir_prefers_the_directory_beside_the_executable(
    tmp_path: Path, monkeypatch
) -> None:
    app = tmp_path / "DCENT_Voice"
    exe, internal = _one_dir(app, "dcent-voice.exe")
    beside = app / "models"
    beside.mkdir()
    _freeze(monkeypatch, executable=exe, meipass=internal)

    assert paths.bundled_models_dir() == beside


def test_frozen_bundled_models_dir_falls_back_into_the_bundle(tmp_path: Path, monkeypatch) -> None:
    app = tmp_path / "DCENT_Voice"
    exe, internal = _one_dir(app, "dcent-voice.exe")
    inside = internal / "models"
    inside.mkdir()
    _freeze(monkeypatch, executable=exe, meipass=internal)

    assert paths.bundled_models_dir() == inside


def test_frozen_without_meipass_falls_back_to_the_executable_directory(
    tmp_path: Path, monkeypatch
) -> None:
    app = tmp_path / "DCENT_Voice"
    app.mkdir()
    exe = app / "dcent-voice.exe"
    exe.write_bytes(b"MZ")
    _freeze(monkeypatch, executable=exe, meipass=None)

    assert paths.bundle_root() == app
    assert paths.app_dir() == app


# --- frozen: macOS .app ------------------------------------------------------


def test_macos_app_bundle_uses_the_contents_directory_that_holds_the_resources(
    tmp_path: Path, monkeypatch
) -> None:
    """PyInstaller >= 6 puts _MEIPASS in Contents/Frameworks; data may be in Resources."""
    contents = tmp_path / "DCENT_Voice.app" / "Contents"
    frameworks = contents / "Frameworks"
    resources = contents / "Resources"
    frameworks.mkdir(parents=True)
    resources.mkdir(parents=True)
    (resources / "config.example.toml").write_text("# example\n", encoding="utf-8")
    macos = contents / "MacOS"
    macos.mkdir()
    exe = macos / "dcent-voice"
    exe.write_bytes(b"\xcf\xfa\xed\xfe")
    _freeze(monkeypatch, executable=exe, meipass=frameworks, platform="darwin")

    assert paths.bundle_root() == resources
    assert paths.resource("config.example.toml").is_file()
    assert paths.app_dir() == macos


def test_macos_app_bundle_keeps_frameworks_when_it_holds_the_resources(
    tmp_path: Path, monkeypatch
) -> None:
    contents = tmp_path / "DCENT_Voice.app" / "Contents"
    frameworks = contents / "Frameworks"
    frameworks.mkdir(parents=True)
    (frameworks / "config.example.toml").write_text("# example\n", encoding="utf-8")
    macos = contents / "MacOS"
    macos.mkdir()
    exe = macos / "dcent-voice"
    exe.write_bytes(b"\xcf\xfa\xed\xfe")
    _freeze(monkeypatch, executable=exe, meipass=frameworks, platform="darwin")

    assert paths.bundle_root() == frameworks


def test_macos_copied_one_dir_layout_under_contents_macos(tmp_path: Path, monkeypatch) -> None:
    """scripts/build_macos_app.sh copies the one-dir tree into Contents/MacOS."""
    macos = tmp_path / "DCENT_Voice.app" / "Contents" / "MacOS"
    exe, internal = _one_dir(macos, "dcent-voice")
    _freeze(monkeypatch, executable=exe, meipass=internal, platform="darwin")

    assert paths.bundle_root() == internal
    assert paths.app_dir() == macos


# --- frozen: the exact trees the release builders produce --------------------
#
# These reproduce the directory structure of scripts/build_linux_appimage.sh and
# scripts/build_macos_app.sh literally, because "the resolver works on a layout
# we invented for the test" is precisely the mistake that shipped the first-run
# seeding bug. Each test asserts the two questions runtime code actually asks:
# where is config.example.toml, and where is the models directory.


def _payload(root: Path, exe_name: str) -> Path:
    """One PyInstaller one-dir payload, as `cp -a "$SRC/."` would leave it."""
    exe, internal = _one_dir(root, exe_name)
    (root / "models" / "parakeet-tdt-0.6b-v3").mkdir(parents=True)
    (internal / "LICENSE").write_text("MIT\n", encoding="utf-8")
    return exe


def test_appimage_layout_resolves_from_usr_bin(tmp_path: Path, monkeypatch) -> None:
    """AppRun exports usr/bin and execs it; the whole payload lives there.

    build_linux_appimage.sh does ``cp -a "$SRC/." "$APPDIR/usr/bin/"``, so at
    runtime (mounted at /tmp/.mount_XXXX) the executable, ``_internal`` and
    ``models`` are all siblings under ``usr/bin``.
    """
    mount = tmp_path / ".mount_DCENTxyz"
    usr_bin = mount / "usr" / "bin"
    exe = _payload(usr_bin, "dcent-voice")
    (mount / "AppRun").parent.mkdir(parents=True, exist_ok=True)
    (mount / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")
    (mount / "usr" / "share" / "applications").mkdir(parents=True)
    _freeze(monkeypatch, executable=exe, meipass=usr_bin / "_internal", platform="linux")

    assert paths.app_dir() == usr_bin
    assert paths.bundle_root() == usr_bin / "_internal"
    assert paths.resource("config.example.toml").is_file()
    assert paths.bundled_models_dir() == usr_bin / "models"
    assert (paths.bundled_models_dir() / "parakeet-tdt-0.6b-v3").is_dir()


def test_deb_layout_resolves_from_opt_even_though_usr_bin_is_a_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    """The .deb installs the payload in /opt and puts a shell wrapper in /usr/bin.

    ``/usr/bin/dcent-voice`` is a ``/bin/sh`` script that execs
    ``/opt/dcent-voice/dcent-voice``. PyInstaller sets ``sys.executable`` to the
    real binary, so resolution must land in /opt — never beside the wrapper.
    """
    root = tmp_path / "deb"
    opt = root / "opt" / "dcent-voice"
    exe = _payload(opt, "dcent-voice")
    usr_bin = root / "usr" / "bin"
    usr_bin.mkdir(parents=True)
    (usr_bin / "dcent-voice").write_text(
        '#!/bin/sh\nexec /opt/dcent-voice/dcent-voice "$@"\n', encoding="utf-8"
    )
    _freeze(monkeypatch, executable=exe, meipass=opt / "_internal", platform="linux")

    assert paths.app_dir() == opt
    assert paths.bundle_root() == opt / "_internal"
    assert paths.resource("config.example.toml").is_file()
    assert paths.bundled_models_dir() == opt / "models"
    assert usr_bin not in paths.app_dir().parents


def test_macos_app_layout_matches_build_macos_app_sh(tmp_path: Path, monkeypatch) -> None:
    """build_macos_app.sh copies the payload into Contents/MacOS, not Frameworks.

    ``Contents/Resources`` holds only the icon, so the ``Frameworks``/``Resources``
    disambiguation must not drag resolution out of ``Contents/MacOS``.
    """
    contents = tmp_path / "DCENT Voice.app" / "Contents"
    macos = contents / "MacOS"
    exe = _payload(macos, "dcent-voice")
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    (resources / "dcent-voice.icns").write_bytes(b"icns")
    (contents / "Info.plist").write_bytes(b"<plist/>")
    _freeze(monkeypatch, executable=exe, meipass=macos / "_internal", platform="darwin")

    assert paths.app_dir() == macos
    assert paths.bundle_root() == macos / "_internal"
    assert paths.resource("config.example.toml").is_file()
    assert paths.bundled_models_dir() == macos / "models"
    # The icon-only Resources directory must never be mistaken for the bundle.
    assert paths.bundle_root() != resources


def test_macos_app_with_a_space_in_its_name_still_resolves(tmp_path: Path, monkeypatch) -> None:
    """The shipped bundle really is named "DCENT Voice.app" — spaces included."""
    macos = tmp_path / "Applications" / "DCENT Voice.app" / "Contents" / "MacOS"
    exe = _payload(macos, "dcent-voice")
    _freeze(monkeypatch, executable=exe, meipass=macos / "_internal", platform="darwin")

    assert paths.resource("config.example.toml").read_text(encoding="utf-8") == "# example\n"
    assert paths.bundled_models_dir().is_dir()


# --- profile root override ---------------------------------------------------


def test_profile_root_is_unset_by_default(monkeypatch) -> None:
    monkeypatch.delenv(paths.PROFILE_ROOT_ENV, raising=False)
    assert paths.profile_root() is None


def test_profile_root_redirects_every_per_user_location(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(paths.PROFILE_ROOT_ENV, str(tmp_path))

    assert paths.profile_root() == tmp_path
    for location in (paths.user_config_dir(), paths.user_data_dir(), paths.user_state_dir()):
        assert tmp_path in location.parents or location == tmp_path


def test_profile_root_keeps_config_data_and_state_separate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(paths.PROFILE_ROOT_ENV, str(tmp_path))

    locations = {paths.user_config_dir(), paths.user_data_dir(), paths.user_state_dir()}
    assert len(locations) == 3


def test_profile_root_reaches_config_logs_privacy_and_registry(tmp_path: Path, monkeypatch) -> None:
    """Everything derived from user_config_dir must follow the override."""
    from dcent_voice import config as config_module
    from dcent_voice import privacy
    from dcent_voice.asr import model_registry
    from dcent_voice.attach import registry
    from dcent_voice.util import logging as app_logging

    monkeypatch.setenv(paths.PROFILE_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(model_registry.MODEL_DIR_ENV, raising=False)

    for candidate in (
        config_module.default_config_path(),
        app_logging.default_log_path(),
        privacy.default_consent_ledger_path(),
        privacy.default_egress_log_path(),
        registry.default_registry_dir(),
        model_registry.model_root(),
    ):
        assert tmp_path in candidate.parents, candidate


def test_empty_profile_root_env_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv(paths.PROFILE_ROOT_ENV, "   ")
    assert paths.profile_root() is None


# --- repo-wide guard ---------------------------------------------------------

# Development/eval harnesses that legitimately need the repository layout. Each
# fails with an explicit message when reached from a frozen build.
PARENTS_ALLOWLIST = {
    "util/paths.py",  # the one definition of the source-checkout root
}

_PARENTS = re.compile(r"parents\[")


def test_runtime_code_never_guesses_paths_with_file_parents() -> None:
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "dcent_voice").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT / "src" / "dcent_voice").as_posix()
        if relative in PARENTS_ALLOWLIST:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _PARENTS.search(line) and "``" not in line:
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == [], (
        "runtime code must resolve bundle paths through dcent_voice.util.paths, not "
        "Path(__file__).parents[n]:\n" + "\n".join(offenders)
    )


def test_development_harnesses_refuse_to_run_from_a_frozen_build(monkeypatch) -> None:
    from dcent_voice import eval_corpus, pipeline

    monkeypatch.setattr(paths, "is_frozen", lambda: True)

    with pytest.raises(RuntimeError, match="development-only"):
        eval_corpus.default_corpus_path()
    with pytest.raises(RuntimeError, match="hold-release|source checkout"):
        pipeline._hold_release_fixture()
