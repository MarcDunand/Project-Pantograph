"""
Build PantographApp/ui/icon.ico from the same drawing as icon.svg: the
pantograph linkage in white on a dark rounded tile, with one blue dot.

Windows shortcuts and the app's window need a .ico, and rasterising an SVG
needs a library we don't otherwise want. The drawing is a rounded square, a
few lines and two dots, so this draws it directly: supersampled into an RGBA
buffer, written as PNG (zlib is stdlib) and wrapped in an ICO.

The tile matters. The first version was dark lines on transparency, which
vanish on a dark taskbar; light lines alone would vanish on a light one. On
its own tile the mark reads on both.

Run it after changing icon.svg:  uv run PantographApp/deferred/make_icon.py
Add a folder to also write each size as a PNG there, to look at:
    uv run PantographApp/deferred/make_icon.py .scratch/icon
"""
import math
import struct
import sys
import zlib
from pathlib import Path

# Everything below is icon.svg, in its 32 × 32 viewBox.
VIEW = 32
TILE_RADIUS = 7
SEGMENTS = [
    (5, 25, 13, 9), (13, 9, 20, 17),           # M5 25 L13 9 L20 17
    (13, 9, 27, 25),                            # M13 9 L27 25
    (9, 17, 20, 17), (20, 17, 23.5, 21),        # M9 17 L20 17 L23.5 21
]
STROKE = 2.2
WHITE_DOT = (5, 25, 1.8)
BLUE_DOT = (27, 25, 1.8)

TILE = (0x1F, 0x23, 0x28)    # the app's near-black
WHITE = (0xFF, 0xFF, 0xFF)
BLUE = (0x1C, 0x7E, 0xD6)

SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4                       # supersampling factor


def _dist_to_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _in_tile(x, y):
    """Inside the rounded square that fills the viewBox."""
    r = TILE_RADIUS
    cx = min(max(x, r), VIEW - r)
    cy = min(max(y, r), VIEW - r)
    return 0 <= x <= VIEW and 0 <= y <= VIEW and math.hypot(x - cx, y - cy) <= r


def _sample(x, y):
    """The colour at one point of the drawing, topmost shape first; None is transparent."""
    if math.hypot(x - BLUE_DOT[0], y - BLUE_DOT[1]) <= BLUE_DOT[2]:
        return BLUE
    if math.hypot(x - WHITE_DOT[0], y - WHITE_DOT[1]) <= WHITE_DOT[2]:
        return WHITE
    for seg in SEGMENTS:
        if _dist_to_segment(x, y, *seg) <= STROKE / 2:
            return WHITE
    return TILE if _in_tile(x, y) else None


def _render(size):
    """RGBA bytes for one size, each pixel the average of SS × SS samples."""
    step = VIEW / (size * SS)
    per = SS * SS
    out = bytearray()
    for py in range(size):
        for px in range(size):
            r = g = b = hits = 0
            for j in range(SS):
                y = (py * SS + j + 0.5) * step
                for i in range(SS):
                    c = _sample((px * SS + i + 0.5) * step, y)
                    if c is not None:
                        r += c[0]
                        g += c[1]
                        b += c[2]
                        hits += 1
            # Straight (not premultiplied) alpha: colour is the average of the
            # samples that landed on the drawing, alpha is how many did.
            out += bytes((r // hits, g // hits, b // hits, hits * 255 // per) if hits else (0, 0, 0, 0))
    return bytes(out)


def _png(size, rgba):
    raw = bytearray()
    for y in range(size):
        raw.append(0)                       # filter: none
        raw += rgba[y * size * 4:(y + 1) * size * 4]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def main():
    images = [(s, _png(s, _render(s))) for s in SIZES]

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

    if len(sys.argv) > 1:                    # previews, to check by eye
        folder = Path(sys.argv[1])
        folder.mkdir(parents=True, exist_ok=True)
        for size, data in images:
            (folder / f"icon-{size}.png").write_bytes(data)
        print(f"wrote {len(images)} PNG previews to {folder}")


if __name__ == "__main__":
    main()
