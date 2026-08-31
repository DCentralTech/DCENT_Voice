# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Turn check results into the three artifacts a user can send us.

* ``doctor-<timestamp>.json`` — machine-readable, validated against
  ``docs/schemas/doctor.schema.json``
* ``doctor-<timestamp>.txt``  — the same content a human can read in a chat window
* ``dcent-voice-diagnostics-<timestamp>.zip`` — both of the above plus the logs
  and a **redacted** copy of ``config.toml``

Redaction is belt-and-braces: DCENT_Voice keeps credentials in the OS keyring,
not in ``config.toml``. But a user may have pasted a key in by hand, and a
diagnostics bundle is a file people forward to strangers, so anything that looks
like a secret is replaced before it leaves the machine.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dcent_voice.util import paths

from .result import CheckResult, summarize

SCHEMA_VERSION = 1
REDACTED = "***REDACTED***"

#: TOML/INI keys whose value is replaced wholesale.
_SECRET_KEY = re.compile(
    r"^(\s*)([A-Za-z0-9_.\-\[\]\"']*"
    r"(?:api_key|apikey|token|secret|password|passwd|credential|client_secret|authorization)"
    r"[A-Za-z0-9_.\-\"']*)(\s*=\s*)(.+)$",
    re.IGNORECASE,
)

#: Values that look like a credential wherever they appear.
_SECRET_VALUE = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9_\-]{16,}|"
    r"Bearer\s+[A-Za-z0-9._\-]{16,}|[A-Za-z0-9_\-]{40,})\b"
)


def timestamp(now: datetime | None = None) -> str:
    """Filename-safe UTC stamp, e.g. ``20260829-142530``."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y%m%d-%H%M%S")


def default_output_dir() -> Path:
    """``<profile root or user config dir>/diagnostics``."""
    root = paths.profile_root()
    base = root if root is not None else paths.user_config_dir()
    return base / "diagnostics"


def build_report(
    results: Sequence[CheckResult],
    *,
    started_at: datetime,
    finished_at: datetime,
    launch_checks: bool,
) -> dict[str, Any]:
    from dcent_voice import __version__

    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": "dcent-voice doctor",
        "appVersion": __version__,
        "generatedAt": finished_at.astimezone(UTC).isoformat(),
        "durationSeconds": round((finished_at - started_at).total_seconds(), 3),
        "options": {"launchChecks": launch_checks},
        "summary": summarize(results),
        "checks": [result.to_json() for result in results],
    }


def render_text(report: dict[str, Any]) -> str:
    """The human-readable twin of the JSON, ordered worst-first per section."""
    summary = report["summary"]
    lines = [
        "DCENT_Voice diagnostics",
        "=" * 60,
        f"version    : {report.get('appVersion', 'unknown')}",
        f"generated  : {report.get('generatedAt', '')}",
        f"duration   : {report.get('durationSeconds', 0)} s",
        f"launch run : {'yes' if report.get('options', {}).get('launchChecks') else 'no'}",
        "",
        f"RESULT: {summary['status'].upper()}  "
        f"({summary['pass']} pass, {summary['warn']} warn, {summary['fail']} fail)",
        "",
    ]
    failures = [check for check in report["checks"] if check["status"] == "fail"]
    warnings = [check for check in report["checks"] if check["status"] == "warn"]
    if failures or warnings:
        lines.append("What to do")
        lines.append("-" * 60)
        for check in (*failures, *warnings):
            lines.append(f"[{check['status'].upper()}] {check['id']}")
            lines.append(f"    {check['detail']}")
            if check.get("remediation"):
                lines.append(f"    -> {check['remediation']}")
            lines.append("")
    lines.append("All checks")
    lines.append("-" * 60)
    for check in report["checks"]:
        lines.append(f"[{check['status']:<4}] {check['id']:<32} {check['detail']}")
    lines.append("")
    lines.append("Details")
    lines.append("-" * 60)
    lines.append(json.dumps({check["id"]: check["data"] for check in report["checks"]}, indent=2))
    lines.append("")
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    *,
    output_dir: Path | None = None,
    stamp: str | None = None,
    json_path: Path | None = None,
    make_zip: bool = True,
    extra_files: Iterable[Path] | None = None,
) -> dict[str, Path]:
    """Write JSON, text and (optionally) the zip. Returns the paths written.

    The report carries an ``artifacts`` block naming its own siblings, so a
    caller that only has the JSON — the tray, which runs doctor in a child
    process — can find the zip to show the user without parsing console output.

    Everything is written owner-only. The bundle contains the user's logs and
    their configuration; on a shared or roaming profile the default umask and
    inherited directory ACLs are not a good enough answer for that.
    """
    directory = output_dir or default_output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    secure_directory(directory)
    stamped = stamp or timestamp()

    json_file = directory / f"doctor-{stamped}.json"
    text_file = directory / f"doctor-{stamped}.txt"
    zip_file = directory / f"dcent-voice-diagnostics-{stamped}.zip" if make_zip else None

    # Name the artifacts inside the report itself, before serializing it.
    report = dict(report)
    report["artifacts"] = {
        "json": str(json_file),
        "text": str(text_file),
        "zip": str(zip_file) if zip_file is not None else "",
    }

    written: dict[str, Path] = {}
    payload = json.dumps(report, indent=2, sort_keys=False)
    text = render_text(report)

    _write_private(json_file, payload)
    written["json"] = json_file

    _write_private(text_file, text)
    written["text"] = text_file

    if json_path is not None:
        json_path = Path(json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(json_path, payload)
        written["requestedJson"] = json_path

    if zip_file is not None:
        written["zip"] = write_bundle(
            zip_file,
            json_file=json_file,
            text_file=text_file,
            extra_files=extra_files,
        )
    return written


def _write_private(path: Path, contents: str) -> None:
    """Write owner-only, atomically, degrading to a plain write if ACLs fail.

    A diagnostics report the user cannot obtain is worse than one with default
    permissions, so an ACL tooling failure logs through and still writes: the
    point of the report is that it reaches us.
    """
    from dcent_voice.attach.registry import write_text_atomic

    try:
        write_text_atomic(path, contents, require_private=True)
    except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
        path.write_text(contents, encoding="utf-8")


def _precreate_private(path: Path) -> None:
    """Create the zip empty and lock it down before any content goes in.

    ``ZipFile`` opens the path itself, so the only moment to restrict access is
    before it holds any of the user's logs — securing it afterwards would leave
    a window in which the bundle was readable.
    """
    from dcent_voice.attach.registry import restrict_private_file

    try:
        path.unlink(missing_ok=True)
        path.touch()
        restrict_private_file(path)
    except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
        # A bundle with default permissions still beats no bundle at all.
        return


def secure_directory(path: Path) -> bool:
    """Restrict a directory to the current user. False when it could not be done."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            return False
        return True
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "icacls",
                str(path),
                "/inheritance:r",
                # (OI)(CI) so the report files created inside inherit the same
                # restriction rather than picking up the profile's ACL.
                "/grant:r",
                f"{user}:(OI)(CI)(F)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def write_bundle(
    destination: Path,
    *,
    json_file: Path,
    text_file: Path,
    extra_files: Iterable[Path] | None = None,
) -> Path:
    """Zip the report, the logs and a redacted config."""
    from .checks.history import failure_record_path, log_files

    destination.parent.mkdir(parents=True, exist_ok=True)
    _precreate_private(destination)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(json_file, arcname=json_file.name)
        bundle.write(text_file, arcname=text_file.name)
        for path in log_files():
            if path.is_file():
                bundle.write(path, arcname=f"logs/{path.name}")
        failure = failure_record_path()
        if failure.is_file():
            bundle.write(failure, arcname=f"logs/{failure.name}")
        config_path = paths.user_config_dir() / "config.toml"
        if config_path.is_file():
            bundle.writestr("config.redacted.toml", redact_config(_read_text(config_path)))
        for path in extra_files or ():
            if Path(path).is_file():
                bundle.write(path, arcname=f"extra/{Path(path).name}")
    return destination


def redact_config(text: str) -> str:
    """Replace anything that looks like a credential, line by line."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out.append(_SECRET_VALUE.sub(REDACTED, line))
            continue
        match = _SECRET_KEY.match(line)
        if match:
            indent, key, equals, _value = match.groups()
            out.append(f'{indent}{key}{equals}"{REDACTED}"')
            continue
        out.append(_SECRET_VALUE.sub(REDACTED, line))
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trailing


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - reported by config.file already
        return f"# doctor could not read this file: {exc}\n"
