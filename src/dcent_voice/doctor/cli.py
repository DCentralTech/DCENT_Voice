# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""``dcent-voice doctor`` — run every check, write the bundle, say what to do.

Doctor is dispatched *before* the configuration is loaded, so it works on the
machine where nothing else does: a corrupt config, a missing payload file, a
broken native DLL. Nothing here may raise past :func:`main`; an unexpected error
becomes exit code 2 ("could not run"), never a silent vanish — which is the very
failure mode this command exists to explain.

Exit codes: ``0`` all checks passed or warned, ``1`` at least one failed,
``2`` doctor itself could not complete.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .result import FAIL, PASS, CheckResult, exit_code_for, summarize

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_COULD_NOT_RUN = 2

#: Honoured by every dialog doctor might show, so automation never blocks.
NO_DIALOGS_ENV = "DCENT_VOICE_NO_DIALOGS"

#: Suppresses opening a file manager, for the same reason as NO_DIALOGS_ENV.
NO_OPEN_ENV = "DCENT_VOICE_NO_OPEN"


@dataclass
class DoctorOutcome:
    """Everything a caller (CLI, tray, installer) needs after a run."""

    results: list[CheckResult]
    report: dict[str, Any]
    files: dict[str, Path] = field(default_factory=dict)

    @property
    def summary(self) -> dict[str, Any]:
        return self.report["summary"]

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [result for result in self.results if result.status == FAIL]

    def headline(self) -> str:
        counts = self.summary
        line = (
            f"{counts['status'].upper()}: {counts['pass']} passed, "
            f"{counts['warn']} warnings, {counts['fail']} failures"
        )
        report_path = self.files.get("json")
        return f"{line}\nReport: {report_path}" if report_path else line


def collect(
    *,
    launch_checks: bool = True,
    probe_timeout_s: float = 60.0,
    include_egress: bool = True,
) -> list[CheckResult]:
    """Run every check in order. A check module that explodes becomes a failure.

    ``include_egress=False`` is for the one caller that runs inside the live
    application (the tray fallback): the egress check loads the speech model and
    patches the process's socket layer, neither of which is acceptable inside an
    app that already has a model loaded and sockets in flight.
    """
    from .checks import config as config_checks
    from .checks import (
        desktop,
        egress,
        environment,
        history,
        instance,
        launch,
        native_libs,
        payload,
        ui_runtime,
    )

    results: list[CheckResult] = []
    results += _guarded("environment", environment.run)
    results += _guarded("payload", payload.run)
    results += _guarded("config", config_checks.run)
    results += _guarded("native", lambda: native_libs.run(timeout_s=probe_timeout_s))
    results += _guarded("ui", ui_runtime.run)
    results += _guarded("desktop", lambda: desktop.run(timeout_s=probe_timeout_s))
    results += _guarded("instance", lambda: instance.run(port=_configured_port()))
    if include_egress:
        results += _guarded("egress", egress.run)
    else:
        results.append(
            CheckResult(
                "egress.connections",
                PASS,
                "skipped: this run is inside the live application, where loading a second copy "
                "of the speech model and patching the socket layer would disturb the app it is "
                "diagnosing",
                "Run diagnostics from the Start Menu (DCENT_Voice Diagnostics) for the egress "
                "proof; it runs in its own process.",
            )
        )
    results += _guarded("history", history.run)
    results += _guarded("launch", lambda: launch.run(enabled=launch_checks))
    return results


def run_doctor(
    *,
    launch_checks: bool = True,
    json_path: Path | None = None,
    output_dir: Path | None = None,
    make_zip: bool = True,
    probe_timeout_s: float = 60.0,
    include_egress: bool = True,
) -> DoctorOutcome:
    """Run the checks and write the report files. Used by the CLI and the tray."""
    from .report import build_report, write_report

    started = datetime.now(UTC)
    results = collect(
        launch_checks=launch_checks,
        probe_timeout_s=probe_timeout_s,
        include_egress=include_egress,
    )
    finished = datetime.now(UTC)
    report = build_report(
        results, started_at=started, finished_at=finished, launch_checks=launch_checks
    )
    files = write_report(report, output_dir=output_dir, json_path=json_path, make_zip=make_zip)
    return DoctorOutcome(results=results, report=report, files=files)


def main(args: Any) -> int:
    """``doctor`` subcommand entry point. Never raises."""
    try:
        outcome = run_doctor(
            launch_checks=not getattr(args, "no_launch_checks", False),
            json_path=getattr(args, "json", None),
            make_zip=getattr(args, "zip", True) is not False,
        )
    except Exception:  # noqa: BLE001 - doctor's own failure must still be reportable
        detail = traceback.format_exc()
        _emit(f"dcent-voice doctor could not run:\n{detail}", error=True)
        _show_dialog("DCENT_Voice diagnostics failed", detail[-1200:])
        return EXIT_COULD_NOT_RUN

    _emit(_console_report(outcome))
    if getattr(args, "open", False):
        open_diagnostics_folder(outcome.files.get("json"))
    if _is_windowed():
        _show_dialog("DCENT_Voice diagnostics", outcome.headline() + _dialog_details(outcome))
    return outcome.exit_code


def open_diagnostics_folder(path: Path | None = None) -> bool:
    """Reveal the diagnostics folder in Explorer/Finder/the desktop file manager.

    Honors ``DCENT_VOICE_NO_OPEN=1``. Opening a file manager is a side effect on
    somebody's actual desktop, so the test suite and CI set that variable the
    same way they set ``DCENT_VOICE_NO_DIALOGS=1``: a test run must never make
    windows appear on the machine it runs on.
    """
    from .report import default_output_dir

    if os.environ.get(NO_OPEN_ENV, "").strip() == "1":
        return False
    target = Path(path).parent if path is not None else default_output_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606 - a directory this process just created
        elif sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", str(target)])  # noqa: S603
        else:
            subprocess.Popen(["xdg-open", str(target)])  # noqa: S603, S607
    except (OSError, AttributeError):
        return False
    return True


def _configured_port() -> int | None:
    """The port the user's config asks for; ``None`` when it cannot be read."""
    try:
        from dcent_voice.config import default_config_path, load_config

        return int(load_config(default_config_path(), create=False).service.port)
    except Exception:  # noqa: BLE001 - a broken config is config.file's finding, not ours
        return None


def _guarded(label: str, runner: Any) -> list[CheckResult]:
    try:
        return list(runner())
    except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
        return [
            CheckResult(
                f"{label}.internal_error",
                FAIL,
                f"the {label} checks could not run: {type(exc).__name__}: {exc}",
                "This is a bug in doctor itself. Please send this report.",
                {"traceback": traceback.format_exc()[-4000:]},
            )
        ]


def _console_report(outcome: DoctorOutcome) -> str:
    lines = [outcome.headline(), ""]
    for result in outcome.results:
        lines.append(f"[{result.status:<4}] {result.id:<32} {result.detail}")
    problems = [item for item in outcome.results if item.status != "pass" and item.remediation]
    if problems:
        lines.append("")
        lines.append("What to do:")
        for result in problems:
            lines.append(f"  {result.id}: {result.remediation}")
    for key in ("text", "zip", "requestedJson"):
        path = outcome.files.get(key)
        if path is not None:
            lines.append(f"{key}: {path}")
    return "\n".join(lines)


def _dialog_details(outcome: DoctorOutcome) -> str:
    problems = [item for item in outcome.results if item.status != "pass"]
    if not problems:
        return "\n\nEverything checked out."
    body = "\n\n".join(
        f"[{item.status.upper()}] {item.id}\n{item.detail}"
        + (f"\n-> {item.remediation}" if item.remediation else "")
        for item in problems[:6]
    )
    more = "" if len(problems) <= 6 else f"\n\n(+{len(problems) - 6} more in the report)"
    zip_path = outcome.files.get("zip")
    tail = f"\n\nSend us this file:\n{zip_path}" if zip_path else ""
    return "\n\n" + body + more + tail


def _is_windowed() -> bool:
    """True in the frozen, console-less Windows build, where stdout goes nowhere.

    A windowed PyInstaller build has ``sys.stdout is None``, and a detached
    launch (the Start Menu shortcut) has no console either — so "no stream at
    all" is the strongest possible evidence of windowed, not an error case.
    """
    if not getattr(sys, "frozen", False):
        return False
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return True
    try:
        return not stream.isatty()
    except (ValueError, OSError, AttributeError):
        return True


def _emit(text: str, *, error: bool = False) -> None:
    """Print when there is somewhere to print to; never fail because there isn't."""
    stream = getattr(sys, "stderr" if error else "stdout", None)
    if stream is None:
        return
    # A closed or detached pipe is not a doctor failure.
    with contextlib.suppress(OSError, ValueError, AttributeError):
        print(text, file=stream)


def _show_dialog(title: str, message: str) -> bool:
    if os.environ.get(NO_DIALOGS_ENV, "").strip() == "1":
        return False
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, message[:8000], title, 0x40 | 0x10000)
    except Exception:  # noqa: BLE001 - a missing dialog must never fail the command
        return False
    return True


def summary_line(results: list[CheckResult]) -> str:
    """One-line summary suitable for a tray notification."""
    counts = summarize(results)
    return (
        f"Diagnostics {counts['status']}: {counts['pass']} passed, "
        f"{counts['warn']} warnings, {counts['fail']} failures"
    )
