# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from dcent_voice.asr.base import ASRProvider, Locality, TranscriptResult  # noqa: E402
from dcent_voice.inject.base import Injector  # noqa: E402
from dcent_voice.llm.base import LLMProvider  # noqa: E402

INTERACTIVE_TEST_ENV = "DCENT_VOICE_ALLOW_INTERACTIVE_TESTS"


def interactive_tests_enabled() -> bool:
    """Return true only for the single explicit live-desktop opt-in value."""

    return os.environ.get(INTERACTIVE_TEST_ENV) == "1"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-hw",
        action="store_true",
        default=False,
        help="Run hardware/model integration tests.",
    )
    parser.addoption(
        "--run-tts-models",
        action="store_true",
        default=False,
        help="Run TTS tests that load/download real Kokoro model assets.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_hw = None if config.getoption("--run-hw") else pytest.mark.skip(reason="requires --run-hw")
    skip_interactive = (
        None
        if interactive_tests_enabled()
        else pytest.mark.skip(reason=f"requires explicit {INTERACTIVE_TEST_ENV}=1")
    )
    skip_tts = (
        None
        if config.getoption("--run-tts-models")
        else pytest.mark.skip(reason="requires --run-tts-models")
    )
    for item in items:
        if skip_hw is not None and "hw" in item.keywords:
            item.add_marker(skip_hw)
        if skip_interactive is not None and "interactive" in item.keywords:
            item.add_marker(skip_interactive)
        if skip_tts is not None and "tts_models" in item.keywords:
            item.add_marker(skip_tts)


class FakeASRProvider(ASRProvider):
    locality = Locality.LOCAL

    def __init__(self, text: str = "hello world") -> None:
        self.text = text
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False

    def transcribe(
        self,
        audio: object,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> TranscriptResult:
        return TranscriptResult(
            text=self.text,
            language="en",
            duration_s=1.0,
            asr_latency_s=0.01,
        )


class FakeLLMProvider(LLMProvider):
    locality = Locality.LOCAL

    def __init__(self, response: str = "cleaned text", healthy: bool = True) -> None:
        self.response = response
        self.healthy = healthy
        self.last_system = ""
        self.last_user = ""

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        self.last_system = system
        self.last_user = user
        return self.response

    def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict[str, object],
        *,
        temperature: float = 0.0,
    ) -> dict[str, object]:
        return {"text": self.response}

    def complete_tools(
        self,
        system: str,
        user: str,
        tools: list[dict[str, object]],
    ) -> list[object]:
        return []

    def health(self) -> bool:
        return self.healthy


class FakeInjector(Injector):
    def __init__(self) -> None:
        self.injected: list[str] = []
        self.retracted: list[int] = []
        self.enters: int = 0
        self.buffer = ""

    def inject(self, text: str) -> None:
        self.injected.append(text)
        self.buffer += text

    def retract(self, char_count: int) -> None:
        n = max(0, int(char_count))
        self.retracted.append(n)
        if n:
            self.buffer = self.buffer[:-n] if n <= len(self.buffer) else ""

    def press_enter(self) -> None:
        self.enters += 1


@pytest.fixture(autouse=True)
def _isolated_profile_root(tmp_path_factory, monkeypatch):
    """Give every test its own profile root, so the suite cannot touch the user.

    Without this, any test that constructs a real path — or spawns the frozen
    exe, which inherits this process's environment — reads and writes the
    developer's live ``%APPDATA%\\DCENT_Voice`` and
    ``%LOCALAPPDATA%\\DCENT\\modules``. That made results depend on whether a
    real ``dcent-voice.exe`` happened to be running: the shipped-default
    relaunch tests assert that no personalization store is created, and a live
    instance recreating ``personalization.json`` failed them for reasons that
    had nothing to do with the code under test.

    ``DCENT_VOICE_PROFILE_ROOT`` relocates the config, logs, privacy ledger,
    recovery journal, personalization store, durable model root and ADE registry
    together (see ``dcent_voice.util.paths``), so one variable is enough. A test
    that needs the platform location still sets or deletes the variable itself;
    ``monkeypatch`` restores this default afterwards either way.
    """
    root = tmp_path_factory.mktemp("profile")
    monkeypatch.setenv("DCENT_VOICE_PROFILE_ROOT", str(root))
    # A test run must never make something appear on the machine it runs on:
    # no modal dialog to block on, no Explorer/Finder window, and no rewrite of
    # the OS login item. All three are read by the code that would do it, so a
    # child process spawned by a test inherits the same restraint.
    monkeypatch.setenv("DCENT_VOICE_NO_DIALOGS", "1")
    monkeypatch.setenv("DCENT_VOICE_NO_OPEN", "1")
    monkeypatch.setenv("DCENT_VOICE_DISABLE_AUTOSTART", "1")

    # bootlog resolves the startup-log path once and memoises it for the life of
    # the process — correct for the app, which is one process per profile, but in
    # a test session the first caller would pin every later test (and possibly
    # the developer's real profile) to that one path.
    from dcent_voice.util import bootlog

    bootlog.reset_boot_log_path()
    yield
    # Leave no memo pointing into a temporary directory that is about to vanish.
    bootlog.reset_boot_log_path()


@pytest.fixture(autouse=True)
def _reset_cli_transcribe_sticky() -> None:
    from dcent_voice.app import reset_cli_compose_sticky, reset_cli_transcribe_sticky

    reset_cli_transcribe_sticky()
    reset_cli_compose_sticky()
    yield
    reset_cli_transcribe_sticky()
    reset_cli_compose_sticky()


@pytest.fixture
def fake_asr() -> FakeASRProvider:
    return FakeASRProvider()


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture
def fake_injector() -> FakeInjector:
    return FakeInjector()


@pytest.fixture
def fake_tts():
    from dcent_voice.tts import FakeTtsBackend

    return FakeTtsBackend()
