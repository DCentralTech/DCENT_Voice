# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""What the last failed startup already told us.

The bootstrap logger writes ``startup.log`` before any heavy import, and a fatal
startup path leaves ``last-startup-failure.json`` behind. Those two files are
usually the entire answer to "nothing happened when I double-clicked it", so
doctor surfaces them at the top of the report and copies them into the zip.

Both locations come from the modules that write them
(:mod:`dcent_voice.util.bootlog` and :mod:`dcent_voice.util.fatal`) rather than
being rebuilt here. That matters for the case doctor exists to explain: when the
profile directory is unwritable those writers fall back to
``%TEMP%\\DCENT_Voice``, and a doctor that looked only under the profile would
report "no logs" for the machine whose logs are the whole story.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dcent_voice.util import paths

from ..result import FAIL, PASS, WARN, CheckResult

#: Log tail length carried inline in the report (the zip has the full files).
TAIL_LINES = 60


def logs_dir() -> Path:
    """Directory the rotating application log lives in."""
    return paths.user_config_dir() / "logs"


def boot_log_path() -> Path:
    """``startup.log``, wherever the bootstrap logger put it — or would put it.

    Uses ``probe_boot_log_path()``, which never touches the filesystem. The
    creating variant (``boot_log_path()``) proves writability by opening the
    file for append, which would be self-defeating here: doctor must be able to
    report "no startup log exists" without creating the very file it is about to
    call missing.
    """
    from dcent_voice.util import bootlog

    probed = bootlog.probe_boot_log_path()
    return probed if probed is not None else logs_dir() / bootlog.BOOT_LOG_FILENAME


def failure_record_path() -> Path:
    """``last-startup-failure.json``, beside the log the fatal handler would use."""
    from dcent_voice.util import fatal

    # Pass the probed location explicitly: left to resolve it itself, this would
    # reach the creating resolver through fatal's own default.
    recorded = fatal.failure_record_path(boot_log_path())
    if recorded is not None:
        return recorded
    return boot_log_path().parent / fatal.FAILURE_FILENAME


def log_files() -> list[Path]:
    """Every log doctor reads and bundles, whether or not it exists yet.

    ``startup.log`` is listed at its real location, which is not necessarily
    under the profile; duplicates are collapsed so the zip never carries the
    same file twice.
    """
    root = logs_dir()
    candidates = [
        boot_log_path(),
        root / "dcent_voice.log",
        root / "dcent_voice_fault.log",
    ]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def run() -> list[CheckResult]:
    return [check_last_startup_failure(), check_logs()]


def check_last_startup_failure() -> CheckResult:
    import json

    path = failure_record_path()
    data: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return CheckResult(
            "history.last_startup_failure",
            PASS,
            "no recorded startup failure (the app has not died during startup since this file "
            "was last cleared)",
            data=data,
        )
    raw = ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data["record"] = json.loads(raw)
    except (OSError, ValueError) as exc:
        data["error"] = str(exc)
        data["raw"] = raw[:4000]
        return CheckResult(
            "history.last_startup_failure",
            WARN,
            f"a startup-failure record exists at {path} but could not be parsed: {exc}",
            "Send the diagnostics zip; the raw file is included.",
            data,
        )
    record = data["record"] if isinstance(data.get("record"), dict) else {}
    when = record.get("timestamp") or record.get("at") or "an earlier run"
    message = record.get("message") or record.get("error") or "no message recorded"
    return CheckResult(
        "history.last_startup_failure",
        FAIL,
        f"the last startup failed at {when}: {message}",
        str(record.get("remediation") or "")
        or "This is the most specific evidence in the report. Fix the condition it names, "
        "then delete the file (or simply relaunch — a successful start rewrites it).",
        data,
    )


def check_logs() -> CheckResult:
    data: dict[str, Any] = {
        "logsDir": str(logs_dir()),
        "bootLog": str(boot_log_path()),
        "files": {},
    }
    present = 0
    for path in log_files():
        entry: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
        if path.is_file():
            present += 1
            try:
                entry["sizeBytes"] = path.stat().st_size
                entry["tail"] = tail_lines(path, TAIL_LINES)
            except OSError as exc:
                entry["error"] = str(exc)
        data["files"][path.name] = entry
    if present == 0:
        return CheckResult(
            "history.logs",
            WARN,
            f"no log files exist at {logs_dir()} or {boot_log_path()}. Either the app has "
            "never started, or it died before it could open a log — which is itself the "
            "finding.",
            "Launch DCENT_Voice once and run doctor again. If there is still no log, "
            "check env.write_access in this report.",
            data,
        )
    return CheckResult(
        "history.logs",
        PASS,
        f"{present} log file(s) found ({logs_dir()}, startup log at {boot_log_path()}); the "
        f"last {TAIL_LINES} lines of each are in this report and the full files are in the "
        "diagnostics zip",
        data=data,
    )


def tail_lines(path: Path, count: int) -> list[str]:
    """Last ``count`` lines, read defensively (logs can be huge or half-written)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            window = min(size, 256 * 1024)
            handle.seek(size - window)
            blob = handle.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if window < size and lines:
        # The first line of a mid-file window is almost certainly truncated.
        lines = lines[1:]
    return [line.rstrip() for line in lines[-count:]]
