# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from dcent_voice.asr.model_registry import (
    MODEL_DIR_ENV,
    PINNED_BASE_MODEL_ID,
    PINNED_BASE_REVISION,
    PINNED_PARAKEET_MODEL_ID,
    PINNED_PARAKEET_REVISION,
    REGISTRY_FILENAME,
    ModelUnavailableError,
    canonical_model_id,
    install_models_from_bundle,
    model_root,
    pinned_model_manifest,
    resolve_faster_whisper_model,
    runtime_model_path,
    safe_model_dir_name,
    stage_verified_payload,
    stage_verified_snapshot,
    valid_faster_whisper_snapshot,
    verified_snapshot_lock,
    verify_pinned_snapshot,
    verify_shipped_payload,
)
from dcent_voice.package_bundle import (
    build_offline_bundle_manifest,
    model_path,
    write_offline_bundle_manifest,
)


def _snapshot(path: Path, *, marker: bytes = b"weights") -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"model_type": "whisper"}', encoding="utf-8")
    (path / "model.bin").write_bytes(marker)
    return path


def _runtime_payload(path: Path) -> Path:
    internal = path / "_internal"
    for relative in (
        "base_library.zip",
        "config.example.toml",
        "THIRD-PARTY-LICENSES.md",
        "python311.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "onnx_asr/__init__.py",
        "onnxruntime/capi/onnxruntime.dll",
        "ctranslate2/ctranslate2.dll",
        "_sounddevice_data/portaudio-binaries/libportaudio64bit.dll",
        "webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
        "dcent_voice/asr/manifests/faster-whisper-base.json",
        "dcent_voice/asr/manifests/parakeet-tdt-0.6b-v3.json",
    ):
        target = internal / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"fixture:{relative}".encode())
    (path / "dcent-voice.exe").write_bytes(b"MZ fixture")
    # WS1: the example config ships twice — _internal (what the resolver reads)
    # and the payload root (what a person can find). verify_shipped_payload
    # requires both, so the fixture must provide both.
    (path / "config.example.toml").write_bytes(b"fixture:config.example.toml")
    (path / "dcent-voice-offline-bundle.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "modelId": PINNED_BASE_MODEL_ID,
                        "present": True,
                        "revision": PINNED_BASE_REVISION,
                    },
                    {
                        "modelId": PINNED_PARAKEET_MODEL_ID,
                        "present": True,
                        "revision": PINNED_PARAKEET_REVISION,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_model_root_honors_environment_override(tmp_path, monkeypatch) -> None:
    override = tmp_path / "Models With Spaces"
    monkeypatch.setenv(MODEL_DIR_ENV, str(override))

    assert model_root() == override.resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows durable model location")
def test_windows_default_model_root_is_not_the_replaceable_install_tree(
    tmp_path, monkeypatch
) -> None:
    import dcent_voice.asr.model_registry as registry

    monkeypatch.delenv(MODEL_DIR_ENV, raising=False)
    monkeypatch.setattr(
        registry.paths,
        "user_data_dir",
        lambda appname: tmp_path / appname,
    )

    root = model_root()

    assert root == tmp_path / "DCENT_Voice.Models"
    assert root != tmp_path / "DCENT_Voice" / "models"


def test_resolver_distinguishes_tiny_and_tiny_en_without_network(tmp_path, monkeypatch) -> None:
    tiny_id = canonical_model_id("tiny")
    tiny_en_id = canonical_model_id("tiny.en")
    tiny = _snapshot(runtime_model_path(tiny_id, root=tmp_path), marker=b"multilingual")
    tiny_en = _snapshot(runtime_model_path(tiny_en_id, root=tmp_path), marker=b"english")

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("model resolution must not access the network")

    monkeypatch.setattr(socket, "create_connection", network_forbidden)

    assert resolve_faster_whisper_model("tiny", root=tmp_path) == str(tiny.resolve())
    assert resolve_faster_whisper_model("faster-whisper:tiny.en:cpu-int8", root=tmp_path) == str(
        tiny_en.resolve()
    )
    assert tiny != tiny_en


def test_resolver_rejects_incomplete_snapshot_without_online_fallback(tmp_path) -> None:
    incomplete = runtime_model_path(canonical_model_id("distil-small.en"), root=tmp_path)
    incomplete.mkdir(parents=True)
    (incomplete / "config.json").write_text("{}", encoding="utf-8")

    assert valid_faster_whisper_snapshot(incomplete) is False
    with pytest.raises(RuntimeError, match="never downloads speech models"):
        resolve_faster_whisper_model("distil-small.en", root=tmp_path)


def test_pinned_base_manifest_has_immutable_revision_and_runtime_hashes() -> None:
    manifest = pinned_model_manifest(PINNED_BASE_MODEL_ID)
    assert manifest is not None
    assert manifest["revision"] == PINNED_BASE_REVISION
    assert manifest["license"] == "MIT"
    assert set(manifest["files"]) == {
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    }
    assert manifest["files"]["model.bin"] == {
        "size": 145217532,
        "sha256": "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
    }


def test_pinned_parakeet_manifest_has_exact_verified_identity() -> None:
    manifest = pinned_model_manifest(PINNED_PARAKEET_MODEL_ID)
    assert manifest is not None
    assert manifest["revision"] == PINNED_PARAKEET_REVISION
    assert manifest["license"] == "CC-BY-4.0"
    assert set(manifest["files"]) == {
        "config.json",
        "decoder_joint-model.int8.onnx",
        "encoder-model.int8.onnx",
        "vocab.txt",
    }
    assert manifest["files"]["encoder-model.int8.onnx"] == {
        "size": 652183999,
        "sha256": "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09",
    }


@pytest.mark.parametrize("model_id", [PINNED_BASE_MODEL_ID, PINNED_PARAKEET_MODEL_ID])
def test_pinned_verifier_is_closed_world_for_files_directories_and_links(
    tmp_path, monkeypatch, model_id: str
) -> None:
    import dcent_voice.asr.model_registry as registry

    content = b"trusted"
    manifest = {
        "schemaVersion": 1,
        "modelId": model_id,
        "revision": "test",
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _model_id: manifest)
    snapshot = tmp_path / model_id.replace("/", "--")
    snapshot.mkdir()
    (snapshot / "weights.bin").write_bytes(content)
    assert verify_pinned_snapshot(snapshot, model_id) == (True, "verified")

    (snapshot / "extra.bin").write_bytes(b"private")
    assert verify_pinned_snapshot(snapshot, model_id)[0] is False
    (snapshot / "extra.bin").unlink()
    (snapshot / "metadata").mkdir()
    assert verify_pinned_snapshot(snapshot, model_id)[0] is False
    (snapshot / "metadata").rmdir()
    link = snapshot / "extra-link"
    try:
        link.symlink_to(snapshot / "weights.bin")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert verify_pinned_snapshot(snapshot, model_id)[0] is False


def test_verify_shipped_payload_requires_both_exact_snapshots(tmp_path, monkeypatch) -> None:
    import dcent_voice.asr.model_registry as registry

    seen: list[tuple[str, Path]] = []
    _runtime_payload(tmp_path)
    (tmp_path / "models").mkdir()

    def verify(path: Path, model_id: str) -> tuple[bool, str]:
        seen.append((model_id, path))
        return (model_id == PINNED_BASE_MODEL_ID, "missing")

    monkeypatch.setattr(registry, "verify_pinned_snapshot", verify)
    with pytest.raises(ModelUnavailableError, match="Parakeet|parakeet"):
        verify_shipped_payload(tmp_path)
    assert {item[0] for item in seen} == {
        PINNED_BASE_MODEL_ID,
        PINNED_PARAKEET_MODEL_ID,
    }


def test_shipped_payload_rejects_missing_runtime_core_before_models(tmp_path, monkeypatch) -> None:
    import dcent_voice.asr.model_registry as registry

    _runtime_payload(tmp_path)
    (tmp_path / "models").mkdir()
    monkeypatch.setattr(
        registry, "verify_pinned_snapshot", lambda _path, _model_id: (True, "verified")
    )
    verify_shipped_payload(tmp_path)

    (tmp_path / "_internal" / "base_library.zip").unlink()
    with pytest.raises(ModelUnavailableError, match="base library archive"):
        verify_shipped_payload(tmp_path)


def test_payload_staging_preserves_nested_runtime_archives_and_excludes_root_release_archives(
    tmp_path, monkeypatch
) -> None:
    import dcent_voice.asr.model_registry as registry

    manifests = {
        PINNED_BASE_MODEL_ID: {
            "files": {
                "weights.bin": {
                    "size": 4,
                    "sha256": hashlib.sha256(b"base").hexdigest(),
                }
            }
        },
        PINNED_PARAKEET_MODEL_ID: {
            "files": {
                "weights.bin": {
                    "size": 8,
                    "sha256": hashlib.sha256(b"parakeet").hexdigest(),
                }
            }
        },
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", manifests.get)
    source = _runtime_payload(tmp_path / "source")
    parakeet = source / "models" / "parakeet-tdt-0.6b-v3"
    whisper = runtime_model_path(PINNED_BASE_MODEL_ID, root=source / "models")
    parakeet.mkdir(parents=True)
    whisper.mkdir(parents=True)
    (parakeet / "weights.bin").write_bytes(b"parakeet")
    (whisper / "weights.bin").write_bytes(b"base")
    (source / "old-release.zip").write_bytes(b"do not recursively bundle releases")

    staged = stage_verified_payload(source, tmp_path / "staged")

    assert (staged / "_internal" / "base_library.zip").read_bytes().startswith(b"fixture:")
    assert not (staged / "old-release.zip").exists()
    verify_shipped_payload(staged)


def test_pinned_verifier_rejects_hardlinks_and_staging_copies_bound_bytes(
    tmp_path, monkeypatch
) -> None:
    import dcent_voice.asr.model_registry as registry

    content = b"trusted snapshot"
    manifest = {
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _id: manifest)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(content)
    linked = tmp_path / "linked"
    linked.mkdir()
    os.link(outside, linked / "weights.bin")
    valid, detail = verify_pinned_snapshot(linked, "test/model")
    assert valid is False
    assert "hard-linked" in detail

    source = tmp_path / "source"
    source.mkdir()
    (source / "weights.bin").write_bytes(content)
    staged = stage_verified_snapshot(source, tmp_path / "staged", "test/model")
    assert verify_pinned_snapshot(staged, "test/model") == (True, "verified")
    assert os.stat(staged / "weights.bin").st_nlink == 1


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate stream contract")
def test_pinned_verifier_rejects_ntfs_alternate_data_streams(tmp_path, monkeypatch) -> None:
    import dcent_voice.asr.model_registry as registry

    content = b"trusted"
    manifest = {
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _id: manifest)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    weights = snapshot / "weights.bin"
    weights.write_bytes(content)
    with open(f"{weights}:secret.payload", "wb") as stream:
        stream.write(b"undeclared")
    valid, detail = verify_pinned_snapshot(snapshot, "test/model")
    assert valid is False
    assert "alternate data stream" in detail


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate stream contract")
def test_pinned_verifier_strips_mark_of_the_web_after_the_hash_verifies(
    tmp_path, monkeypatch
) -> None:
    """Explorer stamps Zone.Identifier on everything it extracts from a ZIP.

    That stream carries no file data, so it cannot change a SHA-256. Rejecting it
    made every portable-ZIP install fail verification at install *and* at every
    model load. It is now stripped once the bytes verify — never before.
    """
    import dcent_voice.asr.model_registry as registry

    content = b"trusted"
    manifest = {
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _id: manifest)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    weights = snapshot / "weights.bin"
    weights.write_bytes(content)
    with open(f"{weights}:Zone.Identifier", "w", encoding="ascii") as stream:
        stream.write("[ZoneTransfer]\r\nZoneId=3\r\n")
    assert registry._windows_named_streams(weights)

    valid, detail = verify_pinned_snapshot(snapshot, "test/model")
    assert (valid, detail) == (True, "verified")
    assert registry._windows_named_streams(weights) == ()
    assert weights.read_bytes() == content


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate stream contract")
def test_mark_of_the_web_tolerance_does_not_admit_other_streams(tmp_path, monkeypatch) -> None:
    """Zone.Identifier is the only tolerated stream, even alongside it."""
    import dcent_voice.asr.model_registry as registry

    content = b"trusted"
    manifest = {
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _id: manifest)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    weights = snapshot / "weights.bin"
    weights.write_bytes(content)
    with open(f"{weights}:Zone.Identifier", "w", encoding="ascii") as stream:
        stream.write("[ZoneTransfer]\r\nZoneId=3\r\n")
    with open(f"{weights}:secret.payload", "wb") as stream:
        stream.write(b"undeclared")

    valid, detail = verify_pinned_snapshot(snapshot, "test/model")
    assert valid is False
    assert "alternate data stream" in detail
    # The foreign stream is rejected, and nothing was stripped on the way out.
    assert len(registry._windows_named_streams(weights)) == 2


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate stream contract")
@pytest.mark.parametrize(
    ("size", "tolerated"),
    [(26, True), (4096, True), (4097, False), (64 * 1024, False)],
)
def test_tolerated_zone_identifier_stream_is_size_bounded(
    tmp_path, monkeypatch, size, tolerated
) -> None:
    """The stream's bytes are never hashed, so tolerating it unbounded is unsafe.

    A genuine Mark-of-the-Web is a short INI stanza. Anything larger is a payload
    riding alongside a snapshot that claims to be byte-exact, and fails like any
    other alternate data stream.
    """
    import dcent_voice.asr.model_registry as registry

    content = b"trusted"
    manifest = {
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _id: manifest)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    weights = snapshot / "weights.bin"
    weights.write_bytes(content)
    with open(f"{weights}:Zone.Identifier", "wb") as stream:
        stream.write(b"z" * size)
    assert registry._windows_named_streams_with_sizes(weights)[0][1] == size

    valid, detail = verify_pinned_snapshot(snapshot, "test/model")

    if tolerated:
        assert (valid, detail) == (True, "verified")
        assert registry._windows_named_streams(weights) == ()
    else:
        assert valid is False
        assert "alternate data stream" in detail
        assert str(registry._MAX_ZONE_IDENTIFIER_BYTES) in detail
        # An oversized stream is rejected, never silently stripped.
        assert registry._windows_named_streams(weights) != ()
    # The file's own bytes are untouched either way.
    assert weights.read_bytes() == content


def test_reparse_rejection_names_the_realistic_cause(tmp_path, monkeypatch) -> None:
    """OneDrive Files-On-Demand, not an attacker, is what a user actually hits."""
    import dcent_voice.asr.model_registry as registry

    monkeypatch.setattr(registry, "_is_reparse", lambda _info: True)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    valid, detail = registry._safe_directory(snapshot)
    assert valid is False
    assert "OneDrive Files-On-Demand" in detail
    assert "/D=" in detail

    weights = snapshot / "weights.bin"
    weights.write_bytes(b"trusted")
    valid, detail = registry._safe_regular_file(weights)
    assert valid is False
    assert "OneDrive Files-On-Demand" in detail
    assert "/D=" in detail


def test_verifier_detects_replacement_between_enumeration_and_open(tmp_path, monkeypatch) -> None:
    import dcent_voice.asr.model_registry as registry

    content = b"trusted"
    manifest = {
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _id: manifest)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    weights = snapshot / "weights.bin"
    weights.write_bytes(content)
    real_open = registry._open_bound_read
    replaced = False

    def replace_then_open(path: Path) -> int:
        nonlocal replaced
        if not replaced:
            replaced = True
            path.unlink()
            path.write_bytes(b"hostile")
        return real_open(path)

    monkeypatch.setattr(registry, "_open_bound_read", replace_then_open)
    valid, detail = verify_pinned_snapshot(snapshot, "test/model")
    assert valid is False
    assert "changed" in detail or "mismatch" in detail


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing contract")
def test_verified_loader_handles_deny_write_delete_replacement(tmp_path, monkeypatch) -> None:
    import dcent_voice.asr.model_registry as registry

    content = b"trusted"
    manifest = {
        "files": {
            "weights.bin": {
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }
    monkeypatch.setattr(registry, "pinned_model_manifest", lambda _id: manifest)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    weights = snapshot / "weights.bin"
    weights.write_bytes(content)
    with verified_snapshot_lock(snapshot, "test/model"):
        with pytest.raises(PermissionError):
            weights.write_bytes(b"hostile")
        with pytest.raises(PermissionError):
            weights.unlink()
    weights.write_bytes(content)


def test_clean_cache_offline_base_fails_actionably_without_network(tmp_path, monkeypatch) -> None:
    calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("network is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(ModelUnavailableError, match="never downloads.*during dictation"):
        resolve_faster_whisper_model("base", root=tmp_path)
    assert calls == 0


def test_corrupt_pinned_base_is_rejected(tmp_path) -> None:
    snapshot = runtime_model_path(PINNED_BASE_MODEL_ID, root=tmp_path)
    _snapshot(snapshot, marker=b"tampered")
    valid, detail = verify_pinned_snapshot(snapshot, PINNED_BASE_MODEL_ID)
    assert valid is False
    assert "mismatch" in detail or "missing" in detail
    with pytest.raises(ModelUnavailableError, match="Corrupt candidates"):
        resolve_faster_whisper_model("base", root=tmp_path)


def test_install_bundle_copies_records_and_resolves_snapshot(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    model_id = "Systran/faster-distil-whisper-small.en"
    source = _snapshot(model_path(bundle, model_id))
    manifest = build_offline_bundle_manifest(bundle, model_ids=(model_id,))
    manifest_path = write_offline_bundle_manifest(bundle, manifest)
    runtime = tmp_path / "runtime models"

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("offline installation must not access the network")
        ),
    )
    installed = install_models_from_bundle(manifest_path, root=runtime)

    destination = runtime_model_path(model_id, root=runtime)
    assert installed[0].model_id == model_id
    assert destination != source
    assert valid_faster_whisper_snapshot(destination)
    assert resolve_faster_whisper_model("distil-small.en", root=runtime) == str(
        destination.resolve()
    )
    registry = json.loads((runtime / REGISTRY_FILENAME).read_text(encoding="utf-8"))
    assert registry["models"][0]["path"] == (
        "faster-whisper/Systran--faster-distil-whisper-small.en"
    )


def test_install_bundle_rejects_snapshot_path_escape(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest_path = bundle / "dcent-voice-offline-bundle.json"
    manifest_path.write_text(
        json.dumps(
            {
                "remoteUrls": [],
                "models": [
                    {
                        "provider": "faster-whisper",
                        "modelId": "Systran/faster-whisper-tiny",
                        "path": "../outside",
                        "present": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes the bundle"):
        install_models_from_bundle(manifest_path, root=tmp_path / "runtime")


@pytest.mark.parametrize("model_id", ["", ".", "..", "../outside", "/absolute", r"C:\\temp"])
def test_model_directory_name_rejects_path_like_identifiers(model_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid model ID"):
        safe_model_dir_name(model_id)


def test_install_bundle_rejects_malicious_model_id_without_deleting_model_root(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    _snapshot(bundle / "models" / "safe")
    manifest_path = bundle / "dcent-voice-offline-bundle.json"
    manifest_path.write_text(
        json.dumps(
            {
                "remoteUrls": [],
                "models": [
                    {
                        "provider": "faster-whisper",
                        "modelId": "..",
                        "path": "models/safe",
                        "present": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    model_root = runtime / "faster-whisper"
    model_root.mkdir(parents=True)
    sentinel = model_root / "keep-me.txt"
    sentinel.write_text("do not delete", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid model ID"):
        install_models_from_bundle(manifest_path, root=runtime)

    assert sentinel.read_text(encoding="utf-8") == "do not delete"
