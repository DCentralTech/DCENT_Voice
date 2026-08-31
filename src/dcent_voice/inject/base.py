# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Define the text-injection interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class RetractUnsupported(RuntimeError):
    """Raised when an injector cannot delete already-typed characters.

    RoutingInjector uses this to fall back to the keystroke backend. A silent
    no-op is unsafe: streaming finalize often needs retract before re-inject
    (e.g. local polish rewrites ``hello world`` → ``Hello world.``).
    """


class Injector(ABC):
    """Protocol implemented by text-injection backends."""

    @abstractmethod
    def inject(self, text: str) -> None:
        """Inject text from the PipelineWorker thread only."""

    def retract(self, char_count: int) -> None:
        """Delete the last ``char_count`` characters from the focused field.

        Used by streaming dictation when spoken edits or local polish shorten or
        rewrite already-injected text. Concrete injectors that can synthesize
        Backspace must override this. The base implementation raises
        :class:`RetractUnsupported` so routers can fall back to keystroke
        injection rather than silently leaving stale text in the field.
        """
        if char_count <= 0:
            return
        raise RetractUnsupported(f"{type(self).__name__} cannot retract {char_count} character(s)")

    def press_enter(self) -> None:
        """Send Enter into the focused field after a spoken submit cue.

        Concrete injectors should synthesize a real Enter/Return key. The base
        implementation uses pynput so clipboard-only backends still submit.
        """
        try:
            from pynput.keyboard import Controller, Key
        except ImportError as exc:  # pragma: no cover - dependency/environment
            raise RuntimeError("pynput is required to press Enter.") from exc
        controller = Controller()
        controller.press(Key.enter)
        controller.release(Key.enter)
