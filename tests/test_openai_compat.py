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
from dcent_voice.llm.openai_compat import OpenAICompatProvider
from dcent_voice.privacy import (
    ConsentLedger,
    ConsentRequired,
    EgressLog,
    PrivacyMonitor,
    ProviderPrivacy,
)


def _cloud_monitor(tmp_path: Path, provider_name: str) -> PrivacyMonitor:
    provider_key = f"llm:{provider_name}"
    return PrivacyMonitor(
        (
            ProviderPrivacy(
                key=provider_key,
                role="llm",
                provider=provider_name,
                locality=Locality.CLOUD,
                payload_type="text",
            ),
        ),
        ledger=ConsentLedger(tmp_path / f"{provider_name}-consent.json"),
        egress_log=EgressLog(tmp_path / f"{provider_name}-egress.jsonl"),
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


def test_openai_compat_complete_posts_chat_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Clean text."}}]},
        )

    provider = OpenAICompatProvider(
        LLMSpec.parse("ollama:qwen2.5:7b"),
        transport=httpx.MockTransport(handler),
    )

    assert provider.complete("system", "user") == "Clean text."
    assert seen["path"] == "/v1/chat/completions"
    assert seen["payload"]["model"] == "qwen2.5:7b"
    assert seen["payload"]["keep_alive"] == "2m"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.invalid:11434/v1",
        "http://0.0.0.0:11434/v1",
        "http://127.0.0.1.evil.invalid:11434/v1",
        "http://user@127.0.0.1:11434/v1",
        "file://127.0.0.1:11434/v1",
        "http://127.0.0.1/v1",
    ],
)
def test_local_provider_rejects_non_loopback_or_ambiguous_endpoint(base_url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        OpenAICompatProvider(
            LLMSpec.parse("ollama:test-model"),
            base_url=base_url,
        )


def test_local_provider_ignores_hostile_proxy_for_full_prompt(
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
    secret_prompt = "FULL PRIVATE TRANSCRIPT must never reach proxy"
    provider = OpenAICompatProvider(
        LLMSpec.parse("ollama:test-model"),
        base_url=f"http://127.0.0.1:{unused_port}/v1",
        timeout_s=0.2,
    )

    try:
        with pytest.raises(httpx.HTTPError):
            provider.complete("private system prompt", secret_prompt)
    finally:
        provider.close()
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2.0)

    assert proxy_requests == []


def test_explicit_cloud_endpoint_remains_supported_with_audit() -> None:
    requests: list[httpx.Request] = []
    audits: list[tuple[str, str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpenAICompatProvider(
        LLMSpec.parse("openai:test-model"),
        base_url="https://compatible.example.invalid/custom/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *entry: audits.append(entry),
    )

    assert provider.complete("system", "user") == "ok"
    assert requests[0].url.path == "/custom/v1/chat/completions"
    assert audits[0][0:2] == ("llm:openai", "text")


def test_provider_does_not_follow_chat_redirect() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://attacker.invalid/collect"})

    provider = OpenAICompatProvider(
        LLMSpec.parse("openai:test-model"),
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *_args: None,
    )

    with pytest.raises(httpx.HTTPStatusError):
        provider.complete("system", "private transcript")
    assert requested_urls == ["https://api.openai.com/v1/chat/completions"]


@pytest.mark.parametrize("spec", ["ollama:local-model", "lmstudio:local-model"])
def test_local_health_uses_models_endpoint_without_cloud_gate(spec: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.extensions["timeout"]["read"] == 2.0
        return httpx.Response(200, json={"data": []})

    provider = OpenAICompatProvider(
        LLMSpec.parse(spec),
        transport=httpx.MockTransport(handler),
        egress_logger=lambda *_args: pytest.fail("local health must not use the cloud gate"),
    )

    assert provider.health() is True


@pytest.mark.parametrize("provider_name", ["openai", "groq", "xai"])
def test_cloud_health_is_consent_gated_and_records_metadata_only_attempt(
    tmp_path: Path, provider_name: str
) -> None:
    secret = f"super-secret-{provider_name}"
    monitor = _cloud_monitor(tmp_path, provider_name)
    monitor.ledger.grant(f"llm:{provider_name}", payload_type="text")
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.extensions["timeout"]["read"] == 2.0
        assert [entry.byte_count for entry in monitor.egress_log.tail()] == [0]
        return httpx.Response(200, json={"data": []})

    provider = OpenAICompatProvider(
        LLMSpec.parse(f"{provider_name}:test-model"),
        api_key=secret,
        timeout_s=30.0,
        transport=httpx.MockTransport(handler),
        egress_logger=_monitor_logger(monitor),
    )

    assert provider.health() is True
    assert requests == 1
    entries = monitor.egress_log.tail()
    assert [(entry.provider_key, entry.payload_type, entry.byte_count) for entry in entries] == [
        (f"llm:{provider_name}", "text", 0)
    ]
    raw_audit = monitor.egress_log.path.read_text(encoding="utf-8")
    assert secret not in raw_audit
    assert "/models" not in raw_audit


def test_cloud_health_rechecks_revoked_consent_before_wire(tmp_path: Path) -> None:
    monitor = _cloud_monitor(tmp_path, "openai")
    monitor.ledger.grant("llm:openai", payload_type="text")
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"data": []})

    provider = OpenAICompatProvider(
        LLMSpec.parse("openai:gpt-test"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
        egress_logger=_monitor_logger(monitor),
    )
    monitor.ledger.revoke("llm:openai")

    with pytest.raises(ConsentRequired, match="llm:openai"):
        provider.health()
    assert requests == 0
    assert monitor.egress_log.tail() == []


def test_cloud_health_rejects_corrupt_consent_before_wire(tmp_path: Path) -> None:
    monitor = _cloud_monitor(tmp_path, "groq")
    monitor.ledger.grant("llm:groq", payload_type="text")
    monitor.ledger.path.write_text("{corrupt", encoding="utf-8")
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"data": []})

    provider = OpenAICompatProvider(
        LLMSpec.parse("groq:test-model"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
        egress_logger=_monitor_logger(monitor),
    )

    with pytest.raises(ConsentRequired, match="llm:groq"):
        provider.health()
    assert requests == 0
    assert monitor.egress_log.tail() == []


def test_cloud_health_without_audit_boundary_fails_closed() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"data": []})

    provider = OpenAICompatProvider(
        LLMSpec.parse("xai:test-model"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
    )

    assert provider.health() is False
    assert requests == 0


def test_cloud_provider_records_egress() -> None:
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert recorded
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpenAICompatProvider(
        LLMSpec.parse("openai:gpt-test"),
        transport=httpx.MockTransport(handler),
        egress_logger=lambda provider_key, payload_type, byte_count: recorded.append(
            (provider_key, payload_type, byte_count)
        ),
    )

    provider.complete("system", "user")

    assert recorded
    assert recorded[0][0] == "llm:openai"
    assert recorded[0][1] == "text"
    assert recorded[0][2] > 0


@pytest.mark.parametrize("method", ["complete", "complete_structured", "complete_tools"])
def test_cloud_post_methods_without_audit_boundary_fail_before_wire(method: str) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={})

    provider = OpenAICompatProvider(
        LLMSpec.parse("openai:gpt-test"),
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


def test_cloud_post_rechecks_revoked_consent_before_wire(tmp_path: Path) -> None:
    monitor = _cloud_monitor(tmp_path, "openai")
    monitor.ledger.grant("llm:openai", payload_type="text")
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpenAICompatProvider(
        LLMSpec.parse("openai:gpt-test"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
        egress_logger=_monitor_logger(monitor),
    )
    monitor.ledger.revoke("llm:openai")

    with pytest.raises(ConsentRequired, match="llm:openai"):
        provider.complete("system", "user")
    assert requests == 0
    assert monitor.egress_log.tail() == []


def test_cloud_post_rejects_corrupt_consent_before_wire(tmp_path: Path) -> None:
    monitor = _cloud_monitor(tmp_path, "groq")
    monitor.ledger.grant("llm:groq", payload_type="text")
    monitor.ledger.path.write_text("{corrupt", encoding="utf-8")
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpenAICompatProvider(
        LLMSpec.parse("groq:test-model"),
        api_key="must-not-leave",
        transport=httpx.MockTransport(handler),
        egress_logger=_monitor_logger(monitor),
    )

    with pytest.raises(ConsentRequired, match="llm:groq"):
        provider.complete("system", "user")
    assert requests == 0
    assert monitor.egress_log.tail() == []


def test_cloud_post_records_attempt_before_network_failure() -> None:
    events: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(("wire", len(request.content)))
        raise httpx.ConnectError("offline", request=request)

    provider = OpenAICompatProvider(
        LLMSpec.parse("xai:test-model"),
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        egress_logger=lambda _provider, _payload, byte_count: events.append(("audit", byte_count)),
    )

    with pytest.raises(httpx.ConnectError, match="offline"):
        provider.complete("system", "user")

    assert [event for event, _size in events] == ["audit", "wire"]
    assert events[0][1] > 0
