# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
import json

import pytest

from dcent_voice.wake_word import load_wake_manifest


def _manifest(tmp_path, *, model_path: str = "hey-dcent.onnx", checksum: str | None = None):
    model = tmp_path / "hey-dcent.onnx"
    model.write_bytes(b"offline-model-fixture")
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "engine": "openwakeword",
                "modelId": "hey-dcent",
                "modelPath": model_path,
                "sha256": checksum or hashlib.sha256(model.read_bytes()).hexdigest(),
                "phrase": "hey-dcent",
                "license": "fixture-only",
                "threshold": 0.55,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_local_manifest_is_checksum_pinned(tmp_path) -> None:
    manifest = load_wake_manifest(_manifest(tmp_path))
    assert manifest.model_id == "hey-dcent"
    assert manifest.model_path == (tmp_path / "hey-dcent.onnx").resolve()
    assert manifest.threshold == 0.55


@pytest.mark.parametrize("model_path", ["https://example.com/model.onnx", "../outside.onnx"])
def test_manifest_refuses_remote_or_escaping_model_paths(tmp_path, model_path) -> None:
    with pytest.raises((ValueError, FileNotFoundError)):
        load_wake_manifest(_manifest(tmp_path, model_path=model_path))


def test_manifest_refuses_checksum_mismatch(tmp_path) -> None:
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_wake_manifest(_manifest(tmp_path, checksum="0" * 64))
