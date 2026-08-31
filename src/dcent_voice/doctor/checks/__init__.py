# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Individual diagnostic checks, one module per area.

Each module exposes ``run(...) -> list[CheckResult]`` and never raises: a check
that cannot answer its question returns a ``warn`` or ``fail`` saying so. The
orchestrator in :mod:`dcent_voice.doctor.cli` wraps each module anyway, because
a diagnostic tool that dies is worse than no diagnostic tool.
"""

from __future__ import annotations
