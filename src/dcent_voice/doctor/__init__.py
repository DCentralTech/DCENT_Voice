# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Diagnostics: one command that explains, remotely, why the app does not work.

Public surface (kept deliberately small so the tray, the installer and the CLI
all go through the same door):

``run_doctor(...)``
    Run every check, write ``doctor-<ts>.json``/``.txt`` and the zip, return a
    :class:`~dcent_voice.doctor.cli.DoctorOutcome`.
``run_doctor_in_background(notify)``
    Tray entry point: runs without launch checks on a daemon thread, opens the
    diagnostics folder, then calls ``notify(title, message)``.
``open_diagnostics_folder(path=None)``
    Reveal the diagnostics folder in the platform file manager.
``start_menu_shortcut_args()``
    Argument list for the installer's "DCENT_Voice Diagnostics" shortcut.

Everything is imported lazily: importing this package must stay cheap enough for
``app.py`` to reach ``doctor`` before it loads a configuration or a native
backend.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cli import DoctorOutcome

__all__ = [
    "main",
    "open_diagnostics_folder",
    "run_doctor",
    "run_doctor_in_background",
    "run_probe_command",
    "start_menu_shortcut_args",
]


def main(args: Any) -> int:
    """``dcent-voice doctor`` entry point (dispatched before the config loads)."""
    from .cli import main as _main

    return _main(args)


def run_doctor(**kwargs: Any) -> DoctorOutcome:
    from .cli import run_doctor as _run_doctor

    return _run_doctor(**kwargs)


def run_probe_command(module: str) -> int:
    """Hidden ``doctor-probe`` entry point: import one module, print a verdict."""
    from .probe import run_probe_command as _run

    return _run(module)


def open_diagnostics_folder(path: Path | None = None) -> bool:
    from .cli import open_diagnostics_folder as _open

    return _open(path)


def start_menu_shortcut_args() -> list[str]:
    """Arguments for the Start Menu "DCENT_Voice Diagnostics" shortcut."""
    return ["doctor", "--open"]


def run_doctor_in_background(
    notify: Callable[[str, str], None] | None = None,
    *,
    open_folder: bool = True,
    launch_checks: bool = False,
) -> threading.Thread:
    """Run diagnostics in a CHILD process, then open the folder and notify.

    The tray calls this. It must not run the checks in-process: the egress check
    loads a second copy of the ~670 MB speech model into the live application and
    patches ``socket.connect``/``getaddrinfo`` process-wide for the duration, so
    the app would be diagnosing itself while disturbing itself. A separate
    process gets a clean socket layer, its own model load, and cannot corrupt the
    running app if it crashes.

    Launch checks are off by default: the tray only exists when the app is
    already running, so starting a second copy to prove the first one starts
    would cost minutes and tell nobody anything.
    """

    def work() -> None:
        title = "DCENT_Voice diagnostics"
        summary: str | None
        try:
            summary, files = _run_doctor_child(launch_checks=launch_checks)
        except Exception as exc:  # noqa: BLE001 - a tray action must never crash the app
            summary, files = _run_doctor_in_process(launch_checks=launch_checks, error=exc)
            if summary is None:
                if notify is not None:
                    notify(title, f"Diagnostics could not run: {type(exc).__name__}: {exc}")
                return
        if open_folder:
            open_diagnostics_folder(files.get("json"))
        if notify is not None:
            bundle = files.get("zip") or files.get("json")
            notify(title, f"{summary}\n{bundle}" if bundle else summary)

    thread = threading.Thread(target=work, name="dcent-voice-doctor", daemon=True)
    thread.start()
    return thread


#: Generous: the child hashes ~670 MB of model weights on a cold cache.
CHILD_TIMEOUT_S = 900.0


def _child_argv(json_path: Path, *, launch_checks: bool) -> list[str]:
    """How to invoke this application's own ``doctor`` subcommand."""
    import sys

    executable = sys.executable or ""
    head = [executable] if getattr(sys, "frozen", False) else [executable, "-m", "dcent_voice"]
    argv = [*head, "doctor", "--json", str(json_path)]
    if not launch_checks:
        argv.append("--no-launch-checks")
    return argv


def _run_doctor_child(*, launch_checks: bool) -> tuple[str, dict[str, Path]]:
    """Run doctor in a child process and read its report. Raises on failure."""
    import json
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="dcent_voice_tray_") as tmp:
        json_path = Path(tmp) / "doctor.json"
        env = os.environ.copy()
        # The child must not steal focus with a dialog or an Explorer window;
        # this thread opens the folder itself once it knows the run succeeded.
        env["DCENT_VOICE_NO_DIALOGS"] = "1"
        env["DCENT_VOICE_NO_OPEN"] = "1"
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            _child_argv(json_path, launch_checks=launch_checks),
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_S,
            env=env,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
        # A non-zero exit means checks failed, which is a normal outcome to
        # report; only a missing or unreadable report is a failure of the run.
        report = json.loads(json_path.read_text(encoding="utf-8"))

    counts = report["summary"]
    summary = (
        f"Diagnostics {counts['status']}: {counts['pass']} passed, "
        f"{counts['warn']} warnings, {counts['fail']} failures"
    )
    artifacts = report.get("artifacts") or {}
    files = {key: Path(value) for key, value in artifacts.items() if value}
    return summary, files


def _run_doctor_in_process(
    *, launch_checks: bool, error: BaseException
) -> tuple[str | None, dict[str, Path]]:
    """Last resort when the child could not be spawned (locked-down host, AV).

    Egress is skipped here on purpose: patching the socket layer of the live
    application, and loading a second model into it, is not worth a check the
    user can get properly by running diagnostics from the Start Menu.
    """
    from .cli import summary_line

    try:
        outcome = run_doctor(launch_checks=launch_checks, include_egress=False)
    except Exception:  # noqa: BLE001 - the caller reports the original spawn error
        return None, {}
    detail = f"{type(error).__name__}: {error}"
    return f"{summary_line(outcome.results)} (in-process fallback: {detail})", outcome.files
