# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcent_voice.package_bundle import (
    DEFAULT_MODEL_IDS,
    build_hub_launch_descriptor,
    build_offline_bundle_manifest,
    read_offline_bundle_manifest,
    safe_model_dir_name,
    write_offline_bundle_manifest,
)
from dcent_voice.util.owned_process import start_owned_process, terminate_owned_process
from scripts.download_models import main as download_models_main
from scripts.verify_offline_bundle import verify_manifest as verify_offline_wheels

_SETUP_STUB_LOCK = Path("packaging/windows/setup-stub/.publish.lock")


@contextlib.contextmanager
def _exclusive_file_lock(path: Path, timeout: float = 600.0):
    """Cross-process exclusive lock (msvcrt on Windows, fcntl elsewhere).

    Several agents/terminals routinely run this suite against the same checkout
    at once. `dotnet publish` writes obj/ and bin/ in place, so two concurrent
    publishes collide ("file is being used by another process") and the build
    itself becomes flaky. Serialising the publish makes concurrent runs wait
    instead of fail.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - released in finally
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire {path} within {timeout}s") from None
                time.sleep(0.5)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _publish_setup_stub(dotnet: Path) -> subprocess.CompletedProcess[str]:
    """`dotnet publish` the Windows Setup stub under a cross-process lock."""
    with _exclusive_file_lock(_SETUP_STUB_LOCK):
        return subprocess.run(
            [
                str(dotnet),
                "publish",
                "packaging/windows/setup-stub/DCENT_Voice.Setup.csproj",
                "-c",
                "Release",
                "--nologo",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )


def test_offline_bundle_manifest_records_wheels_models_without_remote_urls(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    wheel_dir = bundle / "wheels"
    model_dir = bundle / "models" / "faster-whisper" / "Systran--faster-whisper-tiny.en"
    wheel_dir.mkdir(parents=True)
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.bin").write_bytes(b"weights")
    (wheel_dir / "dcent_voice-0.1.0-py3-none-any.whl").write_text("wheel", encoding="utf-8")

    manifest = build_offline_bundle_manifest(
        bundle,
        model_ids=("Systran/faster-whisper-tiny.en",),
        created_at=datetime(2026, 7, 6, tzinfo=UTC),
    )
    manifest_path = write_offline_bundle_manifest(bundle, manifest)
    parsed = read_offline_bundle_manifest(manifest_path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert parsed.product == "DCENT_Voice"
    assert set(raw) == {
        "createdAt",
        "modelDir",
        "models",
        "product",
        "remoteUrls",
        "version",
        "wheelDir",
        "wheels",
        "wheelSha256",
    }
    assert parsed.wheels == ("wheels/dcent_voice-0.1.0-py3-none-any.whl",)
    assert dict(parsed.wheel_sha256) == {
        "wheels/dcent_voice-0.1.0-py3-none-any.whl": (
            "ba59926159d2aa256eb8739b8da7e2b574b960e1202c6d624cbe981cef996c91"
        )
    }
    assert parsed.remote_urls == ()
    by_id = {model.model_id: model for model in parsed.models}
    assert by_id["Systran/faster-whisper-tiny.en"].present is True
    assert by_id["istupakov/parakeet-tdt-0.6b-v3-onnx"].provider == "parakeet"
    assert by_id["istupakov/parakeet-tdt-0.6b-v3-onnx"].present is False


def test_offline_wheel_verifier_rejects_tamper_and_unmanifested_wheels(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    wheels = bundle / "wheels"
    wheels.mkdir(parents=True)
    wheel = wheels / "dcent_voice-0.2.0b1-py3-none-any.whl"
    wheel.write_bytes(b"trusted wheel bytes")
    manifest = build_offline_bundle_manifest(bundle, model_ids=())
    manifest_path = write_offline_bundle_manifest(bundle, manifest)

    assert manifest.version == 2
    assert verify_offline_wheels(manifest_path) == (True, "verified 1 offline wheels")

    wheel.write_bytes(b"tampered wheel bytes")
    valid, detail = verify_offline_wheels(manifest_path)
    assert valid is False
    assert "SHA-256 mismatch" in detail

    wheel.write_bytes(b"trusted wheel bytes")
    (wheels / "dependency-1.0-py3-none-any.whl").write_bytes(b"unbound wheel")
    valid, detail = verify_offline_wheels(manifest_path)
    assert valid is False
    assert "closed manifest" in detail


def test_download_models_dry_run_creates_manifest_without_network(tmp_path, capsys) -> None:
    exit_code = download_models_main(
        [
            "--dry-run",
            "--bundle-dir",
            str(tmp_path / "offline"),
            "--models",
            "Systran/faster-whisper-tiny.en",
        ]
    )
    manifest_path = Path(capsys.readouterr().out.strip())
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["remoteUrls"] == []
    models = {item["modelId"]: item for item in payload["models"]}
    assert models["Systran/faster-whisper-tiny.en"] == {
        "provider": "faster-whisper",
        "modelId": "Systran/faster-whisper-tiny.en",
        "path": "models/faster-whisper/Systran--faster-whisper-tiny.en",
        "present": False,
    }
    assert models["istupakov/parakeet-tdt-0.6b-v3-onnx"]["provider"] == "parakeet"
    assert models["istupakov/parakeet-tdt-0.6b-v3-onnx"]["present"] is False


def test_offline_wheel_builder_uses_locked_export_and_supported_downloader(
    monkeypatch, tmp_path
) -> None:
    from scripts import download_models

    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        if command[:2] == ["uv", "export"]:
            output = Path(command[command.index("--output-file") + 1])
            output.write_text("example==1.0 --hash=sha256:" + "0" * 64, encoding="utf-8")
        elif "wheel" in command and command[:2] != ["uv", "build"]:
            destination = Path(command[command.index("--wheel-dir") + 1])
            (destination / "example-1.0-py3-none-any.whl").write_bytes(b"dependency")
        elif command[:2] == ["uv", "build"]:
            destination = Path(command[command.index("--out-dir") + 1])
            if any("av-shim" in item for item in command):
                (destination / "av-18.0.0+dcentshim.1-py3-none-any.whl").write_bytes(b"shim")
            else:
                (destination / "dcent_voice-0.2.0b1-py3-none-any.whl").write_bytes(b"project")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(download_models.subprocess, "run", fake_run)
    download_models.download_wheels(tmp_path)

    assert (tmp_path / "requirements.lock.txt").is_file()
    assert any(command[:2] == ["uv", "export"] and "--locked" in command for command in calls)
    wheel = next(command for command in calls if command[:2] == ["uv", "tool"])
    assert wheel[:6] == ["uv", "tool", "run", "--from", "pip==26.2.1", "pip"]
    assert "--require-hashes" in wheel
    assert "--wheel-dir" in wheel
    assert "--find-links" in wheel
    assert "--no-header" in calls[0]
    assert calls[0][calls[0].index("--no-emit-package") + 1] == "av"
    assert not any(command[:3] == ["uv", "pip", "download"] for command in calls)
    lock = (tmp_path / "requirements.lock.txt").read_text(encoding="utf-8")
    assert "av==18.0.0+dcentshim.1 --hash=sha256:" in lock


def test_offline_av_shim_is_transparent_and_contains_no_codec_libraries(tmp_path) -> None:
    output = tmp_path / "dist"
    subprocess.run(
        [
            "uv",
            "build",
            "packaging/av-shim",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(output),
        ],
        check=True,
        timeout=60,
    )
    wheel = next(output.glob("av-18.0.0+dcentshim.1-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        source = archive.read("av/__init__.py").decode("utf-8")
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    assert "Fail-closed PyAV compatibility shim" in metadata
    assert "pass decoded PCM audio" in source
    assert not any(name.lower().endswith((".dll", ".so", ".dylib")) for name in names)
    assert not any("ffmpeg" in name.lower() or "x264" in name.lower() for name in names)


def test_sdist_excludes_development_eval_assets(tmp_path) -> None:
    subprocess.run(
        [
            "uv",
            "build",
            "--sdist",
            "--no-sources",
            "--out-dir",
            str(tmp_path),
        ],
        check=True,
        timeout=60,
    )
    archive = next(tmp_path.glob("dcent_voice-*.tar.gz"))
    with tarfile.open(archive, "r:gz") as source:
        names = source.getnames()
    assert not any("/eval/" in name for name in names)
    assert any(name.endswith("/src/dcent_voice/app.py") for name in names)
    assert any(name.endswith("/THIRD-PARTY-LICENSES.md") for name in names)


def test_default_dry_run_records_pinned_revision_and_hash_manifest(tmp_path, capsys) -> None:
    assert download_models_main(["--dry-run", "--bundle-dir", str(tmp_path / "offline")]) == 0
    manifest_path = Path(capsys.readouterr().out.strip())
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    model = next(
        item for item in payload["models"] if item["modelId"] == "Systran/faster-whisper-base"
    )
    assert model["revision"] == "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
    assert model["sha256"]["model.bin"] == (
        "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"
    )
    parakeet = next(
        item
        for item in payload["models"]
        if item["modelId"] == "istupakov/parakeet-tdt-0.6b-v3-onnx"
    )
    assert parakeet["revision"] == "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"
    assert parakeet["license"] == "CC-BY-4.0"
    assert parakeet["sha256"]["encoder-model.int8.onnx"] == (
        "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09"
    )


def test_live_model_download_requires_explicit_license_consent(tmp_path) -> None:
    with pytest.raises(SystemExit, match="--accept-model-license"):
        download_models_main(["--bundle-dir", str(tmp_path / "offline")])


def test_live_downloader_pins_revision_and_verifies(monkeypatch, tmp_path) -> None:
    from scripts import download_models

    seen: dict[str, object] = {}

    def snapshot_download(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    monkeypatch.setattr(
        download_models,
        "verify_pinned_snapshot",
        lambda _path, _model_id: (True, "verified"),
    )
    monkeypatch.setattr(download_models, "pinned_huggingface_snapshot", lambda _model_id: None)
    download_models.download_model("Systran/faster-whisper-base", tmp_path / "model")
    assert seen["revision"] == "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
    assert seen["force_download"] is False
    assert set(seen["allow_patterns"]) == {
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    }


def test_live_parakeet_downloader_pins_revision_and_allowlist(monkeypatch, tmp_path) -> None:
    from scripts import download_models

    seen: dict[str, object] = {}
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=lambda **kwargs: seen.update(kwargs)),
    )
    monkeypatch.setattr(
        download_models,
        "verify_pinned_snapshot",
        lambda _path, _model_id: (True, "verified"),
    )
    monkeypatch.setattr(download_models, "pinned_huggingface_snapshot", lambda _model_id: None)
    download_models.download_model("istupakov/parakeet-tdt-0.6b-v3-onnx", tmp_path / "model")
    assert seen["revision"] == "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"
    assert set(seen["allow_patterns"]) == {
        "config.json",
        "decoder_joint-model.int8.onnx",
        "encoder-model.int8.onnx",
        "vocab.txt",
    }


def test_model_downloader_reuses_verified_pinned_cache_without_network(
    monkeypatch, tmp_path
) -> None:
    from scripts import download_models

    cached = tmp_path / "cached"
    target = tmp_path / "target"
    cached.mkdir()
    staged: list[tuple[Path, Path, str]] = []
    monkeypatch.setattr(download_models, "pinned_huggingface_snapshot", lambda _model_id: cached)
    monkeypatch.setattr(
        download_models,
        "verify_pinned_snapshot",
        lambda path, _model_id: (path == cached, "verified"),
    )
    monkeypatch.setattr(
        download_models,
        "stage_verified_snapshot",
        lambda source, destination, model_id: staged.append((source, destination, model_id)),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(
            snapshot_download=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("network path must not run")
            )
        ),
    )

    download_models.download_model("Systran/faster-whisper-base", target)
    assert staged == [(cached, target, "Systran/faster-whisper-base")]


def test_native_builders_require_pinned_multilingual_snapshot() -> None:
    windows = Path("scripts/build_pyinstaller.ps1").read_text(encoding="utf-8")
    unix = Path("scripts/build_pyinstaller.sh").read_text(encoding="utf-8")
    macos = Path("scripts/build_macos_app.sh").read_text(encoding="utf-8")
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    for script in (windows, unix):
        assert "Systran/faster-whisper-base" in script
        assert "istupakov/parakeet-tdt-0.6b-v3-onnx" in script
        assert "--accept-model-license" in script
        assert "verify-payload" in script
    assert "verify-payload" in macos
    assert "dcent_voice/asr/manifests" in spec


def test_native_builders_generate_artifact_derived_sbom_and_full_notices() -> None:
    windows = Path("scripts/build_pyinstaller.ps1").read_text(encoding="utf-8")
    unix = Path("scripts/build_pyinstaller.sh").read_text(encoding="utf-8")
    generator = Path("scripts/generate_release_sbom.py").read_text(encoding="utf-8")
    notices = Path("THIRD-PARTY-LICENSES.md").read_text(encoding="utf-8")
    for script in (windows, unix):
        assert script.index("download_models.py") < script.index("generate_release_sbom.py")
        assert "PYZ-00.toc" in script
    for required in (
        "PyInstaller bootloader and runtime hooks",
        "CPython",
        "OpenSSL",
        "SQLite",
        "libffi",
        "Microsoft Visual C++ and Universal CRT runtime",
        "PortAudio",
        "Barlow Condensed",
        "Inter",
        "JetBrains Mono",
        "Systran/faster-whisper-base",
        "NVIDIA Parakeet TDT 0.6B v3 ONNX",
    ):
        assert required in generator
    assert "embedded distribution has no bundled license evidence" in generator
    assert "THIRD-PARTY-SBOM.cdx.json" in generator
    assert "full SYSTRAN MIT" in notices
    assert "complete CC-BY-4.0" in notices
    for asset in (
        "Apache-2.0.txt",
        "CC-BY-4.0.txt",
        "libffi-LICENSE.txt",
        "SQLite-LICENSE.md",
        "CTranslate2-LICENSE.txt",
        "proxy_tools-LICENSE.txt",
        "faster-whisper-model-LICENSE.txt",
        "Parakeet-TDT-0.6B-v3-ATTRIBUTION.txt",
        "Microsoft-Visual-Cpp-Runtime-NOTICE.txt",
        "PortAudio-LICENSE.txt",
    ):
        path = Path("packaging/licenses") / asset
        assert path.is_file() and path.stat().st_size > 100

    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    assert 'excludes=["av",' in spec
    assert 'runtime_hooks=[str(ROOT / "packaging" / "pyinstaller_hooks" / "no_pyav.py")]' in spec
    assert '"-asio." not in entry[0].casefold()' in spec
    assert "release payload contains unused PyAV/FFmpeg binaries" in generator
    assert "release payload contains the unused PortAudio ASIO binary" in generator
    font_root = Path("src/dcent_voice/ui/web/fonts")
    font_notice = (font_root / "LICENSE.md").read_text(encoding="utf-8")
    assert (font_root / "OFL-1.1.txt").stat().st_size > 4000
    assert "https://fonts.gstatic.com/" in font_notice
    for font in ("Barlow Condensed", "Inter", "JetBrains Mono"):
        assert font in font_notice

    librispeech = Path("eval/librispeech-ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "https://creativecommons.org/licenses/by/4.0/" in librispeech
    assert "resampled" in librispeech and "noise" in librispeech
    assert Path("packaging/licenses/CC-BY-4.0.txt").stat().st_size > 10000
    assert Path("packaging/licenses/CC0-1.0.txt").stat().st_size > 5000
    for fixture in (
        "tests/fixtures/audio/eval/accented/README.md",
        "tests/fixtures/audio/eval/multilingual/README.md",
    ):
        text = Path(fixture).read_text(encoding="utf-8")
        assert "https://creativecommons.org/publicdomain/zero/1.0/" in text
        assert "packaging/licenses/CC0-1.0.txt" in text


def test_setup_sbom_reads_exact_dotnet_runtime_versions(tmp_path) -> None:
    from scripts.generate_release_sbom import _setup_runtime_versions

    assets = tmp_path / "project.assets.json"
    assets.write_text(
        json.dumps(
            {
                "project": {
                    "frameworks": {
                        "net8.0-windows7.0": {
                            "downloadDependencies": [
                                {
                                    "name": "Microsoft.NETCore.App.Runtime.win-x64",
                                    "version": "[8.0.30, 8.0.30]",
                                },
                                {
                                    "name": "Microsoft.WindowsDesktop.App.Runtime.win-x64",
                                    "version": "[8.0.30, 8.0.30]",
                                },
                            ]
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert _setup_runtime_versions(assets) == {
        "Microsoft .NET Runtime win-x64": "8.0.30",
        "Microsoft Windows Desktop Runtime win-x64": "8.0.30",
    }
    build = Path("scripts/build_installer.ps1").read_text(encoding="utf-8")
    assert build.index("stage-payload") < build.index("--setup-dotnet-root")
    assert build.index("--setup-assets") < build.index(
        "verify-payload", build.index("stage-payload")
    )
    assert "ThirdPartyNotices.txt" in Path("packaging/windows/setup-stub/Program.cs").read_text(
        encoding="utf-8"
    )


def test_release_version_is_single_source_and_tag_mismatch_fails_closed() -> None:
    valid = subprocess.run(
        [sys.executable, "scripts/release_version.py", "--check-version", "v0.2.0-beta.1"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout.strip() == "0.2.0b1"
    invalid = subprocess.run(
        [sys.executable, "scripts/release_version.py", "--check-version", "v9.9.9"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert invalid.returncode == 1
    assert "release version mismatch" in invalid.stderr

    resource = Path("packaging/windows/dcent-voice-version.txt").read_text(encoding="utf-8")
    props = Path("packaging/windows/setup-stub/ReleaseVersion.props").read_text(encoding="utf-8")
    csproj = Path("packaging/windows/setup-stub/DCENT_Voice.Setup.csproj").read_text(
        encoding="utf-8"
    )
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    for field, value in (
        ("ProductName", "DCENT_Voice"),
        ("CompanyName", "D-Central Technologies"),
        ("FileVersion", "0.2.0b1"),
        ("ProductVersion", "0.2.0b1"),
        ("OriginalFilename", "dcent-voice.exe"),
    ):
        assert f"StringStruct('{field}', '{value}')" in resource
    assert "<InformationalVersion>0.2.0b1</InformationalVersion>" in props
    assert '<Import Project="ReleaseVersion.props" />' in csproj
    assert "IncludeSourceRevisionInInformationalVersion" in csproj
    assert "dcent-voice-version.txt" in spec
    assert workflow.count("release_version.py --check-version") == 4


def test_release_version_maps_apple_and_debian_prereleases() -> None:
    from scripts.release_version import apple_versions, debian_version

    assert apple_versions("0.2.0b1") == ("0.2.0", "0.2.201")
    assert apple_versions("0.2.0") == ("0.2.0", "0.2.900")
    assert debian_version("0.2.0b1") == "0.2.0~b1"
    assert debian_version("0.2.0") == "0.2.0"

    macos = Path("scripts/build_macos_app.sh").read_text(encoding="utf-8")
    linux = Path("scripts/build_linux_appimage.sh").read_text(encoding="utf-8")
    assert "--format apple-marketing" in macos
    assert "--format apple-build" in macos
    assert "--format debian" in linux
    if shutil.which("dpkg"):
        assert (
            subprocess.run(
                ["dpkg", "--compare-versions", "0.2.0~b1", "lt", "0.2.0"],
                check=False,
            ).returncode
            == 0
        )


def test_release_jobs_require_full_locked_preflight() -> None:
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert release.count("needs: preflight") == 3
    for gate in (
        "uv sync --extra dev --frozen",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv audit",
        "uv run mypy src/dcent_voice",
        "uv run pytest -q",
        "uv build --no-sources",
    ):
        assert gate in release
    assert ci.count("uv sync --extra dev --frozen") >= 2


def test_tagged_release_fails_closed_without_signing_and_notarization() -> None:
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "Require Authenticode credentials for tagged releases" in release
    assert "Tagged Windows releases require Authenticode credentials" in release
    assert release.count("Get-AuthenticodeSignature -LiteralPath") == 2
    assert release.count("SignatureStatus]::Valid") == 2
    assert "Require signing and notarization credentials for tagged releases" in release
    for name in (
        "CERT_P12_BASE64",
        "CERT_PASSWORD",
        "NOTARY_KEY_BASE64",
        "MACOS_SIGNING_IDENTITY",
        "MACOS_NOTARY_KEY_ID",
        "MACOS_NOTARY_ISSUER",
    ):
        assert name in release
    assert "Tagged macOS releases require $name." in release
    assert 'p["signed"] is True' in release
    assert 'p["notarized"] is True' in release
    assert 'p["unsigned"] is False' in release
    assert "otherwise the artifacts remain unsigned" not in release


def test_windows_setup_uses_exact_locked_dotnet_toolchain() -> None:
    global_json = json.loads(Path("global.json").read_text(encoding="utf-8"))
    project = Path("packaging/windows/setup-stub/DCENT_Voice.Setup.csproj").read_text(
        encoding="utf-8"
    )
    lock = json.loads(
        Path("packaging/windows/setup-stub/packages.lock.json").read_text(encoding="utf-8")
    )
    build = Path("scripts/build_installer.ps1").read_text(encoding="utf-8")
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert global_json["sdk"] == {
        "version": "8.0.424",
        "rollForward": "disable",
        "allowPrerelease": False,
    }
    assert "<RuntimeFrameworkVersion>8.0.30</RuntimeFrameworkVersion>" in project
    assert "<RestorePackagesWithLockFile>true</RestorePackagesWithLockFile>" in project
    assert (
        lock["dependencies"]["net8.0-windows7.0"]["Microsoft.NET.ILLink.Tasks"]["resolved"]
        == "8.0.30"
    )
    assert "dotnet restore $stubProj --runtime win-x64 --locked-mode" in build
    assert "dotnet publish $stubProj -c Release --no-restore" in build
    assert 'dotnet-version: "8.0.424"' in release


def test_built_payload_sbom_is_closed_when_artifact_exists() -> None:
    payload = Path("dist/DCENT_Voice")
    toc = Path("build/DCENT_Voice/PYZ-00.toc")
    if not payload.is_dir() or not toc.is_file():
        pytest.skip("PyInstaller artifact not built in this environment")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate_release_sbom.py",
            "--payload",
            str(payload),
            "--toc",
            str(toc),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sbom = json.loads((payload / "_internal/THIRD-PARTY-SBOM.cdx.json").read_text())
    names = {component["name"] for component in sbom["components"]}
    license_paths = [
        str(prop["value"])
        for component in sbom["components"]
        for prop in component.get("properties", [])
        if prop.get("name") == "dcent:license-path"
    ]
    assert license_paths
    assert all(path.startswith("_internal/licenses/") for path in license_paths)
    assert all(not Path(path).is_absolute() for path in license_paths)
    assert all(not re.match(r"^[A-Za-z]:[/\\\\]", path) for path in license_paths)
    assert all(str(Path.cwd()).replace("\\", "/") not in path for path in license_paths)
    assert {
        "PyInstaller bootloader and runtime hooks",
        "CPython",
        "OpenSSL",
        "SQLite",
        "libffi",
        "click",
        "importlib_metadata",
        "tqdm",
        "Systran/faster-whisper-base",
        "NVIDIA Parakeet TDT 0.6B v3 ONNX",
    } <= names


def test_windows_installer_pipeline_is_complete() -> None:
    """One supported Windows installer: the self-contained .NET SFX stub."""
    ps1 = Path("scripts/install_windows.ps1").read_text(encoding="utf-8")
    stub = Path("packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    recovery = Path("packaging/windows/setup-stub/RecoveryCoordinator.cs").read_text(
        encoding="utf-8"
    )
    csproj = Path("packaging/windows/setup-stub/DCENT_Voice.Setup.csproj").read_text(
        encoding="utf-8"
    )
    build = Path("scripts/build_installer.ps1").read_text(encoding="utf-8")
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\DCENT_Voice" in ps1
    assert "UninstallString" in ps1
    assert "DisplayName" in ps1
    assert "DCENTSFX" in stub
    assert "ValidatePayload(stage)" in stub
    assert "File.ReadAllBytes(selfPath)" not in stub
    assert "File.WriteAllBytes(tmpZip" not in stub
    assert "FileOptions.SequentialScan" in stub
    assert "IncrementalHash.CreateHash(HashAlgorithmName.SHA256)" in stub
    assert "CryptographicOperations.FixedTimeEquals" in stub
    assert "GetSignedContentEnd(input)" in stub
    assert "StopProcessesBelowRoot(dest)" in stub
    assert stub.index("WriteUninstall(dest, uninstallCmd, exe)") < stub.index(
        "CleanupBackupDeferred(backup)"
    )
    assert "RollbackInstallTree(dest, backup)" in stub
    assert '"_internal", "base_library.zip"' in stub
    assert "EnsureSupportedWindowsHost()" in stub
    assert "Is64BitOperatingSystem" in stub
    # WS5: the honest floor. pythonnet needs .NET Framework 4.7.2, preinstalled
    # from Windows 10 1809 (build 17763); 1607 shipped a UI that could not load.
    assert "private const int MinimumWindowsBuild = 17763;" in stub
    assert "version.Build < MinimumWindowsBuild" in stub
    assert "14393" not in stub
    assert '"vcruntime140.dll"' in stub
    assert '"libportaudio64bit.dll"' in stub
    assert '"WebView2Loader.dll"' in stub
    assert '"ctranslate2.dll"' in stub
    assert '"_internal", "THIRD-PARTY-SBOM.cdx.json"' in stub
    assert '"licenses", "runtime", "CPython-LICENSE.txt"' in stub
    assert '"licenses", "models", "CC-BY-4.0.txt"' in stub
    assert "SHA256.HashData" in stub
    assert "FileAttributes.ReparsePoint" in stub
    assert "stage-payload" in build
    assert "verify-payload" in build
    assert "stage-payload" in ps1
    assert "verify-payload" in ps1
    assert "/uninstall" in stub
    assert "/purge-user-data" in stub
    assert "/uninstall-cleanup" not in stub
    assert "/S" in stub
    assert "/D=" in stub
    assert "Uninstall.cmd" in stub
    assert "DCENT_Voice.Uninstall.ps1" in stub
    assert "EnableDelayedExpansion" in stub
    assert "DCENT_Voice-Uninstall-!RUNID!.cmd" in stub
    assert "[guid]::NewGuid().ToString('N')" in stub
    assert '\\"!RUNNER!\\" __go \\"%~dp0.\\" \\"!HELPER!\\"' in stub
    assert 'set \\"RC=!ERRORLEVEL!\\"' in stub
    assert '-InstallRoot \\"%INSTALLROOT%\\"' in stub
    assert "$env:DCENT_UNINSTALL_RUNNER" in stub
    assert "exit /b !RC!" in stub
    assert 'start \\"\\" powershell.exe' not in stub
    assert 'start \\"\\" /b powershell.exe' in stub
    assert "DCENTRecoveryUninstaller" in recovery
    assert "timeoutMilliseconds: 120000" in recovery
    assert "WaitForExit(timeoutMilliseconds)" in recovery
    assert 'LogicalName="DCENT_Voice.Uninstall.ps1"' in csproj
    uninstall = Path("packaging/windows/setup-stub/uninstall.ps1").read_text(encoding="utf-8")
    assert "Test-ProcessBelowRoot" in uninstall
    assert "Get-CimInstance Win32_Process" in uninstall
    assert "CloseMainWindow" in uninstall
    assert "$process.Kill()" in uninstall
    assert "[IO.Directory]::Move($target, $tombstone)" in uninstall
    assert "Open-DirectoryIdentity" in uninstall
    assert "Test-SameIdentity" in uninstall
    assert "Open-PinnedEntry" in uninstall
    assert "SetFileInformationByHandle" in uninstall
    assert "GetFileInformationByHandleEx" in uninstall
    assert "FileIdBothDirectoryRestartInfo" in uninstall
    assert "FileDispositionInfoEx" in uninstall
    assert "FILE_SHARE_DELETE" in uninstall
    assert "New-RetainedTree $tombstone $false" in uninstall
    assert "Assert-RetainedTreeMatchesInventory" in uninstall
    assert "Replace-DurableUtf8 $stateFile" in uninstall
    assert "Write-RecoveryRegistration $state" in uninstall
    assert "DCENTRecoveryUninstaller" in uninstall
    assert 'Remove-ItemProperty -LiteralPath $RunRegistryPath -Name "DCENT_Voice"' in uninstall
    assert "Remove-AdeDiscoveryRecords $AdeModulesRoot" in uninstall
    assert "Remove-WindowsCredentials $CredentialService" in uninstall
    assert "if ([bool]$PurgeUserData)" in uninstall
    assert "Remove-Item -LiteralPath $tombstone" not in uninstall
    assert (
        uninstall.index("Remove-RetainedTree $retainedTree $state")
        < uninstall.index("Remove-Item -LiteralPath $ProgramsRoot")
        < uninstall.index("Remove-Item -LiteralPath $RegistryPath")
    )
    assert "asInvoker" in Path("packaging/windows/setup-stub/app.manifest").read_text(
        encoding="utf-8"
    )
    exe_manifest = Path("packaging/windows/dcent-voice.exe.manifest").read_text(encoding="utf-8")
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    assert "PerMonitorV2" in exe_manifest
    assert "dpiAware" in exe_manifest
    assert "dcent-voice.exe.manifest" in spec
    assert "WinExe" in csproj
    assert "package_windows" in build
    assert "DCENT_Voice-Setup.exe" in build
    assert "write_sha256.ps1" in build


def test_windows_release_checksums_cover_post_sign_and_tagged_artifacts(tmp_path) -> None:
    artifact = tmp_path / "DCENT Voice portable.zip"
    artifact.write_bytes(b"release-bytes")
    sidecar = Path(f"{artifact}.sha256")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/write_sha256.ps1",
            "-Path",
            str(artifact),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    import hashlib

    assert sidecar.read_text(encoding="ascii").strip() == (
        f"{hashlib.sha256(b'release-bytes').hexdigest()}  {artifact.name}"
    )

    portable = Path("scripts/build_portable_zip.ps1").read_text(encoding="utf-8")
    signer = Path("scripts/sign_windows.ps1").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'write_sha256.ps1") -Path $Output -Output "$Output.sha256"' in portable
    assert signer.index("Set-AuthenticodeSignature") < signer.index("write_sha256.ps1")
    assert "if (Test-Path -LiteralPath $shaPath)" in signer
    assert workflow.index("Sign Setup.exe") < workflow.index("$setup =")
    assert "./scripts/write_sha256.ps1 -Path $setup" in workflow
    assert "DCENT_Voice-windows-*.zip.sha256" in workflow


def test_inno_pipeline_is_retired_and_unsupported() -> None:
    """The Inno pipeline moved to packaging/legacy/inno and must stay retired."""
    for retired in (
        "packaging/windows/dcent-voice.iss",
        "packaging/windows/verify-installed.ps1",
        "scripts/build_inno_installer.ps1",
    ):
        assert not Path(retired).exists(), f"{retired} came back; there is one installer"

    legacy = Path("packaging/legacy/inno")
    for moved in ("dcent-voice.iss", "verify-installed.ps1", "build_inno_installer.ps1"):
        assert (legacy / moved).is_file(), moved
    readme = (legacy / "README.md").read_text(encoding="utf-8")
    assert "unsupported" in readme.lower()
    assert "setup-stub" in readme

    # Nothing that builds or ships may reference the retired pipeline.
    for live in (
        ".github/workflows/release.yml",
        ".github/workflows/ci.yml",
        "scripts/build_installer.ps1",
        "scripts/install_windows.ps1",
    ):
        text = Path(live).read_text(encoding="utf-8")
        assert "dcent-voice.iss" not in text, live
        assert "build_inno_installer" not in text, live
        assert "verify-installed.ps1" not in text, live

    # PACKAGING.md may name them, but only to say they are retired.
    packaging_doc = Path("docs/PACKAGING.md").read_text(encoding="utf-8")
    assert "packaging/legacy/inno" in packaging_doc
    assert "retired" in packaging_doc.lower()
    assert "one supported Windows installer" in packaging_doc


#: Quoted verbatim in docs/INSTALL_WINDOWS.md, docs/QA_FRESH_MACHINE.md and
#: docs/PACKAGING.md. Change it in all four places or not at all.
PostInstallCheck_NON_ZERO_NOTICE = (
    "Setup will report this as a failed install so scripted deployments notice."
)


def test_setup_runs_doctor_self_check_under_an_owned_job() -> None:
    """The stub proves the install by running it: bounded, isolated, fail-visible."""
    stub = Path("packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    check = Path("packaging/windows/setup-stub/PostInstallCheck.cs").read_text(encoding="utf-8")
    job = Path("packaging/windows/setup-stub/OwnedJob.cs").read_text(encoding="utf-8")

    # Runs after the tree is in place and before the launch prompt.
    assert stub.index("CleanupBackupDeferred(backup)") < stub.index(
        "PostInstallCheck.Run(exe, dest)"
    )
    assert stub.index("PostInstallCheck.Run(exe, dest)") < stub.index("Launch now?")

    # The exact command, and the isolation that keeps the real profile untouched.
    assert "doctor --json" in check
    assert "--no-launch-checks" in check
    assert '["DCENT_VOICE_PROFILE_ROOT"] = evidence' in check
    assert '["DCENT_VOICE_NO_DIALOGS"] = "1"' in check
    assert '["DCENT_VOICE_DISABLE_AUTOSTART"] = "1"' in check
    assert "public const int TimeoutSeconds = 300;" in check

    # Ownership boundary before the first child instruction (ported DcentOwnedJob).
    assert "CreateSuspended | CreateNoWindow" in job
    assert "AssignProcessToJobObject(handle, created.hProcess)" in job
    assert "ResumeThread(created.hThread)" in job
    assert job.index("AssignProcessToJobObject(handle, created.hProcess)") < job.index(
        "ResumeThread(created.hThread)"
    )
    assert "KillOnJobClose" in job
    assert "job.Terminate(124)" in check
    assert "job.StartSuspendedAssigned(exe, arguments, dest, environment)" in check

    # Exit-code contract: 0 pass/warn, 1 failures, anything else could-not-run.
    assert "ExitCode == 0" in check
    assert "ExitCode == 1" in check

    # A host-dependency failure keeps the install and only skips the launch.
    tail = stub[
        stub.index("PostInstallCheck.Run(exe, dest)") : stub.index("        catch (Exception ex)")
    ]
    assert "RollbackInstallTree" not in tail
    assert "Directory.Delete" not in tail
    assert "PostInstallCheck.SilentDiagnostic(check)" in stub
    assert "check.ReportedFailures" in stub

    # ...but Setup must not declare success: exit 3 in BOTH modes, not just /S.
    failure_branch = stub[stub.index("if (!check.Passed)") : stub.index('Launch now?"')]
    assert failure_branch.count("return 3;") == 1
    assert "return 0;" not in failure_branch
    assert failure_branch.index("MessageBox.Show") < failure_branch.index("return 3;")
    assert PostInstallCheck_NON_ZERO_NOTICE in check
    assert check.count("text.Append(NonZeroExitNotice);") == 2

    # On success nothing is left behind; on failure the report is kept as evidence.
    assert "if (outcome.Passed)" in check
    assert "DeleteEvidence(evidence);" in check
    assert 'outcome.ReportPath = "";' in check
    assert "catch (IOException) { }" in check
    assert "catch (UnauthorizedAccessException) { }" in check

    # The child must not inherit an environment that changes what it proves:
    # hub access pinned off, and every proxy variable emptied so doctor's
    # egress result is an offline proof, not a statement about a proxy.
    assert '["DCENT_VOICE_ALLOW_HUB"] = "0"' in check
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        assert f'["{proxy}"] = ""' in check, proxy

    # Untrusted-size inputs are bounded before they reach a MessageBox.
    assert "public const long MaxReportBytes = 4 * 1024 * 1024;" in check
    assert "public const int MaxListedFailures = 8;" in check
    assert "if (file.Length > MaxReportBytes)" in check
    assert "if (outcome.Failures.Count >= MaxListedFailures)" in check
    assert check.count('.Append(" more, see report\\n")') == 2
    assert "public int OmittedFailures" in check

    # CreateProcessW may write into lpCommandLine, so the buffer needs slack.
    assert "new StringBuilder(command, command.Length + 1)" in job


def test_failed_self_check_exit_code_is_documented_where_users_read_it() -> None:
    """The dialog sentence and the exit code must agree across code and docs."""
    for doc in (
        "docs/INSTALL_WINDOWS.md",
        "docs/QA_FRESH_MACHINE.md",
        "docs/PACKAGING.md",
    ):
        text = Path(doc).read_text(encoding="utf-8")
        assert PostInstallCheck_NON_ZERO_NOTICE in text, doc
        assert "3" in text, doc
    packaging_doc = Path("docs/PACKAGING.md").read_text(encoding="utf-8")
    assert "both interactive and silent mode" in packaging_doc
    # The install survives a failed self-check; only the success claim does not.
    assert "Add/Remove Programs registration both stay" in packaging_doc


def test_setup_reports_missing_host_runtimes_without_bundling_them() -> None:
    stub = Path("packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    host = Path("packaging/windows/setup-stub/HostDependencies.cs").read_text(encoding="utf-8")

    # Both hives, both registry views, and the EdgeUpdate 0.0.0.0 stub rejected.
    assert "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in host
    assert "WOW6432Node" in host
    assert "Registry.LocalMachine" in host
    assert "Registry.CurrentUser" in host
    assert '"0.0.0.0"' in host
    assert "public const int DotNetFramework472Release = 461808;" in host
    assert "NET Framework Setup" in host

    # Never fatal, never bundled, and the network step needs an explicit Yes.
    assert "https://go.microsoft.com/fwlink/p/?LinkId=2124703" in host
    assert "Dictation works, but Settings/overlay need Microsoft Edge WebView2." in stub
    assert "Open the Microsoft download page in your browser now?" in stub
    reporter = stub[
        stub.index("private static void ReportHostDependencies") : stub.index(
            "private static void ValidatePayload"
        )
    ]
    assert "throw" not in reporter
    assert reporter.index("MessageBoxButtons.YesNo") < reporter.index(
        "if (answer != DialogResult.Yes)"
    )
    assert reporter.index("if (answer != DialogResult.Yes)") < reporter.index(
        "HostDependencies.WebView2DownloadUrl,\n                UseShellExecute = true,"
    )
    packaging_doc = Path("docs/PACKAGING.md").read_text(encoding="utf-8")
    assert "WebView2" in packaging_doc
    assert "does not bundle" in packaging_doc.lower()


def test_setup_writes_both_start_menu_shortcuts_and_no_startup_folder_entry() -> None:
    stub = Path("packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    assert 'Path.Combine(programs, Product + ".lnk")' in stub
    assert 'Path.Combine(programs, Product + " Diagnostics.lnk")' in stub
    assert '"doctor --open"' in stub
    assert "$s.Arguments=" in stub
    # One autostart mechanism only: the HKCU Run value autostart.py manages.
    # The retired Inno script created a {userstartup} shortcut; nothing may now.
    assert "Startup" not in stub

    from dcent_voice.doctor import start_menu_shortcut_args

    assert start_menu_shortcut_args() == ["doctor", "--open"]

    ps1 = Path("scripts/install_windows.ps1").read_text(encoding="utf-8")
    assert "DCENT_Voice Diagnostics.lnk" in ps1
    assert "doctor --open" in ps1
    assert "Startup" not in ps1


@pytest.mark.skipif(os.name != "nt", reason="native Windows Setup validation contract")
def test_compiled_setup_stub_rejects_payload_missing_base_library(tmp_path) -> None:
    candidates = [
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "dotnet" / "dotnet.exe"),
        shutil.which("dotnet.exe"),
        shutil.which("dotnet"),
    ]
    dotnet = next((Path(item) for item in candidates if item and Path(item).is_file()), None)
    if dotnet is None:
        pytest.skip(".NET 8 SDK is not installed")
    publish = _publish_setup_stub(dotnet)
    assert publish.returncode == 0, publish.stdout + publish.stderr
    stub = Path(
        "packaging/windows/setup-stub/bin/Release/net8.0-windows/win-x64/"
        "publish/DCENT_Voice-Setup.exe"
    ).resolve()
    assert stub.is_file()
    patched = subprocess.run(
        [sys.executable, "scripts/patch_setup_original_filename.py", str(stub)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert patched.returncode == 0, patched.stderr
    version_env = os.environ.copy()
    version_env["DCENT_SETUP_VERSION_PATH"] = str(stub)
    version_result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "(Get-Item -LiteralPath $env:DCENT_SETUP_VERSION_PATH).VersionInfo "
            "| ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=version_env,
    )
    assert version_result.returncode == 0, version_result.stderr
    version_info = json.loads(version_result.stdout)
    assert version_info["ProductName"] == "DCENT_Voice"
    assert version_info["CompanyName"] == "D-Central Technologies"
    assert version_info["FileVersion"] == "0.2.0.1"
    assert version_info["ProductVersion"] == "0.2.0b1"
    assert version_info["OriginalFilename"] == "DCENT_Voice-Setup.exe"
    broken = tmp_path / "broken-payload"
    broken.mkdir()
    (broken / "dcent-voice.exe").write_bytes(b"MZ")
    result = subprocess.run(
        [str(stub), "/S", "--validate-payload", str(broken)],
        timeout=30,
        check=False,
    )
    assert result.returncode == 1


@pytest.mark.skipif(os.name != "nt", reason="native Windows Setup memory contract")
def test_setup_stub_streams_large_sfx_with_bounded_working_set(tmp_path) -> None:
    import ctypes
    import time
    from ctypes import wintypes

    from dcent_voice.package_windows import write_sfx_files

    dotnet = Path(os.environ.get("LOCALAPPDATA", "")) / "dotnet" / "dotnet.exe"
    if not dotnet.is_file():
        pytest.skip(".NET 8 SDK is not installed")
    publish = _publish_setup_stub(dotnet)
    assert publish.returncode == 0, publish.stdout + publish.stderr
    stub = Path(
        "packaging/windows/setup-stub/bin/Release/net8.0-windows/win-x64/"
        "publish/DCENT_Voice-Setup.exe"
    ).resolve()
    patched = subprocess.run(
        [sys.executable, "scripts/patch_setup_original_filename.py", str(stub)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert patched.returncode == 0, patched.stderr
    payload = tmp_path / "large-payload.zip"
    with payload.open("wb") as stream:
        stream.write(b"PK")
        stream.seek(256 * 1024 * 1024 - 1)
        stream.write(b"\0")
    setup = write_sfx_files(tmp_path / "large-setup.exe", stub, payload)

    process = start_owned_process([str(setup), "/S", "--verify-sfx"])

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    handle = ctypes.windll.kernel32.OpenProcess(0x0410, False, process.pid)
    assert handle
    peak = 0
    deadline = time.monotonic() + 30.0
    timed_out = False
    try:
        while process.poll() is None and time.monotonic() < deadline:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                peak = max(peak, counters.peak_working_set_size)
            time.sleep(0.01)
        timed_out = process.poll() is None
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
        terminate_owned_process(process, grace_s=0.0, kill_s=5.0)
    assert timed_out is False, "bounded SFX verification exceeded 30 seconds"
    assert process.returncode == 0
    assert peak > 0
    assert peak < 192 * 1024 * 1024, f"Setup peak working set was {peak / 1024 / 1024:.1f} MiB"


def test_setup_original_filename_patcher_changes_only_version_fields(tmp_path) -> None:
    source = "DCENT_Voice-Setup.dll".encode("utf-16le")
    target = tmp_path / "stub.exe"
    target.write_bytes(
        "InternalName".encode("utf-16le")
        + b"\0\0"
        + source
        + b"\0\0padding"
        + "OriginalFilename".encode("utf-16le")
        + b"\0\0"
        + source
    )
    result = subprocess.run(
        [sys.executable, "scripts/patch_setup_original_filename.py", str(target)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    updated = target.read_bytes()
    assert updated.count("DCENT_Voice-Setup.dll".encode("utf-16le")) == 1
    assert updated.count("DCENT_Voice-Setup.exe".encode("utf-16le")) == 1


@pytest.mark.skipif(os.name != "nt", reason="native Authenticode SFX structure")
def test_self_signed_sfx_keeps_aligned_trailer_verifiable(tmp_path) -> None:
    from dcent_voice.package_windows import write_sfx_files

    dotnet = Path(os.environ.get("LOCALAPPDATA", "")) / "dotnet" / "dotnet.exe"
    if not dotnet.is_file():
        pytest.skip(".NET 8 SDK is not installed")
    publish = _publish_setup_stub(dotnet)
    assert publish.returncode == 0, publish.stdout + publish.stderr
    stub = Path(
        "packaging/windows/setup-stub/bin/Release/net8.0-windows/win-x64/"
        "publish/DCENT_Voice-Setup.exe"
    ).resolve()
    subprocess.run(
        [sys.executable, "scripts/patch_setup_original_filename.py", str(stub)],
        timeout=30,
        check=True,
    )
    payload = tmp_path / "payload.zip"
    payload.write_bytes(b"PK\x03\x04signed structural probe")
    setup = write_sfx_files(tmp_path / "signed-setup.exe", stub, payload)
    assert setup.stat().st_size % 8 == 0
    sign_env = os.environ.copy()
    sign_env["DCENT_SIGN_TEST_PATH"] = str(setup)
    sign_script = r"""
$rsa = [Security.Cryptography.RSA]::Create(2048)
$request = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
  "CN=DCENT Voice Ephemeral Test",
  $rsa,
  [Security.Cryptography.HashAlgorithmName]::SHA256,
  [Security.Cryptography.RSASignaturePadding]::Pkcs1)
$oids = [Security.Cryptography.OidCollection]::new()
[void]$oids.Add([Security.Cryptography.Oid]::new("1.3.6.1.5.5.7.3.3"))
$request.CertificateExtensions.Add(
  [Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($oids, $false))
$certificate = $request.CreateSelfSigned(
  [DateTimeOffset]::UtcNow.AddMinutes(-1), [DateTimeOffset]::UtcNow.AddMinutes(10))
$signature = Set-AuthenticodeSignature -LiteralPath $env:DCENT_SIGN_TEST_PATH `
  -Certificate $certificate -HashAlgorithm SHA256
if ($null -eq $signature.SignerCertificate) { exit 1 }
"""
    signed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", sign_script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=sign_env,
    )
    assert signed.returncode == 0, signed.stdout + signed.stderr
    verified = subprocess.run(
        [
            sys.executable,
            "scripts/run_bounded.py",
            "--timeout",
            "60",
            "--",
            str(setup),
            "/S",
            "--verify-sfx",
        ],
        capture_output=True,
        text=True,
        timeout=70,
        check=False,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_windows_tree_installer_verifies_before_writing_shell_state() -> None:
    script = Path("scripts/install_windows.ps1").read_text(encoding="utf-8")
    stage = script.index('-Arguments @("stage-payload", $Source, $Destination)')
    stage_timeout = script.index("-TimeoutSeconds 900", stage)
    verify = script.index('-Arguments @("verify-payload", $Destination)', stage_timeout)
    verify_timeout = script.index("-TimeoutSeconds 300", verify)
    shortcut = script.index("$shell.CreateShortcut", verify_timeout)
    registry = script.index("New-Item -Path $uninstallKey", shortcut)
    assert stage < stage_timeout < verify < verify_timeout < shortcut < registry


def test_pack_sfx_round_trip_and_rejects_corrupt_trailer() -> None:
    from dcent_voice.package_windows import MAGIC, pack_sfx, unpack_sfx

    stub = b"MZ" + b"stub-bytes"
    payload = b"PK\x03\x04payload-zip"
    blob = pack_sfx(stub, payload)
    got_stub, got_payload = unpack_sfx(blob)
    assert got_stub == stub
    assert got_payload == payload
    assert blob.rstrip(b"\0").endswith(MAGIC)
    assert blob.startswith(b"MZ")
    assert len(blob) % 8 == 0

    tampered = bytearray(blob)
    tampered[len(stub) + 4] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        unpack_sfx(bytes(tampered))

    try:
        unpack_sfx(b"too-small")
    except ValueError as exc:
        assert "too small" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")

    try:
        unpack_sfx(stub + payload + b"\x00" * 8 + b"NOTMAGIC")
    except ValueError as exc:
        assert "magic" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_write_sfx_cli_writes_pe_with_trailer(tmp_path) -> None:
    from dcent_voice.package_windows import MAGIC, unpack_sfx
    from dcent_voice.package_windows import main as package_main

    stub = tmp_path / "stub.exe"
    payload = tmp_path / "payload.zip"
    out = tmp_path / "DCENT_Voice-Setup.exe"
    stub.write_bytes(b"MZSTUB")
    payload.write_bytes(b"PKZIPDATA")
    assert package_main([str(stub), str(payload), str(out)]) == 0
    blob = out.read_bytes()
    got_stub, got_payload = unpack_sfx(blob)
    assert got_stub == b"MZSTUB"
    assert got_payload == b"PKZIPDATA"
    assert blob.rstrip(b"\0").endswith(MAGIC)


def test_built_setup_exe_is_pe_with_dcent_trailer() -> None:
    """If the real installer was built, it must be a PE + DCENTSFX payload."""
    from dcent_voice.package_windows import MAGIC, unpack_sfx

    setup = Path("dist/DCENT_Voice-Setup.exe")
    if not setup.is_file():
        pytest.skip("Setup.exe not built in this environment")
    blob = setup.read_bytes()
    assert blob[:2] == b"MZ"
    assert blob.rstrip(b"\0").endswith(MAGIC)
    stub, payload = unpack_sfx(blob)
    assert stub.startswith(b"MZ")
    assert payload[:2] == b"PK"
    assert len(payload) > 1024
    sha = Path("dist/DCENT_Voice-Setup.exe.sha256")
    if sha.is_file():
        recorded = sha.read_text(encoding="ascii").split()[0].lower()
        import hashlib

        assert hashlib.sha256(blob).hexdigest() == recorded
    from io import BytesIO
    from zipfile import ZipFile

    names = ZipFile(BytesIO(payload)).namelist()
    assert any(name.replace("\\", "/").endswith("dcent-voice.exe") for name in names)
    assert any("onnx_asr" in name.replace("\\", "/") for name in names)
    assert any(name.replace("\\", "/").endswith("encoder-model.int8.onnx") for name in names)
    example = next(
        name for name in names if name.replace("\\", "/").endswith("config.example.toml")
    )
    text = ZipFile(BytesIO(payload)).read(example).decode("utf-8")
    assert 'asr = "parakeet:tdt-0.6b-v3:int8"' in text


def test_hub_launch_descriptor_matches_packaged_json() -> None:
    descriptor = build_hub_launch_descriptor()
    packaged = json.loads(Path("packaging/launch.json").read_text(encoding="utf-8"))

    assert packaged == descriptor
    assert descriptor["sovereigntyClass"] == "LOCAL"
    assert descriptor["args"][:2] == ["-m", "dcent_voice"]
    assert descriptor["fakeAudioEnv"]["DCENT_VOICE_FAKE_AUDIO"] == "1"
    assert "stt.final" in descriptor["capabilities"]
    assert descriptor["capabilities"] == ["stt.partial", "stt.final", "module.sovereignty"]


def test_install_script_supports_offline_uv_no_index_flow() -> None:
    script = Path("scripts/install.ps1").read_text(encoding="utf-8")

    assert "[switch]$Offline" in script
    assert "[IO.Path]::IsPathRooted($InstallDir)" in script
    assert "[IO.Path]::GetFullPath($InstallDir)" in script
    assert "dcent-voice-offline-bundle.json" in script
    assert "--no-index" in script
    assert "--find-links" in script
    assert "remoteUrls" in script
    assert "verify_offline_bundle.py" in script
    assert script.index("verify_offline_bundle.py") < script.index("uv pip install")
    assert "uv pip install --python $VenvPath --offline --no-index" in script
    assert 'uv python find ">=3.11"' in script
    assert "Offline package installation failed" in script
    assert "dcent_voice.asr.model_registry install-bundle" in script


def test_default_bundle_models_cover_shipped_multilingual_fallback() -> None:
    """The default payload provisions its actual automatic fallback exactly."""
    from dcent_voice.asr.model_registry import canonical_model_id
    from dcent_voice.config import load_config

    config = load_config(Path("config.example.toml"), create=False)
    assert config.current_profile.asr.provider == "parakeet"
    assert config.current_profile.asr.model == "tdt-0.6b-v3"
    assert canonical_model_id("base") == "Systran/faster-whisper-base"
    assert DEFAULT_MODEL_IDS == ("Systran/faster-whisper-base",)
    # Bundle stays lean: no multi-GB accurate model by default.
    assert "Systran/faster-whisper-large-v3" not in DEFAULT_MODEL_IDS


def test_safe_model_dir_name_is_windows_friendly() -> None:
    assert safe_model_dir_name("Systran/faster-whisper-small.en") == (
        "Systran--faster-whisper-small.en"
    )


def test_windows_package_collects_uiautomation_bridge_dlls() -> None:
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")

    assert 'collect_dynamic_libs("uiautomation", destdir="uiautomation/bin")' in spec
    assert "*windows_ui_binaries" in spec
    assert 'os.environ["PATH"] = ui_bin_dir' in spec


def test_pyinstaller_spec_keeps_lgpl_packages_replaceable() -> None:
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")

    assert 'module_collection_mode={"pynput": "py", "pystray": "py"}' in spec
    assert '*copy_metadata("pynput")' in spec
    assert '*copy_metadata("pystray")' in spec
    assert "THIRD-PARTY-LICENSES.md" in spec


def test_pyinstaller_build_script_uses_the_uv_managed_environment() -> None:
    script = Path("scripts/build_pyinstaller.ps1").read_text(encoding="utf-8")

    assert "uv sync --extra dev --frozen" in script
    assert "uv pip install pyinstaller" not in script
    assert "uv run pyinstaller @PyInstallerArgs" in script
    assert '@("packaging/DCENT_Voice.spec", "--clean")' in script
    assert "[IO.FileAttributes]::ReadOnly" in script
    assert "Get-ChildItem -LiteralPath $ExistingPayload -Recurse -Force" in script
    assert "Refusing to normalize attributes outside the release dist directory" in script
    assert "istupakov/parakeet-tdt-0.6b-v3-onnx" in script
    assert "verify-payload" in script
    assert "PyInstaller failed with exit code" in script
    assert "python -m pip" not in script


def test_pyinstaller_release_excludes_editable_install_provenance() -> None:
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    script = Path("scripts/build_pyinstaller.ps1").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "def _is_project_editable_install_metadata" in spec
    assert '".dist-info/direct_url.json"' in spec
    assert "a.datas = [" in spec
    assert "direct_url.json" in script
    assert "dcent_voice-*.dist-info" in script
    assert "./scripts/build_pyinstaller.ps1 -NoConfirm" in workflow
    assert "./scripts/build_installer.ps1" in workflow
    assert "DCENT_Voice-Setup-" in workflow
    portable = Path("scripts/build_portable_zip.ps1").read_text(encoding="utf-8")
    assert "build_portable_zip.ps1" in workflow
    assert "stage-payload" in portable
    assert "verify-payload" in portable


def test_frozen_payload_ships_project_license_readme_and_locked_builder() -> None:
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    stub = Path("packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    unix = Path("scripts/build_pyinstaller.sh").read_text(encoding="utf-8")
    for name in ("LICENSE", "README.md", "THIRD-PARTY-LICENSES.md"):
        assert f'ROOT / "{name}"' in spec
        assert f'"_internal", "{name}"' in stub
    assert '"pyinstaller>=6.11,<7"' in project
    assert "uv pip install pyinstaller" not in unix


def test_linux_packaging_recipe_is_complete() -> None:
    desktop = Path("packaging/linux/dcent-voice.desktop").read_text(encoding="utf-8")
    control = Path("packaging/linux/debian/control").read_text(encoding="utf-8")
    postinst = Path("packaging/linux/debian/postinst").read_text(encoding="utf-8")
    metainfo = Path("packaging/linux/tech.dcentral.dcent-voice.metainfo.xml").read_text(
        encoding="utf-8"
    )
    script = Path("scripts/build_linux_appimage.sh").read_text(encoding="utf-8")
    assert "Name=DCENT Voice" in desktop
    assert "Exec=dcent-voice" in desktop
    assert "Categories=Utility;" in desktop
    assert "Package: dcent-voice" in control
    assert "Maintainer: D-Central Technologies <support@d-central.tech>" in control
    assert "Architecture: amd64" in control
    assert "Recommends: wl-clipboard" in control
    assert "wtype | ydotool" in control
    assert "xclip | xsel, xdotool" in control
    assert "dpkg-statoverride --list" in postinst
    assert 'chmod 0755 "$payload"' in postinst
    assert '[ -L "$payload" ]' in postinst
    assert "clipboard injection helpers are not ready" in postinst
    assert "wl-copy" in postinst and "xdotool" in postinst
    assert "AppDir" in script
    assert "AppImage needs host clipboard helpers" in script
    assert "appimagetool" in script
    assert "dcent-voice.desktop" in script
    assert 'bash "$ROOT/scripts/build_pyinstaller.sh"' in script
    assert "dpkg-deb --root-owner-group --build" in script
    assert "verify-payload" in script
    assert 'verify-payload "$APPDIR/usr/bin"' in script
    assert 'verify-payload "$DEBROOT/opt/dcent-voice"' in script
    assert 'MODEL_ROOT="$SRC/models"' in script
    assert 'find "$MODEL_ROOT" -type d -exec chmod 0755 {} +' in script
    assert 'find "$MODEL_ROOT" -type f -exec chmod 0644 {} +' in script
    assert 'normalize_package_directories "$APPDIR" "AppDir"' in script
    assert 'normalize_package_directories "$DEBROOT" "Debian package tree"' in script
    assert 'find "$tree" -type d -exec chmod 0755 {} +' in script
    assert 'find "$tree" -type d ! -perm 0755 -print -quit' in script
    assert '"$ROOT/packaging/linux/debian/postinst"' in script
    assert 'chmod 0755 "$DEBROOT/DEBIAN/postinst"' in script
    assert "sha256sum" in script
    assert 'sha256sum "$(basename "$APPIMAGE_OUT")"' in script
    assert 'sha256sum "$(basename "$DEB_OUT")"' in script
    assert "AppDir is complete" not in script
    assert "<id>tech.dcentral.dcent-voice</id>" in metainfo
    assert '<launchable type="desktop-id">dcent-voice.desktop</launchable>' in metainfo
    assert "<metadata_license>CC0-1.0</metadata_license>" in metainfo
    assert "@VERSION@" in metainfo
    assert '"$APPDIR/usr/share/metainfo"' in script
    assert '"$DEBROOT/usr/share/metainfo"' in script
    assert script.count("tech.dcentral.dcent-voice.metainfo.xml") >= 4
    assert "--no-appstream" in script
    assert "appstreamcli validate --no-net" in script
    assert script.index('cd "$ROOT"') < script.index('"${BOUNDED[@]}"')

    smoke = Path("scripts/smoke_linux_settings.sh").read_text(encoding="utf-8")
    assert "xvfb-run -a timeout" in smoke
    assert '"$STATUS" -ne 124' in smoke
    assert '"status":"alive_until_timeout"' in smoke


@pytest.mark.skipif(sys.platform != "linux", reason="Linux Debian artifact contract")
def test_linux_deb_artifact_modes_and_non_root_execution() -> None:
    artifact_value = os.environ.get("DCENT_VOICE_LINUX_DEB", "").strip()
    if not artifact_value:
        pytest.skip("set DCENT_VOICE_LINUX_DEB to inspect a built Debian package")
    artifact = Path(artifact_value).resolve()
    assert artifact.is_file(), artifact
    assert shutil.which("dpkg-deb") is not None

    listing = subprocess.run(
        ["dpkg-deb", "--contents", str(artifact)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    directory_modes: dict[str, str] = {}
    for line in listing.splitlines():
        match = re.match(r"^(d\S{9})\s+(\S+)\s+\d+\s+\S+\s+\S+\s+(.+)$", line)
        if match:
            mode, owner, path = match.groups()
            assert owner == "root/root", (path, owner)
            directory_modes[path] = mode

    required = {"./", "./opt/", "./opt/dcent-voice/", "./usr/", "./usr/bin/"}
    assert required <= directory_modes.keys()
    assert set(directory_modes.values()) == {"drwxr-xr-x"}

    control_root = Path(tempfile.mkdtemp(prefix="dcent-voice-control-", dir="/tmp"))
    try:
        subprocess.run(["dpkg-deb", "--control", str(artifact), str(control_root)], check=True)
        postinst = control_root / "postinst"
        assert postinst.stat().st_mode & 0o777 == 0o755
        postinst_source = postinst.read_text(encoding="utf-8")
        assert "dpkg-statoverride --list" in postinst_source
        assert 'chmod 0755 "$payload"' in postinst_source
    finally:
        shutil.rmtree(control_root)

    with tempfile.TemporaryDirectory(prefix="dcent-voice-deb-", dir="/tmp") as root:
        install_root = Path(root)
        install_root.chmod(0o755)
        extracted = install_root / "installed"
        subprocess.run(["dpkg-deb", "--extract", str(artifact), str(extracted)], check=True)
        for path in (extracted, extracted / "opt", extracted / "opt/dcent-voice"):
            assert path.stat().st_mode & 0o777 == 0o755

        runuser = shutil.which("runuser")
        if os.geteuid() != 0 or runuser is None:
            pytest.skip("real non-root frozen execution requires root and runuser")
        result = subprocess.run(
            [
                runuser,
                "-u",
                "nobody",
                "--",
                str(extracted / "opt/dcent-voice/dcent-voice"),
                "--version",
            ],
            capture_output=True,
            check=False,
            cwd="/tmp",
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().startswith("DCENT_Voice ")


def test_linux_appstream_metainfo_is_well_formed_and_launchable() -> None:
    from xml.etree import ElementTree

    source = Path("packaging/linux/tech.dcentral.dcent-voice.metainfo.xml").read_text(
        encoding="utf-8"
    )
    root = ElementTree.fromstring(source.replace("@VERSION@", "0.2.0b1"))

    assert root.tag == "component"
    assert root.attrib == {"type": "desktop-application"}
    assert root.findtext("id") == "tech.dcentral.dcent-voice"
    assert root.findtext("metadata_license") == "CC0-1.0"
    launchable = root.find("launchable")
    assert launchable is not None
    assert launchable.attrib == {"type": "desktop-id"}
    assert launchable.text == "dcent-voice.desktop"
    release = root.find("./releases/release")
    assert release is not None
    assert release.attrib["version"] == "0.2.0b1"


def test_macos_packaging_recipe_is_complete() -> None:
    plist = Path("packaging/macos/Info.plist").read_text(encoding="utf-8")
    script = Path("scripts/build_macos_app.sh").read_text(encoding="utf-8")
    assert "tech.dcentral.dcent-voice" in plist
    assert "NSMicrophoneUsageDescription" in plist
    assert "NSAccessibilityUsageDescription" in plist
    assert "LSUIElement" in plist
    assert "DCENT Voice.app" in script
    assert "codesign" in script
    assert "notarytool" in script
    assert 'bash "$ROOT/scripts/build_pyinstaller.sh"' in script
    assert "hdiutil create" in script
    assert "ditto -c -k" in script
    assert "MACOS_SIGNING_IDENTITY" in script
    assert "MACOS_NOTARY_KEYCHAIN_PROFILE" in script
    assert "producing unsigned CI artifacts" in script
    assert "macos-pipeline-status.json" in script
    assert "--check" in script
    assert "verify-payload" in script
    assert 'verify-payload "$APP/Contents/MacOS"' in script
    assert script.count('verify-payload "$APP/Contents/MacOS"') >= 3
    assert script.index('cd "$ROOT"') < script.index('"${BOUNDED[@]}"')
    assert (
        script.index('notarytool submit "$NOTARY_ZIP"')
        < script.index('stapler staple "$APP"')
        < script.index("hdiutil create")
        < script.index('notarytool submit "$DMG"')
        < script.index('stapler staple "$DMG"')
        < script.index('stapler validate "$MOUNT/DCENT Voice.app"')
    )


def test_unix_payload_builder_bundles_shipped_default_model() -> None:
    script = Path("scripts/build_pyinstaller.sh").read_text(encoding="utf-8")

    assert "uv sync --extra dev --frozen" in script
    assert "uv run pyinstaller packaging/DCENT_Voice.spec --noconfirm --clean" in script
    assert "istupakov/parakeet-tdt-0.6b-v3-onnx" in script
    assert "verify-payload" in script
    assert "direct_url.json" in script
    assert '"$PAYLOAD/dcent-voice" platform-check' in script
    assert 'DEFAULT_OUT="$ROOT/dist/DCENT_Voice"' in script
    assert 'cp -a "$DEFAULT_OUT/." "$PAYLOAD/"' in script


def test_pyinstaller_spec_is_platform_aware() -> None:
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")

    assert "from PyInstaller.compat import is_darwin, is_linux, is_win" in spec
    assert "if is_win else []" in spec
    assert "if is_darwin:" in spec
    assert "elif is_linux:" in spec
    assert '"webview.platforms.cocoa"' in spec
    assert '"webview.platforms.gtk"' in spec
    assert '"gi.repository.Gtk"' in spec
    assert '"gi.repository.WebKit2"' in spec
    assert "_is_linux_host_glib" in spec
    for library in (
        "libgio-2.0.so.0",
        "libglib-2.0.so.0",
        "libgmodule-2.0.so.0",
        "libgobject-2.0.so.0",
    ):
        assert library in spec
    assert '*collect_submodules("gi")' not in spec
    assert '"Security"' in spec
    assert '"WebKit"' in spec
    assert "if is_win else None" in spec
    assert 'hookspath=[str(ROOT / "packaging" / "pyinstaller_hooks")]' in spec

    soup_hook = Path("packaging/pyinstaller_hooks/hook-gi.repository.Soup.py").read_text(
        encoding="utf-8"
    )
    webkit_hook = Path("packaging/pyinstaller_hooks/hook-gi.repository.WebKit2.py").read_text(
        encoding="utf-8"
    )
    assert 'GiModuleInfo("Soup", version)' in soup_hook
    assert '("3.0", "2.4")' in soup_hook
    assert 'GiModuleInfo("WebKit2", version)' in webkit_hook
    assert '("4.1", "4.0")' in webkit_hook


def test_release_workflow_builds_native_linux_and_macos_artifacts() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "linux:" in workflow
    assert "runs-on: ubuntu-22.04" in workflow
    assert "runs-on: ubuntu-24.04" not in workflow
    assert "build-essential python3-dev linux-libc-dev libportaudio2 portaudio19-dev" in workflow
    assert "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0" in workflow
    assert "sha256sum --check --strict" in workflow
    assert "bash scripts/build_linux_appimage.sh" in workflow
    assert "dist/DCENT_Voice-linux-*.AppImage" in workflow
    assert "dist/DCENT_Voice-linux-*.deb" in workflow
    assert "libgirepository1.0-dev" in workflow
    assert "libwebkit2gtk-4.0-dev" in workflow
    assert "libwebkit2gtk-4.0-37" in workflow
    assert "gir1.2-webkit2-4.0" in workflow
    assert "xvfb" in workflow
    assert "smoke_linux_settings.sh" in workflow
    assert "APPIMAGE_RUNTIME_FILE" in workflow
    assert "--runtime-file" in Path("scripts/build_linux_appimage.sh").read_text(encoding="utf-8")
    assert "944632" in workflow
    assert "1cc49bcf1e2ccd593c379adb17c9f85a36d619088296504de95b1d06215aebbf" in workflow
    assert "macos:" in workflow
    assert "runs-on: macos-14" in workflow
    assert "bash scripts/build_macos_app.sh" in workflow
    assert "dist/DCENT_Voice-macos-*.dmg" in workflow
    assert "dist/DCENT_Voice-macos-*.zip" in workflow
    assert "macos-pipeline-status.json" in workflow
    assert "MACOS_CERT_P12_BASE64" in workflow
    assert "MACOS_NOTARY_KEY_BASE64" in workflow
    macos_builder = Path("scripts/build_macos_app.sh").read_text(encoding="utf-8")
    assert ".macos-pipeline-status.XXXXXX.tmp" in macos_builder
    assert 'mv -f -- "$STATUS_TMP" "$STATUS"' in macos_builder
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_macos_pipeline.py" in ci


def test_native_ui_dependencies_are_platform_marked_and_locked() -> None:
    import tomllib

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = set(project["dependencies"])
    assert "PyGObject==3.50.0; platform_system == 'Linux'" in dependencies
    assert "pycairo>=1.26; platform_system == 'Linux'" in dependencies
    assert "SecretStorage>=3.3; platform_system == 'Linux'" in dependencies
    assert "pyobjc-framework-Security>=10.0; platform_system == 'Darwin'" in dependencies
    assert "pyobjc-framework-WebKit>=10.0; platform_system == 'Darwin'" in dependencies

    lock = Path("uv.lock").read_text(encoding="utf-8")
    assert 'name = "pygobject"' in lock
    assert 'name = "pycairo"' in lock
    assert 'name = "secretstorage"' in lock
    assert 'name = "pyobjc-framework-security"' in lock
    assert 'name = "pyobjc-framework-webkit"' in lock

    app = Path("src/dcent_voice/app.py").read_text(encoding="utf-8")
    assert 'gi.require_version("Gdk", "3.0")' in app
    # The accepted WebKit2/Soup ABI pairs live in one place so the runtime
    # loader and doctor's `desktop.webkitgtk` check can never disagree about
    # what "WebKitGTK is available" means.
    assert "LINUX_WEBKIT_ABIS" in app
    probe = Path("src/dcent_voice/doctor/probe.py").read_text(encoding="utf-8")
    assert 'LINUX_WEBKIT_ABIS: tuple[tuple[str, str], ...] = (("4.1", "3.0"), ("4.0", "2.4"))' in (
        probe
    )


def test_linux_webkit_probe_prefers_41_then_falls_back_to_40() -> None:
    from dcent_voice.app import _require_linux_webkit

    class FakeGI:
        def __init__(self, available: set[tuple[str, str]]) -> None:
            self.available = available
            self.calls: list[tuple[str, str]] = []

        def require_version(self, namespace: str, version: str) -> None:
            requested = (namespace, version)
            self.calls.append(requested)
            if requested not in self.available:
                raise ValueError(f"missing {namespace} {version}")

    modern = FakeGI({("WebKit2", "4.1"), ("Soup", "3.0")})
    assert _require_linux_webkit(modern) == ("4.1", "3.0")
    assert modern.calls == [("WebKit2", "4.1"), ("Soup", "3.0")]

    jammy = FakeGI({("WebKit2", "4.0"), ("Soup", "2.4")})
    assert _require_linux_webkit(jammy) == ("4.0", "2.4")
    assert jammy.calls == [
        ("WebKit2", "4.1"),
        ("WebKit2", "4.0"),
        ("Soup", "2.4"),
    ]


def test_frozen_linux_restores_host_library_path(monkeypatch) -> None:
    import sys

    from dcent_voice.app import _restore_frozen_linux_library_path

    monkeypatch.setattr("dcent_voice.app.platform.system", lambda: "Linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/frozen")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/host")

    _restore_frozen_linux_library_path()

    assert os.environ["LD_LIBRARY_PATH"] == "/host"


def test_frozen_linux_clears_injected_library_path(monkeypatch) -> None:
    import sys

    from dcent_voice.app import _restore_frozen_linux_library_path

    monkeypatch.setattr("dcent_voice.app.platform.system", lambda: "Linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/frozen")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    _restore_frozen_linux_library_path()

    assert "LD_LIBRARY_PATH" not in os.environ


def test_debian_runtime_accepts_webkit_40_or_41() -> None:
    control = Path("packaging/linux/debian/control").read_text(encoding="utf-8")

    assert "libwebkit2gtk-4.1-0 | libwebkit2gtk-4.0-37" in control


def test_pyinstaller_spec_includes_onnx_asr_for_parakeet_default() -> None:
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    assert "collect_all" in spec
    assert 'collect_all("onnx_asr")' in spec
    assert "collect_submodules" in spec


def test_packaged_windows_tree_ships_parakeet_default() -> None:
    """If a Windows payload exists, it must be the current default — not a stale Whisper tree."""
    tree = Path("dist/DCENT_Voice")
    if not (tree / "dcent-voice.exe").is_file():
        pytest.skip("Windows payload not built in this environment")
    bundled = tree / "_internal" / "config.example.toml"
    assert bundled.is_file(), "payload is missing bundled config.example.toml"
    # The payload root copy is the human-discoverable one; the installer's
    # ValidatePayload and verify_shipped_payload both require it.
    at_root = tree / "config.example.toml"
    assert at_root.is_file(), "payload root is missing config.example.toml"
    assert at_root.read_bytes() == bundled.read_bytes()
    text = bundled.read_text(encoding="utf-8")
    assert 'active_profile = "desktop"' in text
    assert 'asr = "parakeet:tdt-0.6b-v3:int8"' in text
    assert (tree / "_internal" / "onnx_asr" / "__init__.py").is_file(), (
        "payload onnx_asr is data-only and would shadow the frozen package; "
        "rebuild with collect_all('onnx_asr')"
    )
    weights = tree / "models" / "parakeet-tdt-0.6b-v3" / "encoder-model.int8.onnx"
    assert weights.is_file(), "payload is missing bundled Parakeet ONNX weights"


def test_frozen_exe_ships_compose_writer() -> None:
    """If a Windows payload exists, `compose` must be the current writer."""
    import subprocess

    exe = Path("dist/DCENT_Voice/dcent-voice.exe")
    if not exe.is_file():
        pytest.skip("Windows payload not built in this environment")
    result = subprocess.run(
        [
            str(exe),
            "compose",
            "--style",
            "email",
            "hey can you send the deck to alice actually bob thanks",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "Could you" in out
    assert "Bob" in out
    assert "alice" not in out.lower()
    discourse = subprocess.run(
        [
            str(exe),
            "compose",
            "what I mean by that is we should wait",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert discourse.returncode == 0, discourse.stderr
    kept = discourse.stdout.lower()
    assert "what i mean" in kept
    assert "by that" in kept
    assert "should wait" in kept
    assert not kept.lstrip().startswith("by that")
    listed = subprocess.run(
        [
            str(exe),
            "compose",
            "--style",
            "email",
            "hey can you send the deck to alice actually bob and then update the timeline thanks",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    doc = listed.stdout
    assert "1." in doc
    assert "Bob" in doc
    assert "Thanks," in doc
    named = subprocess.run(
        [
            str(exe),
            "compose",
            "--style",
            "email",
            "Hey Alice, send the deck and then update the timeline thanks",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert named.returncode == 0, named.stderr
    named_out = named.stdout
    assert named_out.startswith("Hey Alice,")
    assert "1. Send the deck" in named_out
    assert "2. Update the timeline" in named_out
    assert named_out.lower().count("hey alice") == 1


def test_pyinstaller_spec_keeps_optional_tts_runtimes_out_of_the_beta_bundle() -> None:
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")

    for package in ("kokoro_onnx", "piper", "espeakng_loader", "phonemizer"):
        assert f'"{package}"' not in spec


def test_payload_ships_the_example_config_at_the_root_and_in_internal() -> None:
    """WS1: the resolver reads _internal; a person opens the folder and sees the root copy."""
    spec = Path("packaging/DCENT_Voice.spec").read_text(encoding="utf-8")
    assert '(str(ROOT / "config.example.toml"), ".")' in spec, "the _internal copy must stay"
    assert 'shutil.copy2(ROOT / "config.example.toml"' in spec, (
        "the spec must also place config.example.toml at the payload root"
    )

    program = Path("packaging/windows/setup-stub/Program.cs").read_text(encoding="utf-8")
    assert 'Path.Combine(root, "_internal", "config.example.toml")' in program
    assert 'Path.Combine(root, "config.example.toml")' in program, (
        "ValidatePayload must require the payload-root copy too"
    )

    registry = Path("src/dcent_voice/asr/model_registry.py").read_text(encoding="utf-8")
    assert 'payload / "config.example.toml"' in registry, (
        "verify_shipped_payload must require the payload-root copy too"
    )


def test_fresh_profile_smoke_script_is_wired_for_ci() -> None:
    """AC1's proof harness must exist and must never bypass config seeding."""
    script = Path("scripts/fresh_profile_smoke.py")
    assert script.is_file()
    source = script.read_text(encoding="utf-8")
    assert "DCENT_VOICE_PROFILE_ROOT" in source
    assert "DCENT_VOICE_SMOKE_MUTEX" in source
    assert "--print-config" in source
    assert '"--config"' not in source
