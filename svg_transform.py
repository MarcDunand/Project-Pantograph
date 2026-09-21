"""
svg_transform.py

Transforms for an existing plot SVG — the kind Pantograph saves — each writing
out a new SVG that can be plotted again.

This is a *sibling* to the post-processors, not one of them. The post-processors
run live, inside the plotting pipeline, on the point stream. These transforms run
offline, on a finished SVG, as a separate step you invoke by hand.

WHAT IT OPERATES ON
-------------------
A plot SVG carries two things: a <metadata> block holding the recording (the
JSON list of strokes/points that the plotter actually replays) and cosmetic
<path>/<circle> elements so the file looks right in a viewer. **The recording is
the source of truth** — importing one reads only the metadata. So every
transform here rewrites the recording and then regenerates matching visuals
from it, keeping the two in sync.

Point format inside the recording: [t, x, y, pressureRaw], where pressureRaw is
in 0..OSC_PRESSURE_MAX and x/y are in that stroke's own canvas units. Line
thickness is pressure-derived exactly as the renderers compute it:
    width = drawingWidth * (0.5 + pressureNorm),   pressureNorm = raw / OSC_MAX
so the filter's notion of "thickness" matches what you see and what the pen lays
down.

TRANSFORMS
----------
  * Flip horizontal — mirror around the vertical centerline.
  * Flip vertical   — mirror around the horizontal centerline.
    Both flip each stroke around ITS OWN canvas center, because the plotter
    re-maps every stroke to the paper by its own canvasWidth/Height. For the
    common single-canvas drawing this is just a mirror about the middle.
  * Filter by min width — keep only strokes whose thickness is at least a
    fraction of the thickest stroke in the drawing. The 0..100% slider is that
    fraction: 0% keeps everything, 50% keeps strokes at least half as thick as
    the thickest, 100% keeps only the very thickest. A stroke is kept or dropped
    whole (never split); its thickness is its thickest segment by default
    (transform() also takes metric="mean").

These are command-line transforms, not part of the app: Pantograph plots what
a pen and the machine can actually do, and doesn't edit finished drawings.
Running this file with a drawing simply opens it in Pantograph, ready to
import onto the canvas.

Run:  python svg_transform.py [drawing.svg]
Self-test (no window):  python svg_transform.py --selftest path/to/some.svg
"""

import argparse
import copy
import sys
from pathlib import Path
from statistics import mean

# The recording format lives in recording.py; these are re-exported so older
# imports (`from svg_transform import load_svg, build_svg`) keep working.
from recording import (DEFAULT_DRAWING_WIDTH, OSC_PRESSURE_MAX, build_svg,  # noqa: F401
                       load_svg, width_for)

_EPS = 1e-9


# ──────────────────────────────────────────────────────────────────────────────
# CORE (pure functions — no GUI, importable and testable)
# ──────────────────────────────────────────────────────────────────────────────

def _stroke_canvas(stroke: dict, viewport_w: float, viewport_h: float) -> tuple[float, float]:
    cw = stroke.get("canvasWidth")  or viewport_w
    ch = stroke.get("canvasHeight") or viewport_h
    return cw, ch


def segment_widths(stroke: dict) -> list[float]:
    """Pressure-derived width of every segment (or the single dot) in a stroke."""
    size = stroke.get("drawingWidth", DEFAULT_DRAWING_WIDTH)
    pts = stroke.get("points") or []
    if not pts:
        return []
    if len(pts) == 1:
        return [width_for(size, pts[0][3] / OSC_PRESSURE_MAX)]
    widths = []
    for a, b in zip(pts, pts[1:]):
        pa = a[3] / OSC_PRESSURE_MAX
        pb = b[3] / OSC_PRESSURE_MAX
        widths.append(width_for(size, (pa + pb) / 2.0))
    return widths


def stroke_thickness(stroke: dict, metric: str = "max") -> float:
    """A single representative thickness for a stroke (used by the width filter)."""
    ws = segment_widths(stroke)
    if not ws:
        return 0.0
    return mean(ws) if metric == "mean" else max(ws)


def max_thickness(rec: dict, metric: str = "max") -> float:
    """Thickness of the thickest stroke in the drawing — the filter's 100% anchor."""
    return max((stroke_thickness(s, metric) for s in rec.get("strokes", [])),
               default=0.0)


def apply_flip_h(rec: dict, viewport_w: float, viewport_h: float) -> None:
    """Mirror left↔right, in place — each stroke around its own canvas center."""
    for s in rec.get("strokes", []):
        cw, _ = _stroke_canvas(s, viewport_w, viewport_h)
        for p in s.get("points", []):
            p[1] = cw - p[1]


def apply_flip_v(rec: dict, viewport_w: float, viewport_h: float) -> None:
    """Mirror top↕bottom, in place — each stroke around its own canvas center."""
    for s in rec.get("strokes", []):
        _, ch = _stroke_canvas(s, viewport_w, viewport_h)
        for p in s.get("points", []):
            p[2] = ch - p[2]


def filter_min_width(rec: dict, percent: float, metric: str = "max") -> dict:
    """
    Drop strokes thinner than `percent`% of the thickest stroke. Whole strokes
    only — a kept stroke is unchanged. Mutates rec's stroke list; returns stats.
    """
    strokes = rec.get("strokes", [])
    max_w = max_thickness(rec, metric)
    threshold = (percent / 100.0) * max_w

    kept = []
    for s in strokes:
        if stroke_thickness(s, metric) >= threshold - _EPS:
            kept.append(s)
    dropped = len(strokes) - len(kept)
    rec["strokes"] = kept
    return {
        "total_strokes": len(strokes),
        "kept_strokes":  len(kept),
        "dropped_strokes": dropped,
        "max_width":     max_w,
        "threshold":     threshold,
        "percent":       percent,
        "metric":        metric,
    }


def transform(rec0: dict, viewport_w: float, viewport_h: float, *,
              flip_h: bool = False, flip_v: bool = False,
              percent: float = 0.0, metric: str = "max") -> tuple[dict, dict]:
    """Apply the chosen transforms to a fresh copy of rec0. Returns (rec, stats)."""
    rec = copy.deepcopy(rec0)
    if flip_h:
        apply_flip_h(rec, viewport_w, viewport_h)
    if flip_v:
        apply_flip_v(rec, viewport_w, viewport_h)
    stats = filter_min_width(rec, percent, metric)
    stats["flip_h"] = flip_h
    stats["flip_v"] = flip_v
    return rec, stats


# ──────────────────────────────────────────────────────────────────────────────
# SELF-TEST (headless)
# ──────────────────────────────────────────────────────────────────────────────
def _selftest(path: str) -> int:
    rec, vw, vh = load_svg(path)
    n0 = len(rec["strokes"])
    print(f"[load] {n0} strokes, viewport {vw:.0f}x{vh:.0f}")

    # Flip is an involution: applying it twice restores the original coords.
    orig_xy = [(p[1], p[2]) for s in rec["strokes"] for p in s["points"]]
    r2, _ = transform(rec, vw, vh, flip_h=True, flip_v=True, percent=0.0)
    r2b, _ = transform(r2, vw, vh, flip_h=True, flip_v=True, percent=0.0)
    back_xy = [(p[1], p[2]) for s in r2b["strokes"] for p in s["points"]]
    assert all(abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6
               for a, b in zip(orig_xy, back_xy)), "double flip must be identity"
    print("[flip] double flip is identity")

    # A single flip-H mirrors x about each stroke's own canvas width.
    rH, _ = transform(rec, vw, vh, flip_h=True, percent=0.0)
    s0, sH = rec["strokes"][0], rH["strokes"][0]
    cw = s0.get("canvasWidth") or vw
    assert abs(sH["points"][0][1] - (cw - s0["points"][0][1])) < 1e-6
    print("[flip] flip-H mirrors about per-stroke canvas")

    # Filter is monotonic: higher % keeps no more strokes; endpoints behave.
    counts = []
    for pct in (0, 25, 50, 75, 100):
        _, st = transform(rec, vw, vh, percent=pct)
        counts.append(st["kept_strokes"])
        print(f"[filter] {pct:3d}% → kept {st['kept_strokes']:4d}/{n0} "
              f"(threshold {st['threshold']:.3f})")
    assert counts[0] == n0, "0% must keep every stroke"
    assert all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1)), \
        "keep-count must be non-increasing in %"
    assert counts[-1] >= 1, "100% keeps at least the thickest stroke"

    # Round-trip: the rebuilt SVG re-parses to the same stroke count.
    rF, st = transform(rec, vw, vh, flip_h=True, percent=50.0)
    svg = build_svg(rF, vw, vh)
    tmp = Path(path).with_name("_selftest_out.svg")
    tmp.write_text(svg, encoding="utf-8")
    rr, _, _ = load_svg(tmp)
    assert len(rr["strokes"]) == st["kept_strokes"], "round-trip stroke mismatch"
    tmp.unlink()
    print(f"[roundtrip] rebuilt SVG re-parses to {len(rr['strokes'])} strokes")
    print("ALL SELF-TESTS PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python svg_transform.py",
        description="Open a drawing in Pantograph. The transforms here are a library, "
                    "used by the tests and importable from Python.")
    ap.add_argument("svg", nargs="?", help="drawing to open")
    ap.add_argument("--selftest", action="store_true",
                    help="run headless self-tests on the given SVG and exit")
    args = ap.parse_args()

    if args.selftest:
        if not args.svg:
            print("[error] --selftest needs an SVG path")
            return 2
        return _selftest(args.svg)

    import listen_to_idraw
    return listen_to_idraw.main(["--open", args.svg] if args.svg else [])


if __name__ == "__main__":
    sys.exit(main())
