# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dcent_voice.inject import windows_uia
from tests.win32_native import requires_win32_native


def test_has_visible_rect_accepts_callable_width_height() -> None:
    control = SimpleNamespace(
        BoundingRectangle=SimpleNamespace(Width=lambda: 400, Height=lambda: 40)
    )
    assert windows_uia._has_visible_rect(control) is True


def test_address_bar_is_browser_chrome_not_a_page_field() -> None:
    assert windows_uia.is_browser_chrome_field("Address and search bar", "view", "Edit")
    assert windows_uia.is_browser_chrome_field("Search or enter web address", "", "edit")
    assert not windows_uia.is_browser_chrome_field("Meeting notes", "meeting-body", "Edit")
    assert not windows_uia.is_browser_chrome_field("Article draft", "", "Document")


def test_read_focused_editable_rejects_omnibox() -> None:
    control = SimpleNamespace(
        Name="Address and search bar",
        AutomationId="view",
        ControlTypeName="Edit",
        GetValuePattern=lambda: SimpleNamespace(Value="https://example.test"),
    )
    with pytest.raises(RuntimeError, match="browser chrome"):
        windows_uia.read_focused_editable(control)


def test_read_focused_editable_uses_value_pattern() -> None:
    control = SimpleNamespace(
        Name="Meeting notes",
        AutomationId="meeting-body",
        ControlTypeName="Edit",
        GetValuePattern=lambda: SimpleNamespace(Value="Hello world."),
    )
    snapshot = windows_uia.read_focused_editable(control)
    assert snapshot.text == "Hello world."
    assert snapshot.kind == "edit"
    assert snapshot.name == "Meeting notes"


def test_read_focused_editable_uses_text_pattern_for_contenteditable() -> None:
    document = SimpleNamespace(GetText=lambda _limit: "Existing article draft")
    control = SimpleNamespace(
        Name="Article draft",
        AutomationId="",
        ControlTypeName="Document",
        GetValuePattern=lambda: None,
        GetTextPattern=lambda: SimpleNamespace(DocumentRange=document),
    )
    snapshot = windows_uia.read_focused_editable(control)
    assert snapshot.text == "Existing article draft"
    assert snapshot.kind == "contenteditable"


def test_set_focused_editable_text_uses_value_pattern() -> None:
    written: list[str] = []
    control = SimpleNamespace(
        Name="Meeting notes",
        AutomationId="meeting-body",
        ControlTypeName="Edit",
        GetValuePattern=lambda: SimpleNamespace(SetValue=written.append),
    )
    windows_uia.set_focused_editable_text("BASE-form", control=control)
    assert written == ["BASE-form"]


def test_walk_named_editable_finds_nested_page_field() -> None:
    field = SimpleNamespace(
        Name="Article draft",
        AutomationId="",
        ControlTypeName="EditControl",
        GetChildren=lambda: [],
    )
    chrome = SimpleNamespace(
        Name="Address and search bar",
        AutomationId="view",
        ControlTypeName="EditControl",
        GetChildren=lambda: [],
    )
    root = SimpleNamespace(
        Name="Edge",
        AutomationId="",
        ControlTypeName="WindowControl",
        GetChildren=lambda: [chrome, field],
    )
    found = windows_uia._walk_named_editable(root, "article draft")
    assert found is field
    assert windows_uia._walk_named_editable(root, "address and search bar") is None


def test_page_field_name_matches_aliases_but_never_omnibox() -> None:
    assert windows_uia.page_field_name_matches(
        "Search Wikipedia",
        ("Search Wikipedia", "Search"),
    )
    assert windows_uia.page_field_name_matches("Search", ("Search", "Search DuckDuckGo"))
    assert not windows_uia.page_field_name_matches(
        "Address and search bar",
        ("Search",),
    )
    assert not windows_uia.page_field_name_matches(
        "Search or enter web address",
        ("Search",),
    )
    assert not windows_uia.page_field_name_matches(
        "Search - Wikipedia",
        ("Search Wikipedia", "Search"),
    )
    assert not windows_uia.page_field_name_matches(
        "Search · GitHub",
        ("Search GitHub", "Search"),
    )
    assert windows_uia.looks_like_navigated_url("https://github.com/search")
    assert not windows_uia.looks_like_navigated_url("Hello world.")
    assert windows_uia.looks_like_search_chrome_label("I'm Feeling Lucky")
    assert windows_uia.looks_like_search_chrome_label("Advanced search:")
    assert windows_uia.looks_like_search_chrome_label("Settings and more (Alt+F)")
    assert not windows_uia.looks_like_search_chrome_label("Hello world.")
    assert not windows_uia.looks_like_search_chrome_label("BASE-edge-google-1")


def test_google_search_combobox_is_editable_page_field() -> None:
    control = SimpleNamespace(
        Name="Search",
        AutomationId="",
        ControlTypeName="ComboBoxControl",
        GetValuePattern=lambda: SimpleNamespace(Value=""),
    )
    snapshot = windows_uia.read_focused_editable(control)
    assert snapshot.kind == "edit"
    assert snapshot.name == "Search"
    assert windows_uia._field_kind("ComboBoxControl", "Search", "") == "edit"


def test_named_page_field_matches_google_q_automation_id() -> None:
    control = SimpleNamespace(
        Name="",
        AutomationId="q",
        ControlTypeName="ComboBox",
        BoundingRectangle=SimpleNamespace(width=500, height=44),
    )
    assert windows_uia._is_named_page_field(control, ("Search", "Search Google", "q"))
    chrome = SimpleNamespace(
        Name="Address and search bar",
        AutomationId="q",
        ControlTypeName="Edit",
    )
    assert not windows_uia._is_named_page_field(chrome, ("q",))


def test_first_page_editable_skips_omnibox_and_root_web_area() -> None:
    page_edit = SimpleNamespace(
        Name="Search",
        AutomationId="",
        ControlTypeName="EditControl",
        BoundingRectangle=SimpleNamespace(width=400, height=40),
        GetChildren=lambda: [],
    )
    chrome = SimpleNamespace(
        Name="Address and search bar",
        AutomationId="view",
        ControlTypeName="EditControl",
        BoundingRectangle=SimpleNamespace(width=800, height=32),
        GetChildren=lambda: [],
    )
    web = SimpleNamespace(
        Name="DuckDuckGo",
        AutomationId="RootWebArea",
        ControlTypeName="DocumentControl",
        BoundingRectangle=SimpleNamespace(width=1280, height=800),
        GetChildren=lambda: [page_edit],
    )
    root = SimpleNamespace(
        Name="Edge",
        AutomationId="",
        ControlTypeName="WindowControl",
        GetChildren=lambda: [chrome, web],
    )
    found_web = windows_uia._walk_root_web_area(root)
    assert found_web is web
    assert windows_uia._first_page_editable(web) is page_edit
    assert windows_uia._first_page_editable(root) is page_edit
    page_title_doc = SimpleNamespace(
        Name="Search - Wikipedia",
        AutomationId="RootWebArea",
        ControlTypeName="DocumentControl",
        BoundingRectangle=SimpleNamespace(width=1280, height=800),
        GetChildren=lambda: [page_edit],
    )
    assert windows_uia._first_page_editable(page_title_doc) is page_edit


def test_walk_search_host_finds_wikipedia_searchform() -> None:
    form = SimpleNamespace(
        Name="",
        AutomationId="searchform",
        ControlTypeName="GroupControl",
        BoundingRectangle=SimpleNamespace(width=280, height=36),
        GetChildren=lambda: [],
    )
    chrome = SimpleNamespace(
        Name="Address and search bar",
        AutomationId="view",
        ControlTypeName="EditControl",
        BoundingRectangle=SimpleNamespace(width=800, height=32),
        GetChildren=lambda: [],
    )
    root = SimpleNamespace(
        Name="Edge",
        AutomationId="",
        ControlTypeName="WindowControl",
        GetChildren=lambda: [chrome, form],
    )
    assert windows_uia._walk_search_host(root) is form
    assert windows_uia._walk_search_host(chrome) is None


@requires_win32_native
def test_refocus_page_field_skips_blind_click_when_window_stayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clicks: list[int] = []
    monkeypatch.setattr(
        windows_uia,
        "read_focused_editable",
        lambda: (_ for _ in ()).throw(RuntimeError("no field")),
    )
    monkeypatch.setattr(
        windows_uia,
        "focus_page_editable",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no field")),
    )
    monkeypatch.setattr(windows_uia, "_walk_search_host", lambda _root: None)
    monkeypatch.setattr(
        windows_uia,
        "_click_search_offset",
        lambda hwnd: clicks.append(hwnd) or True,
    )
    assert windows_uia.refocus_page_field(hwnd=7, steal_recovered=False) is True
    assert clicks == []


@requires_win32_native
def test_refocus_page_field_clicks_only_after_focus_steal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clicks: list[int] = []
    monkeypatch.setattr(
        windows_uia,
        "read_focused_editable",
        lambda: (_ for _ in ()).throw(RuntimeError("no field")),
    )
    monkeypatch.setattr(
        windows_uia,
        "focus_page_editable",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no field")),
    )
    monkeypatch.setattr(windows_uia, "_walk_search_host", lambda _root: None)
    monkeypatch.setattr(
        windows_uia,
        "_click_search_offset",
        lambda hwnd: clicks.append(hwnd) or True,
    )
    monkeypatch.setattr(windows_uia, "_send_ctrl_a", lambda: None)
    assert windows_uia.refocus_page_field(hwnd=7, steal_recovered=True) is True
    assert clicks == [7]


@requires_win32_native
def test_refocus_page_field_walks_owned_window_not_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = object()
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        windows_uia,
        "read_focused_editable",
        lambda: (_ for _ in ()).throw(RuntimeError("no field")),
    )
    monkeypatch.setattr(windows_uia, "_control_from_hwnd", lambda hwnd: fake_root)

    def _focus(**kwargs: object) -> object:
        seen["search_from"] = kwargs.get("search_from")
        return object()

    monkeypatch.setattr(windows_uia, "focus_page_editable", _focus)
    assert windows_uia.refocus_page_field(hwnd=42) is True
    assert seen["search_from"] is fake_root


def test_refocus_page_field_keeps_an_already_focused_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    field = SimpleNamespace(
        Name="Search",
        AutomationId="",
        ControlTypeName="Edit",
        GetValuePattern=lambda: SimpleNamespace(Value="query"),
    )
    monkeypatch.setattr(windows_uia, "_focused_control", lambda: field)
    assert windows_uia.refocus_page_field() is True


def test_read_focused_editable_rejects_page_document_root() -> None:
    control = SimpleNamespace(
        Name="Google",
        AutomationId="RootWebArea",
        ControlTypeName="DocumentControl",
        GetValuePattern=lambda: SimpleNamespace(Value=""),
    )
    with pytest.raises(RuntimeError, match="page document"):
        windows_uia.read_focused_editable(control)


def test_read_focused_editable_rejects_url_document() -> None:
    control = SimpleNamespace(
        Name="Search · GitHub",
        AutomationId="",
        ControlTypeName="DocumentControl",
        GetValuePattern=lambda: SimpleNamespace(Value="https://github.com/search"),
    )
    with pytest.raises(RuntimeError, match="page document"):
        windows_uia.read_focused_editable(control)


def test_set_focused_editable_text_refuses_address_bar() -> None:
    control = SimpleNamespace(
        Name="Address and search bar",
        AutomationId="view",
        ControlTypeName="Edit",
        GetValuePattern=lambda: SimpleNamespace(SetValue=lambda _text: None),
    )
    with pytest.raises(RuntimeError, match="browser chrome"):
        windows_uia.set_focused_editable_text("lost", control=control)
