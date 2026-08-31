# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from dcent_voice.dictation.style import (
    apply_style,
    extract_spoken_list,
    peel_spoken_style,
    resolve_style,
)


def test_code_style_drops_period_on_cli() -> None:
    assert apply_style("git status.", "code") == "git status"
    assert apply_style("git status?", "code") == "git status"
    assert apply_style("Please run the tests.", "code") == "Please run the tests."


def test_chat_style_drops_short_period() -> None:
    assert apply_style("Open settings.", "chat") == "Open settings"
    assert apply_style("What time is the meeting?", "chat") == "What time is the meeting?"


def test_email_style_breaks_after_greeting() -> None:
    text = apply_style("Hi team the build is green.", "email")
    assert text.startswith("Hi team,")
    assert "\n\n" in text
    assert text.split("\n\n", 1)[1].startswith("The build is green")


def test_email_style_promotes_spoken_closing() -> None:
    text = apply_style("The build is green thanks", "email")
    assert text.endswith("Thanks,")
    assert "\n\n" in text
    assert "build is green" in text


def test_formal_style_expands_contractions() -> None:
    assert "do not" in apply_style("I don't think so.", "formal").lower()


def test_formal_style_expands_informal() -> None:
    text = apply_style("Yeah I gonna try.", "formal")
    assert "yes" in text.lower()
    assert "going to" in text.lower()


def test_code_style_strips_docker_period() -> None:
    assert apply_style("docker ps.", "code") == "docker ps"


def test_code_style_joins_camel_and_snake() -> None:
    assert apply_style("rename camel case user name.", "code") == "rename userName."
    assert apply_style("use snake case user name.", "code") == "use user_name."


def test_code_style_renders_explicit_spoken_python_function() -> None:
    spoken = "Define function get user id (user id) colon.\nReturn user _ id."
    assert apply_style(spoken, "code") == "def get_user_id(user_id):\n    return user_id"
    assert apply_style("Define function ready () colon", "code") == "def ready():\n    pass"


def test_code_style_renders_spoken_structures_and_cli_flags() -> None:
    assert apply_style("For each item in items colon.\nPrint (item)", "code") == (
        "for item in items:\n    print(item)"
    )
    assert apply_style("Create class HTTP client colon.\nPass", "code") == (
        "class HTTPClient:\n    pass"
    )
    assert apply_style("Const user id equals get user ();", "code") == (
        "const user_id = get_user();"
    )
    assert apply_style("Please run kubectl get pods dash n production.", "code") == (
        "kubectl get pods -n production"
    )


def test_code_style_renders_members_conditions_assignments_and_docker_flags() -> None:
    assert apply_style("Response dot status code", "code") == "response.status_code"
    assert apply_style("Raw value dot strip ()", "code") == "raw_value.strip()"
    assert apply_style("If user dot enabled colon.\nReturn user dot id.", "code") == (
        "if user.enabled:\n    return user.id"
    )
    assert apply_style("Let response equals await fetch (url);", "code") == (
        "let response = await fetch(url);"
    )
    assert apply_style("Run docker compose dash dash profile dev up dash d.", "code") == (
        "docker compose --profile dev up -d"
    )


def test_code_style_renders_compound_blocks_indexes_and_quoted_cli() -> None:
    compound = (
        "For each HTTP response in responses colon.\n"
        "If response dot status code greater than 399 colon.\n"
        "Return response dot URL."
    )
    assert apply_style(compound, "code") == (
        "for http_response in responses:\n"
        "    if response.status_code > 399:\n"
        "        return response.url"
    )
    assert apply_style("Payload [user id]", "code") == "payload[user_id]"
    assert apply_style("Let key equals await build key (request dot user id);", "code") == (
        "let key = await build_key(request.user_id);"
    )
    assert apply_style("Git commit -m quote fix offline mode quote.", "code") == (
        'git commit -m "fix offline mode"'
    )


def test_email_style_numbers_spoken_ordinal_list() -> None:
    text = apply_style("first milk second eggs third bread", "email")
    assert text == "1. Milk\n2. Eggs\n3. Bread"
    assert extract_spoken_list("buy milk and then eggs and then bread") == [
        "buy milk",
        "eggs",
        "bread",
    ]
    chat = apply_style("buy milk and then eggs and then bread", "chat")
    assert chat.startswith("- ")
    assert "Eggs" in chat or "eggs" in chat
    assert apply_style("first milk second eggs", "plain") == "first milk second eggs"


def test_email_style_splits_subject_and_body() -> None:
    text = apply_style("subject launch delay body we slipped a day thanks", "email")
    assert text.startswith("Subject: launch delay")
    assert "\n\n" in text
    assert text.rstrip().endswith("Thanks,")


def test_email_task_list_is_a_destination_document() -> None:
    spoken = "hey can you send the deck to Bob and then update the timeline thanks"
    email = apply_style(spoken, "email")
    chat = apply_style(spoken, "chat")
    assert email.startswith("Hey,")
    assert "1. Send the deck to Bob" in email
    assert "2. Update the timeline" in email
    assert email.rstrip().endswith("Thanks,")
    assert chat.startswith("- ")
    assert "\n\n" not in chat
    assert "Thanks" not in chat
    assert "send the deck to Bob" in chat or "Send the deck to Bob" in chat
    narrative = apply_style("I went to the store and then I came home.", "email")
    assert "1." not in narrative


def test_resolve_style_uses_app_map_and_override() -> None:
    assert resolve_style("plain", "Code.exe") == "code"
    assert resolve_style("plain", "Code.exe", {"Code.exe": "formal"}) == "formal"
    assert resolve_style("email", "notepad.exe") == "email"
    assert resolve_style("plain", "thunderbird.exe") == "email"
    assert resolve_style("plain", "msedge.exe", window_title="Inbox (2) - user@gmail.com") == (
        "email"
    )
    assert resolve_style("plain", "msedge.exe", window_title="Google") == "plain"
    assert resolve_style("plain", "chrome.exe", window_title="Slack | eng") == "chat"
    assert (
        resolve_style(
            "plain",
            "chrome.exe",
            {"chrome.exe": "code"},
            window_title="Inbox (2) - user@gmail.com",
        )
        == "code"
    )
    assert (
        resolve_style(
            "plain",
            "notepad.exe",
            learned_per_app={"notepad.exe": "email"},
        )
        == "email"
    )
    assert (
        resolve_style(
            "plain",
            "msedge.exe",
            window_title="Inbox (2) - user@gmail.com",
            learned_per_app={"msedge.exe": "chat"},
        )
        == "email"
    )
    assert (
        resolve_style(
            "plain",
            "Code.exe",
            {"Code.exe": "plain"},
            learned_per_app={"code.exe": "formal"},
        )
        == "plain"
    )


def test_email_style_tell_addressee_is_sendable() -> None:
    text = apply_style("tell Alex the invoice is ready.", "email")
    assert text.startswith("Hi Alex,")
    assert "The invoice is ready." in text
    assert text.rstrip().endswith("Thanks,")


def test_chat_style_strips_just_wanted_to_say() -> None:
    text = apply_style("I just wanted to say the build is green.", "chat")
    assert "just wanted" not in text.lower()
    assert "build is green" in text.lower()


def test_email_style_rewrites_spoken_tone_without_inventing_addressee() -> None:
    text = apply_style("I just wanted to say yeah the invoice is gonna be ready.", "email")
    assert "just wanted" not in text.lower()
    assert "yeah" not in text.lower()
    assert "gonna" not in text.lower()
    assert "invoice is going to be ready" in text.lower()
    assert "Hi " not in text


def test_chat_style_strips_wondering_lead() -> None:
    text = apply_style("I was wondering if the build is green.", "chat")
    assert "wondering" not in text.lower()
    assert "build is green" in text.lower()


def test_formal_style_drops_safe_hedges() -> None:
    text = apply_style("To be honest I don't think so.", "formal")
    assert "to be honest" not in text.lower()
    assert "do not" in text.lower()


def test_plain_and_librispeech_line_are_not_restyled() -> None:
    spoken = "He could wait no longer."
    assert apply_style(spoken, "plain") == spoken
    assert apply_style(spoken, "formal") == spoken


def test_notes_style_structures_decision_action_next() -> None:
    text = apply_style(
        "we decided to ship Monday action item update the timeline next step ping alex",
        "notes",
    )
    assert "## Decision" in text
    assert "Ship Monday." in text
    assert "## Action items" in text
    assert "- Update the timeline" in text
    assert "## Next steps" in text
    assert "- Ping alex" in text
    assert "Hi " not in text
    assert "Thanks" not in text


def test_notes_style_lists_become_action_items() -> None:
    text = apply_style("first milk second eggs third bread", "notes")
    assert text == "## Action items\n- Milk\n- Eggs\n- Bread"


def test_notes_style_leaves_clean_prose_alone() -> None:
    spoken = "The build is green."
    assert apply_style(spoken, "notes") == spoken


def test_resolve_style_maps_notes_apps_and_titles() -> None:
    assert resolve_style("plain", "Notion.exe") == "notes"
    assert resolve_style("plain", "Obsidian.exe") == "notes"
    assert resolve_style("plain", "ONENOTE.EXE") == "notes"
    assert resolve_style("plain", "msedge.exe", window_title="Sprint board | Notion") == ("notes")
    assert resolve_style("plain", "notepad.exe") == "plain"


def test_peel_spoken_style_leading_and_trailing_cues() -> None:
    assert peel_spoken_style("email style hey send the deck thanks") == (
        "email",
        "hey send the deck thanks",
    )
    assert peel_spoken_style("as notes we decided to ship Monday") == (
        "notes",
        "we decided to ship Monday",
    )
    assert peel_spoken_style("use code style git status") == ("code", "git status")
    assert peel_spoken_style("in chat style ship monday") == ("chat", "ship monday")
    assert peel_spoken_style("send the deck as an email") == ("email", "send the deck")
    assert peel_spoken_style("um email style hey send the deck") == (
        "email",
        "hey send the deck",
    )
    assert peel_spoken_style("code style is important") == (None, "code style is important")
    assert peel_spoken_style("I sent an email yesterday") == (None, "I sent an email yesterday")
    assert peel_spoken_style("The build is green.") == (None, "The build is green.")
