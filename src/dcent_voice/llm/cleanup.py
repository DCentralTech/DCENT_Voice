# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Clean up transcriptions with local or opted-in language models."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from difflib import SequenceMatcher

from dcent_voice.config import APP_NAME, VocabEntry
from dcent_voice.llm.base import LLMProvider
from dcent_voice.llm.prompts import cleanup_system_prompt, cleanup_user_prompt

logger = logging.getLogger(APP_NAME).getChild("cleanup")

# After a hard connect/timeout failure, skip cleanup for this long so long
# utterances are not repeatedly delayed when LM Studio/Ollama is down.
_CIRCUIT_OPEN_S = 60.0

_PREAMBLE_RE = re.compile(
    r"^(sure[,!.]?\s+|here is\b|here's\b|cleaned\s*(text|version)?\s*[:\-–]?\s*|"
    r"certainly[,!.]?\s+|of course[,!.]?\s+|the cleaned\b)",
    re.IGNORECASE,
)


class CleanupPipeline:
    """Applies configured transcription cleanup while preserving privacy controls."""

    def __init__(
        self,
        provider: LLMProvider | None,
        *,
        enabled: bool = True,
        timeout_s: float = 4.0,
        dictionary: tuple[VocabEntry, ...] = (),
        snippets: tuple[object, ...] = (),
        circuit_open_s: float = _CIRCUIT_OPEN_S,
        health_preflight: bool = False,
        health_retry_s: float = 30.0,
    ) -> None:
        self.provider = provider
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.dictionary = dictionary
        self.snippets = snippets
        self.circuit_open_s = circuit_open_s
        self.health_preflight = health_preflight
        self.health_retry_s = health_retry_s
        # One reused pool bounds the LLM call at timeout_s without creating and
        # tearing down a thread per utterance. Two workers so a single hung call
        # (its blocking httpx request can't be cancelled) can't stall the next.
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False
        self._circuit_open_until = 0.0
        self._health_lock = threading.Lock()
        self._health_available: bool | None = None
        self._health_checked_at = 0.0
        self._health_future: Future[bool] | None = None
        if self.health_preflight and self.provider is not None:
            self._schedule_health_probe()

    def _pool(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="CleanupPipeline")
        return self._executor

    def clean(self, raw_text: str, *, style: str | None = None) -> str:
        """Optionally clean text with the configured local LLM."""
        raw_text = raw_text.strip()
        if not raw_text or not self.enabled or self.provider is None:
            return raw_text
        if self._closed:
            # A config swap closed this pipeline while an utterance was still in
            # flight; don't resurrect the pool (it would leak) — the raw
            # transcript is the correct degraded output here.
            return raw_text
        now = time.monotonic()
        if now < self._circuit_open_until:
            logger.debug(
                "cleanup circuit open for %.0fs more; using raw transcript",
                self._circuit_open_until - now,
            )
            return raw_text
        if self.health_preflight and self._health_available is False:
            if now - self._health_checked_at >= self.health_retry_s:
                self._schedule_health_probe()
            logger.debug("cleanup provider unavailable; bypassing optional cleanup")
            return raw_text

        max_tokens = max(64, min(512, len(raw_text.split()) * 2 + 32))
        future = self._pool().submit(
            self.provider.complete,
            cleanup_system_prompt(style),
            cleanup_user_prompt(raw_text, self.dictionary, self.snippets),
            temperature=0.1,
            max_tokens=max_tokens,
        )
        try:
            cleaned = future.result(timeout=self.timeout_s)
        except TimeoutError:
            future.cancel()
            self._trip_circuit("timeout")
            logger.warning("cleanup timed out after %.1fs; using raw transcript", self.timeout_s)
            return raw_text
        except Exception as exc:
            future.cancel()
            self._trip_circuit(type(exc).__name__)
            logger.warning(
                "cleanup failed (%s: %s); using raw transcript",
                type(exc).__name__,
                exc,
            )
            logger.debug("cleanup failure traceback", exc_info=True)
            return raw_text

        self._set_health_available(True)
        cleaned = normalize_cleanup(cleaned)
        if not cleaned:
            logger.info(
                "cleanup_accepted=False reason=empty raw_len=%s cleaned_len=0",
                len(raw_text),
            )
            return raw_text
        ok, reason = accept_cleanup(raw_text, cleaned)
        if not ok:
            logger.info(
                "cleanup_accepted=False reason=%s raw_len=%s cleaned_len=%s",
                reason,
                len(raw_text),
                len(cleaned),
            )
            return raw_text
        if not cleanup_keeps_snippet_expansions(raw_text, cleaned, self.snippets):
            logger.info(
                "cleanup_accepted=False reason=snippet_expansion raw_len=%s cleaned_len=%s",
                len(raw_text),
                len(cleaned),
            )
            return raw_text
        if not cleanup_keeps_dictionary_written_forms(raw_text, cleaned, self.dictionary):
            logger.info(
                "cleanup_accepted=False reason=dictionary_written raw_len=%s cleaned_len=%s",
                len(raw_text),
                len(cleaned),
            )
            return raw_text
        logger.info(
            "cleanup_accepted=True raw_len=%s cleaned_len=%s",
            len(raw_text),
            len(cleaned),
        )
        return cleaned

    def _trip_circuit(self, reason: str) -> None:
        if self.health_preflight:
            self._set_health_available(False)
        if self.circuit_open_s <= 0:
            return
        self._circuit_open_until = time.monotonic() + self.circuit_open_s
        logger.info("cleanup circuit open for %.0fs after %s", self.circuit_open_s, reason)

    def _schedule_health_probe(self) -> None:
        if self.provider is None or self._closed:
            return
        with self._health_lock:
            if self._health_future is not None and not self._health_future.done():
                return
            future = self._pool().submit(self.provider.health)
            self._health_future = future
        future.add_done_callback(self._finish_health_probe)

    def _finish_health_probe(self, future: Future[bool]) -> None:
        try:
            available = bool(future.result())
        except Exception:
            available = False
        self._set_health_available(available)
        logger.info(
            "cleanup provider preflight status=%s",
            "ready" if available else "unavailable; raw fallback active",
        )

    def _set_health_available(self, available: bool) -> None:
        with self._health_lock:
            self._health_available = available
            self._health_checked_at = time.monotonic()

    @property
    def availability(self) -> str:
        if not self.health_preflight:
            return "unchecked"
        if self._health_available is None:
            return "checking"
        return "ready" if self._health_available else "unavailable"

    def close(self) -> None:
        self._closed = True
        with self._health_lock:
            health_future = self._health_future
            self._health_future = None
        if health_future is not None:
            health_future.cancel()
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


def normalize_cleanup(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def accept_cleanup(raw: str, cleaned: str) -> tuple[bool, str]:
    """Return (accept, reason). Reject invented essays / preambles."""
    if not cleaned.strip():
        return False, "empty"
    if _PREAMBLE_RE.match(cleaned):
        return False, "preamble"
    raw_len = len(raw)
    cleaned_len = len(cleaned)
    if cleaned_len > max(int(2.5 * raw_len), raw_len + 120):
        return False, "too_long"
    if raw_len > 40 and cleaned_len < int(0.2 * raw_len):
        return False, "too_short"
    ratio = SequenceMatcher(None, raw.lower(), cleaned.lower()).ratio()
    if ratio < 0.35:
        return False, "dissimilar"
    return True, "ok"


def cleanup_keeps_snippet_expansions(
    raw: str, cleaned: str, snippets: tuple[object, ...] = ()
) -> bool:
    """Return whether every protected snippet expansion survived cleanup."""
    source = raw or ""
    output = cleaned or ""
    for entry in snippets or ():
        expansion = (getattr(entry, "expansion", None) or "").strip()
        if expansion and expansion in source and expansion not in output:
            return False
    return True


def cleanup_keeps_dictionary_written_forms(
    raw: str, cleaned: str, dictionary: tuple[object, ...] = ()
) -> bool:
    """Return whether every protected dictionary form survived cleanup."""
    source = raw or ""
    output = cleaned or ""
    for entry in dictionary or ():
        written = (getattr(entry, "written", None) or "").strip()
        if written and written in source and written not in output:
            return False
    return True
