#!/usr/bin/env python3
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Render the DCENT_Voice brand icon (particles + waveform) to PNG/ICO assets.

Run from the repository root:

    python scripts/render_brand_icon.py

Outputs:
  src/dcent_voice/ui/web/icons/app-icon-*.png
  src/dcent_voice/ui/web/icons/app-icon.ico
  packaging/dcent-voice.ico
  packaging/dcent-voice-256.png
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "dcent_voice" / "ui" / "web" / "icons"
PACK = ROOT / "packaging"


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _clamp(x: float, lo: int = 0, hi: int = 255) -> int:
    return max(lo, min(hi, int(x)))


def _radial_sphere(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    r: int,
    colors: list[tuple[float, tuple[int, int, int]]],
) -> None:
    for i in range(r, 0, -1):
        t = 1 - i / r
        for j in range(len(colors) - 1):
            t0, c0 = colors[j]
            t1, c1 = colors[j + 1]
            if t0 <= t <= t1 or j == len(colors) - 2:
                u = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                u = max(0.0, min(1.0, u))
                col = tuple(_clamp(_lerp(c0[k], c1[k], u)) for k in range(3)) + (255,)
                draw.ellipse((cx - i, cy - i, cx + i, cy + i), fill=col)
                break
    hx, hy = cx - int(r * 0.28), cy - int(r * 0.35)
    hr = int(r * 0.38)
    for i in range(hr, 0, -1):
        a = int(200 * (i / hr) ** 1.6)
        draw.ellipse(
            (hx - i, hy - i // 1.4, hx + i, hy + i // 1.4),
            fill=(255, 255, 255, min(255, a)),
        )


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pd = ImageDraw.Draw(plate)
    margin = int(size * 0.04)
    pd.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=int(size * 0.22),
        fill=(22, 16, 12, 255),
    )
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((size * 0.12, size * 0.12, size * 0.88, size * 0.88), fill=(255, 110, 0, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=size * 0.08))
    plate = Image.alpha_composite(plate, glow)
    img = Image.alpha_composite(img, plate)

    cx = cy = size // 2
    particles = [
        (cx, cy + int(size * 0.02), size * 0.20, True),
        (cx - int(size * 0.28), cy - int(size * 0.06), size * 0.12, False),
        (cx + int(size * 0.26), cy + int(size * 0.08), size * 0.11, False),
        (cx - int(size * 0.12), cy + int(size * 0.24), size * 0.07, False),
        (cx + int(size * 0.18), cy - int(size * 0.22), size * 0.06, False),
    ]
    big = [
        (0.0, (255, 242, 166)),
        (0.18, (255, 194, 74)),
        (0.48, (247, 147, 26)),
        (0.78, (217, 101, 0)),
        (1.0, (169, 65, 0)),
    ]
    small = [
        (0.0, (255, 240, 160)),
        (0.2, (255, 186, 55)),
        (0.52, (247, 147, 26)),
        (0.82, (212, 91, 0)),
        (1.0, (154, 57, 0)),
    ]

    for px, py, pr, is_core in sorted(particles, key=lambda p: p[2]):
        sh = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        sr = int(pr * 1.15)
        sd.ellipse(
            (px - sr, py - sr + int(pr * 0.12), px + sr, py + sr + int(pr * 0.12)),
            fill=(123, 42, 0, 90),
        )
        sh = sh.filter(ImageFilter.GaussianBlur(radius=max(1, int(pr * 0.35))))
        img = Image.alpha_composite(img, sh)
        d = ImageDraw.Draw(img)
        _radial_sphere(d, px, py, int(pr), big if is_core else small)

    wave = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wave)
    n = 48
    x0, x1 = size * 0.14, size * 0.86
    pattern = [
        0,
        0.02,
        -0.01,
        0.05,
        -0.08,
        0.14,
        -0.18,
        0.22,
        -0.12,
        0.08,
        -0.04,
        0.15,
        -0.2,
        0.1,
        -0.05,
        0.03,
        0,
        0.04,
        -0.02,
        0,
    ]
    pts: list[tuple[float, float]] = []
    for i in range(n):
        t = i / (n - 1)
        x = x0 + (x1 - x0) * t
        p = t * (len(pattern) - 1)
        i0 = int(p)
        i1 = min(i0 + 1, len(pattern) - 1)
        u = p - i0
        h = _lerp(pattern[i0], pattern[i1], u)
        envelope = math.sin(t * math.pi) ** 0.7
        y = cy + h * size * 0.55 * envelope
        pts.append((x, y))

    for w, a in ((int(size * 0.045), 70), (int(size * 0.028), 140), (int(size * 0.016), 255)):
        for i in range(len(pts) - 1):
            t = i / (len(pts) - 1)
            if t < 0.5:
                u = t * 2
                col = (
                    _clamp(_lerp(255, 255, u)),
                    _clamp(_lerp(138, 210, u)),
                    _clamp(_lerp(0, 74, u)),
                    a,
                )
            else:
                u = (t - 0.5) * 2
                col = (
                    _clamp(_lerp(255, 255, u)),
                    _clamp(_lerp(210, 122, u)),
                    _clamp(_lerp(74, 0, u)),
                    a,
                )
            wd.line([pts[i], pts[i + 1]], fill=col, width=max(1, w))
    wave = wave.filter(ImageFilter.GaussianBlur(radius=max(0.5, size * 0.004)))

    sharp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sharp)
    w = max(2, int(size * 0.018))
    for i in range(len(pts) - 1):
        t = i / (len(pts) - 1)
        if t < 0.5:
            u = t * 2
            col = (255, _clamp(_lerp(138, 210, u)), _clamp(_lerp(0, 90, u)), 255)
        else:
            u = (t - 0.5) * 2
            col = (255, _clamp(_lerp(210, 140, u)), _clamp(_lerp(90, 20, u)), 255)
        sd.line([pts[i], pts[i + 1]], fill=col, width=w)

    img = Image.alpha_composite(img, wave)
    img = Image.alpha_composite(img, sharp)
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    PACK.mkdir(parents=True, exist_ok=True)
    master = render(1024)
    master.save(OUT / "app-icon-1024.png")
    sizes = (512, 256, 128, 64, 48, 32, 16)
    images = []
    for s in sizes:
        im = master.resize((s, s), Image.Resampling.LANCZOS)
        im.save(OUT / f"app-icon-{s}.png")
        if s in (256, 128, 64, 48, 32, 16):
            images.append(im)
    images[0].save(
        OUT / "app-icon.ico",
        format="ICO",
        sizes=[(im.width, im.height) for im in images],
        append_images=images[1:],
    )
    shutil.copy(OUT / "app-icon.ico", PACK / "dcent-voice.ico")
    shutil.copy(OUT / "app-icon-256.png", PACK / "dcent-voice-256.png")
    print(f"Wrote icons to {OUT} and {PACK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
