# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import contextlib
import ctypes
import sys

import pytest

from dcent_voice.inject import clipboard
from tests.win32_native import requires_win32_native

pytestmark = requires_win32_native


def _windows_injector(monkeypatch: pytest.MonkeyPatch, *, restore_previous: bool = True):
    monkeypatch.setattr(clipboard.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clipboard, "send_ctrl_v", lambda: None)
    return clipboard.ClipboardPasteInjector(
        restore_previous=restore_previous,
        paste_delay_s=0,
    )


def test_central_clipboard_opener_retries_and_forwards_owner(monkeypatch) -> None:
    calls: list[int | None] = []
    closes = 0

    class User32:
        @staticmethod
        def OpenClipboard(owner):
            calls.append(owner)
            return len(calls) == 3

        @staticmethod
        def CloseClipboard():
            nonlocal closes
            closes += 1
            return True

    monkeypatch.setattr(clipboard, "_user32", User32())
    with clipboard._opened_clipboard(0.1, owner_hwnd=1234, stage="unit_retry_owner"):
        pass
    assert calls == [1234, 1234, 1234]
    assert closes == 1


def test_central_clipboard_opener_permanent_denial_is_bounded_and_diagnostic(
    monkeypatch,
) -> None:
    calls = 0

    class User32:
        @staticmethod
        def OpenClipboard(_owner):
            nonlocal calls
            calls += 1
            return False

        @staticmethod
        def CloseClipboard():
            pytest.fail("failed acquisition must not close the clipboard")

    monkeypatch.setattr(clipboard, "_user32", User32())
    with (
        pytest.raises(clipboard.ClipboardOpenTimeout) as raised,
        clipboard._opened_clipboard(0, stage="unit_permanent_denial"),
    ):
        pass
    assert calls == 1
    assert raised.value.stage == "unit_permanent_denial"
    assert raised.value.attempts == 1
    assert "last_error=" in str(raised.value)


def test_access_denied_retries_with_backoff_then_opens(monkeypatch) -> None:
    calls = 0

    class User32:
        @staticmethod
        def OpenClipboard(_owner):
            nonlocal calls
            calls += 1
            if calls < 4:
                ctypes.set_last_error(clipboard.ERROR_ACCESS_DENIED)
                return False
            return True

        @staticmethod
        def CloseClipboard():
            return True

    monkeypatch.setattr(clipboard, "_user32", User32())
    with clipboard._opened_clipboard(0.5, stage="unit_access_denied_retry"):
        pass
    assert calls == 4


def test_access_denied_timeout_reports_error_5_after_retries(monkeypatch) -> None:
    calls = 0

    class User32:
        @staticmethod
        def OpenClipboard(_owner):
            nonlocal calls
            calls += 1
            ctypes.set_last_error(clipboard.ERROR_ACCESS_DENIED)
            return False

        @staticmethod
        def CloseClipboard():
            pytest.fail("failed acquisition must not close the clipboard")

    monkeypatch.setattr(clipboard, "_user32", User32())
    with (
        pytest.raises(clipboard.ClipboardOpenTimeout) as raised,
        clipboard._opened_clipboard(0.04, stage="unit_access_denied_budget"),
    ):
        pass
    assert calls > 1
    assert raised.value.last_error == clipboard.ERROR_ACCESS_DENIED
    assert raised.value.stage == "unit_access_denied_budget"
    assert "last_error=5" in str(raised.value)


def test_inject_restore_uses_longer_timeout_than_open(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = clipboard.ClipboardPasteInjector(
        restore_previous=True,
        open_timeout_s=0.2,
        restore_timeout_s=1.5,
        paste_delay_s=0,
    )
    restore_timeouts: list[float] = []
    monkeypatch.setattr(clipboard.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        clipboard, "snapshot_clipboard", lambda **_kwargs: [(clipboard.CF_UNICODETEXT, b"old")]
    )
    monkeypatch.setattr(clipboard, "set_clipboard_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(clipboard, "send_ctrl_v", lambda: None)
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: 7)
    monkeypatch.setattr(
        clipboard,
        "restore_clipboard",
        lambda _snapshot, timeout_s=0.5, **_kwargs: restore_timeouts.append(timeout_s),
    )
    injector.inject("dictated")
    assert restore_timeouts == [1.5]


def test_inject_retries_restore_on_access_denied_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector = _windows_injector(monkeypatch)
    injector.restore_timeout_s = 0.25
    attempts: list[int] = []

    def flaky_restore(_snapshot, timeout_s=0.5, **_kwargs):
        attempts.append(int(timeout_s * 1000))
        if len(attempts) == 1:
            raise clipboard.ClipboardOpenTimeout(
                stage="restore",
                timeout_s=timeout_s,
                attempts=8,
                elapsed_s=timeout_s,
                last_error=clipboard.ERROR_ACCESS_DENIED,
            )

    monkeypatch.setattr(
        clipboard, "snapshot_clipboard", lambda **_kwargs: [(clipboard.CF_UNICODETEXT, b"old")]
    )
    monkeypatch.setattr(clipboard, "set_clipboard_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: 8)
    monkeypatch.setattr(clipboard, "restore_clipboard", flaky_restore)
    injector.inject("dictated")
    assert attempts == [250, 250]


def test_inject_restore_failure_is_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = _windows_injector(monkeypatch)
    events: list[str] = []

    def always_denied(_snapshot, timeout_s=0.5, **_kwargs):
        events.append("restore")
        raise clipboard.ClipboardOpenTimeout(
            stage="restore",
            timeout_s=timeout_s,
            attempts=12,
            elapsed_s=timeout_s,
            last_error=clipboard.ERROR_ACCESS_DENIED,
        )

    monkeypatch.setattr(
        clipboard, "snapshot_clipboard", lambda **_kwargs: [(clipboard.CF_UNICODETEXT, b"old")]
    )
    monkeypatch.setattr(
        clipboard, "set_clipboard_text", lambda text, **_kwargs: events.append(f"set:{text}")
    )
    monkeypatch.setattr(clipboard, "send_ctrl_v", lambda: events.append("paste"))
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: 9)
    monkeypatch.setattr(clipboard, "restore_clipboard", always_denied)

    with pytest.raises(clipboard.ClipboardOpenTimeout) as raised:
        injector.inject("dictated")
    assert raised.value.last_error == clipboard.ERROR_ACCESS_DENIED
    assert events == ["set:dictated", "paste", "restore", "restore"]


def test_set_open_timeout_does_not_attempt_spurious_restore(monkeypatch) -> None:
    injector = _windows_injector(monkeypatch)
    error = clipboard.ClipboardOpenTimeout(
        stage="set",
        timeout_s=0.05,
        attempts=5,
        elapsed_s=0.05,
        last_error=clipboard.ERROR_ACCESS_DENIED,
    )
    monkeypatch.setattr(
        clipboard,
        "snapshot_clipboard",
        lambda **_kwargs: [(clipboard.CF_UNICODETEXT, b"old")],
    )
    monkeypatch.setattr(
        clipboard,
        "set_clipboard_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        clipboard,
        "restore_clipboard",
        lambda *_args, **_kwargs: pytest.fail(
            "a pre-mutation publish timeout must not attempt restore"
        ),
    )
    with pytest.raises(clipboard.ClipboardOpenTimeout) as raised:
        injector.inject("dictated")
    assert raised.value.stage == "set"


def test_uncapturable_snapshot_refuses_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = _windows_injector(monkeypatch)
    set_values: list[str] = []
    monkeypatch.setattr(clipboard, "snapshot_clipboard", lambda **_kwargs: None)
    monkeypatch.setattr(
        clipboard, "set_clipboard_text", lambda text, **_kwargs: set_values.append(text)
    )
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: 10)
    monkeypatch.setattr(
        clipboard,
        "restore_clipboard",
        lambda *_args, **_kwargs: pytest.fail("uncapturable clipboard must not be restored"),
    )
    monkeypatch.setattr(
        clipboard,
        "clear_clipboard",
        lambda **_kwargs: pytest.fail("clipboard must never be cleared as a fallback"),
    )

    with pytest.raises(clipboard.ClipboardPreservationError):
        injector.inject("dictated")

    assert set_values == []


@pytest.mark.parametrize("snapshot", [[(clipboard.CF_UNICODETEXT, b"old")], []])
def test_snapshot_is_restored_after_paste(
    monkeypatch: pytest.MonkeyPatch, snapshot: list[tuple[int, bytes]]
) -> None:
    injector = _windows_injector(monkeypatch)
    events: list[object] = []
    monkeypatch.setattr(clipboard, "snapshot_clipboard", lambda **_kwargs: snapshot)
    monkeypatch.setattr(
        clipboard, "set_clipboard_text", lambda text, **_kwargs: events.append(("set", text))
    )
    monkeypatch.setattr(clipboard, "send_ctrl_v", lambda: events.append("paste"))
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: 20)
    monkeypatch.setattr(
        clipboard,
        "restore_clipboard",
        lambda value, **_kwargs: events.append(("restore", value)),
    )

    injector.inject("dictated")

    assert events == [("set", "dictated"), "paste", ("restore", snapshot)]


def test_newer_clipboard_write_skips_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = _windows_injector(monkeypatch)
    sequence = iter((30, 31))
    monkeypatch.setattr(
        clipboard,
        "snapshot_clipboard",
        lambda **_kwargs: [(clipboard.CF_UNICODETEXT, b"old")],
    )
    monkeypatch.setattr(clipboard, "set_clipboard_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: next(sequence))
    monkeypatch.setattr(
        clipboard,
        "restore_clipboard",
        lambda *_args, **_kwargs: pytest.fail("newer clipboard data must not be overwritten"),
    )

    injector.inject("dictated")


def test_restore_disabled_avoids_snapshot_and_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = _windows_injector(monkeypatch, restore_previous=False)
    monkeypatch.setattr(
        clipboard,
        "snapshot_clipboard",
        lambda **_kwargs: pytest.fail("snapshot should be disabled"),
    )
    monkeypatch.setattr(clipboard, "set_clipboard_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: 40)
    monkeypatch.setattr(
        clipboard,
        "restore_clipboard",
        lambda *_args, **_kwargs: pytest.fail("restore should be disabled"),
    )

    injector.inject("dictated")


def test_snapshot_timeout_refuses_before_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = _windows_injector(monkeypatch)
    pasted: list[str] = []

    def timeout(**_kwargs):
        raise TimeoutError

    monkeypatch.setattr(clipboard, "snapshot_clipboard", timeout)
    monkeypatch.setattr(
        clipboard, "set_clipboard_text", lambda text, **_kwargs: pasted.append(text)
    )
    monkeypatch.setattr(clipboard, "get_clipboard_sequence_number", lambda: 50)

    with pytest.raises(TimeoutError):
        injector.inject("dictated")

    assert pasted == []


def test_mixed_hglobal_and_special_format_snapshot_fails_closed(monkeypatch) -> None:
    values = iter((clipboard.CF_UNICODETEXT, clipboard.CF_BITMAP, 0))
    payload = ctypes.create_string_buffer(b"ok\0")

    class User32:
        @staticmethod
        def EnumClipboardFormats(_previous):
            return next(values)

        @staticmethod
        def GetClipboardData(_format):
            return 123

    class Kernel32:
        @staticmethod
        def GlobalSize(_handle):
            return len(payload)

        @staticmethod
        def GlobalLock(_handle):
            return ctypes.addressof(payload)

        @staticmethod
        def GlobalUnlock(_handle):
            return True

    monkeypatch.setattr(
        clipboard,
        "_opened_clipboard",
        lambda _timeout, **_kwargs: contextlib.nullcontext(),
    )
    monkeypatch.setattr(clipboard, "_user32", User32())
    monkeypatch.setattr(clipboard, "_kernel32", Kernel32())
    assert clipboard.snapshot_clipboard() is None


@pytest.mark.parametrize(
    "clipboard_format",
    [
        clipboard.CF_BITMAP,
        clipboard.CF_METAFILEPICT,
        clipboard.CF_PALETTE,
        clipboard.CF_ENHMETAFILE,
        clipboard.CF_DSPBITMAP,
        clipboard.CF_DSPMETAFILEPICT,
        clipboard.CF_DSPENHMETAFILE,
        clipboard.CF_OWNERDISPLAY,
    ],
)
def test_restore_rejects_standard_handle_owned_formats_before_emptying_clipboard(
    monkeypatch, clipboard_format
) -> None:
    monkeypatch.setattr(
        clipboard,
        "_opened_clipboard",
        lambda _timeout, **_kwargs: pytest.fail(
            "clipboard must not be opened for an unsafe snapshot"
        ),
    )
    with pytest.raises(clipboard.ClipboardPreservationError):
        clipboard.restore_clipboard([(clipboard_format, b"not-a-byte-cloneable-handle")])


@pytest.mark.parametrize(
    "clipboard_format",
    [1, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 16, 17, 0x0081, 0xC000],
)
def test_standard_and_registered_hglobal_formats_remain_cloneable(
    clipboard_format,
) -> None:
    assert clipboard._requires_type_specific_clone(clipboard_format) is False


@pytest.mark.parametrize(
    "clipboard_format",
    [
        clipboard.CF_PRIVATEFIRST,
        0x0280,
        clipboard.CF_PRIVATELAST,
        clipboard.CF_GDIOBJFIRST,
        0x0380,
        clipboard.CF_GDIOBJLAST,
    ],
)
def test_owner_managed_ranges_fail_closed_before_restore_mutation(
    monkeypatch, clipboard_format
) -> None:
    monkeypatch.setattr(
        clipboard,
        "_opened_clipboard",
        lambda _timeout, **_kwargs: pytest.fail(
            "clipboard must not open for owner-managed formats"
        ),
    )
    with pytest.raises(clipboard.ClipboardPreservationError):
        clipboard.restore_clipboard([(clipboard_format, b"owner-specific")])


@pytest.mark.parametrize(
    "clipboard_format",
    [clipboard.CF_PRIVATEFIRST, 0x0280, clipboard.CF_PRIVATELAST],
)
def test_private_format_snapshot_fails_closed(monkeypatch, clipboard_format) -> None:
    values = iter((clipboard_format, 0))

    class User32:
        @staticmethod
        def EnumClipboardFormats(_previous):
            return next(values)

        @staticmethod
        def GetClipboardData(_format):
            pytest.fail("private handle must not be inspected or cloned")

    monkeypatch.setattr(
        clipboard,
        "_opened_clipboard",
        lambda _timeout, **_kwargs: contextlib.nullcontext(),
    )
    monkeypatch.setattr(clipboard, "_user32", User32())
    assert clipboard.snapshot_clipboard() is None


def test_embedded_nul_text_refuses_before_clipboard_mutation(monkeypatch) -> None:
    monkeypatch.setattr(
        clipboard,
        "_opened_clipboard",
        lambda _timeout, **_kwargs: pytest.fail(
            "clipboard must not be opened for embedded NUL text"
        ),
    )
    with pytest.raises(ValueError, match="embedded NUL"):
        clipboard.set_clipboard_text("before\0after")


def test_set_timeout_releases_untransferred_global_allocation(monkeypatch) -> None:
    payload = ctypes.create_string_buffer(32)
    freed: list[int] = []

    class Kernel32:
        @staticmethod
        def GlobalAlloc(_flags, _size):
            return 123

        @staticmethod
        def GlobalLock(_handle):
            return ctypes.addressof(payload)

        @staticmethod
        def GlobalUnlock(_handle):
            return True

        @staticmethod
        def GlobalFree(handle):
            freed.append(int(handle))
            return 0

    class Denied:
        def __init__(self, _timeout, **_kwargs):
            pass

        def __enter__(self):
            raise clipboard.ClipboardOpenTimeout(
                stage="set",
                timeout_s=0.01,
                attempts=2,
                elapsed_s=0.01,
                last_error=clipboard.ERROR_ACCESS_DENIED,
            )

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(clipboard, "_kernel32", Kernel32())
    monkeypatch.setattr(clipboard, "_opened_clipboard", Denied)
    with pytest.raises(clipboard.ClipboardOpenTimeout):
        clipboard.set_clipboard_text("dictated", timeout_s=0.01)
    assert freed == [123]


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="requires the real Windows clipboard")
def test_real_clipboard_snapshot_restore_round_trip() -> None:
    original = clipboard.snapshot_clipboard()
    if original is None:
        pytest.skip("current clipboard contains formats that cannot be restored faithfully")
    try:
        custom = int(clipboard._user32.RegisterClipboardFormatW("DCENT Test Arbitrary Format"))
        custom_bytes = b"arbitrary-unlisted-\x00\x01\xff"
        clipboard.restore_clipboard(
            [
                (
                    clipboard.CF_UNICODETEXT,
                    ("snapshot round trip\0").encode("utf-16-le"),
                ),
                (custom, custom_bytes),
            ]
        )
        snapshot = clipboard.snapshot_clipboard()
        assert snapshot is not None
        assert dict(snapshot)[custom] == custom_bytes
        clipboard.set_clipboard_text("replacement")
        clipboard.restore_clipboard(snapshot)
        assert clipboard.get_clipboard_text() == "snapshot round trip"
        assert dict(clipboard.snapshot_clipboard() or [])[custom] == custom_bytes
    finally:
        clipboard.restore_clipboard(original)
