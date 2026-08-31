# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# This script is one of the two sanctioned downloaders (the other is
# scripts/qa/warm_model_cache.py). dcent_voice.util.bootlog pins every other
# process to HF_HUB_OFFLINE=1; opting out has to happen before dcent_voice or
# huggingface_hub is imported, so it happens here at module scope.
os.environ.setdefault("DCENT_VOICE_ALLOW_HUB", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.asr.model_registry import (  # noqa: E402
    pinned_huggingface_snapshot,
    pinned_model_manifest,
    stage_verified_snapshot,
    verify_pinned_snapshot,
)
from dcent_voice.package_bundle import (  # noqa: E402
    DEFAULT_MODEL_IDS,
    PARAKEET_BUNDLE_ID,
    PARAKEET_BUNDLE_REL,
    build_offline_bundle_manifest,
    model_path,
    write_offline_bundle_manifest,
)

DEFAULT_DOWNLOAD_IDS = (*DEFAULT_MODEL_IDS, PARAKEET_BUNDLE_ID)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bundle_dir = args.bundle_dir.resolve()
    requested_ids = tuple(_split_csv(args.models))
    model_ids = tuple(item for item in requested_ids if item != PARAKEET_BUNDLE_ID)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "wheels").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "models" / "faster-whisper").mkdir(parents=True, exist_ok=True)

    if args.include_wheels and not args.dry_run:
        download_wheels(bundle_dir)

    if not args.dry_run:
        if not args.accept_model_license:
            raise SystemExit(
                "Live model acquisition requires --accept-model-license. Review "
                "the pinned model provenance, attribution, and licenses in "
                "THIRD-PARTY-LICENSES.md first."
            )
        for model_id in requested_ids:
            download_model(model_id, _download_target(bundle_dir, model_id))

    manifest = build_offline_bundle_manifest(bundle_dir, model_ids=model_ids)
    manifest_path = write_offline_bundle_manifest(bundle_dir, manifest)
    print(manifest_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the DCENT_Voice offline bundle manifest and optionally fetch models."
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=ROOT / "build" / "offline-bundle",
        help="Bundle root containing wheels/ and models/.",
    )
    parser.add_argument(
        "--accept-model-license",
        action="store_true",
        help="Explicitly consent to fetching the pinned model and accept its license.",
    )
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_DOWNLOAD_IDS),
        help="Comma-separated pinned Hugging Face model IDs to stage.",
    )
    parser.add_argument(
        "--include-wheels",
        action="store_true",
        help="Export the frozen uv lock and download its wheels into bundle/wheels.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create directories and manifest only. No network or package downloads.",
    )
    return parser


def download_wheels(bundle_dir: Path) -> None:
    wheel_dir = bundle_dir / "wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(wheel_dir.glob("*.whl"))
    if existing:
        raise SystemExit(
            "Offline wheel directory must be empty before a locked export: "
            + ", ".join(path.name for path in existing[:5])
        )
    lock_export = bundle_dir / "requirements.lock.txt"
    with tempfile.TemporaryDirectory(prefix="dcent-wheel-export-") as temp_dir:
        temporary_export = Path(temp_dir) / "requirements.lock.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--quiet",
                "--extra",
                "cuda",
                "--no-dev",
                "--locked",
                "--no-header",
                "--no-emit-project",
                "--no-emit-package",
                "av",
                "--no-annotate",
                "--output-file",
                str(temporary_export),
            ],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            [
                "uv",
                "build",
                str(ROOT / "packaging" / "av-shim"),
                "--wheel",
                "--no-sources",
                "--out-dir",
                str(wheel_dir),
            ],
            cwd=ROOT,
            check=True,
        )
        shim_wheels = list(wheel_dir.glob("av-18.0.0+dcentshim.1-*.whl"))
        if len(shim_wheels) != 1:
            raise SystemExit("Locked offline export did not produce exactly one PCM-only AV shim.")
        shim_hash = hashlib.sha256(shim_wheels[0].read_bytes()).hexdigest()
        with temporary_export.open("a", encoding="utf-8") as stream:
            stream.write(f"\nav==18.0.0+dcentshim.1 --hash=sha256:{shim_hash}\n")
        shutil.copyfile(temporary_export, lock_export)
        subprocess.run(
            [
                "uv",
                "tool",
                "run",
                "--from",
                "pip==26.2.1",
                "pip",
                "wheel",
                "--require-hashes",
                "--wheel-dir",
                str(wheel_dir),
                "--find-links",
                str(wheel_dir),
                "--requirement",
                str(temporary_export),
            ],
            cwd=ROOT,
            check=True,
        )
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=ROOT,
        check=True,
    )
    project_wheels = list(wheel_dir.glob("dcent_voice-*.whl"))
    if len(project_wheels) != 1:
        raise SystemExit("Locked offline export did not produce exactly one DCENT_Voice wheel.")


def download_model(model_id: str, target_dir: Path) -> None:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    cached = pinned_huggingface_snapshot(model_id)
    if cached is not None:
        valid, _detail = verify_pinned_snapshot(cached, model_id)
        if valid:
            stage_verified_snapshot(cached, target_dir, model_id)
            return
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit(
            "huggingface_hub is required for live model downloads. "
            "Install it or rerun with --dry-run to create only the manifest."
        ) from exc

    manifest = pinned_model_manifest(model_id)
    if manifest is None:
        raise SystemExit(
            f"No immutable verification manifest exists for {model_id}; refusing download."
        )
    snapshot_download(
        repo_id=model_id,
        revision=manifest["revision"],
        local_dir=target_dir,
        allow_patterns=sorted(manifest["files"]),
        force_download=False,
    )
    shutil.rmtree(target_dir / ".cache", ignore_errors=True)
    valid, detail = verify_pinned_snapshot(target_dir, model_id)
    if not valid:
        raise SystemExit(f"Downloaded model failed verification: {model_id}: {detail}")


def _download_target(bundle_dir: Path, model_id: str) -> Path:
    if model_id == PARAKEET_BUNDLE_ID:
        return bundle_dir / PARAKEET_BUNDLE_REL
    return model_path(bundle_dir, model_id)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
