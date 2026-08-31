# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""PyInstaller entry point: bootstrap logging first, then never exit silently.

This is the script the frozen ``dcent-voice.exe`` runs. It is built
``console=False``, so anything that escaped here previously — an import-time
native DLL failure, an unhandled exception, ``SystemExit(2)`` from
``parser.error`` — produced a process that appeared for a moment and vanished
with no log line and no window. Every path out of this module now goes through
:func:`dcent_voice.util.fatal.report_fatal` unless something already did.
"""

from __future__ import annotations

# MUST stay the first import in the file: it installs the startup log, the crash
# hooks and the offline environment before any other module can fail.
from dcent_voice.util import bootlog  # noqa: I001  isort: skip

import contextlib  # noqa: E402
import sys  # noqa: E402

from dcent_voice.util.fatal import fatal_reported, is_windowed, report_fatal  # noqa: E402


def _log_quiet_exit(code: int) -> None:
    with contextlib.suppress(Exception):
        bootlog.logger().error("exited with code %s before startup completed", code)


def run() -> int:
    """Run the CLI, converting every escape into a logged, visible failure."""
    try:
        from dcent_voice.app import main

        return int(main() or 0)
    except SystemExit as exc:  # argparse and explicit exits land here
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            if code != 0 and not fatal_reported():
                if not is_windowed():
                    # A console caller (or CI) already saw argparse's own message
                    # on stderr; a modal dialog on top of it would be noise. The
                    # log line and the non-zero exit still stand.
                    _log_quiet_exit(code)
                    return code
                return report_fatal(
                    "DCENT_Voice stopped during startup",
                    f"The application exited with code {code} before it finished starting.",
                    log_path=bootlog.boot_log_path(),
                    exit_code=code,
                    exc=exc,
                )
            return code
        # A string exit code is a message argparse/stdlib wants printed.
        if not fatal_reported():
            report_fatal(
                "DCENT_Voice stopped during startup",
                str(code),
                log_path=bootlog.boot_log_path(),
                exit_code=1,
                exc=exc,
            )
        return 1
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:  # noqa: BLE001 - last line of defence
        if fatal_reported():
            return 1
        return report_fatal(
            "DCENT_Voice hit an unexpected error while starting",
            f"{type(exc).__name__}: {exc}",
            log_path=bootlog.boot_log_path(),
            exit_code=1,
            exc=exc,
        )


if __name__ == "__main__":
    sys.exit(run())
