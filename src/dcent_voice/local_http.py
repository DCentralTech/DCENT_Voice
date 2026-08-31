# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Isolated HTTP transport for explicitly local diagnostic endpoints."""

from __future__ import annotations

import ipaddress
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


class LocalEndpointError(ValueError):
    """Raised before wire I/O when an endpoint is not unambiguously loopback."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so local payloads cannot be forwarded elsewhere."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        del newurl
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Redirects are disabled for local diagnostic requests",
            headers,
            fp,
        )


def require_loopback_http_url(url: str) -> str:
    """Return *url* iff it names a literal, credential-free loopback endpoint.

    ``localhost`` is the only hostname accepted.  Other names are deliberately
    not resolved: accepting a DNS name that happens to resolve locally would
    make these local-only probes vulnerable to DNS rebinding.
    """

    if not isinstance(url, str) or not url or url != url.strip():
        raise LocalEndpointError("Local endpoint must be a non-empty canonical URL.")
    if any(ord(character) < 0x20 for character in url):
        raise LocalEndpointError("Local endpoint URL contains control characters.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise LocalEndpointError("Local endpoint URL is malformed.") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise LocalEndpointError("Local endpoint must use HTTP or HTTPS.")
    if not parsed.netloc or hostname is None:
        raise LocalEndpointError("Local endpoint must include a loopback host.")
    if parsed.username is not None or parsed.password is not None:
        raise LocalEndpointError("Local endpoint must not contain credentials.")
    if parsed.fragment:
        raise LocalEndpointError("Local endpoint must not contain a URL fragment.")
    if port is not None and not 1 <= port <= 65535:  # pragma: no cover - urlsplit enforces this.
        raise LocalEndpointError("Local endpoint port is invalid.")

    normalized_host = hostname.lower()
    if normalized_host == "localhost":
        return url
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError as exc:
        raise LocalEndpointError(
            "Local endpoint host must be localhost or a literal loopback address."
        ) from exc
    if not address.is_loopback:
        raise LocalEndpointError("Local endpoint address must be loopback.")
    return url


def build_isolated_local_opener() -> urllib.request.OpenerDirector:
    """Build an opener with no ambient proxy support and no redirects."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


def open_local_http(
    request: str | urllib.request.Request,
    *,
    timeout: float,
    opener: Any | None = None,
) -> Any:
    """Open a validated local URL through an isolated, non-redirecting client."""

    url = request.full_url if isinstance(request, urllib.request.Request) else request
    require_loopback_http_url(url)
    local_opener = opener if opener is not None else build_isolated_local_opener()
    return local_opener.open(request, timeout=timeout)
