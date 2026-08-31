# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import inspect
import json
import traceback
from pathlib import Path

import httpx
import pytest

from dcent_voice.attach.registry import restrict_private_file
from dcent_voice.commands import ade_dispatch as ade_dispatch_module
from dcent_voice.commands.ade_dispatch import (
    ADEDispatcher,
    ADEDispatchError,
    ADEDispatchSecurityError,
    ADEToolCallRejected,
)
from dcent_voice.commands.schema import ToolCall


def _tool_call() -> ToolCall:
    return ToolCall(name="open", arguments={"target": "settings"})


def _write_registry(
    root: Path,
    *,
    endpoint: str = "http://127.0.0.1:9842/command",
    token: str = "ade-session-token",
    token_ref: str = "dcent-ade.token",
    module_id: str = "dcent-ade",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    token_path = root / token_ref
    token_path.write_text(token, encoding="ascii")
    restrict_private_file(token_path)
    registry_path = root / "dcent-ade.json"
    registry_path.write_text(
        json.dumps(
            {
                "moduleId": module_id,
                "displayName": "DCENT ADE",
                "version": "1.0.0",
                "endpoint": endpoint,
                "tokenRef": token_ref,
                "sovereigntyClass": "LOCAL",
                "capabilities": ["command.dispatch"],
            }
        ),
        encoding="utf-8",
    )
    restrict_private_file(registry_path)
    return token_path


def test_dispatch_discovers_registry_endpoint_and_authenticates(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"accepted": True})

    dispatcher = ADEDispatcher(
        registry_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    assert dispatcher.dispatch(_tool_call()) == {
        "dispatched": True,
        "response": {"accepted": True},
    }
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://127.0.0.1:9842/command"
    assert request.headers["Authorization"] == "Bearer ade-session-token"
    assert json.loads(request.content) == {
        "name": "open",
        "arguments": {"target": "settings"},
    }


def test_dispatch_refreshes_rotated_registry_token(tmp_path: Path) -> None:
    token_path = _write_registry(tmp_path, token="first-token")
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["Authorization"])
        return httpx.Response(204)

    dispatcher = ADEDispatcher(
        registry_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )
    dispatcher.dispatch(_tool_call())
    token_path.write_text("second-token", encoding="ascii")
    dispatcher.dispatch(_tool_call())

    assert authorizations == ["Bearer first-token", "Bearer second-token"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example/commands",
        "http://192.168.1.20:9842/commands",
        "file:///tmp/ade.sock",
        "http://user:password@127.0.0.1:9842/commands",
    ],
)
def test_explicit_dispatch_rejects_nonlocal_or_credentialed_endpoint(endpoint: str) -> None:
    with pytest.raises(ADEDispatchSecurityError, match="loopback"):
        ADEDispatcher(endpoint=endpoint, token="secret")


def test_environment_endpoint_and_proxy_cannot_select_dispatch_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DCENT_VOICE_ADE_ENDPOINT", "https://attacker.example/collect")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:8080")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    result = ADEDispatcher(
        registry_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ).dispatch(_tool_call())

    assert result["dispatched"] is False
    assert result["reason"] == "no_endpoint"
    assert calls == 0


def test_desktop_construction_does_not_read_legacy_ade_endpoint_environment() -> None:
    from dcent_voice import app

    source = inspect.getsource(app.run_app)
    assert "DCENT_VOICE_ADE_ENDPOINT" not in source
    assert "ade_dispatcher = ADEDispatcher()" in source


def test_pipeline_marks_selected_text_at_dispatch_boundary() -> None:
    from dcent_voice.pipeline import PipelineWorker

    source = inspect.getsource(PipelineWorker._process_current_utterance)
    assert "selection_present=bool(self._selection_at_press)" in source


def test_remote_registry_endpoint_fails_before_transport(tmp_path: Path) -> None:
    _write_registry(tmp_path, endpoint="https://attacker.example/collect")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    dispatcher = ADEDispatcher(
        registry_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ADEDispatchSecurityError, match="loopback"):
        dispatcher.dispatch(_tool_call())
    assert calls == 0


def test_local_endpoint_redirect_is_not_followed_or_given_bearer_secret() -> None:
    secret = "redirect-safe-ade-bearer"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example/collect"})

    dispatcher = ADEDispatcher(
        endpoint="http://127.0.0.1:9842/command",
        token=secret,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ADEDispatchError) as caught:
        dispatcher.dispatch(_tool_call())

    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"
    assert secret not in "".join(traceback.format_exception(caught.value))


def test_registry_token_must_stay_inside_registry_directory(tmp_path: Path) -> None:
    registry_dir = tmp_path / "registry"
    outside = tmp_path / "outside.token"
    outside.write_text("file-content-must-not-become-a-bearer", encoding="ascii")
    _write_registry(registry_dir, token_ref="placeholder.token")
    payload = json.loads((registry_dir / "dcent-ade.json").read_text(encoding="utf-8"))
    payload["tokenRef"] = str(outside)
    (registry_dir / "dcent-ade.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ADEDispatchSecurityError, match="registry boundary"):
        ADEDispatcher(registry_dir=registry_dir).dispatch(_tool_call())


def test_registry_token_owner_only_verification_fails_before_read_or_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_path = _write_registry(tmp_path, token="must-not-be-read")
    checked: list[Path] = []
    calls = 0

    def verifier(path: Path) -> None:
        checked.append(path)
        if path == token_path.resolve():
            raise PermissionError("simulated non-owner access")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    monkeypatch.setattr(ade_dispatch_module, "verify_private_file", verifier)
    dispatcher = ADEDispatcher(
        registry_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ADEDispatchSecurityError, match="not owner-only"):
        dispatcher.dispatch(_tool_call())
    assert checked == [(tmp_path / "dcent-ade.json").resolve(), token_path.resolve()]
    assert calls == 0


def test_timeout_after_receiver_commit_is_never_retried_or_rendered_with_secret() -> None:
    secret = "top-secret-ade-bearer"
    committed: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Model the dangerous ambiguous case: ADE committed the side effect, but
        # Voice timed out before receiving its acknowledgement.
        committed.append(json.loads(request.content))
        raise httpx.ReadTimeout("reply lost after commit", request=request)

    dispatcher = ADEDispatcher(
        endpoint="http://127.0.0.1:9842/command",
        token=secret,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ADEDispatchError, match="Local ADE command dispatch failed") as caught:
        dispatcher.dispatch(_tool_call())

    rendered = "".join(traceback.format_exception(caught.value))
    assert committed == [{"name": "open", "arguments": {"target": "settings"}}]
    assert secret not in str(caught.value)
    assert secret not in rendered
    assert secret not in repr(dispatcher)


def test_http_error_is_not_retried_and_does_not_render_bearer_secret() -> None:
    secret = "rejected-ade-bearer"
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"detail": "wrong token"})

    dispatcher = ADEDispatcher(
        endpoint="http://localhost:9842/command",
        token=secret,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ADEDispatchError) as caught:
        dispatcher.dispatch(_tool_call())

    assert calls == 1
    assert secret not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("delete_file", {"target": "all"}),
        ("execute_command", {"target": "format C:"}),
        ("OPEN", {"target": "settings"}),
        ("open", {"target": "powershell"}),
        ("open", {"target": "settings", "command": "delete files"}),
        ("create", {"target": "release note", "command": "delete files"}),
        ("search", {}),
        ("search", {"target": 7}),
        ("search", {"target": "safe\nexecute destructive instruction"}),
        ("summarize", {"target": "x" * 257}),
    ],
)
def test_unknown_destructive_or_malformed_tool_calls_fail_before_network(
    name: str,
    arguments: dict[str, object],
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    dispatcher = ADEDispatcher(
        endpoint="http://127.0.0.1:9842/command",
        token="safe-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ADEToolCallRejected):
        dispatcher.dispatch(ToolCall(name=name, arguments=arguments))
    assert calls == 0


@pytest.mark.parametrize("name", ["create", "search", "summarize"])
def test_allowed_tool_schemas_accept_only_one_bounded_target(name: str) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    dispatcher = ADEDispatcher(
        endpoint="http://127.0.0.1:9842/command",
        token="safe-token",
        transport=httpx.MockTransport(handler),
    )
    dispatcher.dispatch(ToolCall(name=name, arguments={"target": "release notes"}))

    assert bodies == [{"name": name, "arguments": {"target": "release notes"}}]


def test_selected_text_blocks_tool_automation_before_registry_or_network(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    dispatcher = ADEDispatcher(
        registry_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ADEToolCallRejected, match="selected text"):
        dispatcher.dispatch(_tool_call(), selection_present=True)
    assert calls == 0
