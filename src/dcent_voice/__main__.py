# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Run the package's command-line application entry point.

``python -m dcent_voice`` shares the frozen entry point's fail-loud wrapper so
the source and packaged runs cannot diverge in how a startup failure surfaces.
"""

from __future__ import annotations

# MUST stay the first import: bootstrap log, crash hooks and offline env.
from .util import bootlog as bootlog  # noqa: F401,I001  isort: skip

from ._packaged import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
