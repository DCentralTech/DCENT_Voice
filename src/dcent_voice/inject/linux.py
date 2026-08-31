# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Linux clipboard-paste injection (X11 + Wayland).

Sets the clipboard (wl-copy on Wayland, xclip/xsel on X11) then sends Ctrl+V
(ydotool on Wayland, xdotool on X11). The keystroke injector (pynput) already
works on X11; this fills the clipboard-paste gap for long text. Tool availability
is detected at call time so a missing helper degrades to a clear no-op rather
than a crash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from dcent_voice.inject.base import Injector
from dcent_voice.inject.clipboard import ClipboardPreservationError

_HELPER_TIMEOUT_S = 2.0
_PROTOCOL_TARGETS = frozenset(
    {
        "TARGETS",
        "MULTIPLE",
        "TIMESTAMP",
        "SAVE_TARGETS",
        "DELETE",
        "INSERT_SELECTION",
    }
)


@dataclass(frozen=True)
class ClipboardTarget:
    """One eagerly materialized X11/Wayland selection representation."""

    type_name: str
    format_bits: int
    data: bytes


ClipboardSnapshot = tuple[ClipboardTarget, ...]


class ClipboardCommandError(RuntimeError):
    """A bounded external clipboard or input helper failed."""

    def __init__(self, *, stage: str, argv: list[str], detail: str) -> None:
        self.stage = stage
        self.argv = tuple(argv)
        super().__init__(f"Linux clipboard stage {stage!r} failed ({argv[0]}): {detail}")


_ACTIVE_RESTORE_OWNER: tuple[Any, ...] | None = None


def is_wayland() -> bool:
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE") == "wayland"


def _clipboard_tools() -> tuple[list[str] | None, list[str] | None]:
    """Return (set_argv, get_argv) for the current session, or (None, None)."""
    if is_wayland() and shutil.which("wl-copy"):
        return ["wl-copy"], (["wl-paste", "-n"] if shutil.which("wl-paste") else None)
    if shutil.which("xclip"):
        return (
            ["xclip", "-selection", "clipboard"],
            ["xclip", "-selection", "clipboard", "-o"],
        )
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]
    return None, None


def _paste_argv() -> list[str] | None:
    if is_wayland():
        # Prefer wtype: it uses the Wayland virtual-keyboard protocol (wlroots,
        # KDE), so it needs no root daemon or /dev/uinput access like ydotool.
        if shutil.which("wtype"):
            return ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]
        if shutil.which("ydotool"):
            return ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]  # ctrl v ctrl
    if shutil.which("xdotool"):
        return ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
    return None


def _copy_argv() -> list[str] | None:
    if is_wayland():
        if shutil.which("wtype"):
            return ["wtype", "-M", "ctrl", "-k", "c", "-m", "ctrl"]
        if shutil.which("ydotool"):
            return ["ydotool", "key", "29:1", "46:1", "46:0", "29:0"]
    if shutil.which("xdotool"):
        return ["xdotool", "key", "--clearmodifiers", "ctrl+c"]
    return None


def _backspace_argv(count: int) -> list[str] | None:
    """Return argv that synthesizes ``count`` Backspaces, or None if unavailable."""
    if count <= 0:
        return None
    n = int(count)
    if is_wayland():
        if shutil.which("wtype"):
            # One -k BackSpace per character (wtype has no repeat multiplier).
            argv: list[str] = ["wtype"]
            for _ in range(n):
                argv.extend(["-k", "BackSpace"])
            return argv
        if shutil.which("ydotool"):
            # KEY_BACKSPACE is 14 on Linux.
            keys: list[str] = []
            for _ in range(n):
                keys.extend(["14:1", "14:0"])
            return ["ydotool", "key", *keys]
    if shutil.which("xdotool"):
        return ["xdotool", "key", "--clearmodifiers", *(["BackSpace"] * n)]
    return None


def _enter_argv() -> list[str] | None:
    if is_wayland():
        if shutil.which("wtype"):
            return ["wtype", "-k", "Return"]
        if shutil.which("ydotool"):
            # KEY_ENTER is 28 on Linux.
            return ["ydotool", "key", "28:1", "28:0"]
    if shutil.which("xdotool"):
        return ["xdotool", "key", "--clearmodifiers", "Return"]
    return None


def _run_bytes(
    argv: list[str],
    *,
    payload: bytes | None = None,
    stage: str,
) -> bytes:
    try:
        if payload is not None:
            # Clipboard owners fork and may retain inherited output handles.
            # DEVNULL keeps the foreground helper bounded without killing the
            # process that continues to own the selection.
            result = subprocess.run(
                argv,
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_HELPER_TIMEOUT_S,
            )
        else:
            result = subprocess.run(argv, capture_output=True, timeout=_HELPER_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ClipboardCommandError(
            stage=stage,
            argv=argv,
            detail=f"timed out after {_HELPER_TIMEOUT_S:.1f}s",
        ) from exc
    except OSError as exc:
        raise ClipboardCommandError(stage=stage, argv=argv, detail=str(exc)) from exc
    if result.returncode != 0:
        stderr = bytes(result.stderr or b"").decode("utf-8", "replace").strip()
        detail = f"exit status {result.returncode}"
        if stderr:
            detail += f": {stderr[:300]}"
        raise ClipboardCommandError(stage=stage, argv=argv, detail=detail)
    return bytes(result.stdout or b"")


def _run(
    argv: list[str],
    *,
    text: str | None = None,
    stage: str = "helper",
) -> str:
    payload = None if text is None else text.encode("utf-8")
    return _run_bytes(argv, payload=payload, stage=stage).decode("utf-8", "replace")


def _gtk_clipboard_api():  # pragma: no cover - exercised with platform fakes
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk

    return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD), Gdk, Gtk


def _atom_name(atom: Any, gdk: Any) -> str:
    name_method = getattr(atom, "name", None)
    raw_name = name_method() if callable(name_method) else gdk.atom_name(atom)
    if raw_name is None:
        return ""
    if isinstance(raw_name, bytes):
        return raw_name.decode("utf-8", "strict")
    return str(raw_name)


def _snapshot_from_board(board: Any, gdk: Any, *, allow_empty: bool) -> ClipboardSnapshot:
    try:
        available, atoms = board.wait_for_targets()
    except Exception as exc:
        raise ClipboardPreservationError("Linux clipboard target enumeration failed.") from exc
    if not available:
        if allow_empty and not atoms:
            return ()
        raise ClipboardPreservationError(
            "Linux clipboard targets could not be enumerated losslessly."
        )

    captured: list[ClipboardTarget] = []
    seen: set[str] = set()
    for atom in atoms or []:
        type_name = _atom_name(atom, gdk)
        if type_name in _PROTOCOL_TARGETS:
            continue
        if not type_name or type_name in seen:
            raise ClipboardPreservationError(
                "Linux clipboard contains an invalid or duplicate MIME target."
            )
        try:
            selection = board.wait_for_contents(atom)
        except Exception as exc:
            raise ClipboardPreservationError(
                f"Linux clipboard target {type_name!r} could not be read."
            ) from exc
        if selection is None:
            raise ClipboardPreservationError(
                f"Linux clipboard owner would not provide {type_name!r}."
            )
        format_bits = int(selection.get_format())
        if format_bits not in {8, 16, 32}:
            raise ClipboardPreservationError(
                f"Linux clipboard target {type_name!r} has unsupported format {format_bits}."
            )
        try:
            payload = bytes(selection.get_data())
        except (TypeError, ValueError) as exc:
            raise ClipboardPreservationError(
                f"Linux clipboard target {type_name!r} is not byte-cloneable."
            ) from exc
        unit_bytes = format_bits // 8
        if len(payload) % unit_bytes:
            raise ClipboardPreservationError(
                f"Linux clipboard target {type_name!r} has a truncated payload."
            )
        captured.append(ClipboardTarget(type_name=type_name, format_bits=format_bits, data=payload))
        seen.add(type_name)
    if not captured and not allow_empty:
        raise ClipboardPreservationError(
            "Linux clipboard has no data targets that can be restored losslessly."
        )
    return tuple(captured)


def snapshot_clipboard() -> ClipboardSnapshot:
    """Clone all Linux selection targets using the native GTK clipboard API."""

    try:
        board, gdk, _gtk = _gtk_clipboard_api()
    except (ImportError, RuntimeError) as exc:
        raise ClipboardPreservationError(
            "Lossless Linux clipboard preservation requires GTK 3/PyGObject."
        ) from exc
    return _snapshot_from_board(board, gdk, allow_empty=False)


def _snapshot_map(snapshot: ClipboardSnapshot) -> dict[str, tuple[int, bytes]]:
    return {item.type_name: (item.format_bits, item.data) for item in snapshot}


def _snapshot_contains_text(snapshot: ClipboardSnapshot, expected: str) -> bool:
    expected_bytes = expected.encode("utf-8")
    for item in snapshot:
        normalized = item.type_name.casefold()
        if not (
            normalized.startswith("text/plain")
            or normalized in {"utf8_string", "text", "string", "compound_text"}
        ):
            continue
        # X11 text selections may expose one terminal NUL that is not part of
        # the logical string. Do not normalize any other bytes.
        candidate = item.data[:-1] if item.data.endswith(b"\0") else item.data
        if candidate == expected_bytes:
            return True
    return False


def restore_clipboard(snapshot: ClipboardSnapshot) -> None:
    """Publish every captured MIME target and verify the restored byte payloads."""

    global _ACTIVE_RESTORE_OWNER

    try:
        board, gdk, gtk = _gtk_clipboard_api()
    except (ImportError, RuntimeError) as exc:
        raise ClipboardPreservationError(
            "Lossless Linux clipboard restoration requires GTK 3/PyGObject."
        ) from exc

    if len(_snapshot_map(snapshot)) != len(snapshot):
        raise ClipboardPreservationError("Linux clipboard snapshot contains duplicate targets.")
    if not snapshot:
        raise ClipboardPreservationError(
            "An empty Linux selection cannot be restored losslessly through GTK."
        )

    entries = [gtk.TargetEntry.new(item.type_name, 0, index) for index, item in enumerate(snapshot)]

    def provide(_clipboard: Any, selection: Any, info: int, values: ClipboardSnapshot) -> None:
        captured = values[int(info)]
        selection.set(selection.get_target(), captured.format_bits, captured.data)

    def released(*_args: Any) -> None:
        return None

    try:
        restored = bool(board.set_with_data(entries, provide, released, snapshot))
    except Exception as exc:
        raise ClipboardPreservationError("Linux clipboard all-format restore failed.") from exc
    if not restored:
        raise ClipboardPreservationError("Linux clipboard rejected the all-format restore.")

    # GTK serves clipboard formats lazily, so retain the callbacks for as long
    # as this process owns the selection and verify every payload immediately.
    _ACTIVE_RESTORE_OWNER = (board, provide, released, snapshot, entries)
    verified = _snapshot_from_board(board, gdk, allow_empty=False)
    if verified != snapshot:
        raise ClipboardPreservationError(
            "Linux clipboard restore verification did not reproduce every MIME target."
        )


class LinuxClipboardPasteInjector(Injector):
    """Linux clipboard text injector with desktop-environment fallbacks."""

    def __init__(self, *, restore_previous: bool = True) -> None:
        self.restore_previous = restore_previous

    def inject(self, text: str) -> None:
        if not text:
            return
        set_argv, _get_argv = _clipboard_tools()
        paste_argv = _paste_argv()
        if set_argv is None or paste_argv is None:
            raise RuntimeError(
                "Linux clipboard injection needs wl-clipboard + wtype/ydotool "
                "(Wayland) or xclip/xsel + xdotool (X11) installed."
            )
        previous = snapshot_clipboard() if self.restore_previous else None
        published = False
        published_snapshot: ClipboardSnapshot | None = None
        try:
            _run(set_argv, text=text, stage="publish_text")
            published = True
            if previous is not None or _get_argv is None:
                published_snapshot = snapshot_clipboard()
                if not _snapshot_contains_text(published_snapshot, text):
                    raise ClipboardCommandError(
                        stage="verify_publish",
                        argv=set_argv,
                        detail="helper exited successfully but did not publish the requested text",
                    )
            elif _run(_get_argv, stage="verify_publish") != text:
                raise ClipboardCommandError(
                    stage="verify_publish",
                    argv=set_argv,
                    detail="helper exited successfully but did not publish the requested text",
                )
            _run(paste_argv, stage="paste_shortcut")
        finally:
            if previous is not None:
                # The target app reads the clipboard asynchronously after Ctrl+V;
                # restoring immediately would race it into pasting the old content.
                if published:
                    time.sleep(0.05)
                if published_snapshot is not None and snapshot_clipboard() != published_snapshot:
                    raise ClipboardPreservationError(
                        "Linux clipboard changed after dictation publish; "
                        "refusing to overwrite the newer contents."
                    )
                restore_clipboard(previous)

    def retract(self, char_count: int) -> None:
        if char_count <= 0:
            return
        argv = _backspace_argv(int(char_count))
        if argv is None:
            raise RuntimeError(
                "Linux retract needs wtype/ydotool (Wayland) or xdotool (X11) "
                "to synthesize Backspace for streaming corrections."
            )
        # Batch large retracts so argv stays within practical process limits.
        if argv[0] == "wtype" and char_count > 40:
            remaining = int(char_count)
            while remaining > 0:
                chunk = min(40, remaining)
                _run(_backspace_argv(chunk) or argv, stage="retract_shortcut")
                remaining -= chunk
            return
        _run(argv, stage="retract_shortcut")

    def press_enter(self) -> None:
        argv = _enter_argv()
        if argv is None:
            raise RuntimeError(
                "Linux Enter needs wtype/ydotool (Wayland) or xdotool (X11) "
                "to submit after a spoken press-enter cue."
            )
        _run(argv, stage="enter_shortcut")
