# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Copy-paste ADE attach: discover the running module, transcribe a WAV, compose text.

No tray, Settings UI, or global hotkeys. Loopback + bearer token only.

    python examples/attach_ade.py tests/fixtures/audio/hello.wav

In-process (no HTTP service) is examples/attach_engine.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dcent_voice.attach import AttachError, VoiceAttachClient


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python examples/attach_ade.py <file.wav>", file=sys.stderr)
        return 2
    try:
        with VoiceAttachClient.discover() as client:
            caps = client.capabilities()
            print(json.dumps(caps, indent=2, sort_keys=True))
            print(json.dumps(client.ready(), indent=2, sort_keys=True))
            result = client.transcribe_file(Path(args[0]), polish=False)
            print(json.dumps(result, indent=2, sort_keys=True))
            # ADE attach transcribe writing style keeps app-scoped learned written forms.
            print(
                json.dumps(
                    client.transcribe_file(Path(args[0]), style="formal", app_context="Code.exe"),
                    indent=2,
                    sort_keys=True,
                )
            )
            # ADE attach transcribe oneshot writing style keeps app-scoped learned written forms.
            print(
                json.dumps(
                    client.transcribe(
                        {
                            "audio": [0.0] * 1600,
                            "samplerate": 16000,
                            "style": "formal",
                            "app_context": "Code.exe",
                        }
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            # ADE attach stream writing style keeps app-scoped learned written forms.
            print(
                json.dumps(
                    client.stream([0.0] * 1600, style="formal", app_context="Code.exe"),
                    indent=2,
                    sort_keys=True,
                )
            )
            # ADE attach learn records app-scoped learned written forms.
            # client.learn("vip", "I'm Ada", app_context="Code.exe")
            # ADE attach personalization snapshot returns app-scoped learned written forms.
            print(json.dumps(client.personalization(), indent=2, sort_keys=True))
            # ADE attach compose writing style keeps dictionary written forms.
            # ADE attach compose writing style keeps snippet expansions.
            composed = client.compose("the meeting is at 5 actually 6", style="email")
            print(json.dumps(composed, indent=2, sort_keys=True))
    except AttachError as exc:
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True), file=sys.stderr)
        return 1
    return 1 if result.get("rejected_reason") else 0


if __name__ == "__main__":
    raise SystemExit(main())
