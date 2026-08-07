"""
log_to_svg.py

Rebuild a plottable SVG from listen_to_idraw's terminal output.

The pipeline prints one line per received point, plus a line whenever the pen
lifts, which together carry everything a replay needs: position, pressure, tool
and stroke boundaries. This turns a saved terminal log back into an SVG in the
same format the preview downloads, so it can be re-plotted with the browser's
"plot svg" button and run through whatever post-processors are switched on.

Usage:
  python log_to_svg.py session.log                  # -> session.svg
  python log_to_svg.py session.log -o drawing.svg

Lines consumed (everything else is ignored):
  [point] (123.4, 567.8)  p=0.42  tool=pen
  [pen up — timeout]
  [mapping] canvas 440×956px → ...

What is faithful, and what is not — see RECONSTRUCTION LIMITS below.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Mirrors listen_to_idraw. Kept as a literal rather than imported so this script
# runs without the pipeline's dependencies (rdp, pythonosc, websockets).
OSC_PRESSURE_MAX = 4.166666507720947

# The log has no timestamps, so replay pacing is synthesised at a constant rate.
# Only the spacing matters: it must stay well under PEN_UP_TIMEOUT_SEC so replay
# never mistakes the gap between two points for a pen lift.
SYNTH_POINT_INTERVAL_SEC = 0.008

# Not printed by the pipeline; these only affect how the SVG looks, never the plot.
DEFAULT_DRAWING_WIDTH = 1.5
DEFAULT_COLOR = {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}

STROKE_THINNING = 0.5   # matches preview.py's renderer

_POINT_RE = re.compile(
    r"\[point\]\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)\s+p=(-?[\d.]+)\s+tool=(\S+)"
)
_PENUP_RE   = re.compile(r"\[pen up")
_MAPPING_RE = re.compile(r"\[mapping\]\s*canvas\s*(\d+)\s*[x×]\s*(\d+)\s*px")


def parse_log(text: str) -> tuple[list, float, float]:
    """Returns (strokes, canvas_w, canvas_h). Each stroke is a list of (x, y, p_norm, tool)."""
    canvas_w = canvas_h = None
    strokes, cur = [], []

    for line in text.splitlines():
        m = _MAPPING_RE.search(line)
        if m:
            canvas_w, canvas_h = float(m.group(1)), float(m.group(2))
            continue
        m = _POINT_RE.search(line)
        if m:
            cur.append((float(m.group(1)), float(m.group(2)),
                        float(m.group(3)), m.group(4)))
            continue
        if _PENUP_RE.search(line) and cur:
            strokes.append(cur)
            cur = []

    if cur:                      # log ended mid-stroke
        strokes.append(cur)
    return strokes, canvas_w, canvas_h


def build_recording(strokes: list, canvas_w: float, canvas_h: float) -> dict:
    out, t = [], 0.0
    for s in strokes:
        pts = []
        for (x, y, p_norm, _tool) in s:
            # The log prints normalised pressure; the pipeline consumes raw.
            pts.append([t, x, y, p_norm * OSC_PRESSURE_MAX])
            t += SYNTH_POINT_INTERVAL_SEC
        out.append({
            "tool":         s[0][3],
            "drawingWidth": DEFAULT_DRAWING_WIDTH,
            "color":        DEFAULT_COLOR,
            "canvasWidth":  canvas_w,
            "canvasHeight": canvas_h,
            "points":       pts,
        })
        t += 0.5   # a clear gap between strokes
    return {"format": "draw2axi-recording", "version": 1, "strokes": out}


def _width_for(size: float, pressure: float) -> float:
    p = max(0.0, min(1.0, pressure))
    return max(0.1, size * (1 - STROKE_THINNING + 2 * STROKE_THINNING * p))


def build_svg(rec: dict, canvas_w: float, canvas_h: float) -> str:
    """
    Renders the same way preview.py does: centerline segments, each width taken
    from the mean pressure of its endpoints.

    Coordinates are written as canvas units. The preview's screen mapping is not
    in the log, so this assumes the defaults (origin 0,0 and a preview sized to
    the canvas) — which is what the page does unless those were edited by hand.
    Only affects the picture; replay reads the metadata.
    """
    size = DEFAULT_DRAWING_WIDTH
    parts = []
    for s in rec["strokes"]:
        pts = [(p[1], p[2], p[3] / OSC_PRESSURE_MAX) for p in s["points"]]
        if len(pts) == 1:
            x, y, p = pts[0]
            parts.append(f'  <circle cx="{x:.2f}" cy="{y:.2f}"'
                         f' r="{_width_for(size, p) / 2:.3f}" fill="#fff"/>')
            continue
        parts.append('  <g stroke="#fff" fill="none" stroke-linecap="round">')
        for a, b in zip(pts, pts[1:]):
            w = _width_for(size, (a[2] + b[2]) / 2)
            parts.append(f'    <path d="M{a[0]:.2f},{a[1]:.2f}L{b[0]:.2f},{b[1]:.2f}"'
                         f' stroke-width="{w:.3f}"/>')
        parts.append('  </g>')

    meta = (json.dumps(rec, separators=(",", ":"))
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w:.0f}" height="{canvas_h:.0f}">',
        f'  <metadata>{meta}</metadata>',
        f'  <rect width="{canvas_w:.0f}" height="{canvas_h:.0f}" fill="#000"/>',
        *parts,
        '</svg>',
    ])


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python log_to_svg.py",
        description="Rebuild a plottable SVG from a listen_to_idraw terminal log.",
    )
    ap.add_argument("log", type=Path, help="saved terminal output")
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="output SVG (default: alongside the log)")
    ap.add_argument("--canvas", default=None, metavar="WxH",
                    help="canvas size, if the log has no [mapping] line (e.g. 440x956)")
    args = ap.parse_args()

    text = args.log.read_text(encoding="utf-8", errors="replace")
    strokes, cw, ch = parse_log(text)

    if not strokes:
        print("[error] no [point] lines found — was the log captured with --raw-osc?")
        return 1

    if args.canvas:
        try:
            w, h = args.canvas.lower().split("x")
            cw, ch = float(w), float(h)
        except ValueError:
            print(f"[error] --canvas must look like 440x956, got {args.canvas!r}")
            return 1
    if not cw or not ch:
        print("[error] no [mapping] line in the log — pass --canvas WxH "
              "(the canvas size iDraw was sending)")
        return 1

    rec = build_recording(strokes, cw, ch)
    svg = build_svg(rec, cw, ch)
    out = args.out or args.log.with_suffix(".svg")
    out.write_text(svg, encoding="utf-8")

    n = sum(len(s) for s in strokes)
    print(f"[ok] {len(strokes)} stroke(s), {n} point(s), canvas {cw:.0f}×{ch:.0f}")
    print(f"[ok] wrote {out}")
    print("     load it with the preview's 'plot svg' button")
    return 0


if __name__ == "__main__":
    sys.exit(main())
