# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import re
from pathlib import Path

SETTINGS_ROOT = Path("src/dcent_voice/ui/web/settings")


def _asset(name: str) -> str:
    return (SETTINGS_ROOT / name).read_text(encoding="utf-8")


def test_general_page_leads_with_hold_to_talk_and_hides_engines() -> None:
    html = _asset("index.html")
    css = _asset("settings.css")

    assert 'id="dictateHero"' in html
    assert "How to dictate" in html
    assert "speak · release" in html
    assert 'class="advanced"' in html
    assert "Profiles, live dictation, Command, and local API" in html
    assert 'class="aurora"' not in html
    assert ".aurora" not in css
    assert "nav-group" in html
    before, after = html.split('class="advanced"', 1)
    assert 'id="localCleanupToggle"' not in before
    assert 'id="localCleanupToggle"' in after


def test_general_page_has_no_conflicting_global_cleanup_control() -> None:
    html = _asset("index.html")
    script = _asset("settings.js")

    assert 'id="cleanupEnabled"' not in html
    assert 'getElementById("cleanupEnabled")' not in script
    assert 'data-field="cleanup_enabled"' in script
    assert 'id="localCleanupToggle"' in html
    assert "set_local_cleanup" in script
    assert "local_cleanup_status" in script
    assert "Never silent cloud" in html


def test_modes_page_explains_behavior_and_risk() -> None:
    html = _asset("index.html")

    for copy in (
        "scratch that",
        "new line",
        "new sentence",
        "bullet",
        "press enter",
        "Text selected:",
        "Nothing selected:",
        "Offline transforms work without an LLM",
        "DCENT_ADE must be running",
    ):
        assert copy in html
    assert "retract" in html.lower()


def test_dictionary_and_snippet_ui_exposes_concise_local_workflows() -> None:
    html = _asset("index.html")
    script = _asset("settings.js")

    dictionary_help = re.search(
        r"<h3>Personal dictionary</h3>\s*<p class=\"subtitle\">(.*?)</p>",
        html,
        re.DOTALL,
    )
    snippet_help = re.search(
        r"<h3>Snippets</h3>\s*<p class=\"subtitle\">(.*?)</p>",
        html,
        re.DOTALL,
    )
    assert dictionary_help is not None
    assert snippet_help is not None

    for help_match in (dictionary_help, snippet_help):
        help_text = re.sub(r"<[^>]+>", " ", help_match.group(1))
        assert len(re.findall(r"\b[\w’-]+\b", help_text)) < 100
        assert "on this machine" in help_text
        assert "reviewed before they change anything" in help_text
        assert "can be undone" in help_text
    for _forbidden_brand in ("Wispr", "BridgeVoice", "BridgeMind", "SuperWhisper"):
        assert _forbidden_brand not in html
    assert "Sticky unpolished" not in html
    assert "writing style keeps" not in html.lower()

    for element_id in (
        "dictionaryFilter",
        "dictionaryStarredOnly",
        "dictionarySort",
        "dictionaryCount",
        "dictionaryRows",
        "addDictionaryRow",
        "importDictionary",
        "exportDictionary",
        "removeVisibleDictionary",
        "undoDictionaryImport",
        "snippetFilter",
        "snippetStarredOnly",
        "snippetSort",
        "snippetCount",
        "snippetRows",
        "addSnippetRow",
        "importSnippets",
        "exportSnippets",
        "removeVisibleSnippets",
        "undoSnippetImport",
        "snippetImportReview",
        "applySnippetImport",
        "cancelSnippetImport",
        "doneSnippetImport",
    ):
        assert f'id="{element_id}"' in html

    for behavior in (
        "filterDictionaryRows",
        "sortDictionaryRows",
        "filterSnippetRows",
        "sortSnippetRows",
        "removeVisibleRows",
        "persistListPrefs",
        "showSnippetImportReview",
        "showSnippetImportUndo",
        "showDictionaryImportUndo",
        "preview_dictionary",
        "preview_snippets",
        "import_dictionary",
        "import_snippets",
        "undo_dictionary_import",
        "undo_snippet_import",
        "export_dictionary",
        "export_snippets",
    ):
        assert behavior in script

    assert "confirm(removeVisibleConfirm" in script
    assert 'data-dict="starred"' in script
    assert 'data-snippet="starred"' in script
    assert 'maxlength="60"' in script
    assert 'maxlength="4000"' in script
    assert 'row.action === "skip"' in script
    assert "row.expansion" in script
    assert "white-space: pre-wrap" in _asset("settings.css")


def test_settings_models_surface_cpu_hardware_banner() -> None:
    script = _asset("settings.js")
    css = _asset("settings.css")

    assert "renderHardwareStatus" in script
    assert "cpu-int8" in script
    assert "no high-end GPU" in script or "without a high-end GPU" in script
    assert ".hardware-banner" in css


def test_learned_dictionary_discloses_destination_scope() -> None:
    script = _asset("settings.js")

    assert "entry.style, entry.app" in script
    assert '|| "all apps"' in script


def test_settings_tabs_have_accessible_relationships_and_keyboard_navigation() -> None:
    html = _asset("index.html")
    script = _asset("settings.js")
    tabs = re.findall(r'<button class="tab[^>]*id="([^"]+)"[^>]*aria-controls="([^"]+)"', html)

    assert 'role="tablist"' in html
    assert tabs
    for tab_id, panel_id in tabs:
        assert f'id="{panel_id}" role="tabpanel" aria-labelledby="{tab_id}"' in html
    for key in ("ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"):
        assert key in script


def test_model_cards_expose_accessible_end_to_end_tradeoff_meters() -> None:
    script = _asset("settings.js")

    for axis in ("Responsiveness", "Accuracy", "Efficiency", "Sovereignty"):
        assert f'title: "{axis}"' in script
    assert 'role="meter"' in script
    assert 'aria-valuenow="${axis.score}"' in script
    assert "LOCAL_LLM_PROVIDERS" in script
    assert "Hybrid — text may leave" in script
    assert "Active profile" in script
    assert "Cleanup readiness:" in script


def test_saving_general_settings_refreshes_active_profile_model_badges() -> None:
    script = _asset("settings.js")

    assert "renderGeneral();\n  renderModels();" in script


def test_provider_disconnect_requires_verified_backend_success() -> None:
    script = _asset("settings.js")

    assert "result.ok !== true" in script
    assert "Could not disconnect ${provider}" in script


def test_model_cards_explain_configured_cpu_gpu_and_automatic_selection() -> None:
    script = _asset("settings.js")

    assert 'asr.option.startsWith("cpu-")' in script
    assert 'asr.option.startsWith("cuda-")' in script
    assert '["int8", "int16", "float32"].includes(asr.option)' in script
    assert 'label: "NVIDIA GPU"' in script
    assert 'label: "Automatic"' in script
    assert "omit the suffix for automatic selection" in script
    assert "lazy: true" in script
    assert 'desktop: { asr: "parakeet:tdt-0.6b-v3:int8"' in script
    html = _asset("index.html")
    assert 'id="languageMode"' in html
    assert "English (fast)" in html
    assert "More languages" in html
    assert "Which language" in html
    assert "Language code" not in html
    assert 'id="languageHint"' in html
    assert "never a giant model" in html
    assert "French" in html
    script = _asset("settings.js")
    assert "fillLanguageSelect" in script
    assert "Detect automatically" in script
    assert "Learned vocabulary" in html
    assert 'id="personalizationProseContext"' in html
    assert "prose_context" in script
    assert "pers.prose_context === true" in script
    assert "pers.enabled === true" in script
    assert "pers.learn === true" in script
    assert "resetPersonalization" in script
    assert 'id="styleDefault"' in html
    assert 'id="learnLast"' in html
    assert 'value="notes"' in html
    assert "Writing style" in html
    assert "Auto Cleanup" in html
    assert 'id="dictationCleanupLevel"' in html
    assert "High — also drop local hedges" in html
    assert "dictationCleanupLevel" in script
    assert "cleanup_level:" in script or "cleanup_level :" in script
    assert "Destination styles by app" in html
    assert 'id="stylePerAppRows"' in html
    assert 'id="addStylePerAppRow"' in html
    assert "collectStylePerAppRows" in script
    assert "built_in" in script
    general_idx = html.find("Auto Cleanup")
    dictionary_polish = html.find("Offline dictation polish")
    assert general_idx != -1 and dictionary_polish != -1
    assert general_idx < dictionary_polish
    assert html.count('id="dictationCleanupLevel"') == 1
    assert "Command hotkey (optional)" in html
    assert 'quality: { asr: "faster-whisper:distil-small.en:cpu-int8"' in script
    assert 'accurate: { asr: "faster-whisper:large-v3:cpu-int8"' in script
    assert 'gpu: { asr: "faster-whisper:distil-small.en:cuda-float16"' in script
    assert 'tiny: { asr: "faster-whisper:tiny.en:cpu-int8", llm: "none"' in script
    assert '"parakeet"' in script


def test_models_page_offers_explicitly_consented_tts_installation() -> None:
    html = _asset("index.html")
    script = _asset("settings.js")

    assert 'id="ttsModels"' in html
    assert '"install_tts_models"' in script
    assert "window.confirm(" in script
    assert "voice.model.download" in script
    assert "restart DCENT_Voice" in script
    assert '"piper"' not in script


def test_models_banner_shows_resolved_asr_and_verified_readiness() -> None:
    script = _asset("settings.js")
    assert "hw.resolved_asr || hw.active_asr" in script
    assert "readiness.ready" in script
    assert "Resolved speech model" in script
    assert "missing or corrupt — reinstall complete package" in script
