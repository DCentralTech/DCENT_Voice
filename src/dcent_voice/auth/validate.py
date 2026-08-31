# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Live API-key validation.

Connecting a cloud provider used to store the key blindly and pop an optimistic
"Connected" toast. This pings the provider's cheapest authenticated endpoint so
the UI can tell the user immediately whether the key actually works, before the
first dictation fails.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from dcent_voice.llm.openai_compat import DEFAULT_BASE_URLS

# Providers whose /models endpoint takes a Bearer token (OpenAI-compatible).
_BEARER_MODELS = {"openai", "groq", "xai"}
_LIVE_VALIDATION_PROVIDERS = _BEARER_MODELS | {"anthropic", "deepgram"}
_PROVIDER_HOSTS = {
    "openai": "api.openai.com",
    "groq": "api.groq.com",
    "xai": "api.x.ai",
    "anthropic": "api.anthropic.com",
    "deepgram": "api.deepgram.com",
}

CREDENTIAL_EGRESS_PAYLOAD = "credentials"


class CredentialEndpointError(ValueError):
    """Raised before wire I/O for a non-canonical credential destination."""


def credential_consent_key(provider: str) -> str:
    """Return the consent/audit key for provider authentication traffic."""

    return f"auth:{provider.strip().lower()}"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    detail: str
    # False when the provider could not be reached at all (offline, DNS, 5xx).
    # Only a reachable provider can actually *reject* a key — callers should
    # save the key unverified rather than discard it when reachable is False.
    reachable: bool = True


def validate_api_key(
    provider: str,
    api_key: str,
    *,
    timeout_s: float = 8.0,
    authorize_egress: Callable[[], None] | None = None,
) -> ValidationResult:
    """Validate a key after the caller authorizes and audits credential egress.

    ``authorize_egress`` is invoked immediately before a live provider request.
    It should bind explicit consent to ``auth:<provider>`` / ``credentials`` and
    append a metadata-only attempt record.  Keeping this callback inside the
    validation boundary makes direct callers fail closed instead of silently
    bypassing the settings privacy controls.
    """

    provider = provider.lower()
    if not api_key or not api_key.strip():
        return ValidationResult(False, "Enter an API key first.")
    if provider in _LIVE_VALIDATION_PROVIDERS:
        if authorize_egress is None:
            return ValidationResult(
                False,
                "Explicit credential egress consent is required before verification.",
            )
        authorize_egress()
    try:
        response = _probe(provider, api_key.strip(), timeout_s)
    except (httpx.HTTPError, CredentialEndpointError) as exc:
        return ValidationResult(
            False, f"Could not reach {provider}: {type(exc).__name__}", reachable=False
        )
    if response is None:
        return ValidationResult(True, "Saved. No live check is available for this provider.")
    if 200 <= response.status_code < 300:
        return ValidationResult(True, "Key verified.")
    if response.status_code in (401, 403):
        return ValidationResult(False, "The provider rejected this key.")
    return ValidationResult(
        False, f"Unexpected response from {provider} ({response.status_code}).", reachable=False
    )


def _probe(provider: str, api_key: str, timeout_s: float) -> httpx.Response | None:
    if provider in _BEARER_MODELS:
        base = DEFAULT_BASE_URLS.get(provider, "").rstrip("/")
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "anthropic":
        url = "https://api.anthropic.com/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif provider == "deepgram":
        url = "https://api.deepgram.com/v1/projects"
        headers = {"Authorization": f"Token {api_key}"}
    else:
        return None

    _require_provider_https_endpoint(provider, url)
    with httpx.Client(
        timeout=timeout_s,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        return client.get(url, headers=headers)


def _require_provider_https_endpoint(provider: str, url: str) -> None:
    """Reject any credential destination other than the canonical provider host."""

    expected_host = _PROVIDER_HOSTS[provider]
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CredentialEndpointError("Credential validation endpoint is malformed.") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != 443)
        or parsed.fragment
    ):
        raise CredentialEndpointError(
            "Credential validation requires the canonical HTTPS endpoint."
        )
