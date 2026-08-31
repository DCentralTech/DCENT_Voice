# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path

from dcent_voice.app import build_parser
from dcent_voice.inject.windows_apps_test import HOLD_RELEASE_APP_NAMES
from dcent_voice.integration.windows_hold_release import _parse_hold_apps, _percentiles


def test_hold_release_command_contract_is_explicit() -> None:
    args = build_parser().parse_args(
        [
            "--config",
            "isolated.toml",
            "hold-release-self-test",
            "--audio",
            "speech.wav",
            "--reference",
            "hello world",
            "--output-device",
            "20",
            "--runs",
            "10",
            "--apps",
            "notepad,vscode,console,edge,chrome",
            "--output-json",
            "report.json",
        ]
    )
    assert args.config == Path("isolated.toml")
    assert args.command == "hold-release-self-test"
    assert args.audio == Path("speech.wav")
    assert args.reference == "hello world"
    assert args.output_device == "20"
    assert args.runs == 10
    assert args.apps == "notepad,vscode,console,edge,chrome"


def test_hold_release_default_apps_are_the_real_matrix() -> None:
    args = build_parser().parse_args(
        [
            "hold-release-self-test",
            "--audio",
            "speech.wav",
            "--reference",
            "Hello world.",
            "--output-device",
            "20",
        ]
    )
    assert args.apps == "all"
    assert args.allow_default_microphone is False
    assert args.real_documents is False
    assert _parse_hold_apps(args.apps) == list(HOLD_RELEASE_APP_NAMES)


def test_hold_release_default_microphone_flag_is_opt_in() -> None:
    args = build_parser().parse_args(
        [
            "hold-release-self-test",
            "--audio",
            "speech.wav",
            "--reference",
            "Hello world.",
            "--output-device",
            "4",
            "--allow-default-microphone",
        ]
    )
    assert args.allow_default_microphone is True


def test_hold_release_real_documents_flag_is_opt_in() -> None:
    args = build_parser().parse_args(
        [
            "hold-release-self-test",
            "--audio",
            "speech.wav",
            "--reference",
            "Hello world.",
            "--output-device",
            "20",
            "--allow-default-microphone",
            "--real-documents",
            "--apps",
            "notepad,vscode,edge",
        ]
    )
    assert args.real_documents is True
    assert args.apps == "notepad,vscode,edge"


def test_parse_hold_apps_rejects_unknown() -> None:
    try:
        _parse_hold_apps("notepad,outlook")
    except ValueError as exc:
        assert "outlook" in str(exc)
    else:
        raise AssertionError("unknown apps must fail closed")


def test_parse_hold_apps_accepts_uia_field_targets() -> None:
    assert _parse_hold_apps("edge-ce,edge-form") == ["edge-ce", "edge-form"]
    assert _parse_hold_apps("all") == list(HOLD_RELEASE_APP_NAMES)
    assert "edge-ce" not in HOLD_RELEASE_APP_NAMES


def test_parse_hold_apps_accepts_live_browser_tabs() -> None:
    assert _parse_hold_apps("edge-ddg,edge-wiki") == ["edge-ddg", "edge-wiki"]
    assert _parse_hold_apps("edge-google,edge-github,edge-gmail") == [
        "edge-google",
        "edge-github",
        "edge-gmail",
    ]
    assert _parse_hold_apps("all") == list(HOLD_RELEASE_APP_NAMES)
    assert "edge-ddg" not in HOLD_RELEASE_APP_NAMES


def test_existing_document_paths_point_at_on_disk_files() -> None:
    from dcent_voice.inject.windows_apps_test import existing_document_paths

    paths = existing_document_paths()
    assert paths["vscode"].name == "w29-workspace-note.txt"
    assert paths["vscode"].is_file()
    assert paths["edge"].name == "w29-browser-field.html"
    assert paths["edge"].is_file()
    assert paths["chrome"].name == "w29-browser-field.html"
    assert paths["chrome"].is_file()
    assert paths["edge-ce"].name == "w31-contenteditable.html"
    assert paths["edge-form"].name == "w31-nested-form.html"
    assert paths["edge-ce"].is_file()
    assert paths["edge-form"].is_file()
    ce = paths["edge-ce"].read_text(encoding="utf-8")
    form = paths["edge-form"].read_text(encoding="utf-8")
    assert 'id="t"' not in ce and "id=t" not in ce
    assert 'contenteditable="true"' in ce
    assert 'aria-label="Article draft"' in ce
    assert 'id="t"' not in form and "id=t" not in form
    assert "textarea" in form and 'name="body"' in form
    assert 'aria-label="Meeting notes"' in form


def test_rmtree_best_effort_ignores_missing_and_locked_trees(tmp_path: Path) -> None:
    from dcent_voice.inject.windows_apps_test import _rmtree_best_effort

    missing = tmp_path / "absent"
    _rmtree_best_effort(missing)
    present = tmp_path / "tree"
    present.mkdir()
    (present / "file.txt").write_text("ok", encoding="utf-8")
    _rmtree_best_effort(present)
    assert not present.exists()


def test_hold_release_percentiles_do_not_hide_tail() -> None:
    result = _percentiles([1.0, 2.0, 3.0, 4.0, 100.0])
    assert result is not None
    assert result["p50"] == 3.0
    assert result["p95"] > 80.0
    assert result["p99"] > result["p95"]
    assert result["maximum"] == 100.0
