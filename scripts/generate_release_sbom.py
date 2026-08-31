#!/usr/bin/env python3
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Generate and validate artifact-derived release notices and a CycloneDX SBOM."""

from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import importlib.metadata as metadata
import json
import shutil
import sqlite3
import ssl
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

SUPPLEMENTAL_LICENSES = {
    "ctranslate2": ("CTranslate2-LICENSE.txt", "MIT"),
    "proxy-tools": ("proxy_tools-LICENSE.txt", "MIT"),
    "tokenizers": ("Apache-2.0.txt", "Apache-2.0"),
}

RUNTIME_LICENSES = {
    "OpenSSL": ("Apache-2.0.txt", "Apache-2.0"),
    "SQLite": ("SQLite-LICENSE.md", "Public Domain"),
    "libffi": ("libffi-LICENSE.txt", "MIT-like"),
}

WINDOWS_NATIVE_LICENSES = {
    "Microsoft Visual C++ and Universal CRT runtime": (
        "Microsoft-Visual-Cpp-Runtime-NOTICE.txt",
        "Microsoft Visual C++ Runtime 2015-2022 terms",
        (
            "msvcp140*.dll",
            "vcruntime140*.dll",
            "api-ms-win-crt-*.dll",
            "ucrtbase.dll",
        ),
    ),
    "PortAudio": (
        "PortAudio-LICENSE.txt",
        "MIT",
        ("libportaudio*.dll", "libportaudio*.dylib", "libportaudio*.so*"),
    ),
}

MODEL_LICENSES = {
    "Systran/faster-whisper-base": (
        "faster-whisper-model-LICENSE.txt",
        "MIT",
        "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    ),
    "NVIDIA Parakeet TDT 0.6B v3 ONNX": (
        "CC-BY-4.0.txt",
        "CC-BY-4.0",
        "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")


def _license_files(dist: metadata.Distribution) -> list[Path]:
    selected: list[Path] = []
    for item in dist.files or ():
        name = PurePosixPath(str(item).replace("\\", "/")).name.casefold()
        if not name.startswith(("license", "licence", "copying", "notice", "authors")):
            continue
        candidate = Path(str(dist.locate_file(item)))
        if candidate.is_file() and not candidate.is_symlink():
            selected.append(candidate)
    return sorted(set(selected), key=lambda path: str(path).casefold())


def _embedded_distributions(toc: Path) -> list[metadata.Distribution]:
    parsed = ast.literal_eval(toc.read_text(encoding="utf-8"))
    if not isinstance(parsed, tuple) or len(parsed) != 2 or not isinstance(parsed[1], list):
        raise ValueError(f"unsupported PyInstaller PYZ TOC: {toc}")
    top_levels = {
        entry[0].partition(".")[0]
        for entry in parsed[1]
        if isinstance(entry, tuple) and entry and isinstance(entry[0], str)
    }
    package_map = metadata.packages_distributions()
    names = {
        dist_name
        for module in top_levels
        for dist_name in package_map.get(module, ())
        if dist_name.casefold().replace("_", "-") != "dcent-voice"
    }
    return sorted(
        (metadata.distribution(name) for name in names),
        key=lambda dist: dist.metadata["Name"].casefold(),
    )


def _copy_notice(source: Path, destination: Path) -> tuple[Path, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination, _sha256(destination)


def _component(
    name: str,
    version: str,
    license_name: str,
    paths: list[tuple[Path, str]],
    *,
    artifact_root: Path,
    kind: str = "library",
) -> dict[str, Any]:
    properties = []
    for path, digest in paths:
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(artifact_root.resolve(strict=True)).as_posix()
        except ValueError as exc:
            raise ValueError(f"license notice escapes the release payload: {resolved}") from exc
        if not relative.startswith("_internal/licenses/"):
            raise ValueError(f"license notice is outside the artifact license tree: {relative}")
        properties.extend(
            [
                {"name": "dcent:license-path", "value": relative},
                {"name": "dcent:license-sha256", "value": digest},
            ]
        )
    component: dict[str, Any] = {
        "type": kind,
        "bom-ref": f"{kind}:{_safe_name(name)}" + (f"@{version}" if version else ""),
        "name": name,
        "licenses": [{"license": {"name": license_name}}],
        "properties": properties,
    }
    if version:
        component["version"] = version
    return component


def _artifact_files(internal: Path, patterns: tuple[str, ...]) -> list[Path]:
    return sorted(
        {
            path
            for pattern in patterns
            for path in internal.rglob(pattern)
            if path.is_file() and not path.is_symlink()
        },
        key=lambda path: path.as_posix().casefold(),
    )


def _windows_file_version(path: Path) -> str:
    """Return a PE fixed-file version when the Windows version API can read it."""

    if sys.platform != "win32":
        return ""
    version_api = ctypes.windll.version
    ignored = ctypes.c_uint(0)
    size = version_api.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version_api.GetFileVersionInfoW(str(path), 0, size, data):
        return ""
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint(0)
    if not version_api.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return ""
    if length.value < 52:
        return ""
    values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
    if values[0] != 0xFEEF04BD:
        return ""
    version_ms, version_ls = values[2], values[3]
    return ".".join(
        str(value)
        for value in (
            version_ms >> 16,
            version_ms & 0xFFFF,
            version_ls >> 16,
            version_ls & 0xFFFF,
        )
    )


def _add_artifact_component(
    components: list[dict[str, Any]],
    *,
    internal: Path,
    license_root: Path,
    source_licenses: Path,
    name: str,
    version: str,
    expression: str,
    notice_name: str,
    binaries: list[Path],
) -> None:
    notice = _copy_notice(source_licenses / notice_name, license_root / "runtime" / notice_name)
    component = _component(
        name,
        version,
        expression,
        [notice],
        artifact_root=internal.parent,
    )
    component["hashes"] = [{"alg": "SHA-256", "content": _sha256(path)} for path in binaries]
    component["properties"].append(
        {
            "name": "dcent:artifact-files",
            "value": ",".join(path.relative_to(internal).as_posix() for path in binaries),
        }
    )
    for path in binaries:
        component["properties"].append(
            {
                "name": "dcent:artifact-file-sha256",
                "value": f"{path.relative_to(internal).as_posix()}={_sha256(path)}",
            }
        )
    components.append(component)


def _setup_runtime_versions(assets: Path) -> dict[str, str]:
    parsed = json.loads(assets.read_text(encoding="utf-8"))
    frameworks = parsed.get("project", {}).get("frameworks", {})
    dependencies: list[dict[str, object]] = []
    for framework in frameworks.values():
        dependencies.extend(framework.get("downloadDependencies", []))
    expected = {
        "Microsoft.NETCore.App.Runtime.win-x64": "Microsoft .NET Runtime win-x64",
        "Microsoft.WindowsDesktop.App.Runtime.win-x64": (
            "Microsoft Windows Desktop Runtime win-x64"
        ),
    }
    versions: dict[str, str] = {}
    for dependency in dependencies:
        package = str(dependency.get("name", ""))
        if package not in expected:
            continue
        declared = str(dependency.get("version", "")).strip()
        version = declared.strip("[]() ")
        if "," in version:
            lower, upper = (part.strip() for part in version.split(",", 1))
            version = lower if declared.startswith("[") and lower == upper else ""
        if not version:
            raise ValueError(f"setup runtime version is not exact: {package} {declared!r}")
        versions[expected[package]] = version
    missing = set(expected.values()) - set(versions)
    if missing:
        raise ValueError(f"setup assets omit required self-contained runtimes: {sorted(missing)}")
    return versions


def generate(
    payload: Path,
    toc: Path,
    repo_root: Path,
    *,
    setup_dotnet_root: Path | None = None,
    setup_assets: Path | None = None,
) -> Path:
    payload = payload.resolve()
    repo_root = repo_root.resolve()
    internal = payload / "_internal"
    source_licenses = repo_root / "packaging" / "licenses"
    if not internal.is_dir() or not toc.is_file() or not source_licenses.is_dir():
        raise FileNotFoundError("payload, PYZ TOC, or packaging/licenses is missing")
    for required in ("LICENSE", "README.md", "THIRD-PARTY-LICENSES.md"):
        path = internal / required
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ValueError(f"release disclosure is missing or unsafe: {path}")

    license_root = internal / "licenses"
    if license_root.exists():
        shutil.rmtree(license_root)
    components: list[dict[str, Any]] = []
    for dist in _embedded_distributions(toc):
        name = dist.metadata["Name"]
        normalized = name.casefold().replace("_", "-")
        version = dist.version
        declared = dist.metadata["License-Expression"] or dist.metadata["License"] or ""
        copied: list[tuple[Path, str]] = []
        for index, source in enumerate(_license_files(dist), start=1):
            target = (
                license_root
                / "python"
                / f"{_safe_name(name)}-{version}"
                / f"{index:02d}-{source.name}"
            )
            copied.append(_copy_notice(source, target))
        if not copied:
            supplemental = SUPPLEMENTAL_LICENSES.get(normalized)
            if supplemental is None:
                raise ValueError(
                    f"embedded distribution has no bundled license evidence: {name} {version}"
                )
            filename, expression = supplemental
            copied.append(
                _copy_notice(
                    source_licenses / filename,
                    license_root / "python" / f"{_safe_name(name)}-{version}" / filename,
                )
            )
            declared = expression
        if not declared.strip():
            declared = SUPPLEMENTAL_LICENSES.get(normalized, ("", "License text bundled"))[1]
        component = _component(
            name,
            version,
            declared.strip(),
            copied,
            artifact_root=payload,
        )
        component["purl"] = f"pkg:pypi/{name}@{version}"
        components.append(component)

    for dist_name, label in (
        ("pyinstaller", "PyInstaller bootloader and runtime hooks"),
        ("pyinstaller-hooks-contrib", "PyInstaller hooks contrib"),
    ):
        dist = metadata.distribution(dist_name)
        notices = _license_files(dist)
        if not notices:
            raise ValueError(f"build runtime has no license evidence: {dist_name}")
        copied = [
            _copy_notice(
                source,
                license_root
                / "runtime"
                / f"{_safe_name(dist_name)}-{dist.version}"
                / f"{index:02d}-{source.name}",
            )
            for index, source in enumerate(notices, start=1)
        ]
        components.append(
            _component(
                label,
                dist.version,
                dist.metadata["License-Expression"]
                or dist.metadata["License"]
                or "GPL-2.0-or-later with Bootloader exception",
                copied,
                artifact_root=payload,
            )
        )

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError(f"CPython license not found: {python_license}")
    python_notice = _copy_notice(python_license, license_root / "runtime" / "CPython-LICENSE.txt")
    components.append(
        _component(
            "CPython",
            sys.version.split()[0],
            "PSF-2.0",
            [python_notice],
            artifact_root=payload,
            kind="framework",
        )
    )

    native_patterns = {
        "OpenSSL": ("libcrypto*", "libssl*"),
        "SQLite": ("sqlite3*", "_sqlite3*"),
        "libffi": ("libffi*", "_ctypes*", "_cffi_backend*"),
    }
    runtime_versions = {
        "OpenSSL": ssl.OPENSSL_VERSION.split()[1],
        "SQLite": sqlite3.sqlite_version,
        # CPython does not expose its linked libffi version. The native file
        # names and SHA-256 hashes remain artifact-derived without inventing one.
        "libffi": "",
    }
    for name, (filename, expression) in RUNTIME_LICENSES.items():
        binaries = _artifact_files(internal, native_patterns[name])
        if not binaries:
            continue
        _add_artifact_component(
            components,
            internal=internal,
            license_root=license_root,
            source_licenses=source_licenses,
            name=name,
            version=runtime_versions[name],
            expression=expression,
            notice_name=filename,
            binaries=binaries,
        )

    windows_payload = (internal / "python311.dll").is_file()
    for name, (notice_name, expression, patterns) in WINDOWS_NATIVE_LICENSES.items():
        binaries = _artifact_files(internal, patterns)
        if not binaries:
            if windows_payload:
                raise ValueError(f"release payload is missing {name} binaries")
            continue
        if name == "PortAudio" and any("-asio." in path.name.casefold() for path in binaries):
            raise ValueError("release payload contains the unused PortAudio ASIO binary")
        version = _windows_file_version(binaries[0]) if name.startswith("Microsoft") else ""
        _add_artifact_component(
            components,
            internal=internal,
            license_root=license_root,
            source_licenses=source_licenses,
            name=name,
            version=version,
            expression=expression,
            notice_name=notice_name,
            binaries=binaries,
        )

    has_pyav = any(
        path.is_file()
        for pattern in ("av.libs/*", "av/*.pyd", "av/*.so")
        for path in internal.glob(pattern)
    )
    if has_pyav:
        raise ValueError(
            "release payload contains unused PyAV/FFmpeg binaries; exclude the optional decoder"
        )

    font_root = internal / "dcent_voice" / "ui" / "web" / "fonts"
    font_license = repo_root / "src" / "dcent_voice" / "ui" / "web" / "fonts"
    font_notices = [
        _copy_notice(
            font_license / "OFL-1.1.txt",
            license_root / "fonts" / "OFL-1.1.txt",
        ),
        _copy_notice(
            font_license / "LICENSE.md",
            license_root / "fonts" / "PROVENANCE.md",
        ),
    ]
    font_specs = {
        "Barlow Condensed": "barlowcondensed-*.woff2",
        "Inter": "inter-*.woff2",
        "JetBrains Mono": "jetbrainsmono-*.woff2",
    }
    for name, pattern in font_specs.items():
        files = _artifact_files(font_root, (pattern,))
        if not files:
            raise ValueError(f"release UI font files are missing: {name}")
        component = _component(
            name,
            "",
            "OFL-1.1",
            font_notices,
            artifact_root=payload,
            kind="file",
        )
        component["hashes"] = [{"alg": "SHA-256", "content": _sha256(path)} for path in files]
        component["properties"].append(
            {
                "name": "dcent:artifact-files",
                "value": ",".join(path.relative_to(internal).as_posix() for path in files),
            }
        )
        components.append(component)

    if (setup_dotnet_root is None) != (setup_assets is None):
        raise ValueError("setup .NET disclosure requires both runtime root and project assets")
    if setup_dotnet_root is not None and setup_assets is not None:
        runtime_license = setup_dotnet_root / "LICENSE.txt"
        third_party = setup_dotnet_root / "ThirdPartyNotices.txt"
        for source in (runtime_license, third_party):
            if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
                raise ValueError(f".NET runtime disclosure is missing or unsafe: {source}")
        dotnet_notices = [
            _copy_notice(
                runtime_license,
                license_root / "runtime" / "dotnet" / "LICENSE.txt",
            ),
            _copy_notice(
                third_party,
                license_root / "runtime" / "dotnet" / "ThirdPartyNotices.txt",
            ),
        ]
        for name, version in _setup_runtime_versions(setup_assets).items():
            components.append(
                _component(
                    name,
                    version,
                    "MIT and bundled notices",
                    dotnet_notices,
                    artifact_root=payload,
                )
            )

    model_specs = (
        (
            "Systran/faster-whisper-base",
            payload / "models" / "faster-whisper" / "Systran--faster-whisper-base",
        ),
        ("NVIDIA Parakeet TDT 0.6B v3 ONNX", payload / "models" / "parakeet-tdt-0.6b-v3"),
    )
    for name, model_root in model_specs:
        if not model_root.is_dir():
            raise ValueError(
                f"shipped model is missing while generating release notices: {model_root}"
            )
        filename, expression, revision = MODEL_LICENSES[name]
        copied = [_copy_notice(source_licenses / filename, license_root / "models" / filename)]
        if name.startswith("NVIDIA"):
            copied.append(
                _copy_notice(
                    source_licenses / "Parakeet-TDT-0.6B-v3-ATTRIBUTION.txt",
                    license_root / "models" / "Parakeet-TDT-0.6B-v3-ATTRIBUTION.txt",
                )
            )
        component = _component(
            name,
            revision,
            expression,
            copied,
            artifact_root=payload,
            kind="machine-learning-model",
        )
        component["properties"].append(
            {"name": "dcent:model-root", "value": model_root.relative_to(payload).as_posix()}
        )
        components.append(component)

    with (repo_root / "pyproject.toml").open("rb") as stream:
        application_version = str(tomllib.load(stream)["project"]["version"])
    components.sort(key=lambda item: str(item["bom-ref"]))
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": f"pkg:pypi/dcent-voice@{application_version}",
                "name": "DCENT_Voice",
                "version": application_version,
                "licenses": [{"license": {"id": "MIT"}}],
            }
        },
        "components": components,
    }
    output = internal / "THIRD-PARTY-SBOM.cdx.json"
    output.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not output.is_file() or output.stat().st_size == 0:
        raise ValueError("SBOM generation did not produce a readable artifact")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--toc", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--setup-dotnet-root", type=Path)
    parser.add_argument("--setup-assets", type=Path)
    args = parser.parse_args(argv)
    try:
        output = generate(
            args.payload,
            args.toc,
            args.repo_root,
            setup_dotnet_root=args.setup_dotnet_root,
            setup_assets=args.setup_assets,
        )
    except (OSError, ValueError, metadata.PackageNotFoundError) as exc:
        print(f"release SBOM validation failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
