# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path

import pytest

from dcent_voice.asr.base import Locality
from dcent_voice.config import (
    STARTER_SNIPPETS,
    ASRSpec,
    ConfigError,
    LLMSpec,
    PersonalizationConfig,
    SnippetEntry,
    VocabEntry,
    dictionary_export_payload,
    dictionary_undo_steps_help,
    effective_snippets,
    ensure_user_config,
    export_done_toast,
    export_download_name,
    export_empty_toast,
    import_applied_toast,
    import_apply_button_label,
    import_cancel_button_label,
    import_cancelled_toast,
    import_done_button_label,
    import_done_help_label,
    import_empty_review_summary,
    import_review_summary,
    import_undo_button_label,
    import_undo_help_label,
    import_undone_toast,
    load_config,
    load_dictionary_import,
    load_snippet_import,
    matches_list_query,
    merge_snippet_entries,
    merge_vocab_entries,
    overlay_priority_title,
    parse_config,
    parse_snippet_import,
    plan_dictionary_import,
    plan_snippet_import,
    remove_visible_confirm,
    remove_visible_empty_toast,
    remove_visible_needs_search,
    remove_visible_noun,
    remove_visible_search_toast,
    remove_visible_toast,
    snippet_export_payload,
    snippet_import_row_starred,
    snippet_star_aria,
    snippet_undo_steps_help,
    starred_count_detail,
    starred_import_detail,
    starred_only_empty_label,
    starred_only_entries,
)


def test_example_config_loads() -> None:
    config = load_config(Path("config.example.toml"), create=False)

    assert config.active_profile == "desktop"
    assert config.current_profile.asr.provider == "parakeet"
    assert config.current_profile.asr.model == "tdt-0.6b-v3"
    assert config.current_profile.asr.compute_type == "int8"
    assert config.current_profile.asr.locality.value == "local"
    assert config.profiles["quality"].asr.model == "distil-small.en"
    from dcent_voice.asr.faster_whisper_provider import resolve_device_compute

    assert resolve_device_compute(config.profiles["tiny"].asr) == ("cpu", "int8")
    # The shipped default profile is fully standalone: local transcription with
    # the optional AI cleanup pass off, so a fresh install needs nothing else
    # installed or running.
    assert config.current_profile.llm.provider == "none"
    assert config.current_profile.llm.enabled is False
    assert config.current_profile.cleanup_enabled is False
    assert config.idle_unload_s == 600.0
    assert config.session_locality is Locality.LOCAL
    assert "tiny" in config.profiles
    assert config.profiles["tiny"].asr.compute_type == "cpu-int8"
    # The laptop profile is the shipped example of optional AI cleanup enabled.
    laptop = config.profiles["laptop"]
    assert laptop.llm.provider == "ollama"
    assert laptop.cleanup_enabled is True
    assert config.dictionary[0].written == "D-Central"
    assert config.dictation.local_polish is True
    assert config.dictation.cleanup_level == "medium"
    assert config.dictation.spoken_edits is True
    assert config.dictation.developer_terms is True
    assert config.snippets is None
    assert effective_snippets(config.snippets) == STARTER_SNIPPETS
    assert config.injector.default == "clipboard"
    assert config.injector.paste_delay_s == 0.10
    assert config.injector.short_text_keystroke_chars == 48
    assert config.language_mode == "english"
    assert config.personalization.enabled is True
    assert config.personalization.learn is True
    assert config.personalization.prose_context is False
    assert config.hotkeys.dictation == "ctrl+win"
    assert config.hotkeys.command == "off"
    assert config.hotkeys.streaming == "off"
    assert config.style.default == "plain"
    assert "multilingual" in config.profiles
    assert config.profiles["multilingual"].asr.model == "base"
    assert config.profiles["whispercpp"].asr.provider == "whisper-cpp"
    assert config.profiles["parakeet"].asr.provider == "parakeet"
    assert config.injector.per_app["WindowsTerminal.exe"] == "keystroke"


@pytest.mark.parametrize("language", ["zz", "fr-CA", "eng", 1, True])
def test_config_rejects_unrecognized_language_identifier(language: object) -> None:
    raw = {
        "language": language,
        "profile": {
            "desktop": {
                "asr": "faster-whisper:base:cpu-int8",
                "llm": "none",
            }
        },
    }
    with pytest.raises(ConfigError, match="language"):
        parse_config(raw)


@pytest.mark.parametrize("field", ["enabled", "learn", "prose_context"])
@pytest.mark.parametrize("invalid", ["false", "true", 1, 0, None, [], {}])
def test_personalization_policy_requires_boolean(field: str, invalid: object) -> None:
    raw = {
        "profile": {
            "desktop": {
                "asr": "faster-whisper:tiny:int8",
                "llm": "none",
            }
        },
        "personalization": {field: invalid},
    }

    with pytest.raises(ConfigError, match=rf"personalization.{field}"):
        parse_config(raw)


@pytest.mark.parametrize("field", ["enabled", "learn", "prose_context"])
@pytest.mark.parametrize("invalid", ["false", "true", 1, 0, None, [], {}])
def test_programmatic_personalization_config_requires_boolean(field: str, invalid: object) -> None:
    values = {"enabled": True, "learn": True, "prose_context": False}
    values[field] = invalid

    with pytest.raises(TypeError, match=rf"personalization.{field}"):
        PersonalizationConfig(**values)  # type: ignore[arg-type]


def test_snippets_and_dictation_parse(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[dictation]
local_polish = false
spoken_edits = true
developer_terms = false

[[snippets.items]]
spoken = "my calendar"
expansion = "https://cal.example/me"

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    assert config.dictation.local_polish is False
    assert config.dictation.developer_terms is False
    assert len(config.snippets) == 1
    assert config.snippets[0].spoken == "my calendar"
    assert "cal.example" in config.snippets[0].expansion
    assert effective_snippets(config.snippets) == config.snippets


def test_cleared_snippets_do_not_resurrect(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[snippets]
items = []

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )
    config = load_config(config_path, create=False)
    assert config.snippets == ()
    assert effective_snippets(config.snippets) == ()


def test_snippet_import_json_merges_and_skips_empty_cues() -> None:
    existing = (SnippetEntry(spoken="my calendar", expansion="old"),)
    incoming = parse_snippet_import(
        '{"schema":"dcent-snippets-v1","items":['
        '{"spoken":"my calendar","expansion":"https://cal.example/me"},'
        '{"spoken":"","expansion":"skip"},'
        '{"spoken":"my email","expansion":"ops@example.com"}]}'
    )
    merged = merge_snippet_entries(existing, incoming)
    assert [entry.spoken for entry in merged] == ["my calendar", "my email"]
    assert merged[0].expansion == "old"


def test_merge_snippet_entries_skips_existing_library_cues() -> None:
    merged = merge_snippet_entries(
        (SnippetEntry(spoken="keep me", expansion="saved"),),
        (SnippetEntry(spoken="keep me", expansion="newer"),),
    )
    assert merged[0].expansion == "saved"
    assert [entry.spoken for entry in merged] == ["keep me"]


def test_snippet_import_accepts_name_text_format() -> None:
    incoming = parse_snippet_import('[{"name":"my address","text":"123 Main Street"}]')
    assert incoming[0].spoken == "my address"
    assert incoming[0].expansion == "123 Main Street"


def test_snippet_starred_roundtrips_export_and_defaults_false() -> None:
    incoming = parse_snippet_import(
        '{"items":[{"spoken":"my email","expansion":"a@b.c","starred":true}]}'
    )
    assert incoming[0].starred is True
    payload = snippet_export_payload(incoming)
    assert payload["items"][0]["starred"] is True
    generic = parse_snippet_import('[{"name":"my address","text":"123 Main Street"}]')
    assert generic[0].starred is False


def test_list_prefs_roundtrip_and_reject_unknown(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    text = Path("config.example.toml").read_text(encoding="utf-8")
    path.write_text(
        text + '\n[lists]\ndictionary_sort = "starred"\nsnippet_sort = "za"\n',
        encoding="utf-8",
    )
    config = load_config(path, create=False)
    assert config.lists.dictionary_sort == "starred"
    assert config.lists.snippet_sort == "za"
    path.write_text(
        text + '\n[lists]\ndictionary_sort = "team"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="dictionary_sort"):
        load_config(path, create=False)
    path.write_text(
        text + '\n[lists]\ndictionary_sort = "newest"\nsnippet_sort = "oldest"\n',
        encoding="utf-8",
    )
    recency = load_config(path, create=False)
    assert recency.lists.dictionary_sort == "newest"
    assert recency.lists.snippet_sort == "oldest"
    path.write_text(
        text
        + "\n[lists]\n"
        + 'dictionary_sort = "starred"\n'
        + 'snippet_sort = "za"\n'
        + "dictionary_starred_only = true\n"
        + "snippet_starred_only = true\n",
        encoding="utf-8",
    )
    starred_only = load_config(path, create=False)
    assert starred_only.lists.dictionary_starred_only is True
    assert starred_only.lists.snippet_starred_only is True
    assert starred_only.lists.dictionary_sort == "starred"


def test_added_at_roundtrips_and_defaults_empty(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8")
        + "\n[[dictionary.terms]]\n"
        + 'spoken = "new term"\n'
        + 'written = "New Term"\n'
        + 'added_at = "2026-08-18T12:00:00Z"\n',
        encoding="utf-8",
    )
    config = load_config(path, create=False)
    stamped = [entry for entry in config.dictionary if entry.spoken == "new term"]
    assert stamped[0].added_at == "2026-08-18T12:00:00Z"
    assert any(entry.added_at == "" for entry in config.dictionary if entry.spoken != "new term")


def test_snippet_export_omits_first_run_starters() -> None:
    payload = snippet_export_payload(None)
    assert payload["schema"] == "dcent-snippets-v1"
    assert payload["items"] == []


def test_snippet_import_rejects_non_json() -> None:
    with pytest.raises(ConfigError, match="JSON"):
        parse_snippet_import("not json")


def test_plan_snippet_import_counts_empty_and_dictionary_skips() -> None:
    entries, parse_stats = load_snippet_import(
        [
            {"spoken": "", "expansion": "gone"},
            {"title": "not a cue"},
            {"spoken": "my calendar", "expansion": "https://cal.example/me"},
            {"spoken": "D-Central", "expansion": "skip me"},
        ]
    )
    assert parse_stats["skipped_empty"] == 2
    merged, stats = plan_snippet_import(
        None,
        entries,
        dictionary=(VocabEntry(spoken="D-Central", written="D-Central"),),
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert [entry.spoken for entry in merged] == ["my calendar"]
    assert stats["added"] == 1
    assert stats["skipped_dictionary"] == 1
    assert stats["skipped_empty"] == 2
    assert stats["skipped"] == 3
    assert stats["applied"] == 1
    assert stats["read"] == 4
    assert stats["changes"] == [
        {"spoken": "", "expansion": "gone", "action": "skip", "reason": "empty"},
        {"spoken": "", "expansion": "", "action": "skip", "reason": "empty"},
        {
            "spoken": "my calendar",
            "expansion": "https://cal.example/me",
            "action": "add",
        },
        {"spoken": "D-Central", "expansion": "skip me", "action": "skip", "reason": "dictionary"},
    ]


def test_snippet_import_skips_malformed_rows_and_keeps_valid() -> None:
    incoming, parse_stats = load_snippet_import(
        [
            {"spoken": "keep me", "expansion": "ok"},
            {"spoken": "x" * 61, "expansion": "too long cue"},
            "not-an-object",
            {"spoken": "also keep", "text": 123},
        ]
    )
    assert [entry.spoken for entry in incoming] == ["keep me"]
    assert parse_stats["skipped_malformed"] == 3
    merged, stats = plan_snippet_import(
        None,
        incoming,
        skipped_empty=parse_stats["skipped_empty"],
        skipped_malformed=parse_stats["skipped_malformed"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert stats["added"] == 1
    assert stats["skipped_malformed"] == 3
    assert stats["applied"] == 1
    assert stats["skipped"] == 3
    assert stats["changes"] == [
        {"spoken": "keep me", "expansion": "ok", "action": "add"},
        {"spoken": "x" * 61, "expansion": "too long cue", "action": "skip", "reason": "malformed"},
        {"spoken": "", "expansion": "", "action": "skip", "reason": "malformed"},
        {"spoken": "also keep", "expansion": "", "action": "skip", "reason": "malformed"},
    ]


def test_plan_snippet_import_lists_empty_only_as_skip_rows() -> None:
    incoming, parse_stats = load_snippet_import(
        [{"spoken": "", "expansion": "gone"}, {"title": "not a cue"}]
    )
    merged, stats = plan_snippet_import(
        None,
        incoming,
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert merged == ()
    assert stats["applied"] == 0
    assert stats["skipped_empty"] == 2
    assert stats["changes"] == [
        {"spoken": "", "expansion": "gone", "action": "skip", "reason": "empty"},
        {"spoken": "", "expansion": "", "action": "skip", "reason": "empty"},
    ]


def test_plan_snippet_import_in_file_duplicates_count_once() -> None:
    incoming, _parse_stats = load_snippet_import(
        [
            {"spoken": "dupe", "expansion": "first"},
            {"spoken": "dupe", "expansion": "second"},
        ]
    )
    merged, stats = plan_snippet_import(None, incoming)
    assert [entry.spoken for entry in merged] == ["dupe"]
    assert merged[0].expansion == "first"
    assert stats["added"] == 1
    assert stats["replaced"] == 0
    assert stats["applied"] == 1
    assert stats["skipped_duplicate"] == 1
    assert stats["skipped"] == 1
    assert stats["changes"] == [
        {"spoken": "dupe", "expansion": "first", "action": "add"},
        {"spoken": "dupe", "expansion": "second", "action": "skip", "reason": "duplicate"},
    ]


def test_plan_snippet_import_counts_later_duplicates_as_skipped() -> None:
    incoming, _parse_stats = load_snippet_import(
        [
            {"spoken": "dupe", "expansion": "first"},
            {"spoken": "dupe", "expansion": "second"},
            {"spoken": "dupe", "expansion": "third"},
        ]
    )
    _merged, stats = plan_snippet_import(None, incoming)
    assert stats["added"] == 1
    assert stats["applied"] == 1
    assert stats["skipped_duplicate"] == 2
    assert stats["skipped"] == 2
    assert stats["changes"] == [
        {"spoken": "dupe", "expansion": "first", "action": "add"},
        {"spoken": "dupe", "expansion": "second", "action": "skip", "reason": "duplicate"},
        {"spoken": "dupe", "expansion": "third", "action": "skip", "reason": "duplicate"},
    ]


def test_plan_snippet_import_replaces_changed_expansion() -> None:
    existing = (
        SnippetEntry(
            spoken="keep me",
            expansion="saved",
            starred=True,
            added_at="2026-01-01T00:00:00+00:00",
        ),
    )
    incoming, _parse_stats = load_snippet_import(
        [
            {"spoken": "keep me", "expansion": "newer"},
            {"spoken": "fresh", "expansion": "ok"},
        ]
    )
    merged, stats = plan_snippet_import(existing, incoming)
    assert [entry.spoken for entry in merged] == ["keep me", "fresh"]
    assert merged[0].expansion == "newer"
    assert merged[0].starred is True
    assert merged[0].added_at == "2026-01-01T00:00:00+00:00"
    assert stats["added"] == 1
    assert stats["replaced"] == 1
    assert stats["skipped_existing"] == 0
    assert stats["applied"] == 2
    assert stats["changes"] == [
        {
            "spoken": "keep me",
            "expansion": "newer",
            "action": "replace",
            "starred": True,
        },
        {"spoken": "fresh", "expansion": "ok", "action": "add"},
    ]


def test_plan_snippet_import_json_applies_star_on_replace() -> None:
    existing = (SnippetEntry(spoken="keep me", expansion="saved"),)
    incoming, _parse_stats = load_snippet_import(
        [{"spoken": "keep me", "expansion": "newer", "starred": True}]
    )
    merged, stats = plan_snippet_import(existing, incoming)
    assert merged[0].expansion == "newer"
    assert merged[0].starred is True
    assert stats["replaced"] == 1
    assert stats["changes"][0]["starred"] is True


def test_plan_snippet_import_skips_unchanged_existing() -> None:
    existing = (SnippetEntry(spoken="keep me", expansion="saved", starred=True),)
    incoming, _parse_stats = load_snippet_import([{"spoken": "keep me", "expansion": "saved"}])
    merged, stats = plan_snippet_import(existing, incoming)
    assert [entry.spoken for entry in merged] == ["keep me"]
    assert merged[0].expansion == "saved"
    assert merged[0].starred is True
    assert stats["added"] == 0
    assert stats["applied"] == 0
    assert stats["skipped_existing"] == 1
    assert stats["replaced"] == 0
    assert stats["changes"] == [
        {"spoken": "keep me", "expansion": "saved", "action": "skip", "reason": "existing"}
    ]


def test_plan_snippet_import_later_existing_copies_count_as_duplicate() -> None:
    existing = (SnippetEntry(spoken="keep me", expansion="saved"),)
    incoming, _parse_stats = load_snippet_import(
        [
            {"spoken": "keep me", "expansion": "newer"},
            {"spoken": "keep me", "expansion": "third"},
        ]
    )
    merged, stats = plan_snippet_import(existing, incoming)
    assert merged[0].expansion == "newer"
    assert stats["added"] == 0
    assert stats["replaced"] == 1
    assert stats["applied"] == 1
    assert stats["skipped_existing"] == 0
    assert stats["skipped_duplicate"] == 1
    assert stats["changes"] == [
        {"spoken": "keep me", "expansion": "newer", "action": "replace"},
        {"spoken": "keep me", "expansion": "third", "action": "skip", "reason": "duplicate"},
    ]


def test_plan_snippet_import_later_dictionary_copies_count_as_duplicate() -> None:
    incoming, _parse_stats = load_snippet_import(
        [
            {"spoken": "D-Central", "expansion": "nope"},
            {"spoken": "D-Central", "expansion": "again"},
        ]
    )
    merged, stats = plan_snippet_import(
        None,
        incoming,
        dictionary=(VocabEntry(spoken="D-Central", written="D-Central"),),
    )
    assert merged == ()
    assert stats["skipped_dictionary"] == 1
    assert stats["skipped_duplicate"] == 1
    assert stats["changes"] == [
        {"spoken": "D-Central", "expansion": "nope", "action": "skip", "reason": "dictionary"},
        {"spoken": "D-Central", "expansion": "again", "action": "skip", "reason": "duplicate"},
    ]


def test_plan_snippet_import_all_dictionary_lists_skip_rows() -> None:
    incoming, _parse_stats = load_snippet_import([{"spoken": "D-Central", "expansion": "nope"}])
    merged, stats = plan_snippet_import(
        None,
        incoming,
        dictionary=(VocabEntry(spoken="D-Central", written="D-Central"),),
    )
    assert merged == ()
    assert stats["added"] == 0
    assert stats["applied"] == 0
    assert stats["skipped_dictionary"] == 1
    assert stats["changes"] == [
        {"spoken": "D-Central", "expansion": "nope", "action": "skip", "reason": "dictionary"}
    ]


def test_snippet_import_skips_blank_expansions() -> None:
    incoming, parse_stats = load_snippet_import(
        [
            {"spoken": "keep me", "expansion": "ok"},
            {"spoken": "no text key"},
            {"spoken": "ws exp", "text": "   "},
        ]
    )
    assert [entry.spoken for entry in incoming] == ["keep me"]
    assert parse_stats["skipped_empty"] == 2
    _, stats = plan_snippet_import(
        None,
        incoming,
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert stats["changes"] == [
        {"spoken": "keep me", "expansion": "ok", "action": "add"},
        {"spoken": "no text key", "expansion": "", "action": "skip", "reason": "empty"},
        {"spoken": "ws exp", "expansion": "   ", "action": "skip", "reason": "empty"},
    ]


def test_dictionary_import_csv_one_and_two_columns() -> None:
    incoming, parse_stats = load_dictionary_import("Flow\nmishear,D-Central\n")
    assert [(entry.spoken, entry.written) for entry in incoming] == [
        ("Flow", "Flow"),
        ("mishear", "D-Central"),
    ]
    merged, stats = plan_dictionary_import(
        None,
        incoming,
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert stats["added"] == 2
    assert stats["applied"] == 2
    assert stats["changes"] == [
        {"spoken": "Flow", "expansion": "Flow", "action": "add"},
        {"spoken": "mishear", "expansion": "D-Central", "action": "add"},
    ]
    assert [entry.spoken for entry in merged] == ["Flow", "mishear"]


def test_plan_dictionary_import_marks_starred_add() -> None:
    incoming, parse_stats = load_dictionary_import("vip,VIP,true\nplain,Plain\n")
    _merged, stats = plan_dictionary_import(
        None,
        incoming,
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert stats["changes"] == [
        {"spoken": "vip", "expansion": "VIP", "action": "add", "starred": True},
        {"spoken": "plain", "expansion": "Plain", "action": "add"},
    ]
    assert incoming[0].starred is True
    assert incoming[1].starred is False
    assert stats["starred_added"] == 1
    assert stats["starred_detail"] == ", starred 1"


def test_starred_import_detail_empty_when_none() -> None:
    assert starred_import_detail(0) == ""
    assert starred_import_detail(3) == ", starred 3"


def test_snippet_star_aria_names_dictation_priority() -> None:
    assert snippet_star_aria(False) == "Star snippet for dictation priority"
    assert snippet_star_aria(True) == "Starred — dictation priority on this machine"


def test_starred_count_detail_empty_when_none() -> None:
    assert starred_count_detail(0) == ""
    assert starred_count_detail(2) == ", 2 starred"


def test_starred_only_empty_label() -> None:
    assert starred_only_empty_label("terms") == "No starred terms"
    assert starred_only_empty_label("snippets") == "No starred snippets"


def test_remove_visible_needs_search_unless_starred_only() -> None:
    assert remove_visible_needs_search("", False) is True
    assert remove_visible_needs_search("", True) is False
    assert remove_visible_needs_search("vip", False) is False


def test_remove_visible_search_toast_names_starred_only() -> None:
    assert remove_visible_search_toast() == "Search or Starred only, then remove visible"


def test_overlay_priority_title_matches_starred_form() -> None:
    assert overlay_priority_title("VIP") == "Starred: VIP"
    assert overlay_priority_title("  my email  ") == "Starred: my email"
    assert overlay_priority_title("") == ""
    assert overlay_priority_title("   ") == ""


def test_snippet_import_row_starred_skips_never_claim_star() -> None:
    assert snippet_import_row_starred({"action": "add", "starred": True}) is True
    assert snippet_import_row_starred({"action": "replace", "starred": True}) is True
    assert snippet_import_row_starred({"action": "add"}) is False
    assert snippet_import_row_starred({"action": "skip", "starred": True}) is False
    assert (
        snippet_import_row_starred({"action": "skip", "reason": "existing", "starred": True})
        is False
    )
    assert snippet_import_row_starred(None) is False


def test_export_follows_search_filter() -> None:
    assert matches_list_query("vip", "VIP", query="vip") is True
    assert matches_list_query("plain", "Plain", query="vip") is False
    assert matches_list_query("plain", "Plain", query="") is True
    assert export_empty_toast(False, "vip") == "Nothing visible to export"
    assert export_empty_toast(True, "") == "Nothing starred to export"
    assert export_empty_toast(False, "") == "Nothing to export"
    assert export_done_toast("dictionary", False, "vip") == "Dictionary exported — visible"
    assert export_done_toast("snippets", True, "vip") == "Snippets exported — visible"
    assert export_done_toast("dictionary", False, "") == "Dictionary exported — stars included"
    assert export_done_toast("dictionary", True, "") == "Dictionary exported — starred only"
    assert export_done_toast("snippets", False, "") == "Snippets exported"
    assert export_done_toast("snippets", True, "") == "Snippets exported — starred only"
    assert export_download_name("dictionary", "vip") == "dcent-dictionary-visible.csv"
    assert export_download_name("snippets", "vip") == "dcent-snippets-visible.json"
    assert export_download_name("dictionary", "") == "dcent-dictionary.csv"
    assert export_download_name("snippets", "") == "dcent-snippets.json"
    assert export_download_name("dictionary", "", True) == "dcent-dictionary-starred.csv"
    assert export_download_name("snippets", "", True) == "dcent-snippets-starred.json"
    assert export_download_name("dictionary", "vip", True) == "dcent-dictionary-visible.csv"
    assert import_cancelled_toast("dictionary") == "Dictionary import cancelled"
    assert import_cancelled_toast("snippets") == "Snippet import cancelled"
    assert (
        import_applied_toast("dictionary", 1, 0, 0)
        == "Dictionary imported 1, replaced 0, skipped 0"
    )
    assert (
        import_applied_toast("snippets", 2, 1, 3, ", 1 starred")
        == "Snippets imported 2, replaced 1, skipped 3, 1 starred"
    )
    assert (
        import_review_summary("dictionary", 1, 0, 0)
        == "Dictionary: import 1, replace 0, skip 0. Nothing is saved until you apply."
    )
    assert (
        import_review_summary("snippets", 2, 1, 3, ", 1 starred")
        == "Snippets: import 2, replace 1, skip 3, 1 starred. Nothing is saved until you apply."
    )
    assert (
        import_empty_review_summary("dictionary", 4)
        == "Dictionary: Nothing will be imported. Skip 4."
    )
    assert (
        import_empty_review_summary("snippets", 2, " (2 empty)")
        == "Snippets: Nothing will be imported. Skip 2 (2 empty)."
    )
    assert import_undone_toast() == "Snippet import undone"
    assert import_undone_toast("dictionary") == "Dictionary import undone"
    assert import_undo_button_label() == "Undo snippet import"
    assert import_undo_button_label("dictionary") == "Undo dictionary import"
    assert import_undo_help_label() == "Undo snippet import"
    assert import_undo_help_label("dictionary") == "Undo dictionary import"
    assert dictionary_undo_steps_help() == "Dictionary undo steps through each apply"
    assert snippet_undo_steps_help() == "Snippet undo steps through each apply"
    assert import_apply_button_label("dictionary") == "Apply dictionary import"
    assert import_apply_button_label("snippets") == "Apply snippet import"
    assert import_cancel_button_label("dictionary") == "Cancel dictionary import"
    assert import_cancel_button_label("snippets") == "Cancel snippet import"
    assert import_done_button_label("dictionary") == "Done dictionary import"
    assert import_done_button_label("snippets") == "Done snippet import"
    assert import_done_help_label("dictionary") == "Done dictionary import"
    assert import_done_help_label("snippets") == "Done snippet import"
    payload = snippet_export_payload(
        (
            SnippetEntry(spoken="plain", expansion="hello"),
            SnippetEntry(spoken="vip", expansion="VIP", starred=True),
        ),
        query="vip",
    )
    assert [item["spoken"] for item in payload["items"]] == ["vip"]


def test_remove_visible_confirm_counts_stars() -> None:
    assert remove_visible_confirm(3, 1, "rows") == (
        "Remove 3 visible rows, 1 starred? This is on this machine — not a cloud team bulk delete."
    )
    assert remove_visible_confirm(1, 0, "row") == (
        "Remove 1 visible row? This is on this machine — not a cloud team bulk delete."
    )
    assert remove_visible_noun("dictionary", 1) == "term"
    assert remove_visible_noun("dictionary", 3) == "terms"
    assert remove_visible_noun("snippets", 1) == "snippet"
    assert remove_visible_noun("snippets", 2) == "snippets"
    assert remove_visible_confirm(3, 1, remove_visible_noun("dictionary", 3)) == (
        "Remove 3 visible terms, 1 starred? This is on this machine — not a cloud team bulk delete."
    )
    assert remove_visible_toast(2, 0, remove_visible_noun("snippets", 2)) == (
        "Removed 2 visible snippets — save to keep"
    )


def test_remove_visible_toast_counts_stars() -> None:
    assert remove_visible_toast(3, 1, "rows") == "Removed 3 visible rows, 1 starred — save to keep"
    assert remove_visible_toast(1, 0, "row") == "Removed 1 visible row — save to keep"


def test_remove_visible_empty_toast_starred_only() -> None:
    assert remove_visible_empty_toast(False) == "No snippets to remove"
    assert remove_visible_empty_toast(False, "dictionary") == "No terms to remove"
    assert remove_visible_empty_toast(False, "snippets") == "No snippets to remove"
    assert remove_visible_empty_toast(True) == "Nothing starred snippets to remove"
    assert remove_visible_empty_toast(True, "dictionary") == "Nothing starred terms to remove"
    assert remove_visible_empty_toast(True, "snippets") == "Nothing starred snippets to remove"


def test_plan_dictionary_import_replaces_changed_written_and_skips_snippet_cues() -> None:
    incoming, parse_stats = load_dictionary_import(
        "keep me,newer\nmy email,ops@example.com\nfresh,ok\nkeep me,again\n"
    )
    merged, stats = plan_dictionary_import(
        (
            VocabEntry(
                spoken="keep me",
                written="saved",
                starred=True,
                added_at="2026-01-01T00:00:00+00:00",
            ),
        ),
        incoming,
        snippets=(SnippetEntry(spoken="my email", expansion="hi"),),
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert [entry.spoken for entry in merged] == ["keep me", "fresh"]
    assert merged[0].written == "newer"
    assert merged[0].starred is True
    assert merged[0].added_at == "2026-01-01T00:00:00+00:00"
    assert stats["skipped_existing"] == 0
    assert stats["replaced"] == 1
    assert stats["skipped_snippet"] == 1
    assert stats["skipped_duplicate"] == 1
    assert stats["added"] == 1
    assert stats["applied"] == 2
    assert stats["changes"] == [
        {
            "spoken": "keep me",
            "expansion": "newer",
            "action": "replace",
            "starred": True,
        },
        {
            "spoken": "my email",
            "expansion": "ops@example.com",
            "action": "skip",
            "reason": "snippet",
        },
        {"spoken": "fresh", "expansion": "ok", "action": "add"},
        {
            "spoken": "keep me",
            "expansion": "again",
            "action": "skip",
            "reason": "duplicate",
        },
    ]


def test_plan_dictionary_import_skips_unchanged_existing() -> None:
    incoming, parse_stats = load_dictionary_import("keep me,saved\n")
    merged, stats = plan_dictionary_import(
        (VocabEntry(spoken="keep me", written="saved", starred=True),),
        incoming,
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert merged[0].written == "saved"
    assert merged[0].starred is True
    assert stats["skipped_existing"] == 1
    assert stats["replaced"] == 0
    assert stats["applied"] == 0
    assert stats["changes"] == [
        {
            "spoken": "keep me",
            "expansion": "saved",
            "action": "skip",
            "reason": "existing",
        },
    ]


def test_plan_dictionary_import_three_column_csv_applies_star_on_replace() -> None:
    incoming, parse_stats = load_dictionary_import("keep me,newer,true\n")
    merged, stats = plan_dictionary_import(
        (VocabEntry(spoken="keep me", written="saved"),),
        incoming,
        skipped_empty=parse_stats["skipped_empty"],
        read=parse_stats["read"],
        skip_changes=parse_stats["skip_changes"],
        entry_indexes=parse_stats["entry_indexes"],
    )
    assert merged[0].written == "newer"
    assert merged[0].starred is True
    assert stats["replaced"] == 1
    assert stats["changes"][0]["starred"] is True


def test_merge_vocab_entries_skips_existing() -> None:
    merged = merge_vocab_entries(
        (VocabEntry(spoken="keep me", written="saved"),),
        (VocabEntry(spoken="keep me", written="newer"),),
    )
    assert merged[0].written == "saved"


def test_dictionary_import_rejects_over_cap() -> None:
    with pytest.raises(ConfigError, match="1,000 entries"):
        load_dictionary_import("\n".join(f"word{i}" for i in range(1001)))


def test_dictionary_import_accepts_sixty_char_words() -> None:
    from dcent_voice.config import MAX_DICTIONARY_SPOKEN, MAX_DICTIONARY_WRITTEN

    assert MAX_DICTIONARY_SPOKEN == 60
    assert MAX_DICTIONARY_WRITTEN == 4000
    word60 = "a" * 60
    incoming, parse_stats = load_dictionary_import(f"{word60}\n")
    assert incoming == (VocabEntry(spoken=word60, written=word60),)
    assert parse_stats["skipped_malformed"] == 0
    too_long = "b" * 61
    incoming, parse_stats = load_dictionary_import(f"{too_long}\nkeep,ok\n")
    assert [(entry.spoken, entry.written) for entry in incoming] == [("keep", "ok")]
    assert parse_stats["skipped_malformed"] == 1
    assert parse_stats["skip_changes"][0]["reason"] == "malformed"


def test_dictionary_import_accepts_four_thousand_char_corrections() -> None:
    from dcent_voice.config import MAX_DICTIONARY_WRITTEN

    assert MAX_DICTIONARY_WRITTEN == 4000
    written = "c" * 4000
    incoming, parse_stats = load_dictionary_import(f"fix,{written}\n")
    assert incoming == (VocabEntry(spoken="fix", written=written),)
    assert parse_stats["skipped_malformed"] == 0
    too_long = "d" * 4001
    incoming, parse_stats = load_dictionary_import(f"fix,{too_long}\nkeep,ok\n")
    assert [(entry.spoken, entry.written) for entry in incoming] == [("keep", "ok")]
    assert parse_stats["skipped_malformed"] == 1
    assert parse_stats["skip_changes"][0]["reason"] == "malformed"


def test_dictionary_export_csv_roundtrips_starred_column() -> None:
    csv_text = dictionary_export_payload(
        (
            VocabEntry(spoken="Flow", written="Flow"),
            VocabEntry(spoken="mishear", written="D-Central, Inc.", starred=True),
        )
    )
    assert "mishear" in csv_text
    assert "true" in csv_text
    incoming, parse_stats = load_dictionary_import(csv_text)
    assert [(entry.spoken, entry.written, entry.starred) for entry in incoming] == [
        ("Flow", "Flow", False),
        ("mishear", "D-Central, Inc.", True),
    ]
    assert parse_stats["skipped_empty"] == 0
    assert dictionary_export_payload(()) == ""
    starred_csv = dictionary_export_payload(
        (
            VocabEntry(spoken="plain", written="Plain"),
            VocabEntry(spoken="vip", written="VIP", starred=True),
        ),
        starred_only=True,
    )
    assert starred_csv == "vip,VIP,true\n"
    assert (
        dictionary_export_payload(
            (
                VocabEntry(spoken="plain", written="Plain"),
                VocabEntry(spoken="vip", written="VIP", starred=True),
            ),
            query="vip",
        )
        == "vip,VIP,true\n"
    )
    assert (
        dictionary_export_payload(
            (
                VocabEntry(spoken="plain", written="Plain"),
                VocabEntry(spoken="vip", written="VIP", starred=True),
            ),
            starred_only=True,
            query="nope",
        )
        == ""
    )
    assert (
        dictionary_export_payload(
            (VocabEntry(spoken="plain", written="Plain"),),
            starred_only=True,
        )
        == ""
    )
    only_starred = starred_only_entries(
        (
            VocabEntry(spoken="plain", written="Plain"),
            VocabEntry(spoken="vip", written="VIP", starred=True),
        ),
        True,
    )
    assert [(entry.spoken, entry.starred) for entry in only_starred] == [("vip", True)]
    two_col, stats = load_dictionary_import("plain,Plain\n")
    assert two_col == (VocabEntry(spoken="plain", written="Plain"),)
    assert two_col[0].starred is False
    assert stats["skipped_malformed"] == 0
    four_col, stats = load_dictionary_import("a,b,true,extra\nkeep,ok\n")
    assert [(entry.spoken, entry.written) for entry in four_col] == [("keep", "ok")]
    assert stats["skipped_malformed"] == 1


def test_dictionary_starred_roundtrips_and_defaults_false(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8")
        + "\n[[dictionary.terms]]\n"
        + 'spoken = "star me"\n'
        + 'written = "Star Me"\n'
        + "starred = true\n",
        encoding="utf-8",
    )
    config = load_config(path, create=False)
    starred = [entry for entry in config.dictionary if entry.spoken == "star me"]
    assert len(starred) == 1
    assert starred[0].starred is True
    plain = [entry for entry in config.dictionary if entry.spoken != "star me"]
    assert plain
    assert all(entry.starred is False for entry in plain)


def test_snippet_import_caps_match_library_size() -> None:
    from dcent_voice.config import MAX_SNIPPET_IMPORT_BYTES, MAX_SNIPPET_IMPORT_ITEMS

    assert MAX_SNIPPET_IMPORT_ITEMS == 1000
    assert MAX_SNIPPET_IMPORT_BYTES == 3_000_000
    incoming, parse_stats = load_snippet_import(
        [{"spoken": f"cue {i}", "expansion": "ok"} for i in range(1000)]
    )
    assert len(incoming) == 1000
    assert parse_stats["skipped_overflow"] == 0
    with pytest.raises(ConfigError, match="1,000 entries"):
        load_snippet_import([{"spoken": f"cue {i}", "expansion": "ok"} for i in range(1001)])


def test_injector_paste_delay_parses(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[injector]
paste_delay_s = 0.75

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )

    assert load_config(config_path, create=False).injector.paste_delay_s == 0.75


def test_injector_paste_delay_rejects_negative(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[injector]
paste_delay_s = -0.01

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="paste_delay_s"):
        load_config(config_path, create=False)


def test_asr_spec_with_compute_type() -> None:
    spec = ASRSpec.parse("faster-whisper:distil-small.en:int8")

    assert spec.provider == "faster-whisper"
    assert spec.model == "distil-small.en"
    assert spec.compute_type == "int8"
    assert spec.locality is Locality.LOCAL


def test_llm_spec_preserves_colon_in_model_name() -> None:
    spec = LLMSpec.parse("ollama:qwen2.5:7b")

    assert spec.provider == "ollama"
    assert spec.model == "qwen2.5:7b"
    assert spec.enabled is True
    assert spec.locality is Locality.LOCAL


def test_none_llm_is_local_and_disabled() -> None:
    spec = LLMSpec.parse("none")

    assert spec.provider == "none"
    assert spec.model is None
    assert spec.enabled is False
    assert spec.locality is Locality.LOCAL


def test_migrate_stale_desktop_asr_and_language_mode() -> None:
    from dcent_voice.config import migrate_raw

    raw = migrate_raw(
        {
            "config_version": 1,
            "language": "en",
            "profile": {
                "desktop": {
                    "asr": "faster-whisper:distil-small.en:cpu-int8",
                    "llm": "none",
                    "cleanup_enabled": False,
                }
            },
        }
    )
    assert raw["language_mode"] == "english"
    assert raw["profile"]["desktop"]["asr"] == "faster-whisper:distil-small.en:cpu-int8"


def test_persist_distil_without_parakeet_weights_goes_to_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dcent_voice.config import persist_stale_desktop_asr

    monkeypatch.setattr("dcent_voice.config._parakeet_weights_present", lambda: False)
    path = tmp_path / "config.toml"
    path.write_text(
        '[profile.quality]\nasr = "faster-whisper:distil-small.en:cpu-int8"\n\n'
        '[profile.desktop]\nasr = "faster-whisper:distil-small.en:int8"\n',
        encoding="utf-8",
    )
    assert persist_stale_desktop_asr(path) is True
    text = path.read_text(encoding="utf-8")
    assert 'desktop]\nasr = "faster-whisper:base.en:cpu-int8"' in text
    assert 'quality]\nasr = "faster-whisper:distil-small.en:cpu-int8"' in text


def test_persist_base_en_to_parakeet_when_weights_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dcent_voice.config import persist_stale_desktop_asr

    weights = tmp_path / "parakeet"
    weights.mkdir()
    (weights / "encoder-model.int8.onnx").write_bytes(b"onnx")
    monkeypatch.setenv("DCENT_VOICE_PARAKEET_DIR", str(weights))
    monkeypatch.setattr("dcent_voice.config._parakeet_weights_present", lambda: True)
    path = tmp_path / "config.toml"
    path.write_text(
        '[profile.quality]\nasr = "faster-whisper:base.en:cpu-int8"\n\n'
        '[profile.desktop]\nasr = "faster-whisper:base.en:cpu-int8"\n',
        encoding="utf-8",
    )
    assert persist_stale_desktop_asr(path) is True
    text = path.read_text(encoding="utf-8")
    assert 'desktop]\nasr = "parakeet:tdt-0.6b-v3:int8"' in text
    assert 'quality]\nasr = "faster-whisper:base.en:cpu-int8"' in text


def test_load_config_migrates_desktop_when_parakeet_weights_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    weights = tmp_path / "parakeet"
    weights.mkdir()
    (weights / "encoder-model.int8.onnx").write_bytes(b"onnx")
    monkeypatch.setenv("DCENT_VOICE_PARAKEET_DIR", str(weights))
    monkeypatch.setattr("dcent_voice.config._parakeet_weights_present", lambda: True)
    path = tmp_path / "config.toml"
    path.write_text(
        'active_profile = "desktop"\n\n'
        '[profile.desktop]\nasr = "faster-whisper:base.en:cpu-int8"\n'
        'llm = "none"\ncleanup_enabled = false\n',
        encoding="utf-8",
    )
    config = load_config(path, create=True)
    assert config.current_profile.asr.raw == "parakeet:tdt-0.6b-v3:int8"
    on_disk = path.read_text(encoding="utf-8")
    assert 'asr = "parakeet:tdt-0.6b-v3:int8"' in on_disk


def test_cloud_profile_marks_session_cloud() -> None:
    config = load_config(Path("config.example.toml"), create=False)
    cloud_profile = config.profiles["cloud"]

    assert cloud_profile.asr.locality is Locality.CLOUD
    assert cloud_profile.locality is Locality.CLOUD


def test_audio_auto_stop_must_not_exceed_max(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[audio]
max_seconds = 30
auto_stop_seconds = 60

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="auto_stop_seconds"):
        load_config(config_path, create=False)


def test_audio_auto_stop_requires_ring_headroom(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[audio]
max_seconds = 60
auto_stop_seconds = 60

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="headroom"):
        load_config(config_path, create=False)


@pytest.mark.parametrize(
    ("max_seconds", "auto_stop_seconds"),
    [("nan", "60"), ("inf", "60"), ("90", "-inf"), ("301", "60"), ("300", "241")],
)
def test_audio_limits_reject_non_finite_or_excessive_values(
    tmp_path: Path, max_seconds: str, auto_stop_seconds: str
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""\
active_profile = "desktop"

[audio]
max_seconds = {max_seconds}
auto_stop_seconds = {auto_stop_seconds}

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="finite|at most"):
        load_config(config_path, create=False)


def test_service_rejects_non_loopback_without_allow_lan(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[service]
host = "0.0.0.0"
port = 8765

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="loopback|allow_lan"):
        load_config(config_path, create=False)


def test_service_allow_lan_permits_non_loopback(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "desktop"

[service]
host = "0.0.0.0"
port = 8765
allow_lan = true

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
cleanup_enabled = false
""",
        encoding="utf-8",
    )

    config = load_config(config_path, create=False)
    assert config.service.host == "0.0.0.0"
    assert config.service.allow_lan is True

    from dcent_voice.attach.registry import create_registry_entry
    from dcent_voice.service.server import format_http_base

    entry = create_registry_entry(
        endpoint=format_http_base(config.service.host, config.service.port),
        version="test",
        registry_dir=tmp_path / "registry",
        token="test-token",
    )
    assert entry.sovereigntyClass == "LAN"


def test_audio_limits_load_from_example() -> None:
    config = load_config(Path("config.example.toml"), create=False)
    assert config.audio.max_seconds == 90
    assert config.audio.auto_stop_seconds == 60


def test_active_profile_must_exist(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
active_profile = "missing"

[profile.desktop]
asr = "faster-whisper:tiny:int8"
llm = "none"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="active_profile"):
        load_config(config_path, create=False)


def test_ensure_user_config_copies_example(tmp_path: Path) -> None:
    destination = tmp_path / "DCENT_Voice" / "config.toml"

    ensured = ensure_user_config(destination, source=Path("config.example.toml"))
    config = load_config(ensured, create=False)

    assert ensured == destination
    assert config.profiles["tiny"].cleanup_enabled is False


def test_idle_unload_s_zero_keeps_warm() -> None:
    config = parse_config(
        {
            "active_profile": "desktop",
            "idle_unload_s": 0,
            "profile": {"desktop": {"asr": "parakeet:tdt-0.6b-v3:int8", "llm": "none"}},
        }
    )
    assert config.idle_unload_s == 0.0


def test_idle_unload_s_rejects_negative() -> None:
    with pytest.raises(ConfigError, match="idle_unload_s"):
        parse_config(
            {
                "active_profile": "desktop",
                "idle_unload_s": -1,
                "profile": {"desktop": {"asr": "parakeet:tdt-0.6b-v3:int8", "llm": "none"}},
            }
        )


# --- WS1: first-run config seeding (fresh-machine root cause) ----------------


def _frozen_payload(tmp_path: Path, monkeypatch) -> Path:
    """Build a minimal PyInstaller one-dir layout and pretend we run from it."""
    import sys

    app = tmp_path / "DCENT_Voice"
    internal = app / "_internal"
    internal.mkdir(parents=True)
    (internal / "config.example.toml").write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    exe = app / "dcent-voice.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return app


def test_find_config_example_prefers_the_frozen_bundle_over_cwd(tmp_path, monkeypatch) -> None:
    """The bug: the frozen exe looked in cwd and parents[2], never in _MEIPASS."""
    from dcent_voice.config import find_config_example

    app = _frozen_payload(tmp_path, monkeypatch)
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    (stray / "config.example.toml").write_text("active_profile = 'stray'\n", encoding="utf-8")
    monkeypatch.chdir(stray)

    assert find_config_example() == app / "_internal" / "config.example.toml"


def test_find_config_example_never_lets_a_stray_cwd_file_win_in_a_source_tree(
    tmp_path, monkeypatch
) -> None:
    from dcent_voice.config import find_config_example

    stray = tmp_path / "elsewhere"
    stray.mkdir()
    (stray / "config.example.toml").write_text("active_profile = 'stray'\n", encoding="utf-8")
    monkeypatch.chdir(stray)

    assert find_config_example() == Path(__file__).resolve().parents[1] / "config.example.toml"


def test_find_config_example_uses_cwd_only_as_a_last_resort(tmp_path, monkeypatch) -> None:
    """With no bundle and no source tree reachable, cwd is still better than nothing."""
    import sys

    from dcent_voice.config import find_config_example
    from dcent_voice.util import paths

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "empty" / "dcent-voice.exe"))
    monkeypatch.setattr(paths, "resource", lambda *parts: tmp_path / "empty" / Path(*parts))
    fallback = tmp_path / "cwd"
    fallback.mkdir()
    (fallback / "config.example.toml").write_text("active_profile = 'tiny'\n", encoding="utf-8")
    monkeypatch.chdir(fallback)

    assert find_config_example() == fallback / "config.example.toml"


def test_fresh_profile_seeds_config_from_the_frozen_bundle(tmp_path, monkeypatch) -> None:
    """AC1 in miniature: an empty profile root plus a neutral cwd must seed."""
    from dcent_voice.config import default_config_path, ensure_user_config, load_config
    from dcent_voice.util import paths

    _frozen_payload(tmp_path, monkeypatch)
    profile = tmp_path / "profile"
    monkeypatch.setenv(paths.PROFILE_ROOT_ENV, str(profile))
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)

    destination = default_config_path()
    assert not destination.exists()

    ensured = ensure_user_config()

    assert ensured == destination
    assert ensured.is_file()
    assert profile in ensured.parents
    assert load_config(ensured, create=False).active_profile == "desktop"


def test_ensure_user_config_leaves_an_existing_corrupt_file_alone(tmp_path) -> None:
    """Classifying a bad config is load_config's job, not the seeding layer's."""
    destination = tmp_path / "config.toml"
    destination.write_text("this is not = valid toml [", encoding="utf-8")

    assert ensure_user_config(destination, source=Path("config.example.toml")) == destination
    assert destination.read_text(encoding="utf-8") == "this is not = valid toml ["


def test_ensure_user_config_reports_source_and_destination_when_the_example_is_missing(
    tmp_path,
) -> None:
    destination = tmp_path / "profile" / "config.toml"
    missing = tmp_path / "nowhere" / "config.example.toml"

    with pytest.raises(ConfigError) as excinfo:
        ensure_user_config(destination, source=missing)

    message = str(excinfo.value)
    assert str(destination) in message
    assert str(missing) in message


def test_ensure_user_config_reports_both_paths_when_the_write_fails(tmp_path, monkeypatch) -> None:
    import os

    destination = tmp_path / "profile" / "config.toml"
    source = Path("config.example.toml").resolve()

    def _boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(ConfigError) as excinfo:
        ensure_user_config(destination, source=source)

    message = str(excinfo.value)
    assert str(destination) in message
    assert str(source) in message


def test_ensure_user_config_leaves_no_temporary_file_behind_on_failure(
    tmp_path, monkeypatch
) -> None:
    import os

    destination = tmp_path / "profile" / "config.toml"
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    with pytest.raises(ConfigError):
        ensure_user_config(destination, source=Path("config.example.toml").resolve())

    assert list(destination.parent.iterdir()) == []


def test_user_config_dir_honors_the_profile_root_override(tmp_path, monkeypatch) -> None:
    from dcent_voice.config import default_config_path, user_config_dir
    from dcent_voice.util import paths

    monkeypatch.setenv(paths.PROFILE_ROOT_ENV, str(tmp_path))

    assert tmp_path in user_config_dir().parents or user_config_dir().parent == tmp_path
    assert tmp_path in default_config_path().parents


def test_bundled_default_config_path_resolves_inside_the_frozen_bundle(
    tmp_path, monkeypatch
) -> None:
    from dcent_voice.config import bundled_default_config_path, load_bundled_default_config

    app = _frozen_payload(tmp_path, monkeypatch)

    assert bundled_default_config_path() == app / "_internal" / "config.example.toml"
    assert load_bundled_default_config().active_profile == "desktop"
