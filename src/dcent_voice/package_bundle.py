# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Create and validate offline model bundles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dcent_voice.asr.model_registry import (
    PINNED_BASE_MODEL_ID,
    PINNED_PARAKEET_MODEL_ID,
    pinned_model_manifest,
    safe_model_dir_name,
    valid_faster_whisper_snapshot,
    verify_pinned_snapshot,
)

APP_NAME = "DCENT_Voice"
BUNDLE_MANIFEST = "dcent-voice-offline-bundle.json"
# Whisper fallbacks an air-gapped install must still be able to run. Desktop
# default is Parakeet; that entry is appended separately when weights exist.
DEFAULT_MODEL_IDS = (PINNED_BASE_MODEL_ID,)


@dataclass(frozen=True)
class BundledModel:
    provider: str
    model_id: str
    path: str
    present: bool
    revision: str | None = None
    sha256: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    license: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class OfflineBundleManifest:
    version: int
    product: str
    created_at: str
    wheel_dir: str
    model_dir: str
    wheels: tuple[str, ...] = field(default_factory=tuple)
    wheel_sha256: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    models: tuple[BundledModel, ...] = field(default_factory=tuple)
    remote_urls: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["createdAt"] = data.pop("created_at")
        data["wheelDir"] = data.pop("wheel_dir")
        data["modelDir"] = data.pop("model_dir")
        data["remoteUrls"] = list(data.pop("remote_urls"))
        data["wheels"] = list(self.wheels)
        data["wheelSha256"] = dict(data.pop("wheel_sha256"))
        data["models"] = [_model_json(model) for model in self.models]
        return data


def _model_json(model: BundledModel) -> dict[str, Any]:
    data: dict[str, Any] = {
        "provider": model.provider,
        "modelId": model.model_id,
        "path": model.path,
        "present": model.present,
    }
    if model.revision is not None:
        data["revision"] = model.revision
        data["sha256"] = dict(model.sha256)
        data["license"] = model.license
        data["source"] = model.source
    return data


def model_path(bundle_dir: Path, model_id: str) -> Path:
    return bundle_dir / "models" / "faster-whisper" / safe_model_dir_name(model_id)


PARAKEET_BUNDLE_ID = PINNED_PARAKEET_MODEL_ID
PARAKEET_BUNDLE_REL = "models/parakeet-tdt-0.6b-v3"


def _parakeet_bundle_entry(root: Path) -> BundledModel:
    dest = root / "models" / "parakeet-tdt-0.6b-v3"
    manifest = pinned_model_manifest(PARAKEET_BUNDLE_ID)
    assert manifest is not None
    present, _detail = verify_pinned_snapshot(dest, PARAKEET_BUNDLE_ID)
    return BundledModel(
        provider="parakeet",
        model_id=PARAKEET_BUNDLE_ID,
        path=PARAKEET_BUNDLE_REL,
        present=present,
        revision=str(manifest["revision"]),
        sha256=tuple(
            (name, str(metadata["sha256"])) for name, metadata in sorted(manifest["files"].items())
        ),
        license=str(manifest["license"]),
        source=str(manifest["source"]),
    )


def build_offline_bundle_manifest(
    bundle_dir: Path,
    *,
    model_ids: tuple[str, ...] = DEFAULT_MODEL_IDS,
    created_at: datetime | None = None,
) -> OfflineBundleManifest:
    root = bundle_dir.resolve()
    wheels = tuple(
        sorted(
            _relative_to_root(path, root)
            for path in (root / "wheels").glob("*.whl")
            if path.is_file()
        )
    )
    wheel_sha256 = tuple((relative, _sha256_file(root / relative)) for relative in wheels)
    models_list: list[BundledModel] = []
    for model_id in model_ids:
        path = model_path(root, model_id)
        pinned = pinned_model_manifest(model_id)
        if pinned is not None:
            present, _detail = verify_pinned_snapshot(path, model_id)
            revision = str(pinned["revision"])
            checksums = tuple(
                (name, str(metadata["sha256"]))
                for name, metadata in sorted(pinned["files"].items())
            )
        else:
            present = valid_faster_whisper_snapshot(path)
            revision = None
            checksums = ()
        models_list.append(
            BundledModel(
                provider="faster-whisper",
                model_id=model_id,
                path=_relative_to_root(path, root),
                present=present,
                revision=revision,
                sha256=checksums,
                license=(str(pinned["license"]) if pinned else None),
                source=(str(pinned["source"]) if pinned else None),
            )
        )
    models = tuple(models_list) + (_parakeet_bundle_entry(root),)

    return OfflineBundleManifest(
        version=2,
        product=APP_NAME,
        created_at=(created_at or datetime.now(UTC)).isoformat(),
        wheel_dir="wheels",
        model_dir="models/faster-whisper",
        wheels=wheels,
        wheel_sha256=wheel_sha256,
        models=models,
        remote_urls=(),
    )


def write_offline_bundle_manifest(
    bundle_dir: Path,
    manifest: OfflineBundleManifest,
    *,
    filename: str = BUNDLE_MANIFEST,
) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    path = bundle_dir / filename
    path.write_text(
        json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_offline_bundle_manifest(path: Path) -> OfflineBundleManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    models = tuple(
        BundledModel(
            provider=str(item["provider"]),
            model_id=str(item["modelId"]),
            path=str(item["path"]),
            present=bool(item["present"]),
            revision=(str(item["revision"]) if item.get("revision") else None),
            sha256=tuple(
                sorted(
                    (str(name), str(digest)) for name, digest in (item.get("sha256") or {}).items()
                )
            ),
            license=(str(item["license"]) if item.get("license") else None),
            source=(str(item["source"]) if item.get("source") else None),
        )
        for item in raw.get("models", [])
    )
    return OfflineBundleManifest(
        version=int(raw["version"]),
        product=str(raw["product"]),
        created_at=str(raw["createdAt"]),
        wheel_dir=str(raw["wheelDir"]),
        model_dir=str(raw["modelDir"]),
        wheels=tuple(str(wheel) for wheel in raw.get("wheels", [])),
        wheel_sha256=tuple(
            sorted(
                (str(name), str(digest)) for name, digest in (raw.get("wheelSha256") or {}).items()
            )
        ),
        models=models,
        remote_urls=tuple(str(url) for url in raw.get("remoteUrls", [])),
    )


def build_hub_launch_descriptor() -> dict[str, Any]:
    return {
        "moduleId": "dcent-voice",
        "displayName": "DCENT Voice",
        "sovereigntyClass": "LOCAL",
        "command": ".venv/Scripts/python.exe",
        "args": ["-m", "dcent_voice", "--no-tray", "--no-overlay"],
        "fakeAudioArgs": [
            "-m",
            "dcent_voice",
            "--no-tray",
            "--no-overlay",
            "--no-hotkeys",
        ],
        "env": {
            "DCENT_VOICE_SERVICE_HOST": "127.0.0.1",
            "DCENT_VOICE_SERVICE_PORT": "8765",
        },
        "fakeAudioEnv": {
            "DCENT_VOICE_FAKE_AUDIO": "1",
            "DCENT_VOICE_SERVICE_HOST": "127.0.0.1",
            "DCENT_VOICE_SERVICE_PORT": "8765",
        },
        "capabilities": [
            "stt.partial",
            "stt.final",
            "module.sovereignty",
        ],
    }


def _relative_to_root(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
