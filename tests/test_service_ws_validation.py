# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dcent_voice.service.api import ServiceEngine, create_app
from dcent_voice.service.ws import (
    MAX_STREAM_JSON_BYTES,
    WS_CLOSE_POLICY,
    WS_CLOSE_TOO_LARGE,
    add_stream_websocket,
)


def _client(fake_asr: Any) -> TestClient:
    engine = ServiceEngine(asr=fake_asr)
    app = create_app(engine)
    add_stream_websocket(app, engine)
    return TestClient(app)


def _assert_json_close(client: TestClient, payload: Any, code: int) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/stream") as websocket,
    ):
        websocket.send_json(payload)
        websocket.receive_json()
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("final", "true"),
        ("final", 1),
        ("final", None),
        ("polish", "false"),
        ("polish", 0),
        ("polish", None),
    ],
)
def test_stream_rejects_boolean_coercion(fake_asr: Any, field: str, value: Any) -> None:
    _assert_json_close(
        _client(fake_asr),
        {"audio": [0.1], "samplerate": 16_000, field: value},
        WS_CLOSE_POLICY,
    )


@pytest.mark.parametrize("style", [None, 1, True, "", "FORMAL", "telepathy", "x" * 32])
def test_stream_rejects_non_enum_or_non_string_style(fake_asr: Any, style: Any) -> None:
    _assert_json_close(
        _client(fake_asr),
        {"audio": [0.1], "samplerate": 16_000, "style": style},
        WS_CLOSE_POLICY,
    )


def test_stream_rejects_oversized_style(fake_asr: Any) -> None:
    _assert_json_close(
        _client(fake_asr),
        {"audio": [0.1], "samplerate": 16_000, "style": "x" * 33},
        WS_CLOSE_TOO_LARGE,
    )


@pytest.mark.parametrize("app_context", [None, 1, True, "", [], {}])
def test_stream_rejects_invalid_or_empty_app_context(fake_asr: Any, app_context: Any) -> None:
    _assert_json_close(
        _client(fake_asr),
        {"audio": [0.1], "samplerate": 16_000, "app_context": app_context},
        WS_CLOSE_POLICY,
    )


def test_stream_app_context_boundary_and_literal_booleans_remain_valid(fake_asr: Any) -> None:
    client = _client(fake_asr)
    with client.websocket_connect("/stream") as websocket:
        websocket.send_json(
            {
                "audio": [0.1],
                "samplerate": 16_000,
                "final": True,
                "polish": False,
                "style": "formal",
                "app_context": "a" * 128,
            }
        )
        message = websocket.receive_json()

    assert message["type"] == "final"


def test_stream_rejects_oversized_app_context(fake_asr: Any) -> None:
    _assert_json_close(
        _client(fake_asr),
        {"audio": [0.1], "samplerate": 16_000, "app_context": "a" * 129},
        WS_CLOSE_TOO_LARGE,
    )


def test_stream_rejects_malformed_json_and_binary_frames(fake_asr: Any) -> None:
    client = _client(fake_asr)
    with (
        pytest.raises(WebSocketDisconnect) as malformed,
        client.websocket_connect("/stream") as websocket,
    ):
        websocket.send_text("{")
        websocket.receive_json()
    assert malformed.value.code == WS_CLOSE_POLICY

    with (
        pytest.raises(WebSocketDisconnect) as binary,
        client.websocket_connect("/stream") as websocket,
    ):
        websocket.send_bytes(b"{}")
        websocket.receive_json()
    assert binary.value.code == WS_CLOSE_POLICY


@pytest.mark.parametrize("payload", [[], "audio", 1, None, True])
def test_stream_rejects_non_object_json(fake_asr: Any, payload: Any) -> None:
    _assert_json_close(_client(fake_asr), payload, WS_CLOSE_POLICY)


def test_stream_rejects_oversized_json_before_parsing(fake_asr: Any) -> None:
    client = _client(fake_asr)
    payload = json.dumps({"padding": "x" * MAX_STREAM_JSON_BYTES})
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/stream") as websocket,
    ):
        websocket.send_text(payload)
        websocket.receive_json()
    assert excinfo.value.code == WS_CLOSE_TOO_LARGE
