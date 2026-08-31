# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Populate a Hugging Face hub cache with the pinned model snapshots.

``scripts/download_models.py`` stages the shipped models into the payload with
:func:`dcent_voice.asr.model_registry.stage_verified_snapshot` whenever
:func:`pinned_huggingface_snapshot` already finds a *verifying* snapshot in the
hub cache.  That is the only reuse hook the release build exposes, so CI warms
the cache through this script and then restores it with ``actions/cache``.

Two details make the difference between a cache that hits and one that does
not:

* ``verify_pinned_snapshot`` rejects symlinks and hard links
  (``_safe_regular_file``), and ``huggingface_hub`` populates its cache with
  symlinks into ``blobs/`` on every platform where symlinks are permitted.  So
  the download lands in a scratch directory and this script copies **real
  regular files** into the snapshot directory.
* The snapshot directory must contain exactly the manifest's file set — no
  ``.cache``/``.gitattributes`` extras — because verification re-enumerates the
  directory and compares the name set.

The script is offline-safe: if the cache already verifies, nothing is
downloaded and no network call is made.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# This script is the sanctioned downloader, exactly like scripts/download_models.py.
# The app-side offline enforcement (WS2) keys off this variable, so set it before
# dcent_voice / huggingface_hub are imported.
os.environ.setdefault("DCENT_VOICE_ALLOW_HUB", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.asr.model_registry import (  # noqa: E402
    safe_model_dir_name,
    verify_pinned_snapshot,
)

MANIFEST_DIR = ROOT / "src" / "dcent_voice" / "asr" / "manifests"


def default_cache_dir() -> Path:
    """Mirror ``model_registry._huggingface_snapshot``'s cache resolution."""
    configured = os.environ.get("HF_HUB_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "huggingface" / "hub"


def snapshot_dir(cache_dir: Path, model_id: str, revision: str) -> Path:
    return cache_dir / f"models--{safe_model_dir_name(model_id)}" / "snapshots" / revision


def warm(manifest_path: Path, cache_dir: Path, *, offline: bool) -> bool:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_id = str(manifest["modelId"])
    revision = str(manifest["revision"])
    names = sorted(manifest["files"])
    target = snapshot_dir(cache_dir, model_id, revision)

    valid, detail = verify_pinned_snapshot(target, model_id)
    if valid:
        print(f"cache hit: {model_id}@{revision[:12]} -> {target}")
        return True
    if target.exists():
        print(f"cache miss ({detail}): rebuilding {target}")
    if offline:
        print(f"offline: refusing to download {model_id} ({detail})", file=sys.stderr)
        return False

    from huggingface_hub import hf_hub_download

    scratch = Path(tempfile.mkdtemp(prefix="dcent-warm-", dir=str(cache_dir.parent)))
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=str(target.parent)))
    published = False
    try:
        for name in names:
            downloaded = hf_hub_download(
                repo_id=model_id,
                filename=name,
                revision=revision,
                local_dir=str(scratch),
            )
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            # copyfile dereferences: the cache must hold regular files, never
            # the symlinks huggingface_hub would otherwise leave behind.
            shutil.copyfile(downloaded, destination)
        valid, detail = verify_pinned_snapshot(staging, model_id)
        if not valid:
            print(f"warmed snapshot failed verification: {model_id}: {detail}", file=sys.stderr)
            return False
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
        published = True
        print(f"warmed: {model_id}@{revision[:12]} -> {target}")
        return True
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Hugging Face hub cache root (default: $HF_HUB_CACHE or ~/.cache/huggingface/hub)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Verify the cache without downloading; exit non-zero when it would need the network.",
    )
    args = parser.parse_args(argv)

    cache_dir = (args.cache_dir or default_cache_dir()).resolve()
    manifests = sorted(MANIFEST_DIR.glob("*.json"))
    if not manifests:
        print(f"No pinned manifests under {MANIFEST_DIR}", file=sys.stderr)
        return 2

    cache_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot_dir(cache_dir, str(manifest["modelId"]), str(manifest["revision"])).parent.mkdir(
            parents=True, exist_ok=True
        )
        if not warm(manifest_path, cache_dir, offline=args.offline):
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
