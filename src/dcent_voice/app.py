# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Create and run the DCENT_Voice desktop application."""

from __future__ import annotations

# MUST be the first DCENT_Voice import: installs the bootstrap log, the crash
# hooks and the offline environment before anything heavy (and anything that
# transitively imports huggingface_hub) is loaded. See util/bootlog.py.
from .util import bootlog as bootlog  # noqa: I001  isort: skip

import argparse
import contextlib
import importlib
import json
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, autostart
from .asr.base import Locality
from .attach.registry import (
    build_launch_descriptor,
    create_registry_entry,
    create_token,
    remove_registry_entry,
    remove_stale_registry_entries,
    write_install_manifest,
    write_registry_entry,
)
from .attach.single_instance import (
    AlreadyRunningError,
    LockUnavailableError,
    SingleInstanceLock,
    force_clear_stale_lock,
)
from .config import (
    AppConfig,
    ConfigError,
    default_config_path,
    effective_snippets,
    load_config,
)
from .events import (
    AsrReadyChanged,
    ConfigChanged,
    EventBus,
    PrivacyChanged,
    ShutdownRequested,
    WakeWordDetected,
)
from .privacy import ConsentRequired, PrivacyMonitor
from .util.fatal import report_fatal
from .util.logging import configure_logging, default_log_path
from .util.wait import wait_any

# ---------------------------------------------------------------------------
# Lazy heavy imports
#
# ``dcent_voice.app`` used to eagerly import ~70 modules, several of which pull
# native libraries (ctranslate2, onnxruntime, sounddevice/PortAudio, uvicorn,
# pywebview, pystray). In the windowed frozen build an unrelated DLL failure in
# any one of them killed the process before a single line could be logged — and
# it killed ``--version``, ``--print-config``, ``doctor``, ``verify-payload``
# and ``platform-check`` too, none of which need those libraries at all.
#
# The module-level names below are resolved on first use through ``__getattr__``
# and then cached in ``globals()``. Function bodies keep referring to them as
# plain globals, and tests keep monkeypatching ``app.<Name>`` exactly as before,
# because attribute access is what triggers the import.
# ---------------------------------------------------------------------------
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "build_asr_from_spec": (".asr.factory", "build_asr_provider"),
    "AudioCapture": (".audio.capture", "AudioCapture"),
    "CredentialStore": (".auth.store", "CredentialStore"),
    "benchmark_main": (".bench_latency", "main"),
    "CommandExecutor": (".commands.actions", "CommandExecutor"),
    "ADEDispatcher": (".commands.ade_dispatch", "ADEDispatcher"),
    "CommandRouter": (".commands.router", "CommandRouter"),
    "DeviceBenchError": (".devices", "DeviceBenchError"),
    "fetch_samples_ms": (".devices", "fetch_samples_ms"),
    "format_device_bench_report": (".devices", "format_device_bench_report"),
    "parse_samples_ms": (".devices", "parse_samples_ms"),
    "run_device_bench": (".devices", "run_device_bench"),
    "HotkeyManager": (".hotkeys", "HotkeyManager"),
    "Injector": (".inject.base", "Injector"),
    "ClipboardPasteInjector": (".inject.clipboard", "ClipboardPasteInjector"),
    "PynputTypeInjector": (".inject.keystroke", "PynputTypeInjector"),
    "WindowsSendInputInjector": (".inject.keystroke", "WindowsSendInputInjector"),
    "RoutingInjector": (".inject.router", "RoutingInjector"),
    "get_selected_text": (".inject.selection", "get_selected_text"),
    "AnthropicProvider": (".llm.anthropic", "AnthropicProvider"),
    "CleanupPipeline": (".llm.cleanup", "CleanupPipeline"),
    "OpenAICompatProvider": (".llm.openai_compat", "OpenAICompatProvider"),
    "PersonalizationStore": (".personalization", "PersonalizationStore"),
    "PipelineConfig": (".pipeline", "PipelineConfig"),
    "PipelineWorker": (".pipeline", "PipelineWorker"),
    "RecoveryStore": (".recovery", "RecoveryStore"),
    "ServiceEngine": (".service.api", "ServiceEngine"),
    "create_app": (".service.api", "create_app"),
    "add_dvap_websocket": (".service.dvap", "add_dvap_websocket"),
    "ServiceThread": (".service.server", "ServiceThread"),
    "format_http_base": (".service.server", "format_http_base"),
    "VoiceRuntimeControl": (".service.voice_control", "VoiceRuntimeControl"),
    "add_event_websocket": (".service.ws", "add_event_websocket"),
    "add_stream_websocket": (".service.ws", "add_stream_websocket"),
    "CallbackMicGate": (".tts", "CallbackMicGate"),
    "SoundDeviceSink": (".tts", "SoundDeviceSink"),
    "build_tts_backend": (".tts", "build_tts_backend"),
    "OverlayController": (".ui.overlay", "OverlayController"),
    "SettingsController": (".ui.settings", "SettingsController"),
    "WizardController": (".ui.settings", "WizardController"),
    "TrayApp": (".ui.tray", "TrayApp"),
    "TrayCallbacks": (".ui.tray", "TrayCallbacks"),
    "CoalescingWorker": (".util.coalescing", "CoalescingWorker"),
    "SessionResumeMonitor": (".util.win_session", "SessionResumeMonitor"),
    "OpenWakeWordService": (".wake_word", "OpenWakeWordService"),
}


if TYPE_CHECKING:  # pragma: no cover - resolved lazily at runtime via __getattr__
    from .asr.factory import build_asr_provider as build_asr_from_spec
    from .audio.capture import AudioCapture
    from .auth.store import CredentialStore
    from .bench_latency import main as benchmark_main
    from .commands.actions import CommandExecutor
    from .commands.ade_dispatch import ADEDispatcher
    from .commands.router import CommandRouter
    from .devices import (
        DeviceBenchError,
        fetch_samples_ms,
        format_device_bench_report,
        parse_samples_ms,
        run_device_bench,
    )
    from .hotkeys import HotkeyManager
    from .inject.base import Injector
    from .inject.clipboard import ClipboardPasteInjector
    from .inject.keystroke import PynputTypeInjector, WindowsSendInputInjector
    from .inject.router import RoutingInjector
    from .inject.selection import get_selected_text
    from .llm.anthropic import AnthropicProvider
    from .llm.cleanup import CleanupPipeline
    from .llm.openai_compat import OpenAICompatProvider
    from .personalization import PersonalizationStore
    from .pipeline import PipelineConfig, PipelineWorker
    from .recovery import RecoveryStore
    from .service.api import ServiceEngine, create_app
    from .service.dvap import add_dvap_websocket
    from .service.server import ServiceThread, format_http_base
    from .service.voice_control import VoiceRuntimeControl
    from .service.ws import add_event_websocket, add_stream_websocket
    from .tts import CallbackMicGate, SoundDeviceSink, build_tts_backend
    from .ui.overlay import OverlayController
    from .ui.settings import SettingsController, WizardController
    from .ui.tray import TrayApp, TrayCallbacks
    from .util.coalescing import CoalescingWorker
    from .util.win_session import SessionResumeMonitor
    from .wake_word import OpenWakeWordService


def __getattr__(name: str) -> Any:
    target = _LAZY_IMPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    # Relative to the *package* (``dcent_voice``), not this module: ``app`` is a
    # module, so ``import_module(".inject.base", __name__)`` would look for a
    # non-existent ``dcent_voice.app.inject``.
    value = getattr(importlib.import_module(module_name, __package__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_IMPORTS})


def _bind_runtime_names(*names: str) -> None:
    """Materialise lazily-imported names into ``globals()`` before they are read.

    PEP 562's module ``__getattr__`` is only consulted for *attribute* access on
    the module object (``app.PipelineWorker``, which is what the tests
    monkeypatch). ``LOAD_GLOBAL`` inside a function body looks in ``globals()``
    and then builtins and never calls it — so every function that uses one of
    these names has to bind it first.

    A name a test has already monkeypatched onto the module is left untouched;
    that is the whole reason binding goes through ``globals()`` rather than a
    function-local ``import``.
    """
    for name in names:
        if name not in globals():
            globals()[name] = __getattr__(name)


_APP_READY_STARTED = time.perf_counter()


#: Shown 5 s after a first-run wizard that could not open, so the guidance is
#: not lost in a balloon the user missed while the icon was still appearing.
FIRST_RUN_TRAY_REMINDER = (
    "DCENT_Voice is in the notification area (Windows may hide it under the ^ "
    'chevron). Right-click the icon → Advanced → "Setup wizard..." to finish setup.'
)


def _webview2_present() -> bool:
    """Whether the Edge WebView2 runtime is registered (always True off Windows)."""
    try:
        from .ui.webview_runtime import windows_webview2_runtime_present

        return windows_webview2_runtime_present()
    except Exception:  # pragma: no cover - defensive; registry read is guarded
        return False


def _start_first_run_dialog(
    config: AppConfig,
    *,
    webview2_missing: bool,
    on_shown,
    logger,
    gui_missing: bool = False,
):
    """Show the native first-run dialog off the main thread, exactly once.

    ``MessageBoxW`` is modal and blocking; the main thread is about to host the
    pywebview/overlay loop, and the tray and hotkeys are already running. The
    education flag is persisted only when a human actually saw the dialog, so a
    suppressed (``DCENT_VOICE_NO_DIALOGS=1``) or headless run is not recorded as
    educated.
    """
    from .ui.first_run import show_first_run_dialog

    def _run() -> None:
        try:
            if show_first_run_dialog(
                config, webview2_missing=webview2_missing, gui_missing=gui_missing
            ):
                on_shown()
        except Exception:
            logger.exception("first-run dialog failed")

    thread = threading.Thread(target=_run, name="FirstRunDialog", daemon=True)
    thread.start()
    return thread


def auto_open_first_run_wizard(*, no_tray: bool, first_run_education_shown: bool) -> bool:
    """Whether *this* launch should open the setup wizard (WS3/AC3).

    First launch must show a window: a tray balloon alone is invisible on
    Windows, which hides new notification-area icons under the ``^`` chevron.
    The window is opened exactly once — ``privacy.first_run_education_shown``
    is persisted when it closes — and only *after* ``hotkeys.start()``, so
    hold-to-talk is already live and nothing is blocked on the WebView host.

    ``no_tray`` runs are headless automation with no human to teach. Without
    the Edge WebView2 runtime the wizard cannot render at all; that host gets
    the native first-run dialog instead (:mod:`dcent_voice.ui.first_run`).
    """
    if first_run_education_shown or no_tray:
        return False
    if sys.platform == "win32":
        from .ui.webview_runtime import windows_webview2_runtime_present

        return windows_webview2_runtime_present()
    return True


def _tts_code_policy(skip_code: bool):
    from .tts import CodePolicy

    return CodePolicy.SKIP if skip_code else CodePolicy.SPEAK


def _build_tts_mic_gate(
    capture: AudioCapture,
    config: AppConfig,
    *,
    tts_available: bool,
) -> CallbackMicGate | None:
    """Build the configured half-duplex coupling for local TTS playback."""

    _bind_runtime_names("CallbackMicGate")
    if not tts_available:
        return None
    if config.tts.mic_policy == "pause":
        return CallbackMicGate(on_start=capture.stop)
    if config.tts.mic_policy == "duck":
        return CallbackMicGate(
            on_start=lambda: capture.set_input_gain(config.tts.duck_gain),
            on_stop=lambda: capture.set_input_gain(1.0),
        )
    return None


def report_lock_unavailable(exc: LockUnavailableError) -> int:
    """The OS refused us a single-instance lock — not another running copy.

    Kept as one function because two call sites need the identical message: the
    first acquire, and the retry that ``handle_already_running`` performs after
    clearing a stale lock.
    """
    return report_fatal(
        "DCENT_Voice could not create its instance lock",
        f"{exc}\n\nSign out and back in, or run `dcent-voice doctor` for details.",
        log_path=default_log_path(),
        exit_code=1,
        exc=exc,
    )


def _print_config_recovery_notice() -> None:
    """Tell a console/CLI caller their settings were reset, without consuming it.

    ``run_app`` (and WS3's tray toast) still need the notice, so this only peeks.
    """
    from . import config as config_module

    notice = config_module.recovery_notice
    if notice is not None and sys.stderr is not None:
        with contextlib.suppress(Exception):
            print(f"WARNING: {notice.message()}", file=sys.stderr)


def announce_config_recovery(logger=None) -> str | None:  # type: ignore[no-untyped-def]
    """Report (once) that an invalid settings file was reset, and return the text.

    ``load_config`` quarantines a corrupt or unparsable ``config.toml`` and
    reseeds from the shipped example rather than refusing to start. That is the
    right trade — a startup that works beats one that is technically correct
    about a file the user cannot see — but only if we say so. WS3's tray code
    can call this to show the same string as a toast.
    """
    from .config import take_recovery_notice

    notice = take_recovery_notice()
    if notice is None:
        return None
    text = notice.message()
    (logger or bootlog.logger()).warning("%s (reason: %s)", text, notice.reason)
    return text


def _autostart_sync_enabled() -> bool:
    """Return whether this process may reconcile the OS login-item setting."""
    value = os.environ.get("DCENT_VOICE_DISABLE_AUTOSTART", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dcent-voice")
    parser.add_argument("--version", action="version", version=f"DCENT_Voice {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml. Defaults to the user config under APPDATA.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Load config and print the selected profile. Useful during Wave 0.",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Start without the system tray icon.",
    )
    parser.add_argument(
        "--no-hotkeys",
        action="store_true",
        help="Start without global hotkeys. Useful for service-only smoke runs.",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="Start without the pywebview overlay.",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="Open the settings dashboard and exit when it closes.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open the first-run setup wizard and exit when it closes.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Verify hotkey listener starts, print status, and exit.",
    )
    subparsers = parser.add_subparsers(dest="command")
    devices = subparsers.add_parser("devices", help="Inspect audio devices and profile fit.")
    devices.add_argument(
        "--bench",
        action="store_true",
        help="Suggest the best local profile from voice finalize latency samples.",
    )
    devices.add_argument(
        "--device-class",
        choices=["gpu", "cpu"],
        default="cpu",
        help="Hardware class for finalize latency thresholding.",
    )
    devices.add_argument(
        "--samples-ms",
        default=None,
        help="Comma-separated finalize latency samples in milliseconds.",
    )
    devices.add_argument(
        "--bench-url",
        default=None,
        help="Local HTTP endpoint returning {'finalizeMs':[...]} or {'samplesMs':[...]} JSON.",
    )
    devices.add_argument(
        "--assume-input-devices",
        type=int,
        default=None,
        help="Override detected input-device count for repeatable fixture runs.",
    )
    devices.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    subparsers.add_parser("benchmark", help="Measure local ASR latency for the active profile.")
    transcribe = subparsers.add_parser(
        "transcribe",
        help="Transcribe a WAV file with the headless engine (no tray or hotkeys).",
    )
    transcribe.add_argument("audio", type=Path, help="16-bit PCM WAV path")
    transcribe_output = transcribe.add_mutually_exclusive_group()
    transcribe_output.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON EngineResult and CLI timing metadata.",
    )
    transcribe_output.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write JSON to a file (works from the windowed frozen executable).",
    )
    transcribe.add_argument("--language", default=None, help="Override language hint.")
    transcribe.add_argument(
        "--no-polish",
        action="store_true",
        default=None,
        help="Skip offline postprocess.",
    )
    transcribe.add_argument(
        "--style",
        default=None,
        choices=("plain", "email", "chat", "code", "formal"),
        help="Destination writing style. Omit to use Settings.",
    )
    transcribe.add_argument("--asr", default=None, help="Override ASR spec.")
    transcribe.add_argument(
        "--prose-context",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Explicitly allow or refuse longer learned rewrites for trusted prose.",
    )
    transcribe.add_argument(
        "--app",
        dest="app_context",
        default=None,
        help="Destination app for app-scoped learned terms. Omit for global terms only.",
    )
    compose = subparsers.add_parser(
        "compose",
        help="Rewrite spoken text locally (no ASR, no network, no LLM).",
    )
    compose.add_argument("text", nargs="+", help="Spoken transcript to rewrite.")
    compose.add_argument(
        "--style",
        default=None,
        choices=("plain", "email", "chat", "code", "formal"),
        help="Destination writing style. Omit to reuse the last in-process style.",
    )
    compose.add_argument(
        "--cleanup-level",
        default="medium",
        choices=("none", "light", "medium", "high"),
        help="Local Auto Cleanup analog. Default medium. No LLM.",
    )
    compose.add_argument(
        "--no-polish",
        action="store_true",
        default=None,
        help="Skip offline postprocess fillers; writing style still applies.",
    )
    compose.add_argument(
        "--app",
        dest="app_context",
        default=None,
        help="Destination app for app-scoped learned terms. Omit for global terms only.",
    )
    subparsers.add_parser(
        "engine-info",
        help="Print headless engine capabilities as JSON.",
    )
    subparsers.add_parser(
        "platform-check",
        help="Verify native desktop integration imports and exit.",
    )
    injection_test = subparsers.add_parser(
        "injection-self-test",
        help="Inject only into a private native test control and report exact results.",
    )
    injection_test.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write the report to a file (required by the windowed frozen executable).",
    )
    injection_test.add_argument("--runs", type=int, default=5)
    injection_apps = subparsers.add_parser("injection-app-matrix", help=argparse.SUPPRESS)
    injection_apps.add_argument("--output-json", type=Path, default=None)
    injection_apps.add_argument("--runs", type=int, default=3)
    injection_apps.add_argument(
        "--apps",
        default="all",
        help="Comma-separated isolated targets: notepad,vscode,console,edge,chrome "
        "plus edge-ce,edge-form,chrome-ce,chrome-form "
        "and live tabs edge-ddg,edge-google,edge-wiki,edge-github,edge-gmail.",
    )
    hold_release = subparsers.add_parser("hold-release-self-test", help=argparse.SUPPRESS)
    hold_release.add_argument("--audio", type=Path, required=True)
    hold_release.add_argument("--reference", required=True)
    hold_release.add_argument("--output-device", required=True)
    hold_release.add_argument("--runs", type=int, default=10)
    hold_release.add_argument("--output-json", type=Path, default=None)
    hold_release.add_argument(
        "--apps",
        default="all",
        help="Comma-separated isolated targets: notepad,vscode,console,edge,chrome "
        "plus edge-ce,edge-form,chrome-ce,chrome-form "
        "and live tabs edge-ddg,edge-google,edge-wiki,edge-github,edge-gmail.",
    )
    hold_release.add_argument(
        "--allow-default-microphone",
        action="store_true",
        help="Allow audio.input_device to stay at the OS default microphone.",
    )
    hold_release.add_argument(
        "--real-documents",
        action="store_true",
        help="Inject into existing Notepad/VS Code/browser documents instead of temp scratch.",
    )
    # Private child-process entry point. It accepts a nonce-bearing contract
    # created by injection-self-test and never targets an external application.
    injection_target = subparsers.add_parser("injection-test-target", help=argparse.SUPPRESS)
    injection_target.add_argument("contract", type=Path)
    orphan_parent = subparsers.add_parser("injection-test-orphan-parent", help=argparse.SUPPRESS)
    orphan_parent.add_argument("root", type=Path)
    orphan_parent.add_argument("output", type=Path)
    verify_models = subparsers.add_parser(
        "verify-payload", help="Verify the exact shipped speech-model payload."
    )
    verify_models.add_argument("payload", type=Path)
    stage_models = subparsers.add_parser(
        "stage-payload", help="Copy an install tree through verified model handles."
    )
    stage_models.add_argument("source", type=Path)
    stage_models.add_argument("destination", type=Path)
    learn = subparsers.add_parser(
        "learn",
        help="Record a local typed correction (no audio stored).",
    )
    learn.add_argument("--from", dest="spoken", default="", help="What the recognizer heard.")
    learn.add_argument("--to", dest="written", default="", help="What you meant.")
    learn.add_argument("--last", dest="correction", default="", help="Correct the last utterance.")
    learn.add_argument(
        "--style",
        default=None,
        choices=("plain", "email", "chat", "code", "formal"),
        help="Limit the correction to this writing style.",
    )
    learn.add_argument(
        "--app",
        dest="app_context",
        default=None,
        help="Limit the correction to this destination process/app.",
    )
    learn.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write JSON to a file (works from the windowed frozen executable).",
    )
    frozen_restart = subparsers.add_parser("app-learned-stream-restart", help=argparse.SUPPRESS)
    frozen_restart.add_argument("--store", type=Path, default=None)
    frozen_restart.add_argument("--audio", type=Path, required=True)
    frozen_restart.add_argument("--out", type=Path, required=True)
    frozen_restart.add_argument("--chunk-s", dest="chunk_s", type=float, default=0.25)
    frozen_restart.add_argument("--config-file", type=Path, default=None)
    frozen_restart.add_argument("--reference", default=None)
    doctor = subparsers.add_parser(
        "doctor",
        aliases=["diagnose"],
        help="Diagnose why the app will not start and write a report you can send us.",
    )
    doctor.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write the JSON report to this path.",
    )
    doctor.add_argument(
        "--open",
        action="store_true",
        help="Open the diagnostics folder when the run finishes.",
    )
    doctor.add_argument(
        "--no-launch-checks",
        action="store_true",
        help="Skip the trial launch (the slowest check) and report everything else.",
    )
    doctor.add_argument(
        "--zip",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write the diagnostics zip alongside the report (default: yes).",
    )
    # Private child-process entry point: doctor imports each native extension in
    # a subprocess so a hard crash is reported instead of inherited. The frozen
    # sys.executable is this exe, so the child needs a way back in.
    doctor_probe = subparsers.add_parser("doctor-probe", help=argparse.SUPPRESS)
    doctor_probe.add_argument("module")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, benchmark_args = parser.parse_known_args(argv)
    if benchmark_args and args.command != "benchmark":
        parser.error(f"unrecognized arguments: {' '.join(benchmark_args)}")

    # Installer integrity commands are deliberately configuration-independent.
    # They must work even when the user profile is absent, corrupt, or unreadable,
    # and must not create/migrate profile state before an install is accepted.
    if args.command in {
        "verify-payload",
        "stage-payload",
        "injection-self-test",
        "injection-app-matrix",
        "injection-test-target",
        "injection-test-orphan-parent",
        "app-learned-stream-restart",
        # Diagnostics must run on the machine where nothing else does: before
        # load_config, so a corrupt or missing configuration is a finding in the
        # report rather than a silent exit.
        "doctor",
        "diagnose",
        "doctor-probe",
    }:
        if args.command in {"doctor", "diagnose"}:
            from .doctor import main as run_doctor_command

            return run_doctor_command(args)
        if args.command == "doctor-probe":
            from .doctor import run_probe_command

            return run_probe_command(args.module)
        if args.command == "injection-self-test":
            from .inject.windows_self_test import run_self_test_command

            return run_self_test_command(output_json=args.output_json, runs=args.runs)
        if args.command == "injection-app-matrix":
            from .inject.windows_apps_test import run_apps_test_command

            apps = [item.strip().lower() for item in args.apps.split(",") if item.strip()]
            return run_apps_test_command(output_json=args.output_json, apps=apps, runs=args.runs)
        if args.command == "injection-test-target":
            from .inject.windows_self_test import run_target_command

            return run_target_command(args.contract)
        if args.command == "injection-test-orphan-parent":
            from .inject.windows_self_test import run_orphan_parent_command

            return run_orphan_parent_command(args.root, args.output)
        if args.command == "app-learned-stream-restart":
            from .engine import run_app_learned_stream_restart_worker

            run_app_learned_stream_restart_worker(
                str(args.store) if args.store else "",
                str(args.audio),
                float(args.chunk_s),
                str(args.out),
                config_path=str(args.config_file) if args.config_file else None,
                reference=args.reference,
            )
            return 0
        return run_payload_command(args)

    _restore_frozen_linux_library_path()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Deliberately NOT parser.error(): argparse writes to stderr and exits 2,
        # which in the windowed frozen build is a process that flashes and
        # disappears with no log, no dialog and nothing for the user to act on.
        # Genuine CLI *usage* errors above still use parser.error.
        return report_fatal(
            "DCENT_Voice could not read its configuration",
            f"{exc}\n\nRun `dcent-voice doctor` for a full diagnosis.",
            log_path=bootlog.boot_log_path(),
            exit_code=2,
            exc=exc,
        )
    _print_config_recovery_notice()

    if args.print_config:
        print(f"config: {config.source_path or default_config_path()}")
        print(f"active_profile: {config.active_profile}")
        print(f"asr: {config.current_profile.asr.raw}")
        print(f"llm: {config.current_profile.llm.raw}")
        print(f"language_mode: {config.language_mode}")
        print(f"language: {config.language}")
        print(f"privacy: {config.session_locality.value}")
        return 0

    if args.command == "compose":
        _bind_runtime_names("PersonalizationStore")
        store = PersonalizationStore(
            enabled=config.personalization.enabled,
            learn=config.personalization.learn,
        )
        return run_compose_command(config, args, personalization=store)

    if args.command == "transcribe":
        _bind_runtime_names("PersonalizationStore")
        store = PersonalizationStore(
            enabled=config.personalization.enabled,
            learn=config.personalization.learn,
        )
        return run_transcribe_command(config, args, personalization=store)

    if args.command == "engine-info":
        return run_engine_info(config)

    if args.command == "platform-check":
        return run_platform_check()

    if args.command == "hold-release-self-test":
        from .integration.windows_hold_release import run_hold_release_command

        return run_hold_release_command(
            config,
            audio_path=args.audio,
            reference=args.reference,
            output_device=args.output_device,
            runs=args.runs,
            output_json=args.output_json,
            apps=args.apps,
            allow_default_microphone=args.allow_default_microphone,
            real_documents=args.real_documents,
        )

    if args.command == "learn":
        _bind_runtime_names("PersonalizationStore")
        store = PersonalizationStore(
            enabled=config.personalization.enabled,
            learn=config.personalization.learn,
        )
        return run_learn_command(config, args, personalization=store)

    if args.self_test:
        return run_self_test(config)

    if args.settings:
        return run_settings(config)

    if args.setup:
        return run_setup(config)

    if args.command == "devices":
        return run_devices_command(config, args, parser)

    if args.command == "benchmark":
        benchmark_argv = ["--config", str(config.source_path)] if config.source_path else []
        benchmark_argv.extend(benchmark_args)
        _bind_runtime_names("benchmark_main")
        return benchmark_main(benchmark_argv)

    run_kwargs = dict(
        no_tray=args.no_tray,
        no_hotkeys=args.no_hotkeys,
        no_overlay=args.no_overlay,
    )
    try:
        return run_app(config, **run_kwargs)
    except ConsentRequired as exc:
        return report_fatal(
            "Cloud provider consent is required",
            "DCENT_Voice will not contact a cloud provider without recorded consent.\n\n"
            "Missing consent for: " + ", ".join(exc.missing) + "\n\n"
            "Use the Settings privacy/accounts flow, or switch to a local profile.",
            log_path=default_log_path(),
            exit_code=2,
            exc=exc,
        )
    except LockUnavailableError as exc:
        return report_lock_unavailable(exc)
    except AlreadyRunningError as exc:
        # handle_already_running clears a stale lock and retries run_app, which
        # can raise LockUnavailableError of its own. That retry happens *inside*
        # this handler, so the sibling except clause above cannot catch it —
        # without this the tailored message would be replaced by the generic
        # "unexpected error" dialog in _packaged.run.
        try:
            return handle_already_running(exc, config=config, run_kwargs=run_kwargs)
        except LockUnavailableError as lock_exc:
            return report_lock_unavailable(lock_exc)


def run_devices_command(
    config: AppConfig,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    _bind_runtime_names(
        "DeviceBenchError",
        "fetch_samples_ms",
        "format_device_bench_report",
        "parse_samples_ms",
        "run_device_bench",
    )
    if not args.bench:
        parser.error("devices currently supports --bench.")
        return 2

    try:
        samples = parse_samples_ms(args.samples_ms)
        if args.bench_url:
            samples.extend(fetch_samples_ms(args.bench_url))
        report = run_device_bench(
            config,
            device_class=args.device_class,
            samples_ms=samples,
            assumed_input_devices=args.assume_input_devices,
        )
    except DeviceBenchError as exc:
        parser.error(str(exc))
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_device_bench_report(report))
    return 0


def run_app(
    config: AppConfig,
    *,
    no_tray: bool = False,
    no_hotkeys: bool = False,
    no_overlay: bool = False,
) -> int:
    _bind_runtime_names(*_LAZY_IMPORTS)
    logger = configure_logging()
    logger.info("Starting DCENT_Voice with profile %s", config.active_profile)
    # A settings file we had to reset must never be a silent surprise. The
    # returned text is also toasted once the tray exists (WS3).
    config_recovery_notice = announce_config_recovery(logger)
    # Keep the OS login item in sync with the config flag (previously a no-op).
    # Isolated smoke/automation runs opt out so they never mutate a user's
    # actual login item while exercising the rest of the desktop runtime.
    if _autostart_sync_enabled():
        with contextlib.suppress(Exception):
            autostart.set_enabled(config.launch_at_startup)
    else:
        logger.info("autostart synchronization disabled by environment")
    with contextlib.suppress(Exception):
        removed = remove_stale_registry_entries()
        if removed:
            logger.info("removed %s stale ADE registry entr(y/ies)", len(removed))
    with contextlib.suppress(Exception):
        write_install_manifest()
    # Consent before lock so we never hold the instance lock on a hard config error.
    privacy = PrivacyMonitor.from_config(config)
    privacy.validate_cloud_consent()

    # Claim single-instance ownership before any heavy side effects (ASR load,
    # pipeline thread, overlay window) so a second launch cannot race partial runtimes.
    instance_lock = SingleInstanceLock()
    instance_lock.acquire()

    # Predeclare every resource used by the shared teardown path. Startup itself
    # is inside the same try/finally, so a constructor or start() failure cannot
    # leave the instance lock, bus, pipeline, or config worker behind.
    bus: EventBus | None = None
    pipeline: PipelineWorker | None = None
    runtime_reloader = None
    session_monitor: SessionResumeMonitor | None = None
    hotkeys: HotkeyManager | None = None
    service = None
    registry_entry = None
    tray: TrayApp | None = None
    overlay = None
    llm = None
    cleanup = None
    voice_control = None

    try:
        credential_store = build_credential_store(logger)
        personalization = PersonalizationStore(
            enabled=config.personalization.enabled,
            learn=config.personalization.learn,
        )
        recovery = RecoveryStore.from_config(config)
        recovery.update_policy(config.recovery)

        bus = EventBus()
        shutdown = threading.Event()
        bus.subscribe(lambda ev: shutdown.set() if isinstance(ev, ShutdownRequested) else None)
        bus.start()
        gui_gate = _GuiHostGate(shutdown)
        settings = SettingsController(
            config=config,
            bus=bus,
            privacy=privacy,
            credential_store=credential_store,
            on_window_requested=gui_gate.request,
            personalization=personalization,
            recovery_store=recovery,
        )
        wizard = WizardController(
            config=config,
            bus=bus,
            privacy=privacy,
            credential_store=credential_store,
            on_window_requested=gui_gate.request,
            recovery_store=recovery,
        )
        bus.publish(PrivacyChanged(privacy.status.value))

        # Read once, before anything can persist the flag: the tray balloon
        # marks first-run education as shown, and it starts below.
        first_run = not config.privacy.first_run_education_shown

        asr = build_asr_provider(config, privacy, credential_store)
        # asr.load() is deliberately *not* called here: it SHA-256s ~670 MB and
        # would hold the main thread until long after a user expects an icon.
        # It is started on a daemon thread below, once the lifecycle listener
        # exists to publish AsrReadyChanged.
        llm = build_llm_provider(config, privacy, credential_store)
        cleanup = (
            CleanupPipeline(
                llm,
                enabled=config.current_profile.cleanup_enabled,
                dictionary=config.dictionary,
                snippets=effective_snippets(config.snippets),
                health_preflight=isinstance(llm, OpenAICompatProvider)
                and llm.provider_name in {"ollama", "lmstudio"},
            )
            if llm is not None and config.current_profile.cleanup_enabled
            else None
        )
        capture = AudioCapture(
            device=config.audio.input_device,
            max_seconds=config.audio.max_seconds,
        )
        wake_service = OpenWakeWordService(
            lambda phrase: bus.publish(WakeWordDetected(phrase)),
            device=config.audio.input_device,
        )
        # Hotkeys is constructed later; this holder is filled once it exists so
        # DVAP voice.mode.set can switch hold vs toggle without a false report.
        hotkey_holder: dict[str, HotkeyManager | None] = {"manager": None}

        def _apply_activation_mode(mode: str) -> None:
            from dataclasses import replace

            manager = hotkey_holder["manager"]
            if manager is None:
                return
            current = manager.config
            desired = "toggle" if mode == "toggle" else "hold"
            if current.mode != desired:
                manager.config = replace(current, mode=desired)

        voice_control = VoiceRuntimeControl(
            capture,
            wake_service,
            on_activation_mode=_apply_activation_mode,
        )
        overlay = None
        overlay_created = False
        if config.overlay.enabled and not no_overlay:
            meter = capture.meter
            if meter is None:
                raise RuntimeError("audio capture meter was not initialized")
            overlay = OverlayController(config=config.overlay, meter=meter)
            if config.overlay.lazy:
                logger.info("lazy overlay enabled; WebView will start on first dictation")
            else:
                overlay_created = overlay.create_window()
                if not overlay_created:
                    logger.warning("pywebview overlay unavailable; continuing without overlay.")
                else:
                    overlay.set_privacy(privacy.status.value)

        injector = build_injector(config)
        command_router = CommandRouter(llm)
        command_executor = CommandExecutor(injector)
        # Transcript-derived automation is discovered through the authenticated
        # local ADE registry. A raw environment URL is intentionally not trusted.
        ade_dispatcher = ADEDispatcher()

        # Tray is created early enough that pipeline notify can forward toasts.
        # ``key``/``min_interval_s`` are forwarded so rate-limited callers (the
        # ASR lifecycle listener) do not fail with a TypeError swallowed by the
        # listener's own exception guard.
        def tray_notify(title: str, body: str, **kwargs: Any) -> None:
            if tray is not None:
                tray.notify_user(title, body, **kwargs)
            else:
                logger.info("notify: %s — %s", title, body)

        # The holder tracks which provider is wired so a replaced provider's
        # late lifecycle events can be ignored (see _bind_asr_lifecycle).
        asr_current = {"asr": asr}
        _bind_asr_lifecycle(asr, bus, tray_notify, current_of=lambda: asr_current["asr"])
        asr_ready_at_start = bool(getattr(asr, "is_loaded", lambda: False)())
        if asr_ready_at_start:
            bus.publish(AsrReadyChanged(True, "startup"))
        else:
            # Off the main thread: the tray icon, hotkeys and the local service
            # must all be up before the model verification hash finishes. The
            # provider publishes AsrReadyChanged through the listener bound
            # above; a hotkey pressed first records and loads on demand (the
            # pipeline already shows "Loading model…" for an unloaded provider).
            _warn_if_asr_unready(asr, logger, background=True)

        pipeline = PipelineWorker(
            bus=bus,
            capture=capture,
            asr=asr,
            injector=injector,
            overlay=overlay,
            cleanup=cleanup,
            command_router=command_router,
            command_executor=command_executor,
            ade_dispatcher=ade_dispatcher,
            selection_getter=get_selected_text,
            notify=tray_notify,
            recovery_store=recovery,
            config=build_pipeline_config(
                config,
                cleanup_enabled=cleanup is not None,
                personalization=personalization,
            ),
        )
        pipeline.start()

        service = None
        service_engine = None
        registry_entry = None
        runtime_lock = threading.RLock()

        def hotkeys_status_provider() -> dict:
            if hotkeys is None:
                return {
                    "enabled": False,
                    "ok": True,
                    "status": "disabled",
                    "listener_running": False,
                    "critical": False,
                }
            snap = hotkeys.status()
            return {
                "enabled": snap.enabled,
                # recovering/dead must not report healthy — ADE/monitors need truth.
                "ok": (snap.status == "ok") or not snap.enabled,
                "status": snap.status,
                "listener_running": snap.listener_running,
                "last_event_age_s": snap.last_event_age_s,
                "restarts": snap.restarts,
                "detail": snap.detail,
                "critical": True,
            }

        def asr_status_provider() -> dict:
            status_fn = getattr(asr, "runtime_status", None)
            if callable(status_fn):
                return status_fn()
            return {
                "ok": True,
                "status": "configured",
                "provider": type(asr).__name__,
                "locality": getattr(getattr(asr, "locality", None), "value", "unknown"),
            }

        def apply_config_change(_ev: ConfigChanged) -> None:
            nonlocal config, privacy, asr, llm, cleanup, command_router, command_executor
            nonlocal injector, hotkeys, service_engine
            with runtime_lock:
                next_config = load_config(config.source_path or default_config_path(), create=False)
                next_privacy = PrivacyMonitor.from_config(next_config)
                next_privacy.validate_cloud_consent()
                # Keep the loaded model across saves that cannot affect it. The
                # existing lifecycle binding stays with the reused provider, so
                # AsrReadyChanged keeps flowing without rebinding.
                asr_key = asr_reuse_key(next_config)
                if asr_key is not None and asr_key == asr_reuse_key(config):
                    next_asr = asr
                else:
                    next_asr = build_asr_provider(next_config, next_privacy, credential_store)
                    _bind_asr_lifecycle(
                        next_asr, bus, tray_notify, current_of=lambda: asr_current["asr"]
                    )
                llm_key = llm_reuse_key(next_config)
                if llm_key is not None and llm_key == llm_reuse_key(config):
                    next_llm = llm
                else:
                    next_llm = build_llm_provider(next_config, next_privacy, credential_store)
                next_injector = build_injector(next_config)
                next_cleanup = (
                    CleanupPipeline(
                        next_llm,
                        enabled=next_config.current_profile.cleanup_enabled,
                        dictionary=next_config.dictionary,
                        snippets=effective_snippets(next_config.snippets),
                        health_preflight=isinstance(next_llm, OpenAICompatProvider)
                        and next_llm.provider_name in {"ollama", "lmstudio"},
                    )
                    if next_llm is not None and next_config.current_profile.cleanup_enabled
                    else None
                )
                next_router = CommandRouter(next_llm)
                next_executor = CommandExecutor(next_injector)
                if next_config.audio.input_device != config.audio.input_device:
                    capture.set_device(next_config.audio.input_device)
                if next_config.audio.max_seconds != config.audio.max_seconds:
                    capture.max_seconds = next_config.audio.max_seconds
                    capture.stop()
                pipeline.update_runtime(
                    asr=next_asr,
                    injector=next_injector,
                    cleanup=next_cleanup,
                    command_router=next_router,
                    command_executor=next_executor,
                    config=build_pipeline_config(
                        next_config,
                        cleanup_enabled=next_cleanup is not None,
                        personalization=personalization,
                    ),
                )
                recovery.update_policy(next_config.recovery)
                personalization.update_policy(
                    enabled=next_config.personalization.enabled,
                    learn=next_config.personalization.learn,
                )
                if service_engine is not None:
                    with service_engine.lock:
                        service_engine.asr = next_asr
                        service_engine.cleanup = next_cleanup
                        service_engine.router = next_router
                        service_engine.privacy = next_privacy
                if hotkeys is not None and (
                    next_config.hotkeys != config.hotkeys
                    or next_config.audio.auto_stop_seconds != config.audio.auto_stop_seconds
                ):
                    hotkeys.stop(finalize_active=True)
                    hotkeys = HotkeyManager(
                        next_config.hotkeys,
                        bus,
                        stuck_timeout_s=next_config.audio.auto_stop_seconds + 5.0,
                    )
                    hotkeys.start()
                    hotkey_holder["manager"] = hotkeys
                if overlay is not None:
                    overlay.set_privacy(next_privacy.status.value)
                settings.api._update_runtime(next_config, next_privacy)
                wizard.settings_api._update_runtime(next_config, next_privacy)
                if tray is not None:
                    tray.refresh(next_config)
                if (
                    next_config.launch_at_startup != config.launch_at_startup
                    and _autostart_sync_enabled()
                ):
                    with contextlib.suppress(Exception):
                        autostart.set_enabled(next_config.launch_at_startup)
                if asr is not next_asr:
                    # Detach first so late events from the old provider are
                    # dropped, then release its model memory off this thread
                    # (an unload can block on the provider's inference lock).
                    asr_current["asr"] = next_asr
                    old_asr = asr
                    if callable(getattr(old_asr, "unload", None)):

                        def _unload_replaced(provider=old_asr) -> None:
                            with contextlib.suppress(Exception):
                                provider.unload()

                        threading.Thread(
                            target=_unload_replaced,
                            name="AsrReplacedUnload",
                            daemon=True,
                        ).start()
                if llm is not None and llm is not next_llm and hasattr(llm, "close"):
                    llm.close()
                if (
                    cleanup is not None
                    and cleanup is not next_cleanup
                    and hasattr(cleanup, "close")
                ):
                    cleanup.close()
                config = next_config
                privacy = next_privacy
                asr = next_asr
                llm = next_llm
                injector = next_injector
                cleanup = next_cleanup
                command_router = next_router
                command_executor = next_executor
                bus.publish(PrivacyChanged(privacy.status.value))

        def _apply_config_change_safe(ev: ConfigChanged) -> None:
            try:
                apply_config_change(ev)
            except ConsentRequired as exc:
                bus.publish(
                    PrivacyChanged(
                        "consent_required",
                        detail=str(exc),
                        consent_state="required",
                        reason="config_change_blocked_by_cloud_consent",
                        missing_providers=exc.missing,
                    )
                )
            except ConfigError as exc:
                logger.error("config reload failed; keeping previous settings: %s", exc)
                tray_notify("DCENT_Voice settings", f"Settings not applied: {exc}")
            except Exception:
                logger.exception("config reload failed; keeping previous settings")
                tray_notify(
                    "DCENT_Voice settings",
                    "Settings could not be applied — see the log. Previous settings remain active.",
                )

        runtime_reloader = CoalescingWorker(_apply_config_change_safe, name="ConfigApply")
        runtime_reloader.start()

        def on_runtime_event(ev) -> None:
            if isinstance(ev, ConfigChanged):
                # Rebuilding providers can load a multi-GB speech model; doing that
                # inline would stall the bus dispatcher and freeze hotkey handling
                # (RT-REL-1). Apply on a worker thread — runtime_lock serializes
                # overlapping applies, and each apply re-reads the config from disk,
                # so rapid saves converge on the on-disk state.
                runtime_reloader.request(ev)

        bus.subscribe(on_runtime_event)
        if config.service.enabled:
            # One session token secures the API + WebSockets and is written to the
            # ADE registry token file so the ADE host can authenticate.
            service_token = create_token()
            service_engine = ServiceEngine(
                asr=asr,
                cleanup=cleanup,
                router=command_router,
                privacy=privacy,
                personalization=personalization,
                snippets=effective_snippets(config.snippets),
                dictionary=tuple(config.dictionary),
                status_providers={
                    "asr": asr_status_provider,
                    "hotkeys": hotkeys_status_provider,
                    "pipeline": pipeline.status_snapshot,
                    "capture": capture.status_snapshot,
                },
            )
            service_app = create_app(service_engine, token=service_token)
            add_event_websocket(service_app, bus, token=service_token)
            add_stream_websocket(service_app, service_engine, token=service_token)

            # Local TTS: only advertised/served when a backend's model assets are
            # present (build_tts_backend returns None otherwise). The configured
            # half-duplex policy either pauses capture or ducks samples while TTS
            # speaks; a PTT press still cancels playback via barge_in.
            def make_tts_backend():
                """Create isolated mutable TTS state for one DVAP connection."""
                return build_tts_backend(config.tts)

            tts_available = make_tts_backend() is not None
            tts_mic_gate = _build_tts_mic_gate(
                capture,
                config,
                tts_available=tts_available,
            )
            # DVAP envelope for ADE: hello/welcome negotiation, STT family,
            # module.sovereignty from the privacy ledger, and (when TTS is
            # available) tts.append/tts.cancel playback + barge_in on PTT.
            add_dvap_websocket(
                service_app,
                service_engine,
                bus,
                token=service_token,
                tts_backend_factory=make_tts_backend,
                sink_factory=lambda: SoundDeviceSink(device=voice_control.output_device),
                mic_gate=tts_mic_gate,
                voice_control=voice_control,
                code_policy=_tts_code_policy(config.tts.skip_code),
            )
            service = ServiceThread(
                service_app,
                host=config.service.host,
                port=config.service.port,
            )
            service.start()
            # Do not advertise ADE discovery until the HTTP server is actually
            # accepting connections (port-in-use / bind failure leave a dead endpoint).
            if not service.wait_ready(timeout=5.0):
                err = service.error
                logger.error(
                    "local service failed to become ready on %s:%s (%s); "
                    "skipping ADE registry publish",
                    config.service.host,
                    config.service.port,
                    err or "timeout",
                )
                with contextlib.suppress(Exception):
                    tray_notify(
                        "DCENT_Voice service",
                        f"Local API failed to start on port {config.service.port}. "
                        "Dictation hotkeys still work; ADE attach is unavailable.",
                    )
                with contextlib.suppress(Exception):
                    service.stop()
                service = None
            else:
                registry_entry = None
                try:
                    registry_entry = create_registry_entry(
                        endpoint=format_http_base(config.service.host, config.service.port),
                        version=__version__,
                        pid=os.getpid(),
                        token=service_token,
                        tts_available=tts_available,
                        launch=build_launch_descriptor(),
                    )
                    write_registry_entry(registry_entry)
                except OSError as exc:
                    # A running attach API without a securely publishable bearer
                    # token is not a degraded mode: stop it and remove any partial
                    # registry/token artifacts instead of failing open.
                    logger.error(
                        "Could not secure ADE registry credentials; disabling local service: %s",
                        exc,
                    )
                    if registry_entry is not None:
                        with contextlib.suppress(OSError, ValueError):
                            remove_registry_entry(registry_entry)
                    with contextlib.suppress(Exception):
                        service.stop()
                    service = None
                    with contextlib.suppress(Exception):
                        tray_notify(
                            "DCENT_Voice service",
                            "Local API disabled because its private ADE token "
                            "could not be secured.",
                        )
        webview2_missing = sys.platform == "win32" and not _webview2_present()
        if not no_tray:

            def tray_set_profile(name: str) -> None:
                settings.api.set_config({"active_profile": name})

            def tray_set_cleanup(enabled: bool) -> None:
                # The pipeline uses the active profile's cleanup flag, so toggle
                # that (not the global) — routed through the normal reload path.
                active = settings.api._config.active_profile
                settings.api.set_config({"profile": {active: {"cleanup_enabled": enabled}}})

            def tray_open_log_folder() -> bool:
                from .ui.first_run import log_folder, open_folder

                return open_folder(log_folder())

            def tray_run_diagnostics() -> bool:
                # WS4 (`dcent_voice.doctor`) is a separate workstream: degrade to
                # a toast rather than a broken menu item if it is not present.
                try:
                    from .doctor import run_doctor_in_background
                except Exception:
                    logger.warning("diagnostics requested but the doctor module is unavailable")
                    tray_notify(
                        "DCENT_Voice diagnostics",
                        "Diagnostics are not available in this build. Use "
                        "'Open log folder' and send the log instead.",
                    )
                    return False
                # Returns a started daemon thread; it must not run here, because
                # this executes on pystray's event thread. The summary toast and
                # the folder are produced by that thread when it finishes.
                run_doctor_in_background(tray_notify)
                tray_notify(
                    "DCENT_Voice diagnostics",
                    "Collecting diagnostics… the report folder opens when it finishes.",
                    key="diagnostics-started",
                    min_interval_s=15.0,
                )
                return True

            def tray_install_webview2() -> bool:
                from .ui.first_run import open_webview2_download

                return open_webview2_download()

            tray = TrayApp(
                config=config,
                bus=bus,
                asr_ready=asr_ready_at_start,
                webview2_missing=webview2_missing,
                callbacks=TrayCallbacks(
                    set_profile=tray_set_profile,
                    set_cleanup_enabled=tray_set_cleanup,
                    open_settings=settings.open,
                    open_setup=wizard.open,
                    mark_first_run_education_shown=(
                        lambda: settings.api.mark_first_run_education_shown()
                    ),
                    open_log_folder=tray_open_log_folder,
                    run_diagnostics=tray_run_diagnostics,
                    install_webview2=tray_install_webview2,
                ),
            )
            tray.start()
            # Timestamped on purpose: paired with "parakeet ready" this line is
            # the evidence that the icon no longer waits for the model hash.
            logger.info(
                "tray started (asr_ready=%s) %.2fs after process start",
                asr_ready_at_start,
                time.perf_counter() - _APP_READY_STARTED,
            )
            # WS2 may have reset a corrupt settings file before we got here.
            # Say so out loud: silently different settings is its own bug.
            if config_recovery_notice:
                with contextlib.suppress(Exception):
                    tray_notify("DCENT_Voice settings", config_recovery_notice)
        if not no_hotkeys:
            # Stuck-key safety sits slightly above auto-stop so the pipeline owns
            # intentional max-hold finalization; stuck only clears orphaned chords.
            hotkeys = HotkeyManager(
                config.hotkeys,
                bus,
                stuck_timeout_s=config.audio.auto_stop_seconds + 5.0,
            )
            hotkeys.start()
            hotkey_holder["manager"] = hotkeys

            def _on_session_resume(reason: str) -> None:
                if hotkeys is not None:
                    hotkeys.force_rebind(reason=reason)

            session_monitor = SessionResumeMonitor(_on_session_resume)
            session_monitor.start()

        logger.info("DCENT_Voice running. Press Ctrl+C to quit.")
        # Hold-to-talk is live at this point (hotkeys.start above). Only now may
        # a first-run window appear — education must never delay dictation.
        open_initial_wizard = auto_open_first_run_wizard(
            no_tray=no_tray,
            first_run_education_shown=not first_run,
        )

        def _persist_first_run_shown() -> None:
            with contextlib.suppress(Exception):
                settings.api.mark_first_run_education_shown()

        if open_initial_wizard:
            # Persist when the wizard closes as well as when "Finish setup" is
            # pressed, so a user who closes the window never sees it again.
            wizard.on_closed = _persist_first_run_shown
            # Do not call wizard.open() on this thread: its readiness callback
            # waits for pywebview's persistent master window, while webview.start
            # must run here on macOS. Request the host now and create the wizard
            # from webview's ready callback after the master exists.
            gui_gate.requested.set()
        elif first_run and not no_tray:
            _start_first_run_dialog(
                config,
                webview2_missing=webview2_missing,
                on_shown=_persist_first_run_shown,
                logger=logger,
            )

        def _open_initial_wizard() -> None:
            if not open_initial_wizard or shutdown.is_set():
                return
            try:
                if not wizard.open():
                    raise RuntimeError("pywebview is unavailable")
            except Exception:
                logger.exception("first-run setup window failed to open")
                tray_notify(
                    "DCENT_Voice setup",
                    "Setup could not open. Use the tray menu to try again.",
                )
                # A first launch that shows nothing is the whole bug class WS3
                # exists to remove. On Linux/macOS the wizard *is* the first-run
                # surface (there is no WebView2 pre-check to route around it), so
                # when it cannot render — no WebKitGTK, no display — fall back to
                # the same native education the Windows no-WebView2 host gets.
                if sys.platform != "win32":
                    _start_first_run_dialog(
                        config,
                        webview2_missing=False,
                        gui_missing=True,
                        on_shown=_persist_first_run_shown,
                        logger=logger,
                    )
                # The balloon above can be missed while the icon is still
                # settling into the notification area. Say where Setup lives a
                # few seconds later, once the icon is certainly drawn.
                reminder = threading.Timer(
                    5.0,
                    lambda: tray_notify("DCENT_Voice", FIRST_RUN_TRAY_REMINDER),
                )
                reminder.daemon = True
                reminder.start()

        if overlay is not None:
            if (
                not overlay_created
                and config.overlay.lazy
                and _wait_for_lazy_overlay_host(
                    shutdown, gui_gate.requested, overlay.start_requested
                )
            ):
                overlay_created = overlay.create_window()
                if overlay_created:
                    overlay.set_privacy(privacy.status.value)
                else:
                    logger.warning(
                        "pywebview overlay unavailable on first use; continuing without overlay."
                    )
            if overlay_created:
                _run_overlay_until_shutdown(
                    shutdown=shutdown,
                    overlay=overlay,
                    pipeline=pipeline,
                    notify=tray_notify,
                    logger=logger,
                    ui_controllers=(settings, wizard),
                    gui_gate=gui_gate,
                    on_host_ready=_open_initial_wizard,
                )
            elif not shutdown.is_set():
                pipeline.overlay = None
                # A transparent overlay may fail on a backend where an ordinary
                # Settings window still works. Keep waiting for a UI request and
                # fall back to a hidden master host instead of making one overlay
                # failure permanently disable Settings for this process.
                if _wait_for_gui_or_shutdown(shutdown, gui_gate.requested):
                    _run_ui_host_until_shutdown(
                        shutdown=shutdown,
                        ui_controllers=(settings, wizard),
                        logger=logger,
                        gui_gate=gui_gate,
                        on_host_ready=_open_initial_wizard,
                    )
        else:
            if _wait_for_gui_or_shutdown(shutdown, gui_gate.requested):
                _run_ui_host_until_shutdown(
                    shutdown=shutdown,
                    ui_controllers=(settings, wizard),
                    logger=logger,
                    gui_gate=gui_gate,
                    on_host_ready=_open_initial_wizard,
                )
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received.")
    finally:
        logger.info("Shutting down DCENT_Voice.")
        # Per-step suppress so one teardown failure cannot skip lock release.
        if runtime_reloader is not None and not runtime_reloader.stop(timeout=10.0):
            logger.error("config apply worker did not stop before teardown")
        if session_monitor is not None:
            with contextlib.suppress(Exception):
                session_monitor.stop()
        if hotkeys is not None:
            with contextlib.suppress(Exception):
                hotkeys.stop()
        if voice_control is not None:
            with contextlib.suppress(Exception):
                voice_control.close()
        if service is not None:
            with contextlib.suppress(Exception):
                service.stop()
        if registry_entry is not None:
            with contextlib.suppress(Exception):
                remove_registry_entry(registry_entry)
        if pipeline is not None:
            with contextlib.suppress(Exception):
                pipeline.stop()
        if tray is not None:
            with contextlib.suppress(Exception):
                tray.stop()
        if overlay is not None:
            with contextlib.suppress(Exception):
                overlay.destroy()
        if llm is not None and hasattr(llm, "close"):
            with contextlib.suppress(Exception):
                llm.close()
        if cleanup is not None and hasattr(cleanup, "close"):
            with contextlib.suppress(Exception):
                cleanup.close()
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.stop()
        with contextlib.suppress(Exception):
            instance_lock.release()
    return 0


def handle_already_running(
    exc: AlreadyRunningError,
    *,
    config: AppConfig | None = None,
    run_kwargs: dict | None = None,
    _retry: bool = True,
) -> int:
    """Surface an already-running instance so launches don't look broken.

    If the lock points at a dead process (crash left a stale file), clear it and
    auto-retry start once so autostart / double-click does not exit 0 with no
    running instance.
    """
    log_path = default_log_path()
    # Stale lock after a crash: reclaim and retry start once.
    if force_clear_stale_lock():
        print("Cleared a stale DCENT_Voice lock (previous process had exited).")
        if _retry and config is not None:
            print("Retrying start after stale lock clear…")
            try:
                return run_app(config, **(run_kwargs or {}))
            except AlreadyRunningError as again:
                return handle_already_running(
                    again, config=config, run_kwargs=run_kwargs, _retry=False
                )
            except ConsentRequired:
                raise
        print("Starting was blocked by a leftover lock file; please launch again.")
        if platform.system() == "Windows":
            with contextlib.suppress(Exception):
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0,
                    "DCENT_Voice had stopped, but a leftover lock file blocked restart.\n\n"
                    "That lock was cleared. Please start DCENT_Voice again.\n\n"
                    f"Log: {log_path}",
                    "DCENT_Voice",
                    0x40,  # MB_ICONINFORMATION
                )
        return 0

    message = (
        f"{exc}\n\n"
        "DCENT_Voice runs in the system tray (notification area).\n"
        "Look for the brand icon near the clock, or open Settings now.\n\n"
        f"Log: {log_path}"
    )
    print(f"ERROR: {exc}")
    print("DCENT_Voice is already running in the system tray.")
    print(f"Log: {log_path}")
    # Visible dialog when launched from Explorer / shortcut (no console).
    if platform.system() == "Windows":
        with contextlib.suppress(Exception):
            import ctypes

            MB_ICONINFORMATION = 0x40
            MB_YESNO = 0x4
            IDYES = 6
            result = ctypes.windll.user32.MessageBoxW(
                0,
                message + "\n\nOpen Settings now?",
                "DCENT_Voice",
                MB_YESNO | MB_ICONINFORMATION,
            )
            if result == IDYES:
                try:
                    # Settings path does not take the single-instance lock.
                    run_settings(config if config is not None else load_config())
                except ConfigError as config_exc:
                    # "Already running" plus an unreadable config is the one
                    # combination that used to end in a swallowed exception and
                    # a silent exit 0. Name it.
                    return report_fatal(
                        "DCENT_Voice is already running, but its configuration is unreadable",
                        f"{config_exc}\n\nRun `dcent-voice doctor` for a full diagnosis.",
                        log_path=log_path,
                        exit_code=2,
                        exc=config_exc,
                    )
                except Exception:
                    bootlog.logger().exception("could not open settings")
                return 0
            return 0
    return 1


def run_self_test(config: AppConfig) -> int:
    """Verify the configured local ASR model and hotkey listener liveness."""
    _bind_runtime_names("HotkeyManager")
    logger = configure_logging()
    bus = EventBus()
    bus.start()
    manager = HotkeyManager(config.hotkeys, bus)
    asr = None
    try:
        asr = build_asr_provider(config)
        asr.load()
        print(f"asr.model={config.current_profile.asr.model}")
        print("asr.status=ready")
        manager.start()
        time.sleep(0.5)
        snap = manager.status()
        print(f"hotkeys.status={snap.status}")
        print(f"hotkeys.listener_running={snap.listener_running}")
        print(f"hotkeys.detail={snap.detail}")
        print(f"hotkeys.dictation={config.hotkeys.dictation}")
        ok = snap.listener_running and snap.status == "ok"
        logger.info("self-test %s status=%s", "PASS" if ok else "FAIL", snap.status)
        return 0 if ok else 1
    except Exception as exc:
        print(f"self-test FAIL: {type(exc).__name__}: {exc}")
        logger.exception("self-test failed")
        return 1
    finally:
        with contextlib.suppress(Exception):
            manager.stop()
        if asr is not None:
            with contextlib.suppress(Exception):
                asr.unload()
        bus.stop()


def asr_reuse_key(config: AppConfig) -> tuple | None:
    """Identity of a *local* ASR provider built from ``config``, else ``None``.

    Saving any setting republishes ``ConfigChanged``, and the reload used to
    rebuild the ASR provider unconditionally — so persisting
    ``first_run_education_shown`` swapped the freshly loaded model for a cold
    one and the ~670 MB load ran a second time (``/health`` reported
    ``model_loaded=False`` until the next utterance). Two configs with equal,
    non-``None`` keys describe the same provider, so the loaded one can be kept.

    ``None`` means "always rebuild". Cloud providers return it deliberately:
    rebuilding is what re-runs ``validate_cloud_consent()``, picks up a rotated
    API key from the credential store, and rebinds the egress logger to the new
    ``PrivacyMonitor``. None of that may be skipped to save a cheap HTTP client,
    and the expensive resource this exists to protect — a multi-hundred-MB local
    model — is never involved in the cloud case.
    """
    profile = config.current_profile
    if profile.asr.locality is Locality.CLOUD:
        return None
    return (
        profile.asr.raw,
        profile.language,
        config.language_mode,
        config.idle_unload_s,
    )


def llm_reuse_key(config: AppConfig) -> tuple | None:
    """Identity of a *local* LLM provider built from ``config``, else ``None``.

    Same contract and the same cloud carve-out as :func:`asr_reuse_key`.
    """
    spec = config.current_profile.llm
    if not spec.enabled or spec.locality is Locality.CLOUD:
        return None
    return (spec.raw, spec.enabled)


def build_asr_provider(
    config: AppConfig,
    privacy: PrivacyMonitor | None = None,
    credential_store: CredentialStore | None = None,
):
    _bind_runtime_names("build_asr_from_spec")
    profile = config.current_profile
    if profile.asr.locality is Locality.CLOUD:
        if privacy is None:
            raise ConsentRequired((f"asr:{profile.asr.provider}",))
        privacy.validate_cloud_consent()
    egress_logger = None
    if privacy is not None:

        def egress_logger(provider_key, payload_type, byte_count):
            privacy.record_egress(
                provider_key,
                payload_type=payload_type,
                byte_count=byte_count,
            )

    api_key = (
        credential_store.get_secret(profile.asr.provider, "api_key") if credential_store else None
    )
    return build_asr_from_spec(
        profile.asr,
        language=profile.language,
        language_mode=config.language_mode,
        api_key=api_key,
        egress_logger=egress_logger,
        idle_unload_s=config.idle_unload_s,
    )


_sticky_cli_compose_style: str | None = None
_sticky_cli_compose_no_polish: bool | None = None


def reset_cli_compose_sticky() -> None:
    """Clear process-level CLI compose style/polish sticky."""
    global _sticky_cli_compose_style, _sticky_cli_compose_no_polish
    _sticky_cli_compose_style = None
    _sticky_cli_compose_no_polish = None


def run_compose_command(
    config: AppConfig,
    args: argparse.Namespace,
    *,
    personalization: PersonalizationStore | None = None,
) -> int:
    """Compose text through the same local path used by desktop dictation."""
    from dcent_voice.dictation.postprocess import compose_dictation
    from dcent_voice.dictation.style import normalize_style

    global _sticky_cli_compose_style, _sticky_cli_compose_no_polish
    # --style / --no-polish set on an earlier in-process call remain when omitted.
    if getattr(args, "style", None) is not None:
        _sticky_cli_compose_style = args.style
    raw_no_polish = getattr(args, "no_polish", None)
    if raw_no_polish is True:
        _sticky_cli_compose_no_polish = True
    style_arg = (
        args.style if getattr(args, "style", None) is not None else _sticky_cli_compose_style
    )
    no_polish = (
        True
        if raw_no_polish is True
        else bool(_sticky_cli_compose_no_polish)
        if raw_no_polish is None
        else False
    )
    try:
        style = normalize_style(style_arg)
    except ValueError as exc:
        print(f"compose failed: {exc}", file=sys.stderr)
        return 2
    # Pass --no-polish alone for raw postprocess skip without style rewrite.
    # With --style, compose still runs and honors --no-polish (no local filler strip).
    dictionary = tuple(config.dictionary)
    as_vocab = getattr(personalization, "as_vocab", None)
    if callable(as_vocab):
        with contextlib.suppress(Exception):
            dictionary = dictionary + tuple(
                as_vocab(style=style, app=getattr(args, "app_context", None))
            )
    print(
        compose_dictation(
            " ".join(args.text),
            style=style,
            snippets=effective_snippets(config.snippets),
            dictionary=dictionary,
            polish=not no_polish,
            cleanup_level=args.cleanup_level,
        )
    )
    return 0


def run_payload_command(args: argparse.Namespace) -> int:
    """Model-integrity CLI used by source and frozen package/install paths."""
    from dcent_voice.asr.model_registry import (
        stage_verified_payload,
        verify_shipped_payload,
    )

    try:
        if args.command == "verify-payload":
            root = args.payload.resolve()
            verify_shipped_payload(root)
            print(f"verified payload: {root}")
        else:
            source = args.source.resolve()
            destination = args.destination.resolve()
            staged = stage_verified_payload(source, destination)
            print(f"staged verified payload: {staged}")
    except Exception as exc:
        print(f"{args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def build_pipeline_config(
    config: AppConfig,
    *,
    cleanup_enabled: bool,
    personalization: PersonalizationStore | None,
) -> PipelineConfig:
    """Resolve the desktop pipeline policy for app and integration harnesses."""

    _bind_runtime_names("PipelineConfig")
    return PipelineConfig(
        cleanup_enabled=cleanup_enabled,
        dictionary=config.dictionary,
        snippets=effective_snippets(config.snippets),
        local_polish=config.dictation.local_polish,
        spoken_edits=config.dictation.spoken_edits,
        developer_terms=config.dictation.developer_terms,
        cleanup_level=config.dictation.cleanup_level,
        focus_guard_enabled=True,
        max_utterance_s=config.audio.auto_stop_seconds,
        personalization=personalization,
        personalization_prose_context=config.personalization.prose_context,
        style_default=config.style.default,
        style_per_app=dict(config.style.per_app),
        language_mode=config.language_mode,
        language=config.language,
    )


_sticky_cli_transcribe_style: str | None = None
_sticky_cli_transcribe_no_polish: bool | None = None


def reset_cli_transcribe_sticky() -> None:
    """Clear process-level CLI transcribe style/polish sticky."""
    global _sticky_cli_transcribe_style, _sticky_cli_transcribe_no_polish
    _sticky_cli_transcribe_style = None
    _sticky_cli_transcribe_no_polish = None


def run_transcribe_command(
    config: AppConfig,
    args: argparse.Namespace,
    *,
    personalization: PersonalizationStore | None = None,
) -> int:
    """Transcribe a WAV through the headless local pipeline."""
    from dcent_voice.engine import VoiceEngine, load_wav_mono

    global _sticky_cli_transcribe_style, _sticky_cli_transcribe_no_polish
    command_started = time.perf_counter()
    if args.asr:
        from dataclasses import replace

        from dcent_voice.config import ASRSpec

        profile = replace(config.current_profile, asr=ASRSpec.parse(args.asr))
        profiles = dict(config.profiles)
        profiles[config.active_profile] = profile
        config = replace(config, profiles=profiles)
    # --style / --no-polish set on an earlier in-process call remain when omitted.
    if args.style is not None:
        _sticky_cli_transcribe_style = args.style
    if args.no_polish is True:
        _sticky_cli_transcribe_no_polish = True
    style = args.style if args.style is not None else _sticky_cli_transcribe_style
    no_polish = (
        True
        if args.no_polish is True
        else bool(_sticky_cli_transcribe_no_polish)
        if args.no_polish is None
        else False
    )
    store = (
        personalization if personalization is not None else getattr(args, "personalization", None)
    )
    audio_load_s = 0.0
    model_load_s = 0.0
    transcribe_s = 0.0
    unload_s = 0.0
    engine = None
    try:
        engine = VoiceEngine(
            config,
            asr=getattr(args, "asr_provider", None),
            personalization=store,
            polish=not no_polish,
        )
        started = time.perf_counter()
        audio, samplerate = load_wav_mono(args.audio)
        audio_load_s = time.perf_counter() - started
        started = time.perf_counter()
        engine.load()
        model_load_s = time.perf_counter() - started
        started = time.perf_counter()
        transcribe_kwargs = {
            "samplerate": samplerate,
            "language": args.language,
            "prose_context": args.prose_context,
            "style": style,
        }
        app_context = getattr(args, "app_context", None)
        if app_context is not None:
            transcribe_kwargs["app_context"] = app_context
        result = engine.transcribe(audio, **transcribe_kwargs)
        transcribe_s = time.perf_counter() - started
    except Exception as exc:
        print(f"transcribe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        started = time.perf_counter()
        if engine is not None:
            engine.unload()
        unload_s = time.perf_counter() - started
    payload = result.to_dict()
    payload["cli_measurement"] = {
        "schema_version": 1,
        "scope": "headless_transcribe_process",
        "frozen": bool(getattr(sys, "frozen", False)),
        "audio_load_s": audio_load_s,
        "model_load_s": model_load_s,
        "transcribe_s": transcribe_s,
        "unload_s": unload_s,
        # Starts once dcent_voice.app is ready. An external process timer is
        # required to include the frozen bootloader, imports, and Python startup.
        "since_app_ready_s": time.perf_counter() - _APP_READY_STARTED,
        "command_s": time.perf_counter() - command_started,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output_json is not None:
        args.output_json.write_text(rendered + "\n", encoding="utf-8")
    elif args.json:
        print(rendered)
    else:
        print(result.text)
    return 1 if result.rejected_reason else 0


def run_learn_command(
    config: AppConfig,
    args: argparse.Namespace,
    *,
    personalization: PersonalizationStore | None = None,
) -> int:
    """Record an app-scoped typed correction without audio."""
    from dcent_voice.engine import VoiceEngine

    try:
        engine = VoiceEngine(config, personalization=personalization)
        if args.correction:
            result = engine.learn_last(
                args.correction, style=args.style, app_context=args.app_context
            )
        elif args.spoken and args.written:
            result = engine.learn(
                args.spoken,
                args.written,
                style=args.style,
                app_context=args.app_context,
            )
        elif args.style and args.app_context:
            result = engine.remember_app_style(args.app_context, args.style)
        else:
            print(
                "learn requires --from and --to, --last, or --style and --app",
                file=sys.stderr,
            )
            return 2
    except Exception as exc:
        print(f"learn failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output_json is not None:
        args.output_json.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if result.get("ok") else 1


def run_engine_info(config: AppConfig) -> int:
    from dcent_voice.engine import VoiceEngine

    try:
        engine = VoiceEngine(config)
        print(json.dumps(engine.capabilities(), indent=2, sort_keys=True))
    except ConsentRequired as exc:
        print(
            "engine-info failed: cloud provider consent is required for " + ", ".join(exc.missing),
            file=sys.stderr,
        )
        return 1
    return 0


def run_platform_check() -> int:
    """Import the native desktop backend without opening windows or devices."""
    system = platform.system()
    try:
        if system == "Linux":
            import gi

            gi.require_version("Gdk", "3.0")
            gi.require_version("Gtk", "3.0")
            webkit_version, _soup_version = _require_linux_webkit(gi)
            from gi.repository import Gdk, Gio, GLib, Gtk, Soup, WebKit2
            from webview.platforms import gtk as webview_gtk

            _ = (Gdk, Gio, GLib, Gtk, Soup, WebKit2, webview_gtk)
            backend = f"gtk-webkit2-{webkit_version}"
        elif system == "Darwin":
            import AppKit
            import Quartz
            import Security
            import WebKit
            import webview.platforms.cocoa

            _ = (AppKit, Quartz, Security, WebKit, webview.platforms.cocoa)
            backend = "cocoa"
        elif system == "Windows":
            import uiautomation
            import webview.platforms.winforms
            import win32gui

            _ = (uiautomation, webview.platforms.winforms, win32gui)
            backend = "winforms"
        else:
            raise RuntimeError(f"unsupported desktop platform: {system}")
    except Exception as exc:
        print(
            json.dumps(
                {
                    "backend": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "platform": system,
                    "status": "error",
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {"backend": backend, "platform": system, "status": "ready"},
            sort_keys=True,
        )
    )
    return 0


def _restore_frozen_linux_library_path() -> None:
    """Keep host WebKit renderer processes on the host shared-library ABI."""

    if platform.system() != "Linux" or not getattr(sys, "frozen", False):
        return
    original = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if original is None:
        os.environ.pop("LD_LIBRARY_PATH", None)
    else:
        os.environ["LD_LIBRARY_PATH"] = original


def _require_linux_webkit(gi_module: Any) -> tuple[str, str]:
    """Select pywebview's preferred WebKit/Soup ABI with a Jammy fallback."""
    from .doctor.probe import LINUX_WEBKIT_ABIS

    first_error: ValueError | None = None
    for webkit_version, soup_version in LINUX_WEBKIT_ABIS:
        try:
            gi_module.require_version("WebKit2", webkit_version)
            gi_module.require_version("Soup", soup_version)
        except ValueError as exc:
            if first_error is None:
                first_error = exc
            continue
        return webkit_version, soup_version
    raise RuntimeError("GTK requires WebKit2 4.1/Soup 3.0 or WebKit2 4.0/Soup 2.4") from first_error


def _bind_asr_lifecycle(asr, bus: EventBus, notify, *, current_of=None) -> None:
    """Publish ASR readiness for ``asr`` while it is the wired provider.

    ``current_of`` is a zero-arg callable returning the provider the app is
    currently using. A config reload can replace the provider while a
    background load of the old one is still in flight; without this guard the
    replaced provider's late ``ready`` event would make the tray claim a model
    is ready that is no longer wired to the pipeline.
    """
    setter = getattr(asr, "set_lifecycle_listener", None)
    if not callable(setter):
        return

    def _on_ready(ready: bool, detail: str) -> None:
        if current_of is not None and current_of() is not asr:
            return
        bus.publish(AsrReadyChanged(ready, detail))
        if not ready:
            notify(
                "DCENT_Voice",
                "Speech model unloaded to save RAM. Next dictation will show Loading.",
                key="asr-unload",
                min_interval_s=30.0,
            )

    setter(_on_ready)


def _warn_if_asr_unready(asr, logger, *, background: bool = False) -> threading.Thread | None:
    """Load the configured model so the first PTT cannot fail silently.

    ``background=True`` (desktop startup) runs the load on a daemon thread:
    verifying and mapping the shipped Parakeet snapshot is a ~670 MB SHA-256
    pass, and doing it inline delayed the tray icon by seconds on a cold disk —
    the "nothing happens when I launch it" report. Readiness still reaches the
    UI through the provider's lifecycle listener (``AsrReadyChanged``), and a
    hotkey pressed before the load finishes records normally and transcribes
    once the model is resident. ``--self-test`` keeps its own synchronous load.
    """

    def _load() -> None:
        try:
            asr.load()
        except Exception as exc:
            logger.warning(
                "ASR startup check failed; dictation is unavailable until the "
                "model issue is fixed: %s",
                exc,
            )

    if not background:
        _load()
        return None
    thread = threading.Thread(target=_load, name="AsrStartupLoad", daemon=True)
    thread.start()
    logger.info("ASR model load started in the background; tray and hotkeys do not wait")
    return thread


def run_settings(config: AppConfig) -> int:
    _bind_runtime_names("SettingsController")
    configure_logging()
    privacy = PrivacyMonitor.from_config(config)
    bus = EventBus()
    bus.start()
    controller = SettingsController(
        config=config,
        bus=bus,
        privacy=privacy,
        credential_store=build_credential_store(None),
    )
    if not controller.open():
        bus.stop()
        return _report_missing_gui_runtime("settings dashboard")
    try:
        import webview

        webview.start()
    finally:
        bus.stop()
    return 0


def _report_missing_gui_runtime(surface: str) -> int:
    """Surface a missing WebView2/pywebview host as a dialog, not a lost print().

    ``--settings`` / ``--setup`` on the windowed frozen build have no stdout, so
    ``print(missing_windows_webview2_message())`` told the user nothing at all —
    the window simply never appeared.
    """
    from dcent_voice.ui.webview_runtime import (
        missing_windows_webview2_message,
        windows_webview2_runtime_present,
    )

    if sys.platform == "win32" and not windows_webview2_runtime_present():
        detail = missing_windows_webview2_message()
    else:
        detail = f"pywebview is required for the {surface}."
    return report_fatal(
        f"DCENT_Voice could not open the {surface}",
        detail,
        log_path=default_log_path(),
        exit_code=1,
    )


def run_setup(config: AppConfig) -> int:
    _bind_runtime_names("WizardController")
    configure_logging()
    privacy = PrivacyMonitor.from_config(config)
    bus = EventBus()
    bus.start()
    controller = WizardController(
        config=config,
        bus=bus,
        privacy=privacy,
        credential_store=build_credential_store(None),
    )
    if not controller.open():
        bus.stop()
        return _report_missing_gui_runtime("setup wizard")
    try:
        import webview

        webview.start()
    finally:
        bus.stop()
    return 0


def build_llm_provider(
    config: AppConfig,
    privacy: PrivacyMonitor | None = None,
    credential_store: CredentialStore | None = None,
) -> OpenAICompatProvider | AnthropicProvider | None:
    _bind_runtime_names("AnthropicProvider", "OpenAICompatProvider")
    spec = config.current_profile.llm
    if not spec.enabled:
        return None
    if spec.locality is Locality.CLOUD:
        if privacy is None:
            raise ConsentRequired((f"llm:{spec.provider}",))
        privacy.validate_cloud_consent()
    egress_logger = None
    if privacy is not None:

        def egress_logger(provider_key, payload_type, byte_count):
            privacy.record_egress(
                provider_key,
                payload_type=payload_type,
                byte_count=byte_count,
            )

    api_key = _effective_credential(spec.provider, credential_store)
    if spec.provider == "anthropic":
        return AnthropicProvider(spec, api_key=api_key, egress_logger=egress_logger)
    return OpenAICompatProvider(spec, api_key=api_key, egress_logger=egress_logger)


def _effective_credential(provider: str, credential_store: CredentialStore | None) -> str | None:
    """Prefer an OAuth access token (account sign-in) over a stored API key."""
    if credential_store is None:
        return None
    from .auth.oauth import OAuthAuth

    token = OAuthAuth(credential_store).token(provider)
    if token is not None and token.access_token:
        return token.access_token
    return credential_store.get_secret(provider, "api_key")


def build_injector(config: AppConfig) -> RoutingInjector:
    _bind_runtime_names(
        "ClipboardPasteInjector",
        "Injector",
        "PynputTypeInjector",
        "RoutingInjector",
        "WindowsSendInputInjector",
    )
    restore = config.injector.restore_clipboard
    system = platform.system()
    if system == "Darwin":
        from .inject.macos import MacOSClipboardPasteInjector

        clipboard: Injector = MacOSClipboardPasteInjector(restore_previous=restore)
    elif system == "Linux":
        from .inject.linux import LinuxClipboardPasteInjector

        clipboard = LinuxClipboardPasteInjector(restore_previous=restore)
    else:
        clipboard = ClipboardPasteInjector(
            restore_previous=restore,
            paste_delay_s=config.injector.paste_delay_s,
            paste_min_delay_s=config.injector.paste_min_delay_s,
        )
    keystroke = WindowsSendInputInjector() if system == "Windows" else PynputTypeInjector()
    injectors: dict[str, Injector] = {"clipboard": clipboard, "keystroke": keystroke}
    return RoutingInjector(
        default_name=config.injector.default,
        injectors=injectors,
        per_app=config.injector.per_app,
        short_text_keystroke_chars=config.injector.short_text_keystroke_chars,
    )


def build_credential_store(logger=None) -> CredentialStore | None:
    _bind_runtime_names("CredentialStore")
    try:
        return CredentialStore()
    except RuntimeError as exc:
        if logger is not None:
            logger.warning("Credential store unavailable: %s", exc)
        return None


def _wait_for_shutdown(
    shutdown: threading.Event,
    overlay: OverlayController | None,
    ui_controllers: tuple[Any, ...] = (),
    extra_windows: tuple[Any, ...] = (),
) -> None:
    try:
        shutdown.wait()
    finally:
        if overlay is not None:
            overlay.destroy()
        for controller in ui_controllers:
            close = getattr(controller, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
                continue
            window = getattr(controller, "window", None)
            if window is not None:
                with contextlib.suppress(Exception):
                    window.destroy()
                controller.window = None
        for window in extra_windows:
            with contextlib.suppress(Exception):
                window.destroy()


def _run_overlay_until_shutdown(
    *,
    shutdown: threading.Event,
    overlay: OverlayController,
    pipeline: PipelineWorker,
    notify,
    logger,
    ui_controllers: tuple[Any, ...] = (),
    gui_gate: _GuiHostGate | None = None,
    on_host_ready: Any | None = None,
) -> None:
    """Run the optional GUI without making it a process-liveness dependency."""

    def _host_ready() -> None:
        if gui_gate is not None:
            gui_gate.mark_ready()
        if on_host_ready is not None:
            on_host_ready()
        _wait_for_shutdown(shutdown, overlay, ui_controllers)

    loop_failed = False
    try:
        overlay.start_event_loop(_host_ready)
    except Exception:
        if shutdown.is_set():
            return
        loop_failed = True
        logger.exception(
            "overlay GUI loop failed; continuing headless (log: %s)",
            default_log_path(),
        )
    if shutdown.is_set():
        return

    if gui_gate is not None:
        gui_gate.mark_unavailable()

    if not loop_failed:
        logger.error(
            "overlay event loop exited unexpectedly; continuing headless "
            "with tray, hotkeys, and local service (log: %s)",
            default_log_path(),
        )
    # Stop future pipeline events from targeting the dead native window. The
    # WebView loop is already gone, so detach without invoking native destroy.
    pipeline.overlay = None
    overlay.detach()
    with contextlib.suppress(Exception):
        notify(
            "DCENT_Voice overlay stopped",
            "Dictation is still running. Restart the app to restore the visual overlay.",
        )
    _wait_for_shutdown(shutdown, None)


def _wait_for_lazy_overlay_host(
    shutdown: threading.Event,
    gui_requested: threading.Event,
    overlay_start: threading.Event,
) -> bool:
    """Block until first dictation, Settings, or shutdown. No timeout poll."""
    wait_any(shutdown, gui_requested, overlay_start)
    return not shutdown.is_set()


def _wait_for_gui_or_shutdown(shutdown: threading.Event, gui_requested: threading.Event) -> bool:
    wait_any(shutdown, gui_requested)
    return (not shutdown.is_set()) and gui_requested.is_set()


class _GuiHostGate:
    """Synchronize background tray callbacks with pywebview's main-thread host.

    pywebview requires ``start`` on the main thread on macOS, while tray menu
    callbacks arrive on another thread. Controllers call :meth:`request` before
    creating a window; that wakes the main thread and waits until a persistent
    master window is running. This prevents the user-facing window from becoming
    the master whose close can terminate the entire GUI loop.
    """

    def __init__(self, shutdown: threading.Event, *, startup_timeout_s: float = 15.0) -> None:
        self.shutdown = shutdown
        self.startup_timeout_s = startup_timeout_s
        self.requested = threading.Event()
        self._ready = threading.Event()
        self._unavailable = threading.Event()

    def request(self) -> None:
        if self._unavailable.is_set():
            raise RuntimeError("The desktop UI host is unavailable.")
        if self.shutdown.is_set():
            raise RuntimeError("The application is shutting down.")
        self.requested.set()
        deadline = time.monotonic() + self.startup_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("The desktop UI host did not start in time.")
            wait_any(
                self._ready,
                self._unavailable,
                self.shutdown,
                timeout=remaining,
            )
            # Recheck terminal states after observing readiness. Shutdown or an
            # event-loop failure may race the wakeup; window creation must not
            # begin against a host that is already tearing down.
            if self._unavailable.is_set():
                raise RuntimeError("The desktop UI host is unavailable.")
            if self.shutdown.is_set():
                raise RuntimeError("The application is shutting down.")
            if self._ready.is_set():
                return

    def mark_ready(self) -> None:
        self._ready.set()

    def mark_unavailable(self) -> None:
        self._ready.clear()
        self._unavailable.set()


def _run_ui_host_until_shutdown(
    *,
    shutdown: threading.Event,
    ui_controllers: tuple[Any, ...],
    logger,
    gui_gate: _GuiHostGate | None = None,
    on_host_ready: Any | None = None,
) -> None:
    """Host Settings/Wizard when the visual dictation overlay is disabled."""
    try:
        import webview

        host = webview.create_window(
            "DCENT_Voice UI host",
            html="<html></html>",
            width=1,
            height=1,
            hidden=True,
            frameless=True,
            focus=False,
        )

        def _host_ready() -> None:
            if gui_gate is not None:
                gui_gate.mark_ready()
            if on_host_ready is not None:
                on_host_ready()
            _wait_for_shutdown(shutdown, None, ui_controllers, extra_windows=(host,))

        webview.start(_host_ready)
    except Exception:
        if gui_gate is not None:
            gui_gate.mark_unavailable()
        logger.exception("settings UI host failed; continuing headless")
    if not shutdown.is_set():
        if gui_gate is not None:
            gui_gate.mark_unavailable()
        logger.warning("settings UI loop exited; voice runtime remains active")
        _wait_for_shutdown(shutdown, None, ui_controllers)
