# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from dcent_voice.auth import validate
from dcent_voice.auth.validate import validate_api_key


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_empty_key_is_rejected_without_a_network_call() -> None:
    result = validate_api_key("openai", "   ")
    assert result.ok is False


def test_accepted_key(monkeypatch) -> None:
    monkeypatch.setattr(validate, "_probe", lambda *a, **k: _FakeResponse(200))
    result = validate_api_key("xai", "xai-abc", authorize_egress=lambda: None)
    assert result.ok is True


def test_rejected_key(monkeypatch) -> None:
    monkeypatch.setattr(validate, "_probe", lambda *a, **k: _FakeResponse(401))
    result = validate_api_key("openai", "sk-bad", authorize_egress=lambda: None)
    assert result.ok is False
    assert result.reachable is True  # a real rejection, not a network blip
    assert "reject" in result.detail.lower()


def test_network_error_is_marked_unreachable(monkeypatch) -> None:
    def raise_offline(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(validate, "_probe", raise_offline)
    result = validate_api_key("openai", "sk-good", authorize_egress=lambda: None)
    assert result.ok is False
    assert result.reachable is False  # caller should save the key unverified


def test_server_error_is_marked_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(validate, "_probe", lambda *a, **k: _FakeResponse(503))
    result = validate_api_key("openai", "sk-good", authorize_egress=lambda: None)
    assert result.ok is False
    assert result.reachable is False


def test_provider_without_live_check_is_saved(monkeypatch) -> None:
    monkeypatch.setattr(validate, "_probe", lambda *a, **k: None)
    assert validate_api_key("somethingelse", "key").ok is True


def test_live_validation_fails_closed_without_egress_authorization(monkeypatch) -> None:
    called = False

    def probe(*args, **kwargs):
        nonlocal called
        called = True
        return _FakeResponse(200)

    monkeypatch.setattr(validate, "_probe", probe)

    result = validate_api_key("openai", "sk-private")

    assert result.ok is False
    assert "consent" in result.detail.lower()
    assert called is False


def test_egress_authorization_runs_immediately_before_probe(monkeypatch) -> None:
    order: list[str] = []

    def authorize() -> None:
        order.append("audit")

    def probe(*args, **kwargs):
        order.append("wire")
        return _FakeResponse(401)

    monkeypatch.setattr(validate, "_probe", probe)

    result = validate_api_key("openai", "sk-private", authorize_egress=authorize)

    assert result.ok is False
    assert order == ["audit", "wire"]


def test_failed_egress_authorization_prevents_probe(monkeypatch) -> None:
    called = False

    def probe(*args, **kwargs):
        nonlocal called
        called = True
        return _FakeResponse(200)

    def reject() -> None:
        raise PermissionError("audit unavailable")

    monkeypatch.setattr(validate, "_probe", probe)

    with pytest.raises(PermissionError, match="audit unavailable"):
        validate_api_key("deepgram", "dg-private", authorize_egress=reject)

    assert called is False


def test_probe_client_disables_environment_proxies_and_redirects(monkeypatch) -> None:
    events: list[str] = []
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["options"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, url: str, *, headers: dict[str, str]):
            events.append("wire")
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse(302)

    monkeypatch.setenv("HTTPS_PROXY", "http://hostile-proxy.invalid:8080")
    monkeypatch.setattr(validate.httpx, "Client", FakeClient)

    result = validate_api_key(
        "openai",
        "sk-private-never-proxied",
        authorize_egress=lambda: events.append("audit"),
    )

    assert result.ok is False
    assert events == ["audit", "wire"]
    assert captured["options"] == {
        "timeout": 8.0,
        "trust_env": False,
        "follow_redirects": False,
    }
    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["headers"] == {"Authorization": "Bearer sk-private-never-proxied"}


@pytest.mark.parametrize(
    "provider,unsafe_base",
    [
        ("openai", "http://api.openai.com/v1"),
        ("groq", "https://api.groq.com.evil.invalid/openai/v1"),
        ("xai", "https://user:password@api.x.ai/v1"),
        ("openai", "https://api.openai.com:444/v1"),
    ],
)
def test_bearer_probe_rejects_noncanonical_endpoint_before_client_or_key_egress(
    monkeypatch, provider: str, unsafe_base: str
) -> None:
    client_calls = 0
    audits = 0

    def client(*_args, **_kwargs):
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("HTTP client must not be constructed")

    def authorize() -> None:
        nonlocal audits
        audits += 1

    monkeypatch.setitem(validate.DEFAULT_BASE_URLS, provider, unsafe_base)
    monkeypatch.setattr(validate.httpx, "Client", client)

    result = validate_api_key(provider, "must-not-leave", authorize_egress=authorize)

    assert result.ok is False
    assert result.reachable is False
    assert audits == 1
    assert client_calls == 0


@pytest.mark.parametrize(
    "provider,url",
    [
        ("openai", "https://api.openai.com/v1/models"),
        ("groq", "https://api.groq.com/openai/v1/models"),
        ("xai", "https://api.x.ai/v1/models"),
        ("anthropic", "https://api.anthropic.com/v1/models"),
        ("deepgram", "https://api.deepgram.com/v1/projects"),
    ],
)
def test_all_live_probe_destinations_require_canonical_provider_https(
    provider: str, url: str
) -> None:
    validate._require_provider_https_endpoint(provider, url)
    with pytest.raises(validate.CredentialEndpointError):
        validate._require_provider_https_endpoint(provider, url.replace("https://", "http://"))


def test_probe_does_not_send_credentials_or_connect_to_hostile_environment_proxy(
    monkeypatch,
) -> None:
    proxy_requests: list[tuple[str, bytes]] = []

    class HostileProxy(BaseHTTPRequestHandler):
        def _capture(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            proxy_requests.append((self.command, self.rfile.read(length)))
            self.send_response(502)
            self.end_headers()

        do_CONNECT = _capture
        do_GET = _capture

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), HostileProxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("ALL_PROXY", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")

    original_getaddrinfo = socket.getaddrinfo

    def block_provider_dns(host: object, *args: object, **kwargs: object):
        if str(host).lower() == "api.openai.com":
            raise socket.gaierror("provider DNS intentionally blocked by test")
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", block_provider_dns)
    try:
        with pytest.raises(httpx.ConnectError):
            validate._probe("openai", "sk-private-proxy-canary", 0.5)
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)

    assert proxy_requests == []


def test_probe_does_not_follow_redirect_or_forward_key(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://credential-capture.invalid/redirected"},
            request=request,
        )

    real_client = httpx.Client

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(validate.httpx, "Client", client_factory)

    result = validate_api_key(
        "openai",
        "sk-private-redirect-canary",
        authorize_egress=lambda: None,
    )

    assert result.ok is False
    assert len(requests) == 1
    assert requests[0].url == "https://api.openai.com/v1/models"
    assert requests[0].headers["Authorization"] == "Bearer sk-private-redirect-canary"
