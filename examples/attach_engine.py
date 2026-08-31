# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Minimal in-process attach: transcribe a WAV with no tray, overlay, or hotkeys.

python examples/attach_engine.py tests/fixtures/audio/hello.wav
python examples/attach_ade.py tests/fixtures/audio/hello.wav
dcent-voice compose --style email the meeting is at 5 actually 6
dcent-voice compose --cleanup-level high I guess I think we should ship Monday
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dcent_voice.engine import VoiceEngine


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python examples/attach_engine.py <file.wav>", file=sys.stderr)
        return 2
    engine = VoiceEngine.from_config()
    try:
        print(json.dumps(engine.capabilities(), indent=2, sort_keys=True))
        result = engine.transcribe_file(Path(args[0]))
        # Headless learn records app-scoped learned written forms.
        # engine.learn("vip", "I'm Ada", app_context="Code.exe")
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        # Headless transcribe_file writing style keeps app-scoped learned written forms.
        print(
            json.dumps(
                engine.transcribe_file(
                    Path(args[0]), style="formal", app_context="Code.exe"
                ).to_dict(),
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        engine.unload()
    return 0 if not result.rejected_reason else 1


if __name__ == "__main__":
    raise SystemExit(main())
