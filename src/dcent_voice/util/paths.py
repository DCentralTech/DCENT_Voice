# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Single source of truth for bundle, resource and per-user profile paths.

Every packaging layout DCENT_Voice ships answers "where is resource X" and
"where does this user's state live" differently:

* source checkout           ``<repo>/config.example.toml``
* PyInstaller one-dir (Win) ``<app>\\_internal\\config.example.toml`` (``sys._MEIPASS``)
* PyInstaller one-dir (Lin) ``<app>/_internal/config.example.toml``
* macOS ``.app``            ``…/Contents/Frameworks`` (PyInstaller >= 6) with the
                            data files reachable through ``…/Contents/Resources``

Runtime code must never guess with ``Path(__file__).resolve().parents[n]``:
that expression silently resolves to the wrong directory in a frozen build and
is the root cause of the first-run config seeding failure (see
the fresh-machine reliability work).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

APP_NAME = "DCENT_Voice"

#: Test/automation override. When set, *every* per-user location (config, data,
#: state) lives beneath this directory instead of the platform profile, so a
#: fresh-machine run can be simulated without touching the real user profile.
PROFILE_ROOT_ENV = "DCENT_VOICE_PROFILE_ROOT"

#: A data file that is present in every packaging layout at the bundle root.
#: Used to disambiguate the macOS ``.app`` ``Frameworks`` / ``Resources`` split.
_BUNDLE_MARKER = "config.example.toml"


def is_frozen() -> bool:
    """True when running from a PyInstaller (or equivalent) frozen build."""
    return bool(getattr(sys, "frozen", False))


def source_root() -> Path:
    """Repository root of a source checkout.

    Only meaningful for a development/eval harness; frozen builds have no
    repository. Callers that may run frozen must guard with :func:`is_frozen`.
    """
    return Path(__file__).resolve().parents[3]


def bundle_root() -> Path:
    """Directory that holds the bundled data files (``config.example.toml`` …)."""
    if not is_frozen():
        return source_root()
    meipass = getattr(sys, "_MEIPASS", None)
    root = Path(str(meipass)) if meipass else _executable_dir()
    return _macos_resource_root(root)


def app_dir() -> Path:
    """Directory that holds the launchable application.

    Frozen: the directory containing the executable (the payload root, i.e. the
    parent of ``_internal`` for a PyInstaller one-dir build). Source: the repo
    root. This is the correct working directory for a launch descriptor.
    """
    if is_frozen():
        return _executable_dir()
    return source_root()


def resource(*parts: str) -> Path:
    """Path to a bundled data file, e.g. ``resource("config.example.toml")``."""
    return bundle_root().joinpath(*parts)


def bundled_models_dir() -> Path:
    """Shipped model root: beside the executable, falling back to the bundle."""
    beside_exe = app_dir() / "models"
    if beside_exe.is_dir():
        return beside_exe
    inside_bundle = bundle_root() / "models"
    if inside_bundle.is_dir():
        return inside_bundle
    return beside_exe


def profile_root() -> Path | None:
    """The ``DCENT_VOICE_PROFILE_ROOT`` override, or ``None`` when unset."""
    raw = (os.environ.get(PROFILE_ROOT_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def user_config_dir() -> Path:
    """Per-user configuration directory (config, logs, privacy ledger, …)."""
    root = profile_root()
    if root is not None:
        return root / "config"
    import platformdirs

    # roaming=True + appauthor=False keeps the existing Windows path
    # (%APPDATA%\DCENT_Voice) while giving the native location on macOS
    # (~/Library/Application Support/DCENT_Voice) and Linux (~/.config/DCENT_Voice).
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))


def user_data_dir(
    app_name: str = APP_NAME, *, appauthor: str | Literal[False] | None = False
) -> Path:
    """Per-user data directory (durable models)."""
    root = profile_root()
    if root is not None:
        return root / "data" / app_name
    import platformdirs

    return Path(platformdirs.user_data_dir(app_name, appauthor=appauthor))


def user_state_dir(
    app_name: str = APP_NAME, app_author: str | Literal[False] | None = False
) -> Path:
    """Per-user state directory (ADE registry, tokens, instance lock)."""
    root = profile_root()
    if root is not None:
        return root / "state" / app_name
    import platformdirs

    return Path(platformdirs.user_state_dir(app_name, app_author))


def _executable_dir() -> Path:
    executable = getattr(sys, "executable", None)
    if not executable:  # pragma: no cover - defensive; frozen builds always set it
        return Path.cwd()
    return Path(executable).resolve().parent


def _macos_resource_root(root: Path) -> Path:
    """Resolve the ``.app`` directory that actually contains the data files.

    PyInstaller >= 6 puts ``sys._MEIPASS`` at ``Contents/Frameworks`` and links
    data into ``Contents/Resources``; older layouts use ``Contents/MacOS``. Pick
    whichever sibling really holds the bundled files rather than assuming.
    """
    if sys.platform != "darwin":
        return root
    contents = root.parent
    if contents.name != "Contents":
        return root
    for candidate in (root, contents / "Resources", contents / "Frameworks"):
        if (candidate / _BUNDLE_MARKER).is_file():
            return candidate
    return root
