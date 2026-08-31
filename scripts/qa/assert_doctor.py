# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Judge a ``dcent-voice doctor`` JSON report in CI.

``doctor`` exits 1 when *any* check fails, which is the right behaviour on a
user's machine. CI runners are not user machines: a headless macOS runner can
never hold the Accessibility TCC grant, and no runner has a microphone. Those
are properties of the runner, not defects in the build.

Rather than weaken the checks or let the job pass on a bare ``|| true``, the
workflow runs ``doctor`` unjudged and hands the report here with an explicit,
reviewable ``--allow-fail`` list. Every other failure still fails the build.

The script also reports when an allowed id did **not** fail, so an exception
that has become unnecessary shows up instead of quietly accumulating.

Exit codes: 0 accepted, 1 unexpected failures, 2 the report is missing or
unreadable (a doctor that could not run at all).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "docs" / "schemas" / "doctor.schema.json"

FAIL = "fail"
WARN = "warn"
PASS = "pass"


def validate_schema(report: dict) -> list[str]:
    """Validate against the published schema when jsonschema is importable."""
    try:
        import jsonschema
    except ImportError:
        print("note: jsonschema not installed; skipping schema validation")
        return []
    if not SCHEMA.is_file():
        print(f"note: no schema at {SCHEMA}; skipping schema validation")
        return []
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(report)
    ]
    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Path to the doctor --json report.")
    parser.add_argument(
        "--allow-fail",
        action="append",
        default=[],
        metavar="CHECK_ID",
        help=(
            "A check id whose failure is a property of this runner, not the build "
            "(repeatable). Every occurrence must be justified in the workflow comment."
        ),
    )
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="Skip validating the report against docs/schemas/doctor.schema.json.",
    )
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(f"doctor wrote no report at {args.report}: it could not run.", file=sys.stderr)
        return 2
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"doctor report at {args.report} is unreadable: {exc}", file=sys.stderr)
        return 2

    if not args.no_schema:
        errors = validate_schema(report)
        for error in errors:
            print(f"schema violation: {error}", file=sys.stderr)
        if errors:
            return 1

    checks = report.get("checks") or []
    summary = report.get("summary") or {}
    allowed = set(args.allow_fail)

    print(
        f"doctor {report.get('appVersion', '?')} on {report.get('tool', '?')}: "
        f"{summary.get('pass', 0)} pass, {summary.get('warn', 0)} warn, "
        f"{summary.get('fail', 0)} fail, overall {summary.get('status', '?')}"
    )

    unexpected: list[dict] = []
    accepted: list[dict] = []
    for check in checks:
        if check.get("status") != FAIL:
            continue
        (accepted if check.get("id") in allowed else unexpected).append(check)

    for check in checks:
        if check.get("status") == WARN:
            print(f"  warn     {check.get('id')}: {check.get('detail')}")
    for check in accepted:
        print(f"  accepted {check.get('id')}: {check.get('detail')} [allowed on this runner]")
    for check in unexpected:
        print(f"  FAIL     {check.get('id')}: {check.get('detail')}", file=sys.stderr)
        remediation = check.get("remediation") or ""
        if remediation:
            print(f"           remediation: {remediation}", file=sys.stderr)

    stale = allowed - {check.get("id") for check in accepted}
    known = {check.get("id") for check in checks}
    for check_id in sorted(stale):
        if check_id in known:
            print(f"  note     --allow-fail {check_id} did not fail; drop the exception.")
        else:
            print(f"  note     --allow-fail {check_id} matched no check id; it may be a typo.")

    if unexpected:
        ids = ", ".join(sorted(str(check.get("id")) for check in unexpected))
        print(f"\ndoctor reported {len(unexpected)} unexpected failure(s): {ids}", file=sys.stderr)
        return 1
    print("\ndoctor: no unexpected failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
