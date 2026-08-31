# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import re

import pytest
from keyring.errors import KeyringLocked, NoKeyringError, PasswordDeleteError

from dcent_voice.auth.base import ApiKeyAuth, AuthMode
from dcent_voice.auth.oauth import OAuthAuth, OAuthToken, build_authorization_url, create_pkce_pair
from dcent_voice.auth.registry import get_provider_capability
from dcent_voice.auth.store import CredentialStore


class FakeKeyring:
    def __init__(self) -> None:
        self.values = {}

    def get_password(self, service_name: str, username: str):
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


class UnavailableKeyring:
    def get_password(self, service_name: str, username: str):
        raise NoKeyringError("no desktop keyring")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise NoKeyringError("no desktop keyring")

    def delete_password(self, service_name: str, username: str) -> None:
        raise NoKeyringError("no desktop keyring")


class MissingPasswordKeyring(FakeKeyring):
    def delete_password(self, service_name: str, username: str) -> None:
        raise PasswordDeleteError("password not found")


class RetainingKeyring(FakeKeyring):
    def __init__(self, *, silent: bool = False) -> None:
        super().__init__()
        self.silent = silent
        self.locked = False

    def get_password(self, service_name: str, username: str):
        if self.locked:
            raise KeyringLocked("credential store locked")
        return super().get_password(service_name, username)

    def delete_password(self, service_name: str, username: str) -> None:
        if self.locked:
            raise KeyringLocked("credential store locked")
        if self.silent:
            return
        raise PasswordDeleteError("delete failed")


def test_registry_marks_cloud_providers_api_key() -> None:
    for name in ("openai", "anthropic", "groq", "deepgram"):
        capability = get_provider_capability(name)
        assert capability is not None
        assert capability.locality == "cloud"
        assert capability.auth_modes == (AuthMode.API_KEY,)


def test_api_key_auth_uses_credential_store() -> None:
    store = CredentialStore(FakeKeyring())
    auth = ApiKeyAuth(store)

    account = auth.connect("openai", "sk-test", label="test key")

    assert account.connected is True
    assert auth.status("openai").label == "test key"
    auth.disconnect("openai")
    assert auth.status("openai").connected is False


def test_unavailable_desktop_keyring_is_safe_for_status_and_explicit_on_mutation() -> None:
    store = CredentialStore(UnavailableKeyring())

    assert store.get_secret("openai", "api_key") is None
    with pytest.raises(RuntimeError, match="credential store is unavailable"):
        store.delete_secret("openai", "api_key")
    with pytest.raises(RuntimeError, match="credential store is unavailable"):
        store.set_secret("openai", "api_key", "sk-test")


def test_delete_secret_ignores_only_confirmed_absence() -> None:
    store = CredentialStore(MissingPasswordKeyring())

    store.delete_secret("openai", "api_key")


@pytest.mark.parametrize("silent", [False, True])
def test_delete_secret_surfaces_a_retained_credential_without_leaking_it(silent: bool) -> None:
    backend = RetainingKeyring(silent=silent)
    store = CredentialStore(backend)
    store.set_secret("openai", "api_key", "sk-never-show-this")

    with pytest.raises(RuntimeError) as caught:
        store.delete_secret("openai", "api_key")

    assert "sk-never-show-this" not in str(caught.value)
    assert store.get_secret("openai", "api_key") == "sk-never-show-this"


def test_delete_secret_locked_then_unlocked_retains_and_reports_secret() -> None:
    backend = RetainingKeyring()
    store = CredentialStore(backend)
    store.set_secret("openai", "api_key", "sk-retained")
    backend.locked = True

    with pytest.raises(RuntimeError, match="credential store is unavailable"):
        store.delete_secret("openai", "api_key")

    backend.locked = False
    assert store.get_secret("openai", "api_key") == "sk-retained"


def test_pkce_pair_shape_and_authorization_url() -> None:
    pair = create_pkce_pair()

    assert re.match(r"^[A-Za-z0-9_-]+$", pair.verifier)
    assert re.match(r"^[A-Za-z0-9_-]+$", pair.challenge)
    url = build_authorization_url(
        "https://auth.example.test/authorize",
        client_id="client",
        redirect_uri="http://127.0.0.1/callback",
        scope="read",
        state="state",
        code_challenge=pair.challenge,
    )
    assert "code_challenge_method=S256" in url


def test_oauth_auth_stores_and_removes_token() -> None:
    store = CredentialStore(FakeKeyring())
    auth = OAuthAuth(store)

    account = auth.connect_token(
        "example",
        OAuthToken(access_token="access", refresh_token="refresh", expires_in=60),
        label="OAuth account",
        mode=AuthMode.DEVICE_CODE,
    )

    assert account.connected is True
    assert account.auth_mode is AuthMode.DEVICE_CODE
    assert auth.token("example").refresh_token == "refresh"
    auth.disconnect("example")
    assert auth.status("example").connected is False
