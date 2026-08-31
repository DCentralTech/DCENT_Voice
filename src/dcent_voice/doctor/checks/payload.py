# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Is the installed payload complete, intact and unblocked?

The installer already validates the payload in C#
(``packaging/windows/setup-stub/Program.cs`` ``ValidatePayload``), but it does so
once, at install time, and it throws. Doctor re-runs the same expectations
against the *installed* tree and **reports** every deviation instead of raising,
because the interesting case is a payload that was fine at install and is not
fine now: an antivirus quarantine, a partial copy, a OneDrive placeholder, or a
Mark-of-the-Web stamped by Explorer on a ZIP extraction.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from dcent_voice.util import paths

from ..result import FAIL, PASS, WARN, CheckResult

#: Mirrors ``ValidatePayload`` in ``packaging/windows/setup-stub/Program.cs``.
#: ``tests/test_doctor.py`` asserts the two lists stay in step.
REQUIRED_WINDOWS_FILES: tuple[str, ...] = (
    "dcent-voice.exe",
    "config.example.toml",
    "dcent-voice-offline-bundle.json",
    "_internal/base_library.zip",
    "_internal/python311.dll",
    "_internal/vcruntime140.dll",
    "_internal/vcruntime140_1.dll",
    "_internal/msvcp140.dll",
    "_internal/ctranslate2/ctranslate2.dll",
    "_internal/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll",
    "_internal/webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
    "_internal/config.example.toml",
    "_internal/LICENSE",
    "_internal/README.md",
    "_internal/THIRD-PARTY-LICENSES.md",
    "_internal/THIRD-PARTY-SBOM.cdx.json",
    "_internal/licenses/runtime/CPython-LICENSE.txt",
    "_internal/licenses/runtime/Apache-2.0.txt",
    "_internal/licenses/runtime/SQLite-LICENSE.md",
    "_internal/licenses/runtime/libffi-LICENSE.txt",
    "_internal/licenses/runtime/Microsoft-Visual-Cpp-Runtime-NOTICE.txt",
    "_internal/licenses/runtime/PortAudio-LICENSE.txt",
    "_internal/licenses/runtime/dotnet/LICENSE.txt",
    "_internal/licenses/runtime/dotnet/ThirdPartyNotices.txt",
    "_internal/licenses/fonts/OFL-1.1.txt",
    "_internal/licenses/fonts/PROVENANCE.md",
    "_internal/licenses/models/faster-whisper-model-LICENSE.txt",
    "_internal/licenses/models/CC-BY-4.0.txt",
    "_internal/licenses/models/Parakeet-TDT-0.6B-v3-ATTRIBUTION.txt",
    "_internal/onnx_asr/__init__.py",
    "_internal/onnxruntime/capi/onnxruntime.dll",
    "_internal/dcent_voice/asr/manifests/faster-whisper-base.json",
    "_internal/dcent_voice/asr/manifests/parakeet-tdt-0.6b-v3.json",
)

_NOT_PACKAGED = (
    "not applicable: this is a source checkout, not an installed payload "
    "(run doctor from the installed dcent-voice executable to check the payload)"
)


def run(*, payload_root: Path | None = None, frozen: bool | None = None) -> list[CheckResult]:
    is_frozen = paths.is_frozen() if frozen is None else frozen
    root = payload_root if payload_root is not None else paths.app_dir()
    if not is_frozen and payload_root is None:
        return [
            CheckResult("payload.runtime_files", PASS, _NOT_PACKAGED),
            CheckResult("payload.models", PASS, _NOT_PACKAGED),
            CheckResult("payload.alternate_data_streams", PASS, _NOT_PACKAGED),
        ]
    return [
        check_runtime_files(root),
        check_models(root),
        check_alternate_data_streams(root),
    ]


def check_runtime_files(root: Path) -> CheckResult:
    missing: list[str] = []
    empty: list[str] = []
    unsafe: list[str] = []
    for relative in REQUIRED_WINDOWS_FILES:
        if os.name != "nt" and relative.endswith(".dll"):
            continue
        path = root / relative
        try:
            info = os.lstat(path)
        except OSError:
            missing.append(relative)
            continue
        if getattr(info, "st_file_attributes", 0) & 0x400:
            unsafe.append(f"{relative} (reparse point)")
        elif info.st_size <= 0:
            empty.append(relative)
    data = {
        "payloadRoot": str(root),
        "required": len(REQUIRED_WINDOWS_FILES),
        "missing": missing,
        "empty": empty,
        "unsafe": unsafe,
    }
    problems = [
        *(f"missing: {name}" for name in missing),
        *(f"empty: {name}" for name in empty),
        *unsafe,
    ]
    if problems:
        return CheckResult(
            "payload.runtime_files",
            FAIL,
            f"{len(problems)} required payload file(s) are missing, empty or unsafe under "
            f"{root}: " + "; ".join(problems[:12]),
            "Reinstall DCENT_Voice from the official Setup.exe. If an antivirus removed a "
            "file, restore it from quarantine and add the install folder to the exclusion "
            "list before reinstalling.",
            data,
        )
    return CheckResult(
        "payload.runtime_files",
        PASS,
        f"all {len(REQUIRED_WINDOWS_FILES)} required payload files present under {root}",
        data=data,
    )


def check_models(root: Path) -> CheckResult:
    """Run the shipped-payload verifier and report its verdict rather than raising."""
    from dcent_voice.asr.model_registry import ModelUnavailableError, verify_shipped_payload

    data = {"payloadRoot": str(root), "modelsDir": str(root / "models")}
    try:
        verify_shipped_payload(root)
    except ModelUnavailableError as exc:
        return CheckResult(
            "payload.models",
            FAIL,
            f"the shipped speech-model payload did not verify: {exc}",
            "Reinstall DCENT_Voice from the official Setup.exe (the models are staged by the "
            "installer and are never downloaded at runtime). If the files were extracted from "
            "a ZIP with Explorer, run 'Unblock-File' on them first; if the folder is inside "
            "OneDrive, move the install to a non-synced path.",
            data,
        )
    except OSError as exc:
        return CheckResult(
            "payload.models",
            FAIL,
            f"the shipped model payload could not be read: {exc}",
            "Check that the install folder is readable and not a OneDrive placeholder.",
            data,
        )
    return CheckResult(
        "payload.models",
        PASS,
        "both shipped model snapshots verified (size and SHA-256 match the pinned manifests)",
        data=data,
    )


def check_alternate_data_streams(root: Path) -> CheckResult:
    """Explorer stamps ``Zone.Identifier`` on anything extracted from a download."""
    if os.name != "nt":
        return CheckResult(
            "payload.alternate_data_streams",
            PASS,
            "not applicable: NTFS alternate data streams are a Windows concept",
        )
    from dcent_voice.asr.model_registry import _windows_named_streams

    zone: list[str] = []
    other: list[str] = []
    errors: list[str] = []
    for path in _scanned_files(root):
        try:
            streams = _windows_named_streams(path)
        except OSError as exc:
            errors.append(f"{_relative(path, root)}: {exc}")
            continue
        for name in streams:
            label = f"{_relative(path, root)}{name}"
            if name.split(":")[1:2] == ["Zone.Identifier"]:
                zone.append(label)
            else:
                other.append(label)
    data = {
        "payloadRoot": str(root),
        "zoneIdentifier": zone,
        "otherStreams": other,
        "errors": errors,
    }
    if other:
        return CheckResult(
            "payload.alternate_data_streams",
            FAIL,
            f"{len(other)} payload file(s) carry an unexpected NTFS alternate data stream: "
            + "; ".join(other[:8]),
            "Model verification rejects files with extra data streams. Reinstall from the "
            "official Setup.exe rather than copying the folder.",
            data,
        )
    if zone:
        return CheckResult(
            "payload.alternate_data_streams",
            WARN,
            f"{len(zone)} payload file(s) carry a Mark-of-the-Web (Zone.Identifier) stream, "
            "which Explorer adds to anything extracted from a downloaded ZIP: "
            + "; ".join(zone[:8]),
            "In PowerShell, run: Get-ChildItem -Recurse '"
            + str(root)
            + "' | Unblock-File   — then run doctor again.",
            data,
        )
    return CheckResult(
        "payload.alternate_data_streams",
        PASS,
        "no alternate data streams on the payload files",
        data=data,
    )


def _scanned_files(root: Path) -> list[Path]:
    """Payload-root files plus every model file: bounded, and the ones that matter."""
    found: list[Path] = []
    with contextlib.suppress(OSError):
        found.extend(entry for entry in root.iterdir() if entry.is_file())
    models = root / "models"
    if models.is_dir():
        with contextlib.suppress(OSError):
            found.extend(entry for entry in models.rglob("*") if entry.is_file())
    return found


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
