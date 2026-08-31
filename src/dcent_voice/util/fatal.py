# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""The single user-visible failure surface for startup errors.

The frozen Windows executable is built ``console=False``. Before this module
existed, every pre-startup failure (missing example config, corrupt TOML,
``ConsentRequired``, a mutex the OS refused to create, an import-time native DLL
error) ended in ``parser.error()`` or an unhandled exception: exit code 2, no
window, no log, no message. The process appeared for a moment and vanished.

:func:`report_fatal` is the replacement. Every call does all four of:

1. logs ``CRITICAL`` to the application logger (so it lands in ``startup.log``
   and, when configured, ``dcent_voice.log``);
2. writes ``<logs>/last-startup-failure.json`` for ``dcent-voice doctor``;
3. shows a native dialog naming the problem *and the log path* — Windows
   ``MessageBoxW``, macOS ``osascript``, Linux ``notify-send`` — whenever a
   desktop session exists;
4. prints to stderr, always, so a console/CI run sees the same text.

Set ``DCENT_VOICE_NO_DIALOGS=1`` to keep 1/2/4 and suppress only the dialog
(used by CI, the fresh-profile smoke and the tests).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

#: Suppress the modal dialog only. Logging, the JSON record and stderr remain.
NO_DIALOGS_ENV = "DCENT_VOICE_NO_DIALOGS"

FAILURE_FILENAME = "last-startup-failure.json"

DEFAULT_TITLE = "DCENT_Voice could not start"

_MB_OK = 0x0
_MB_ICONERROR = 0x10
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000

_reported = False


def fatal_reported() -> bool:
    """True once :func:`report_fatal` has run in this process.

    Entry points use this so a failure that already showed a dialog is not
    reported a second time by the outermost ``except BaseException``.
    """
    return _reported


def reset_fatal_state() -> None:
    """Test hook: forget that a fatal was already reported."""
    global _reported
    _reported = False


def report_fatal(
    title: str,
    message: str,
    *,
    log_path: Path | str | None = None,
    exit_code: int = 1,
    exc: BaseException | None = None,
) -> int:
    """Log, record, and show a fatal startup error. Returns ``exit_code``.

    Never raises: a failure surface that can itself fail is the bug this module
    exists to remove.
    """
    global _reported
    _reported = True

    resolved_log = _resolve_log_path(log_path)
    detail = f"{title}\n\n{message}"
    if resolved_log is not None:
        detail = f"{detail}\n\nLog: {resolved_log}"

    with contextlib.suppress(Exception):
        from dcent_voice.util import bootlog

        bootlog.logger().critical(
            "FATAL %s: %s (log=%s exit=%s)",
            title,
            message,
            resolved_log,
            exit_code,
            exc_info=exc if exc is not None else None,
        )

    with contextlib.suppress(Exception):
        _write_failure_record(title, message, resolved_log, exc)

    with contextlib.suppress(Exception):
        _print_stderr(detail)

    if not _dialogs_suppressed():
        with contextlib.suppress(Exception):
            _show_dialog(title, detail)

    with contextlib.suppress(Exception):
        from dcent_voice.util.logging import flush_logging

        flush_logging()
    return exit_code


def is_windowed() -> bool:
    """True when this process has no usable stderr at all.

    That is the ``console=False`` frozen build launched from Explorer: PyInstaller
    leaves ``sys.stdout``/``sys.stderr`` as ``None``, so anything printed is
    simply lost. A piped subprocess (CI, the fresh-profile smoke, a test) has
    real streams and is *not* windowed, which is what keeps this from being an
    "is it a tty" check that would misfire on every redirected run.

    Note this does not gate :func:`report_fatal`'s dialog — a genuine startup
    failure is shown whenever a desktop session exists, because the user who
    double-clicked the shortcut needs to see it either way. It only gates the
    generic "exited non-zero" fallback, where a console caller has already read
    the real message on stderr.
    """
    for name in ("stderr", "stdout"):
        stream = getattr(sys, name, None)
        if stream is None:
            return True
        try:
            if stream.fileno() < 0:
                return True
        except Exception:
            return True
    return False


def desktop_session_available() -> bool:
    """Whether showing a modal dialog can reach a human."""
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def failure_record_path(log_path: Path | None = None) -> Path | None:
    """Where ``last-startup-failure.json`` is written (``doctor`` reads it)."""
    resolved = _resolve_log_path(log_path)
    if resolved is None:
        return None
    return resolved.parent / FAILURE_FILENAME


def _resolve_log_path(log_path: Path | str | None) -> Path | None:
    if log_path is not None:
        return Path(log_path)
    try:
        from dcent_voice.util import bootlog

        return bootlog.boot_log_path()
    except Exception:  # pragma: no cover - defensive
        return None


def _write_failure_record(
    title: str,
    message: str,
    log_path: Path | None,
    exc: BaseException | None,
) -> None:
    target = failure_record_path(log_path)
    if target is None:
        return
    try:
        from dcent_voice import __version__
    except Exception:  # pragma: no cover
        __version__ = "unknown"  # noqa: N806
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "title": title,
        "message": message,
        "log_path": str(log_path) if log_path is not None else None,
        # Never the raw command line: `compose`/`learn` carry spoken text in
        # argv, and this file goes into the diagnostics zip a user emails us.
        "argv": _redacted_argv(),
        "version": __version__,
        "exc_type": type(exc).__name__ if exc is not None else None,
        "executable": getattr(sys, "executable", None),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _redacted_argv() -> list[str]:
    try:
        from dcent_voice.util.bootlog import redact_argv

        return redact_argv()
    except Exception:  # pragma: no cover - defensive; never lose the record
        # Failing closed: no argv at all beats an unredacted one.
        return []


def _print_stderr(detail: str) -> None:
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    stream.write(f"DCENT_Voice: {detail}\n")
    with contextlib.suppress(Exception):
        stream.flush()


def _dialogs_suppressed() -> bool:
    value = (os.environ.get(NO_DIALOGS_ENV) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _show_dialog(title: str, detail: str) -> None:
    if not desktop_session_available():
        return
    if sys.platform == "win32":
        _show_windows_dialog(title, detail)
    elif sys.platform == "darwin":
        _show_macos_dialog(title, detail)
    else:
        _show_linux_notification(title, detail)


def _show_windows_dialog(title: str, detail: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        0,
        detail,
        f"DCENT_Voice — {title}"[:120],
        _MB_OK | _MB_ICONERROR | _MB_SETFOREGROUND | _MB_TOPMOST,
    )


def _show_macos_dialog(title: str, detail: str) -> None:
    import subprocess  # noqa: S404 - fixed argv, no shell

    from dcent_voice.util.applescript import quote

    # Not json.dumps: AppleScript has no \uXXXX escape, so an ASCII-safe JSON
    # literal renders the em dash in this very title as the text "u2014".
    script = f"display alert {quote(f'DCENT_Voice — {title}')} message {quote(detail)} as critical"
    subprocess.run(  # noqa: S603
        ["/usr/bin/osascript", "-e", script],
        check=False,
        capture_output=True,
        timeout=30,
    )


def _show_linux_notification(title: str, detail: str) -> None:
    import shutil
    import subprocess  # noqa: S404 - fixed argv, no shell

    notify = shutil.which("notify-send")
    if not notify:
        return
    subprocess.run(  # noqa: S603
        [notify, "--urgency=critical", f"DCENT_Voice — {title}", detail],
        check=False,
        capture_output=True,
        timeout=15,
    )
