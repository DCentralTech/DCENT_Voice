# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Describe attachable modules for the service registry."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from platformdirs import user_state_dir

from dcent_voice.sovereignty import (
    advertised_capabilities,
    default_capability_sovereignty,
    sovereignty_class_for_service_bind,
)
from dcent_voice.util import paths

APP_AUTHOR = "D-Central Technologies"
APP_NAME = "DCENT"
MODULES_DIRNAME = "modules"
_CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


@dataclass(frozen=True)
class ModuleRegistryEntry:
    moduleId: str
    displayName: str
    version: str
    endpoint: str
    tokenRef: str
    sovereigntyClass: str = "LOCAL"
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    # DVAP 1.1: per-capability data-flow declarations for operations whose class
    # differs from the module default. Each item is {capability, sovereigntyClass,
    # reason?} per registry-entry.schema.json.
    capabilitySovereignty: tuple[dict[str, str], ...] = field(default_factory=tuple)
    pid: int | None = None
    launch: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        data["capabilitySovereignty"] = [dict(block) for block in self.capabilitySovereignty]
        if self.pid is None:
            data.pop("pid")
        if self.launch is None:
            data.pop("launch")
        return data


def default_registry_dir() -> Path:
    # An explicit profile root (test/automation) outranks the platform location
    # so a fresh-machine run never writes into the real user profile.
    if paths.profile_root() is not None:
        return paths.user_state_dir(APP_NAME) / MODULES_DIRNAME
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME / MODULES_DIRNAME
    return Path(user_state_dir(APP_NAME, APP_AUTHOR)) / MODULES_DIRNAME


def create_token() -> str:
    return secrets.token_urlsafe(32)


def create_registry_entry(
    *,
    endpoint: str,
    version: str,
    registry_dir: Path | None = None,
    pid: int | None = None,
    token: str | None = None,
    tts_available: bool = False,
    voice_control_available: bool = True,
    launch: dict[str, Any] | None = None,
) -> ModuleRegistryEntry:
    root = registry_dir or default_registry_dir()
    token_path = root / "dcent-voice.token"
    write_text_atomic(token_path, token or create_token())
    endpoint_host = urlparse(endpoint).hostname or ""
    return ModuleRegistryEntry(
        moduleId="dcent-voice",
        displayName="DCENT Voice",
        version=version,
        endpoint=endpoint,
        tokenRef=str(token_path),
        sovereigntyClass=sovereignty_class_for_service_bind(endpoint_host).value,
        # STT plus the consent-gated model-download capability always; TTS
        # (tts.append/tts.cancel/barge_in) only when a backend's model assets are
        # actually present, so discovery reflects real service state (Wave E1).
        capabilities=advertised_capabilities(
            tts_available=tts_available,
            voice_control_available=voice_control_available,
        ),
        capabilitySovereignty=tuple(default_capability_sovereignty()),
        pid=pid or os.getpid(),
        launch=launch,
    )


def write_registry_entry(
    entry: ModuleRegistryEntry,
    *,
    registry_dir: Path | None = None,
) -> Path:
    root = registry_dir or default_registry_dir()
    path = root / f"{entry.moduleId}.json"
    write_text_atomic(path, json.dumps(entry.to_json(), indent=2, sort_keys=True))
    return path


def read_registry_entry(path: Path) -> ModuleRegistryEntry:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ModuleRegistryEntry(
        moduleId=str(raw["moduleId"]),
        displayName=str(raw["displayName"]),
        version=str(raw["version"]),
        endpoint=str(raw["endpoint"]),
        tokenRef=str(raw["tokenRef"]),
        sovereigntyClass=str(raw.get("sovereigntyClass", "LOCAL")),
        capabilities=tuple(str(capability) for capability in raw.get("capabilities", [])),
        capabilitySovereignty=tuple(
            {str(key): str(value) for key, value in block.items()}
            for block in raw.get("capabilitySovereignty", [])
            if isinstance(block, dict)
        ),
        pid=int(raw["pid"]) if "pid" in raw else None,
        launch=dict(raw["launch"]) if isinstance(raw.get("launch"), dict) else None,
    )


def build_launch_descriptor() -> dict[str, Any]:
    """Return the installed entrypoint the native host may spawn, never renderer input."""

    if getattr(sys, "frozen", False):
        return {"command": sys.executable, "args": ["--no-tray", "--no-overlay"]}
    return {
        "command": sys.executable,
        "args": ["-m", "dcent_voice", "--no-tray", "--no-overlay"],
        # Never the caller's cwd: the host may spawn us from anywhere.
        "cwd": str(paths.app_dir()),
    }


def write_install_manifest(*, registry_dir: Path | None = None) -> Path:
    """Persist a non-secret cold-launch descriptor alongside ephemeral live state."""

    root = registry_dir or default_registry_dir()
    path = root / "dcent-voice.install.json"
    payload = {"moduleId": "dcent-voice", "launch": build_launch_descriptor()}
    write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True),
        require_private=False,
    )
    return path


def remove_registry_entry(entry: ModuleRegistryEntry, *, registry_dir: Path | None = None) -> None:
    root = registry_dir or default_registry_dir()
    for path in (root / f"{entry.moduleId}.json", Path(entry.tokenRef)):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def remove_stale_registry_entries(*, registry_dir: Path | None = None) -> list[Path]:
    root = registry_dir or default_registry_dir()
    removed: list[Path] = []
    if not root.exists():
        return removed

    for path in root.glob("*.json"):
        try:
            entry = read_registry_entry(path)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if entry.pid is not None and not is_pid_running(entry.pid):
            path.unlink(missing_ok=True)
            # Also drop the bearer token file so a dead instance cannot leave
            # a readable session secret on disk.
            with contextlib.suppress(OSError, ValueError):
                token_path = Path(entry.tokenRef)
                if token_path.is_file():
                    token_path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_pid_running_windows(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_pid_running_windows(pid: int) -> bool:
    # os.kill(pid, 0) is NOT a liveness check on Windows: CPython maps signal 0
    # to GenerateConsoleCtrlEvent, which raises WinError 87 for a non-console PID
    # and surfaces as an uncatchable SystemError. Query the process directly via
    # OpenProcess instead.
    import ctypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - Windows-only path
        return False

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # No handle: access-denied means the process exists but is privileged;
        # anything else (e.g. invalid parameter) means it is gone.
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
            return True  # exists but could not be queried; assume alive
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def write_text_atomic(path: Path, contents: str, *, require_private: bool = True) -> None:
    """Atomically publish text, requiring verified owner-only ACLs by default.

    Session tokens and live registry entries use the strict default. The
    persistent install manifest is non-secret and explicitly opts into
    best-effort restriction so an ACL tooling failure cannot break installation.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        if require_private:
            # The randomly named file is visible as soon as mkstemp returns.
            # Secure and verify it while it is still empty so inherited Windows
            # directory ACLs can never expose token/transcript bytes, even for
            # the short interval before atomic publication.
            restrict_private_file(temp_path)
        else:
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                restrict_private_file(temp_path)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1  # ownership transferred to ``handle``
        with handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        if require_private:
            try:
                _verify_private_file(path)
            except BaseException:
                # A move should preserve the secured ACL, but verify the final
                # public name and remove it if the platform says otherwise.
                with contextlib.suppress(OSError):
                    path.unlink()
                raise
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def restrict_private_file(path: Path) -> None:
    """Apply and verify owner-only access, raising if privacy cannot be proven."""

    if sys.platform == "win32":
        _restrict_private_file_windows(path)
    else:
        os.chmod(path, 0o600)
    _verify_private_file(path)


def verify_private_file(path: Path) -> None:
    """Verify an existing file is owner-only without changing its permissions."""

    _verify_private_file(path)


def _verify_private_file(path: Path) -> None:
    if sys.platform == "win32":
        _verify_private_file_windows(path)
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise PermissionError(f"private file mode verification failed: {oct(mode)}")


#: Well-known SIDs that an elevated token's *default DACL* stamps onto every
#: file the process creates: local SYSTEM and the local Administrators group.
#: ``/inheritance:r`` does not touch them because they are explicit ACEs, not
#: inherited ones, so a credential written from an admin session came out
#: readable by both and failed owner-only verification. Referenced by SID rather
#: than by name so this works on a non-English Windows too.
_LOCAL_SYSTEM_SID = "*S-1-5-18"
_LOCAL_ADMINISTRATORS_SID = "*S-1-5-32-544"


def _restrict_private_file_windows(path: Path) -> None:
    """Remove inherited ACEs; allow only the current user full control."""
    # icacls is available on all supported Windows builds used for DCENT.
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        raise PermissionError("USERNAME is required to restrict a private file")
    try:
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:(R,W)",
                # The account keeps access through the explicit grant above, so
                # dropping these does not lock an administrator out of a file
                # their own token created.
                "/remove",
                _LOCAL_SYSTEM_SID,
                "/remove",
                _LOCAL_ADMINISTRATORS_SID,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionError("could not invoke private-file ACL restriction") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "icacls failed").strip()
        raise PermissionError(f"could not restrict private file ACL: {detail}")


def _verify_private_file_windows(path: Path) -> None:
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        raise PermissionError("USERNAME is required to verify a private file")
    try:
        result = subprocess.run(
            ["icacls", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionError("could not invoke private-file ACL verification") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "icacls verification failed").strip()
        raise PermissionError(f"could not verify private file ACL: {detail}")
    output = result.stdout or ""
    if "(I)" in output:
        raise PermissionError("private file ACL still contains inherited entries")
    expected = user.casefold()
    principals: list[str] = []
    rendered_path = str(path).casefold()
    basename = path.name.casefold()
    for line in output.splitlines():
        if ":(" not in line:
            continue
        principal = line.split(":(", 1)[0].strip().casefold()
        if principal.startswith(rendered_path):
            principal = principal[len(rendered_path) :].strip()
        elif basename and basename in principal:
            # icacls echoes the path it resolved, which is not always the
            # spelling we passed: an 8.3 short component expands to its long
            # form. Comparing against our own string then fails to strip the
            # prefix, and the whole '<path> <principal>' run is mistaken for a
            # principal. Strip through the file name, which both spellings share.
            principal = principal[principal.rfind(basename) + len(basename) :].strip()
        if principal:
            principals.append(principal)
    if not principals or any(
        principal != expected and not principal.endswith(f"\\{expected}")
        for principal in principals
    ):
        raise PermissionError(
            "private file ACL is not restricted to the current user "
            f"(expected {expected!r}, parsed {principals!r})"
        )
