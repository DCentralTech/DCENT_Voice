# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

from dcent_voice.util.timing import StageTimer


def test_stage_timer_records_named_stage() -> None:
    timer = StageTimer()

    with timer.stage("asr"):
        pass

    assert len(timer.records) == 1
    assert timer.records[0].name == "asr"
    assert timer.records[0].duration_s >= 0
    assert "asr" in timer.as_dict()
    assert "total" in timer.format_table()
