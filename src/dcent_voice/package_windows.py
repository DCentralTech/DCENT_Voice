# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Windows Setup.exe packing format shared by the build script and tests.

Layout of DCENT_Voice-Setup.exe:

    [native stub bytes][zip payload][SHA-256(payload)][u64 zip length][8-byte magic][0-7 zero pad]

The stub locates the payload from the trailer so a user gets one double-click
installer without Inno Setup. Signing is a separate Authenticode step.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"DCENTSFX"
LENGTH_STRUCT = struct.Struct("<Q")
HASH_SIZE = hashlib.sha256().digest_size
LEGACY_TRAILER_SIZE = LENGTH_STRUCT.size + len(MAGIC)
TRAILER_SIZE = HASH_SIZE + LENGTH_STRUCT.size + len(MAGIC)
MAX_ALIGNMENT_PADDING = 7


def pack_sfx(stub: bytes, payload_zip: bytes) -> bytes:
    if not stub:
        raise ValueError("installer stub is empty")
    if not payload_zip:
        raise ValueError("installer payload is empty")
    digest = hashlib.sha256(payload_zip).digest()
    unaligned = stub + payload_zip + digest + LENGTH_STRUCT.pack(len(payload_zip)) + MAGIC
    return unaligned + (b"\0" * (-len(unaligned) % 8))


def unpack_sfx(blob: bytes) -> tuple[bytes, bytes]:
    trailer_end = _trailer_end(blob)
    if trailer_end < LEGACY_TRAILER_SIZE + 1:
        raise ValueError("installer is too small to contain a payload")
    if trailer_end < TRAILER_SIZE + 1:
        return _unpack_legacy_sfx(blob, trailer_end)
    length_start = trailer_end - len(MAGIC) - LENGTH_STRUCT.size
    digest_start = length_start - HASH_SIZE
    zip_len = LENGTH_STRUCT.unpack(blob[length_start : length_start + LENGTH_STRUCT.size])[0]
    if zip_len <= 0 or zip_len > trailer_end - TRAILER_SIZE:
        raise ValueError("installer payload length is corrupt")
    zip_start = digest_start - zip_len
    if zip_start < 1:
        raise ValueError("installer payload overlaps the stub")
    payload = blob[zip_start : zip_start + zip_len]
    expected_digest = blob[digest_start:length_start]
    if not hmac.compare_digest(hashlib.sha256(payload).digest(), expected_digest):
        # Read-only compatibility for already-built v1 artifacts. New writers
        # and the native installer accept only the SHA-256-bound v2 layout.
        try:
            return _unpack_legacy_sfx(blob, trailer_end)
        except ValueError:
            raise ValueError("installer payload checksum is corrupt") from None
    return blob[:zip_start], payload


def _trailer_end(blob: bytes) -> int:
    for padding in range(MAX_ALIGNMENT_PADDING + 1):
        end = len(blob) - padding
        if padding and blob[end:] != b"\0" * padding:
            continue
        if end >= len(MAGIC) and blob[end - len(MAGIC) : end] == MAGIC:
            return end
    if len(blob) < LEGACY_TRAILER_SIZE + 1:
        return len(blob)
    raise ValueError("installer trailer magic is missing")


def _unpack_legacy_sfx(blob: bytes, trailer_end: int) -> tuple[bytes, bytes]:
    length_start = trailer_end - len(MAGIC) - LENGTH_STRUCT.size
    zip_len = LENGTH_STRUCT.unpack(blob[length_start : length_start + LENGTH_STRUCT.size])[0]
    if zip_len <= 0 or zip_len > trailer_end - LEGACY_TRAILER_SIZE:
        raise ValueError("installer payload length is corrupt")
    zip_start = trailer_end - LEGACY_TRAILER_SIZE - zip_len
    payload = blob[zip_start : zip_start + zip_len]
    if zip_start < 1 or not payload.startswith(b"PK"):
        raise ValueError("legacy installer payload is corrupt")
    return blob[:zip_start], payload


def write_sfx(path: Path, stub: bytes, payload_zip: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pack_sfx(stub, payload_zip))
    return path


def write_sfx_files(path: Path, stub_path: Path, payload_path: Path) -> Path:
    """Write an SFX without loading the release payload into memory."""
    stub_size = stub_path.stat().st_size
    payload_size = payload_path.stat().st_size
    if stub_size <= 0:
        raise ValueError("installer stub is empty")
    if payload_size <= 0:
        raise ValueError("installer payload is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    digest = hashlib.sha256()
    try:
        with temporary.open("xb") as output, stub_path.open("rb") as stub:
            shutil.copyfileobj(stub, output, length=1024 * 1024)
            with payload_path.open("rb") as payload:
                while chunk := payload.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            output.write(digest.digest())
            output.write(LENGTH_STRUCT.pack(payload_size))
            output.write(MAGIC)
            output.write(b"\0" * (-output.tell() % 8))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


@dataclass(frozen=True)
class SilentInstallScore:
    """Silent /S /D extract of the freeze a user would run."""

    dest_exe: str
    dest_size: int
    dest_sha256: str
    onedir_sha256: str
    onedir_match: bool
    returncode: int
    kind: str = "silent_install"


def score_shipped_default_silent_install(
    setup: Path | str,
    onedir_exe: Path | str,
    dest: Path | str,
    *,
    timeout_s: float = 300.0,
) -> SilentInstallScore:
    """Silent /S /D extract. Shipped default silent install lands onedir."""
    if sys.platform != "win32":
        raise RuntimeError("silent install scoring requires Windows")
    setup_path = Path(setup)
    onedir_path = Path(onedir_exe)
    dest_path = Path(dest)
    if not setup_path.is_file():
        raise FileNotFoundError(f"missing Setup.exe: {setup_path}")
    if not onedir_path.is_file():
        raise FileNotFoundError(f"missing onedir exe: {onedir_path}")
    dest_path.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [str(setup_path), "/S", f"/D={dest_path}"],
        check=False,
        timeout=timeout_s,
        creationflags=creationflags,
    )
    dest_exe = dest_path / "dcent-voice.exe"
    if completed.returncode != 0:
        raise RuntimeError(f"silent install failed rc={completed.returncode}")
    if not dest_exe.is_file():
        raise FileNotFoundError(f"silent install missing exe: {dest_exe}")
    dest_bytes = dest_exe.read_bytes()
    onedir_bytes = onedir_path.read_bytes()
    dest_digest = hashlib.sha256(dest_bytes).hexdigest()
    onedir_digest = hashlib.sha256(onedir_bytes).hexdigest()
    if dest_digest != onedir_digest:
        raise RuntimeError("silent install exe does not match onedir")
    return SilentInstallScore(
        dest_exe=str(dest_exe),
        dest_size=len(dest_bytes),
        dest_sha256=dest_digest,
        onedir_sha256=onedir_digest,
        onedir_match=True,
        returncode=int(completed.returncode),
        kind="silent_install",
    )


@dataclass(frozen=True)
class SilentUninstallScore:
    """Silent /S /uninstall /D of a custom dest extract."""

    dest: str
    dest_exe_present_after: bool
    dest_removed: bool
    install_returncode: int
    uninstall_returncode: int
    kind: str = "silent_uninstall"


def score_shipped_default_silent_uninstall(
    setup: Path | str,
    dest: Path | str,
    *,
    timeout_s: float = 300.0,
) -> SilentUninstallScore:
    """Silent /S /uninstall /D. Shipped default silent uninstall removes onedir."""
    if sys.platform != "win32":
        raise RuntimeError("silent uninstall scoring requires Windows")
    setup_path = Path(setup)
    dest_path = Path(dest)
    if not setup_path.is_file():
        raise FileNotFoundError(f"missing Setup.exe: {setup_path}")
    dest_path.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    installed = subprocess.run(
        [str(setup_path), "/S", f"/D={dest_path}"],
        check=False,
        timeout=timeout_s,
        creationflags=creationflags,
    )
    dest_exe = dest_path / "dcent-voice.exe"
    if installed.returncode != 0:
        raise RuntimeError(f"silent uninstall setup failed rc={installed.returncode}")
    if not dest_exe.is_file():
        raise FileNotFoundError(f"silent uninstall missing installed exe: {dest_exe}")
    removed = subprocess.run(
        [str(setup_path), "/S", "/uninstall", f"/D={dest_path}"],
        check=False,
        timeout=timeout_s,
        creationflags=creationflags,
    )
    if removed.returncode != 0:
        raise RuntimeError(f"silent uninstall failed rc={removed.returncode}")
    present = dest_exe.is_file()
    if present:
        raise RuntimeError(f"silent uninstall left exe: {dest_exe}")
    gone = not dest_path.exists()
    return SilentUninstallScore(
        dest=str(dest_path),
        dest_exe_present_after=present,
        dest_removed=gone,
        install_returncode=int(installed.returncode),
        uninstall_returncode=int(removed.returncode),
        kind="silent_uninstall",
    )


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(
            "usage: python -m dcent_voice.package_windows STUB.exe PAYLOAD.zip OUT.exe",
            file=sys.stderr,
        )
        return 2
    stub_path, payload_path, out_path = (Path(item) for item in args)
    write_sfx_files(out_path, stub_path, payload_path)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
