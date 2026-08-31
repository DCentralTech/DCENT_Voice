# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Remix committed LibriSpeech fixtures with additive white noise at a fixed SNR.

Reproducible: pinned RNG seed, 16 kHz mono int16. Not a new recording.
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SEED = 47
SNR_DB = 8.0


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"{path} must be 16-bit mono")
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64) / 32768.0
    return samples, rate


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = np.round(clipped * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def mix_awgn(samples: np.ndarray, *, snr_db: float, seed: int) -> np.ndarray:
    power = float(np.mean(np.square(samples)))
    if power < 1e-10:
        raise ValueError("refusing to mix noise onto silence")
    noise_power = power / (10.0 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(samples.shape[0])
    noise *= np.sqrt(noise_power / float(np.mean(np.square(noise))))
    return samples + noise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mix AWGN onto eval fixtures.")
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--snr-db", type=float, default=SNR_DB)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    source = args.source if args.source.is_absolute() else ROOT / args.source
    dest = args.dest if args.dest.is_absolute() else ROOT / args.dest
    samples, rate = _read_wav(source)
    mixed = mix_awgn(samples, snr_db=args.snr_db, seed=args.seed)
    _write_wav(dest, mixed, rate)
    print(f"wrote {dest} snr_db={args.snr_db} seed={args.seed} n={len(mixed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
