# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Scan every input device for live signal.

Run it and KEEP TALKING (count "one, two, three, four ...") the whole time.
Each input device is recorded for ~1.8 s; the loudest are the ones that hear you.

    python scripts/scan_mics.py
"""

from __future__ import annotations

import numpy as np
import sounddevice as sd

SECONDS = 1.8


def _levels(rec: np.ndarray) -> tuple[float, float]:
    rec = np.asarray(rec, dtype=np.float32).reshape(-1)
    if rec.size == 0:
        return 0.0, 0.0
    return float(np.sqrt(np.mean(rec**2))), float(np.max(np.abs(rec)))


def main() -> int:
    devices = sd.query_devices()
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None

    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    print(f"Default input device index: {default_in}")
    print(f"Scanning {len(inputs)} input devices for ~{SECONDS:.1f}s each. SPEAK CONTINUOUSLY.\n")

    results = []
    for i, d in inputs:
        try:
            sr = int(d["default_samplerate"] or 16000)
            rec = sd.rec(int(SECONDS * sr), samplerate=sr, channels=1, dtype="float32", device=i)
            sd.wait()
            rms, peak = _levels(rec)
            results.append((peak, rms, i, d["name"]))
            flag = "  <== HEARS YOU" if peak > 0.02 else ("  (weak)" if peak > 0.005 else "")
            star = " *default" if i == default_in else ""
            print(f"[{i:>2}] peak={peak:.4f} rms={rms:.5f}  {d['name']}{star}{flag}")
        except Exception as exc:  # noqa: BLE001 - report and continue scanning
            print(f"[{i:>2}] ERROR: {exc}  ({d['name']})")

    print()
    live = sorted((r for r in results if r[0] > 0.02), reverse=True)
    if live:
        peak, _rms, idx, name = live[0]
        print(f"BEST MIC: index {idx} peak={peak:.4f} -> {name!r}")
        print("Set it in %APPDATA%/DCENT_Voice/config.toml under [audio]:")
        print(f"    input_device = {idx}")
    else:
        print("No device clearly heard you. Check the mic is unmuted and, for a")
        print("SteelSeries Arctis/Sonar, that Sonar is routing the mic to a capture stream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
