# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Local faster-whisper model discovery and offline-bundle installation."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dcent_voice.util import paths

APP_NAME = "DCENT_Voice"
WINDOWS_MODEL_APP_NAME = "DCENT_Voice.Models"
MODEL_DIR_ENV = "DCENT_VOICE_MODEL_DIR"
REGISTRY_FILENAME = "dcent-voice-models.json"
PINNED_BASE_MODEL_ID = "Systran/faster-whisper-base"
PINNED_BASE_REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
PINNED_PARAKEET_MODEL_ID = "istupakov/parakeet-tdt-0.6b-v3-onnx"
PINNED_PARAKEET_REVISION = "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"
_MANIFEST_DIR = Path(__file__).with_name("manifests")
_LOG = logging.getLogger(__name__)


class ModelUnavailableError(RuntimeError):
    """A local model is absent or corrupt; runtime download is forbidden."""


# faster-whisper aliases resolve to these Hugging Face repositories. Keep tiny
# and tiny.en distinct: the former is multilingual, while the latter is English-only.
MODEL_ALIASES: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
}


@dataclass(frozen=True)
class InstalledModel:
    provider: str
    model_id: str
    path: str
    installed_at: str


def model_root() -> Path:
    """Return the runtime model root, honoring the explicit environment override."""
    override = os.environ.get(MODEL_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # Windows Setup installs executable payloads at
    # ``%LOCALAPPDATA%\DCENT_Voice``.  Model data must be a sibling, not a
    # child of that replaceable tree, so upgrades and ordinary uninstall can
    # never erase explicitly installed models.  Other platforms already keep
    # application data outside their native package locations.
    if os.name == "nt":
        return paths.user_data_dir(WINDOWS_MODEL_APP_NAME)
    return paths.user_data_dir(APP_NAME) / "models"


def faster_whisper_root(*, root: Path | None = None) -> Path:
    return (root or model_root()) / "faster-whisper"


def safe_model_dir_name(model_id: str) -> str:
    """Encode a repository-style model ID as one safe directory component."""
    value = str(model_id).strip()
    # Bundles are untrusted input. Never allow a model ID to become a path, a
    # Windows drive-qualified path, or an empty/dot component before replacing
    # the one separator valid for a Hugging Face repository identifier.
    if not value or "\\" in value or ":" in value or value.startswith("/"):
        raise ValueError(f"Invalid model ID for local registry: {model_id!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Invalid model ID for local registry: {model_id!r}")
    return "--".join(parts)


def canonical_model_id(model_reference: str) -> str:
    """Map a faster-whisper alias or profile spec to its canonical repository ID."""
    reference = _model_name_from_reference(model_reference)
    return MODEL_ALIASES.get(reference.lower(), reference)


def runtime_model_path(model_id: str, *, root: Path | None = None) -> Path:
    """Return the verified one-component runtime destination for a model ID."""
    destination_root = faster_whisper_root(root=root).resolve()
    destination = (destination_root / safe_model_dir_name(model_id)).resolve()
    if destination.parent != destination_root:
        raise ValueError(f"Model ID escapes the local registry: {model_id!r}")
    return destination


def valid_faster_whisper_snapshot(path: Path) -> bool:
    """Whether ``path`` has the minimum complete CTranslate2 snapshot structure."""
    if not path.is_dir():
        return False
    config = path / "config.json"
    weights = path / "model.bin"
    if not config.is_file() or not weights.is_file():
        return False
    if config.is_symlink() or weights.is_symlink() or weights.stat().st_size <= 0:
        return False
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict)


def pinned_model_manifest(model_id: str) -> dict[str, Any] | None:
    """Return immutable checked metadata for a model shipped by the product."""
    filenames = {
        PINNED_BASE_MODEL_ID: "faster-whisper-base.json",
        PINNED_PARAKEET_MODEL_ID: "parakeet-tdt-0.6b-v3.json",
    }
    filename = filenames.get(model_id)
    if filename is None:
        return None
    path = _MANIFEST_DIR / filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelUnavailableError(
            f"The built-in verification manifest for {model_id} is unavailable. "
            "Reinstall DCENT Voice from a verified package."
        ) from exc
    if (
        raw.get("schemaVersion") != 1
        or raw.get("modelId") != model_id
        or raw.get("revision")
        != {
            PINNED_BASE_MODEL_ID: PINNED_BASE_REVISION,
            PINNED_PARAKEET_MODEL_ID: PINNED_PARAKEET_REVISION,
        }[model_id]
        or not isinstance(raw.get("files"), dict)
    ):
        raise ModelUnavailableError(f"The model manifest for {model_id} is invalid.")
    return raw


def verify_pinned_snapshot(path: Path, model_id: str) -> tuple[bool, str]:
    """Verify an exact, ordinary, single-link snapshot using bound handles."""
    manifest = pinned_model_manifest(model_id)
    if manifest is None:
        return valid_faster_whisper_snapshot(path), "unmanaged local snapshot"
    safe, detail = _safe_directory(path)
    if not safe:
        return False, detail
    expected_names = set(manifest["files"])
    try:
        entries = tuple(path.iterdir())
    except OSError:
        return False, "cannot enumerate snapshot directory"
    actual_names = {entry.name for entry in entries}
    folded = [name.casefold() for name in actual_names]
    if len(folded) != len(set(folded)):
        return False, "case-colliding snapshot entries"
    undeclared = sorted(actual_names - expected_names)
    missing = sorted(expected_names - actual_names)
    if undeclared:
        return False, f"undeclared snapshot entries: {', '.join(undeclared)}"
    if missing:
        return False, f"missing snapshot files: {', '.join(missing)}"
    for entry in entries:
        safe, detail = _safe_regular_file(entry)
        if not safe:
            return False, detail
    # Every declared file is hashed on every verification. Nothing here is
    # cached: a size/mtime key would be attacker-settable (os.utime restores a
    # timestamp, and a same-size payload keeps the size), so a "verified"
    # verdict must never be reachable without reading the bytes.
    for name, expected in manifest["files"].items():
        file_path = path / name
        try:
            digest, size = _hash_bound_file(file_path)
        except OSError:
            return False, f"cannot read file: {name}"
        except ValueError as exc:
            return False, f"unsafe file {name}: {exc}"
        if type(expected.get("size")) is not int or size != expected["size"]:
            return False, f"size mismatch: {name}"
        if digest != expected.get("sha256"):
            return False, f"SHA-256 mismatch: {name}"
        safe, detail = _safe_regular_file(file_path, strip_mark_of_the_web=True)
        if not safe:
            return False, detail
    try:
        final_entries = tuple(path.iterdir())
    except OSError:
        return False, "cannot re-enumerate snapshot directory"
    if {entry.name for entry in final_entries} != expected_names:
        return False, "snapshot entries changed during verification"
    return True, "verified"


#: Appended to every reparse-point rejection. The realistic cause is not an
#: attack but a synced or redirected profile: OneDrive Files-On-Demand turns
#: files under %LOCALAPPDATA%/%USERPROFILE% into reparse points, and folder
#: redirection does the same for the whole tree.
_REPARSE_HINT = (
    " — this is usually OneDrive Files-On-Demand or a redirected/synced folder "
    "turning the file into a placeholder; install to a folder that is not synced "
    "(Setup: /D=C:\\DCENT_Voice) or mark the folder 'Always keep on this device'"
)

#: NTFS stamps this stream on anything Explorer extracts from a downloaded ZIP
#: (Mark-of-the-Web). It carries no file data, so it cannot change a hash — it
#: is stripped after verification instead of failing the snapshot.
_ZONE_IDENTIFIER = "Zone.Identifier"

#: A real Mark-of-the-Web is a short INI stanza (``[ZoneTransfer]`` plus a
#: ``ZoneId``, sometimes a referrer/host URL) — tens of bytes, not kilobytes.
#: The stream's contents are never hashed, so tolerating it unbounded would let
#: arbitrary unverified bytes ride along inside a snapshot that claims to be
#: byte-exact. 4 KiB is far above any genuine value and far below useful payload.
_MAX_ZONE_IDENTIFIER_BYTES = 4096


def _safe_directory(path: Path) -> tuple[bool, str]:
    try:
        info = os.lstat(path)
    except OSError:
        return False, "snapshot directory is missing or unsafe"
    if not stat.S_ISDIR(info.st_mode):
        return False, "snapshot directory is a link or reparse point"
    if _is_reparse(info):
        return False, "snapshot directory is a link or reparse point" + _REPARSE_HINT
    return True, "safe"


def _safe_regular_file(path: Path, *, strip_mark_of_the_web: bool = False) -> tuple[bool, str]:
    try:
        info = os.lstat(path)
    except OSError:
        return False, f"cannot stat file: {path.name}"
    if not stat.S_ISREG(info.st_mode):
        return False, f"unsafe snapshot entry: {path.name}"
    if _is_reparse(info):
        return False, f"unsafe snapshot entry: {path.name}" + _REPARSE_HINT
    if info.st_nlink != 1:
        return False, f"hard-linked snapshot entry: {path.name}"
    if os.name == "nt":
        try:
            streams = _windows_named_streams_with_sizes(path)
        except OSError:
            return False, f"cannot enumerate data streams: {path.name}"
        foreign = [name for name, _size in streams if _stream_base_name(name) != _ZONE_IDENTIFIER]
        if foreign:
            return False, f"alternate data stream on snapshot entry: {path.name}"
        # A tolerated Zone.Identifier stream is unhashed bytes riding alongside a
        # verified file. A genuine Mark-of-the-Web is a short INI stanza; anything
        # larger is not one, whatever it is named, so it fails like any other
        # stream rather than being tolerated and deleted.
        oversized = [size for _name, size in streams if size > _MAX_ZONE_IDENTIFIER_BYTES]
        if oversized:
            return False, (
                f"alternate data stream on snapshot entry: {path.name} "
                f"(Zone.Identifier is {max(oversized)} bytes, over the "
                f"{_MAX_ZONE_IDENTIFIER_BYTES}-byte limit for a Mark-of-the-Web)"
            )
        if streams and strip_mark_of_the_web:
            # Only reached once the file's own bytes have hashed correctly, so
            # removing the Mark-of-the-Web here cannot mask tampering.
            _strip_zone_identifier(path)
    return True, "safe"


def _stream_base_name(name: str) -> str:
    """``:Zone.Identifier:$DATA`` -> ``Zone.Identifier``."""
    parts = name.split(":")
    return parts[1] if len(parts) > 2 else name.strip(":")


def _strip_zone_identifier(path: Path) -> None:
    """Delete the Mark-of-the-Web stream; never fatal if it cannot be removed."""
    try:
        os.remove(f"{path}:{_ZONE_IDENTIFIER}")
    except OSError as exc:
        _LOG.info(
            "could not remove Mark-of-the-Web stream from %s: %s",
            path.name,
            exc,
        )
        return
    _LOG.info(
        "removed the Mark-of-the-Web (Zone.Identifier) stream from %s after "
        "its SHA-256 verified; the file was extracted from a downloaded archive",
        path.name,
    )


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _windows_named_streams(path: Path) -> tuple[str, ...]:
    """Enumerate NTFS stream *names*; only the unnamed ``::$DATA`` stream is valid."""
    return tuple(name for name, _size in _windows_named_streams_with_sizes(path))


def _windows_named_streams_with_sizes(path: Path) -> tuple[tuple[str, int], ...]:
    """Enumerate NTFS streams as ``(name, size)``, excluding the unnamed stream.

    The size matters: a tolerated ``Zone.Identifier`` stream is bytes that no
    manifest hash covers, so it must be bounded before it is tolerated.
    """
    if os.name != "nt":
        return ()
    import ctypes
    from ctypes import wintypes

    class StreamData(ctypes.Structure):
        _fields_ = [("size", ctypes.c_longlong), ("name", wintypes.WCHAR * 296)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    first = kernel32.FindFirstStreamW
    first.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(StreamData), wintypes.DWORD]
    first.restype = wintypes.HANDLE
    nxt = kernel32.FindNextStreamW
    nxt.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    nxt.restype = wintypes.BOOL
    close = kernel32.FindClose
    close.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    data = StreamData()
    handle = first(str(path), 0, ctypes.byref(data), 0)
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "FindFirstStreamW failed")
    found: list[tuple[str, int]] = []
    try:
        while True:
            found.append((data.name, int(data.size)))
            if not nxt(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error == 38:  # ERROR_HANDLE_EOF
                    break
                raise OSError(error, "FindNextStreamW failed")
    finally:
        close(handle)
    return tuple((name, size) for name, size in found if name != "::$DATA")


def _open_bound_read(path: Path) -> int:
    """Open without following links; Windows denies write/delete sharing."""
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 0x1, None, 3, 0x00200000 | 0x08000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _hash_bound_file(path: Path) -> tuple[str, int]:
    before_path = os.lstat(path)
    fd = _open_bound_read(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("not an ordinary single-link file")
        if (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino):
            raise ValueError("file changed during open")
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(fd)
        after_path = os.lstat(path)
        identity = (before.st_dev, before.st_ino, before.st_size)
        if identity != (after.st_dev, after.st_ino, after.st_size) or identity != (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
        ):
            raise ValueError("file changed during verification")
        return digest.hexdigest(), before.st_size
    finally:
        os.close(fd)


@contextlib.contextmanager
def verified_snapshot_lock(path: Path, model_id: str):
    """Hold verified file handles while a model loader reopens the snapshot.

    On Windows the handles deny write/delete sharing, closing the demonstrated
    verify-to-loader replacement window. POSIX handles provide identity checks
    but cannot prevent a same-user rename; packaged storage permissions remain
    part of that platform's boundary.
    """
    manifest = pinned_model_manifest(model_id)
    if manifest is None:
        yield path
        return
    safe, detail = _safe_directory(path)
    if not safe:
        raise ModelUnavailableError(f"Snapshot failed verification: {detail}")
    opened: list[tuple[str, int, os.stat_result]] = []
    try:
        for name in manifest["files"]:
            source = path / name
            path_info = os.lstat(source)
            fd = _open_bound_read(source)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino, info.st_size) != (
                path_info.st_dev,
                path_info.st_ino,
                path_info.st_size,
            ):
                os.close(fd)
                raise ModelUnavailableError(f"Snapshot changed while locking: {name}")
            opened.append((name, fd, info))
        for name, fd, info in opened:
            expected = manifest["files"][name]
            os.lseek(fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while block := os.read(fd, 1024 * 1024):
                digest.update(block)
            current = os.fstat(fd)
            current_path = os.lstat(path / name)
            identity = (info.st_dev, info.st_ino, info.st_size)
            if identity != (current.st_dev, current.st_ino, current.st_size) or identity != (
                current_path.st_dev,
                current_path.st_ino,
                current_path.st_size,
            ):
                raise ModelUnavailableError(f"Snapshot changed while locking: {name}")
            if digest.hexdigest() != expected["sha256"] or info.st_size != expected["size"]:
                raise ModelUnavailableError(f"Snapshot bytes changed while locking: {name}")
            safe, detail = _safe_regular_file(path / name)
            if not safe:
                raise ModelUnavailableError(detail)
        if {entry.name for entry in path.iterdir()} != set(manifest["files"]):
            raise ModelUnavailableError("Snapshot entries changed while locking")
        yield path
    finally:
        for _name, fd, _info in opened:
            os.close(fd)


def bundled_model_root() -> Path:
    """Model root beside the frozen executable or repository checkout."""
    return paths.bundled_models_dir()


def _huggingface_snapshot(model_id: str, revision: str | None = None) -> Path | None:
    """Find an already-cached snapshot by filesystem only; never contact Hub."""
    cache = os.environ.get("HF_HUB_CACHE", "").strip()
    cache_root = (
        Path(cache).expanduser() if cache else Path.home() / ".cache" / "huggingface" / "hub"
    )
    repository = cache_root / f"models--{safe_model_dir_name(model_id)}"
    snapshots = repository / "snapshots"
    if revision:
        candidate = snapshots / revision
        return candidate if candidate.is_dir() else None
    if not snapshots.is_dir():
        return None
    candidates = sorted(
        (entry for entry in snapshots.iterdir() if entry.is_dir()),
        key=lambda entry: entry.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def pinned_huggingface_snapshot(model_id: str) -> Path | None:
    """Return the exact pinned local Hub snapshot, without any network call."""
    manifest = pinned_model_manifest(model_id)
    if manifest is None:
        return None
    return _huggingface_snapshot(model_id, str(manifest["revision"]))


def resolve_faster_whisper_model(model_reference: str, *, root: Path | None = None) -> str:
    """Resolve to local weights only; never preserve an online model alias."""
    reference = _model_name_from_reference(model_reference)
    explicit = Path(reference).expanduser()
    if (explicit.is_absolute() or reference.startswith(".")) and valid_faster_whisper_snapshot(
        explicit
    ):
        return str(explicit.resolve())

    canonical = canonical_model_id(reference)
    candidates = [runtime_model_path(canonical, root=root)]
    if root is None:
        candidates.append(runtime_model_path(canonical, root=bundled_model_root()))
    manifest = pinned_model_manifest(canonical)
    if root is None:
        cached = _huggingface_snapshot(
            canonical,
            str(manifest["revision"]) if manifest is not None else None,
        )
        if cached is not None:
            candidates.append(cached)

    failures: list[str] = []
    for candidate in candidates:
        if manifest is not None:
            valid, detail = verify_pinned_snapshot(candidate, canonical)
        else:
            valid = valid_faster_whisper_snapshot(candidate)
            detail = "incomplete local snapshot"
        if valid:
            return str(candidate.resolve())
        if candidate.exists():
            failures.append(f"{candidate}: {detail}")
    reason = f" Corrupt candidates: {'; '.join(failures)}." if failures else ""
    raise ModelUnavailableError(
        f"Local Faster Whisper model {canonical!r} is unavailable.{reason} "
        "DCENT Voice never downloads speech models during dictation. Reinstall "
        "the complete package or explicitly build and install the verified offline "
        "model bundle with scripts/download_models.py."
    )


def faster_whisper_model_status(model_reference: str) -> dict[str, Any]:
    """Readiness record for Settings/capabilities without network access."""
    canonical = canonical_model_id(model_reference)
    manifest = pinned_model_manifest(canonical)
    try:
        path = resolve_faster_whisper_model(model_reference)
    except ModelUnavailableError as exc:
        return {
            "ready": False,
            "model_id": canonical,
            "revision": None if manifest is None else manifest["revision"],
            "path": None,
            "detail": str(exc),
        }
    return {
        "ready": True,
        "model_id": canonical,
        "revision": None if manifest is None else manifest["revision"],
        "path": path,
        "detail": "Verified local snapshot ready" if manifest else "Local snapshot ready",
    }


def verify_shipped_payload(payload: Path) -> None:
    """Require a bootable PyInstaller runtime and both exact model snapshots."""
    for component in (payload, payload / "models"):
        safe, detail = _safe_directory(component)
        if not safe:
            raise ModelUnavailableError(f"Shipped payload path is unsafe: {detail}")
    _verify_runtime_payload(payload)
    snapshots = {
        PINNED_PARAKEET_MODEL_ID: payload / "models" / "parakeet-tdt-0.6b-v3",
        PINNED_BASE_MODEL_ID: runtime_model_path(PINNED_BASE_MODEL_ID, root=payload / "models"),
    }
    failures: list[str] = []
    for model_id, path in snapshots.items():
        valid, detail = verify_pinned_snapshot(path, model_id)
        if not valid:
            failures.append(f"{model_id}: {detail}")
    if failures:
        raise ModelUnavailableError(
            "Shipped model payload is incomplete or unsafe: " + "; ".join(failures)
        )


def _verify_runtime_payload(payload: Path) -> None:
    """Reject model-complete bundles whose frozen application cannot boot."""
    windows_entry = payload / "dcent-voice.exe"
    posix_entry = payload / "dcent-voice"
    if windows_entry.exists() or windows_entry.is_symlink():
        entrypoint = windows_entry
        python_candidates = tuple((payload / "_internal").glob("python*.dll"))
        onnx_candidates = tuple((payload / "_internal").glob("onnxruntime/capi/onnxruntime.dll"))
    elif posix_entry.exists() or posix_entry.is_symlink():
        entrypoint = posix_entry
        python_candidates = tuple((payload / "_internal").glob("libpython*.so*"))
        onnx_candidates = (
            *(payload / "_internal").glob("onnxruntime/capi/libonnxruntime.so*"),
            *(payload / "_internal").glob("onnxruntime/capi/onnxruntime_pybind11_state*.so"),
        )
    else:
        raise ModelUnavailableError(
            "Shipped runtime is incomplete: missing dcent-voice executable entrypoint."
        )

    _require_runtime_file(entrypoint, "frozen executable")
    # Shipped twice on purpose: ``_internal`` is what the resolver reads, the
    # payload-root copy is what a person finds when they open the folder.
    # Both are required so neither can silently disappear from a build.
    _require_runtime_file(
        payload / "config.example.toml", "bundled default configuration (payload root)"
    )
    internal = payload / "_internal"
    safe, detail = _safe_directory(internal)
    if not safe:
        raise ModelUnavailableError(f"Shipped runtime is incomplete: {detail}")
    for relative, label in (
        ("base_library.zip", "Python base library archive"),
        ("config.example.toml", "bundled default configuration (_internal)"),
        ("THIRD-PARTY-LICENSES.md", "third-party license inventory"),
        ("onnx_asr/__init__.py", "Parakeet runtime package"),
        (
            "dcent_voice/asr/manifests/faster-whisper-base.json",
            "Faster Whisper model manifest",
        ),
        (
            "dcent_voice/asr/manifests/parakeet-tdt-0.6b-v3.json",
            "Parakeet model manifest",
        ),
    ):
        _require_runtime_file(internal / relative, label)

    if not any(_runtime_candidate_is_safe(path) for path in python_candidates):
        # macOS PyInstaller layouts use a framework binary rather than libpython.so.
        mac_python = tuple(internal.glob("Python.framework/**/Python"))
        if not any(_runtime_candidate_is_safe(path) for path in mac_python):
            raise ModelUnavailableError(
                "Shipped runtime is incomplete: missing a safe Python shared runtime."
            )
    if not any(_runtime_candidate_is_safe(path) for path in onnx_candidates):
        mac_onnx = (
            *internal.glob("onnxruntime/capi/*onnxruntime*.dylib"),
            *internal.glob("onnxruntime/capi/onnxruntime_pybind11_state*.so"),
        )
        if not any(_runtime_candidate_is_safe(path) for path in mac_onnx):
            raise ModelUnavailableError(
                "Shipped runtime is incomplete: missing a safe ONNX Runtime library."
            )

    if entrypoint == windows_entry:
        for relative, label in (
            ("vcruntime140.dll", "VC++ runtime"),
            ("vcruntime140_1.dll", "VC++ runtime (x64 extras)"),
            ("msvcp140.dll", "VC++ C++ runtime"),
            ("ctranslate2/ctranslate2.dll", "CTranslate2 library"),
            (
                "_sounddevice_data/portaudio-binaries/libportaudio64bit.dll",
                "PortAudio library",
            ),
            (
                "webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
                "WebView2 loader",
            ),
        ):
            _require_runtime_file(internal / relative, label)

    bundle_path = payload / "dcent-voice-offline-bundle.json"
    _require_runtime_file(bundle_path, "offline bundle manifest")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        by_id = {
            item.get("modelId"): item for item in bundle.get("models", []) if isinstance(item, dict)
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        raise ModelUnavailableError(
            f"Shipped runtime has an invalid offline bundle manifest: {exc}"
        ) from exc
    for model_id, revision in (
        (PINNED_PARAKEET_MODEL_ID, PINNED_PARAKEET_REVISION),
        (PINNED_BASE_MODEL_ID, PINNED_BASE_REVISION),
    ):
        record = by_id.get(model_id)
        if (
            not isinstance(record, dict)
            or record.get("present") is not True
            or record.get("revision") != revision
        ):
            raise ModelUnavailableError(
                f"Shipped runtime manifest does not declare verified {model_id}@{revision}."
            )


def _require_runtime_file(path: Path, label: str) -> None:
    safe, detail = _safe_regular_file(path)
    if not safe:
        raise ModelUnavailableError(f"Shipped runtime is incomplete ({label}): {detail}")
    if path.stat().st_size <= 0:
        raise ModelUnavailableError(f"Shipped runtime is incomplete: empty {label}.")


def _runtime_candidate_is_safe(path: Path) -> bool:
    safe, _detail = _safe_regular_file(path)
    return safe and path.stat().st_size > 0


def stage_verified_snapshot(source: Path, destination: Path, model_id: str) -> Path:
    """Copy a pinned snapshot from verified open handles into a private tree."""
    manifest = pinned_model_manifest(model_id)
    if manifest is None:
        raise ModelUnavailableError(f"No pinned manifest exists for {model_id}")
    valid, detail = verify_pinned_snapshot(source, model_id)
    if not valid:
        raise ModelUnavailableError(f"Source snapshot failed verification: {detail}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}.sealed-", dir=destination.parent))
    try:
        for name, expected in manifest["files"].items():
            _copy_verified_handle(source / name, temp / name, expected)
        # Re-enumeration detects additions/replacements while the copy ran;
        # staged bytes are independently checked before publication.
        source_valid, source_detail = verify_pinned_snapshot(source, model_id)
        staged_valid, staged_detail = verify_pinned_snapshot(temp, model_id)
        if not source_valid:
            raise ModelUnavailableError(f"Source changed during staging: {source_detail}")
        if not staged_valid:
            raise ModelUnavailableError(f"Staged snapshot failed verification: {staged_detail}")
        _publish_directory(temp, destination)
        return destination
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def stage_verified_payload(source: Path, destination: Path) -> Path:
    """Create a private package tree whose models come only from bound handles."""
    source = Path(os.path.abspath(source))
    verify_shipped_payload(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}.sealed-", dir=destination.parent))
    try:
        _copy_runtime_payload(source, temp)
        stage_verified_snapshot(
            source / "models" / "parakeet-tdt-0.6b-v3",
            temp / "models" / "parakeet-tdt-0.6b-v3",
            PINNED_PARAKEET_MODEL_ID,
        )
        stage_verified_snapshot(
            runtime_model_path(PINNED_BASE_MODEL_ID, root=source / "models"),
            runtime_model_path(PINNED_BASE_MODEL_ID, root=temp / "models"),
            PINNED_BASE_MODEL_ID,
        )
        verify_shipped_payload(temp)
        _publish_directory(temp, destination)
        return destination
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def _copy_runtime_payload(source: Path, destination: Path) -> None:
    """Copy runtime content while excluding release archives only at payload root."""
    for child in source.iterdir():
        if child.name == "models":
            continue
        if child.is_file() and child.suffix.lower() in {".zip", ".rar"}:
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _copy_verified_handle(source: Path, destination: Path, expected: dict[str, Any]) -> None:
    safe, detail = _safe_regular_file(source)
    if not safe:
        raise ModelUnavailableError(detail)
    path_before = os.lstat(source)
    fd = _open_bound_read(source)
    try:
        before = os.fstat(fd)
        identity = (before.st_dev, before.st_ino, before.st_size)
        if before.st_nlink != 1 or identity != (
            path_before.st_dev,
            path_before.st_ino,
            path_before.st_size,
        ):
            raise ModelUnavailableError(f"Source changed before copy: {source.name}")
        digest = hashlib.sha256()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(fd, "rb", closefd=False) as src, destination.open("xb") as dst:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(block)
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(fd)
        after_path = os.lstat(source)
        if identity != (after.st_dev, after.st_ino, after.st_size) or identity != (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
        ):
            raise ModelUnavailableError(f"Source changed during copy: {source.name}")
        if before.st_size != expected.get("size") or digest.hexdigest() != expected.get("sha256"):
            raise ModelUnavailableError(f"Source bytes failed verification: {source.name}")
        safe, detail = _safe_regular_file(source)
        if not safe:
            raise ModelUnavailableError(detail)
        destination.chmod(0o444)
    finally:
        os.close(fd)


def _publish_directory(temp: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}.previous")
    if backup.exists() or backup.is_symlink():
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        else:
            backup.unlink()
    moved_old = False
    try:
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError(f"Refusing unsafe destination: {destination}")
            destination.replace(backup)
            moved_old = True
        temp.replace(destination)
    except Exception:
        if moved_old and not destination.exists() and backup.exists():
            backup.replace(destination)
        raise
    finally:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def install_models_from_bundle(
    manifest_path: Path, *, root: Path | None = None
) -> tuple[InstalledModel, ...]:
    """Validate and copy bundle snapshots into the runtime registry root."""
    manifest_path = manifest_path.resolve()
    bundle_root = manifest_path.parent
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw.get("remoteUrls"):
        raise ValueError("Offline bundle manifest must not contain remote URLs.")

    entries = raw.get("models")
    if not isinstance(entries, list):
        raise ValueError("Offline bundle manifest models must be a list.")

    runtime_root = (root or model_root()).resolve()
    destination_root = faster_whisper_root(root=runtime_root)
    destination_root.mkdir(parents=True, exist_ok=True)
    installed: list[InstalledModel] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider", "")).strip()
        if provider not in {"faster-whisper", "parakeet"}:
            continue
        model_id = str(item.get("modelId", "")).strip()
        relative = str(item.get("path", "")).strip()
        if provider == "parakeet" and not item.get("present"):
            continue
        if not model_id or not relative or not item.get("present"):
            missing = model_id or "<unknown>"
            raise ValueError(f"Offline bundle has an unavailable model entry: {missing}")
        source = _safe_bundle_path(bundle_root, relative)
        if provider == "faster-whisper" and not valid_faster_whisper_snapshot(source):
            raise ValueError(f"Offline model snapshot is incomplete: {model_id}")
        pinned = pinned_model_manifest(model_id)
        if pinned is not None:
            if item.get("revision") != pinned["revision"]:
                raise ValueError(f"Offline model revision mismatch: {model_id}")
            valid, detail = verify_pinned_snapshot(source, model_id)
            if not valid:
                raise ValueError(
                    f"Offline model snapshot failed verification: {model_id}: {detail}"
                )

        if provider == "faster-whisper":
            destination = runtime_model_path(model_id, root=runtime_root)
            if destination.parent != destination_root:
                raise ValueError(f"Model ID escapes the local registry: {model_id!r}")
        else:
            if model_id != PINNED_PARAKEET_MODEL_ID:
                raise ValueError(f"Unsupported Parakeet bundle model: {model_id}")
            destination = runtime_root / "parakeet-tdt-0.6b-v3"
        if pinned is not None:
            stage_verified_snapshot(source, destination, model_id)
        else:
            _replace_snapshot(source, destination)
        if provider == "faster-whisper" and not valid_faster_whisper_snapshot(destination):
            raise ValueError(f"Installed model snapshot failed validation: {model_id}")
        if pinned is not None:
            valid, detail = verify_pinned_snapshot(destination, model_id)
            if not valid:
                raise ValueError(
                    f"Installed model snapshot failed verification: {model_id}: {detail}"
                )
        installed.append(
            InstalledModel(
                provider=provider,
                model_id=model_id,
                path=destination.relative_to(runtime_root).as_posix(),
                installed_at=datetime.now(UTC).isoformat(),
            )
        )

    if not installed:
        raise ValueError("Offline bundle contains no installable speech models.")
    _write_registry(tuple(installed), root=runtime_root)
    return tuple(installed)


def _model_name_from_reference(model_reference: str) -> str:
    reference = str(model_reference).strip()
    if reference.lower().startswith("faster-whisper:"):
        parts = reference.split(":")
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()
    return reference


def _safe_bundle_path(bundle_root: Path, relative: str) -> Path:
    candidate = (bundle_root / relative).resolve()
    try:
        candidate.relative_to(bundle_root)
    except ValueError as exc:
        raise ValueError(f"Offline model path escapes the bundle: {relative}") from exc
    return candidate


def _replace_snapshot(
    source: Path,
    destination: Path,
    *,
    allowed_names: set[str] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        if allowed_names is None:
            shutil.copytree(source, temp, dirs_exist_ok=True)
        else:
            for name in sorted(allowed_names):
                shutil.copy2(source / name, temp / name)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError(f"Refusing to replace unsafe model destination: {destination}")
            shutil.rmtree(destination)
        temp.replace(destination)
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def _write_registry(models: tuple[InstalledModel, ...], *, root: Path | None = None) -> Path:
    registry_root = root or model_root()
    registry_root.mkdir(parents=True, exist_ok=True)
    path = registry_root / REGISTRY_FILENAME
    temp = path.with_suffix(path.suffix + ".tmp")
    payload: dict[str, Any] = {"version": 1, "models": [asdict(model) for model in models]}
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage installed DCENT Voice speech models.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser(
        "install-bundle", help="Install snapshots from an offline bundle."
    )
    install.add_argument("manifest", type=Path)
    verify = subparsers.add_parser(
        "verify-payload", help="Verify every model required by a native payload."
    )
    verify.add_argument("payload", type=Path)
    stage = subparsers.add_parser(
        "stage-payload", help="Create a private package tree with handle-bound models."
    )
    stage.add_argument("source", type=Path)
    stage.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify-payload":
        verify_shipped_payload(args.payload.resolve())
        print("verified")
        return 0
    if args.command == "stage-payload":
        print(stage_verified_payload(args.source, args.destination))
        return 0
    models = install_models_from_bundle(args.manifest)
    for model in models:
        print(f"installed {model.model_id}: {model.path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the installer
    raise SystemExit(main())
