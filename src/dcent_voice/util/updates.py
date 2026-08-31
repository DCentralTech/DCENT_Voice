# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Manual update check.

Queries the project's GitHub "latest release" and compares its version to the
running one so the app can tell the user an update is available. Nothing in
this module schedules or initiates a check. The caller invokes it only for the
Settings action, and downloading/applying remains a separate installer step.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# Canonical public release feed. Keep this slug aligned with [project.urls].
RELEASES_API = "https://api.github.com/repos/DCentralTech/DCENT_Voice/releases/latest"


@dataclass(frozen=True)
class UpdateInfo:
    available: bool
    current: str
    latest: str
    url: str = ""
    # False when the check itself failed (offline, release feed missing) so the
    # UI can say "couldn't check" instead of a false "you're up to date".
    ok: bool = True


def parse_version(value: str) -> tuple[int, ...]:
    core = value.strip().lstrip("vV").split("+")[0].split("-")[0]
    parts: list[int] = []
    for chunk in core.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _prerelease_rank(value: str) -> tuple[int, int]:
    """(1, 0) for a final release, (0, n) for pre-release number n.

    A final release outranks any pre-release of the same core version, so a
    user on 0.2.0b1 is told the 0.2.0 final is available — and 0.2.0b2
    outranks 0.2.0b1 within the beta channel.
    """
    core = value.strip().lstrip("vV").split("+")[0]
    head, sep, tail = core.partition("-")
    # PEP 440 style: pre-release fused into the last chunk ("0.2.0b1").
    for chunk in head.split("."):
        stripped = chunk.lstrip("0123456789")
        if stripped:
            digits = "".join(ch for ch in stripped if ch.isdigit())
            return (0, int(digits) if digits else 0)
    # SemVer style: a "-beta.1" / "-rc.2" suffix.
    if sep:
        digits = "".join(ch for ch in tail if ch.isdigit())
        return (0, int(digits) if digits else 0)
    return (1, 0)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    # Pad to equal length so "1.0.0" == "1.0" instead of comparing as newer.
    width = max(len(a), len(b))
    a_key = a + (0,) * (width - len(a)) + _prerelease_rank(latest)
    b_key = b + (0,) * (width - len(b)) + _prerelease_rank(current)
    return a_key > b_key


def check_for_update(
    current: str,
    *,
    url: str = RELEASES_API,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = 5.0,
) -> UpdateInfo:
    failed = UpdateInfo(available=False, current=current, latest=current, ok=False)
    try:
        destination = httpx.URL(url)
    except (TypeError, ValueError):
        return failed
    if (
        destination.scheme.lower() != "https"
        or not destination.host
        or destination.username
        or destination.password
    ):
        return failed

    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout_s,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.get(
                destination,
                headers={"Accept": "application/vnd.github+json"},
            )
            if response.is_redirect:
                return failed
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return failed
    if not isinstance(data, dict):
        return failed
    latest = str(data.get("tag_name", "")).lstrip("vV")
    if not latest:
        return failed
    return UpdateInfo(
        available=is_newer(latest, current),
        current=current,
        latest=latest,
        url=str(data.get("html_url", "")),
    )
