# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
from __future__ import annotations

import httpx
import pytest

from dcent_voice.util.retry import with_retries


def _no_sleep(_seconds: float) -> None:
    return None


def test_retries_transient_then_succeeds() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("transient")
        return "ok"

    assert with_retries(fn, attempts=3, sleep=_no_sleep) == "ok"
    assert len(calls) == 3


def test_gives_up_after_exhausting_attempts() -> None:
    def fn() -> str:
        raise httpx.ConnectTimeout("still down")

    with pytest.raises(httpx.ConnectTimeout):
        with_retries(fn, attempts=2, sleep=_no_sleep)


def test_non_transient_error_is_not_retried() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        raise ValueError("bad request, not transient")

    with pytest.raises(ValueError):
        with_retries(fn, attempts=3, sleep=_no_sleep)
    assert len(calls) == 1
