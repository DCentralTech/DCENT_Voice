# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Classify data flows by sovereignty boundary."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from urllib.parse import urlparse


class SovereigntyClass(StrEnum):
    LOCAL = "LOCAL"
    LAN = "LAN"
    SERVER_EGRESS = "SERVER_EGRESS"
    CLOUD = "CLOUD"
    UNKNOWN = "UNKNOWN"


# --- DVAP capability vocabulary -------------------------------------------------
#
# These plain-data constants describe what the Voice module advertises over DVAP
# (docs/attachment-protocol.md). They live here — in the dependency-light
# sovereignty module — so both the registry writer (`attach.registry`) and the
# `/dvap` endpoint (`service.dvap`) can share one source of truth without pulling
# numpy/streaming into the registry path.

DVAP_PROTOCOL = "dvap"
# Highest protocol version this build implements; the v1.1 sovereignty extension
# is a superset of v1.0. Ordered most-preferred first for version negotiation.
DVAP_SUPPORTED_VERSIONS: tuple[str, ...] = ("1.2", "1.1", "1.0")
DVAP_VERSION = DVAP_SUPPORTED_VERSIONS[0]

# Interactive STT capabilities the module always serves (Wave E0).
STT_CAPABILITIES: tuple[str, ...] = ("stt.partial", "stt.final", "audio.in.stream")

# Text-only compose. Always served; same local compose_dictation as POST /compose.
TEXT_COMPOSE_CAPABILITY = "text.compose"

# TTS capabilities (Wave E1). Advertised and accepted ONLY when a TTS backend is
# actually available (model assets present) — otherwise there is no playback for
# an interrupt to cancel and no meaningfully emittable `barge_in`, so they stay
# out of `hello`/`welcome`. `barge_in` is server-emitted but advertised so ADE
# knows to expect it.
TTS_CAPABILITIES: tuple[str, ...] = (
    "tts.append",
    "tts.cancel",
    "barge_in",
    "tts.synth.stream",
)
VOICE_CONTROL_CAPABILITIES: tuple[str, ...] = ("voice.mode", "voice.devices")


def served_capabilities(
    *, tts_available: bool, voice_control_available: bool = False
) -> tuple[str, ...]:
    """Interactive capabilities `/dvap` accepts in a session (welcome set).

    STT and text compose always; TTS only when a backend is available.
    """

    capabilities = list(STT_CAPABILITIES)
    capabilities.append(TEXT_COMPOSE_CAPABILITY)
    if tts_available:
        capabilities.extend(TTS_CAPABILITIES)
    if voice_control_available:
        capabilities.extend(VOICE_CONTROL_CAPABILITIES)
    return tuple(capabilities)


def advertised_capabilities(
    *, tts_available: bool, voice_control_available: bool = False
) -> tuple[str, ...]:
    """What discovery (registry entry / `hello`) advertises to ADE.

    The served interactive capabilities plus the non-session, consent-gated
    `voice.model.download` descriptor.
    """

    return (
        *served_capabilities(
            tts_available=tts_available,
            voice_control_available=voice_control_available,
        ),
        MODEL_DOWNLOAD_CAPABILITY,
    )


# Downloading speech-model assets is a real, consent-gated capability whose data
# flow is SERVER_EGRESS (the module is local but may fetch from upstream on the
# user's behalf). It is advertised in discovery but is not an interactive
# session capability, so `/dvap` treats a request for it as optional (ignored).
MODEL_DOWNLOAD_CAPABILITY = "voice.model.download"

# What the registry entry advertises to ADE for discovery.
ADVERTISED_CAPABILITIES: tuple[str, ...] = (
    *STT_CAPABILITIES,
    TEXT_COMPOSE_CAPABILITY,
    MODEL_DOWNLOAD_CAPABILITY,
)

# The full DVAP vocabulary this build recognizes as valid protocol capabilities.
# A requested capability outside this set is treated as an unknown REQUIRED
# capability and fails negotiation; a known one the module cannot currently serve
# is an optional capability and is ignored (dropped from the accepted set).
KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "stt.partial",
        "stt.final",
        "tts.append",
        "tts.cancel",
        "barge_in",
        "audio.in.stream",
        "text.compose",
        "tts.synth.stream",
        "voice.mode",
        "voice.devices",
        "voice.model.download",
        "module.sovereignty",
    }
)


def default_capability_sovereignty() -> list[dict[str, str]]:
    """Per-capability data-flow declarations for the registry entry and `hello`.

    Only capabilities whose class differs from — or usefully clarifies — the
    module default (LOCAL) are listed; missing capabilities inherit LOCAL.
    """

    return [
        {
            "capability": "stt.final",
            "sovereigntyClass": SovereigntyClass.LOCAL.value,
            "reason": "Audio transcription remains on this machine.",
        },
        {
            "capability": MODEL_DOWNLOAD_CAPABILITY,
            "sovereigntyClass": SovereigntyClass.SERVER_EGRESS.value,
            "reason": (
                "Model asset download can contact upstream registries only after "
                "explicit user action."
            ),
        },
    ]


# Privacy-status string (see events.PrivacyChanged / privacy.PrivacyStatus) mapped
# to the observed data-flow class ADE should enforce.
_STATUS_TO_CLASS = {
    "sovereign": SovereigntyClass.LOCAL,
    "hybrid": SovereigntyClass.SERVER_EGRESS,
    "cloud": SovereigntyClass.CLOUD,
}


def sovereignty_class_for_status(status: str) -> SovereigntyClass:
    """Map a privacy-status string to its observed sovereignty class."""

    return _STATUS_TO_CLASS.get(status, SovereigntyClass.UNKNOWN)


def sovereignty_class_for_service_bind(host: str) -> SovereigntyClass:
    """Classify the transport boundary created by a service bind address.

    Only an explicit loopback address is process-local. Wildcards (``0.0.0.0``
    and ``::``), private addresses, and named interfaces all expose a network
    boundary and are therefore advertised to ADE as ``LAN``. This classification
    describes service reachability; per-capability processing may still be LOCAL.
    """

    normalized = _normalize_host(host)
    if normalized == "localhost":
        return SovereigntyClass.LOCAL
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return SovereigntyClass.LAN
    return SovereigntyClass.LOCAL if address.is_loopback else SovereigntyClass.LAN


_RISK_RANK = {
    SovereigntyClass.LOCAL: 0,
    SovereigntyClass.LAN: 1,
    SovereigntyClass.SERVER_EGRESS: 2,
    SovereigntyClass.UNKNOWN: 3,
    SovereigntyClass.CLOUD: 4,
}


def classify_ip_address(host: str) -> SovereigntyClass | None:
    normalized = _normalize_host(host)
    ipv4 = _parse_ipv4(normalized)

    if ipv4 is not None:
        a, b, _, _ = ipv4
        if a in {0, 127}:
            return SovereigntyClass.LOCAL
        if (
            a == 10
            or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168)
            or (a == 169 and b == 254)
        ):
            return SovereigntyClass.LAN
        return SovereigntyClass.CLOUD

    if normalized == "::1":
        return SovereigntyClass.LOCAL

    if ":" in normalized:
        if normalized.startswith(("fe80:", "fc", "fd")):
            return SovereigntyClass.LAN
        return SovereigntyClass.CLOUD

    return None


def classify_host(host: str) -> SovereigntyClass:
    normalized = _normalize_host(host)

    if not normalized:
        return SovereigntyClass.CLOUD

    if normalized == "localhost":
        return SovereigntyClass.LOCAL

    ip_class = classify_ip_address(normalized)
    if ip_class is not None:
        return ip_class

    if normalized.endswith((".local", ".lan")):
        return SovereigntyClass.LAN

    return SovereigntyClass.CLOUD


def classify_endpoint(url: str, resolved_addresses: tuple[str, ...] = ()) -> SovereigntyClass:
    if resolved_addresses:
        resolved = tuple(
            classify_ip_address(address) or SovereigntyClass.CLOUD for address in resolved_addresses
        )
        return max(resolved, key=lambda value: _RISK_RANK[value])

    parsed = urlparse(url)
    if not parsed.hostname:
        return SovereigntyClass.CLOUD

    return classify_host(parsed.hostname)


def _normalize_host(host: str) -> str:
    return host.strip().lower().removeprefix("[").removesuffix("]")


def _parse_ipv4(host: str) -> tuple[int, int, int, int] | None:
    parts = host.split(".")
    if len(parts) != 4 or any(part == "" for part in parts):
        return None

    try:
        a, b, c, d = (int(part) for part in parts)
    except ValueError:
        return None

    if any(octet < 0 or octet > 255 for octet in (a, b, c, d)):
        return None

    return a, b, c, d
