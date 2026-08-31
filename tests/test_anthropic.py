# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from dcent_voice.asr.base import Locality
from dcent_voice.config import LLMSpec
from dcent_voice.llm.anthropic import AnthropicProvider
from dcent_voice.privacy import (
    ConsentLedger,
    ConsentRequired,
    EgressLog,
    PrivacyMonitor,
    ProviderPrivacy,
)


def _cloud_monitor(tmp_path: Path) -> PrivacyMonitor:
    return PrivacyMonitor(
        (
            ProviderPrivacy(
                key="llm:anthropic",
                role="llm",
                provider="anthropic",
                locality=Locality.CLOUD,
                payload_type="text",
            ),
        ),
        ledger=ConsentLedger(tmp_path / "anthropic-consent.json"),
        egress_log=EgressLog(tmp_path / "anthropic-egress.jsonl"),
    )


def _monitor_logger(monitor: PrivacyMonitor) -> Callable[[str, str, int], None]:
    def record(provider_key: str, payload_type: str, byte_count: int) -> None:
        monitor.record_egress(
            provider_key,
            payload_type=payload_type,
            byte_count=byte_count,
        )

    return record


def _hostile_proxy() -> tuple[ThreadingHTTPServer, threading.Thread, list[bytes]]:
    requests: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            requests.append(self.rfile.read(length))
            self.send_response(502)
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests


def test_anthropic_complete_posts_messages_payload() -> None:
    seen: dict[str, Any] = {}
    audited = False

    def handler(request: httpx.Request) -> httpx.Response:
        assert audited
        seen["path"] = request.url.path
        seen["headers"] = request.headers
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "Clean text."}]},
        )

    def audit(_provider: str, _payload: str, byte_count: int) -> None:
        nonlocal audited
        assert byte_count > 0
        audited = True

    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        egress_logger=audit,
    )

    assert provider.complete("system", "user") == "Clean text."
    assert seen["path"] == "/v1/messages"
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["payload"]["model"] == "claude-test"


def test_anthropic_structured_and_egress() -> None:
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"action":"noop"}'}]},
        )

    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda provider_key, payload_type, byte_count: recorded.append(
            (provider_key, payload_type, byte_count)
        ),
    )

    assert provider.complete_structured("system", "user", {"type": "object"}) == {"action": "noop"}
    assert recorded[0][0] == "llm:anthropic"
    assert recorded[0][1] == "text"
    assert recorded[0][2] > 0


@pytest.mark.parametrize("method", ["complete", "complete_structured", "complete_tools"])
def test_anthropic_post_methods_without_audit_boundary_fail_before_wire(method: str) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={})

    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="egress audit boundary"):
        if method == "complete":
            provider.complete("system", "user")
        elif method == "complete_structured":
            provider.complete_structured("system", "user", {"type": "object"})
        else:
            provider.complete_tools("system", "user", [])

    assert requests == 0


def test_anthropic_post_rechecks_revoked_consent_before_wire(tmp_path: Path) -> None:
    monitor = _cloud_monitor(tmp_path)
    monitor.ledger.grant("llm:anthropic", payload_type="text")
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"content": []})

    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
        egress_logger=_monitor_logger(monitor),
    )
    monitor.ledger.revoke("llm:anthropic")

    with pytest.raises(ConsentRequired, match="llm:anthropic"):
        provider.complete("system", "user")
    assert requests == 0
    assert monitor.egress_log.tail() == []


def test_anthropic_post_rejects_corrupt_consent_before_wire(tmp_path: Path) -> None:
    monitor = _cloud_monitor(tmp_path)
    monitor.ledger.grant("llm:anthropic", payload_type="text")
    monitor.ledger.path.write_text("{corrupt", encoding="utf-8")
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"content": []})

    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
        egress_logger=_monitor_logger(monitor),
    )

    with pytest.raises(ConsentRequired, match="llm:anthropic"):
        provider.complete("system", "user")
    assert requests == 0
    assert monitor.egress_log.tail() == []


def test_anthropic_post_records_attempt_before_network_failure() -> None:
    events: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("wire", len(request.content)))
        raise httpx.ConnectError("offline", request=request)

    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda _provider, _payload, byte_count: events.append(("audit", byte_count)),
    )

    with pytest.raises(httpx.ConnectError, match="offline"):
        provider.complete("system", "user")

    assert [event for event, _size in events] == ["audit", "wire"]
    assert events[0][1] > 0


def test_anthropic_ignores_hostile_proxy_for_full_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy, proxy_thread, proxy_requests = _hostile_proxy()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        unused_port = reservation.getsockname()[1]
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("ALL_PROXY", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="test-key",
        base_url=f"http://127.0.0.1:{unused_port}/v1",
        timeout_s=0.2,
        egress_logger=lambda *_args: None,
    )

    try:
        with pytest.raises(httpx.HTTPError):
            provider.complete("private system prompt", "FULL PRIVATE TRANSCRIPT")
    finally:
        provider.close()
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2.0)

    assert proxy_requests == []


def test_anthropic_does_not_follow_message_redirect() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://attacker.invalid/collect"})

    provider = AnthropicProvider(
        LLMSpec.parse("anthropic:claude-test"),
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *_args: None,
    )

    with pytest.raises(httpx.HTTPStatusError):
        provider.complete("system", "private transcript")
    assert requested_urls == ["https://api.anthropic.com/v1/messages"]
