# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from dcent_voice.config import SnippetEntry, VocabEntry
from dcent_voice.events import (
    AppMode,
    EventBus,
    HotkeyPressed,
    HotkeyReleased,
    StateChanged,
    TranscriptReady,
    WakeWordDetected,
)
from dcent_voice.personalization import PersonalizationStore
from dcent_voice.pipeline import (
    IncrementalCommitter,
    PipelineConfig,
    PipelineWorker,
    _apply_personalization,
    _discard_message,
    _overlay_cleanup_label,
    _overlay_style_label,
    apply_dictionary,
    build_hotwords,
    build_initial_prompt,
    stream_pass_wait_s,
)
from dcent_voice.state import AppState
from tests.win32_native import requires_win32_native


def _cfg(**kwargs) -> PipelineConfig:
    """Pipeline config for unit tests that assert raw ASR passthrough.

    Offline polish is on by default in production; tests that check exact
    transcript strings disable it so they isolate capture/ASR/inject behavior.
    """
    kwargs.setdefault("focus_guard_enabled", False)
    kwargs.setdefault("local_polish", False)
    kwargs.setdefault("spoken_edits", False)
    kwargs.setdefault("developer_terms", False)
    return PipelineConfig(**kwargs)


@pytest.fixture(autouse=True)
def _stable_pipeline_foreground(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold-to-talk unit tests expect plain passthrough unless they override foreground."""
    monkeypatch.setattr("dcent_voice.pipeline._foreground_process_name", lambda: "")
    monkeypatch.setattr("dcent_voice.pipeline.window_title", lambda _hwnd: "")


@pytest.mark.parametrize("invalid", ["false", "true", 1, 0, None, [], {}])
def test_pipeline_config_rejects_non_boolean_prose_context(invalid: object) -> None:
    with pytest.raises(TypeError, match="personalization_prose_context"):
        PipelineConfig(personalization_prose_context=invalid)  # type: ignore[arg-type]


def test_pipeline_use_site_rejects_mutated_context_before_custom_store() -> None:
    class TruthyStore:
        def __init__(self) -> None:
            self.calls = 0

        def apply(self, text: str, **kwargs) -> str:
            self.calls += 1
            return "rewritten" if kwargs["prose_context"] else text

    store = TruthyStore()
    config = PipelineConfig(personalization=store)
    config.personalization_prose_context = "false"  # type: ignore[assignment]

    result = _apply_personalization(
        config,
        "Open d central settings.",
        style="plain",
        app="notes.exe",
    )

    assert result == "Open d central settings."
    assert store.calls == 0


class FakeCapture:
    samplerate = 16000

    def __init__(self, audio) -> None:
        self.audio = audio
        self.started = False
        self.stopped = False

    def begin_utterance(self) -> None:
        self.started = True

    def end_utterance(self):
        return self.audio

    def peek_utterance(self):
        return self.audio

    def stop(self) -> None:
        self.stopped = True


class SequenceASR:
    """ASR whose text grows across passes, like real incremental streaming."""

    locality = None

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls = 0

    def load(self) -> None:  # pragma: no cover - trivial
        pass

    def unload(self) -> None:  # pragma: no cover - trivial
        pass

    def transcribe(
        self,
        audio,
        samplerate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ):
        from dcent_voice.asr.base import TranscriptResult

        index = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        return TranscriptResult(
            text=self.texts[index], language="en", duration_s=1.0, asr_latency_s=0.01
        )


def test_pipeline_transcribes_and_injects(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    recovered: list[tuple[str, str, str]] = []
    recovery = type(
        "Recovery",
        (),
        {"record": lambda _self, text, *, reason, mode: recovered.append((text, reason, mode))},
    )()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(),
        recovery_store=recovery,
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == ["hello world"]
    assert ready[0].raw == "hello world"
    assert ready[0].injected is True
    assert recovered == []
    worker.stop()
    bus.stop()


def test_pipeline_command_removes_bullets_from_press_time_selection(
    fake_asr, fake_injector
) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    fake_asr.text = "remove bullets"

    class TargetedInjector(type(fake_injector)):
        def __init__(self) -> None:
            super().__init__()
            self.targeted: list[tuple[str, object]] = []

        def inject_into_target(self, text: str, target: object) -> None:
            self.targeted.append((text, target))

    injector = TargetedInjector()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=injector,
        config=_cfg(),
        selection_getter=lambda: "- milk\n- eggs",
    )
    worker.start()

    target = object()
    bus.publish(HotkeyPressed(AppMode.COMMAND, focus_target=target))
    bus.publish(HotkeyReleased(AppMode.COMMAND))

    assert done.wait(1.0)
    assert injector.targeted == [("milk eggs", target)]
    assert injector.injected == []
    assert ready[0].injected is True
    assert ready[0].reason == "rules_unbullet"
    worker.stop()
    bus.stop()


def test_pipeline_command_stashes_transformed_result_when_target_injection_fails(
    fake_asr, fake_injector
) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    fake_asr.text = "remove bullets"

    class FailingTargetedInjector(type(fake_injector)):
        def inject_into_target(self, text: str, target: object) -> None:
            raise RuntimeError("target rejected injection")

    recovered: list[tuple[str, str, str]] = []
    recovery = type(
        "Recovery",
        (),
        {"record": lambda _self, text, *, reason, mode: recovered.append((text, reason, mode))},
    )()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=FailingTargetedInjector(),
        config=_cfg(),
        selection_getter=lambda: "- milk\n- eggs",
        recovery_store=recovery,
    )
    stashed: list[str] = []
    worker._stash_transcript = stashed.append  # type: ignore[method-assign]
    worker.start()

    bus.publish(HotkeyPressed(AppMode.COMMAND, focus_target=object()))
    bus.publish(HotkeyReleased(AppMode.COMMAND))

    assert done.wait(1.0)
    assert stashed == ["milk eggs"]
    assert ready[0].injected is False
    assert ready[0].reason == "error:RuntimeError"
    assert recovered == [("milk eggs", "error:RuntimeError", "command")]
    worker.stop()
    bus.stop()


def test_cancel_dead_default_failover_ignores_unstarted_thread(fake_asr, fake_injector) -> None:
    bus = EventBus()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(),
    )
    worker._failover_thread = threading.Thread(target=lambda: None)
    worker._cancel_dead_default_failover()
    worker._failover_thread = None
    bus.stop()


def test_pipeline_uses_target_captured_in_hotkey_callback(fake_asr, fake_injector) -> None:
    bus = EventBus()
    capture = FakeCapture(np.ones(16000, dtype=np.float32) * 0.1)
    worker = PipelineWorker(
        bus=bus,
        capture=capture,
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(),
    )
    callback_target = object()
    bus.start()
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=callback_target))
    deadline = time.monotonic() + 1.0
    while not capture.started and time.monotonic() < deadline:
        time.sleep(0.01)

    assert capture.started is True
    assert worker._focus_target_at_press is callback_target
    worker.stop()
    bus.stop()


def test_pipeline_injects_into_press_target_after_focus_steal(
    fake_asr, fake_injector, monkeypatch
) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()

    class TargetedInjector(type(fake_injector)):
        def __init__(self) -> None:
            super().__init__()
            self.targeted: list[tuple[str, object]] = []

        def inject_into_target(self, text: str, target: object) -> None:
            self.targeted.append((text, target))

    injector = TargetedInjector()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=injector,
        config=_cfg(focus_guard_enabled=True),
    )
    worker.start()
    monkeypatch.setattr("dcent_voice.pipeline.focus_changed", lambda _hwnd: True)
    monkeypatch.setattr("dcent_voice.pipeline.restore_foreground", lambda _hwnd: False)
    monkeypatch.setattr("dcent_voice.pipeline.window_title", lambda _hwnd: "Cursor")

    target = type("T", (), {"top_hwnd": 4242, "process_id": 99})()
    bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=target))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert injector.targeted == [("hello world", target)]
    assert injector.injected == []
    assert ready[0].injected is True
    worker.stop()
    bus.stop()


@pytest.mark.parametrize("capture_target", [False, True])
def test_pipeline_focus_loss_never_falls_back_to_global_injection(
    fake_asr, fake_injector, monkeypatch, capture_target
) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    notices: list[str] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(focus_guard_enabled=True),
        notify=lambda _title, body: notices.append(body),
    )
    # Simulate clipboard unavailability without touching the host clipboard.
    worker._stash_transcript = lambda _text: False  # type: ignore[method-assign]
    monkeypatch.setattr("dcent_voice.pipeline._capture_windows_focus_target", lambda: None)
    monkeypatch.setattr("dcent_voice.pipeline.focus_changed", lambda _hwnd: True)
    monkeypatch.setattr("dcent_voice.pipeline.restore_foreground", lambda _hwnd: False)
    monkeypatch.setattr("dcent_voice.pipeline.window_title", lambda _hwnd: "focus stealer")
    worker.start()

    target = object() if capture_target else None
    bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=target))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == []
    assert fake_injector.enters == 0
    assert ready[0].injected is False
    assert ready[0].reason == "focus_changed"
    assert notices and all("copied" not in notice.casefold() for notice in notices)
    worker.stop()
    bus.stop()


def test_pipeline_targeted_text_never_falls_back_to_global_enter_after_focus_loss(
    fake_asr, fake_injector, monkeypatch
) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    fake_asr.text = "hello world press enter"

    class TextOnlyTargetedInjector(type(fake_injector)):
        def __init__(self) -> None:
            super().__init__()
            self.targeted: list[tuple[str, object]] = []

        def inject_into_target(self, text: str, target: object) -> None:
            self.targeted.append((text, target))

    injector = TextOnlyTargetedInjector()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=injector,
        config=_cfg(focus_guard_enabled=True),
    )
    monkeypatch.setattr("dcent_voice.pipeline.focus_changed", lambda _hwnd: True)
    monkeypatch.setattr("dcent_voice.pipeline.restore_foreground", lambda _hwnd: False)
    monkeypatch.setattr("dcent_voice.pipeline.window_title", lambda _hwnd: "focus stealer")
    worker.start()

    target = object()
    bus.publish(HotkeyPressed(AppMode.DICTATION, focus_target=target))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert injector.targeted == [("hello world", target)]
    assert injector.injected == []
    assert injector.enters == 0
    assert ready[0].injected is True
    assert ready[0].reason == "focus_changed"
    worker.stop()
    bus.stop()


@requires_win32_native
def test_pipeline_refocuses_page_field_when_window_stayed_foreground(
    fake_asr, fake_injector, monkeypatch
) -> None:
    calls: list[int | None] = []
    monkeypatch.setattr("dcent_voice.pipeline.focus_changed", lambda _hwnd: False)
    monkeypatch.setattr(
        "dcent_voice.inject.windows_uia.refocus_page_field",
        lambda hwnd=None, timeout_s=1.2, steal_recovered=False: calls.append(hwnd) or True,
    )
    worker = PipelineWorker(
        bus=EventBus(),
        capture=FakeCapture(np.ones(1600, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(focus_guard_enabled=True),
    )
    worker._foreground_hwnd = 4242
    worker._foreground_process = "msedge.exe"
    assert worker._ensure_target_foreground(True) is True
    assert calls == [4242]


def test_pipeline_does_not_refocus_page_field_in_vscode(
    fake_asr, fake_injector, monkeypatch
) -> None:
    calls: list[int | None] = []
    monkeypatch.setattr("dcent_voice.pipeline.focus_changed", lambda _hwnd: False)
    monkeypatch.setattr(
        "dcent_voice.inject.windows_uia.refocus_page_field",
        lambda hwnd=None, timeout_s=1.2, steal_recovered=False: calls.append(hwnd) or True,
    )
    worker = PipelineWorker(
        bus=EventBus(),
        capture=FakeCapture(np.ones(1600, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(focus_guard_enabled=True),
    )
    worker._foreground_hwnd = 4242
    worker._foreground_process = "Code.exe"
    assert worker._ensure_target_foreground(True) is True
    assert calls == []


def test_desktop_pipeline_captures_destination_for_scoped_personalization(
    fake_asr, fake_injector, monkeypatch, tmp_path: Path
) -> None:
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    store = PersonalizationStore(tmp_path / "personalization.json")
    for _ in range(2):
        store.record_correction("d central", "D-Central", style="plain", app="code.exe")
    monkeypatch.setattr("dcent_voice.pipeline._foreground_process_name", lambda: "Code.exe")
    fake_asr.text = "Open d central repository."
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(
            personalization=store,
            personalization_prose_context=True,
            style_default="plain",
            style_per_app={"Code.exe": "plain"},
        ),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == ["Open D-Central repository."]
    assert worker._foreground_process == ""
    worker.stop()
    bus.stop()


def test_pipeline_uses_learned_app_style_for_hold_release(
    tmp_path: Path, monkeypatch, fake_asr, fake_injector
) -> None:
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    store = PersonalizationStore(tmp_path / "personalization.json")
    store.remember_app_style("notepad.exe", "email", immediate=True)
    monkeypatch.setattr("dcent_voice.pipeline._foreground_process_name", lambda: "notepad.exe")
    fake_asr.text = "tell alex the invoice is ready"
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(personalization=store, style_default="plain"),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    injected = fake_injector.injected[0]
    assert injected.startswith("Hi Alex,")
    assert injected.rstrip().endswith("Thanks,")
    worker.stop()
    bus.stop()


def test_pipeline_hold_release_writing_style_keeps_learned_written_forms(
    tmp_path: Path, fake_asr, fake_injector
) -> None:
    from dcent_voice.dictation.style import apply_style

    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    store = PersonalizationStore(tmp_path / "personalization.json", enabled=True, learn=True)
    term = store.record_correction("vip", "I'm Ada", source="typed")
    assert term is not None
    fake_asr.text = "vip"
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(
            personalization=store,
            dictionary=(),
            snippets=(),
            style_default="formal",
        ),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(1.0)
    assert fake_injector.injected
    injected = fake_injector.injected[0]
    assert "I'm Ada" in injected
    assert "I am Ada" not in injected
    assert apply_style("I'm Ada", "formal") == "I am Ada"
    worker.stop()
    bus.stop()


def test_pipeline_discards_too_short_audio(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(100, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == []
    assert ready[0].discarded is True
    assert ready[0].reason == "too_short"
    worker.stop()
    bus.stop()


class FakeOverlay:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_state(self, state: str) -> None:
        self.calls.append(("set_state", state))

    def set_message(self, message: str) -> None:
        self.calls.append(("set_message", message))

    def hide(self) -> None:
        self.calls.append(("hide",))

    def hide_later(self, delay_s: float) -> None:
        self.calls.append(("hide_later", delay_s))

    def show(self) -> None:
        self.calls.append(("show",))


class UnloadedASR:
    locality = None

    def __init__(self) -> None:
        self.loaded = False
        self.load_calls = 0

    def is_loaded(self) -> bool:
        return self.loaded

    def load(self) -> None:
        self.load_calls += 1
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False

    def transcribe(self, audio, samplerate: int = 16000, **_kwargs):
        from dcent_voice.asr.base import TranscriptResult

        return TranscriptResult(
            text="hello world", language="en", duration_s=1.0, asr_latency_s=0.01
        )


class GatedColdASR(UnloadedASR):
    def __init__(self, blocked_stage: str) -> None:
        super().__init__()
        self.blocked_stage = blocked_stage
        self.entered = threading.Event()
        self.release = threading.Event()

    def load(self) -> None:
        self.load_calls += 1
        if self.blocked_stage == "load":
            self.entered.set()
            assert self.release.wait(3.0)
        self.loaded = True

    def transcribe(self, audio, samplerate: int = 16000, **_kwargs):
        from dcent_voice.asr.base import TranscriptResult

        if self.blocked_stage == "transcribe":
            self.entered.set()
            assert self.release.wait(3.0)
        return TranscriptResult(
            text="POST STOP TEXT.", language="en", duration_s=1.0, asr_latency_s=0.01
        )


def test_cold_model_load_waits_until_capture_ends(fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    overlay = FakeOverlay()
    asr = UnloadedASR()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    deadline = time.monotonic() + 1.0
    while not worker.capture.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ("set_state", "loading") in overlay.calls
    assert ("set_message", "Loading model…") in overlay.calls
    assert asr.load_calls == 0

    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert asr.load_calls == 1
    worker.stop()
    bus.stop()


def test_cold_streaming_defers_model_load_and_partials_until_capture_ends(
    fake_injector,
) -> None:
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    asr = UnloadedASR()
    capture = FakeCapture(np.ones(16000, dtype=np.float32) * 0.1)
    worker = PipelineWorker(
        bus=bus,
        capture=capture,
        asr=asr,
        injector=fake_injector,
        config=_cfg(),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.STREAMING))
    deadline = time.monotonic() + 1.0
    while not capture.started and time.monotonic() < deadline:
        time.sleep(0.02)

    assert capture.started is True
    assert asr.load_calls == 0
    assert worker._stream_thread is None

    bus.publish(HotkeyReleased(AppMode.STREAMING))

    assert done.wait(1.0)
    assert asr.load_calls == 1
    worker.stop()
    bus.stop()


@pytest.mark.parametrize("mode", [AppMode.DICTATION, AppMode.STREAMING])
@pytest.mark.parametrize("blocked_stage", ["load", "transcribe"])
def test_stop_terminally_fences_cold_finalize_effects(
    mode: AppMode,
    blocked_stage: str,
    fake_injector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    observed_events: list[object] = []
    processing_seen = threading.Event()

    def observe(event: object) -> None:
        observed_events.append(event)
        if isinstance(event, StateChanged) and event.state == "processing":
            processing_seen.set()

    bus.subscribe(observe)
    bus.start()
    overlay = FakeOverlay()
    notifications: list[tuple[str, str]] = []
    clipboard: list[str] = []
    monkeypatch.setattr(
        "dcent_voice.inject.clipboard.set_clipboard_text",
        lambda text: clipboard.append(text),
    )
    asr = GatedColdASR(blocked_stage)
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=asr,
        injector=fake_injector,
        overlay=overlay,
        notify=lambda title, body: notifications.append((title, body)),
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(mode))
    bus.publish(HotkeyReleased(mode))

    assert asr.entered.wait(1.0)
    assert processing_seen.wait(1.0)
    worker.stop(timeout=0.05)

    assert worker._thread.is_alive() is True
    assert worker.state.state is AppState.IDLE
    effects_at_return = (
        list(fake_injector.injected),
        list(fake_injector.retracted),
        list(overlay.calls),
        list(notifications),
        list(observed_events),
        list(clipboard),
    )

    asr.release.set()
    worker._thread.join(timeout=1.0)
    time.sleep(0.02)

    assert worker._thread.is_alive() is False
    assert worker.state.state is AppState.IDLE
    assert (
        list(fake_injector.injected),
        list(fake_injector.retracted),
        list(overlay.calls),
        list(notifications),
        list(observed_events),
        list(clipboard),
    ) == effects_at_return
    assert fake_injector.injected == []
    assert clipboard == []
    assert not any(isinstance(event, TranscriptReady) for event in observed_events)
    bus.stop()


def test_stop_during_cleanup_never_injects_or_publishes_after_return(
    fake_asr,
    fake_injector,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class GatedCleanup:
        def clean(self, text: str, *, style: str) -> str:
            del text, style
            entered.set()
            assert release.wait(3.0)
            return "POST STOP CLEANUP."

    bus = EventBus()
    ready: list[TranscriptReady] = []
    bus.subscribe(lambda event: ready.append(event) if isinstance(event, TranscriptReady) else None)
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        cleanup=GatedCleanup(),
        config=_cfg(cleanup_enabled=True),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert entered.wait(1.0)
    worker.stop(timeout=0.05)
    assert worker._thread.is_alive() is True
    release.set()
    worker._thread.join(timeout=1.0)

    assert worker._thread.is_alive() is False
    assert worker.state.state is AppState.IDLE
    assert fake_injector.injected == []
    assert ready == []
    bus.stop()


def test_discard_message_teaches_common_reasons() -> None:
    assert _discard_message("silence") == "No speech detected"
    assert _discard_message("no_audio") == "No audio from selected microphone"
    assert _discard_message("too_short") == "Too short"
    assert _discard_message("asr_empty") == "No speech recognized"
    assert _discard_message("edited_empty") == "Cleared — scratch that"
    assert _discard_message("nothing_to_undo") == "Nothing to undo"
    assert _discard_message("asr_hallucination") == "Could not understand speech"
    assert _discard_message("asr_hint_echo") == "Could not understand speech"
    assert _discard_message("unknown") == "Discarded"


def test_wake_word_starts_dictation(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(max_utterance_s=0.2, wake_end_silence_s=0.0),
    )
    worker.start()
    bus.publish(WakeWordDetected("hey-dcent"))
    # Auto-stop ends the wake session (silence watch disabled for the test).
    assert done.wait(2.0)
    assert fake_injector.injected == ["hello world"]
    assert ready[0].injected is True
    worker.stop()
    bus.stop()


def test_pipeline_discards_edited_empty_with_friendly_overlay(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    overlay = FakeOverlay()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=type(fake_asr)("this was a mistake scratch that"),
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(spoken_edits=True),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == []
    assert ready[0].discarded is True
    assert ready[0].reason == "edited_empty"
    assert ("set_state", "discarded") in overlay.calls
    assert ("set_message", "Cleared — scratch that") in overlay.calls
    worker.stop()
    bus.stop()


def test_pipeline_discards_silence_with_overlay_feedback(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    overlay = FakeOverlay()
    notices: list[tuple[str, str]] = []
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.zeros(16000, dtype=np.float32)),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        notify=lambda title, body: notices.append((title, body)),
        config=_cfg(),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == []
    assert ready[0].discarded is True
    assert ready[0].reason == "silence"
    assert ("set_state", "discarded") in overlay.calls
    assert any(call[0] == "set_message" for call in overlay.calls)
    assert any(call[0] == "hide_later" for call in overlay.calls)
    assert notices  # tray-style notify fired
    worker.stop()
    bus.stop()


class DeadDefaultCapture(FakeCapture):
    def status_snapshot(self):
        return {
            "default_was_dead": True,
            "resolved_name": "Microphone (Arctis 5 Chat)",
        }


def test_pipeline_names_dead_default_microphone(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    overlay = FakeOverlay()
    notices: list[tuple[str, str]] = []
    worker = PipelineWorker(
        bus=bus,
        capture=DeadDefaultCapture(np.zeros(16000, dtype=np.float32)),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        notify=lambda title, body: notices.append((title, body)),
        config=_cfg(),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == []
    assert ready[0].discarded is True
    assert ready[0].reason == "no_audio"
    assert any(
        call == ("set_message", "No audio from Microphone (Arctis 5 Chat)")
        for call in overlay.calls
    )
    assert notices == [("DCENT_Voice", "No audio from Microphone (Arctis 5 Chat)")]
    worker.stop()
    bus.stop()


def test_pipeline_status_snapshot_alive(fake_asr, fake_injector) -> None:
    bus = EventBus()
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(),
    )
    worker.start()
    snap = worker.status_snapshot()
    assert snap["alive"] is True
    assert snap["ok"] is True
    worker.stop()
    bus.stop()


def test_build_initial_prompt_is_natural_written_forms() -> None:
    prompt = build_initial_prompt(
        (
            VocabEntry(spoken="d central", written="D-Central"),
            VocabEntry(spoken="d cent voice", written="DCENT_Voice"),
        )
    )
    assert prompt is not None
    assert "->" not in prompt
    assert "Custom vocabulary" not in prompt
    assert "D-Central" in prompt
    assert "DCENT_Voice" in prompt
    assert build_hotwords((VocabEntry(spoken="d central", written="D-Central"),))


def test_starred_dictionary_terms_lead_asr_hints() -> None:
    prompt = build_initial_prompt(
        (
            VocabEntry(spoken="plain cue", written="PlainTerm"),
            VocabEntry(spoken="star cue", written="StarredTerm", starred=True),
        )
    )
    assert prompt is not None
    assert prompt.index("StarredTerm") < prompt.index("PlainTerm")
    crowded = tuple(
        VocabEntry(spoken=f"plain {index}", written=f"Plain{index}") for index in range(30)
    ) + (VocabEntry(spoken="vip", written="VIPTerm", starred=True),)
    capped = build_initial_prompt(crowded)
    assert capped is not None
    assert capped.startswith("VIPTerm")
    assert "Plain29" not in capped
    hot = build_hotwords(
        (
            VocabEntry(spoken="plain cue", written="PlainTerm"),
            VocabEntry(spoken="star cue", written="StarredTerm", starred=True),
        )
    )
    assert hot is not None
    assert hot.index("StarredTerm") < hot.index("PlainTerm")


def test_starred_snippet_cues_lead_asr_hints() -> None:
    hot = build_hotwords(
        (VocabEntry(spoken="plain cue", written="PlainTerm"),),
        (
            SnippetEntry(spoken="my email", expansion="plain@example.com"),
            SnippetEntry(spoken="vip cue", expansion="ops@example.com", starred=True),
        ),
    )
    assert hot is not None
    assert hot.index("vip cue") < hot.index("PlainTerm")
    assert hot.index("vip cue") < hot.index("my email")
    assert "ops@example.com" not in hot
    prompt = build_initial_prompt(
        (VocabEntry(spoken="plain cue", written="PlainTerm"),),
        (SnippetEntry(spoken="vip cue", expansion="ops@example.com", starred=True),),
    )
    assert prompt is not None
    assert prompt.index("vip cue") < prompt.index("PlainTerm")
    assert "ops@example.com" not in prompt
    crowded = tuple(
        VocabEntry(spoken=f"plain {index}", written=f"Plain{index}") for index in range(30)
    )
    capped = build_initial_prompt(
        crowded,
        (SnippetEntry(spoken="vip cue", expansion="x", starred=True),),
    )
    assert capped is not None
    assert capped.startswith("vip cue")
    assert "Plain29" not in capped


def test_apply_dictionary_starred_wins_when_spoken_conflicts() -> None:
    text = apply_dictionary(
        "foo",
        (
            VocabEntry(spoken="foo", written="plain"),
            VocabEntry(spoken="foo", written="starred", starred=True),
        ),
    )
    assert text == "starred"


def test_overlay_priority_chip_when_starred_term_is_spoken() -> None:
    from dcent_voice.pipeline import PipelineConfig, _overlay_priority_label

    empty = PipelineConfig()
    assert _overlay_priority_label(empty) == ""
    starred = PipelineConfig(dictionary=(VocabEntry(spoken="vip", written="VIP", starred=True),))
    assert _overlay_priority_label(starred) == ""
    assert _overlay_priority_label(starred, "hello world") == ""
    assert _overlay_priority_label(starred, "call vip now") == "VIP"
    assert _overlay_priority_label(starred, "call VIP now") == "VIP"
    assert _overlay_priority_label(starred, "viper") == ""
    plain = PipelineConfig(dictionary=(VocabEntry(spoken="vip", written="VIP"),))
    assert _overlay_priority_label(plain, "call vip now") == ""


def test_overlay_priority_chip_when_starred_snippet_is_spoken() -> None:
    from dcent_voice.pipeline import PipelineConfig, _overlay_priority_label

    starred = PipelineConfig(
        snippets=(SnippetEntry(spoken="my email", expansion="ops@example.com", starred=True),)
    )
    assert _overlay_priority_label(starred) == ""
    assert _overlay_priority_label(starred, "hello world") == ""
    assert _overlay_priority_label(starred, "send my email now") == "ops@example.com"
    assert _overlay_priority_label(starred, "ops@example.com") == "ops@example.com"
    assert _overlay_priority_label(starred, "my emails") == ""
    plain = PipelineConfig(snippets=(SnippetEntry(spoken="my email", expansion="ops@example.com"),))
    assert _overlay_priority_label(plain, "send my email now") == ""


def test_overlay_priority_chip_truncates_long_snippet_expansion() -> None:
    from dcent_voice.pipeline import PipelineConfig, _overlay_priority_label

    long_expansion = "https://example.com/" + ("path/" * 10)
    starred = PipelineConfig(
        snippets=(SnippetEntry(spoken="my link", expansion=long_expansion, starred=True),)
    )
    label = _overlay_priority_label(starred, "open my link please")
    assert label == f"{long_expansion[:23]}…"
    assert len(label) == 24


def test_apply_dictionary_replaces_spoken() -> None:
    text = apply_dictionary(
        "hello d central world",
        (VocabEntry(spoken="d central", written="D-Central"),),
    )
    assert text == "hello D-Central world"


def test_local_asr_hints_keep_learned_terms_off_cloud() -> None:
    from types import SimpleNamespace

    from dcent_voice.asr.base import Locality
    from dcent_voice.dictation.vocab import shipped_domain_vocab
    from dcent_voice.pipeline import merge_asr_hint_dictionary

    class _Local:
        locality = Locality.LOCAL

    class _Cloud:
        locality = Locality.CLOUD

    store = SimpleNamespace(
        as_vocab=lambda **_kwargs: (VocabEntry(spoken="private client", written="PrivateClient"),)
    )
    local = merge_asr_hint_dictionary(
        (),
        asr=_Local(),
        personalization=store,
        style="plain",
        app="notepad.exe",
    )
    written = {entry.written for entry in local}
    assert "PrivateClient" in written
    assert {entry.written for entry in shipped_domain_vocab()} <= written
    cloud = merge_asr_hint_dictionary(
        (VocabEntry(spoken="d central", written="D-Central"),),
        asr=_Cloud(),
        personalization=store,
    )
    assert [entry.written for entry in cloud] == ["D-Central"]


def test_apply_dictionary_does_not_replace_inside_larger_words() -> None:
    text = apply_dictionary(
        "centralized d central systems",
        (VocabEntry(spoken="d central", written="D-Central"),),
    )

    assert text == "centralized D-Central systems"


def test_pipeline_discards_asr_hallucination_without_recovery(fake_injector) -> None:
    class RejectingASR:
        locality = None

        def load(self) -> None:
            pass

        def unload(self) -> None:
            pass

        def transcribe(self, audio, samplerate=16000, initial_prompt=None, hotwords=None):
            from dcent_voice.asr.base import TranscriptResult

            return TranscriptResult(
                text="",
                language="en",
                duration_s=1.0,
                asr_latency_s=0.01,
                rejected_reason="asr_hallucination",
            )

    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    recovered: list[tuple[str, str, str]] = []
    recovery = type(
        "Recovery",
        (),
        {"record": lambda _self, text, *, reason, mode: recovered.append((text, reason, mode))},
    )()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=RejectingASR(),
        injector=fake_injector,
        config=_cfg(),
        recovery_store=recovery,
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(1.0)
    assert ready[0].discarded is True
    assert ready[0].reason == "asr_hallucination"
    assert fake_injector.injected == []
    assert recovered == []
    worker.stop()
    bus.stop()


def test_pipeline_auto_stops_long_hold(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(max_utterance_s=0.15),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    # No HotkeyReleased — auto-stop must finalize.
    assert done.wait(2.0)
    assert ready[0].injected is True
    assert ready[0].reason == "auto_stop"
    assert fake_injector.injected == ["hello world"]
    worker.stop()
    bus.stop()


def test_incremental_committer_requires_three_agreements() -> None:
    committer = IncrementalCommitter(agreement_passes=3)
    # Growing prefixes still need three-pass LCP (identical-repeat is the
    # only first-emit shortcut).
    assert committer.update("hello") == ""
    assert committer.update("hello world") == ""
    assert committer.update("hello world") == "hello"
    # Last three windows now share "hello world".
    assert committer.update("hello world friend") == "world"
    assert committer.update("hello world friend") == ""
    assert committer.update("hello world friend") == "friend"


def test_stream_pass_wait_uses_earlier_first_peek() -> None:
    shipped = PipelineConfig()
    assert stream_pass_wait_s(shipped, first=True) == pytest.approx(0.32)
    assert stream_pass_wait_s(shipped, first=False) == pytest.approx(0.45)
    fast = PipelineConfig(stream_interval_s=0.05, stream_first_peek_s=0.32)
    assert stream_pass_wait_s(fast, first=True) == pytest.approx(0.05)


def test_reconcile_matching_final_is_silent_noop(fake_injector) -> None:
    worker = PipelineWorker(
        bus=EventBus(),
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=SequenceASR(["hello world"]),
        injector=fake_injector,
        config=_cfg(),
    )
    worker._stream_injected = True
    worker._stream_injected_text = "hello world"
    fake_injector.inject("hello world")
    worker._reconcile_stream_injection("hello world")
    assert fake_injector.injected == ["hello world"]
    assert fake_injector.retracted == []
    assert fake_injector.buffer == "hello world"


def test_pipeline_streaming_lifecycle(fake_injector) -> None:
    # Press the streaming hotkey -> incremental deltas are typed as words
    # stabilize -> release finalizes the tail and returns the state to idle.
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    asr = SequenceASR(
        [
            "hello there",
            "hello there my friend",
            "hello there my friend",  # three-pass agreement commits prefix
            "hello there my friend from dcentral",
        ]
    )
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=asr,
        injector=fake_injector,
        config=_cfg(stream_interval_s=0.05),
    )
    worker.start()

    bus.publish(HotkeyPressed(AppMode.STREAMING))
    deadline = time.monotonic() + 3.0
    while not fake_injector.injected and time.monotonic() < deadline:
        time.sleep(0.02)  # let at least two streaming passes commit a delta
    bus.publish(HotkeyReleased(AppMode.STREAMING))

    assert done.wait(3.0)
    assert ready[0].mode is AppMode.STREAMING
    assert ready[0].injected is True
    # The concatenated deltas reconstruct the final transcript exactly.
    assert "".join(fake_injector.injected) == "hello there my friend from dcentral"
    assert worker.state.state.value == "idle"
    worker.stop()
    bus.stop()


def test_streaming_focus_failure_recovers_final_text_once(fake_injector, monkeypatch) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    recovered: list[tuple[str, str, str]] = []
    recovery = type(
        "Recovery",
        (),
        {"record": lambda _self, text, *, reason, mode: recovered.append((text, reason, mode))},
    )()
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=SequenceASR(["final private text press enter"]),
        injector=fake_injector,
        config=_cfg(stream_interval_s=10.0, focus_guard_enabled=True),
        recovery_store=recovery,
    )
    worker._stash_transcript = lambda _text: None  # type: ignore[method-assign]
    monkeypatch.setattr("dcent_voice.pipeline.focus_changed", lambda _hwnd: True)
    monkeypatch.setattr("dcent_voice.pipeline.restore_foreground", lambda _hwnd: False)
    monkeypatch.setattr("dcent_voice.pipeline.window_title", lambda _hwnd: "other app")
    worker.start()

    target = type("Target", (), {"top_hwnd": 4242})()
    bus.publish(HotkeyPressed(AppMode.STREAMING, focus_target=target))
    bus.publish(HotkeyReleased(AppMode.STREAMING))

    assert done.wait(2.0)
    assert ready[0].reason == "focus_changed"
    assert ready[0].injected is False
    assert recovered == [("final private text", "focus_changed", "streaming")]
    assert fake_injector.injected == []
    assert fake_injector.enters == 0
    worker.stop()
    bus.stop()


def test_streaming_enter_only_focus_failure_never_submits_to_stealer(
    fake_injector, monkeypatch
) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    recovered: list[tuple[str, str, str]] = []
    recovery = type(
        "Recovery",
        (),
        {"record": lambda _self, text, *, reason, mode: recovered.append((text, reason, mode))},
    )()
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=SequenceASR(["press enter"]),
        injector=fake_injector,
        config=_cfg(stream_interval_s=10.0, focus_guard_enabled=True),
        recovery_store=recovery,
    )
    monkeypatch.setattr("dcent_voice.pipeline.focus_changed", lambda _hwnd: True)
    monkeypatch.setattr("dcent_voice.pipeline.restore_foreground", lambda _hwnd: False)
    monkeypatch.setattr("dcent_voice.pipeline.window_title", lambda _hwnd: "other app")
    worker.start()

    target = type("Target", (), {"top_hwnd": 4242})()
    bus.publish(HotkeyPressed(AppMode.STREAMING, focus_target=target))
    bus.publish(HotkeyReleased(AppMode.STREAMING))

    assert done.wait(2.0)
    assert ready[0].reason == "focus_changed"
    assert ready[0].injected is False
    assert ready[0].discarded is True
    assert recovered == []
    assert fake_injector.injected == []
    assert fake_injector.enters == 0
    worker.stop()
    bus.stop()


def test_streaming_overlay_shows_partial_before_first_inject(fake_injector) -> None:
    bus = EventBus()
    overlay = FakeOverlay()
    asr = SequenceASR(["hello there"])
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(stream_interval_s=0.02, stream_first_peek_s=0.02),
    )
    bus.start()
    worker.start()
    bus.publish(HotkeyPressed(AppMode.STREAMING))
    deadline = time.monotonic() + 2.0
    while asr.calls < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    deadline = time.monotonic() + 1.0
    while ("set_message", "hello there") not in overlay.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ("set_message", "hello there") in overlay.calls
    assert fake_injector.injected == []
    bus.publish(HotkeyReleased(AppMode.STREAMING))
    worker.stop()
    bus.stop()


def test_streaming_finalize_retracts_on_scratch_that(fake_injector) -> None:
    """Spoken edits that clear the utterance must remove already-typed stream text."""
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    # Two identical passes now commit "hello world"; finalize ASR includes scratch that.
    asr = SequenceASR(
        [
            "hello world",
            "hello world",
            "hello world scratch that",
        ]
    )
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=asr,
        injector=fake_injector,
        config=_cfg(stream_interval_s=0.05, spoken_edits=True, local_polish=False),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.STREAMING))
    deadline = time.monotonic() + 3.0
    while not fake_injector.injected and time.monotonic() < deadline:
        time.sleep(0.02)
    bus.publish(HotkeyReleased(AppMode.STREAMING))
    assert done.wait(3.0)
    # Committed text was typed, then fully retracted on finalize.
    assert fake_injector.retracted
    assert sum(fake_injector.retracted) >= len("hello world")
    assert fake_injector.buffer == ""
    assert ready[0].mode is AppMode.STREAMING
    worker.stop()
    bus.stop()


def test_streaming_finalize_never_interleaves_injection() -> None:
    class BlockingInjector:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.injected: list[str] = []
            self.buffer = ""

        def inject(self, text: str) -> None:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.injected.append(text)
                self.buffer += text
                first = len(self.injected) == 1
            self.entered.set()
            if first:
                assert self.release.wait(2.0)
            with self.lock:
                self.active -= 1

        def retract(self, char_count: int) -> None:
            n = max(0, int(char_count))
            if n:
                self.buffer = self.buffer[:-n] if n <= len(self.buffer) else ""

    class TimedOutJoin:
        def join(self, timeout: float) -> None:
            assert timeout == 3.0

        def is_alive(self) -> bool:
            return True

    injector = BlockingInjector()
    worker = PipelineWorker(
        bus=EventBus(),
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=SequenceASR(["hello tail"]),
        injector=injector,
        config=_cfg(stream_interval_s=0.01),
    )
    worker._stream_stop = threading.Event()
    worker._stream_committer = IncrementalCommitter()
    worker._stream_committer.update("hello")
    straggler = threading.Thread(target=worker._streaming_loop)
    worker._stream_thread = TimedOutJoin()
    worker.state.press(AppMode.STREAMING)
    worker.state.release(AppMode.STREAMING)
    straggler.start()
    assert injector.entered.wait(1.0)

    finalizer = threading.Thread(target=worker._finish_streaming)
    finalizer.start()
    time.sleep(0.05)
    assert injector.max_active == 1
    assert injector.injected == ["hello"]

    injector.release.set()
    straggler.join(1.0)
    finalizer.join(1.0)

    assert not straggler.is_alive()
    assert not finalizer.is_alive()
    assert injector.max_active == 1
    assert injector.injected == ["hello", " tail"]


def test_post_stop_streaming_straggler_does_not_commit_or_inject() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingASR(SequenceASR):
        def transcribe(self, *args, **kwargs):
            entered.set()
            assert release.wait(1.0)
            return super().transcribe(*args, **kwargs)

    class RecordingInjector:
        def __init__(self) -> None:
            self.injected: list[str] = []

        def inject(self, text: str) -> None:
            self.injected.append(text)

    worker = PipelineWorker(
        bus=EventBus(),
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=BlockingASR(["hello tail"]),
        injector=RecordingInjector(),
        config=_cfg(stream_interval_s=0.01),
    )
    worker._stream_stop = threading.Event()
    worker._stream_committer = IncrementalCommitter()
    worker._stream_committer.update("hello")
    straggler = threading.Thread(target=worker._streaming_loop)
    straggler.start()
    assert entered.wait(1.0)

    worker._stream_stop.set()
    release.set()
    straggler.join(1.0)

    assert worker.injector.injected == []
    assert worker._stream_committer.finalize("hello tail") == "hello tail"


def test_pipeline_update_runtime_applies_next_utterance(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(),
    )
    worker.start()
    replacement = type(fake_asr)("new text")
    worker.update_runtime(asr=replacement, config=_cfg())

    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))

    assert done.wait(1.0)
    assert fake_injector.injected == ["new text"]
    worker.stop()
    bus.stop()


def test_overlay_live_state_names_each_mode() -> None:
    from dcent_voice.pipeline import _overlay_live_state

    assert _overlay_live_state(AppMode.DICTATION) == "listening"
    assert _overlay_live_state(AppMode.STREAMING) == "streaming"
    assert _overlay_live_state(AppMode.COMMAND) == "command"


def test_overlay_style_chip_follows_destination_app() -> None:
    cfg = PipelineConfig()
    assert _overlay_style_label(cfg, "outlook.exe", "Inbox — Outlook") == "Email"
    assert _overlay_style_label(cfg, "notion.exe", "Meeting notes") == "Notes"
    assert _overlay_style_label(cfg, "Code.exe", "pipeline.py — Visual Studio Code") == "Code"
    assert _overlay_style_label(cfg, "notepad.exe", "Untitled") == "Plain"
    assert _overlay_cleanup_label(cfg) == "Medium"
    assert _overlay_cleanup_label(PipelineConfig(cleanup_level="high")) == "High"
    assert _overlay_cleanup_label(PipelineConfig(cleanup_level="none")) == "None"


def test_press_sets_overlay_style_and_cleanup_chips(fake_asr, fake_injector, monkeypatch) -> None:
    class ChipOverlay(FakeOverlay):
        def set_language(self, label: str) -> None:
            self.calls.append(("set_language", label))

        def set_style(self, label: str) -> None:
            self.calls.append(("set_style", label))

        def set_cleanup(self, label: str) -> None:
            self.calls.append(("set_cleanup", label))

    overlay = ChipOverlay()
    monkeypatch.setattr("dcent_voice.pipeline._foreground_process_name", lambda: "outlook.exe")
    bus = EventBus()
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(cleanup_level="medium"),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    deadline = time.monotonic() + 1.0
    while ("set_style", "Email") not in overlay.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    worker.stop()
    bus.stop()
    assert ("set_style", "Email") in overlay.calls
    assert ("set_cleanup", "Medium") in overlay.calls
    assert ("set_language", "") in overlay.calls


@pytest.mark.parametrize(
    ("process", "style"),
    [
        ("notion.exe", "Notes"),
        ("Code.exe", "Code"),
        ("notepad.exe", "Plain"),
    ],
)
def test_press_overlay_style_chip_matches_built_in_apps(
    fake_asr, fake_injector, monkeypatch, process: str, style: str
) -> None:
    class ChipOverlay(FakeOverlay):
        def set_style(self, label: str) -> None:
            self.calls.append(("set_style", label))

        def set_cleanup(self, label: str) -> None:
            self.calls.append(("set_cleanup", label))

    overlay = ChipOverlay()
    monkeypatch.setattr("dcent_voice.pipeline._foreground_process_name", lambda: process)
    bus = EventBus()
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    deadline = time.monotonic() + 1.0
    while ("set_style", style) not in overlay.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    worker.stop()
    bus.stop()
    assert ("set_style", style) in overlay.calls
    assert ("set_cleanup", "Medium") in overlay.calls


def test_press_spoken_style_overrides_destination_and_overlay(
    fake_asr, fake_injector, monkeypatch
) -> None:
    class ChipOverlay(FakeOverlay):
        def set_style(self, label: str) -> None:
            self.calls.append(("set_style", label))

    overlay = ChipOverlay()
    monkeypatch.setattr("dcent_voice.pipeline._foreground_process_name", lambda: "notepad.exe")
    fake_asr.text = "email style hey send the deck thanks"
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected
    text = fake_injector.injected[-1]
    assert "email style" not in text.lower()
    assert text.startswith("Hey")
    assert ("set_style", "Plain") in overlay.calls
    assert ("set_style", "Email") in overlay.calls


def test_scratch_that_alone_retracts_last_insert(fake_asr, fake_injector) -> None:
    bus = EventBus()
    ready: list[TranscriptReady] = []
    done = threading.Event()
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    overlay = FakeOverlay()
    fake_asr.text = "hello world"
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    assert fake_injector.injected == ["hello world"]
    done.clear()
    fake_asr.text = "scratch that"
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.retracted == [len("hello world")]
    assert ready[-1].reason == "undone"
    assert ready[-1].discarded is False
    assert ("set_message", "Undone") in overlay.calls


def test_scratch_that_alone_with_nothing_to_undo(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    ready: list[TranscriptReady] = []
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    overlay = FakeOverlay()
    fake_asr.text = "undo that"
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected == []
    assert fake_injector.retracted == []
    assert ready[0].reason == "nothing_to_undo"
    assert ready[0].discarded is True
    assert ("set_message", "Nothing to undo") in overlay.calls


def test_press_spoken_cleanup_overrides_level_and_overlay(
    fake_asr, fake_injector, monkeypatch
) -> None:
    class ChipOverlay(FakeOverlay):
        def set_cleanup(self, label: str) -> None:
            self.calls.append(("set_cleanup", label))

    overlay = ChipOverlay()
    monkeypatch.setattr("dcent_voice.pipeline._foreground_process_name", lambda: "notepad.exe")
    fake_asr.text = "cleanup high I think we should ship Monday"
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(local_polish=True, spoken_edits=True),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected
    text = fake_injector.injected[-1]
    assert "i think" not in text.lower()
    assert "cleanup high" not in text.lower()
    assert ("set_cleanup", "Medium") in overlay.calls
    assert ("set_cleanup", "High") in overlay.calls


def test_press_enter_after_text_injects_then_submits(fake_asr, fake_injector) -> None:
    bus = EventBus()
    ready: list[TranscriptReady] = []
    done = threading.Event()
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    overlay = FakeOverlay()
    fake_asr.text = "hello world press enter"
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected == ["hello world"]
    assert fake_injector.enters == 1
    assert ready[-1].reason == "sent"
    assert ready[-1].discarded is False
    assert ("set_message", "Sent") in overlay.calls


def test_press_enter_alone_sends_without_insert(fake_asr, fake_injector) -> None:
    bus = EventBus()
    ready: list[TranscriptReady] = []
    done = threading.Event()
    bus.subscribe(
        lambda ev: (ready.append(ev), done.set()) if isinstance(ev, TranscriptReady) else None
    )
    bus.start()
    overlay = FakeOverlay()
    fake_asr.text = "press enter"
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected == []
    assert fake_injector.enters == 1
    assert ready[-1].reason == "enter"
    assert ready[-1].discarded is False
    assert ("set_message", "Enter") in overlay.calls


def test_press_enter_mid_utterance_stays_in_text(fake_asr, fake_injector) -> None:
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    overlay = FakeOverlay()
    fake_asr.text = "press enter to continue"
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        overlay=overlay,
        config=_cfg(),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected == ["press enter to continue"]
    assert fake_injector.enters == 0


def test_pipeline_expands_filled_snippet(fake_asr, fake_injector) -> None:
    fake_asr.text = "send it to my email please"
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(
            snippets=(SnippetEntry(spoken="my email", expansion="ops@example.com"),),
        ),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected
    text = fake_injector.injected[-1]
    assert "ops@example.com" in text
    assert "my email" not in text.lower()


def test_pipeline_cleared_snippets_do_not_seed_starters(fake_asr, fake_injector) -> None:
    fake_asr.text = "send it to my email please"
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(lambda ev: done.set() if isinstance(ev, TranscriptReady) else None)
    bus.start()
    worker = PipelineWorker(
        bus=bus,
        capture=FakeCapture(np.ones(16000, dtype=np.float32) * 0.1),
        asr=fake_asr,
        injector=fake_injector,
        config=_cfg(snippets=()),
    )
    worker.start()
    bus.publish(HotkeyPressed(AppMode.DICTATION))
    bus.publish(HotkeyReleased(AppMode.DICTATION))
    assert done.wait(2.0)
    worker.stop()
    bus.stop()
    assert fake_injector.injected
    text = fake_injector.injected[-1]
    assert "my email" in text.lower()
    assert "@" not in text
