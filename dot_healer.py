"""
dot_healer.py

Repair strokes that a too-fast pen tore into a trail of dots.

THE BUG THIS FIXES
------------------
listen_to_idraw infers stroke boundaries from *time*: if the gap between two
received OSC points exceeds PEN_UP_TIMEOUT_SEC, it assumes the pen lifted and
ends the stroke. When the Pencil is dragged quickly, iDraw sends points far
enough apart in time that this timeout fires between *consecutive* points — so a
single continuous line gets chopped into a run of one-point strokes. In the
plot SVG those one-point strokes render as `<circle>` dots, while the slower
sections on either side stayed intact as `<g>` line groups. The signature is
unmistakable:

    line ─ • • • • ─ line          (a group, then circles, then a group)

You can see it in a viewer as a solid line that dissolves into a dotted line and
then resolves back into a solid line — the pen never actually left the paper
there; the pipeline only thought it did.

THE HEAL
--------
The recording inside the SVG's <metadata> is what actually gets re-plotted (see
svg_transform.py / preview.py's uploadSVG), and replay draws every stroke in it
as one uninterrupted pen-down (listen_to_idraw._replay_recording). So healing is
just: find each `line, dots…, line` run and concatenate those strokes back into
one. The rebuilt stroke replays as a single continuous line — no lifts, no dots
— exactly as it was drawn. The cosmetic <path>/<circle> elements are regenerated
from the mended recording, so the picture matches the plot.

WHAT COUNTS AS A RUN (and the guards against false positives)
-------------------------------------------------------------
A run is merged only when all of these hold, so a deliberate tap-dot or two
unrelated marks that happen to sit near each other are left alone:

  * It is bracketed by a real line (>= 2 points) on BOTH sides — a stray dot at
    the very start or end of the drawing has nothing to reconnect and is kept.
  * At least one dot sits between the two lines.
  * Every hand-off along the run — line→first dot, dot→dot, last dot→line — is
    shorter than --max-gap. A continuous fast stroke leaves closely spaced dots;
    a large jump means these were separate marks, so the run breaks there.
  * The strokes share a canvas and tool (they came from one drawing gesture).

A run may chain (line, dots, line, dots, line → one stroke) as long as each link
passes. Point timestamps are already monotonic across strokes in draw order, so
concatenating them keeps replay pacing valid.

USAGE
-----
  python dot_healer.py drawing.svg                 # -> drawing_healed.svg
  python dot_healer.py drawing.svg -o mended.svg
  python dot_healer.py drawing.svg --max-gap 20
  python dot_healer.py drawing.svg --dry-run       # report only, write nothing

Load the result with the preview's "plot svg" button. This is an offline,
by-hand step — a sibling to svg_transform.py, not one of the live
post-processors.
"""

import argparse
import copy
import math
import sys
from pathlib import Path

# Reuse the pipeline's exact SVG parse/serialize so the healed file round-trips
# in the same format everything else in draw2axi speaks.
from svg_transform import load_svg, build_svg


# Fraction of the canvas diagonal used as the default max hand-off gap when the
# user does not pass --max-gap. Generous enough to bridge a fast dotted trail,
# tight enough that marks on opposite sides of the page never weld together.
DEFAULT_GAP_FRACTION = 0.06


# ──────────────────────────────────────────────────────────────────────────────
# STROKE PREDICATES / GEOMETRY
# ──────────────────────────────────────────────────────────────────────────────
def _points(stroke: dict) -> list:
    return stroke.get("points") or []

def is_dot(stroke: dict) -> bool:
    """A one-point stroke — what a torn-off fast point becomes (renders as a circle)."""
    return len(_points(stroke)) == 1

def is_line(stroke: dict) -> bool:
    """A stroke the pen actually drew as a line (renders as a path group)."""
    return len(_points(stroke)) >= 2

def _first_xy(stroke: dict) -> tuple:
    p = _points(stroke)[0]
    return (p[1], p[2])

def _last_xy(stroke: dict) -> tuple:
    p = _points(stroke)[-1]
    return (p[1], p[2])

def _canvas(stroke: dict, vw: float, vh: float) -> tuple:
    return (stroke.get("canvasWidth") or vw, stroke.get("canvasHeight") or vh)


def _joinable(a: dict, b: dict, max_gap: float, vw: float, vh: float) -> bool:
    """
    True if b directly continues a: same canvas and tool, and the jump from a's
    last point to b's first point is within max_gap. This is the per-link test
    the whole run must pass end to end.
    """
    if _canvas(a, vw, vh) != _canvas(b, vw, vh):
        return False
    if a.get("tool", "pen") != b.get("tool", "pen"):
        return False
    (ax, ay), (bx, by) = _last_xy(a), _first_xy(b)
    return math.hypot(bx - ax, by - ay) <= max_gap


def _merge(strokes: list) -> dict:
    """Concatenate a run into one stroke, keeping the first line's metadata."""
    out = copy.deepcopy(strokes[0])
    pts = list(out.get("points") or [])
    for s in strokes[1:]:
        pts.extend(copy.deepcopy(_points(s)))
    out["points"] = pts
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CORE
# ──────────────────────────────────────────────────────────────────────────────
def auto_max_gap(vw: float, vh: float) -> float:
    """Default hand-off gap: a small fraction of the canvas diagonal."""
    return DEFAULT_GAP_FRACTION * math.hypot(vw, vh)


def heal_recording(rec: dict, vw: float, vh: float,
                   max_gap: float | None = None) -> dict:
    """
    Mend every `line, dots…, line` run in rec, in place. Returns stats.

    Walks the stroke list once. At each line, greedily grows the longest chain
    of `dots…, line` continuations whose every link is joinable (one or more
    dots between each pair of lines), then replaces the whole chain with one
    merged stroke.
    """
    if max_gap is None:
        max_gap = auto_max_gap(vw, vh)

    strokes = rec.get("strokes") or []
    n = len(strokes)
    healed: list = []
    runs = 0
    dots_healed = 0

    def joinable(a, b):
        return _joinable(a, b, max_gap, vw, vh)

    i = 0
    while i < n:
        s = strokes[i]
        if is_line(s):
            chain = [i]          # stroke indices to merge, starting at this line
            j = i + 1
            while j < n:
                # Scan a maximal run of joinable dots starting at j.
                k = j
                while (k < n and is_dot(strokes[k])
                       and joinable(strokes[k - 1] if k > j else strokes[chain[-1]],
                                    strokes[k])):
                    k += 1
                n_dots = k - j
                # Accept the run only if it has at least one dot and closes on a line.
                if (n_dots >= 1 and k < n and is_line(strokes[k])
                        and joinable(strokes[k - 1], strokes[k])):
                    chain.extend(range(j, k + 1))
                    dots_healed += n_dots
                    j = k + 1
                else:
                    break
            if len(chain) > 1:
                healed.append(_merge([strokes[c] for c in chain]))
                runs += 1
                i = j
                continue
        healed.append(s)
        i += 1

    rec["strokes"] = healed
    return {
        "total_strokes":  n,
        "healed_strokes": len(healed),
        "removed_strokes": n - len(healed),
        "runs":           runs,
        "dots_healed":    dots_healed,
        "max_gap":        max_gap,
    }


# ──────────────────────────────────────────────────────────────────────────────
# SELF-TEST (headless)
# ──────────────────────────────────────────────────────────────────────────────
def _mk_stroke(pts: list, tool: str = "pen", cw: float = 100.0, ch: float = 100.0) -> dict:
    return {"tool": tool, "drawingWidth": 1.5,
            "color": {"r": 1, "g": 1, "b": 1, "a": 1},
            "canvasWidth": cw, "canvasHeight": ch,
            "points": [[i * 0.01, x, y, 2.0] for i, (x, y) in enumerate(pts)]}


def _selftest() -> int:
    # A line, four fast dots continuing it, then a line — the classic tear.
    strokes = [
        _mk_stroke([(0, 0), (10, 0)]),          # line
        _mk_stroke([(12, 0)]),                  # dot
        _mk_stroke([(14, 0)]),                  # dot
        _mk_stroke([(16, 0)]),                  # dot
        _mk_stroke([(18, 0)]),                  # dot
        _mk_stroke([(20, 0), (30, 0)]),         # line
        _mk_stroke([(80, 80)]),                 # lone far dot — must be left alone
    ]
    rec = {"format": "draw2axi-recording", "version": 1, "strokes": strokes}

    st = heal_recording(copy.deepcopy(rec), 100, 100, max_gap=5.0)
    assert st["runs"] == 1, st
    assert st["dots_healed"] == 4, st
    assert st["healed_strokes"] == 2, st         # merged stroke + the lone dot
    print("[selftest] classic line-dots-line run healed into one stroke")

    # A run of dots not bracketed by a line on both sides stays untouched.
    only_lead = {"strokes": [_mk_stroke([(0, 0), (10, 0)]),
                             _mk_stroke([(12, 0)]), _mk_stroke([(14, 0)])]}
    st2 = heal_recording(only_lead, 100, 100, max_gap=5.0)
    assert st2["runs"] == 0, st2
    print("[selftest] dots with no closing line are left alone")

    # A single dot between two lines still heals (no minimum-dots gate).
    one_dot = {"strokes": [_mk_stroke([(0, 0), (10, 0)]),
                           _mk_stroke([(12, 0)]),
                           _mk_stroke([(14, 0), (24, 0)])]}
    st3 = heal_recording(copy.deepcopy(one_dot), 100, 100, max_gap=5.0)
    assert st3["runs"] == 1, st3
    print("[selftest] a single bracketed dot is healed")

    # A big gap breaks the run: the far dot is not joinable.
    far = {"strokes": [_mk_stroke([(0, 0), (10, 0)]),
                       _mk_stroke([(12, 0)]), _mk_stroke([(99, 0)]),
                       _mk_stroke([(100, 0), (110, 0)])]}
    st5 = heal_recording(copy.deepcopy(far), 100, 100, max_gap=5.0)
    assert st5["runs"] == 0, st5
    print("[selftest] a gap wider than --max-gap breaks the run")

    print("ALL SELF-TESTS PASSED")
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python dot_healer.py",
        description="Reconnect strokes a fast pen tore into a trail of dots "
                    "(line, dots…, line → one continuous stroke).")
    ap.add_argument("svg", nargs="?", type=Path, help="plot SVG to heal")
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="output SVG (default: <input>_healed.svg)")
    ap.add_argument("--max-gap", type=float, default=None, metavar="UNITS",
                    help="longest hand-off between consecutive elements, in canvas "
                         "units (default: auto, ~6%% of the canvas diagonal)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be healed but write nothing")
    ap.add_argument("--selftest", action="store_true",
                    help="run headless self-tests and exit")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not args.svg:
        ap.error("an SVG path is required (or pass --selftest)")

    try:
        rec, vw, vh = load_svg(args.svg)
    except Exception as e:            # noqa: BLE001 — surface any load failure plainly
        print(f"[error] {e}")
        return 1

    st = heal_recording(rec, vw, vh, max_gap=args.max_gap)
    print(f"[ok] {st['total_strokes']} strokes in, {st['healed_strokes']} out - "
          f"healed {st['runs']} torn run(s), reconnecting {st['dots_healed']} dot(s)")
    print(f"     max-gap {st['max_gap']:.2f} canvas units")

    if st["runs"] == 0:
        print("     nothing to heal - no line-dots-line runs matched.")
    if args.dry_run:
        print("     (dry run — no file written)")
        return 0

    out = args.out or args.svg.with_name(f"{args.svg.stem}_healed.svg")
    out.write_text(build_svg(rec, vw, vh), encoding="utf-8")
    print(f"[ok] wrote {out}")
    print("     load it with the preview's 'plot svg' button")
    return 0


if __name__ == "__main__":
    sys.exit(main())
