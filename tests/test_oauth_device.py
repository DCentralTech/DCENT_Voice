# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dcent_voice.auth import oauth as oauth_module
from dcent_voice.auth.oauth import (
    OAuthAuth,
    OAuthToken,
    exchange_authorization_code,
    poll_device_token,
    request_device_code,
)
from dcent_voice.auth.validate import CREDENTIAL_EGRESS_PAYLOAD, credential_consent_key
from dcent_voice.privacy import ConsentLedger, ConsentRequired, EgressLog


class _DictStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_secret(self, provider: str, name: str):
        return self.values.get((provider, name))

    def set_secret(self, provider: str, name: str, value: str) -> None:
        self.values[(provider, name)] = value

    def delete_secret(self, provider: str, name: str) -> None:
        self.values.pop((provider, name), None)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _privacy_authorizer(
    tmp_path: Path,
) -> tuple[ConsentLedger, EgressLog, Callable[[], None]]:
    ledger = ConsentLedger(tmp_path / "consent.json")
    egress = EgressLog(tmp_path / "egress.jsonl")
    key = credential_consent_key("xai")

    def authorize() -> None:
        if not ledger.has_consent(key, payload_type=CREDENTIAL_EGRESS_PAYLOAD):
            raise ConsentRequired((key,))
        egress.record(key, payload_type=CREDENTIAL_EGRESS_PAYLOAD, byte_count=0)

    return ledger, egress, authorize


def _invoke_oauth_helper(
    operation: str,
    *,
    transport: httpx.BaseTransport | None,
    authorize_egress: Callable[[], None] | None,
    endpoint: str | None = None,
) -> object:
    if operation == "device":
        return request_device_code(
            endpoint or "https://accounts.x.ai/oauth2/device/code",
            client_id="client-secret",
            transport=transport,
            authorize_egress=authorize_egress,
        )
    if operation == "poll":
        return poll_device_token(
            endpoint or "https://accounts.x.ai/oauth2/token",
            client_id="client-secret",
            device_code="device-secret",
            transport=transport,
            authorize_egress=authorize_egress,
        )
    return exchange_authorization_code(
        endpoint or "https://accounts.x.ai/oauth2/token",
        client_id="client-secret",
        code="authorization-secret",
        redirect_uri="http://127.0.0.1/callback",
        code_verifier="verifier-secret",
        transport=transport,
        authorize_egress=authorize_egress,
    )


def _hostile_proxy() -> tuple[ThreadingHTTPServer, threading.Thread, list[bytes]]:
    requests: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            request = f"{self.command} {self.path}\n{self.headers}".encode() + body
            requests.append(request)
            self.send_response(502)
            self.end_headers()

        def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler contract
            self._record()

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            self._record()

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests


def test_request_device_code_parses_grant() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "dc-123",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://accounts.x.ai/device",
                "interval": 5,
                "expires_in": 600,
            },
        )

    grant = request_device_code(
        "https://accounts.x.ai/oauth2/device/code",
        client_id="cid",
        scope="api",
        transport=_transport(handler),
        authorize_egress=lambda: None,
    )
    assert grant.device_code == "dc-123"
    assert grant.user_code == "WXYZ-1234"
    assert grant.verification_uri == "https://accounts.x.ai/device"
    assert grant.interval == 5


def test_poll_device_token_returns_access_token() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "tok-abc", "token_type": "Bearer", "expires_in": 3600}
        )

    token = poll_device_token(
        "https://accounts.x.ai/oauth2/token",
        client_id="cid",
        device_code="dc-123",
        transport=_transport(handler),
        authorize_egress=lambda: None,
    )
    assert isinstance(token, OAuthToken)
    assert token.access_token == "tok-abc"
    assert token.expires_in == 3600


def test_exchange_authorization_code_returns_access_token() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok-pkce"})

    token = exchange_authorization_code(
        "https://accounts.x.ai/oauth2/token",
        client_id="cid",
        code="code-123",
        redirect_uri="http://127.0.0.1/callback",
        code_verifier="verifier-123",
        transport=_transport(handler),
        authorize_egress=lambda: None,
    )

    assert token.access_token == "tok-pkce"


@pytest.mark.parametrize("operation", ["device", "poll", "exchange"])
def test_oauth_helpers_isolate_client_from_ambient_transport_configuration(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: list[dict[str, object]] = []
    real_client = httpx.Client

    def guarded_client(*args, **kwargs):
        options.append(dict(kwargs))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(oauth_module.httpx, "Client", guarded_client)

    _invoke_oauth_helper(
        operation,
        transport=_transport(lambda _request: httpx.Response(200, json={})),
        authorize_egress=lambda: None,
    )

    assert len(options) == 1
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


@pytest.mark.parametrize("operation", ["device", "poll", "exchange"])
def test_oauth_helpers_ignore_hostile_proxy_without_leaking_credentials(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy, proxy_thread, proxy_requests = _hostile_proxy()
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    for variable in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.setenv(variable, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    try:
        # A bound-but-not-listening local port fails immediately if the OAuth
        # helper connects directly. If ambient proxies were honored, the proxy
        # would instead receive a CONNECT request for this endpoint.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            unused_port = reservation.getsockname()[1]
            with pytest.raises(httpx.TransportError):
                _invoke_oauth_helper(
                    operation,
                    endpoint=f"https://127.0.0.1:{unused_port}/oauth/token",
                    transport=None,
                    authorize_egress=lambda: None,
                )
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2.0)

    assert proxy_requests == []
    captured = b"\n".join(proxy_requests)
    for secret in (
        b"client-secret",
        b"device-secret",
        b"authorization-secret",
        b"verifier-secret",
    ):
        assert secret not in captured


@pytest.mark.parametrize("operation", ["device", "poll", "exchange"])
def test_oauth_helpers_do_not_follow_credential_redirects(operation: str) -> None:
    requests: list[httpx.Request] = []
    authorizations = 0

    def authorize() -> None:
        nonlocal authorizations
        authorizations += 1

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://attacker.invalid/collect"},
        )

    with pytest.raises(httpx.HTTPStatusError, match="Redirect response"):
        _invoke_oauth_helper(
            operation,
            transport=_transport(handler),
            authorize_egress=authorize,
        )

    assert authorizations == 1
    assert len(requests) == 1
    assert requests[0].url.host == "accounts.x.ai"


@pytest.mark.parametrize("operation", ["device", "poll", "exchange"])
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://accounts.x.ai/oauth2/token",
        "accounts.x.ai/oauth2/token",
        "https://user:secret@accounts.x.ai/oauth2/token",
        "https://accounts.x.ai:invalid/oauth2/token",
        "https://accounts.x.ai/oauth2/token#fragment",
    ],
)
def test_oauth_helpers_reject_insecure_or_invalid_endpoint_before_consent_and_wire(
    operation: str,
    endpoint: str,
) -> None:
    requests: list[httpx.Request] = []
    authorizations = 0

    def authorize() -> None:
        nonlocal authorizations
        authorizations += 1

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        _invoke_oauth_helper(
            operation,
            endpoint=endpoint,
            transport=_transport(handler),
            authorize_egress=authorize,
        )

    assert authorizations == 0
    assert requests == []


@pytest.mark.parametrize("operation", ["device", "poll", "exchange"])
def test_public_oauth_helpers_fail_closed_without_authorizer(operation: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(ConsentRequired, match="OAuth credential egress"):
        _invoke_oauth_helper(
            operation,
            transport=_transport(handler),
            authorize_egress=None,
        )

    assert requests == []


def test_revoked_consent_blocks_device_poll_before_wire(tmp_path: Path) -> None:
    ledger, egress, authorize = _privacy_authorizer(tmp_path)
    key = credential_consent_key("xai")
    ledger.grant(key, payload_type=CREDENTIAL_EGRESS_PAYLOAD)
    ledger.revoke(key)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "must-not-arrive"})

    with pytest.raises(ConsentRequired, match="auth:xai"):
        _invoke_oauth_helper(
            "poll",
            transport=_transport(handler),
            authorize_egress=authorize,
        )

    assert requests == []
    assert egress.tail() == []


def test_corrupt_consent_ledger_blocks_device_request_before_wire(tmp_path: Path) -> None:
    ledger, egress, authorize = _privacy_authorizer(tmp_path)
    ledger.path.write_text('{"auth:xai":', encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"device_code": "must-not-arrive"})

    with pytest.raises(ConsentRequired, match="auth:xai"):
        _invoke_oauth_helper(
            "device",
            transport=_transport(handler),
            authorize_egress=authorize,
        )

    assert requests == []
    assert egress.tail() == []


def test_authorization_code_audit_precedes_wire_and_logs_no_secrets(tmp_path: Path) -> None:
    ledger, egress, base_authorize = _privacy_authorizer(tmp_path)
    key = credential_consent_key("xai")
    ledger.grant(key, payload_type=CREDENTIAL_EGRESS_PAYLOAD)
    order: list[str] = []

    def authorize() -> None:
        order.append("audit")
        base_authorize()

    def handler(_request: httpx.Request) -> httpx.Response:
        order.append("wire")
        assert len(egress.tail()) == 1
        return httpx.Response(200, json={"access_token": "returned-token-secret"})

    token = exchange_authorization_code(
        "https://accounts.x.ai/oauth2/token",
        client_id="client-secret",
        code="authorization-secret",
        redirect_uri="http://127.0.0.1/callback",
        code_verifier="verifier-secret",
        transport=_transport(handler),
        authorize_egress=authorize,
    )

    assert token.access_token == "returned-token-secret"
    assert order == ["audit", "wire"]
    raw_log = egress.path.read_text(encoding="utf-8")
    for secret in (
        "client-secret",
        "authorization-secret",
        "verifier-secret",
        "returned-token-secret",
    ):
        assert secret not in raw_log


def test_network_failure_keeps_pre_wire_audit_without_device_secret(tmp_path: Path) -> None:
    ledger, egress, authorize = _privacy_authorizer(tmp_path)
    key = credential_consent_key("xai")
    ledger.grant(key, payload_type=CREDENTIAL_EGRESS_PAYLOAD)

    def handler(request: httpx.Request) -> httpx.Response:
        assert len(egress.tail()) == 1
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(httpx.ConnectError, match="offline"):
        poll_device_token(
            "https://accounts.x.ai/oauth2/token",
            client_id="client-secret",
            device_code="device-secret",
            transport=_transport(handler),
            authorize_egress=authorize,
        )

    entries = egress.tail()
    assert len(entries) == 1
    assert entries[0].provider_key == "auth:xai"
    assert entries[0].payload_type == CREDENTIAL_EGRESS_PAYLOAD
    assert entries[0].byte_count == 0
    raw_log = egress.path.read_text(encoding="utf-8")
    assert "client-secret" not in raw_log
    assert "device-secret" not in raw_log


def test_expired_token_is_not_returned(monkeypatch) -> None:
    # An expired access token must not shadow a working API key in the
    # credential-precedence chain (app._effective_credential).
    store = _DictStore()
    auth = OAuthAuth(store)
    monkeypatch.setattr(oauth_module.time, "time", lambda: 1_000_000.0)
    auth.connect_token("xai", OAuthToken(access_token="tok", expires_in=3600))

    assert auth.token("xai") is not None  # fresh

    monkeypatch.setattr(oauth_module.time, "time", lambda: 1_000_000.0 + 3600.0)
    assert auth.token("xai") is None  # expired -> fall back to API key

    # Status still reports the sign-in so the UI can offer re-auth.
    assert auth.status("xai").connected is True


def test_token_without_expiry_never_expires(monkeypatch) -> None:
    store = _DictStore()
    auth = OAuthAuth(store)
    auth.connect_token("xai", OAuthToken(access_token="tok"))
    monkeypatch.setattr(oauth_module.time, "time", lambda: 9_999_999_999.0)
    token = auth.token("xai")
    assert token is not None
    assert token.access_token == "tok"


def test_disconnect_clears_obtained_at() -> None:
    store = _DictStore()
    auth = OAuthAuth(store)
    auth.connect_token("xai", OAuthToken(access_token="tok", expires_in=3600))
    auth.disconnect("xai")
    assert store.values == {}
