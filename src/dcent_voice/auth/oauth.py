# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Implement OAuth device and PKCE authentication flows."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from dcent_voice.auth.base import AuthMode, AuthStrategy, ProviderAccount, _delete_all
from dcent_voice.privacy import ConsentRequired

# Per-provider OAuth device-code configuration. xAI/Grok exposes an OAuth 2.0
# device flow via accounts.x.ai tied to a SuperGrok / X Premium+ subscription.
# NOTE: `client_id` must be a client registered with the provider — set the real
# value once D-Central registers a DCENT_Voice OAuth client with xAI; until then
# the "Sign in" flow surfaces the provider's error and API-key connect is used.
OAUTH_CONFIGS: dict[str, dict[str, str]] = {
    "xai": {
        "device_endpoint": "https://accounts.x.ai/oauth2/device/code",
        "token_endpoint": "https://accounts.x.ai/oauth2/token",
        "client_id": "",
        "scope": "api",
    },
}


@dataclass(frozen=True)
class PKCEPair:
    verifier: str
    challenge: str


@dataclass(frozen=True)
class DeviceCodeGrant:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str = ""
    interval: int = 5
    expires_in: int = 600


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    token_type: str = "Bearer"
    refresh_token: str = ""
    expires_in: int | None = None


class OAuthAuth(AuthStrategy):
    """Authentication strategy for provider OAuth connections."""

    mode = AuthMode.OAUTH_PKCE

    def __init__(self, store) -> None:
        self.store = store

    def connect_token(
        self,
        provider: str,
        token: OAuthToken,
        *,
        label: str = "",
        mode: AuthMode = AuthMode.OAUTH_PKCE,
    ) -> ProviderAccount:
        if not token.access_token:
            raise ValueError("OAuth access token cannot be empty.")
        self.store.set_secret(provider, "oauth_access_token", token.access_token)
        self.store.set_secret(provider, "oauth_token_type", token.token_type)
        self.store.set_secret(provider, "oauth_auth_mode", mode.value)
        if token.refresh_token:
            self.store.set_secret(provider, "oauth_refresh_token", token.refresh_token)
        if token.expires_in is not None:
            self.store.set_secret(provider, "oauth_expires_in", str(token.expires_in))
            # Record when the token was issued so token() can tell that it has
            # expired instead of handing out a dead credential forever.
            self.store.set_secret(provider, "oauth_obtained_at", str(int(time.time())))
        if label:
            self.store.set_secret(provider, "label", label)
        return self.status(provider)

    def status(self, provider: str) -> ProviderAccount:
        access_token = self.store.get_secret(provider, "oauth_access_token")
        label = self.store.get_secret(provider, "label") or (
            "OAuth connected" if access_token else ""
        )
        mode_raw = self.store.get_secret(provider, "oauth_auth_mode") or self.mode.value
        try:
            mode = AuthMode(mode_raw)
        except ValueError:
            mode = self.mode
        return ProviderAccount(
            provider=provider, connected=bool(access_token), auth_mode=mode, label=label
        )

    def token(self, provider: str) -> OAuthToken | None:
        access_token = self.store.get_secret(provider, "oauth_access_token")
        if not access_token:
            return None
        expires_in_raw = self.store.get_secret(provider, "oauth_expires_in")
        expires_in = int(expires_in_raw) if expires_in_raw else None
        if self._expired(provider, expires_in):
            # An expired access token must not shadow a working API key in the
            # credential-precedence chain. (No refresh flow yet; the refresh
            # token stays stored for when one lands.)
            return None
        return OAuthToken(
            access_token=access_token,
            token_type=self.store.get_secret(provider, "oauth_token_type") or "Bearer",
            refresh_token=self.store.get_secret(provider, "oauth_refresh_token") or "",
            expires_in=expires_in,
        )

    def _expired(self, provider: str, expires_in: int | None) -> bool:
        if expires_in is None:
            return False
        obtained_raw = self.store.get_secret(provider, "oauth_obtained_at")
        if not obtained_raw:
            return False
        try:
            obtained_at = int(obtained_raw)
        except ValueError:
            return False
        # Slack so a token isn't used in its final moments — capped at half the
        # lifetime so short-lived tokens aren't declared dead on arrival.
        slack = min(60.0, expires_in / 2)
        return time.time() >= obtained_at + expires_in - slack

    def disconnect(self, provider: str) -> None:
        _delete_all(
            self.store,
            provider,
            (
                "oauth_access_token",
                "oauth_token_type",
                "oauth_auth_mode",
                "oauth_refresh_token",
                "oauth_expires_in",
                "oauth_obtained_at",
                "label",
            ),
        )


def create_pkce_pair() -> PKCEPair:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PKCEPair(verifier=verifier, challenge=challenge)


def build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{authorization_endpoint}?{query}"


def exchange_authorization_code(
    token_endpoint: str,
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    transport: httpx.BaseTransport | None = None,
    authorize_egress: Callable[[], None] | None = None,
) -> OAuthToken:
    """Exchange a PKCE code after authorizing credential egress."""

    response = _post_oauth_form(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        transport=transport,
        authorize_egress=authorize_egress,
    )
    return _token_from_json(response.json())


def request_device_code(
    device_endpoint: str,
    *,
    client_id: str,
    scope: str = "",
    transport: httpx.BaseTransport | None = None,
    authorize_egress: Callable[[], None] | None = None,
) -> DeviceCodeGrant:
    """Start RFC 8628 device authorization after authorizing credential egress."""

    response = _post_oauth_form(
        device_endpoint,
        data={"client_id": client_id, "scope": scope},
        transport=transport,
        authorize_egress=authorize_egress,
    )
    data = response.json()
    return DeviceCodeGrant(
        device_code=str(data.get("device_code", "")),
        user_code=str(data.get("user_code", "")),
        verification_uri=str(data.get("verification_uri") or data.get("verification_url") or ""),
        verification_uri_complete=str(data.get("verification_uri_complete", "")),
        interval=int(data.get("interval", 5)),
        expires_in=int(data.get("expires_in", 600)),
    )


def poll_device_token(
    token_endpoint: str,
    *,
    client_id: str,
    device_code: str,
    transport: httpx.BaseTransport | None = None,
    authorize_egress: Callable[[], None] | None = None,
) -> OAuthToken:
    """Poll an RFC 8628 token endpoint after authorizing credential egress."""

    response = _post_oauth_form(
        token_endpoint,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device_code,
        },
        transport=transport,
        authorize_egress=authorize_egress,
    )
    return _token_from_json(response.json())


def _post_oauth_form(
    endpoint: str,
    *,
    data: dict[str, str],
    transport: httpx.BaseTransport | None,
    authorize_egress: Callable[[], None] | None,
) -> httpx.Response:
    """POST credentials through an isolated, non-redirecting HTTPS client."""

    _require_https_endpoint(endpoint)
    with httpx.Client(
        transport=transport,
        timeout=15.0,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        # Keep the consent recheck/audit adjacent to the actual network call.
        # In particular, do not prepare another request or follow a redirect
        # after this authorization point.
        _authorize_oauth_egress(authorize_egress)
        response = client.post(endpoint, data=data)
        response.raise_for_status()
        return response


def _require_https_endpoint(endpoint: str) -> None:
    """Reject malformed or non-TLS OAuth endpoints before consent or I/O."""

    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("OAuth endpoint must be an absolute HTTPS URL.")
    if any(character.isspace() or ord(character) < 0x20 for character in endpoint):
        raise ValueError("OAuth endpoint must be an absolute HTTPS URL.")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        # Accessing port performs urllib's range and syntax validation.
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OAuth endpoint must be an absolute HTTPS URL.") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
        or "\\" in endpoint
    ):
        raise ValueError("OAuth endpoint must be an absolute HTTPS URL.")


def _authorize_oauth_egress(authorize_egress: Callable[[], None] | None) -> None:
    """Fail closed when a public OAuth network helper lacks an egress gate."""

    if authorize_egress is None:
        raise ConsentRequired(("OAuth credential egress",))
    authorize_egress()


def _token_from_json(data: dict[str, Any]) -> OAuthToken:
    return OAuthToken(
        access_token=str(data.get("access_token", "")),
        token_type=str(data.get("token_type", "Bearer")),
        refresh_token=str(data.get("refresh_token", "")),
        expires_in=int(data["expires_in"]) if data.get("expires_in") is not None else None,
    )
