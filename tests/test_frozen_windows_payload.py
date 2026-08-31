# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Guard the frozen Windows tree a friend receives in Setup.exe."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "dist" / "DCENT_Voice"
EXE = FROZEN / "dcent-voice.exe"
INTERNAL = FROZEN / "_internal"

CUDA_NAMES = ("nvcuda.dll", "cublas64", "cudart64", "cublasLt", "nvinfer")

REQUIRED_RELATIVE = (
    "dcent-voice.exe",
    # WS1: shipped twice on purpose. ``_internal`` is what the frozen resolver
    # reads; the payload-root copy is what a person finds in the install folder.
    "config.example.toml",
    "_internal/config.example.toml",
    "_internal/python311.dll",
    "_internal/vcruntime140.dll",
    "_internal/vcruntime140_1.dll",
    "_internal/msvcp140.dll",
    "_internal/ucrtbase.dll",
    "_internal/onnxruntime/capi/onnxruntime.dll",
    "_internal/ctranslate2/ctranslate2.dll",
    "_internal/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll",
    "_internal/webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
    "models/parakeet-tdt-0.6b-v3/encoder-model.int8.onnx",
    "models/faster-whisper/Systran--faster-whisper-base/model.bin",
)


def _pe_dlls(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise AssertionError(f"{path} is not a PE image")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    coff = e_lfanew + 4
    _machine, num_sections, _ts, _sym, _ns, size_opt, _chars = struct.unpack_from(
        "<HHIIIHH", data, coff
    )
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    pe32plus = magic == 0x20B
    dd_off = opt + (112 if pe32plus else 96)
    import_rva, _import_size = struct.unpack_from("<II", data, dd_off + 8)
    delay_rva, _delay_size = struct.unpack_from("<II", data, dd_off + 13 * 8)
    sec_off = opt + size_opt
    sections: list[tuple[int, int, int, int]] = []
    for index in range(num_sections):
        rec = data[sec_off + index * 40 : sec_off + (index + 1) * 40]
        _vs, va, rs, raw = struct.unpack_from("<IIII", rec, 8)
        sections.append((va, _vs, raw, rs))

    def rva_to_off(rva: int) -> int | None:
        for va, vs, raw, rs in sections:
            if va <= rva < va + max(vs, rs):
                return raw + (rva - va)
        return None

    def read_cstr(rva: int) -> str | None:
        off = rva_to_off(rva)
        if off is None:
            return None
        end = data.find(b"\0", off)
        return data[off:end].decode("ascii", "replace")

    def parse(rva: int, descriptor_size: int, name_off: int) -> tuple[str, ...]:
        if not rva:
            return ()
        off = rva_to_off(rva)
        if off is None:
            return ()
        names: list[str] = []
        index = 0
        while index < 500:
            rec = data[off + index * descriptor_size : off + (index + 1) * descriptor_size]
            if len(rec) < descriptor_size or rec.count(b"\0") == len(rec):
                break
            name_rva = struct.unpack_from("<I", rec, name_off)[0]
            if name_rva == 0 and struct.unpack_from("<I", rec, 0)[0] == 0:
                break
            name = read_cstr(name_rva) if name_rva else None
            if name:
                names.append(name)
            index += 1
        return tuple(names)

    return parse(import_rva, 20, 12), parse(delay_rva, 32, 4) if delay_rva else ()


def _cuda_hits(names: tuple[str, ...]) -> tuple[str, ...]:
    lowered = [name.lower() for name in names]
    return tuple(name for name in lowered if any(token in name for token in CUDA_NAMES))


@pytest.mark.skipif(not EXE.is_file(), reason="frozen Windows payload is not built")
def test_frozen_windows_payload_ships_runtime_and_models() -> None:
    missing = [relative for relative in REQUIRED_RELATIVE if not (FROZEN / relative).is_file()]
    assert missing == []
    from dcent_voice.asr.model_registry import verify_shipped_payload

    verify_shipped_payload(FROZEN)


@pytest.mark.skipif(not EXE.is_file(), reason="frozen Windows payload is not built")
def test_frozen_windows_payload_does_not_require_nvidia_cuda() -> None:
    targets = [
        EXE,
        INTERNAL / "ctranslate2" / "ctranslate2.dll",
        INTERNAL / "onnxruntime" / "capi" / "onnxruntime.dll",
        INTERNAL / "ctranslate2" / "cudnn64_9.dll",
    ]
    for path in targets:
        if not path.is_file():
            continue
        required, delayed = _pe_dlls(path)
        assert _cuda_hits(required) == (), f"{path.name} hard-imports CUDA: {required}"
        # Delay-load CUDA is acceptable; a required NVIDIA driver import is not.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows WebView2 registry")
def test_this_host_has_webview2_or_documents_the_gap() -> None:
    from dcent_voice.ui.webview_runtime import windows_webview2_runtime_present

    # A typical friend PC (Win11 / Win10+Edge) registers WebView2. The probe
    # must not crash; absence is a documented Settings-only gap, not a Setup miss.
    assert windows_webview2_runtime_present() in {True, False}
