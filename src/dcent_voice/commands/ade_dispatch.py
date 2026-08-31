# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Dispatch authenticated commands to a locally discovered DCENT ADE instance."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from dcent_voice.attach.contract import LOOPBACK_HOSTS
from dcent_voice.attach.registry import (
    default_registry_dir,
    read_registry_entry,
    verify_private_file,
)
from dcent_voice.commands.schema import ToolCall

ADE_MODULE_ID = "dcent-ade"
ADE_REGISTRY_FILENAME = f"{ADE_MODULE_ID}.json"
MAX_TOKEN_BYTES = 512
MAX_TOOL_TARGET_CHARS = 256
ALLOWED_TOOL_NAMES = frozenset({"create", "open", "search", "summarize"})
ALLOWED_OPEN_TARGETS = frozenset(
    {
        "agent sessions",
        "agents",
        "board",
        "browser",
        "calendar",
        "chat",
        "command palette",
        "dashboard",
        "editor",
        "email",
        "files",
        "git",
        "memory",
        "mission control",
        "orion",
        "project dashboard",
        "settings",
        "terminal",
        "voice",
    }
)


class ADEDispatchError(RuntimeError):
    """A fail-closed ADE discovery or dispatch error with no secret detail."""


class ADEDispatchSecurityError(ADEDispatchError):
    """The configured ADE target crossed the local authenticated boundary."""


class ADEToolCallRejected(ADEDispatchSecurityError):
    """A transcript-derived tool call was outside the narrow safe grammar."""


def assert_local_ade_endpoint(endpoint: str) -> None:
    """Accept HTTP(S) only on an explicit loopback hostname or address."""

    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ADEDispatchSecurityError("ADE registry endpoint has an invalid port.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or host not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is None
    ):
        raise ADEDispatchSecurityError(
            "ADE command dispatch only permits an authenticated loopback HTTP endpoint."
        )


@dataclass
class ADEDispatcher:
    """Dispatch transcript-derived tool calls across the local ADE trust boundary.

    Desktop construction leaves ``endpoint`` and ``token`` unset. Each dispatch
    then re-reads ``dcent-ade.json`` and its bounded, local ``tokenRef`` so an ADE
    restart can rotate credentials without restarting Voice. Explicit targets are
    retained for tests and embedders, but are subject to the same loopback rule.
    """

    endpoint: str | None = None
    token: str | None = field(default=None, repr=False)
    timeout_s: float = 2.0
    registry_dir: Path | str | None = None
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _explicit_target: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._explicit_target = self.endpoint is not None or self.token is not None
        if self._explicit_target:
            if not self.endpoint or not self.token:
                raise ADEDispatchSecurityError(
                    "An explicit ADE dispatcher requires both endpoint and bearer token."
                )
            assert_local_ade_endpoint(self.endpoint)
            _validate_token(self.token)
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

    def dispatch(
        self,
        tool_call: ToolCall,
        *,
        selection_present: bool = False,
    ) -> dict[str, Any]:
        payload = validate_tool_call(tool_call, selection_present=selection_present)
        target = self._target()
        if target is None:
            return {
                "dispatched": False,
                "reason": "no_endpoint",
                "tool_call": tool_call.model_dump(),
            }
        endpoint, token = target
        headers = {"Authorization": f"Bearer {token}"}
        try:
            # trust_env=False prevents HTTP(S)_PROXY environment injection from
            # carrying a transcript-derived request or bearer token off-machine.
            with httpx.Client(
                timeout=self.timeout_s,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                # A tool call can have side effects. A read/connect timeout does
                # not prove the receiver failed to commit, so never retry this
                # POST until ADE negotiates and honors an idempotency contract.
                response = client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPError:
            # Provider exceptions can retain a Request object containing headers.
            # Suppress their context and expose a fixed message so a crash/log path
            # cannot serialize the bearer token.
            raise ADEDispatchError("Local ADE command dispatch failed.") from None
        try:
            body = response.json() if response.content else None
        except ValueError:
            raise ADEDispatchError("Local ADE returned an invalid response.") from None
        return {"dispatched": True, "response": body}

    def _target(self) -> tuple[str, str] | None:
        if self._explicit_target:
            # __post_init__ established both values; retain a defensive guard for
            # callers that mutate public dataclass attributes after construction.
            if not self.endpoint or not self.token:
                raise ADEDispatchSecurityError("ADE dispatcher target became incomplete.")
            assert_local_ade_endpoint(self.endpoint)
            _validate_token(self.token)
            return self.endpoint, self.token
        return self._discover_target()

    def _discover_target(self) -> tuple[str, str] | None:
        root = (
            Path(self.registry_dir) if self.registry_dir is not None else default_registry_dir()
        ).resolve()
        registry_path = root / ADE_REGISTRY_FILENAME
        if not registry_path.is_file():
            return None
        try:
            if registry_path.is_symlink():
                raise ADEDispatchSecurityError("ADE registry entry must not be a symlink.")
            verify_private_file(registry_path)
            entry = read_registry_entry(registry_path)
            if entry.moduleId != ADE_MODULE_ID:
                raise ADEDispatchSecurityError("ADE registry module identity did not match.")
            assert_local_ade_endpoint(entry.endpoint)
            token = _read_registry_token(root, entry.tokenRef)
        except (OSError, ValueError, KeyError, TypeError):
            raise ADEDispatchSecurityError("ADE registry entry is invalid.") from None
        return entry.endpoint, token


def validate_tool_call(
    tool_call: ToolCall,
    *,
    selection_present: bool = False,
) -> dict[str, Any]:
    """Return the canonical narrow payload or reject before discovery/network."""

    if selection_present:
        raise ADEToolCallRejected("ADE automation is disabled while selected text is present.")
    if tool_call.name not in ALLOWED_TOOL_NAMES:
        raise ADEToolCallRejected("ADE tool name is not allowed.")
    arguments = tool_call.arguments
    if set(arguments) != {"target"}:
        raise ADEToolCallRejected("ADE tool arguments do not match the allowed schema.")
    target = arguments.get("target")
    if type(target) is not str:
        raise ADEToolCallRejected("ADE tool target must be a string.")
    target = target.strip()
    if (
        not target
        or len(target) > MAX_TOOL_TARGET_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in target)
    ):
        raise ADEToolCallRejected("ADE tool target is invalid.")
    if tool_call.name == "open":
        normalized_target = " ".join(target.casefold().split())
        if normalized_target.startswith("the "):
            normalized_target = normalized_target[4:]
        if normalized_target not in ALLOWED_OPEN_TARGETS:
            raise ADEToolCallRejected("ADE open target is not allowed.")
    return {"name": tool_call.name, "arguments": {"target": target}}


def _read_registry_token(registry_root: Path, token_ref: str) -> str:
    token_path = Path(token_ref)
    if not token_path.is_absolute():
        token_path = registry_root / token_path
    if token_path.is_symlink():
        raise ADEDispatchSecurityError("ADE bearer token must not be a symlink.")
    try:
        resolved = token_path.resolve(strict=True)
        resolved.relative_to(registry_root)
        size = resolved.stat().st_size
    except (OSError, ValueError):
        raise ADEDispatchSecurityError(
            "ADE bearer token is outside the registry boundary."
        ) from None
    if not resolved.is_file() or size > MAX_TOKEN_BYTES:
        raise ADEDispatchSecurityError("ADE bearer token file is invalid.")
    try:
        verify_private_file(resolved)
    except PermissionError:
        raise ADEDispatchSecurityError("ADE bearer token is not owner-only.") from None
    try:
        token = resolved.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise ADEDispatchSecurityError("ADE bearer token could not be read safely.") from None
    _validate_token(token)
    return token


def _validate_token(token: str) -> None:
    encoded = token.encode("ascii", errors="ignore")
    if (
        not token
        or len(encoded) != len(token)
        or len(encoded) > MAX_TOKEN_BYTES
        or any(character.isspace() or not character.isprintable() for character in token)
    ):
        raise ADEDispatchSecurityError("ADE bearer token is invalid.")
