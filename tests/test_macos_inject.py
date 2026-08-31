# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
from types import ModuleType

import pytest

from dcent_voice.inject import macos
from dcent_voice.inject.clipboard import ClipboardPreservationError


class _FakeNSData:
    @classmethod
    def dataWithBytes_length_(cls, value: bytes, length: int) -> bytes:
        return bytes(value[:length])


class _FakePasteboardItem:
    reject_format: str | None = None

    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self.values = dict(values or {})

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def types(self) -> list[str]:
        return list(self.values)

    def dataForType_(self, type_name: str) -> bytes | None:
        return self.values.get(type_name)

    def setData_forType_(self, value: bytes, type_name: str) -> bool:
        if type_name == self.reject_format:
            return False
        self.values[type_name] = bytes(value)
        return True


class _FakePasteboard:
    def __init__(self, items: list[_FakePasteboardItem]) -> None:
        self.items = list(items)
        self.change_count = 10
        self.reject_text = False
        self.reject_write = False
        self.clear_calls = 0

    def changeCount(self) -> int:
        return self.change_count

    def pasteboardItems(self) -> list[_FakePasteboardItem]:
        return list(self.items)

    def types(self) -> list[str]:
        return list(dict.fromkeys(key for item in self.items for key in item.values))

    def clearContents(self) -> int:
        self.clear_calls += 1
        self.change_count += 1
        self.items = []
        return self.change_count

    def setString_forType_(self, value: str, type_name: str) -> bool:
        if self.reject_text:
            return False
        self.items = [_FakePasteboardItem({type_name: value.encode("utf-8")})]
        return True

    def writeObjects_(self, items: list[_FakePasteboardItem]) -> bool:
        if self.reject_write:
            return False
        self.items = list(items)
        return True


def _install_fake_api(monkeypatch: pytest.MonkeyPatch, board: _FakePasteboard) -> None:
    class NSPasteboard:
        @classmethod
        def generalPasteboard(cls) -> _FakePasteboard:
            return board

    monkeypatch.setattr(
        macos,
        "_pasteboard_api",
        lambda: (NSPasteboard, _FakePasteboardItem, "public.utf8-plain-text", _FakeNSData),
    )
    monkeypatch.setattr(
        macos,
        "_pasteboard",
        lambda: (NSPasteboard, "public.utf8-plain-text"),
    )


def _install_import_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "AppKit", ModuleType("AppKit"))
    monkeypatch.setitem(sys.modules, "Quartz", ModuleType("Quartz"))


def _original_items() -> list[_FakePasteboardItem]:
    return [
        _FakePasteboardItem(
            {
                "public.utf8-plain-text": b"formatted text",
                "public.rtf": b"{\\rtf1 formatted text}",
                "public.png": b"\x89PNG\r\n\x1a\nimage",
            }
        ),
        _FakePasteboardItem(
            {
                "public.file-url": b"file:///Users/example/report.pdf",
                "public.url": b"https://example.invalid/report",
            }
        ),
    ]


def test_all_item_representations_round_trip_through_injection(monkeypatch) -> None:
    board = _FakePasteboard(_original_items())
    _install_fake_api(monkeypatch, board)
    _install_import_fakes(monkeypatch)
    original = macos.snapshot_clipboard()
    pasted: list[str] = []
    monkeypatch.setattr(macos, "send_cmd_v", lambda: pasted.append("paste"))
    monkeypatch.setattr(macos.time, "sleep", lambda _seconds: None)

    macos.MacOSClipboardPasteInjector().inject("dictated")

    assert pasted == ["paste"]
    assert macos.snapshot_clipboard() == original
    assert {value.type_name for item in original for value in item} >= {
        "public.rtf",
        "public.png",
        "public.file-url",
    }


def test_unreadable_representation_fails_closed_before_clear(monkeypatch) -> None:
    unreadable = _FakePasteboardItem({"public.png": b"image"})
    unreadable.dataForType_ = lambda _type_name: None  # type: ignore[method-assign]
    board = _FakePasteboard([unreadable])
    _install_fake_api(monkeypatch, board)
    _install_import_fakes(monkeypatch)
    monkeypatch.setattr(
        macos,
        "send_cmd_v",
        lambda: pytest.fail("paste must not run after an unsafe snapshot"),
    )

    with pytest.raises(ClipboardPreservationError, match="would not provide"):
        macos.MacOSClipboardPasteInjector().inject("dictated")

    assert board.clear_calls == 0


def test_failed_text_publish_is_checked_and_original_formats_are_restored(monkeypatch) -> None:
    board = _FakePasteboard(_original_items())
    _install_fake_api(monkeypatch, board)
    _install_import_fakes(monkeypatch)
    original = macos.snapshot_clipboard()
    board.reject_text = True
    monkeypatch.setattr(
        macos,
        "send_cmd_v",
        lambda: pytest.fail("paste must not run after a failed publish"),
    )

    with pytest.raises(RuntimeError, match="refused"):
        macos.MacOSClipboardPasteInjector().inject("dictated")

    assert macos.snapshot_clipboard() == original


def test_restore_write_failure_is_not_silent(monkeypatch) -> None:
    board = _FakePasteboard(_original_items())
    _install_fake_api(monkeypatch, board)
    snapshot = macos.snapshot_clipboard()
    board.reject_write = True

    with pytest.raises(ClipboardPreservationError, match="rejected"):
        macos.restore_clipboard(snapshot)


def test_restore_preflight_failure_does_not_clear_current_board(monkeypatch) -> None:
    board = _FakePasteboard(_original_items())
    _install_fake_api(monkeypatch, board)
    snapshot = macos.snapshot_clipboard()
    _FakePasteboardItem.reject_format = "public.rtf"
    try:
        with pytest.raises(ClipboardPreservationError, match="prepare"):
            macos.restore_clipboard(snapshot)
    finally:
        _FakePasteboardItem.reject_format = None

    assert board.clear_calls == 0


def test_newer_clipboard_write_is_never_overwritten_by_restore(monkeypatch) -> None:
    board = _FakePasteboard(_original_items())
    _install_fake_api(monkeypatch, board)
    _install_import_fakes(monkeypatch)
    monkeypatch.setattr(macos.time, "sleep", lambda _seconds: None)

    def user_copies_new_image() -> None:
        board.items = [_FakePasteboardItem({"public.png": b"newer-user-image"})]
        board.change_count += 1

    monkeypatch.setattr(macos, "send_cmd_v", user_copies_new_image)

    with pytest.raises(ClipboardPreservationError, match="newer contents"):
        macos.MacOSClipboardPasteInjector().inject("dictated")

    assert board.items[0].values == {"public.png": b"newer-user-image"}
