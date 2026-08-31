# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
# ruff: noqa: F821 - PyInstaller injects Analysis/PYZ/EXE/COLLECT into spec globals.
# PyInstaller one-dir packaging spike for DCENT_Voice.
# Run from repository root with:
#   pyinstaller packaging/DCENT_Voice.spec --noconfirm

import os
import shutil
from pathlib import Path

from PyInstaller.compat import is_darwin, is_linux, is_win
from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path.cwd()

asr_datas = []
for package in ("faster_whisper",):
    asr_datas += collect_data_files(package)
# collect_data_files("onnx_asr") alone drops a data-only folder that shadows
# the PYZ package and makes `import onnx_asr` fail in the frozen exe.
onnx_datas, onnx_bins, onnx_hidden = collect_all("onnx_asr")
asr_datas += onnx_datas

asr_binaries = []
for package in ("ctranslate2", "onnxruntime", "tokenizers"):
    asr_binaries += collect_dynamic_libs(package)
asr_binaries += onnx_bins

# uiautomation is Windows-only and is absent from native macOS/Linux build
# environments because pyproject uses platform markers. Never ask PyInstaller
# to collect it on those hosts.
windows_ui_binaries = (
    collect_dynamic_libs("uiautomation", destdir="uiautomation/bin") if is_win else []
)
if windows_ui_binaries:
    # PyInstaller's ctypes scanner resolves bare DLL names against PATH even
    # when they are already listed in ``binaries``.  Expose the source folder
    # during analysis so the required-via-ctypes check is accurate as well.
    ui_bin_dir = str(Path(windows_ui_binaries[0][0]).parent)
    os.environ["PATH"] = ui_bin_dir + os.pathsep + os.environ.get("PATH", "")

# ``dcent_voice.app`` resolves its heavy submodules lazily (see the
# ``_LAZY_IMPORTS`` table there) so a broken native DLL cannot kill
# ``--version`` / ``--print-config`` / ``doctor`` in the windowed build. That
# hides them from PyInstaller's static analysis, so the whole package is
# collected explicitly instead.
app_hiddenimports = collect_submodules("dcent_voice")

asr_hiddenimports = [
    "faster_whisper",
    "faster_whisper.transcribe",
    "faster_whisper.vad",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "onnx_asr",
    "onnx_asr.adapters",
    "onnx_asr.models.nemo",
    "huggingface_hub",
    *collect_submodules("onnx_asr"),
    *onnx_hidden,
]

platform_hiddenimports = []
if is_win:
    platform_hiddenimports += [
        "win32api",
        "win32gui",
        "win32process",
        "uiautomation",
        "webview.platforms.winforms",
    ]
elif is_darwin:
    platform_hiddenimports += [
        "AppKit",
        "Foundation",
        "Quartz",
        "Security",
        "WebKit",
        "webview.platforms.cocoa",
    ]
elif is_linux:
    # pystray/pynput select their Xorg implementation dynamically. pywebview's
    # GTK implementation is likewise loaded by name when the settings window
    # is opened, so static analysis cannot discover these imports.
    platform_hiddenimports += [
        "Xlib",
        "gi",
        "gi.repository.Gdk",
        "gi.repository.Gio",
        "gi.repository.GLib",
        "gi.repository.Gtk",
        "gi.repository.Soup",
        "gi.repository.WebKit2",
        "pystray._xorg",
        "webview.platforms.gtk",
    ]


def _is_project_editable_install_metadata(entry: tuple[str, str, str]) -> bool:
    """Exclude local editable-install provenance from the public artifact."""

    destination = entry[0].replace("\\", "/")
    return destination.startswith("dcent_voice-") and destination.endswith(
        ".dist-info/direct_url.json"
    )


def _is_linux_host_glib(entry: tuple[str, str, str]) -> bool:
    """Keep the host WebKit and its foundational GLib ABI in one family."""

    if not is_linux:
        return False
    destination = Path(entry[0]).name
    return destination in {
        "libgio-2.0.so.0",
        "libglib-2.0.so.0",
        "libgmodule-2.0.so.0",
        "libgobject-2.0.so.0",
    }


a = Analysis(
    [str(ROOT / "src" / "dcent_voice" / "_packaged.py")],
    pathex=[str(ROOT / "src")],
    binaries=[*asr_binaries, *windows_ui_binaries],
    datas=[
        (str(ROOT / "config.example.toml"), "."),
        (str(ROOT / "LICENSE"), "."),
        (str(ROOT / "README.md"), "."),
        (str(ROOT / "THIRD-PARTY-LICENSES.md"), "."),
        (
            str(ROOT / "src" / "dcent_voice" / "asr" / "manifests"),
            "dcent_voice/asr/manifests",
        ),
        (str(ROOT / "src" / "dcent_voice" / "ui" / "web"), "dcent_voice/ui/web"),
        *copy_metadata("pynput"),
        *copy_metadata("pystray"),
        *asr_datas,
    ],
    hiddenimports=[
        "pynput.keyboard",
        "pystray",
        "PIL.Image",
        "PIL.ImageDraw",
        "webview",
        "uvicorn",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.auto",
        "websockets",
        "fastapi",
        "dcent_voice.package_windows",
        *app_hiddenimports,
        *platform_hiddenimports,
        *asr_hiddenimports,
    ],
    hookspath=[str(ROOT / "packaging" / "pyinstaller_hooks")],
    hooksconfig={},
    # DCENT_Voice always hands decoded float32 PCM arrays to faster-whisper.
    # Its optional file-decoder import would otherwise pull PyAV's FFmpeg build
    # (including GPL codecs) into every offline artifact.  The runtime hook
    # provides a fail-closed placeholder for that unused import path.
    runtime_hooks=[str(ROOT / "packaging" / "pyinstaller_hooks" / "no_pyav.py")],
    # fsspec ships a conftest module in its runtime wheel; collecting it pulls
    # the complete pytest stack into production even though the app never uses
    # test tooling. Keep setuptools because PyInstaller's runtime hook imports
    # it for distribution metadata compatibility; Pygments is retained only if
    # another reachable runtime dependency still imports it after this cut.
    excludes=["av", "fsspec.conftest", "pytest", "_pytest", "iniconfig", "pluggy"],
    noarchive=False,
    # Keep LGPL Python packages as replaceable source trees in the one-dir
    # bundle instead of embedding them only in the executable's PYZ archive.
    module_collection_mode={"pynput": "py", "pystray": "py"},
)
# ``importlib.metadata.version("dcent-voice")`` needs the distribution metadata,
# but PEP 610's ``direct_url.json`` records this checkout's local path when the
# development environment is installed editable. It is not runtime metadata and
# must never ship in a public release artifact.
a.datas = [entry for entry in a.datas if not _is_project_editable_install_metadata(entry)]
# The default PortAudio binary supplies MME, DirectSound, WDM/KS and WASAPI.
# The separately bundled ASIO build is never selected by DCENT_Voice and would
# redistribute the Steinberg ASIO SDK under terms the product does not need.
a.binaries = [entry for entry in a.binaries if "-asio." not in entry[0].casefold()]
# WebKitGTK is intentionally supplied by the supported Linux distribution.
# Bundling Jammy's GLib next to Noble's system WebKit creates a mixed ABI graph
# and crashes before the settings window opens. Resolve these four inseparable
# foundation libraries from the host alongside WebKit on every Linux release.
a.binaries = [entry for entry in a.binaries if not _is_linux_host_glib(entry)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dcent-voice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-compressed Python EXEs are a classic antivirus false-positive trigger
    # and save little in a one-dir build — ship uncompressed.
    upx=False,
    # Tray app: no console window. Crashes still land in the user log dir via
    # the excepthook/faulthandler wiring in util.logging.
    console=False,
    # Brand particles + waveform ICO (see scripts/render_brand_icon.py).
    icon=str(ROOT / "packaging" / "dcent-voice.ico") if is_win else None,
    # Per-monitor DPI so frozen hunt-clicks land in the same client
    # coordinates as the python.org interpreter (Google's page box is small).
    manifest=(str(ROOT / "packaging" / "windows" / "dcent-voice.exe.manifest") if is_win else None),
    version=(str(ROOT / "packaging" / "windows" / "dcent-voice-version.txt") if is_win else None),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DCENT_Voice",
)

# PyInstaller one-dir places every data file under ``_internal``. The resolver
# (dcent_voice.util.paths) finds the example config there, but a person who
# opens the install folder must also be able to see and copy it, so ship a
# second copy at the payload root. Both copies are required by the installer's
# ValidatePayload and by model_registry.verify_shipped_payload.
# COLLECT has already written the tree by the time this statement runs; every
# platform (including macOS, whose .app is assembled later by
# scripts/build_macos_app.sh from this same directory) gets the copy here.
_payload_root = Path(DISTPATH) / coll.name
shutil.copy2(ROOT / "config.example.toml", _payload_root / "config.example.toml")
