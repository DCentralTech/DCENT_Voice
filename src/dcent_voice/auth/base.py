# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Define authentication abstractions for external providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class AuthMode(Enum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH_PKCE = "oauth_pkce"
    DEVICE_CODE = "device_code"


@dataclass(frozen=True)
class ProviderAccount:
    provider: str
    connected: bool
    auth_mode: AuthMode
    label: str = ""


class AuthStrategy(ABC):
    """Protocol for authenticating an external provider account."""

    mode: AuthMode

    @abstractmethod
    def status(self, provider: str) -> ProviderAccount:
        """Return connection status for a provider."""

    @abstractmethod
    def disconnect(self, provider: str) -> None:
        """Remove credentials for a provider."""


class ApiKeyAuth(AuthStrategy):
    """Authentication strategy that stores and validates API keys."""

    mode = AuthMode.API_KEY

    def __init__(self, store) -> None:
        self.store = store

    def connect(self, provider: str, api_key: str, *, label: str = "") -> ProviderAccount:
        if not api_key.strip():
            raise ValueError("API key cannot be empty.")
        self.store.set_secret(provider, "api_key", api_key.strip())
        if label:
            self.store.set_secret(provider, "label", label)
        return self.status(provider)

    def status(self, provider: str) -> ProviderAccount:
        api_key = self.store.get_secret(provider, "api_key")
        label = self.store.get_secret(provider, "label") or ("API key saved" if api_key else "")
        return ProviderAccount(
            provider=provider, connected=bool(api_key), auth_mode=self.mode, label=label
        )

    def disconnect(self, provider: str) -> None:
        _delete_all(self.store, provider, ("api_key", "label"))


def _delete_all(store, provider: str, names: tuple[str, ...]) -> None:
    """Best-effort every deletion, then report any incomplete cleanup."""

    failed = False
    for name in names:
        try:
            store.delete_secret(provider, name)
        except Exception:
            failed = True
    if failed:
        # Keep the error independent of provider names, keyring account names,
        # and credential values so it is safe to cross the settings bridge.
        raise RuntimeError(
            "The operating-system credential store could not remove every saved credential."
        ) from None
