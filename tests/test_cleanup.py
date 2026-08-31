# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import threading
import time

from dcent_voice.asr.base import Locality
from dcent_voice.config import SnippetEntry, VocabEntry
from dcent_voice.llm.base import LLMProvider
from dcent_voice.llm.cleanup import (
    CleanupPipeline,
    accept_cleanup,
    cleanup_keeps_dictionary_written_forms,
    cleanup_keeps_snippet_expansions,
    normalize_cleanup,
)
from dcent_voice.llm.prompts import cleanup_user_prompt


class ErrorLLM(LLMProvider):
    locality = Locality.LOCAL

    def complete(self, system, user, *, temperature=0.2, max_tokens=1024) -> str:
        raise RuntimeError("offline")

    def complete_structured(self, system, user, schema, *, temperature=0.0):
        return {}

    def complete_tools(self, system, user, tools):
        return []

    def health(self) -> bool:
        return False


class SlowLLM(ErrorLLM):
    def complete(self, system, user, *, temperature=0.2, max_tokens=1024) -> str:
        time.sleep(0.2)
        return "too late"


def test_cleanup_returns_provider_text(fake_llm) -> None:
    fake_llm.response = "I think we should ship it."
    cleanup = CleanupPipeline(fake_llm, enabled=True, timeout_s=1.0)

    assert cleanup.clean("um so i think uh we should ship it") == "I think we should ship it."


def test_cleanup_email_style_reaches_provider(fake_llm) -> None:
    from dcent_voice.llm.prompts import cleanup_system_prompt

    fake_llm.response = "Hi team,\n\nThe build is green."
    cleanup = CleanupPipeline(fake_llm, enabled=True, timeout_s=1.0)
    assert cleanup.clean("hi team the build is green", style="email") == (
        "Hi team,\n\nThe build is green."
    )
    assert "Destination: email" in fake_llm.last_system
    assert "Destination: email" in cleanup_system_prompt("email")
    assert "Destination: email" not in cleanup_system_prompt("plain")
    assert "structured notes" in cleanup_system_prompt("notes").lower()
    assert "structured notes" not in cleanup_system_prompt("plain").lower()


def test_cleanup_disabled_returns_raw(fake_llm) -> None:
    cleanup = CleanupPipeline(fake_llm, enabled=False)

    assert cleanup.clean(" raw words ") == "raw words"


def test_cleanup_error_degrades_to_raw() -> None:
    cleanup = CleanupPipeline(ErrorLLM(), enabled=True, timeout_s=1.0)

    assert cleanup.clean("keep this") == "keep this"


def test_cleanup_circuit_breaker_skips_after_failure() -> None:
    cleanup = CleanupPipeline(ErrorLLM(), enabled=True, timeout_s=1.0, circuit_open_s=30.0)
    assert cleanup.clean("first") == "first"
    # Second call must not invoke the provider while the circuit is open.
    calls = {"n": 0}

    class CountingLLM(ErrorLLM):
        def complete(self, system, user, *, temperature=0.2, max_tokens=1024) -> str:
            calls["n"] += 1
            raise RuntimeError("offline")

    cleanup.provider = CountingLLM()
    assert cleanup.clean("second") == "second"
    assert calls["n"] == 0


def test_cleanup_timeout_degrades_to_raw() -> None:
    cleanup = CleanupPipeline(SlowLLM(), enabled=True, timeout_s=0.01)

    assert cleanup.clean("keep this") == "keep this"


def test_cleanup_preflight_bypasses_unavailable_local_provider() -> None:
    checked = threading.Event()
    calls = {"complete": 0}

    class OfflineLLM(ErrorLLM):
        def health(self) -> bool:
            checked.set()
            return False

        def complete(self, system, user, *, temperature=0.2, max_tokens=1024) -> str:
            calls["complete"] += 1
            time.sleep(0.2)
            return "late"

    cleanup = CleanupPipeline(
        OfflineLLM(),
        enabled=True,
        health_preflight=True,
        health_retry_s=30.0,
    )
    assert checked.wait(1.0)
    deadline = time.monotonic() + 1.0
    while cleanup.availability == "checking" and time.monotonic() < deadline:
        time.sleep(0.005)

    started = time.monotonic()
    assert cleanup.clean("keep this raw") == "keep this raw"
    assert time.monotonic() - started < 0.1
    assert cleanup.availability == "unavailable"
    assert calls["complete"] == 0
    cleanup.close()


def test_cleanup_prompt_includes_dictionary() -> None:
    prompt = cleanup_user_prompt(
        "d central voice",
        (VocabEntry(spoken="d central", written="D-Central"),),
    )

    assert "d central => D-Central" in prompt


def test_cleanup_prompt_lists_starred_terms_first() -> None:
    prompt = cleanup_user_prompt(
        "say it",
        (
            VocabEntry(spoken="plain", written="PlainTerm"),
            VocabEntry(spoken="star", written="StarredTerm", starred=True),
        ),
    )
    assert prompt.index("star => StarredTerm") < prompt.index("plain => PlainTerm")


def test_cleanup_prompt_lists_starred_snippet_cues_first() -> None:
    prompt = cleanup_user_prompt(
        "send it",
        (VocabEntry(spoken="plain", written="PlainTerm"),),
        (
            SnippetEntry(spoken="my email", expansion="plain@example.com"),
            SnippetEntry(spoken="vip cue", expansion="star@example.com", starred=True),
        ),
    )
    assert "vip cue => star@example.com" in prompt
    assert prompt.index("vip cue => star@example.com") < prompt.index("plain => PlainTerm")
    assert prompt.index("vip cue => star@example.com") < prompt.index(
        "my email => plain@example.com"
    )


def test_cleanup_keeps_snippet_expansions(fake_llm) -> None:
    snippets = (SnippetEntry(spoken="my email", expansion="um ops@example.com"),)
    fake_llm.response = "Send ops@example.com."
    cleanup = CleanupPipeline(
        fake_llm,
        enabled=True,
        timeout_s=1.0,
        snippets=snippets,
        circuit_open_s=0.0,
    )
    assert cleanup.clean("Send um ops@example.com.") == "Send um ops@example.com."
    assert (
        cleanup_keeps_snippet_expansions(
            "Send um ops@example.com.",
            "Send ops@example.com.",
            snippets,
        )
        is False
    )
    fake_llm.response = "Please send um ops@example.com."
    assert cleanup.clean("Send um ops@example.com.") == "Please send um ops@example.com."


def test_cleanup_keeps_dictionary_written_forms(fake_llm) -> None:
    dictionary = (VocabEntry(spoken="vip", written="um VIP Corp"),)
    fake_llm.response = "Say VIP Corp."
    cleanup = CleanupPipeline(
        fake_llm,
        enabled=True,
        timeout_s=1.0,
        dictionary=dictionary,
        circuit_open_s=0.0,
    )
    assert cleanup.clean("Say um VIP Corp.") == "Say um VIP Corp."
    assert (
        cleanup_keeps_dictionary_written_forms(
            "Say um VIP Corp.",
            "Say VIP Corp.",
            dictionary,
        )
        is False
    )
    fake_llm.response = "Please say um VIP Corp."
    assert cleanup.clean("Say um VIP Corp.") == "Please say um VIP Corp."


def test_normalize_cleanup_removes_wrapping_quotes() -> None:
    assert normalize_cleanup('"Hello."') == "Hello."


def test_accept_cleanup_rejects_essay_expansion() -> None:
    raw = "fix the map editor please"
    cleaned = "Sure, here is a full multi-paragraph analysis of world editors. " * 20
    ok, reason = accept_cleanup(raw, cleaned)
    assert ok is False
    assert reason in {"too_long", "dissimilar", "preamble"}


def test_accept_cleanup_rejects_preamble() -> None:
    ok, reason = accept_cleanup("hello world", "Here is the cleaned text: hello world")
    assert ok is False
    assert reason == "preamble"


def test_accept_cleanup_keeps_light_edit() -> None:
    ok, reason = accept_cleanup(
        "um so we should fix the map editor",
        "We should fix the map editor.",
    )
    assert ok is True
    assert reason == "ok"


def test_cleanup_pipeline_rejects_invented_essay(fake_llm) -> None:
    fake_llm.response = "Certainly! " + ("Detailed essay about OSRS. " * 40)
    cleanup = CleanupPipeline(fake_llm, enabled=True, timeout_s=1.0)
    assert cleanup.clean("fix the map") == "fix the map"
