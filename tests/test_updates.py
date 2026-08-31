# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from dcent_voice.util.updates import RELEASES_API, check_for_update, is_newer, parse_version


class _CountingTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.hits = 0
        super().__init__(("127.0.0.1", 0), _CountingHandler)


class _CountingHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _CountingTcpServer)
        server.hits += 1


@contextmanager
def _counting_listener() -> Iterator[_CountingTcpServer]:
    server = _CountingTcpServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_release_feed_matches_canonical_public_repository() -> None:
    assert RELEASES_API == "https://api.github.com/repos/DCentralTech/DCENT_Voice/releases/latest"


def test_public_repository_metadata_is_consistent() -> None:
    repository = "https://github.com/DCentralTech/DCENT_Voice"

    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert f'Homepage = "{repository}"' in pyproject
    assert f'Repository = "{repository}"' in pyproject
    assert f'Issues = "{repository}/issues"' in pyproject
    assert repository in readme


def test_parse_version_strips_prefix_and_suffix() -> None:
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("0.1.0+dev") == (0, 1, 0)
    assert parse_version("2.0") == (2, 0)


def test_is_newer_compares_correctly() -> None:
    assert is_newer("0.2.0", "0.1.9") is True
    assert is_newer("0.1.0", "0.1.0") is False
    assert is_newer("0.1.0", "0.2.0") is False


def test_is_newer_understands_prerelease_versions() -> None:
    # The final release outranks its own betas; later betas outrank earlier.
    assert is_newer("0.2.0", "0.2.0b1") is True
    assert is_newer("0.2.0b1", "0.2.0") is False
    assert is_newer("0.2.0b2", "0.2.0b1") is True
    assert is_newer("0.2.0-beta.2", "0.2.0-beta.1") is True
    assert is_newer("0.2.0", "0.2.0-beta.1") is True
    assert is_newer("0.2.1b1", "0.2.0") is True
    assert is_newer("0.2.0b1", "0.2.0b1") is False


def test_is_newer_pads_unequal_lengths() -> None:
    # "1.0.0" and "1.0" are the same version, not an update.
    assert is_newer("1.0.0", "1.0") is False
    assert is_newer("1.0", "1.0.0") is False
    assert is_newer("1.0.1", "1.0") is True


def _transport(tag: str) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tag_name": tag, "html_url": "https://example/releases"})

    return httpx.MockTransport(handler)


def test_check_reports_available_update() -> None:
    info = check_for_update("0.1.0", transport=_transport("v0.3.0"))
    assert info.ok is True
    assert info.available is True
    assert info.latest == "0.3.0"
    assert info.url == "https://example/releases"


def test_check_reports_up_to_date() -> None:
    info = check_for_update("0.3.0", transport=_transport("v0.3.0"))
    assert info.ok is True
    assert info.available is False


def test_check_reports_failure_not_up_to_date() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    info = check_for_update("0.1.0", transport=httpx.MockTransport(handler))
    # A failed check must be distinguishable from "up to date".
    assert info.ok is False
    assert info.available is False
    assert info.current == "0.1.0"


def test_check_rejects_non_https_destination_before_wire() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"tag_name": "v9.9.9"})

    info = check_for_update(
        "0.1.0",
        url="http://updates.example.invalid/latest",
        transport=httpx.MockTransport(handler),
    )

    assert info.ok is False
    assert requests == []


def test_check_does_not_follow_redirect() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://redirected.example.invalid/latest"},
        )

    info = check_for_update("0.1.0", transport=httpx.MockTransport(handler))

    assert info.ok is False
    assert requests == [RELEASES_API]


def test_check_rejects_non_object_release_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"tag_name": "v9.9.9"}])

    info = check_for_update("0.1.0", transport=httpx.MockTransport(handler))

    assert info.ok is False


def test_check_ignores_hostile_ambient_proxy(monkeypatch) -> None:
    with _counting_listener() as destination, _counting_listener() as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(name, proxy_url)
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)

        info = check_for_update(
            "0.1.0",
            url=f"https://127.0.0.1:{destination.server_address[1]}/latest",
            timeout_s=1.0,
        )

    assert info.ok is False
    assert destination.hits == 1
    assert proxy.hits == 0
