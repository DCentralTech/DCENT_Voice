# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Record pipeline stage timings."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StageRecord:
    name: str
    duration_s: float


@dataclass
class StageTimer:
    """Collect elapsed timings for named dictation-pipeline stages."""

    records: list[StageRecord] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.records.append(StageRecord(name=name, duration_s=time.perf_counter() - start))

    def as_dict(self) -> dict[str, float]:
        return {record.name: record.duration_s for record in self.records}

    def total_s(self) -> float:
        return sum(record.duration_s for record in self.records)

    def format_table(self) -> str:
        if not self.records:
            return "No timings recorded."
        width = max(len(record.name) for record in self.records)
        rows = ["stage".ljust(width) + "  seconds"]
        rows.append("-" * width + "  -------")
        for record in self.records:
            rows.append(f"{record.name.ljust(width)}  {record.duration_s:7.3f}")
        rows.append(f"{'total'.ljust(width)}  {self.total_s():7.3f}")
        return "\n".join(rows)
