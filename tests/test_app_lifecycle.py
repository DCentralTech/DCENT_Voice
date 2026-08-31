# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcent_voice import app
from dcent_voice.app import auto_open_first_run_wizard
from dcent_voice.config import load_config


def test_macos_pipeline_check_is_complete_on_this_host() -> None:
    from scripts.check_macos_pipeline import inspect_pipeline

    report = inspect_pipeline()
    assert report["unsigned_recipe_complete"] is True
    assert report["missing"] == []
    assert report["can_build_binary_on_this_host"] is False or report["this_host"] == "Darwin"
    assert "not a product win" in report["notarization_blocker"]


def test_first_run_opens_the_setup_wizard_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: first launch shows a window; later launches never do."""
    monkeypatch.setattr(
        "dcent_voice.ui.webview_runtime.windows_webview2_runtime_present", lambda: True
    )
    assert auto_open_first_run_wizard(no_tray=False, first_run_education_shown=False) is True
    # Second launch: the persisted flag suppresses it forever.
    assert auto_open_first_run_wizard(no_tray=False, first_run_education_shown=True) is False
    # Headless automation has no human to teach.
    assert auto_open_first_run_wizard(no_tray=True, first_run_education_shown=False) is False


def test_first_run_wizard_is_skipped_without_webview2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without WebView2 the wizard cannot render; the native dialog takes over."""
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(
        "dcent_voice.ui.webview_runtime.windows_webview2_runtime_present", lambda: False
    )
    assert auto_open_first_run_wizard(no_tray=False, first_run_education_shown=False) is False


def test_native_first_run_dialog_persists_the_flag_only_when_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dcent_voice.ui import first_run

    config = load_config(Path("config.example.toml"), create=False)
    shown: list[bool] = []
    seen_args: list[dict] = []

    def fake_dialog(cfg, *, webview2_missing: bool, gui_missing: bool = False) -> bool:
        seen_args.append(
            {"config": cfg, "webview2_missing": webview2_missing, "gui_missing": gui_missing}
        )
        return True

    monkeypatch.setattr(first_run, "show_first_run_dialog", fake_dialog)
    thread = app._start_first_run_dialog(
        config,
        webview2_missing=True,
        on_shown=lambda: shown.append(True),
        logger=logging.getLogger("test.first_run"),
    )
    thread.join(timeout=5.0)
    assert shown == [True]
    assert seen_args[0]["webview2_missing"] is True

    # A suppressed dialog (CI / DCENT_VOICE_NO_DIALOGS) must not record the
    # user as educated: the next launch has to try again.
    shown.clear()
    monkeypatch.setattr(first_run, "show_first_run_dialog", lambda *_a, **_k: False)
    thread = app._start_first_run_dialog(
        config,
        webview2_missing=True,
        on_shown=lambda: shown.append(True),
        logger=logging.getLogger("test.first_run"),
    )
    thread.join(timeout=5.0)
    assert shown == []


def test_tray_diagnostics_call_site_matches_the_doctor_contract() -> None:
    """The tray calls ``run_doctor_in_background(notify)`` on pystray's thread.

    It must accept a single positional notify callable and hand back a started
    thread rather than doing the work inline — a blocking call here freezes the
    whole tray menu.
    """
    import inspect
    import threading

    from dcent_voice.doctor import run_doctor_in_background

    signature = inspect.signature(run_doctor_in_background)
    parameters = list(signature.parameters.values())
    assert parameters[0].kind in {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }
    assert all(p.default is not inspect.Parameter.empty for p in parameters[1:])
    assert signature.return_annotation in {threading.Thread, "threading.Thread"}


def test_first_run_dialog_is_suppressed_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from dcent_voice.ui.first_run import show_first_run_dialog

    monkeypatch.setenv("DCENT_VOICE_NO_DIALOGS", "1")
    config = load_config(Path("config.example.toml"), create=False)
    assert show_first_run_dialog(config, webview2_missing=True) is False


def test_first_run_dialog_text_carries_the_required_facts() -> None:
    from dcent_voice.ui.first_run import dialog_text

    config = load_config(Path("config.example.toml"), create=False)
    text = dialog_text(config, webview2_missing=True, platform="win32")
    assert "Hold Ctrl+Win and speak" in text
    assert "chevron" in text
    assert "Everything stays on this machine" in text
    assert "https://go.microsoft.com/fwlink/p/?LinkId=2124703" in text
    # The runtime paragraph appears only when the runtime is actually missing.
    assert "WebView2" not in dialog_text(config, webview2_missing=False, platform="win32")


class FakeLock:
    def __init__(self) -> None:
        self.acquired = False
        self.released = False

    def acquire(self) -> None:
        self.acquired = True

    def release(self) -> None:
        self.released = True


class FakeBus:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def subscribe(self, _callback):
        return lambda: None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def publish(self, _event) -> None:
        pass


def _base_patches(monkeypatch: pytest.MonkeyPatch, lock: FakeLock, bus: FakeBus) -> None:
    monkeypatch.setattr(app, "configure_logging", lambda: logging.getLogger("test.lifecycle"))
    monkeypatch.setattr(app, "_autostart_sync_enabled", lambda: False)
    monkeypatch.setattr(app, "remove_stale_registry_entries", lambda: [])
    monkeypatch.setattr(app, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(app, "EventBus", lambda: bus)
    monkeypatch.setattr(app, "build_credential_store", lambda _logger: None)


def test_startup_constructor_failure_releases_bus_and_instance_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = FakeLock()
    bus = FakeBus()
    _base_patches(monkeypatch, lock, bus)
    monkeypatch.setattr(
        app,
        "SettingsController",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("settings failed")),
    )

    with pytest.raises(RuntimeError, match="settings failed"):
        app.run_app(load_config(Path("config.example.toml"), create=False), no_overlay=True)

    assert lock.acquired and lock.released
    assert bus.started and bus.stopped


def test_failure_after_pipeline_start_stops_pipeline_bus_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = FakeLock()
    bus = FakeBus()
    _base_patches(monkeypatch, lock, bus)
    dummy_controller = SimpleNamespace()
    monkeypatch.setattr(app, "SettingsController", lambda **_kwargs: dummy_controller)
    monkeypatch.setattr(app, "WizardController", lambda **_kwargs: dummy_controller)
    asr = SimpleNamespace(load=lambda: None, unload=lambda: None)
    monkeypatch.setattr(app, "build_asr_provider", lambda *_args, **_kwargs: asr)
    monkeypatch.setattr(app, "build_llm_provider", lambda *_args, **_kwargs: None)
    capture = SimpleNamespace(meter=None, stop=lambda: None)
    monkeypatch.setattr(app, "AudioCapture", lambda **_kwargs: capture)
    injector = SimpleNamespace(inject=lambda _text: None)
    monkeypatch.setattr(app, "build_injector", lambda _config: injector)
    monkeypatch.setattr(app, "CommandRouter", lambda _llm: SimpleNamespace())
    monkeypatch.setattr(app, "CommandExecutor", lambda _injector: SimpleNamespace())
    monkeypatch.setattr(app, "ADEDispatcher", lambda **_kwargs: SimpleNamespace())

    class FakePipeline:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

    pipeline = FakePipeline()
    monkeypatch.setattr(app, "PipelineWorker", lambda **_kwargs: pipeline)
    monkeypatch.setattr(
        app,
        "CoalescingWorker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("worker failed")),
    )

    with pytest.raises(RuntimeError, match="worker failed"):
        app.run_app(load_config(Path("config.example.toml"), create=False), no_overlay=True)

    assert pipeline.started and pipeline.stopped
    assert bus.started and bus.stopped
    assert lock.acquired and lock.released


def test_reuse_keys_ignore_settings_that_cannot_affect_the_provider() -> None:
    """Saving an unrelated setting must not look like a different provider."""
    from dataclasses import replace

    from dcent_voice.config import ASRSpec

    config = load_config(Path("config.example.toml"), create=False)
    baseline = app.asr_reuse_key(config)
    assert baseline is not None

    flipped = replace(config, privacy=replace(config.privacy, first_run_education_shown=True))
    assert app.asr_reuse_key(flipped) == baseline
    assert app.llm_reuse_key(flipped) == app.llm_reuse_key(config)

    # Anything the provider is actually built from must break the key.
    assert app.asr_reuse_key(replace(config, idle_unload_s=1.0)) != baseline
    assert app.asr_reuse_key(replace(config, language_mode="multilingual")) != baseline
    profile = config.current_profile
    retuned = replace(
        config,
        profiles={
            **config.profiles,
            config.active_profile: replace(profile, language="fr"),
        },
    )
    assert app.asr_reuse_key(retuned) != baseline

    # Cloud never reuses: rebuilding is what re-validates consent, picks up a
    # rotated API key, and rebinds the egress logger to the new PrivacyMonitor.
    cloud = replace(
        config,
        profiles={
            **config.profiles,
            config.active_profile: replace(profile, asr=ASRSpec.parse("deepgram:nova-3")),
        },
    )
    assert app.asr_reuse_key(cloud) is None


def test_saving_the_first_run_flag_does_not_rebuild_the_asr_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first-run flag save must not swap a loaded model for a cold one.

    Persisting ``first_run_education_shown`` publishes ``ConfigChanged`` like
    any other save. The reload used to rebuild ASR unconditionally, so the
    ~670 MB model loaded twice on first run and ``/health`` reported
    ``model_loaded=False`` until the next utterance.
    """
    from dataclasses import replace

    from dcent_voice.events import ConfigChanged

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    lock = FakeLock()
    bus = FakeBus()
    _base_patches(monkeypatch, lock, bus)

    class DummyApi:
        def _update_runtime(self, *_args, **_kwargs) -> None:
            return None

    dummy_controller = SimpleNamespace(api=DummyApi(), settings_api=DummyApi(), open=lambda: True)
    monkeypatch.setattr(app, "SettingsController", lambda **_k: dummy_controller)
    monkeypatch.setattr(app, "WizardController", lambda **_k: dummy_controller)

    asr = SimpleNamespace(
        load=lambda: None,
        unload=lambda: None,
        is_loaded=lambda: True,
        set_lifecycle_listener=lambda _cb: None,
    )
    asr_builds: list[int] = []

    def counting_build_asr(*_args, **_kwargs):
        asr_builds.append(1)
        return asr

    monkeypatch.setattr(app, "build_asr_provider", counting_build_asr)
    monkeypatch.setattr(app, "build_llm_provider", lambda *_a, **_k: None)
    monkeypatch.setattr(
        app, "AudioCapture", lambda **_k: SimpleNamespace(meter=None, stop=lambda: None)
    )
    monkeypatch.setattr(app, "OpenWakeWordService", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(app, "VoiceRuntimeControl", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(app, "build_injector", lambda _c: SimpleNamespace())
    monkeypatch.setattr(app, "CommandRouter", lambda _llm: SimpleNamespace())
    monkeypatch.setattr(app, "CommandExecutor", lambda _i: SimpleNamespace())
    monkeypatch.setattr(app, "ADEDispatcher", lambda **_k: SimpleNamespace())

    applied_to: list[object] = []
    monkeypatch.setattr(
        app,
        "PipelineWorker",
        lambda **_k: SimpleNamespace(
            start=lambda: None,
            stop=lambda: None,
            update_runtime=lambda **kwargs: applied_to.append(kwargs["asr"]),
        ),
    )

    # Capture the reload callable the app hands to its coalescing worker, so the
    # real closure runs rather than a reimplementation of it.
    captured: dict[str, object] = {}

    def fake_worker(fn, **_kwargs):
        captured["apply"] = fn
        return SimpleNamespace(
            start=lambda: None, stop=lambda timeout=0.0: True, request=lambda _e: None
        )

    monkeypatch.setattr(app, "CoalescingWorker", fake_worker)

    class FakeTray:
        def __init__(self, **_kwargs) -> None:
            pass

        def refresh(self, _config=None) -> None:
            return None

        def notify_user(self, _title: str, _body: str, **_kwargs) -> None:
            return None

        def start(self) -> None:
            # Everything the reload touches exists by now. Flip only the
            # first-run flag on disk and drive the real apply path.
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "first_run_education_shown = false",
                    "first_run_education_shown = true",
                ),
                encoding="utf-8",
            )
            captured["apply"](ConfigChanged("test"))
            raise RuntimeError("stop after reload")

        def stop(self) -> None:
            pass

    monkeypatch.setattr(app, "TrayApp", FakeTray)

    config = load_config(config_path, create=False)
    config = replace(config, service=replace(config.service, enabled=False))
    with pytest.raises(RuntimeError, match="stop after reload"):
        app.run_app(config, no_overlay=True, no_hotkeys=True)

    assert asr_builds == [1], "the reload rebuilt the ASR provider it should have reused"
    assert applied_to == [asr], "the pipeline must keep the already-loaded provider"


def test_tray_starts_before_the_asr_model_finishes_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S9/AC3: verifying ~670 MB must not delay the only visible surface.

    The model load is held open for the duration of the test; ``tray.start()``
    still runs, and observes an ASR provider that is not loaded yet.
    """
    import threading
    from dataclasses import replace

    lock = FakeLock()
    bus = FakeBus()
    _base_patches(monkeypatch, lock, bus)
    dummy_controller = SimpleNamespace(api=SimpleNamespace(), open=lambda: True)
    monkeypatch.setattr(app, "SettingsController", lambda **_kwargs: dummy_controller)
    monkeypatch.setattr(app, "WizardController", lambda **_kwargs: dummy_controller)

    load_started = threading.Event()
    release_load = threading.Event()

    class SlowAsr:
        def __init__(self) -> None:
            self.loaded = False

        def is_loaded(self) -> bool:
            return self.loaded

        def load(self) -> None:
            load_started.set()
            release_load.wait(10.0)
            self.loaded = True

        def unload(self) -> None:
            self.loaded = False

    asr = SlowAsr()
    monkeypatch.setattr(app, "build_asr_provider", lambda *_a, **_k: asr)
    monkeypatch.setattr(app, "build_llm_provider", lambda *_a, **_k: None)
    monkeypatch.setattr(
        app, "AudioCapture", lambda **_k: SimpleNamespace(meter=None, stop=lambda: None)
    )
    monkeypatch.setattr(app, "OpenWakeWordService", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(app, "VoiceRuntimeControl", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(app, "build_injector", lambda _c: SimpleNamespace())
    monkeypatch.setattr(app, "CommandRouter", lambda _llm: SimpleNamespace())
    monkeypatch.setattr(app, "CommandExecutor", lambda _i: SimpleNamespace())
    monkeypatch.setattr(app, "ADEDispatcher", lambda **_k: SimpleNamespace())
    monkeypatch.setattr(
        app,
        "PipelineWorker",
        lambda **_k: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    monkeypatch.setattr(
        app,
        "CoalescingWorker",
        lambda *_a, **_k: SimpleNamespace(
            start=lambda: None, stop=lambda timeout=0.0: True, request=lambda _e: None
        ),
    )

    observed: dict[str, object] = {}

    class FakeTray:
        def __init__(self, **kwargs) -> None:
            observed["asr_ready_kwarg"] = kwargs.get("asr_ready")

        def start(self) -> None:
            observed["started"] = True
            observed["asr_loaded_at_tray_start"] = asr.is_loaded()
            raise RuntimeError("stop after tray start")

        def stop(self) -> None:
            pass

    monkeypatch.setattr(app, "TrayApp", FakeTray)

    config = load_config(Path("config.example.toml"), create=False)
    config = replace(config, service=replace(config.service, enabled=False))
    try:
        with pytest.raises(RuntimeError, match="stop after tray start"):
            app.run_app(config, no_overlay=True, no_hotkeys=True)
    finally:
        release_load.set()

    assert observed["started"] is True
    assert observed["asr_ready_kwarg"] is False
    assert observed["asr_loaded_at_tray_start"] is False
    # The load really was in flight rather than skipped.
    assert load_started.wait(5.0)


def test_replaced_provider_lifecycle_events_are_dropped() -> None:
    """A provider swapped out by a config reload must not publish readiness.

    The background load thread can finish after ``apply_config_change`` has
    replaced the provider; its late ``ready`` event would otherwise make the
    tray report a model as ready that is no longer wired to the pipeline.
    """

    class RecordingBus:
        def __init__(self) -> None:
            self.events: list = []

        def publish(self, event) -> None:
            self.events.append(event)

    class FakeProvider:
        def __init__(self) -> None:
            self.listener = None

        def set_lifecycle_listener(self, listener) -> None:
            self.listener = listener

    bus = RecordingBus()
    old = FakeProvider()
    new = FakeProvider()
    current = {"asr": old}
    app._bind_asr_lifecycle(old, bus, lambda *a, **k: None, current_of=lambda: current["asr"])
    app._bind_asr_lifecycle(new, bus, lambda *a, **k: None, current_of=lambda: current["asr"])

    old.listener(True, "load finished")
    assert len(bus.events) == 1  # still current: published

    current["asr"] = new  # the reload swapped providers
    old.listener(True, "late load finished")
    assert len(bus.events) == 1  # stale provider: dropped

    new.listener(True, "ready")
    assert len(bus.events) == 2  # the wired provider still publishes
