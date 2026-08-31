# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Small retry helper for transient network failures.

Cloud ASR/LLM/ADE calls were single-attempt, so one dropped packet failed a whole
utterance. This retries only *transient* failures (connect/timeout) with
exponential backoff — a 4xx (bad key, bad request) is not transient and raises
immediately so the user gets the real error fast.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import httpx

T = TypeVar("T")

_TRANSIENT: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.TransportError)


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.4,
    max_delay: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(attempts):
        try:
            return fn()
        except _TRANSIENT:
            if attempt == attempts - 1:
                raise
            sleep(min(max_delay, base_delay * (2**attempt)))
    raise RuntimeError("with_retries exhausted without returning")  # pragma: no cover
