"""
Build PantographApp/ui/icon.ico from the same pantograph drawing as icon.svg.

Windows shortcuts need a .ico, and rasterising an SVG needs a library we don't
otherwise want. The drawing is only lines and two dots, so this draws it
directly: supersampled into an RGBA buffer, written as PNG (zlib is stdlib) and
wrapped in an ICO.

Run it after changing icon.svg:  uv run PantographApp/deferred/make_icon.py
"""
import math
import struct
import zlib
from pathlib import Path

# The linkage, in icon.svg's 26 × 20 viewBox.
SEGMENTS = [
    (2, 18, 9, 4), (9, 4, 16, 11),            # M2 18 L9 4 L16 11
    (9, 4, 24, 18),                            # M9 4 L24 18
    (5.5, 11, 12.5, 11), (12.5, 11, 16, 11), (16, 11, 20, 14.5),
]
DOTS = [(2, 18, 1.4), (24, 18, 1.4)]
VIEW_W, VIEW_H = 26, 20
STROKE = 1.6

SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4                      # supersampling factor
INK = (0x1F, 0x23, 0x28)    # the app's near-black, on transparency


def _dist_to_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _coverage(size):
    """Alpha per pixel, by sampling SS×SS points inside each one."""
    n = size * SS
    # Fit the viewBox into the square, with a little air around it.
    scale = min(n / VIEW_W, n / VIEW_H) * 0.86
    ox = (n - VIEW_W * scale) / 2
    oy = (n - VIEW_H * scale) / 2
    half = STROKE * scale / 2

    hit = bytearray(n * n)
    for sy in range(n):
        uy = (sy + 0.5 - oy) / scale
        for sx in range(n):
            ux = (sx + 0.5 - ox) / scale
            on = False
            for (x1, y1, x2, y2) in SEGMENTS:
                if _dist_to_segment(ux, uy, x1, y1, x2, y2) * scale <= half:
                    on = True
                    break
            if not on:
                for (cx, cy, r) in DOTS:
                    if math.hypot(ux - cx, uy - cy) <= r:
                        on = True
                        break
            if on:
                hit[sy * n + sx] = 1

    alpha = bytearray(size * size)
    per = SS * SS
    for y in range(size):
        for x in range(size):
            c = 0
            for j in range(SS):
                row = (y * SS + j) * n + x * SS
                for i in range(SS):
                    c += hit[row + i]
            alpha[y * size + x] = (c * 255) // per
    return alpha


def _png(size, alpha):
    r, g, b = INK
    raw = bytearray()
    for y in range(size):
        raw.append(0)                       # filter: none
        for x in range(size):
            a = alpha[y * size + x]
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def main():
    images = [(s, _png(s, _coverage(s))) for s in SIZES]

    header = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs = b"", b""
    offset = len(header) + 16 * len(images)
    for size, data in images:
        d = 0 if size >= 256 else size       # 0 means 256 in an ICO
        entries += struct.pack("<BBBBHHII", d, d, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)

    out = Path(__file__).resolve().parents[1] / "ui" / "icon.ico"
    out.write_bytes(header + entries + blobs)
    print(f"wrote {out} ({out.stat().st_size:,} bytes, {len(images)} sizes)")


if __name__ == "__main__":
    main()
