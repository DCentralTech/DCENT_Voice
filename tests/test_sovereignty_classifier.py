# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dcent_voice.sovereignty import (
    SovereigntyClass,
    classify_endpoint,
    classify_host,
    sovereignty_class_for_service_bind,
)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.2", "::1"])
def test_service_bind_loopback_is_local(host: str) -> None:
    assert sovereignty_class_for_service_bind(host) is SovereigntyClass.LOCAL


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "voicebox.lan"])
def test_service_bind_wildcard_or_non_loopback_is_lan(host: str) -> None:
    assert sovereignty_class_for_service_bind(host) is SovereigntyClass.LAN


def test_python_classifier_matches_shared_parity_vectors() -> None:
    fixture = _load_parity_fixture()

    for host_case in fixture["hostCases"]:
        expected = SovereigntyClass(host_case["expectedClass"])
        assert classify_host(host_case["input"]) is expected

    for endpoint_case in fixture["endpointCases"]:
        expected = SovereigntyClass(endpoint_case["expectedClass"])
        resolved_addresses = tuple(endpoint_case.get("resolvedAddresses", ()))

        assert classify_endpoint(endpoint_case["url"], resolved_addresses) is expected

    assert len(fixture["hostCases"]) + len(fixture["endpointCases"]) >= 24


def _load_parity_fixture() -> dict[str, Any]:
    path = _shared_parity_fixture_path()
    return json.loads(path.read_text(encoding="utf-8"))


def _shared_parity_fixture_path() -> Path:
    voice_root = Path(__file__).resolve().parents[1]
    ade_fixture = (
        voice_root.parent
        / "DCENT_ADE"
        / "docs"
        / "schemas"
        / "sovereignty"
        / "classifier-parity.json"
    )
    if ade_fixture.exists():
        return ade_fixture

    return voice_root / "tests" / "fixtures" / "classifier-parity.json"
