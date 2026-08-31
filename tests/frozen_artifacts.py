# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Guards for tests that spawn a prebuilt frozen executable from ``dist\\``.

``tests/conftest.py`` gives every test its own ``DCENT_VOICE_PROFILE_ROOT``, and
a child process inherits it — but only a build that already understands the
variable will honor it. A payload built before WS6 ignores it and writes to the
developer's real ``%APPDATA%\\DCENT_Voice`` while the assertions inspect the
isolated profile, so a check like "the shipped default must not create a
personalization store" would pass without testing anything at all.

That silent-pass mode is the thing to prevent, so these tests skip with an
actionable reason rather than run against a payload that cannot be isolated.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

#: Probe result per executable path; a launch costs seconds, so do it once.
_PROFILE_ROOT_AWARE: dict[str, bool] = {}


def frozen_exe_honors_profile_root(exe: Path) -> bool:
    """True when ``exe`` confines its state to ``DCENT_VOICE_PROFILE_ROOT``.

    Asks the build itself with ``--print-config``, which prints the config path
    it resolved: an override-aware build names a path inside the probe
    directory, an older one names the real user profile.
    """
    key = str(exe.resolve())
    cached = _PROFILE_ROOT_AWARE.get(key)
    if cached is not None:
        return cached
    honors = False
    with tempfile.TemporaryDirectory(prefix="dcent_voice_probe_") as probe:
        env = os.environ.copy()
        env["DCENT_VOICE_PROFILE_ROOT"] = probe
        env["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"
        env["DCENT_VOICE_NO_DIALOGS"] = "1"
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [str(exe), "--print-config"],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            honors = False
        else:
            honors = completed.returncode == 0 and probe in completed.stdout
    _PROFILE_ROOT_AWARE[key] = honors
    return honors


def require_isolatable_frozen_exe(exe: Path) -> None:
    """Skip unless ``exe`` exists and keeps its state inside the test profile."""
    if not exe.is_file():
        pytest.skip(f"no frozen executable at {exe}; build it with scripts/build_pyinstaller.ps1")
    if not frozen_exe_honors_profile_root(exe):
        pytest.skip(
            f"{exe} predates DCENT_VOICE_PROFILE_ROOT and would write the real user profile "
            "while this test inspects the isolated one; rebuild it with "
            "scripts/build_pyinstaller.ps1"
        )
