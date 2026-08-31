# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from dcent_voice.privacy import ConsentLedger, ConsentRequired, EgressLog
from dcent_voice.tts.assets import (
    ASSETS_BY_BACKEND,
    MODEL_DOWNLOAD_KEY,
    ChecksumError,
    DownloadTransportError,
    TtsModelAsset,
    asset_present,
    default_fetch,
    download_asset,
    install_backend_assets,
    tts_model_dir,
)

_DATA = b"fake kokoro weights \x00\x01\x02"
_SHA = hashlib.sha256(_DATA).hexdigest()


class _CountingTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.hits = 0
        super().__init__(("127.0.0.1", 0), _CountingHandler)


class _CountingHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _CountingTcpServer)
        server.hits += 1


@contextmanager
def _counting_listener() -> Iterator[_CountingTcpServer]:
    server = _CountingTcpServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _asset(sha: str = _SHA) -> TtsModelAsset:
    return TtsModelAsset(
        key="test-model.onnx",
        filename="test-model.onnx",
        url="https://example.invalid/test-model.onnx",
        sha256=sha,
        license="Apache-2.0",
        license_url="https://example.invalid/license",
        note="Test asset.",
    )


def test_download_requires_consent(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    egress = EgressLog(tmp_path / "egress.jsonl")
    with pytest.raises(ConsentRequired) as excinfo:
        download_asset(
            _asset(),
            tmp_path / "models",
            ledger=ledger,
            fetch=lambda url: _DATA,
            egress_log=egress,
        )
    assert MODEL_DOWNLOAD_KEY in excinfo.value.missing
    assert egress.tail() == []


def test_download_rejects_consent_for_the_wrong_payload_type(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="text")
    egress = EgressLog(tmp_path / "egress.jsonl")

    with pytest.raises(ConsentRequired):
        download_asset(
            _asset(),
            tmp_path / "models",
            ledger=ledger,
            fetch=lambda url: _DATA,
            egress_log=egress,
        )
    assert egress.tail() == []


def test_download_after_consent_verifies_and_writes_license(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="model")
    egress = EgressLog(tmp_path / "egress.jsonl")
    dest = tmp_path / "models"

    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return _DATA

    path = download_asset(_asset(), dest, ledger=ledger, fetch=fetch, egress_log=egress)

    assert path.read_bytes() == _DATA
    assert asset_present(_asset(), dest)
    # License note written next to the asset.
    note = dest / "test-model.onnx.LICENSE.txt"
    assert "Apache-2.0" in note.read_text(encoding="utf-8")
    # Attempt is recorded before wire, followed by transfer completion metadata.
    tail = egress.tail()
    assert [entry.provider_key for entry in tail] == [MODEL_DOWNLOAD_KEY] * 2
    assert [entry.payload_type for entry in tail] == ["model", "model"]
    assert [entry.byte_count for entry in tail] == [0, len(_DATA)]
    assert calls == ["https://example.invalid/test-model.onnx"]


def test_checksum_mismatch_raises_and_leaves_no_asset(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="model")
    dest = tmp_path / "models"
    bad = _asset(sha="0" * 64)
    egress = EgressLog(tmp_path / "egress.jsonl")

    with pytest.raises(ChecksumError):
        download_asset(bad, dest, ledger=ledger, fetch=lambda url: _DATA, egress_log=egress)

    assert not (dest / "test-model.onnx").exists()
    entries = egress.tail()
    assert len(entries) == 1
    assert entries[0].provider_key == MODEL_DOWNLOAD_KEY
    assert entries[0].payload_type == "model"
    assert entries[0].byte_count == 0


def test_network_failure_is_audited_before_fetch(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="model")
    egress = EgressLog(tmp_path / "egress.jsonl")

    def unavailable(_url: str) -> bytes:
        entries = egress.tail()
        assert len(entries) == 1
        assert entries[0].byte_count == 0
        raise OSError("network unavailable")

    with pytest.raises(OSError, match="network unavailable"):
        download_asset(
            _asset(),
            tmp_path / "models",
            ledger=ledger,
            fetch=unavailable,
            egress_log=egress,
        )

    entries = egress.tail()
    assert [(entry.provider_key, entry.payload_type, entry.byte_count) for entry in entries] == [
        (MODEL_DOWNLOAD_KEY, "model", 0)
    ]


def test_download_without_egress_log_fails_closed_before_fetch(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="model")
    calls = 0

    def fetch(_url: str) -> bytes:
        nonlocal calls
        calls += 1
        return _DATA

    with pytest.raises(RuntimeError, match="metadata-only egress log"):
        download_asset(_asset(), tmp_path / "models", ledger=ledger, fetch=fetch)

    assert calls == 0


def test_download_rejects_missing_checksum_before_egress(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="model")
    calls = 0
    egress = EgressLog(tmp_path / "egress.jsonl")

    def fetch(_url: str) -> bytes:
        nonlocal calls
        calls += 1
        return _DATA

    with pytest.raises(ChecksumError, match="SHA-256 pin is required"):
        download_asset(
            _asset(""),
            tmp_path / "models",
            ledger=ledger,
            fetch=fetch,
            egress_log=egress,
        )

    assert calls == 0
    assert egress.tail() == []


def test_shipped_tts_assets_have_sha256_pins_and_immutable_sources() -> None:
    assets = [asset for backend in ASSETS_BY_BACKEND.values() for asset in backend]

    assert assets
    assert tuple(ASSETS_BY_BACKEND) == ("kokoro",)
    assert all(len(asset.sha256) == 64 for asset in assets)
    assert all(set(asset.sha256) <= set("0123456789abcdef") for asset in assets)
    assert all("/resolve/main/" not in asset.url for asset in assets)
    assert all("piper-voices" not in asset.url for asset in assets)


def test_download_is_idempotent_when_present(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")
    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="model")
    dest = tmp_path / "models"
    egress = EgressLog(tmp_path / "egress.jsonl")

    calls = 0

    def fetch(url: str) -> bytes:
        nonlocal calls
        calls += 1
        return _DATA

    download_asset(_asset(), dest, ledger=ledger, fetch=fetch, egress_log=egress)
    # Second call sees the verified asset and does not re-fetch.
    download_asset(_asset(), dest, ledger=ledger, fetch=fetch, egress_log=egress)
    assert calls == 1
    assert [entry.byte_count for entry in egress.tail()] == [0, len(_DATA)]


def test_default_fetch_rejects_https_to_http_redirect_before_downgraded_wire() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://mirror.invalid/model.bin"})

    with pytest.raises(DownloadTransportError, match="must use HTTPS"):
        default_fetch(
            "https://models.example.invalid/model.bin",
            transport=httpx.MockTransport(handler),
        )

    assert requests == ["https://models.example.invalid/model.bin"]


def test_default_fetch_rejects_initial_http_before_wire() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=_DATA)

    with pytest.raises(DownloadTransportError, match="must use HTTPS"):
        default_fetch(
            "http://models.example.invalid/model.bin",
            transport=httpx.MockTransport(handler),
        )

    assert requests == []


def test_default_fetch_follows_relative_https_redirect() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/model.bin":
            return httpx.Response(307, headers={"location": "/release/model.bin"})
        return httpx.Response(200, content=_DATA)

    data = default_fetch(
        "https://models.example.invalid/model.bin",
        transport=httpx.MockTransport(handler),
    )

    assert data == _DATA
    assert requests == [
        "https://models.example.invalid/model.bin",
        "https://models.example.invalid/release/model.bin",
    ]


def test_default_fetch_ignores_hostile_ambient_proxy(monkeypatch) -> None:
    with _counting_listener() as destination, _counting_listener() as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(name, proxy_url)
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(httpx.TransportError):
            default_fetch(f"https://127.0.0.1:{destination.server_address[1]}/model.bin")

    assert destination.hits == 1
    assert proxy.hits == 0


def test_tts_model_dir_layout(tmp_path) -> None:
    path = tts_model_dir("kokoro", root=tmp_path)
    assert path == tmp_path / "models" / "tts" / "kokoro"


def test_install_backend_assets_requires_consent_and_is_idempotent(tmp_path, monkeypatch) -> None:
    from dcent_voice.tts import assets as assets_module

    first = _asset()
    second = TtsModelAsset(
        key="test-voices.bin",
        filename="test-voices.bin",
        url="https://example.invalid/test-voices.bin",
        sha256=_SHA,
        license="Apache-2.0",
    )
    monkeypatch.setitem(assets_module.ASSETS_BY_BACKEND, "kokoro", (first, second))
    ledger = ConsentLedger(tmp_path / "consent.json")
    egress = EgressLog(tmp_path / "egress.jsonl")
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return _DATA

    with pytest.raises(ConsentRequired):
        install_backend_assets(
            "kokoro", ledger=ledger, egress_log=egress, fetch=fetch, root=tmp_path
        )
    assert calls == []

    ledger.grant(MODEL_DOWNLOAD_KEY, payload_type="model")
    paths = install_backend_assets(
        "kokoro", ledger=ledger, egress_log=egress, fetch=fetch, root=tmp_path
    )
    assert [path.name for path in paths] == ["test-model.onnx", "test-voices.bin"]
    assert calls == [first.url, second.url]
    assert [entry.byte_count for entry in egress.tail()] == [0, len(_DATA), 0, len(_DATA)]

    install_backend_assets("kokoro", ledger=ledger, egress_log=egress, fetch=fetch, root=tmp_path)
    assert calls == [first.url, second.url]
    assert [entry.byte_count for entry in egress.tail()] == [0, len(_DATA), 0, len(_DATA)]


def test_install_backend_assets_rejects_unknown_backend(tmp_path) -> None:
    with pytest.raises(ValueError, match="kokoro.*Piper"):
        install_backend_assets("auto", ledger=ConsentLedger(tmp_path / "consent.json"))


def test_install_backend_assets_refuses_deferred_piper_without_egress(tmp_path) -> None:
    ledger = ConsentLedger(tmp_path / "consent.json")

    with pytest.raises(ValueError, match="Piper is deferred"):
        install_backend_assets("piper", ledger=ledger, fetch=lambda _url: _DATA, root=tmp_path)

    assert not ledger.path.exists()
