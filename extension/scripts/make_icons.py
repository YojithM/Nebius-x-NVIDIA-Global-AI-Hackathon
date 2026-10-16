#!/usr/bin/env python3
"""Generate the extension's PNG icons, with no image library.

Chrome warns (and looks unfinished) without real icons, but checking in binary
blobs you can't regenerate is worse. This draws them from math instead: a
rounded-square badge with a white microphone glyph, anti-aliased by
supersampling. Re-run it any time to regenerate:

    python3 extension/scripts/make_icons.py
"""

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "public" / "icons"
SIZES = (16, 48, 128)
SAMPLES = 4  # supersampling factor per axis

BG_TOP = (125, 125, 245)
BG_BOTTOM = (78, 78, 190)
WHITE = (255, 255, 255)


def _rounded_square(x, y, corner=0.34):
    """Inside a rounded square spanning [-1, 1] on both axes?"""
    ax, ay = abs(x), abs(y)
    straight = 1 - corner
    if ax <= straight or ay <= straight:
        return ax <= 1 and ay <= 1
    return (ax - straight) ** 2 + (ay - straight) ** 2 <= corner**2


def _mic_glyph(x, y):
    """The white microphone: capsule, then the arc beneath it, then the stem."""
    # Capsule body — a stadium from y=-0.50 to y=0.10, half-width 0.17.
    radius, top, bottom = 0.17, -0.50, 0.10
    clamped = min(max(y, top + radius), bottom - radius)
    if (x**2 + (y - clamped) ** 2) <= radius**2:
        return True

    # The U-shaped arc under the capsule.
    if y >= -0.08:
        distance = (x**2 + (y + 0.08) ** 2) ** 0.5
        if 0.30 <= distance <= 0.40:
            return True

    # Stem down to the base.
    if abs(x) <= 0.055 and 0.30 <= y <= 0.50:
        return True

    return False


def render(size):
    """Return raw RGBA rows for one icon size."""
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            r = g = b = a = 0
            hits = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    # Sample position in normalised [-1, 1] space.
                    x = ((px + (sx + 0.5) / SAMPLES) / size) * 2 - 1
                    y = ((py + (sy + 0.5) / SAMPLES) / size) * 2 - 1
                    if not _rounded_square(x, y):
                        continue
                    hits += 1
                    if _mic_glyph(x, y):
                        r += WHITE[0]
                        g += WHITE[1]
                        b += WHITE[2]
                    else:
                        # Vertical gradient across the badge.
                        t = (y + 1) / 2
                        r += int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
                        g += int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
                        b += int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
            total = SAMPLES * SAMPLES
            if hits:
                row.append(
                    (r // hits, g // hits, b // hits, int(255 * hits / total))
                )
            else:
                row.append((0, 0, 0, 0))
        rows.append(row)
    return rows


def write_png(path, size, rows):
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBBB", *pixel) for pixel in row)
        for row in rows
    )

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT_DIR / f"icon{size}.png"
        write_png(path, size, render(size))
        print(f"  wrote {path.relative_to(OUT_DIR.parents[2])} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
