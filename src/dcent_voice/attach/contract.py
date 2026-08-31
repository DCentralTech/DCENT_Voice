# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Stable versioned headless attach contract (no tray, Settings, or hotkeys)."""

from __future__ import annotations

from typing import Any

from dcent_voice.asr.base import Locality

# Integer string. Bump only for breaking attach-field changes. App __version__
# is the product release; this is the embedder contract.
API_VERSION = "1"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_STATUS_CODES = {
    401: ("unauthorized", False),
    413: ("payload_too_large", False),
    422: ("invalid_request", False),
    503: ("busy", True),
}


def headless_surface() -> dict[str, Any]:
    """Flags every attach payload must advertise."""
    return {
        "api_version": API_VERSION,
        "headless": True,
        "requires_tray": False,
        "requires_hotkeys": False,
        "requires_settings_ui": False,
    }


def error_object(
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": bool(retryable)}


def error_payload(
    status_code: int,
    detail: Any,
    *,
    code: str | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """Machine-readable envelope that keeps FastAPI ``detail`` for compatibility."""
    default_code, default_retry = _STATUS_CODES.get(status_code, ("error", False))
    resolved_code = code or default_code
    if status_code == 422 and code is None and not isinstance(detail, str):
        resolved_code = "invalid_request"
    message = detail if isinstance(detail, str) else "Invalid request"
    resolved_retry = default_retry if retryable is None else bool(retryable)
    return {
        "detail": detail,
        "error": error_object(resolved_code, message, retryable=resolved_retry),
    }


def probe_hardware(asr: Any | None = None) -> dict[str, Any]:
    """Local hardware-auto snapshot. Never phones home."""
    spec = getattr(asr, "spec", None)
    provider = getattr(spec, "provider", type(asr).__name__ if asr is not None else "unknown")
    model = getattr(spec, "model", "") or ""
    compute = getattr(spec, "compute_type", None) or "int8"
    device = "cpu"
    cuda_ready = False
    try:
        from dcent_voice.asr.faster_whisper_provider import (
            cuda_runtime_ready,
            resolve_device_compute,
        )

        cuda_ready = bool(cuda_runtime_ready())
        if spec is not None and getattr(spec, "provider", "") == "faster-whisper":
            device, compute = resolve_device_compute(spec)
        elif getattr(spec, "provider", "") == "parakeet":
            device, compute = "cpu", getattr(spec, "compute_type", None) or "int8"
        elif cuda_ready and str(compute) in {"default", "auto", ""}:
            device, compute = "cuda", "float16"
    except Exception:
        cuda_ready = False
    locality = getattr(asr, "locality", None)
    return {
        "auto": True,
        "cuda_ready": cuda_ready,
        "active_device": device,
        "active_compute": compute,
        "provider": provider,
        "model": model,
        "local": locality is Locality.LOCAL if locality is not None else True,
    }


def model_loaded(asr: Any | None) -> bool:
    if asr is None:
        return False
    if getattr(asr, "_model", None) is not None:
        return True
    loaded = getattr(asr, "loaded", None)
    return bool(loaded) if isinstance(loaded, bool) else False
