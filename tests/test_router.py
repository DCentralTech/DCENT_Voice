# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from dcent_voice.asr.base import Locality
from dcent_voice.commands.router import CommandRouter
from dcent_voice.llm.base import LLMProvider


class RepairLLM(LLMProvider):
    locality = Locality.LOCAL

    def complete(self, system, user, *, temperature=0.2, max_tokens=1024) -> str:
        return '{"action":"insert_text","text":"repaired","confidence":0.8,"reason":"repair"}'

    def complete_structured(self, system, user, schema, *, temperature=0.0):
        raise ValueError("bad json")

    def complete_tools(self, system, user, tools):
        return []

    def health(self) -> bool:
        return True


def test_rules_fallback_rewrites_selection_more_formal() -> None:
    intent = CommandRouter().route("make this more formal", "we can't ship late")

    assert intent.action == "rewrite_selection"
    assert "cannot" in intent.text.lower()
    assert intent.text.endswith(".")

    missing_apostrophe = CommandRouter().route("formalize", "we cant ship late")
    assert missing_apostrophe.text == "We cannot ship late."


def test_rules_fallback_fixes_grammar_and_warms_tone() -> None:
    grammar = CommandRouter().route("fix the grammar", "im ready but he are late")
    assert grammar.action == "rewrite_selection"
    assert grammar.text == "I'm ready but he is late."
    agreement = CommandRouter().route("proofread", "she dont know and it have failed")
    assert agreement.text == "She does not know and it has failed."
    plural = CommandRouter().route("fix grammar", "they has not replied and the builds is broken")
    assert plural.text == "They have not replied and the builds are broken."

    warmer = CommandRouter().route("make this friendlier", "send the report today")
    assert warmer.action == "rewrite_selection"
    assert warmer.text == "Could you please send the report today? Thank you."


def test_rules_fallback_translation_without_local_llm_fails_closed() -> None:
    intent = CommandRouter().route("translate this to French", "The build is green.")
    assert intent.action == "noop"
    assert intent.text == ""
    assert intent.reason == "offline_translation_requires_local_llm"

    persuasive = CommandRouter().route("make this persuasive", "The plan saves time.")
    assert persuasive.action == "noop"
    assert persuasive.text == ""
    assert persuasive.reason == "unsupported_selection_transform_requires_local_llm"


def test_rules_fallback_uppercase_and_bullets() -> None:
    upper = CommandRouter().route("make this uppercase", "ship it")
    assert upper.action == "rewrite_selection"
    assert upper.text == "SHIP IT"

    bullets = CommandRouter().route("make a bullet list", "milk, eggs, bread")
    assert bullets.action == "rewrite_selection"
    assert bullets.text.startswith("- ")
    assert "eggs" in bullets.text


def test_rules_fallback_shortens_redundancy_even_below_hard_limit() -> None:
    intent = CommandRouter().route(
        "make this shorter",
        "I just wanted to let you know that we really need to finish in order to ship",
    )
    assert intent.action == "rewrite_selection"
    assert intent.text == "we need to finish to ship"


def test_rules_fallback_dev_case_and_unbullet() -> None:
    snake = CommandRouter().route("snake case", "Hello World API")
    assert snake.action == "rewrite_selection"
    assert snake.text == "hello_world_api"

    camel = CommandRouter().route("camel case", "hello world api")
    assert camel.text == "helloWorldApi"

    kebab = CommandRouter().route("kebab case", "Hello World API")
    assert kebab.text == "hello-world-api"

    constant = CommandRouter().route("constant case", "hello world api")
    assert constant.text == "HELLO_WORLD_API"

    plain = CommandRouter().route("as plain text", "- milk\n- eggs")
    assert plain.text == "milk eggs"


def test_rules_fallback_remove_bullets_beats_generic_bullet_transform() -> None:
    for command in ("remove bullets", "remove bullet points", "unbullet this"):
        intent = CommandRouter().route(command, "- milk\n- eggs")

        assert intent.action == "rewrite_selection"
        assert intent.reason == "rules_unbullet"
        assert intent.text == "milk eggs"


def test_rules_fallback_quote_number_list_and_spacing() -> None:
    quoted = CommandRouter().route("quote this", "ship it")
    assert quoted.text == '"ship it"'

    numbered = CommandRouter().route("numbered list", "milk, eggs, bread")
    assert numbered.text.startswith("1. ")
    assert "2. eggs" in numbered.text

    spaced = CommandRouter().route("fix spacing", "too   many\n\n\nspaces")
    assert "too many" in spaced.text
    assert "\n\n\n" not in spaced.text

    reversed_words = CommandRouter().route("reverse this", "alpha beta gamma")
    assert reversed_words.text == "gamma beta alpha"


def test_rules_fallback_arithmetic_inserts_answer() -> None:
    intent = CommandRouter().route("what's 2+2")

    assert intent.action == "insert_text"
    assert intent.text == "4"


def test_rules_fallback_tool_call() -> None:
    intent = CommandRouter().route("open project dashboard")

    assert intent.action == "tool_call"
    assert intent.tool_call is not None
    assert intent.tool_call.name == "open"


def test_router_repairs_malformed_structured_output_before_rules() -> None:
    intent = CommandRouter(RepairLLM()).route("ignored")

    assert intent.action == "insert_text"
    assert intent.text == "repaired"
    assert intent.reason == "repair"
