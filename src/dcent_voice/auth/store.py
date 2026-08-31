# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Store provider credentials in the operating-system keyring."""

from __future__ import annotations

from typing import Protocol

SERVICE_NAME = "DCENT_Voice"


class KeyringBackend(Protocol):
    """Protocol implemented by operating-system keyring adapters."""

    def get_password(self, service_name: str, username: str) -> str | None: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


class CredentialStore:
    """Provider credential store backed by the operating-system keyring."""

    def __init__(
        self, backend: KeyringBackend | None = None, *, service_name: str = SERVICE_NAME
    ) -> None:
        if backend is None:
            try:
                import keyring
            except ImportError as exc:  # pragma: no cover - dependency/environment specific
                raise RuntimeError("keyring is required for credential storage.") from exc
            backend = keyring
        self.backend = backend
        self.service_name = service_name

    def get_secret(self, provider: str, name: str) -> str | None:
        try:
            return self.backend.get_password(self.service_name, _username(provider, name))
        except Exception as exc:
            if _is_keyring_unavailable(exc):
                return None
            raise

    def set_secret(self, provider: str, name: str, value: str) -> None:
        try:
            self.backend.set_password(self.service_name, _username(provider, name), value)
        except Exception as exc:
            if _is_keyring_unavailable(exc):
                raise RuntimeError(
                    "The operating-system credential store is unavailable. "
                    "Start or unlock the desktop keyring and try again."
                ) from exc
            raise

    def delete_secret(self, provider: str, name: str) -> None:
        username = _username(provider, name)
        try:
            self.backend.delete_password(self.service_name, username)
        except Exception as exc:
            if _is_password_not_found(self.backend, self.service_name, username, exc):
                return
            if _is_keyring_unavailable(exc):
                raise RuntimeError(
                    "The operating-system credential store is unavailable. "
                    "Start or unlock the desktop keyring and try again."
                ) from exc
            raise

        # Treat deletion as successful only after the backend confirms the
        # credential is absent. This catches adapters that return without doing
        # anything and prevents callers from reporting a false disconnect.
        try:
            remaining = self.backend.get_password(self.service_name, username)
        except Exception as exc:
            if _is_keyring_unavailable(exc):
                raise RuntimeError(
                    "The operating-system credential store is unavailable. "
                    "Start or unlock the desktop keyring and try again."
                ) from exc
            raise
        if remaining is not None:
            raise RuntimeError("The credential store did not remove the saved credential.")


def _username(provider: str, name: str) -> str:
    return f"{provider}:{name}"


def _is_keyring_unavailable(exc: Exception) -> bool:
    """Recognize keyring backend failures without masking unrelated adapters."""

    try:
        from keyring.errors import KeyringError
    except ImportError:  # pragma: no cover - keyring is a declared dependency
        return False
    return isinstance(exc, KeyringError)


def _is_password_not_found(
    backend: KeyringBackend,
    service_name: str,
    username: str,
    exc: Exception,
) -> bool:
    """Confirm that a delete error means the requested credential is absent."""

    try:
        from keyring.errors import PasswordDeleteError
    except ImportError:  # pragma: no cover - keyring is a declared dependency
        return False
    if not isinstance(exc, PasswordDeleteError):
        return False
    try:
        return backend.get_password(service_name, username) is None
    except Exception:
        # A locked or unavailable store cannot prove absence. The original
        # deletion failure therefore remains actionable rather than being
        # mistaken for the normal "already absent" case.
        return False
