# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import ctypes
import sys

import pytest

from dcent_voice.inject import clipboard as cb
from dcent_voice.inject.base import Injector, RetractUnsupported
from dcent_voice.inject.router import RoutingInjector, get_foreground_process_name
from tests.win32_native import requires_win32_native


class RecordingInjector(Injector):
    def __init__(self) -> None:
        self.values: list[str] = []
        self.retracted: list[int] = []
        self.enters: int = 0
        self.buffer = ""

    def inject(self, text: str) -> None:
        self.values.append(text)
        self.buffer += text

    def retract(self, char_count: int) -> None:
        n = max(0, int(char_count))
        self.retracted.append(n)
        if n:
            self.buffer = self.buffer[:-n] if n <= len(self.buffer) else ""

    def press_enter(self) -> None:
        self.enters += 1


class NoRetractClipboard(Injector):
    """Clipboard-like injector that inherits the base retract failure."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self.buffer = ""

    def inject(self, text: str) -> None:
        self.values.append(text)
        self.buffer += text


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 SendInput struct layout")
def test_input_struct_size_matches_win32_sendinput() -> None:
    # Regression: the INPUT union must be sized to its largest member so
    # sizeof(INPUT) equals the size SendInput validates cbSize against (40 on
    # x64, 28 on x86). With only KEYBDINPUT it was 32 and SendInput failed with
    # ERROR_INVALID_PARAMETER (87) — no keystroke was ever injected.
    assert ctypes.sizeof(cb.INPUT_UNION) == max(
        ctypes.sizeof(cb.MOUSEINPUT),
        ctypes.sizeof(cb.KEYBDINPUT),
        ctypes.sizeof(cb.HARDWAREINPUT),
    )
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(cb.INPUT) == expected


def test_routing_uses_captured_target_pid_not_stolen_foreground(monkeypatch) -> None:
    clipboard = RecordingInjector()
    keystroke = RecordingInjector()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        per_app={"notepad.exe": "keystroke"},
        process_name_fn=lambda: "Cursor.exe",
    )
    monkeypatch.setattr(
        "dcent_voice.inject.router.process_name_from_pid",
        lambda process_id: "notepad.exe" if process_id == 4242 else None,
    )
    target = type("T", (), {"process_id": 4242, "supports_edit_messages": False})()
    decision = router.resolve_decision("hello", target, bind_process_to_target=True)
    assert decision.process_name == "notepad.exe"
    assert decision.resolved_injector == "keystroke"


def test_routing_injector_uses_per_app_override() -> None:
    clipboard = RecordingInjector()
    keystroke = RecordingInjector()
    injector = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        per_app={"WindowsTerminal.exe": "keystroke"},
        process_name_fn=lambda: "WindowsTerminal.exe",
    )

    injector.inject("hello")

    assert clipboard.values == []
    assert keystroke.values == ["hello"]


def test_short_text_uses_keystroke_unless_per_app_overrides() -> None:
    clipboard = RecordingInjector()
    keystroke = RecordingInjector()
    injector = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "notepad.exe",
        short_text_keystroke_chars=48,
    )
    injector.inject("Hello world")
    assert keystroke.values == ["Hello world"]
    assert clipboard.values == []

    long_text = "x" * 80
    injector.inject(long_text)
    assert clipboard.values == [long_text]


def test_press_time_native_edit_keeps_newlines_on_keystroke_replace(monkeypatch) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Windows")

    class TargetedRecordingInjector(RecordingInjector):
        def inject_targeted(self, text: str, target: object) -> None:
            self.values.append(text)

    clipboard = TargetedRecordingInjector()
    keystroke = TargetedRecordingInjector()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "notepad.exe",
        short_text_keystroke_chars=48,
    )
    target = type("T", (), {"supports_edit_messages": True, "process_id": 7})()
    decision = router.resolve_decision(
        "Hello\n\nWorld",
        target,
        bind_process_to_target=True,
    )
    assert decision.resolved_injector == "keystroke"
    assert decision.delivery == "native_replace"


@pytest.mark.parametrize(
    "error",
    [
        cb.ClipboardOpenTimeout(
            stage="snapshot",
            timeout_s=2.0,
            attempts=40,
            elapsed_s=2.0,
            last_error=cb.ERROR_ACCESS_DENIED,
        ),
        cb.ClipboardOpenTimeout(
            stage="set",
            timeout_s=2.0,
            attempts=40,
            elapsed_s=2.0,
            last_error=cb.ERROR_ACCESS_DENIED,
        ),
        cb.ClipboardPreservationError("non-cloneable clipboard"),
    ],
)
def test_native_edit_falls_back_after_safe_clipboard_failure(monkeypatch, error) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Windows")

    class FailingClipboard(RecordingInjector):
        def inject_targeted(self, text: str, target: object) -> None:
            raise error

    class NativeKeystroke(RecordingInjector):
        def inject_targeted(self, text: str, target: object) -> None:
            self.values.append(text)

    clipboard = FailingClipboard()
    keystroke = NativeKeystroke()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "notepad.exe",
        short_text_keystroke_chars=48,
    )
    target = type("T", (), {"supports_edit_messages": True, "process_id": 7})()
    decision = router.inject_into_target_with_decision("x" * 80, target)
    assert keystroke.values == ["x" * 80]
    assert decision.configured_injector == "clipboard"
    assert decision.resolved_injector == "keystroke"
    assert decision.delivery == "native_replace"


def test_native_edit_never_falls_back_after_clipboard_restore_failure(monkeypatch) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Windows")
    error = cb.ClipboardOpenTimeout(
        stage="restore",
        timeout_s=3.0,
        attempts=60,
        elapsed_s=3.0,
        last_error=cb.ERROR_ACCESS_DENIED,
    )

    class FailingClipboard(RecordingInjector):
        def inject_targeted(self, text: str, target: object) -> None:
            raise error

    class NativeKeystroke(RecordingInjector):
        def inject_targeted(self, text: str, target: object) -> None:
            self.values.append(text)

    keystroke = NativeKeystroke()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": FailingClipboard(), "keystroke": keystroke},
        process_name_fn=lambda: "notepad.exe",
    )
    target = type("T", (), {"supports_edit_messages": True, "process_id": 7})()
    with pytest.raises(cb.ClipboardOpenTimeout) as raised:
        router.inject_into_target_with_decision("x" * 80, target)
    assert raised.value.stage == "restore"
    assert keystroke.values == []


@pytest.mark.parametrize("text", ["line one\nline two", "A\tB\r\nC", "control:\x01"])
@requires_win32_native
def test_windows_short_route_sends_nonexact_controls_to_clipboard(monkeypatch, text) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Windows")
    clipboard = RecordingInjector()
    keystroke = RecordingInjector()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "Editor.exe",
        short_text_keystroke_chars=48,
    )
    router.inject(text)
    assert clipboard.values == [text]
    assert keystroke.values == []


@requires_win32_native
def test_windows_short_route_keeps_emoji_zwj_and_combining_on_unicode_keystrokes(
    monkeypatch,
) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Windows")
    clipboard = RecordingInjector()
    keystroke = RecordingInjector()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "Editor.exe",
        short_text_keystroke_chars=48,
    )
    text = "日本語 👨‍👩‍👧‍👦 e\u0301"
    router.inject(text)
    assert keystroke.values == [text]
    assert clipboard.values == []


def test_routing_injector_falls_back_to_default() -> None:
    clipboard = RecordingInjector()
    keystroke = RecordingInjector()
    injector = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        per_app={"WindowsTerminal.exe": "keystroke"},
        process_name_fn=lambda: "notepad.exe",
    )

    injector.inject("hello")

    assert clipboard.values == ["hello"]
    assert keystroke.values == []


def test_base_retract_raises_instead_of_silent_noop() -> None:
    class Bare(Injector):
        def inject(self, text: str) -> None:
            pass

    with pytest.raises(RetractUnsupported):
        Bare().retract(5)


def test_routing_retract_falls_back_when_clipboard_cannot_retract() -> None:
    """Shipped default path: clipboard default + keystroke fallback.

    macOS/Linux used to inherit a silent base no-op retract; polish rewrites
    then double-pasted (hello world + Hello world.). Router must fall back.
    """
    clipboard = NoRetractClipboard()
    keystroke = RecordingInjector()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "SomeApp.exe",
    )

    router.inject("hello world")
    assert clipboard.buffer == "hello world"
    assert keystroke.buffer == ""

    # Polish-style divergence: full replace needs retract then re-inject.
    router.retract(len("hello world"))
    assert keystroke.retracted == [len("hello world")]
    assert keystroke.buffer == ""
    router.inject("Hello world.")
    # Clipboard still receives the polished inject (default route).
    assert clipboard.values[-1] == "Hello world."


def test_routing_retract_without_keystroke_fallback_raises() -> None:
    clipboard = NoRetractClipboard()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard},
        process_name_fn=lambda: "app",
    )
    with pytest.raises(RetractUnsupported):
        router.retract(3)


def test_pipeline_reconcile_uses_router_keystroke_fallback_for_polish(
    fake_asr,
) -> None:
    """End-to-end reconcile on the real RoutingInjector path (no FakeInjector)."""
    from dcent_voice.pipeline import PipelineWorker

    clipboard = NoRetractClipboard()
    keystroke = RecordingInjector()
    router = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "Editor.exe",
    )
    worker = PipelineWorker(
        bus=__import__("dcent_voice.events", fromlist=["EventBus"]).EventBus(),
        capture=type(
            "C",
            (),
            {
                "begin_utterance": lambda self: None,
                "end_utterance": lambda self: __import__("numpy").ones(1600),
                "peek_utterance": lambda self: __import__("numpy").ones(1600),
                "stop": lambda self: None,
            },
        )(),
        asr=fake_asr,
        injector=router,
    )
    worker._stream_injected = True
    worker._stream_injected_text = "hello world"
    clipboard.inject("hello world")  # simulate prior stream paste
    # Divergent final text (local polish capitalization + period).
    worker._reconcile_stream_injection("Hello world.")
    assert keystroke.retracted == [len("hello world")]
    assert clipboard.values[-1] == "Hello world."
    assert worker._stream_injected_text == "Hello world."


def test_foreground_process_routes_to_macos_adapter(monkeypatch) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "dcent_voice.inject.router._macos_foreground_process_name",
        lambda: "Example.app",
    )

    assert get_foreground_process_name() == "Example.app"


def test_foreground_process_routes_to_linux_adapter(monkeypatch) -> None:
    monkeypatch.setattr("dcent_voice.inject.router.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "dcent_voice.inject.router._linux_foreground_process_name",
        lambda: "code",
    )

    assert get_foreground_process_name() == "code"


def test_checked_keystroke_restores_stolen_press_time_window(monkeypatch) -> None:
    from dcent_voice.inject.keystroke import WindowsSendInputInjector
    from dcent_voice.inject.windows_focus import WindowsFocusTarget

    restored: list[int] = []
    sent: list[str] = []
    monkeypatch.setattr(
        "dcent_voice.inject.windows_focus.focus_is_unchanged",
        lambda _target: False,
    )
    monkeypatch.setattr(
        "dcent_voice.inject.windows_focus.restore_foreground",
        lambda hwnd: restored.append(int(hwnd)) or True,
    )
    monkeypatch.setattr(
        "dcent_voice.inject.windows_focus.require_focus_unchanged",
        lambda _target: None,
    )
    injector = WindowsSendInputInjector()
    monkeypatch.setattr(injector, "inject", lambda text: sent.append(text))
    target = WindowsFocusTarget(
        top_hwnd=4242,
        focus_hwnd=4242,
        thread_id=7,
        class_name="Chrome_WidgetWin_1",
        process_id=99,
    )
    injector.inject_checked("hello extra-app", target)
    assert restored == [4242]
    assert sent == ["hello extra-app"]


def test_routing_press_enter_uses_active_injector() -> None:
    clipboard = RecordingInjector()
    keystroke = RecordingInjector()
    injector = RoutingInjector(
        default_name="clipboard",
        injectors={"clipboard": clipboard, "keystroke": keystroke},
        process_name_fn=lambda: "notepad.exe",
    )
    injector.press_enter()
    assert clipboard.enters == 1
    assert keystroke.enters == 0
