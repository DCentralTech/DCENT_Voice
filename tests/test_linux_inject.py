# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from dcent_voice.inject import linux
from dcent_voice.inject.clipboard import ClipboardPreservationError


class _Atom:
    def __init__(self, value: str) -> None:
        self.value = value

    def name(self) -> str:
        return self.value


class _Selection:
    def __init__(self, target: str, format_bits: int, data: bytes) -> None:
        self.target = target
        self.format_bits = format_bits
        self.data = data

    def get_format(self) -> int:
        return self.format_bits

    def get_data(self) -> bytes:
        return self.data


@dataclass
class _TargetEntry:
    target: str
    info: int


class _TargetEntryFactory:
    @classmethod
    def new(cls, target: str, _flags: int, info: int) -> _TargetEntry:
        return _TargetEntry(target, info)


class _Gtk:
    TargetEntry = _TargetEntryFactory


class _Gdk:
    @staticmethod
    def atom_name(atom: _Atom) -> str:
        return atom.value


class _OutgoingSelection:
    def __init__(self, target: str, destination: dict[str, tuple[int, bytes]]) -> None:
        self.target = target
        self.destination = destination

    def get_target(self) -> _Atom:
        return _Atom(self.target)

    def set(self, _atom: _Atom, format_bits: int, data: bytes) -> None:
        self.destination[self.target] = (format_bits, bytes(data))


class _GtkBoard:
    def __init__(self, values: dict[str, tuple[int, bytes]]) -> None:
        self.values = dict(values)
        self.unreadable: set[str] = set()
        self.reject_restore = False
        self.clear_calls = 0

    def wait_for_targets(self):
        if not self.values:
            return False, []
        return True, [_Atom(name) for name in self.values]

    def wait_for_contents(self, atom: _Atom):
        if atom.value in self.unreadable:
            return None
        format_bits, data = self.values[atom.value]
        return _Selection(atom.value, format_bits, data)

    def set_with_data(self, entries, provide, _released, snapshot) -> bool:
        if self.reject_restore:
            return False
        restored: dict[str, tuple[int, bytes]] = {}
        for entry in entries:
            selection = _OutgoingSelection(entry.target, restored)
            provide(self, selection, entry.info, snapshot)
        self.values = restored
        return True

    def clear(self) -> None:
        self.clear_calls += 1
        self.values = {}


def _install_gtk_fake(monkeypatch: pytest.MonkeyPatch, board: _GtkBoard) -> None:
    monkeypatch.setattr(linux, "_gtk_clipboard_api", lambda: (board, _Gdk, _Gtk))


def test_wayland_detection(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert linux.is_wayland() is True
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert linux.is_wayland() is False


def test_clipboard_tools_prefer_wayland(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    set_argv, _ = linux._clipboard_tools()
    assert set_argv is not None and set_argv[0] == "wl-copy"
    # wtype (Wayland virtual-keyboard protocol, no root daemon) wins when present.
    assert linux._paste_argv()[0] == "wtype"
    assert linux._copy_argv()[-2:] == ["-m", "ctrl"]


def test_wayland_paste_falls_back_to_ydotool(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    tools = {"wl-copy", "wl-paste", "ydotool"}
    monkeypatch.setattr(
        linux.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None
    )
    assert linux._paste_argv()[0] == "ydotool"
    assert linux._copy_argv()[0] == "ydotool"


def test_clipboard_tools_fall_back_to_xclip_on_x11(monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    tools = {"xclip", "xdotool"}
    monkeypatch.setattr(
        linux.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None
    )
    set_argv, _ = linux._clipboard_tools()
    assert set_argv is not None and set_argv[0] == "xclip"
    assert linux._paste_argv()[0] == "xdotool"
    assert linux._copy_argv()[-1] == "ctrl+c"


def test_missing_tools_raise_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(linux.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        linux.LinuxClipboardPasteInjector().inject("hello")


def test_backspace_argv_wtype_and_xdotool(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv = linux._backspace_argv(3)
    assert argv is not None
    assert argv[0] == "wtype"
    assert argv.count("BackSpace") == 3

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    tools = {"xdotool"}
    monkeypatch.setattr(
        linux.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None
    )
    argv = linux._backspace_argv(2)
    assert argv is not None
    assert argv[0] == "xdotool"
    assert argv.count("BackSpace") == 2


def test_linux_retract_raises_without_tools(monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(linux.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="retract"):
        linux.LinuxClipboardPasteInjector().retract(4)


def test_linux_retract_runs_backspace_argv(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(
        linux.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xdotool" else None
    )
    monkeypatch.setattr(
        linux,
        "_run",
        lambda argv, text=None, stage="helper": calls.append(list(argv)) or "",
    )
    linux.LinuxClipboardPasteInjector().retract(2)
    assert calls
    assert calls[0][0] == "xdotool"
    assert calls[0].count("BackSpace") == 2


def test_enter_argv_wtype_and_xdotool(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv = linux._enter_argv()
    assert argv == ["wtype", "-k", "Return"]

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    tools = {"xdotool"}
    monkeypatch.setattr(
        linux.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None
    )
    argv = linux._enter_argv()
    assert argv is not None
    assert argv[0] == "xdotool"
    assert "Return" in argv


def test_linux_press_enter_raises_without_tools(monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(linux.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Enter"):
        linux.LinuxClipboardPasteInjector().press_enter()


def test_gtk_snapshot_restores_images_files_rtf_and_multiple_mime_types(monkeypatch) -> None:
    original = {
        "text/plain;charset=utf-8": (8, b"formatted text"),
        "text/rtf": (8, b"{\\rtf1 formatted text}"),
        "image/png": (8, b"\x89PNG\r\n\x1a\nimage"),
        "text/uri-list": (8, b"file:///home/user/report.pdf\r\n"),
        "application/x-custom": (32, b"\x01\x00\x00\x00"),
    }
    board = _GtkBoard(original)
    _install_gtk_fake(monkeypatch, board)

    snapshot = linux.snapshot_clipboard()
    board.values = {"text/plain;charset=utf-8": (8, b"dictated")}
    linux.restore_clipboard(snapshot)

    assert board.values == original


def test_gtk_unreadable_mime_fails_closed_before_helper_mutation(monkeypatch) -> None:
    board = _GtkBoard({"image/png": (8, b"image")})
    board.unreadable.add("image/png")
    _install_gtk_fake(monkeypatch, board)
    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["wl-copy"], ["wl-paste"]))
    monkeypatch.setattr(linux, "_paste_argv", lambda: ["wtype"])
    monkeypatch.setattr(
        linux,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("helpers must not run after unsafe snapshot"),
    )

    with pytest.raises(ClipboardPreservationError, match="would not provide"):
        linux.LinuxClipboardPasteInjector().inject("dictated")

    assert board.values == {"image/png": (8, b"image")}


def test_gtk_restore_rejection_is_not_silent(monkeypatch) -> None:
    board = _GtkBoard({"text/rtf": (8, b"rich")})
    _install_gtk_fake(monkeypatch, board)
    snapshot = linux.snapshot_clipboard()
    board.reject_restore = True

    with pytest.raises(ClipboardPreservationError, match="rejected"):
        linux.restore_clipboard(snapshot)


def test_helper_nonzero_exit_is_stage_specific(monkeypatch) -> None:
    monkeypatch.setattr(
        linux.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["wl-copy"], 4, stdout=b"", stderr=b"permission denied"
        ),
    )

    with pytest.raises(linux.ClipboardCommandError) as raised:
        linux._run(["wl-copy"], text="dictated", stage="publish_text")

    assert raised.value.stage == "publish_text"
    assert "exit status 4" in str(raised.value)


def test_helper_timeout_is_bounded_and_stage_specific(monkeypatch) -> None:
    def timeout(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, linux._HELPER_TIMEOUT_S)

    monkeypatch.setattr(linux.subprocess, "run", timeout)

    with pytest.raises(linux.ClipboardCommandError) as raised:
        linux._run(["wl-paste"], stage="selection_read")

    assert raised.value.stage == "selection_read"
    assert "timed out" in str(raised.value)


def test_publish_failure_never_sends_paste_and_restores_snapshot(monkeypatch) -> None:
    snapshot = (linux.ClipboardTarget("image/png", 8, b"image"),)
    calls: list[str] = []
    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["wl-copy"], ["wl-paste"]))
    monkeypatch.setattr(linux, "_paste_argv", lambda: ["wtype"])
    monkeypatch.setattr(linux, "snapshot_clipboard", lambda: snapshot)
    monkeypatch.setattr(
        linux,
        "restore_clipboard",
        lambda value: calls.append("restore") if value == snapshot else None,
    )

    def run(_argv, *, text=None, stage="helper"):
        calls.append(stage)
        if stage == "publish_text":
            raise linux.ClipboardCommandError(stage=stage, argv=["wl-copy"], detail="denied")
        return ""

    monkeypatch.setattr(linux, "_run", run)

    with pytest.raises(linux.ClipboardCommandError):
        linux.LinuxClipboardPasteInjector().inject("dictated")

    assert calls == ["publish_text", "restore"]


def test_paste_failure_restores_every_captured_format(monkeypatch) -> None:
    snapshot = (
        linux.ClipboardTarget("text/rtf", 8, b"rich"),
        linux.ClipboardTarget("image/png", 8, b"image"),
    )
    published = (linux.ClipboardTarget("text/plain;charset=utf-8", 8, b"dictated"),)
    snapshots = iter((snapshot, published, published))
    calls: list[str] = []
    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["xclip"], ["xclip", "-o"]))
    monkeypatch.setattr(linux, "_paste_argv", lambda: ["xdotool"])
    monkeypatch.setattr(linux, "snapshot_clipboard", lambda: next(snapshots))
    monkeypatch.setattr(
        linux,
        "restore_clipboard",
        lambda value: calls.append("restore") if value == snapshot else None,
    )
    monkeypatch.setattr(linux.time, "sleep", lambda _seconds: None)

    def run(_argv, *, text=None, stage="helper"):
        calls.append(stage)
        if stage == "paste_shortcut":
            raise linux.ClipboardCommandError(stage=stage, argv=["xdotool"], detail="denied")
        return ""

    monkeypatch.setattr(linux, "_run", run)

    with pytest.raises(linux.ClipboardCommandError):
        linux.LinuxClipboardPasteInjector().inject("dictated")

    assert calls == ["publish_text", "paste_shortcut", "restore"]


def test_zero_exit_without_requested_payload_never_sends_paste(monkeypatch) -> None:
    previous = (linux.ClipboardTarget("image/png", 8, b"old-image"),)
    stale = (linux.ClipboardTarget("text/plain", 8, b"old clipboard"),)
    snapshots = iter((previous, stale, stale))
    calls: list[str] = []
    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["wl-copy"], ["wl-paste"]))
    monkeypatch.setattr(linux, "_paste_argv", lambda: ["wtype"])
    monkeypatch.setattr(linux, "snapshot_clipboard", lambda: next(snapshots))
    monkeypatch.setattr(linux.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(linux, "_run", lambda _argv, **kwargs: calls.append(kwargs["stage"]) or "")
    monkeypatch.setattr(linux, "restore_clipboard", lambda value: calls.append("restore"))

    with pytest.raises(linux.ClipboardCommandError) as raised:
        linux.LinuxClipboardPasteInjector().inject("dictated")

    assert raised.value.stage == "verify_publish"
    assert calls == ["publish_text", "restore"]


def test_newer_linux_clipboard_write_is_never_overwritten(monkeypatch) -> None:
    previous = (linux.ClipboardTarget("image/png", 8, b"old-image"),)
    published = (linux.ClipboardTarget("text/plain", 8, b"dictated"),)
    newer = (linux.ClipboardTarget("text/uri-list", 8, b"file:///newer"),)
    snapshots = iter((previous, published, newer))
    monkeypatch.setattr(linux, "_clipboard_tools", lambda: (["wl-copy"], ["wl-paste"]))
    monkeypatch.setattr(linux, "_paste_argv", lambda: ["wtype"])
    monkeypatch.setattr(linux, "snapshot_clipboard", lambda: next(snapshots))
    monkeypatch.setattr(linux, "_run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(linux.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        linux,
        "restore_clipboard",
        lambda _snapshot: pytest.fail("a newer user clipboard must not be overwritten"),
    )

    with pytest.raises(ClipboardPreservationError, match="newer contents"):
        linux.LinuxClipboardPasteInjector().inject("dictated")
