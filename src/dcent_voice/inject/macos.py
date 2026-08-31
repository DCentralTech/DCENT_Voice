# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""macOS clipboard-paste injection.

Sets the general pasteboard via NSPasteboard and sends Cmd+V via CoreGraphics
CGEvent. Requires pyobjc (AppKit + Quartz) and the app must be granted
Accessibility permission for the synthetic key events to be delivered. The
keystroke injector (pynput) is the fallback and needs the same permission.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from dcent_voice.inject.base import Injector
from dcent_voice.inject.clipboard import ClipboardPreservationError

_CMD_KEY = 0x37  # kVK_Command
_V_KEY = 0x09  # kVK_ANSI_V
_C_KEY = 0x08  # kVK_ANSI_C
_BACKSPACE_KEY = 0x33  # kVK_Delete (Backspace; forward-delete is 0x75)
_RETURN_KEY = 0x24  # kVK_Return


@dataclass(frozen=True)
class PasteboardFormat:
    """One eagerly materialized pasteboard representation."""

    type_name: str
    data: bytes


PasteboardSnapshot = tuple[tuple[PasteboardFormat, ...], ...]

_PROTOCOL_PASTEBOARD_TYPES = frozenset({"TARGETS", "MULTIPLE", "TIMESTAMP"})


def _pasteboard():  # pragma: no cover - macOS only
    from AppKit import NSPasteboard, NSStringPboardType

    return NSPasteboard, NSStringPboardType


def _pasteboard_api():  # pragma: no cover - macOS only
    from AppKit import NSPasteboard, NSPasteboardItem, NSStringPboardType
    from Foundation import NSData

    return NSPasteboard, NSPasteboardItem, NSStringPboardType, NSData


def get_clipboard_text() -> str | None:  # pragma: no cover - macOS only
    NSPasteboard, NSStringPboardType = _pasteboard()
    return NSPasteboard.generalPasteboard().stringForType_(NSStringPboardType)


def get_clipboard_change_count() -> int:  # pragma: no cover - macOS only
    NSPasteboard, _NSStringPboardType = _pasteboard()
    return int(NSPasteboard.generalPasteboard().changeCount())


def set_clipboard_text(text: str) -> None:  # pragma: no cover - macOS only
    NSPasteboard, NSStringPboardType = _pasteboard()
    board = NSPasteboard.generalPasteboard()
    board.clearContents()
    if not board.setString_forType_(text, NSStringPboardType):
        raise RuntimeError("macOS pasteboard refused the temporary text payload.")


def snapshot_clipboard() -> PasteboardSnapshot:  # pragma: no cover - macOS only
    """Materialize every representation of every general-pasteboard item.

    A lazy owner may refuse to provide one of its advertised representations.
    In that case mutation is unsafe, so callers fail closed before clearing the
    board instead of silently degrading images, files, or rich text to text.
    """

    NSPasteboard, _NSPasteboardItem, _string_type, _NSData = _pasteboard_api()
    board = NSPasteboard.generalPasteboard()
    start_count = int(board.changeCount())
    raw_items = list(board.pasteboardItems() or [])
    board_types = {
        str(type_name)
        for type_name in (board.types() or [])
        if str(type_name) not in _PROTOCOL_PASTEBOARD_TYPES
    }
    if board_types and not raw_items:
        raise ClipboardPreservationError(
            "macOS pasteboard advertises data that cannot be materialized as items."
        )

    captured: list[tuple[PasteboardFormat, ...]] = []
    for item in raw_items:
        formats: list[PasteboardFormat] = []
        seen: set[str] = set()
        for raw_type in item.types() or []:
            type_name = str(raw_type)
            if type_name in _PROTOCOL_PASTEBOARD_TYPES:
                continue
            if not type_name or type_name in seen:
                raise ClipboardPreservationError(
                    "macOS pasteboard contains an invalid or duplicate representation."
                )
            value = item.dataForType_(raw_type)
            if value is None:
                raise ClipboardPreservationError(
                    f"macOS pasteboard owner would not provide {type_name!r}."
                )
            try:
                payload = bytes(value)
            except (TypeError, ValueError) as exc:
                raise ClipboardPreservationError(
                    f"macOS pasteboard representation {type_name!r} is not byte-cloneable."
                ) from exc
            formats.append(PasteboardFormat(type_name=type_name, data=payload))
            seen.add(type_name)
        if not formats:
            raise ClipboardPreservationError(
                "macOS pasteboard item has no byte-cloneable representations."
            )
        captured.append(tuple(formats))

    if int(board.changeCount()) != start_count:
        raise ClipboardPreservationError("macOS pasteboard changed while it was being cloned.")
    return tuple(captured)


def restore_clipboard(snapshot: PasteboardSnapshot) -> None:  # pragma: no cover - macOS only
    """Restore an all-format snapshot, checking every preflight and board write."""

    NSPasteboard, NSPasteboardItem, _string_type, NSData = _pasteboard_api()
    board = NSPasteboard.generalPasteboard()
    restored_items: list[Any] = []

    # Build and validate detached NSPasteboardItems before mutating the board.
    for captured_item in snapshot:
        if not captured_item:
            raise ClipboardPreservationError("macOS snapshot contains an empty item.")
        restored = NSPasteboardItem.alloc().init()
        for captured_format in captured_item:
            value = NSData.dataWithBytes_length_(captured_format.data, len(captured_format.data))
            if value is None or not restored.setData_forType_(value, captured_format.type_name):
                raise ClipboardPreservationError(
                    "macOS could not prepare pasteboard representation "
                    f"{captured_format.type_name!r} for restoration."
                )
        restored_items.append(restored)

    board.clearContents()
    if restored_items and not board.writeObjects_(restored_items):
        raise ClipboardPreservationError(
            "macOS pasteboard rejected the all-format restore after mutation."
        )
    if not restored_items and list(board.pasteboardItems() or []):
        raise ClipboardPreservationError("macOS pasteboard could not restore its empty state.")
    if snapshot_clipboard() != snapshot:
        raise ClipboardPreservationError(
            "macOS pasteboard restore verification did not reproduce every representation."
        )


def _send_cmd_key(key_code: int) -> None:  # pragma: no cover - macOS only
    import Quartz

    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for keydown in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(source, key_code, keydown)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.005)


def send_cmd_v() -> None:  # pragma: no cover - macOS only
    _send_cmd_key(_V_KEY)


def send_cmd_c() -> None:  # pragma: no cover - macOS only
    _send_cmd_key(_C_KEY)


def send_backspaces(count: int) -> None:  # pragma: no cover - macOS only
    """Synthesize ``count`` Backspace keypresses into the focused field."""
    if count <= 0:
        return
    import Quartz

    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for _ in range(int(count)):
        for keydown in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(source, _BACKSPACE_KEY, keydown)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.002)


def send_enter() -> None:  # pragma: no cover - macOS only
    """Synthesize one Return keypress into the focused field."""
    import Quartz

    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for keydown in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(source, _RETURN_KEY, keydown)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.005)


class MacOSClipboardPasteInjector(Injector):
    """macOS clipboard text injector using native paste events."""

    def __init__(self, *, restore_previous: bool = True) -> None:
        self.restore_previous = restore_previous

    def inject(self, text: str) -> None:  # pragma: no cover - macOS only
        if not text:
            return
        try:
            import AppKit  # noqa: F401
            import Quartz  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "macOS clipboard injection requires pyobjc (AppKit + Quartz)."
            ) from exc
        previous = snapshot_clipboard() if self.restore_previous else None
        published = False
        published_change_count: int | None = None
        try:
            set_clipboard_text(text)
            published = True
            published_change_count = get_clipboard_change_count()
            send_cmd_v()
        finally:
            if previous is not None:
                if published:
                    time.sleep(0.05)
                if (
                    published_change_count is not None
                    and get_clipboard_change_count() != published_change_count
                ):
                    raise ClipboardPreservationError(
                        "macOS pasteboard changed after dictation publish; "
                        "refusing to overwrite the newer contents."
                    )
                restore_clipboard(previous)

    def retract(self, char_count: int) -> None:  # pragma: no cover - macOS only
        if char_count <= 0:
            return
        try:
            import Quartz  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "macOS retract requires pyobjc Quartz for Backspace synthesis."
            ) from exc
        send_backspaces(int(char_count))

    def press_enter(self) -> None:  # pragma: no cover - macOS only
        try:
            import Quartz  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("macOS Enter requires pyobjc Quartz for Return synthesis.") from exc
        send_enter()
