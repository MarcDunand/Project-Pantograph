"""
svg_transform.py

A standalone GUI tool for transforming an existing plot SVG — the kind
listen_to_idraw / the browser preview export, or log_to_svg.py produces — and
writing out a new SVG that can be re-plotted with the preview's "plot svg"
button.

This is a *sibling* to the post-processors, not one of them. The post-processors
run live, inside the plotting pipeline, on the point stream. These transforms run
offline, on a finished SVG, as a separate step you invoke by hand.

WHAT IT OPERATES ON
-------------------
A plot SVG carries two things: a <metadata> block holding the recording (the
JSON list of strokes/points that the plotter actually replays) and cosmetic
<path>/<circle> elements so the file looks right in a viewer. **The recording is
the source of truth** — re-plotting reads only the metadata (see preview.py's
uploadSVG). So every transform here rewrites the recording and then regenerates
matching visuals from it, keeping the two in sync.

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
    whole (never split); its thickness is its thickest segment by default (or the
    mean, via the toggle).

Run:  python svg_transform.py [optional_input.svg]
Self-test (no GUI):  python svg_transform.py --selftest path/to/some.svg
"""

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from statistics import mean

# ── constants (mirrored from the pipeline so this runs with no extra deps) ─────
OSC_PRESSURE_MAX      = 4.166666507720947
STROKE_THINNING       = 0.5          # matches preview.py / log_to_svg
DEFAULT_DRAWING_WIDTH = 1.5
_EPS                  = 1e-9

_METADATA_RE = re.compile(r"<metadata>(.*?)</metadata>", re.S)
_SVG_SIZE_RE = re.compile(r'<svg[^>]*\bwidth="([\d.]+)"[^>]*\bheight="([\d.]+)"')


# ──────────────────────────────────────────────────────────────────────────────
# CORE (pure functions — no GUI, importable and testable)
# ──────────────────────────────────────────────────────────────────────────────
def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1.0 else v


def width_for(size: float, pressure_norm: float) -> float:
    """Rendered/plotted line width for a point. Identical to preview.widthFor."""
    p = _clamp01(pressure_norm)
    return max(0.1, size * (1 - STROKE_THINNING + 2 * STROKE_THINNING * p))


def load_svg(path) -> tuple[dict, float, float]:
    """
    Read a plot SVG. Returns (recording, viewport_w, viewport_h).
    Raises ValueError if the file carries no draw2axi recording.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    m = _METADATA_RE.search(text)
    if not m or not m.group(1).strip():
        raise ValueError("no <metadata> recording found — not a draw2axi plot SVG")
    meta = (m.group(1)
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
    rec = json.loads(meta)
    if rec.get("format") != "draw2axi-recording":
        raise ValueError(f"unexpected recording format: {rec.get('format')!r}")

    sm = _SVG_SIZE_RE.search(text)
    if sm:
        vw, vh = float(sm.group(1)), float(sm.group(2))
    else:
        # Fall back to the largest per-stroke canvas.
        cws = [s.get("canvasWidth")  or 0 for s in rec.get("strokes", [])]
        chs = [s.get("canvasHeight") or 0 for s in rec.get("strokes", [])]
        vw, vh = (max(cws) or 1.0), (max(chs) or 1.0)
    return rec, vw, vh


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


def build_svg(rec: dict, viewport_w: float, viewport_h: float) -> str:
    """
    Serialize a recording back to a plot SVG, in the same shape the pipeline
    emits: embedded metadata + white centerline segments on black, each segment
    width taken from the mean pressure of its endpoints (per-stroke drawingWidth).
    """
    parts = []
    for s in rec.get("strokes", []):
        size = s.get("drawingWidth", DEFAULT_DRAWING_WIDTH)
        pts = s.get("points") or []
        if not pts:
            continue
        if len(pts) == 1:
            x, y, praw = pts[0][1], pts[0][2], pts[0][3]
            r = width_for(size, praw / OSC_PRESSURE_MAX) / 2
            parts.append(f'  <circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.3f}" fill="#fff"/>')
            continue
        parts.append('  <g stroke="#fff" fill="none" stroke-linecap="round">')
        for a, b in zip(pts, pts[1:]):
            pa = a[3] / OSC_PRESSURE_MAX
            pb = b[3] / OSC_PRESSURE_MAX
            w = width_for(size, (pa + pb) / 2.0)
            parts.append(f'    <path d="M{a[1]:.2f},{a[2]:.2f}L{b[1]:.2f},{b[2]:.2f}"'
                         f' stroke-width="{w:.3f}"/>')
        parts.append('  </g>')

    meta = (json.dumps(rec, separators=(",", ":"))
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{viewport_w:.0f}" height="{viewport_h:.0f}">',
        f'  <metadata>{meta}</metadata>',
        f'  <rect width="{viewport_w:.0f}" height="{viewport_h:.0f}" fill="#000"/>',
        *parts,
        '</svg>',
    ])


# ──────────────────────────────────────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────────────────────────────────────
def run_gui(initial_path: str | None = None) -> int:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    PREVIEW_MAX = 440          # longest edge of each preview canvas, px
    BG = "#111"

    root = tk.Tk()
    root.title("draw2axi — SVG transform")
    root.configure(bg=BG)

    state = {"rec": None, "vw": 1.0, "vh": 1.0, "path": None,
             "result_rec": None, "box": (PREVIEW_MAX, PREVIEW_MAX)}

    # ── controls (left) ───────────────────────────────────────────────────────
    ctrl = tk.Frame(root, bg=BG, padx=12, pady=12)
    ctrl.grid(row=0, column=0, sticky="ns")

    file_lbl = tk.Label(ctrl, text="No file loaded", bg=BG, fg="#ddd",
                        wraplength=220, justify="left", anchor="w")
    file_lbl.pack(fill="x", pady=(0, 8))

    tk.Button(ctrl, text="Open SVG…", command=lambda: do_open()).pack(fill="x")
    tk.Button(ctrl, text="Save transformed SVG…",
              command=lambda: do_save()).pack(fill="x", pady=(4, 12))

    flip_h_var = tk.BooleanVar(value=False)
    flip_v_var = tk.BooleanVar(value=False)
    tk.Checkbutton(ctrl, text="Flip horizontal  (mirror ↔)", variable=flip_h_var,
                   bg=BG, fg="#ddd", selectcolor=BG, activebackground=BG,
                   activeforeground="#fff", anchor="w").pack(fill="x")
    tk.Checkbutton(ctrl, text="Flip vertical  (mirror ↕)", variable=flip_v_var,
                   bg=BG, fg="#ddd", selectcolor=BG, activebackground=BG,
                   activeforeground="#fff", anchor="w").pack(fill="x", pady=(0, 12))

    tk.Label(ctrl, text="Filter by min width", bg=BG, fg="#fff",
             anchor="w", font=("TkDefaultFont", 10, "bold")).pack(fill="x")
    tk.Label(ctrl, text="Keep strokes at least this thick, relative to the\n"
                        "thickest stroke. 0% keeps all, 100% keeps only the\n"
                        "thickest.", bg=BG, fg="#999", justify="left",
             anchor="w").pack(fill="x")

    pct_var = tk.DoubleVar(value=0.0)
    pct_lbl = tk.Label(ctrl, text="0%", bg=BG, fg="#8cf", anchor="w")
    pct_lbl.pack(fill="x")
    tk.Scale(ctrl, from_=0, to=100, orient="horizontal", variable=pct_var,
             showvalue=False, bg=BG, fg="#ddd", troughcolor="#333",
             highlightthickness=0,
             command=lambda v: pct_lbl.config(text=f"{float(v):.0f}%")
             ).pack(fill="x")

    metric_var = tk.StringVar(value="max")
    mrow = tk.Frame(ctrl, bg=BG)
    mrow.pack(fill="x", pady=(4, 12))
    tk.Label(mrow, text="Measure thickness by:", bg=BG, fg="#999").pack(side="left")
    ttk.Combobox(mrow, textvariable=metric_var, values=["max", "mean"],
                 width=6, state="readonly").pack(side="left", padx=6)

    tk.Button(ctrl, text="Preview", command=lambda: do_preview(),
              font=("TkDefaultFont", 10, "bold")).pack(fill="x")
    tk.Button(ctrl, text="Reset transforms",
              command=lambda: do_reset()).pack(fill="x", pady=(4, 12))

    stats_lbl = tk.Label(ctrl, text="", bg=BG, fg="#bbb", justify="left",
                         anchor="w", wraplength=220)
    stats_lbl.pack(fill="x")

    # ── previews (right) ──────────────────────────────────────────────────────
    view = tk.Frame(root, bg=BG, padx=8, pady=12)
    view.grid(row=0, column=1, sticky="nsew")
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(0, weight=1)

    tk.Label(view, text="Original", bg=BG, fg="#fff").grid(row=0, column=0)
    tk.Label(view, text="Result",   bg=BG, fg="#fff").grid(row=0, column=1)
    cv_orig = tk.Canvas(view, width=PREVIEW_MAX, height=PREVIEW_MAX,
                        bg="#000", highlightthickness=1, highlightbackground="#333")
    cv_res  = tk.Canvas(view, width=PREVIEW_MAX, height=PREVIEW_MAX,
                        bg="#000", highlightthickness=1, highlightbackground="#333")
    cv_orig.grid(row=1, column=0, padx=6)
    cv_res.grid(row=1, column=1, padx=6)

    # ── rendering ─────────────────────────────────────────────────────────────
    def _box_for(vw: float, vh: float) -> tuple[float, float]:
        if vw <= 0 or vh <= 0:
            return PREVIEW_MAX, PREVIEW_MAX
        if vw >= vh:
            return PREVIEW_MAX, PREVIEW_MAX * vh / vw
        return PREVIEW_MAX * vw / vh, PREVIEW_MAX

    def _render(canvas, rec):
        canvas.delete("all")
        bw, bh = state["box"]
        vw, vh = state["vw"], state["vh"]
        canvas.config(width=bw, height=bh)
        canvas.create_rectangle(0, 0, bw, bh, fill="#000", outline="")
        if not rec:
            return
        for s in rec.get("strokes", []):
            cw, ch = _stroke_canvas(s, vw, vh)
            size = s.get("drawingWidth", DEFAULT_DRAWING_WIDTH)
            pts = s.get("points") or []
            wscale = bw / cw if cw else 1.0
            if len(pts) == 1:
                x = (pts[0][1] / cw) * bw
                y = (pts[0][2] / ch) * bh
                r = max(0.5, width_for(size, pts[0][3] / OSC_PRESSURE_MAX) * wscale / 2)
                canvas.create_oval(x - r, y - r, x + r, y + r, fill="#fff", outline="")
                continue
            for a, b in zip(pts, pts[1:]):
                pa = a[3] / OSC_PRESSURE_MAX
                pb = b[3] / OSC_PRESSURE_MAX
                w = max(1.0, width_for(size, (pa + pb) / 2.0) * wscale)
                canvas.create_line((a[1] / cw) * bw, (a[2] / ch) * bh,
                                    (b[1] / cw) * bw, (b[2] / ch) * bh,
                                    fill="#fff", width=w, capstyle="round")

    # ── actions ───────────────────────────────────────────────────────────────
    def do_open(path=None):
        path = path or filedialog.askopenfilename(
            title="Open plot SVG",
            filetypes=[("SVG", "*.svg"), ("All files", "*.*")])
        if not path:
            return
        try:
            rec, vw, vh = load_svg(path)
        except Exception as e:      # noqa: BLE001 — surface any load failure to the user
            messagebox.showerror("Could not load SVG", str(e))
            return
        state.update(rec=rec, vw=vw, vh=vh, path=path,
                     box=_box_for(vw, vh))
        n = sum(len(s.get("points") or []) for s in rec.get("strokes", []))
        file_lbl.config(text=f"{Path(path).name}\n{len(rec.get('strokes', []))} "
                             f"strokes · {n} pts · canvas {vw:.0f}x{vh:.0f}")
        _render(cv_orig, rec)
        do_preview()

    def _current_transform():
        return transform(state["rec"], state["vw"], state["vh"],
                         flip_h=flip_h_var.get(), flip_v=flip_v_var.get(),
                         percent=float(pct_var.get()), metric=metric_var.get())

    def do_preview():
        if not state["rec"]:
            return
        rec, stats = _current_transform()
        state["result_rec"] = rec
        _render(cv_res, rec)
        flips = ", ".join([f for f, on in
                           (("flip-H", stats["flip_h"]), ("flip-V", stats["flip_v"])) if on]) or "none"
        stats_lbl.config(
            text=(f"Flips: {flips}\n"
                  f"Kept {stats['kept_strokes']} / {stats['total_strokes']} strokes "
                  f"(dropped {stats['dropped_strokes']})\n"
                  f"Threshold: {stats['threshold']:.3f} of max {stats['max_width']:.3f} "
                  f"({stats['percent']:.0f}%, by {stats['metric']})"))

    def do_reset():
        flip_h_var.set(False)
        flip_v_var.set(False)
        pct_var.set(0.0)
        pct_lbl.config(text="0%")
        metric_var.set("max")
        do_preview()

    def do_save():
        if not state["rec"]:
            messagebox.showinfo("Nothing to save", "Open an SVG first.")
            return
        rec, _ = _current_transform()      # save exactly what the controls describe
        src = Path(state["path"])
        out = filedialog.asksaveasfilename(
            title="Save transformed SVG", defaultextension=".svg",
            initialfile=f"{src.stem}_transformed.svg", initialdir=str(src.parent),
            filetypes=[("SVG", "*.svg")])
        if not out:
            return
        try:
            Path(out).write_text(build_svg(rec, state["vw"], state["vh"]),
                                 encoding="utf-8")
        except Exception as e:      # noqa: BLE001
            messagebox.showerror("Could not save", str(e))
            return
        messagebox.showinfo("Saved", f"Wrote {Path(out).name}\n"
                                     f"{len(rec.get('strokes', []))} strokes.\n"
                                     "Load it with the preview's 'plot svg' button.")

    if initial_path:
        do_open(initial_path)

    root.mainloop()
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# SELF-TEST (headless — exercises the core without opening a window)
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
        description="Transform a plot SVG (flip / filter by width) in a GUI.")
    ap.add_argument("svg", nargs="?", help="SVG to open on launch")
    ap.add_argument("--selftest", action="store_true",
                    help="run headless self-tests on the given SVG and exit")
    args = ap.parse_args()

    if args.selftest:
        if not args.svg:
            print("[error] --selftest needs an SVG path")
            return 2
        return _selftest(args.svg)

    return run_gui(args.svg)


if __name__ == "__main__":
    sys.exit(main())
