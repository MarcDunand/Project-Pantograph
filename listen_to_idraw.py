"""
listen_to_idraw.py

Full pipeline: Apple Pencil → iDraw OSC → Python → AxiDraw

Stages:
  1. Receive raw OSC scalar messages from iDraw OSC
  2. Reconstruct drawing points and infer stroke boundaries
  3. Map canvas coordinates → paper coordinates (with aspect-ratio letterboxing)
  4. Simplify each stroke (reduce redundant points before sending to plotter)
  5. Buffer simplified points and feed them to AxiDraw one move at a time

Usage:
  python listen_to_idraw.py

In iDraw OSC (iPad):
  Set IP   -> your computer's local Wi-Fi IP  (e.g. 192.168.1.57)
  Set Port -> 8800

Dependencies:
  pip install python-osc websockets rdp pyaxidraw
  (pyaxidraw install instructions: https://axidraw.com/doc/py_api/)
"""

import argparse
import collections
import math
import threading
import time

import numpy as np
from rdp import rdp as _rdp_fn

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

import postprocess
import preview

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these values to match your setup
# ──────────────────────────────────────────────────────────────────────────────

OSC_PORT = 8800

# Maximum pressure value reported by iDraw OSC (Apple Pencil at full press).
# Finger input always reports 1.0. Divide raw values by this to get a 0–1 scale.
OSC_PRESSURE_MAX = 4.166666507720947

# Physical plotting area on paper, in inches.
# The AxiDraw will never move outside this rectangle.
# Change these when you switch paper sizes or want a smaller plot area.
PAPER_WIDTH_IN  = 8.5
PAPER_HEIGHT_IN = 11

# How long a gap between received points (in seconds) counts as a pen lift.
# 0.15s (150ms) is a reasonable default — short enough to feel responsive,
# long enough to not trigger falsely during normal drawing pauses.
PEN_UP_TIMEOUT_SEC = 0.15

# Minimum distance between consecutive points sent to the plotter, in paper inches.
# Points that are closer together than this are skipped to avoid flooding the
# AxiDraw with tiny moves that cause stuttering.
# 0.01" (≈ 0.25mm) is a good starting point — increase if the plotter queue grows
# too fast during fast drawing, decrease for more accuracy on slow careful lines.
STREAM_MIN_DIST_IN = 0.01

# Raw pressure value that iDraw OSC sends as a placeholder/default (normalises to ≈ 0.24).
# Isolated occurrences are replaced by linear interpolation between neighbours;
# a stroke where every point has this value is discarded entirely.
SPURIOUS_RAW_PRESSURE: float = 1.0

# ──────────────────────────────────────────────────────────────────────────────
# POST-PROCESSING EFFECTS — flip one on to transform what gets plotted
# ──────────────────────────────────────────────────────────────────────────────
#
# Each switch enables one effect from postprocess.py, which rewrites the plot
# command stream on its way to the plotter. The preview always shows the
# drawing as it was actually drawn — only the pen is affected.
#
# Effects are not designed against each other; turn on one at a time.

EFFECT_ZIGZAG           = True   # pen moves fully in x, then fully in y — lines come out as zigzag steps
EFFECT_PRESSURE_HATCH   = False   # hard-pressed parts of a stroke grow perpendicular hatch marks
EFFECT_STROKE_CONNECTOR = False    # after each stroke, draw a line from its midpoint to a nearby stroke's
EFFECT_MIRROR           = False    # short strokes sometimes get a vertically mirrored copy over themselves

_EFFECT_SWITCHES = {
    "zigzag":           EFFECT_ZIGZAG,
    "pressure_hatch":   EFFECT_PRESSURE_HATCH,
    "stroke_connector": EFFECT_STROKE_CONNECTOR,
    "mirror":           EFFECT_MIRROR,
}

# ──────────────────────────────────────────────────────────────────────────────
# COORDINATE MAPPING
# ──────────────────────────────────────────────────────────────────────────────
#
# The canvas from iDraw OSC has its own pixel dimensions (e.g. 440 × 956).
# We need to map those to real paper coordinates in inches.
#
# We preserve the aspect ratio of the canvas ("letterbox" mode):
#   - Scale the canvas to fit inside the paper area without stretching.
#   - Center the scaled canvas on the paper.
#   - Any leftover paper area becomes margin (dead zone the plotter won't use).
#
# Example with a 440×956 canvas on 9×12" paper:
#   canvas aspect = 440/956 = 0.46  (portrait, narrower than paper)
#   paper aspect  = 9/12    = 0.75
#   canvas is narrower → scale to fit height → drawable area = 5.52 × 12"
#   horizontal margin = (9 - 5.52) / 2 = 1.74" on each side
#
# The computed offset (margin_x, margin_y) and scale are recalculated each
# time canvas dimensions arrive from iDraw OSC, so changing tablets just works.

def compute_mapping(canvas_w: float, canvas_h: float) -> dict:
    """
    Returns the offset and scale needed to map canvas pixels → paper inches,
    preserving aspect ratio and centering the result on the paper.
    """
    canvas_aspect = canvas_w / canvas_h
    paper_aspect  = PAPER_WIDTH_IN / PAPER_HEIGHT_IN

    if canvas_aspect < paper_aspect:
        # Canvas is relatively taller than paper → fit to height, letterbox sides
        draw_h = PAPER_HEIGHT_IN
        draw_w = PAPER_HEIGHT_IN * canvas_aspect
    else:
        # Canvas is relatively wider than paper → fit to width, letterbox top/bottom
        draw_w = PAPER_WIDTH_IN
        draw_h = PAPER_WIDTH_IN / canvas_aspect

    margin_x = (PAPER_WIDTH_IN  - draw_w) / 2.0
    margin_y = (PAPER_HEIGHT_IN - draw_h) / 2.0

    return {
        "draw_w":   draw_w,
        "draw_h":   draw_h,
        "margin_x": margin_x,
        "margin_y": margin_y,
        "scale_x":  draw_w / canvas_w,
        "scale_y":  draw_h / canvas_h,
    }


def canvas_to_paper(x: float, y: float, mapping: dict) -> tuple[float, float]:
    """Convert a single canvas point to paper inches using a precomputed mapping."""
    px = mapping["margin_x"] + x * mapping["scale_x"]
    py = mapping["margin_y"] + y * mapping["scale_y"]
    return px, py


def canvas_to_physical(x: float, y: float, mapping: dict) -> tuple[float, float]:
    """
    Convert a canvas point all the way to final AxiDraw physical coordinates:
    paper mapping, 90° landscape rotation, then flip H/V.

    Flip is applied here — the last step of this function, before the result
    is used by anything else — so every downstream setting (tilt, etc.) always
    operates on the same physical axis regardless of how flip H/V are set.
    Returned px runs 0→PAPER_HEIGHT_IN (physical long axis), py runs
    0→PAPER_WIDTH_IN (physical short axis).
    """
    px, py = canvas_to_paper(x, y, mapping)
    px, py = py, PAPER_WIDTH_IN - px
    if preview.flip_x:
        px = PAPER_HEIGHT_IN - px
    if preview.flip_y:
        py = PAPER_WIDTH_IN - py
    return px, py


def physical_to_canvas(px: float, py: float, mapping: dict) -> tuple[float, float]:
    """
    Inverse of canvas_to_physical: final AxiDraw physical coordinates back to the
    canvas pixels the preview draws in. Used to show the optimized (post-filter)
    and effect command streams in the preview, on top of the raw OSC points.
    Because it undoes the same flip + rotation, a point round-trips exactly, so
    the optimized layer lands on the raw one and only *thinning* shows as a gap.
    """
    if preview.flip_x:
        px = PAPER_HEIGHT_IN - px
    if preview.flip_y:
        py = PAPER_WIDTH_IN - py
    px_paper = PAPER_WIDTH_IN - py
    py_paper = px
    x = (px_paper - mapping["margin_x"]) / mapping["scale_x"]
    y = (py_paper - mapping["margin_y"]) / mapping["scale_y"]
    return x, y


# ──────────────────────────────────────────────────────────────────────────────
# REAL-TIME PLOT COMMAND QUEUE
# ──────────────────────────────────────────────────────────────────────────────
#
# Commands are streamed to the plotter as each point arrives from OSC rather
# than buffering a full stroke. This lets the AxiDraw start moving mid-stroke.
#
# A deque (not a Queue) is used so the optimizer thread can inspect and rewrite
# pending lineto sequences via RDP when the plotter falls behind.
#
# Each item: (enqueue_time, kind, *args)
#   (t, "moveto",    x, y)  — pen-up travel to stroke start
#   (t, "pendown")          — lower pen
#   (t, "lineto",    x, y)  — pen-down move
#   (t, "dot_dwell")        — pause briefly (for single-tap dots)
#   (t, "penup")            — lift pen
#   (t, "home")             — travel to (0, 0) with pen up

_plot_deque: collections.deque = collections.deque()
_plot_lock  = threading.Lock()

# Serializes pen-state mutations (_pen_is_down, _last_plot_pt, _pending_024)
# between the point-emitting thread and the pen-up watchdog thread. Reentrant
# because _emit_point holds it while calling _maybe_pen_up.
_pen_lock   = threading.RLock()

_ad               = None   # live AxiDraw handle; used by shutdown cleanup
_last_plot_pt:    tuple | None = None
_stroke_had_moves: bool        = False

_pending_024:               list  = []     # stroke commands buffered while pressure is spurious
_stroke_has_good_pressure:  bool  = False  # True once a non-spurious point is seen this stroke
_stroke_last_good_pressure: float = 0.0   # last non-spurious normalised pressure


# ──────────────────────────────────────────────────────────────────────────────
# EFFECT CHAIN  (see postprocess.py)
# ──────────────────────────────────────────────────────────────────────────────
#
# Every drawing command goes through _enqueue(), which runs it through the
# enabled effects and appends whatever they produce. Effects run here, at
# enqueue time rather than in the plotter loop, so that the adaptive RDP
# optimizer downstream also thins whatever geometry they add, and queue lag
# stays measured from the real command backlog.

# Live, browser-tunable copy of each effect's knobs: name → {attr: value}.
# Seeded from the class defaults so a chain built before any browser connects
# already matches what the effects panel will show.
_effect_params = {
    spec["name"]: {p["attr"]: p["default"] for p in spec["params"]}
    for spec in postprocess.effect_specs()
}

_effect_chain = postprocess.build_chain(_EFFECT_SWITCHES, _effect_params)
_effect_ctx   = postprocess.Ctx(x_max=PAPER_HEIGHT_IN, y_max=PAPER_WIDTH_IN)

# When True, the plotter lays down only what the effect chain *adds* — the base
# centerline is dropped. Lets a finished drawing be re-run (via replay) with just
# its postprocessing marks, on top of a base layer already on the paper.
_effects_only: bool = False


def _rebuild_effect_chain() -> None:
    """
    Rebuild the live effect chain from the current switches and knob values.
    Held under _plot_lock so an in-flight _enqueue never sees a half-swapped
    chain. Rebuilding resets per-effect state (anchors, accumulated midpoints),
    which is fine between strokes and an acceptable blip if tuned mid-stroke.
    """
    global _effect_chain
    with _plot_lock:
        _effect_chain = postprocess.build_chain(_EFFECT_SWITCHES, _effect_params)


def _advance_effect_ctx(cmd: tuple) -> None:
    """
    Bring the context up to date *for* cmd, before the effects see it, so that
    counters describe the command currently being handled.
    """
    kind = cmd[1]
    if kind == "moveto":
        _effect_ctx.stroke_index += 1
        _effect_ctx.point_index   = 0
    elif kind == "pendown":
        _effect_ctx.pen_is_down = True
    elif kind == "lineto":
        _effect_ctx.point_index += 1
    elif kind == "penup":
        _effect_ctx.pen_is_down = False


def _enqueue(cmd: tuple) -> None:
    """
    Push one drawing command onto the plot deque, via the effect chain.

    Control commands (home, pen_test_*) are machine operations rather than part
    of the drawing, so they use _enqueue_raw and skip effects entirely.
    """
    with _plot_lock:
        _advance_effect_ctx(cmd)
        if not _effect_chain:
            out = [cmd]
        else:
            out = postprocess.apply_chain(_effect_chain, cmd, _effect_ctx)
        # Everything the effects added on top of the base command (out minus the
        # passed-through base) — the "effect" layer, and all that gets plotted in
        # effects-only mode.
        added = [c for c in out if c is not cmd and c != cmd]
        _plot_deque.extend(added if _effects_only else out)
        # Only now does cmd's position become "the previous one", so that an
        # effect handling cmd sees the point it is moving *from* in last_xy.
        xy = postprocess.xy_of(cmd)
        if xy is not None:
            _effect_ctx.last_xy = xy

    # Mirror the two derived layers to the preview: the optimized centerline
    # (this base command, post distance-filter) in white, and the effect
    # additions in blue. Independent of what is plotted, so the preview always
    # shows both even in effects-only mode. Broadcast is thread-safe, non-blocking.
    _broadcast_plot_layers(cmd, added)


def _broadcast_plot_layers(base: tuple, added: list) -> None:
    """Send the optimized (base) and effect-added commands to the preview."""
    mapping = state["_mapping"]
    if not mapping:
        return
    preview.broadcast(_layer_msg("optimized", base, mapping))
    for c in added:
        preview.broadcast(_layer_msg("effect", c, mapping))


def _layer_msg(layer: str, cmd: tuple, mapping: dict) -> dict:
    """Build a preview 'layer' message for one plot command, in canvas pixels."""
    kind = postprocess.kind_of(cmd)
    msg = {"type": "layer", "layer": layer, "kind": kind}
    xy = postprocess.xy_of(cmd)
    if xy is not None:
        cx, cy = physical_to_canvas(xy[0], xy[1], mapping)
        p = postprocess.pressure_of(cmd)
        msg.update({
            "x": cx,
            "y": cy,
            "pressure": p if p is not None else 0.0,
            "drawingWidth": state["drawingWidth"],
            "canvasWidth":  state["canvasWidth"],
        })
    return msg


def _enqueue_raw(cmd: tuple) -> None:
    """Push a command straight to the plot deque, bypassing effects."""
    with _plot_lock:
        _plot_deque.append(cmd)

# ──────────────────────────────────────────────────────────────────────────────
# ADAPTIVE OPTIMIZATION STATE
# ──────────────────────────────────────────────────────────────────────────────
#
# _opt_enabled       — master switch; disables all adaptive optimization
# _opt_scale         — 0..1, the optimization level reached at _lag_threshold_sec
# _lag_threshold_sec — lag (s) at which optimization reaches _opt_scale
# _limit_lag         — False: cap at _opt_scale past threshold (predictable ceiling)
#                      True:  continue climbing at 2× rate past threshold (catch-up)
# _current_lag_sec   — rolling lag estimate (written by both plotter + optimizer)

_opt_enabled:       bool  = True
_opt_scale:         float = 0.5
_lag_threshold_sec: float = 3.0
_limit_lag:         bool  = True
_current_lag_sec:   float = 0.0
_min_dist_in:       float = STREAM_MIN_DIST_IN

# Pen position settings (0-100, direct AxiDraw servo %; lower = pen further down)
_variable_pressure:      bool  = False
_pen_pos_up:             int   = 60
_pen_down_min:           int   = 40   # position used when variable pressure is off, or at ~0 pressure
_pen_down_max:           int   = 20   # position used at full (1.0) pressure when variable pressure is on
_pressure_update_rate:   int   = 100  # % of lineto points that trigger a mid-stroke pen position update

# Canvas tilt compensation (degrees).  x_tilt corrects tilt along the physical
# short axis (py, PAPER_WIDTH_IN) and y_tilt along the physical long axis
# (px, PAPER_HEIGHT_IN) — matching how X/Y read on the machine itself, not the
# internal landscape px/py naming. Positive x_tilt means small py is physically
# higher — the pen needs less travel there, so pen_pos_down is nudged upward.
# The servo-unit correction is computed from the physical tilt angle, the
# distance from the paper centre, and TILT_SERVO_PER_INCH.
_x_tilt_deg: float = 0.0
_y_tilt_deg: float = 0.0
TILT_SERVO_PER_INCH = 100.0   # approx: 1" of height change → 100 servo-unit offset


def _tilt_pen_offset(px: float, py: float) -> float:
    """
    Servo-unit offset added to pen_pos_down at paper position (px, py) inches.
    Positive = surface is closer to pen here, so pen_pos_down needs to increase.
    px runs 0→PAPER_HEIGHT_IN (physical long axis), py runs 0→PAPER_WIDTH_IN
    (physical short axis). x_tilt maps to the short axis (py) and y_tilt to
    the long axis (px) to match how X/Y read on the physical machine.
    """
    if _x_tilt_deg == 0.0 and _y_tilt_deg == 0.0:
        return 0.0
    offset_x = math.tan(math.radians(_x_tilt_deg)) * (PAPER_WIDTH_IN  / 2.0 - py)
    offset_y = math.tan(math.radians(_y_tilt_deg)) * (PAPER_HEIGHT_IN / 2.0 - px)
    return (offset_x + offset_y) * TILT_SERVO_PER_INCH


def _compute_effective_scale(lag: float) -> float:
    """
    Returns the effective optimization scale (0.0 → _opt_scale or higher) for
    the given lag value. Used identically by the distance filter and RDP optimizer.

    Limit lag OFF:  linear ramp from 0 → _opt_scale as lag goes 0 → threshold.
                    Hard caps at _opt_scale beyond the threshold.
    Limit lag ON:   same ramp to _opt_scale at threshold, then continues climbing
                    at 2× the rate with no ceiling (actively chasing the target).

    Examples with _opt_scale=0.30, threshold=3s:
      lag=0s  → 0.00   lag=1.5s → 0.15   lag=3s → 0.30
      lag=4s  → 0.47 (ON) / 0.30 (OFF)
      lag=6s  → 0.90 (ON) / 0.30 (OFF)
    """
    if not _opt_enabled or lag < 0.01:
        return 0.0
    threshold = max(0.1, _lag_threshold_sec)
    if lag <= threshold:
        return _opt_scale * (lag / threshold)
    elif _limit_lag:
        # Past threshold: continue at 2× rate — 2·(lag/threshold) - 1 gives the
        # piecewise-linear continuation that starts at _opt_scale at threshold.
        return _opt_scale * (2.0 * lag / threshold - 1.0)
    else:
        return _opt_scale


# ──────────────────────────────────────────────────────────────────────────────
# PLOTTER THREAD  +  ADAPTIVE OPTIMIZER
# ──────────────────────────────────────────────────────────────────────────────

def _plotter_thread():
    """
    Polls the plot deque and executes commands on the AxiDraw one at a time.
    Records command age at dequeue time to keep _current_lag_sec up to date.
    """
    global _ad, _current_lag_sec

    if not DRY_RUN:
        from pyaxidraw import axidraw
        ad = axidraw.AxiDraw()
        ad.interactive()
        ad.options.units = 0   # use inches
        ad.options.speed_pendown = 15
        ad.options.speed_penup = 25
        ad.options.accel = 50
        ad.options.pen_rate_lower = 70
        ad.options.pen_rate_raise = 70
        ad.options.pen_delay_down = -100
        ad.options.pen_delay_up = -100
        try:
            ad.connect()
            ad.options.pen_pos_up   = _pen_pos_up
            ad.options.pen_pos_down = _pen_down_min
            ad.update()          # push pen positions to EBB via servo_init
            ad.penup()           # ensure known pen state on startup
            print("[axidraw] connected")
            _ad = ad
        except Exception as e:
            print(f"[axidraw] ERROR: could not connect — {e}")
            print("[axidraw] falling back to dry-run output")
            ad = None
    else:
        ad = None
        print("[axidraw] DRY RUN mode — no USB connection")

    # Per-stroke state for mid-stroke pressure updates (local to this thread)
    _lineto_counter      = 0
    _last_applied_down   = None   # last pen_pos_down sent to EBB this stroke

    while True:
        cmd = None
        with _plot_lock:
            if _plot_deque:
                cmd = _plot_deque.popleft()

        if cmd is None:
            time.sleep(0.005)
            continue

        # cmd = (enqueue_time, kind, *args)
        _current_lag_sec = time.monotonic() - cmd[0]
        kind = cmd[1]

        if kind == "moveto":
            x, y = cmd[2], cmd[3]
            if not _show_raw_osc:
                print(f"[axidraw] travel → ({x:.3f}\", {y:.3f}\")")
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()
                ad.penup()
                ad.moveto(x, y)
            elif not _show_raw_osc:
                print(f"  penup")
                print(f"  moveto  ({x:.3f}\", {y:.3f}\")")

        elif kind == "pendown":
            pressure = cmd[2] if len(cmd) > 2 else 1.0
            px_cmd   = cmd[3] if len(cmd) > 3 else 0.0
            py_cmd   = cmd[4] if len(cmd) > 4 else 0.0
            if _variable_pressure:
                target = _pen_down_min + (_pen_down_max - _pen_down_min) * pressure
            else:
                target = float(_pen_down_min)
            target += _tilt_pen_offset(px_cmd, py_cmd)
            target_pos = max(0, min(100, round(target)))
            if ad:
                ad.options.pen_pos_down = target_pos
                ad.update()
                ad.pendown()
            elif not _show_raw_osc:
                print(f"  pendown  (pos={target_pos})")
            _lineto_counter    = 0
            _last_applied_down = target_pos

        elif kind == "lineto":
            x, y = cmd[2], cmd[3]
            pressure = cmd[4] if len(cmd) > 4 else 1.0

            if (_variable_pressure or _x_tilt_deg != 0.0 or _y_tilt_deg != 0.0) and _pressure_update_rate > 0:
                interval = max(1, round(100 / _pressure_update_rate))
                _lineto_counter += 1
                if _lineto_counter % interval == 0:
                    if _variable_pressure:
                        target = _pen_down_min + (_pen_down_max - _pen_down_min) * pressure
                    else:
                        target = float(_pen_down_min)
                    target += _tilt_pen_offset(x, y)
                    new_pos = max(0, min(100, round(target)))
                    if new_pos != _last_applied_down:
                        _last_applied_down = new_pos
                        if ad:
                            ad.options.pen_pos_down = new_pos
                            ad.update()
                        elif not _show_raw_osc:
                            print(f"  [pressure/tilt] pos={new_pos}")

            if ad:
                ad.lineto(x, y)
            elif not _show_raw_osc:
                print(f"  lineto  ({x:.3f}\", {y:.3f}\")")

        elif kind == "dot_dwell":
            if ad:
                time.sleep(0.1)
            elif not _show_raw_osc:
                print(f"  [dot — 100ms dwell]")

        elif kind == "penup":
            if not _show_raw_osc:
                print(f"[axidraw] pen up")
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()
                ad.penup()
            elif not _show_raw_osc:
                print(f"  penup")

        elif kind == "home":
            if not _show_raw_osc:
                print(f"[axidraw] homing → (0.000\", 0.000\")")
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()
                ad.penup()
                ad.moveto(0, 0)
            elif not _show_raw_osc:
                print(f"  penup")
                print(f"  moveto  (0.000\", 0.000\")")

        elif kind == "pen_test_up":
            if not _show_raw_osc:
                print(f"[axidraw] pen test → up (pos={_pen_pos_up})")
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()   # servo_init re-sends SC commands; moves pen if pos changed
                ad.penup()
            elif not _show_raw_osc:
                print(f"  penup (pos={_pen_pos_up})")

        elif kind == "pen_test_min":
            if not _show_raw_osc:
                print(f"[axidraw] pen test → min down (pos={_pen_down_min})")
            if ad:
                ad.options.pen_pos_down = _pen_down_min
                ad.update()
                ad.pendown()
            elif not _show_raw_osc:
                print(f"  pendown (pos={_pen_down_min})")

        elif kind == "pen_test_max":
            if not _show_raw_osc:
                print(f"[axidraw] pen test → max down (pos={_pen_down_max})")
            if ad:
                ad.options.pen_pos_down = _pen_down_max
                ad.update()
                ad.pendown()
            elif not _show_raw_osc:
                print(f"  pendown (pos={_pen_down_max})")


def _run_rdp_on_deque(epsilon: float) -> int:
    """
    Apply RDP simplification to all pending lineto runs in the deque.
    Must be called with _plot_lock held.
    Returns the number of points removed.
    """
    if len(_plot_deque) < 3:
        return 0

    items = list(_plot_deque)
    result = []
    removed = 0
    i = 0

    while i < len(items):
        cmd = items[i]
        if cmd[1] == "lineto":
            # Collect the full contiguous run of lineto commands
            run_start = i
            while i < len(items) and items[i][1] == "lineto":
                i += 1
            run = items[run_start:i]

            if len(run) >= 3:
                pts = np.array([[r[2], r[3]] for r in run])
                mask = _rdp_fn(pts, epsilon=epsilon, return_mask=True)
                surviving = [r for r, keep in zip(run, mask) if keep]
                removed += len(run) - len(surviving)
                result.extend(surviving)
            else:
                result.extend(run)
        else:
            result.append(cmd)
            i += 1

    if removed > 0:
        _plot_deque.clear()
        _plot_deque.extend(result)

    return removed


def _optimizer_thread():
    """
    Runs at 10 Hz. Measures queue lag from the oldest pending command's timestamp
    and applies adaptive RDP simplification when the plotter falls behind.

    Two-layer approach:
      1. Adaptive distance filter in _emit_point (upstream, cheap — blocks new pts)
      2. RDP on buffered lineto sequences here (downstream, powerful — reclaims
         already-queued points while the plotter is busy on earlier moves)

    Lag is always measured so the browser display and distance filter stay
    accurate even when optimization is disabled.

    Epsilon scales from STREAM_MIN_DIST_IN (no lag) to 15× base (full lag, scale=1).
    When limit-lag is off, aggressiveness scales over a 5 s reference horizon.
    """
    global _current_lag_sec

    while True:
        time.sleep(0.1)   # 10 Hz

        # Always update lag — needed for the browser display and the adaptive
        # distance filter in _emit_point, regardless of opt_enabled.
        with _plot_lock:
            lag = (time.monotonic() - _plot_deque[0][0]) if _plot_deque else 0.0
        _current_lag_sec = lag

        if lag < 0.01:
            continue

        eff = _compute_effective_scale(lag)
        epsilon = _min_dist_in * (1.0 + eff * 14.0)

        if epsilon <= _min_dist_in * 1.05:
            continue

        with _plot_lock:
            n = _run_rdp_on_deque(epsilon)

        if n > 0 and not _show_raw_osc:
            print(f"[opt] -{n} pts  lag={lag:.2f}s  ε={epsilon:.4f}\"")


def _lag_broadcast_thread():
    """Broadcasts current plotter lag to all browser clients every 500 ms."""
    while True:
        time.sleep(0.5)
        preview.broadcast({"type": "lag", "seconds": round(_current_lag_sec, 2)})


# ──────────────────────────────────────────────────────────────────────────────
# CLI FLAGS  (set by argparse in __main__ before the server starts)
# ──────────────────────────────────────────────────────────────────────────────

_show_raw_osc = False   # --raw-osc: print every OSC message verbatim
DRY_RUN       = False   # --dry-run: compute and print moves, skip USB


def _log_raw(address, *args):
    """Print a single OSC message in raw mode."""
    val = args[0] if len(args) == 1 else list(args)
    print(f"[osc]  {address:<20}  {val}")


# ──────────────────────────────────────────────────────────────────────────────
# OSC STATE
# ──────────────────────────────────────────────────────────────────────────────

state = {
    "x":            None,
    "y":            None,
    "pressure":     1.0,
    "r":            0.0,
    "g":            0.0,
    "b":            0.0,
    "a":            1.0,
    "tool":         "pen",
    "canvasWidth":  440.0,
    "canvasHeight": 956.0,
    "drawingWidth": 1.5,
    "eraserWidth":  0.0,
    "_last_point_time": None,
    "_pen_is_down":     False,
    "_mapping":         None,    # recomputed when canvas size arrives
}

DRAWABLE_TOOLS = {"pen", "pencil", "marker", "monoline", "crayon", "fountainPen", "waterColor"}


# ──────────────────────────────────────────────────────────────────────────────
# SVG REPLAY
# ──────────────────────────────────────────────────────────────────────────────
#
# An SVG downloaded from the preview carries the drawing's raw input points in a
# <metadata> block. Replaying one pushes those points back through _emit_point,
# exactly as if they had just arrived over OSC — so the paper mapping, flips,
# tilt, the effect chain and the optimizer all re-apply downstream. That is the
# point of replaying at the input level: the same drawing can be plotted again
# with different post-processors switched on.

# Idle gaps longer than this are shortened on replay, so a drawing with long
# pauses does not take its original wall-clock time to plot. Must stay above
# PEN_UP_TIMEOUT_SEC or between-stroke pen lifts would stop being inferred.
REPLAY_MAX_GAP_SEC = 0.30


def _replay_recording(rec: dict) -> None:
    """Feed a recorded drawing back through the live pipeline. Runs on its own thread."""
    global _effect_chain, _effect_ctx

    strokes = rec.get("strokes") or []
    n_pts   = sum(len(s.get("points") or []) for s in strokes)
    print(f"[replay] {len(strokes)} stroke(s), {n_pts} point(s) — starting")

    # Rebuild the effects so a replay never inherits state (or RNG position)
    # from whatever was drawn before it.
    with _plot_lock:
        _effect_chain = postprocess.build_chain(_EFFECT_SWITCHES, _effect_params)
        _effect_ctx   = postprocess.Ctx(x_max=PAPER_HEIGHT_IN, y_max=PAPER_WIDTH_IN)

    for s in strokes:
        pts = s.get("points") or []
        if not pts:
            continue

        # Recompute whenever the recording's canvas differs from what is loaded —
        # and whenever no mapping exists yet, or every point would be dropped by
        # _emit_point's mapping guard.
        cw, ch = s.get("canvasWidth"), s.get("canvasHeight")
        if cw and ch and (cw != state["canvasWidth"] or ch != state["canvasHeight"]
                          or not state["_mapping"]):
            state["canvasWidth"], state["canvasHeight"] = cw, ch
            _update_mapping()

        state["tool"]         = s.get("tool", "pen")
        state["drawingWidth"] = s.get("drawingWidth", 1.5)
        col = s.get("color") or {}
        state["r"] = col.get("r", 0.0)
        state["g"] = col.get("g", 0.0)
        state["b"] = col.get("b", 0.0)
        state["a"] = col.get("a", 1.0)

        prev_t = None
        for pt in pts:
            t_pt, x, y, p_raw = pt[0], pt[1], pt[2], pt[3]
            if prev_t is not None:
                time.sleep(min(max(0.0, t_pt - prev_t), REPLAY_MAX_GAP_SEC))
            prev_t = t_pt
            state["x"]        = x
            state["y"]        = y
            state["pressure"] = p_raw
            _emit_point()

        # End the stroke deliberately rather than waiting on the watchdog: replay
        # pacing is not the original pacing, so the quiet period that would have
        # triggered the lift may never occur.
        state["_last_point_time"] = time.time() - PEN_UP_TIMEOUT_SEC - 1.0
        _maybe_pen_up()

    print("[replay] done")


# ──────────────────────────────────────────────────────────────────────────────
# OSC HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def _maybe_pen_up():
    """Fire a pen-up event if the point stream has gone quiet."""
    global _last_plot_pt, _stroke_had_moves
    # Held under _pen_lock so this never nulls _last_plot_pt in the middle of an
    # _emit_point read-modify-write on another thread (the crash during replay).
    with _pen_lock:
        last = state["_last_point_time"]
        if last is None:
            return
        if state["_pen_is_down"] and (time.time() - last) > PEN_UP_TIMEOUT_SEC:
            state["_pen_is_down"] = False

            if _pending_024:
                if not _stroke_has_good_pressure:
                    # Every point in the stroke had spurious pressure — discard it entirely.
                    # moveto was already queued (plotter travels pen-up to that spot), but
                    # pendown was never sent, so no ink is deposited.
                    n = len(_pending_024)
                    print(
                        f"[pressure] ERROR: stroke discarded — all {n} point(s) had"
                        f" spurious pressure (raw=1.0, norm≈0.24)"
                    )
                    _pending_024.clear()
                    _last_plot_pt     = None
                    _stroke_had_moves = False
                    # The stroke is dropped from the plot, but it still happened on
                    # the tablet — the preview has been drawing it and must be told
                    # it ended, or the next stroke continues it: a phantom line on
                    # screen, and two strokes merged into one in the recording.
                    preview.broadcast({"type": "pen_up"})
                    return
                # Trailing spurious points — interpolate pressure down to 0
                _flush_pending_024(0.0)

            preview.broadcast({"type": "pen_up"})
            t = time.monotonic()
            if not _stroke_had_moves:
                _enqueue(postprocess.dot_dwell(t))
            _enqueue(postprocess.penup(t))
            _last_plot_pt = None
            _stroke_had_moves = False
            if not _show_raw_osc:
                print("[pen up — timeout]")


def _pen_watchdog_thread():
    """
    Polls for a quiet point stream and fires pen-up when the timeout elapses.
    This ensures the final stroke of a session is always finalized, even when
    the user stops drawing and never starts a new stroke to trigger _maybe_pen_up.
    """
    while True:
        time.sleep(PEN_UP_TIMEOUT_SEC / 2)
        _maybe_pen_up()


def _flush_pending_024(next_pressure: float) -> None:
    """
    Emit buffered spurious-pressure items with linearly interpolated pressures
    (from _stroke_last_good_pressure → next_pressure), then clear the buffer.
    Sets _stroke_had_moves for any lineto commands emitted.
    """
    global _stroke_had_moves
    n = len(_pending_024)
    if n == 0:
        return
    p_prev = _stroke_last_good_pressure
    p_next = next_pressure
    for i, item in enumerate(_pending_024):
        p_interp = p_prev + (p_next - p_prev) * (i + 1) / (n + 1)
        kind, t, px, py = item
        if kind == "pendown":
            _enqueue(postprocess.pendown(t, p_interp, px, py))
        else:
            _enqueue(postprocess.lineto(t, px, py, p_interp))
            _stroke_had_moves = True
    _pending_024.clear()


def _emit_point():
    """
    Fires on every /y message (the last field in each OSC burst).
    Streams plot commands to the deque in real time and broadcasts to the preview.

    Two optimisation layers fire here:
      1. Adaptive distance filter — skips points too close to the last enqueued
         point; threshold grows with lag when optimisation is enabled.
      2. RDP on the deque backlog — handled in _optimizer_thread.
    """
    global _last_plot_pt, _stroke_had_moves, _stroke_has_good_pressure, _stroke_last_good_pressure

    x = state["x"]
    y = state["y"]
    if x is None or y is None:
        return
    if state["tool"] not in DRAWABLE_TOOLS:
        return

    mapping = state["_mapping"]
    if not mapping:
        return

    now = time.time()
    t   = time.monotonic()

    # Whole pen-state read-modify-write runs under _pen_lock so the watchdog
    # thread cannot lift the pen (and null _last_plot_pt) partway through it.
    with _pen_lock:
        _maybe_pen_up()

        was_down = state["_pen_is_down"]
        state["_pen_is_down"]     = True
        state["_last_point_time"] = now

        # Paper mapping + landscape rotation + flip H/V, all in one step — see
        # canvas_to_physical(). px ∈ [0, PAPER_HEIGHT_IN], py ∈ [0, PAPER_WIDTH_IN].
        px, py = canvas_to_physical(x, y, mapping)

        # Normalize pressure to 0–1 (raw iDraw range is 0–OSC_PRESSURE_MAX)
        pressure_norm = min(1.0, max(0.0, state["pressure"] / OSC_PRESSURE_MAX))
        spurious = (state["pressure"] == SPURIOUS_RAW_PRESSURE)

        if not was_down:
            # First point of a new stroke — reset filter state, queue travel, hold pen down
            _stroke_had_moves = False
            _stroke_has_good_pressure = False
            _stroke_last_good_pressure = 0.0
            _pending_024.clear()
            _last_plot_pt = (px, py)
            _enqueue(postprocess.moveto(t, px, py))
            if spurious:
                _pending_024.append(("pendown", t, px, py))
            else:
                _stroke_has_good_pressure = True
                _stroke_last_good_pressure = pressure_norm
                _enqueue(postprocess.pendown(t, pressure_norm, px, py))
        else:
            # Continuation — apply adaptive distance filter before enqueuing
            lx, ly = _last_plot_pt
            eff = _compute_effective_scale(_current_lag_sec)
            effective_min_dist = _min_dist_in * (1.0 + eff * 4.0)

            if math.hypot(px - lx, py - ly) >= effective_min_dist:
                if spurious:
                    _pending_024.append(("lineto", t, px, py))
                    _last_plot_pt = (px, py)
                else:
                    if _pending_024:
                        _flush_pending_024(pressure_norm)
                    _stroke_has_good_pressure = True
                    _stroke_last_good_pressure = pressure_norm
                    _enqueue(postprocess.lineto(t, px, py, pressure_norm))
                    _last_plot_pt = (px, py)
                    _stroke_had_moves = True

    # Send to preview.
    # `t` and `pressureRaw` exist so the browser can record the drawing verbatim
    # for SVG replay: raw is what the spurious-pressure logic keys on, and the
    # normalised value cannot be inverted back to it once it clamps at 1.0.
    preview.broadcast({
        "type":         "point",
        "t":            now,
        "x":            x,
        "y":            y,
        "pressure":     pressure_norm,
        "pressureRaw":  state["pressure"],
        "r":            state["r"],
        "g":            state["g"],
        "b":            state["b"],
        "a":            state["a"],
        "tool":         state["tool"],
        "drawingWidth": state["drawingWidth"],
        "canvasWidth":  state["canvasWidth"],
        "canvasHeight": state["canvasHeight"],
    })

    if not _show_raw_osc:
        print(
            f"[point] ({x:.1f}, {y:.1f})  "
            f"p={pressure_norm:.2f}  "
            f"tool={state['tool']}"
        )


def _handle_x(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["x"] = args[0]

def _handle_pressure(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["pressure"] = args[0]

def _handle_r(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["r"] = args[0]

def _handle_g(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["g"] = args[0]

def _handle_b(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["b"] = args[0]

def _handle_a(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["a"] = args[0]

def _handle_drawing_width(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["drawingWidth"] = args[0]

def _handle_eraser_width(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["eraserWidth"] = args[0]

def _handle_aspect(address, *args):
    if _show_raw_osc: _log_raw(address, *args)

def _handle_y(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["y"] = args[0]
    _emit_point()   # /y is always the last field per burst

def _handle_canvas_width(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["canvasWidth"] = args[0]
    _update_mapping()

def _handle_canvas_height(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["canvasHeight"] = args[0]
    _update_mapping()

def _update_mapping():
    w = state["canvasWidth"]
    h = state["canvasHeight"]
    if w and h:
        state["_mapping"] = compute_mapping(w, h)
        m = state["_mapping"]
        if not _show_raw_osc:
            print(
                f"[mapping] canvas {w:.0f}×{h:.0f}px → "
                f"draw area {m['draw_w']:.2f}\"×{m['draw_h']:.2f}\" "
                f"on {PAPER_WIDTH_IN}\"×{PAPER_HEIGHT_IN}\" paper  "
                f"(margins: x={m['margin_x']:.2f}\" y={m['margin_y']:.2f}\")"
            )
        preview.broadcast({"type": "canvas_size", "width": w, "height": h})

def _handle_tool_flag(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    value     = args[0]
    tool_name = address.lstrip("/")
    state[tool_name] = value
    if value == 1.0:
        state["tool"] = tool_name
        preview.broadcast({"type": "tool_change", "tool": tool_name})
        if not _show_raw_osc:
            print(f"[tool] -> {tool_name}")

def _handle_unknown(address, *args):
    if _show_raw_osc:
        _log_raw(address, *args)
    else:
        print(f"[unknown] {address}: {args}")


# ──────────────────────────────────────────────────────────────────────────────
# DISPATCHER
# ──────────────────────────────────────────────────────────────────────────────

def _build_dispatcher() -> Dispatcher:
    d = Dispatcher()

    d.map("/x",        _handle_x)
    d.map("/y",        _handle_y)
    d.map("/pressure", _handle_pressure)
    d.map("/aspectX",  _handle_aspect)
    d.map("/aspectY",  _handle_aspect)
    d.map("/r",        _handle_r)
    d.map("/g",        _handle_g)
    d.map("/b",        _handle_b)
    d.map("/a",        _handle_a)
    d.map("/canvasWidth",   _handle_canvas_width)
    d.map("/canvasHeight",  _handle_canvas_height)
    d.map("/drawingWidth",  _handle_drawing_width)
    d.map("/eraserWidth",   _handle_eraser_width)

    for tool in ("pen", "pencil", "marker", "monoline", "crayon",
                 "fountainPen", "waterColor", "bitmapEraser", "vectorEraser"):
        d.map(f"/{tool}", _handle_tool_flag)

    d.set_default_handler(_handle_unknown)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="python listen_to_idraw.py",
        description="iDraw OSC → Python → AxiDraw live drawing pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python listen_to_idraw.py              # plot live\n"
            "  python listen_to_idraw.py --dry-run    # print moves, no USB\n"
            "  python listen_to_idraw.py --raw-osc    # show raw OSC stream"
        ),
    )
    parser.add_argument(
        "--raw-osc",
        action="store_true",
        help=(
            "Print every incoming OSC message verbatim (address + value) "
            "instead of the normal pipeline output. "
            "Useful for verifying what iDraw OSC is actually sending "
            "and discovering new message addresses."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute and print all plotter moves without connecting to the AxiDraw. "
            "Use this to verify coordinate mapping before putting pen to paper."
        ),
    )
    cli = parser.parse_args()
    _show_raw_osc = cli.raw_osc
    DRY_RUN       = cli.dry_run

    print("=" * 60)
    print("  iDraw OSC -> Python -> AxiDraw")
    print(f"  OSC port     : {OSC_PORT}")
    print(f"  Paper size   : {PAPER_WIDTH_IN}\" × {PAPER_HEIGHT_IN}\"")
    print(f"  Min pt dist  : {STREAM_MIN_DIST_IN}\"")
    print(f"  AxiDraw      : {'DRY RUN' if DRY_RUN else 'ENABLED'}")
    print(f"  Output mode  : {'RAW OSC' if _show_raw_osc else 'pipeline'}")
    _fx = [e.name for e in _effect_chain]
    print(f"  Effects      : {', '.join(_fx) if _fx else 'none'}")
    print("=" * 60)
    print()
    print("  In iDraw OSC (iPad):")
    print("    IP   -> your computer's local Wi-Fi IP")
    print(f"   Port -> {OSC_PORT}")
    print()

    # Compute initial mapping using default canvas size.
    # This will be recalculated as soon as real canvas dimensions arrive.
    state["_mapping"] = compute_mapping(state["canvasWidth"], state["canvasHeight"])

    # Start preview server (opens browser)
    preview.start(open_browser=True)

    # Register callback so browser control messages reach the plotter and optimizer
    def _handle_preview_message(msg):
        global _opt_enabled, _opt_scale, _lag_threshold_sec, _limit_lag, _min_dist_in, \
               _variable_pressure, _pen_pos_up, _pen_down_min, _pen_down_max, _pressure_update_rate, \
               _x_tilt_deg, _y_tilt_deg, _effects_only
        t = msg.get("type")
        if t == "home":
            _enqueue_raw((time.monotonic(), "home"))
            if not _show_raw_osc:
                print("[axidraw] home queued — will execute after current commands")
        elif t == "set_opt_enabled":
            _opt_enabled = bool(msg.get("enabled", True))
        elif t == "set_opt_scale":
            _opt_scale = float(msg.get("value", 0.5))
        elif t == "set_lag_threshold":
            _lag_threshold_sec = max(0.1, float(msg.get("value", 3.0)))
        elif t == "set_limit_lag":
            _limit_lag = bool(msg.get("enabled", True))
        elif t == "set_min_dist":
            _min_dist_in = max(0.001, float(msg.get("value", STREAM_MIN_DIST_IN)))
        elif t == "set_variable_pressure":
            _variable_pressure = bool(msg.get("enabled", False))
        elif t == "set_pen_up_pos":
            _pen_pos_up = max(0, min(100, int(round(float(msg.get("value", 60))))))
        elif t == "set_pen_down_min":
            _pen_down_min = max(0, min(100, int(round(float(msg.get("value", 40))))))
        elif t == "set_pen_down_max":
            _pen_down_max = max(0, min(100, int(round(float(msg.get("value", 20))))))
        elif t == "set_pressure_update_rate":
            _pressure_update_rate = max(0, min(100, int(round(float(msg.get("value", 100))))))
        elif t == "set_x_tilt":
            _x_tilt_deg = float(msg.get("value", 0.0))
        elif t == "set_y_tilt":
            _y_tilt_deg = float(msg.get("value", 0.0))
        elif t == "set_effect_enabled":
            name = msg.get("name")
            if name in _EFFECT_SWITCHES:
                _EFFECT_SWITCHES[name] = bool(msg.get("enabled", False))
                _rebuild_effect_chain()
                if not _show_raw_osc:
                    print(f"[effect] {name} {'on' if _EFFECT_SWITCHES[name] else 'off'}")
        elif t == "set_effect_param":
            name = msg.get("name")
            attr = msg.get("attr")
            if name in _effect_params and attr in _effect_params[name]:
                _effect_params[name][attr] = msg.get("value")
                _rebuild_effect_chain()
        elif t == "set_effects_only":
            _effects_only = bool(msg.get("enabled", False))
            if not _show_raw_osc:
                print(f"[effect] effects-only {'on' if _effects_only else 'off'}")
        elif t == "replay":
            rec = msg.get("recording") or {}
            threading.Thread(
                target=_replay_recording, args=(rec,), daemon=True
            ).start()
        elif t == "pen_test_up":
            _enqueue_raw((time.monotonic(), "pen_test_up"))
            if not _show_raw_osc:
                print("[axidraw] pen test up queued")
        elif t == "pen_test_min":
            _enqueue_raw((time.monotonic(), "pen_test_min"))
            if not _show_raw_osc:
                print("[axidraw] pen test down-min queued")
        elif t == "pen_test_max":
            _enqueue_raw((time.monotonic(), "pen_test_max"))
            if not _show_raw_osc:
                print("[axidraw] pen test down-max queued")

    preview.register_message_callback(_handle_preview_message)

    # Start plotter thread (separate from OSC so motors never block reception)
    threading.Thread(target=_plotter_thread,       daemon=True).start()

    # Watchdog: fires pen-up when the point stream goes quiet
    threading.Thread(target=_pen_watchdog_thread,  daemon=True).start()

    # Optimizer: adapts RDP aggressiveness based on queue lag
    threading.Thread(target=_optimizer_thread,     daemon=True).start()

    # Lag broadcaster: sends current lag to the browser every 500 ms
    threading.Thread(target=_lag_broadcast_thread, daemon=True).start()

    # Start OSC listener — blocks until Ctrl+C
    dispatcher = _build_dispatcher()
    server = ThreadingOSCUDPServer(("0.0.0.0", OSC_PORT), dispatcher)
    print(f"[osc] Listening on 0.0.0.0:{OSC_PORT} ...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[shutdown] lifting pen, disabling XY motors, and disconnecting...")
        if _ad:
            try:
                _ad.penup()
                _ad.disconnect()
            except Exception:
                pass
            # Re-connect briefly in res_home2 mode: raises pen and disengages
            # XY stepper motors so the carriage can be moved home manually.
            try:
                from pyaxidraw import axidraw as _axi_mod
                _disarm = _axi_mod.AxiDraw()
                _disarm.plot_setup()
                _disarm.options.mode = "res_home2"
                _disarm.plot_run()
                print("[axidraw] XY motors disengaged — move carriage home manually")
            except Exception as _e:
                print(f"[axidraw] WARNING: could not disengage XY motors ({_e})")
