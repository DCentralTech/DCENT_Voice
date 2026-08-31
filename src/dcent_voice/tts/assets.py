# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Consent-gated TTS model-asset acquisition.

Kokoro weights are NOT bundled (see THIRD-PARTY-LICENSES.md). Fetching them is a
real egress event: the module is local, but downloading contacts an upstream
host, so the data-flow class is ``SERVER_EGRESS`` and the pull is gated on the
same :class:`~dcent_voice.privacy.ConsentLedger` used for cloud providers, under
the ``voice.model.download`` capability key.

This mirrors the faster-whisper asset handling (``scripts/download_models.py`` /
``package_bundle.py``) and adds what that path lacks and this one requires: a
**SHA-256 checksum** verified after download, and a **license note** written next
to the asset. No network happens at import; ``download_asset`` takes an injectable
``fetch`` so CI validates the checksum/consent/egress logic offline.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from dcent_voice.config import user_config_dir
from dcent_voice.privacy import ConsentLedger, ConsentRequired, EgressLog

# Consent key + capability under which every model download is recorded. Matches
# sovereignty.MODEL_DOWNLOAD_CAPABILITY so the DVAP `voice.model.download` block
# and the consent ledger agree.
MODEL_DOWNLOAD_KEY = "voice.model.download"


@dataclass(frozen=True)
class TtsModelAsset:
    """One downloadable model file with its integrity + license metadata."""

    key: str  # stable id, e.g. "kokoro-v1.0.onnx"
    filename: str  # on-disk name under the backend's model dir
    url: str
    sha256: str
    license: str  # SPDX id, e.g. "Apache-2.0"
    license_url: str = ""
    note: str = ""


# Known assets. URLs/checksums are pinned so a tampered or truncated download
# fails verification. Kokoro-82M is Apache-2.0 (ADR V003 default). XTTS and
# Piper are deliberately absent from the public beta pending license review.
KOKORO_ASSETS: tuple[TtsModelAsset, ...] = (
    TtsModelAsset(
        key="kokoro-v1.0.onnx",
        filename="kokoro-v1.0.onnx",
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
        sha256="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        license="Apache-2.0",
        license_url="https://huggingface.co/hexgrad/Kokoro-82M",
        note="Kokoro-82M ONNX weights (kokoro-onnx runtime, onnxruntime).",
    ),
    TtsModelAsset(
        key="kokoro-voices-v1.0.bin",
        filename="voices-v1.0.bin",
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        license="Apache-2.0",
        license_url="https://huggingface.co/hexgrad/Kokoro-82M",
        note="Kokoro voice embeddings.",
    ),
)

ASSETS_BY_BACKEND: dict[str, tuple[TtsModelAsset, ...]] = {
    "kokoro": KOKORO_ASSETS,
}


class ChecksumError(RuntimeError):
    """Raised when a downloaded asset does not match its pinned SHA-256."""


class DownloadTransportError(RuntimeError):
    """Raised when a model download would use an unsafe transport."""


def tts_model_dir(backend: str, *, root: Path | None = None) -> Path:
    """Directory holding a backend's downloaded model assets."""
    base = root or user_config_dir()
    return base / "models" / "tts" / backend


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def asset_present(asset: TtsModelAsset, dest_dir: Path) -> bool:
    """True if an asset exists and matches its required SHA-256 pin."""
    path = dest_dir / asset.filename
    if not path.exists() or not _has_valid_sha256_pin(asset.sha256):
        return False
    return sha256_file(path) == asset.sha256


def backend_assets_present(backend: str, *, root: Path | None = None) -> bool:
    """True if every asset a backend needs is on disk (no network)."""
    assets = ASSETS_BY_BACKEND.get(backend, ())
    if not assets:
        return False
    dest = tts_model_dir(backend, root=root)
    return all(asset_present(asset, dest) for asset in assets)


def install_backend_assets(
    backend: str,
    *,
    ledger: ConsentLedger,
    egress_log: EgressLog | None = None,
    fetch: Callable[[str], bytes] | None = None,
    root: Path | None = None,
    force: bool = False,
) -> tuple[Path, ...]:
    """Install every pinned asset for a supported TTS backend.

    Only a currently public-beta backend identifier is accepted. Callers cannot
    turn this consented product path into an arbitrary URL or filesystem downloader.
    Existing verified assets are retained, so retries resume at the first missing
    or invalid file and do not produce duplicate egress records for good files.
    """

    normalized = backend.strip().lower()
    if normalized not in ASSETS_BY_BACKEND:
        raise ValueError(
            "TTS backend must be 'kokoro'; Piper is deferred pending compatible voice licensing."
        )
    assets = ASSETS_BY_BACKEND[normalized]
    destination = tts_model_dir(normalized, root=root)
    fetch_asset = fetch or default_fetch
    return tuple(
        download_asset(
            asset,
            destination,
            ledger=ledger,
            fetch=fetch_asset,
            egress_log=egress_log,
            force=force,
        )
        for asset in assets
    )


def download_asset(
    asset: TtsModelAsset,
    dest_dir: Path,
    *,
    ledger: ConsentLedger,
    fetch: Callable[[str], bytes],
    egress_log: EgressLog | None = None,
    force: bool = False,
) -> Path:
    """Download one asset after consent, verify its checksum, note its license.

    Raises :class:`~dcent_voice.privacy.ConsentRequired` if the user has not
    granted ``voice.model.download`` and :class:`ChecksumError` on mismatch. The
    egress is recorded (``SERVER_EGRESS``) so it shows in the privacy ledger.
    """

    path = dest_dir / asset.filename
    if not _has_valid_sha256_pin(asset.sha256):
        raise ChecksumError(f"{asset.key}: a 64-character SHA-256 pin is required")
    if not force and asset_present(asset, dest_dir):
        _write_license_note(asset, dest_dir)
        return path

    if not ledger.has_consent(MODEL_DOWNLOAD_KEY, payload_type="model"):
        raise ConsentRequired((MODEL_DOWNLOAD_KEY,))
    if egress_log is None:
        raise RuntimeError("TTS model download requires a metadata-only egress log.")

    # Record the attempt before touching the network.  A zero byte count means
    # that the transfer has not completed; this leaves an auditable event even
    # when DNS/TLS/HTTP or checksum validation fails.  If the audit log itself
    # cannot be written, fail closed and do not invoke the fetcher.
    egress_log.record(MODEL_DOWNLOAD_KEY, payload_type="model", byte_count=0)
    data = fetch(asset.url)  # SERVER_EGRESS: contacts an upstream host.
    actual = hashlib.sha256(data).hexdigest()
    if actual != asset.sha256:
        raise ChecksumError(f"{asset.key}: expected sha256 {asset.sha256}, got {actual}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    _write_license_note(asset, dest_dir)

    egress_log.record(MODEL_DOWNLOAD_KEY, payload_type="model", byte_count=len(data))
    return path


def default_fetch(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bytes:  # pragma: no cover - network path exercised with MockTransport
    """Fetch bytes over HTTPS without permitting redirect downgrades.

    Redirects are followed explicitly so every destination is checked before
    the next request.  Cross-host HTTPS redirects remain supported because
    pinned release assets commonly use them, but HTTP and malformed targets
    fail closed.
    """
    import httpx

    redirect_statuses = {301, 302, 303, 307, 308}
    maximum_redirects = 10
    current = httpx.URL(url)
    _require_https_url(current)

    with httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=120.0,
        transport=transport,
    ) as client:
        for redirect_count in range(maximum_redirects + 1):
            response = client.get(current)
            if response.status_code not in redirect_statuses:
                response.raise_for_status()
                return response.content

            location = response.headers.get("location")
            if not location:
                raise DownloadTransportError("Model download redirect is missing Location.")
            if redirect_count >= maximum_redirects:
                raise DownloadTransportError("Model download exceeded the HTTPS redirect limit.")
            destination = response.url.join(location)
            _require_https_url(destination)
            current = destination

    raise DownloadTransportError("Model download did not reach a final HTTPS response.")


def _require_https_url(url: httpx.URL) -> None:
    if url.scheme.lower() != "https" or not url.host:
        raise DownloadTransportError("Model downloads and redirects must use HTTPS.")
    if url.username or url.password:
        raise DownloadTransportError("Model download URLs must not contain credentials.")


def _write_license_note(asset: TtsModelAsset, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    note = dest_dir / f"{asset.filename}.LICENSE.txt"
    if note.exists():
        return
    lines = [
        f"Asset: {asset.key}",
        f"License: {asset.license}",
    ]
    if asset.license_url:
        lines.append(f"License URL: {asset.license_url}")
    if asset.note:
        lines.append(f"Note: {asset.note}")
    lines.append(f"Source: {asset.url}")
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _has_valid_sha256_pin(value: str) -> bool:
    """Return whether a checksum is a canonical 64-character SHA-256 digest."""
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
