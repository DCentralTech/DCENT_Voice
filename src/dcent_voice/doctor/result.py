# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""The one shape every diagnostic check produces.

A check never raises and never prints: it returns a :class:`CheckResult` with a
stable ``id`` (documented in ``docs/TROUBLESHOOTING.md``), a status, a plain
detail sentence, and — when the status is not ``pass`` — a remediation the user
can actually act on.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

PASS = "pass"
WARN = "warn"
FAIL = "fail"

#: Ordered worst-last, so ``max`` over this ranking summarizes a run.
_RANK = {PASS: 0, WARN: 1, FAIL: 2}


@dataclass(frozen=True)
class CheckResult:
    """One diagnostic outcome."""

    id: str
    status: str
    detail: str
    remediation: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _RANK:
            raise ValueError(f"invalid check status: {self.status!r}")

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
            "data": _jsonable(self.data),
        }


def worst_status(results: Iterable[CheckResult]) -> str:
    """The most severe status in ``results`` (``pass`` when empty)."""
    return max(
        (result.status for result in results),
        key=lambda status: _RANK[status],
        default=PASS,
    )


def summarize(results: Iterable[CheckResult]) -> dict[str, Any]:
    """Counts plus the overall status, as embedded in the report."""
    materialized = list(results)
    counts = {status: 0 for status in _RANK}
    for result in materialized:
        counts[result.status] += 1
    return {
        "pass": counts[PASS],
        "warn": counts[WARN],
        "fail": counts[FAIL],
        "total": len(materialized),
        "status": worst_status(materialized),
    }


def exit_code_for(results: Iterable[CheckResult]) -> int:
    """0 when everything passed or only warned, 1 when anything failed."""
    return 1 if worst_status(results) == FAIL else 0


def _jsonable(value: Any) -> Any:
    """Coerce paths, tuples, sets and stray objects into JSON-safe values."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(item) for item in value]
    return str(value)
