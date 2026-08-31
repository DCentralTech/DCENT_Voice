# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from dcent_voice import local_http
from dcent_voice.bench_latency import run_openai_compat_roundtrip
from dcent_voice.devices import DeviceBenchError, fetch_samples_ms
from dcent_voice.local_http import build_isolated_local_opener


class _MemoryResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _MemoryResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _RecordingOpener:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[object, float]] = []

    def open(self, request: object, *, timeout: float) -> _MemoryResponse:
        self.calls.append((request, timeout))
        return _MemoryResponse(self.payload)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/bench",
        "http://localhost.evil/bench",
        "http://user:password@localhost/bench",
        "http://0.0.0.0:8000/bench",
        "http://[::]:8000/bench",
        "file:///tmp/bench.json",
    ],
)
def test_local_probes_reject_non_loopback_and_ambiguous_urls_before_wire(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sample_opener = _RecordingOpener({"finalizeMs": [12]})
    with pytest.raises(DeviceBenchError):
        fetch_samples_ms(url, opener=sample_opener)
    assert sample_opener.calls == []

    llm_opener = _RecordingOpener({"choices": [{"message": {"content": "must not be returned"}}]})
    assert (
        run_openai_compat_roundtrip(
            url,
            "local-model",
            "private prompt that must stay local",
            opener=llm_opener,
        )
        is None
    )
    assert llm_opener.calls == []
    assert "private prompt" not in capsys.readouterr().out


def test_local_probes_accept_literal_ipv4_ipv6_and_localhost_with_injected_openers() -> None:
    for url in (
        "http://localhost:8000/bench",
        "https://127.0.0.1:8443/bench",
        "http://127.255.1.2:8000/bench",
        "https://[::1]:8443/bench",
    ):
        opener = _RecordingOpener({"finalizeMs": [11, 13]})
        assert fetch_samples_ms(url, opener=opener) == [11.0, 13.0]
        assert len(opener.calls) == 1

    llm_opener = _RecordingOpener({"choices": [{"message": {"content": "ok"}}]})
    result = run_openai_compat_roundtrip(
        "http://127.0.0.1:11434/v1",
        "local-model",
        "Return ok",
        opener=llm_opener,
    )
    assert result is not None
    assert result.detail == "ok"
    assert len(llm_opener.calls) == 1


def test_default_local_opener_ignores_hostile_proxy_environment(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")

    handlers: list[object] = []
    original_build_opener = local_http.urllib.request.build_opener

    def capture_build_opener(*given_handlers: object):
        handlers.extend(given_handlers)
        return original_build_opener(*given_handlers)

    monkeypatch.setattr(local_http.urllib.request, "build_opener", capture_build_opener)
    build_isolated_local_opener()
    proxy_handlers = [handler for handler in handlers if hasattr(handler, "proxies")]

    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _target_handler(hits: list[bytes]) -> type[BaseHTTPRequestHandler]:
    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            hits.append(b"")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"finalizeMs":[1]}')

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            hits.append(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

    return TargetHandler


def _redirect_handler(location: str) -> type[BaseHTTPRequestHandler]:
    class RedirectHandler(BaseHTTPRequestHandler):
        def _redirect(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self.send_response(307)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _redirect
        do_POST = _redirect

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

    return RedirectHandler


def test_local_probes_reject_redirect_without_forwarding_samples_or_private_prompt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_hits: list[bytes] = []
    with _serve(_target_handler(target_hits)) as target:
        target_url = f"http://127.0.0.1:{target.server_port}/capture"
        with _serve(_redirect_handler(target_url)) as redirect:
            redirect_url = f"http://127.0.0.1:{redirect.server_port}/redirect"

            with pytest.raises(DeviceBenchError, match="Redirects are disabled"):
                fetch_samples_ms(redirect_url)
            assert (
                run_openai_compat_roundtrip(
                    redirect_url,
                    "local-model",
                    "private prompt that must never reach redirect target",
                )
                is None
            )

    assert target_hits == []
    assert "private prompt" not in capsys.readouterr().out
