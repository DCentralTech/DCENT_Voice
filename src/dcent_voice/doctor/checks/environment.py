# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Where the app is, where its state lives, and whether that ground is solid.

These checks answer the questions a remote helper asks first: which build is
this, which profile is it using, can it write there, and is the profile a
OneDrive-redirected or junctioned path (which the model verifier rejects).
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import stat
import sys
from pathlib import Path

from dcent_voice.util import paths

from ..result import FAIL, PASS, WARN, CheckResult

#: Below this the install or a model unpack can fail mid-write.
_DISK_FAIL_BYTES = 500 * 1024 * 1024
_DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024

#: pythonnet (the pywebview host on Windows) needs .NET Framework 4.7.2, which
#: is preinstalled from Windows 10 1809 (build 17763) onward.
_WINDOWS_BUILD_FLOOR = 17763


def run() -> list[CheckResult]:
    return [
        check_os(),
        check_architecture(),
        check_install_layout(),
        check_profile_paths(),
        check_disk_space(),
        check_write_access(),
        check_redirected_paths(),
    ]


def check_os() -> CheckResult:
    data = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    build = windows_build()
    if build is not None:
        data["build"] = build
        if build < _WINDOWS_BUILD_FLOOR:
            return CheckResult(
                "env.os",
                WARN,
                f"Windows build {build} is below the supported floor {_WINDOWS_BUILD_FLOOR} "
                "(Windows 10 version 1809).",
                "Dictation may still work, but Settings, the overlay and the setup wizard "
                "need .NET Framework 4.7.2 and the Edge WebView2 runtime. Update Windows, "
                "or install .NET Framework 4.8 from Microsoft.",
                data,
            )
    return CheckResult(
        "env.os",
        PASS,
        f"{data['system']} {data['release']}" + (f" build {build}" if build else ""),
        data=data,
    )


def check_architecture() -> CheckResult:
    is_64bit = sys.maxsize > 2**32
    data = {"pointerBits": 64 if is_64bit else 32, "machine": platform.machine()}
    if not is_64bit:
        return CheckResult(
            "env.architecture",
            FAIL,
            "This is a 32-bit process; DCENT_Voice ships 64-bit binaries only.",
            "Install the x64 build on a 64-bit Windows, Linux or macOS host.",
            data,
        )
    return CheckResult("env.architecture", PASS, "64-bit process", data=data)


def check_install_layout() -> CheckResult:
    data = {
        "frozen": paths.is_frozen(),
        "executable": str(sys.executable or ""),
        "cwd": _safe_cwd(),
        "bundleRoot": str(paths.bundle_root()),
        "appDir": str(paths.app_dir()),
        "bundledModelsDir": str(paths.bundled_models_dir()),
        # Whether this run had a console at all. A windowed build launched from a
        # shortcut has sys.stdout is None, which is the environment in which an
        # unguarded write would make the shortcut fail invisibly — so record it
        # rather than infer it from how the report happened to be produced.
        "stdout": _stream_kind(getattr(sys, "stdout", None)),
        "stderr": _stream_kind(getattr(sys, "stderr", None)),
    }
    marker = paths.resource("config.example.toml")
    data["configExample"] = str(marker)
    if not marker.is_file():
        return CheckResult(
            "env.install",
            FAIL,
            f"The bundled default configuration is missing at {marker}. Without it the app "
            "cannot create a config on a machine that has never run it.",
            "Reinstall DCENT_Voice from the official Setup.exe or portable ZIP; do not copy "
            "individual files out of the payload folder.",
            data,
        )
    kind = "frozen build" if data["frozen"] else "source checkout"
    return CheckResult("env.install", PASS, f"{kind}; bundle root {data['bundleRoot']}", data=data)


def profile_directories() -> dict[str, Path]:
    """The per-user locations the app writes to, keyed by role."""
    return {
        "config": paths.user_config_dir(),
        "logs": paths.user_config_dir() / "logs",
        "data": paths.user_data_dir(),
        "state": paths.user_state_dir(),
    }


def check_profile_paths() -> CheckResult:
    override = paths.profile_root()
    directories = profile_directories()
    data = {role: str(path) for role, path in directories.items()}
    data["profileRootOverride"] = str(override) if override else ""
    data["configFile"] = str(paths.user_config_dir() / "config.toml")
    detail = "profile root override in effect" if override else "platform user profile"
    return CheckResult("env.profile", PASS, f"{detail}: {directories['config']}", data=data)


def check_disk_space() -> CheckResult:
    targets = {"install": paths.app_dir(), "profile": paths.user_config_dir()}
    data: dict[str, object] = {}
    worst = PASS
    problems: list[str] = []
    for role, path in targets.items():
        anchor = _existing_ancestor(path)
        try:
            usage = shutil.disk_usage(anchor)
        except OSError as exc:
            data[role] = {"path": str(path), "error": str(exc)}
            worst = WARN if worst == PASS else worst
            problems.append(f"{role}: cannot measure free space ({exc})")
            continue
        free_gb = usage.free / (1024**3)
        data[role] = {"path": str(path), "measuredAt": str(anchor), "freeBytes": usage.free}
        if usage.free < _DISK_FAIL_BYTES:
            worst = FAIL
            problems.append(f"{role} drive has only {free_gb:.2f} GB free")
        elif usage.free < _DISK_WARN_BYTES:
            worst = WARN if worst != FAIL else worst
            problems.append(f"{role} drive has {free_gb:.2f} GB free")
    if worst == PASS:
        summary = ", ".join(
            f"{role} {entry['freeBytes'] / (1024**3):.1f} GB free"  # type: ignore[index]
            for role, entry in data.items()
            if isinstance(entry, dict) and "freeBytes" in entry
        )
        return CheckResult("env.disk_space", PASS, summary or "measured", data=data)
    return CheckResult(
        "env.disk_space",
        worst,
        "; ".join(problems),
        "Free up disk space. The install needs about 1 GB and the first model load writes "
        "logs and diagnostics into the user profile.",
        data,
    )


def check_write_access() -> CheckResult:
    failures: list[str] = []
    data: dict[str, object] = {}
    for role, path in profile_directories().items():
        ok, detail = _probe_write(path)
        data[role] = {"path": str(path), "writable": ok, "detail": detail}
        if not ok:
            failures.append(f"{role} ({path}): {detail}")
    if failures:
        return CheckResult(
            "env.write_access",
            FAIL,
            "DCENT_Voice cannot write to " + "; ".join(failures),
            "Check folder permissions, disk space, and any security software that locks the "
            "user profile. Setting DCENT_VOICE_PROFILE_ROOT to a writable directory is a "
            "quick workaround.",
            data,
        )
    return CheckResult("env.write_access", PASS, "all profile directories are writable", data=data)


def check_redirected_paths() -> CheckResult:
    """Reparse points and OneDrive redirection break model verification (S5)."""
    candidates: dict[str, Path] = {}
    for name in ("LOCALAPPDATA", "APPDATA"):
        raw = os.environ.get(name)
        if raw:
            candidates[name] = Path(raw)
    override = paths.profile_root()
    if override is not None:
        candidates["DCENT_VOICE_PROFILE_ROOT"] = override
    candidates["configDir"] = paths.user_config_dir()
    candidates["modelsDir"] = paths.user_data_dir()

    data: dict[str, object] = {}
    flagged: list[str] = []
    for label, path in candidates.items():
        entry = {
            "path": str(path),
            "reparsePoint": is_reparse_point(path),
            "oneDrive": _looks_like_onedrive(path),
        }
        data[label] = entry
        if entry["reparsePoint"]:
            flagged.append(f"{label} ({path}) is a reparse point/junction")
        elif entry["oneDrive"]:
            flagged.append(f"{label} ({path}) looks OneDrive-redirected")
    if flagged:
        return CheckResult(
            "env.redirected_paths",
            WARN,
            "; ".join(flagged)
            + ". Model verification rejects reparse points, and OneDrive Files-On-Demand can "
            "make model files unreadable placeholders.",
            "Install to a non-synced folder (Setup.exe /D=C:\\DCENT_Voice), or exclude the "
            "DCENT_Voice folders from OneDrive and mark them 'Always keep on this device'.",
            data,
        )
    return CheckResult("env.redirected_paths", PASS, "no redirected profile paths", data=data)


def windows_build() -> int | None:
    """The Windows build number, or ``None`` off Windows / when unavailable."""
    if sys.platform != "win32":
        return None
    version = platform.version()
    parts = version.split(".")
    if len(parts) >= 3 and parts[2].isdigit():
        return int(parts[2])
    return None


def is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _looks_like_onedrive(path: Path) -> bool:
    text = str(path).casefold()
    if "onedrive" in text:
        return True
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(name)
        if root and text.startswith(root.casefold()):
            return True
    return False


def _existing_ancestor(path: Path) -> Path:
    current = path
    for candidate in (current, *current.parents):
        if candidate.exists():
            return candidate
    return Path(current.anchor or ".")


def _probe_write(path: Path) -> tuple[bool, str]:
    probe = path / ".dcent-voice-doctor-write-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc.strerror or exc}"
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return True, "writable"


def _stream_kind(stream: object) -> str:
    """Describe a standard stream without assuming it exists or is usable."""
    if stream is None:
        return "none (no console: windowed or detached launch)"
    try:
        return f"{type(stream).__name__} (tty)" if stream.isatty() else type(stream).__name__
    except (ValueError, OSError, AttributeError):
        return f"{type(stream).__name__} (unusable)"


def _safe_cwd() -> str:
    try:
        return str(Path.cwd())
    except OSError as exc:  # pragma: no cover - a deleted cwd is rare but real
        return f"<unavailable: {exc}>"
