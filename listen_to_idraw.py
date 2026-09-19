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
import logging
import math
import socket
import sys
import threading
import time

import numpy as np
from rdp import rdp as _rdp_fn

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

import postprocess
import preview
import recording

# Everything goes through this logger: INFO and up reach the console, DEBUG
# (every point and plotter move) only with --verbose. PantographApp.shell sets
# up the handlers, including the log file.
log = logging.getLogger("pantograph")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these values to match your setup
# ──────────────────────────────────────────────────────────────────────────────

OSC_PORT = 8800

# Maximum pressure value reported by iDraw OSC (Apple Pencil at full press).
# Finger input always reports 1.0. Divide raw values by this to get a 0–1 scale.
OSC_PRESSURE_MAX = 4.166666507720947

# Physical plotting area on paper, in inches, portrait (width ≤ height).
# The AxiDraw will never move outside this rectangle. These are the defaults;
# the paper size is a setting (set_paper), clamped to what the AxiDraw model
# can reach.
PAPER_WIDTH_IN  = 8.5
PAPER_HEIGHT_IN = 11

# AxiDraw models, by pyaxidraw's options.model number: (name, travel along the
# machine's long axis, travel along its short axis), in inches. The paper's
# long side runs along the long axis (see canvas_to_physical).
AXIDRAW_MODELS = {
    1: ("AxiDraw V2 / V3 / SE/A4", 11.81, 8.58),
    2: ("AxiDraw SE/A3",           16.93, 11.69),
    3: ("AxiDraw V3 XLX",          23.39, 8.58),
    4: ("AxiDraw MiniKit",          6.30, 4.00),
    5: ("AxiDraw SE/A1",           34.02, 23.39),
    6: ("AxiDraw SE/A2",           23.39, 17.01),
    7: ("AxiDraw V3/B6",            7.48, 5.51),
}

# Strokes are no longer split by timing. iDraw OSC sends a "state block"
# (/r /g /b /a, the tool flags, /canvasWidth, /canvasHeight, /drawingWidth,
# /eraserWidth) before every new stroke and never in the middle of one, so that
# block is what ends one stroke and starts the next — see STROKE BOUNDARIES.
#
# The pen still has to come off the paper when the Pencil pauses, or it sits
# there bleeding ink. After this long with no new point the plotter *rests* the
# pen: lifts it without ending the stroke. If the stroke carries on, the lift is
# taken back out of the queue when the plotter has not reached it yet, or the
# pen goes back down where it left off — either way the line stays unbroken.
PEN_REST_SEC = 0.15

# Minimum distance between consecutive points sent to the plotter, in paper inches.
# Points that are closer together than this are skipped to avoid flooding the
# AxiDraw with tiny moves that cause stuttering.
# 0.01" (≈ 0.25mm) is a good starting point — increase if the plotter queue grows
# too fast during fast drawing, decrease for more accuracy on slow careful lines.
STREAM_MIN_DIST_IN = 0.01

# Raw pressure value that iDraw OSC sends as a placeholder/default (normalises to ≈ 0.24).
# Isolated occurrences are glitches and are replaced by linear interpolation
# between neighbours.
SPURIOUS_RAW_PRESSURE: float = 1.0

# A run of this many placeholder values in a row is not a glitch: iDraw has no
# pressure reading at all (a finger, or a Pencil it isn't reading). The run is
# then plotted at NO_PRESSURE_VALUE (normalised 0–1) instead. A whole stroke of
# placeholders too short to reach the run length gets the same value when it
# ends, since it has no real neighbours to interpolate from.
NO_PRESSURE_RUN:   int   = 5
NO_PRESSURE_VALUE: float = 0.5

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

# Serializes pen-state mutations (_pen_is_down, _last_plot_pt, _pending_024,
# the rest state) between the OSC thread, the pen-rest watchdog thread and the
# replay thread. Reentrant because the stroke functions call each other under it.
_pen_lock   = threading.RLock()

# Marks the replay thread so the points it emits can be tagged on the way to the
# browser. Thread-local rather than a plain global because live OSC input and a
# replay can be in flight at the same time, on different threads, and each
# broadcast must carry its own origin. See _emit_point's "replay" field.
_replay_flag = threading.local()


def _in_replay() -> bool:
    """True when the calling thread is replaying a recording."""
    return getattr(_replay_flag, "active", False)

_ad               = None   # live AxiDraw handle; used by shutdown cleanup
_last_plot_pt:    tuple | None = None
_stroke_had_moves: bool        = False

_pending_024:               list  = []     # stroke commands buffered while pressure is spurious
_stroke_has_good_pressure:  bool  = False  # True once a non-spurious point is seen this stroke
_stroke_last_good_pressure: float = 0.0   # last non-spurious normalised pressure
_spurious_run:              int   = 0     # placeholder values in a row, this stroke

_pen_rested:   bool = False   # stroke still open, but the pen was lifted during a pause
_rest_cmds:    list = []      # the commands that rested it, kept so they can be taken back
_rest_dwelled: bool = False   # the rest already paused for a tap-dot, so the stroke end needn't


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

# What the plotter is doing, for the UI's status button. States:
#   connected    — AxiDraw open and taking commands
#   not_found    — no AxiDraw on USB (commands are logged and dropped)
#   unavailable  — pyaxidraw isn't installed
#   dry_run      — --dry-run: never touch USB
#   error        — was connected, then failed (message says why); queue discarded
plotter_status = {"state": "not_found", "message": ""}

_plotter_connect_request = threading.Event()   # set by connect_plotter(); served by the plotter thread
_plotter_stop            = threading.Event()   # set by shutdown(); the plotter thread exits
_plotter_thread_handle: threading.Thread | None = None


def _set_plotter_status(state: str, message: str = "") -> None:
    plotter_status.update(state=state, message=message)
    preview.broadcast({"type": "plotter_status", **plotter_status})
    level = logging.ERROR if state == "error" else logging.INFO
    log.log(level, "[axidraw] %s%s", state, f" — {message}" if message else "")


def connect_plotter() -> None:
    """Ask the plotter thread to (re)connect the AxiDraw. Safe from any thread."""
    _plotter_connect_request.set()


def _open_axidraw():
    """
    Open the AxiDraw and put it in a known state. Returns the handle, or None
    with plotter_status saying why. Only ever called on the plotter thread,
    which owns the USB connection.
    """
    try:
        from pyaxidraw import axidraw
    except Exception as e:           # noqa: BLE001 — any import failure means no plotter
        _set_plotter_status("unavailable", f"pyaxidraw isn't installed ({e})")
        return None
    try:
        ad = axidraw.AxiDraw()
        ad.interactive()
        ad.options.model = _axidraw_model
        ad.options.units = 0   # use inches
        ad.options.speed_pendown = 15
        ad.options.speed_penup = 25
        ad.options.accel = 50
        ad.options.pen_rate_lower = 70
        ad.options.pen_rate_raise = 70
        ad.options.pen_delay_down = -100
        ad.options.pen_delay_up = -100
        # connect() returns False (it doesn't raise) when no AxiDraw is on USB.
        if not ad.connect():
            _set_plotter_status("not_found", "no AxiDraw found on USB")
            return None
        ad.options.pen_pos_up   = _pen_pos_up
        ad.options.pen_pos_down = _pen_down_min
        ad.update()          # push pen positions to EBB via servo_init
        ad.penup()           # ensure known pen state on startup
    except Exception as e:           # noqa: BLE001
        _set_plotter_status("error", f"could not connect ({e})")
        return None
    _set_plotter_status("connected")
    return ad


def _close_axidraw(ad) -> None:
    try:
        ad.disconnect()
    except Exception:                # noqa: BLE001 — it's going away either way
        pass


def _plotter_thread():
    """
    Polls the plot deque and executes commands on the AxiDraw one at a time.
    Records command age at dequeue time to keep _current_lag_sec up to date.
    Owns the USB connection: connecting, reconnecting and command errors all
    happen here. With no AxiDraw, commands are logged (DEBUG) and dropped.
    """
    global _ad, _current_lag_sec

    if DRY_RUN:
        ad = None
        _set_plotter_status("dry_run", "--dry-run: moves are logged, not plotted")
    else:
        ad = _open_axidraw()
    _ad = ad

    # Per-stroke state for mid-stroke pressure updates (local to this thread)
    lineto_counter    = 0
    last_applied_down = None   # last pen_pos_down sent to EBB this stroke

    def run(cmd):
        nonlocal lineto_counter, last_applied_down
        kind = cmd[1]

        if kind == "moveto":
            x, y = cmd[2], cmd[3]
            log.debug("[axidraw] travel → (%.3f\", %.3f\")", x, y)
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()
                ad.penup()
                ad.moveto(x, y)

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
            log.debug("  pendown  (pos=%d)", target_pos)
            if ad:
                ad.options.pen_pos_down = target_pos
                ad.update()
                ad.pendown()
            lineto_counter    = 0
            last_applied_down = target_pos

        elif kind == "lineto":
            x, y = cmd[2], cmd[3]
            pressure = cmd[4] if len(cmd) > 4 else 1.0

            if (_variable_pressure or _x_tilt_deg != 0.0 or _y_tilt_deg != 0.0) and _pressure_update_rate > 0:
                interval = max(1, round(100 / _pressure_update_rate))
                lineto_counter += 1
                if lineto_counter % interval == 0:
                    if _variable_pressure:
                        target = _pen_down_min + (_pen_down_max - _pen_down_min) * pressure
                    else:
                        target = float(_pen_down_min)
                    target += _tilt_pen_offset(x, y)
                    new_pos = max(0, min(100, round(target)))
                    if new_pos != last_applied_down:
                        last_applied_down = new_pos
                        log.debug("  [pressure/tilt] pos=%d", new_pos)
                        if ad:
                            ad.options.pen_pos_down = new_pos
                            ad.update()

            log.debug("  lineto  (%.3f\", %.3f\")", x, y)
            if ad:
                ad.lineto(x, y)

        elif kind == "dot_dwell":
            log.debug("  [dot — 100ms dwell]")
            if ad:
                time.sleep(0.1)

        elif kind == "penup":
            log.debug("[axidraw] pen up")
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()
                ad.penup()

        elif kind == "home":
            log.info("[axidraw] homing → (0.000\", 0.000\")")
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()
                ad.penup()
                ad.moveto(0, 0)

        elif kind == "pen_test_up":
            log.info("[axidraw] pen test → up (pos=%d)", _pen_pos_up)
            if ad:
                ad.options.pen_pos_up = _pen_pos_up
                ad.update()   # servo_init re-sends SC commands; moves pen if pos changed
                ad.penup()

        elif kind == "pen_test_min":
            log.info("[axidraw] pen test → min down (pos=%d)", _pen_down_min)
            if ad:
                ad.options.pen_pos_down = _pen_down_min
                ad.update()
                ad.pendown()

        elif kind == "pen_test_max":
            log.info("[axidraw] pen test → max down (pos=%d)", _pen_down_max)
            if ad:
                ad.options.pen_pos_down = _pen_down_max
                ad.update()
                ad.pendown()

    while not _plotter_stop.is_set():
        if _plotter_connect_request.is_set():
            _plotter_connect_request.clear()
            if not DRY_RUN:
                if ad:
                    _close_axidraw(ad)
                ad = _open_axidraw()
                _ad = ad

        cmd = None
        with _plot_lock:
            if _plot_deque:
                cmd = _plot_deque.popleft()

        if cmd is None:
            time.sleep(0.005)
            continue

        # cmd = (enqueue_time, kind, *args)
        _current_lag_sec = time.monotonic() - cmd[0]
        try:
            run(cmd)
        except Exception as e:       # noqa: BLE001 — typically the USB cable was pulled
            # The rest of the queue was planned for a machine we no longer
            # control; drop it rather than replay it into an unknown position.
            with _plot_lock:
                _plot_deque.clear()
            if ad:
                _close_axidraw(ad)
            ad = _ad = None
            _set_plotter_status("error", f"lost the AxiDraw during '{cmd[1]}' ({e}); queue discarded")


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

        if n > 0:
            log.debug("[opt] -%d pts  lag=%.2fs  ε=%.4f\"", n, lag, epsilon)


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
# pauses does not take its original wall-clock time to plot. Stroke boundaries
# come from the recording itself, so this only affects pacing (a gap above
# PEN_REST_SEC rests the pen mid-stroke, exactly as it would live).
REPLAY_MAX_GAP_SEC = 0.30


def _replay_recording(rec: dict) -> None:
    """Feed a recorded drawing back through the live pipeline. Runs on its own thread."""
    global _effect_chain, _effect_ctx

    strokes = rec.get("strokes") or []
    n_pts   = sum(len(s.get("points") or []) for s in strokes)
    log.info("[replay] %d stroke(s), %d point(s) — starting", len(strokes), n_pts)

    # Tag every point this thread emits as replay-originated, so the browser
    # animates it without recording it. Without this the browser re-records the
    # drawing it is replaying, doubling the canvas and any SVG downloaded after.
    _replay_flag.active = True

    # Rebuild the effects so a replay never inherits state (or RNG position)
    # from whatever was drawn before it.
    with _plot_lock:
        _effect_chain = postprocess.build_chain(_EFFECT_SWITCHES, _effect_params)
        _effect_ctx   = postprocess.Ctx(x_max=PAPER_HEIGHT_IN, y_max=PAPER_WIDTH_IN)

    # A live stroke left open would otherwise swallow the replay's first stroke.
    _end_stroke()

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

        # The recording already knows where each stroke ends — this stands in
        # for the state block iDraw would have sent before the next one.
        _end_stroke()

    log.info("[replay] done")


# ──────────────────────────────────────────────────────────────────────────────
# STROKE BOUNDARIES
# ──────────────────────────────────────────────────────────────────────────────
#
# A stroke ends only when the next one's state block arrives (or, on replay,
# where the recording says it ends). Timing never splits a stroke; it only rests
# the pen during a pause, and that rest is undone if the stroke carries on.
#
# Consequence: iDraw sends nothing when the Pencil lifts, so the *latest* stroke
# stays open until the next one begins. Its pen is off the paper (rested), but
# the effects that act at a stroke's end — zigzag's last corner, pressure hatch,
# stroke connector — and the preview's pen_up run when the next stroke starts.

def _end_stroke():
    """Close the open stroke, if there is one."""
    global _last_plot_pt, _stroke_had_moves, _pen_rested, _rest_cmds, _rest_dwelled
    # Held under _pen_lock so this never nulls _last_plot_pt in the middle of an
    # _emit_point read-modify-write on another thread (the crash during replay).
    with _pen_lock:
        if not state["_pen_is_down"]:
            return
        state["_pen_is_down"] = False
        rested_dwell = _rest_dwelled
        _pen_rested   = False
        _rest_cmds    = []
        _rest_dwelled = False

        if _pending_024:
            if not _stroke_has_good_pressure:
                # Every point was a placeholder, too few to reach NO_PRESSURE_RUN
                # (a short finger stroke or tap). Nothing real to interpolate
                # from, so plot it at the no-pressure value — pendown is in the
                # buffer, so this is where the pen first goes down.
                _flush_pending_024(NO_PRESSURE_VALUE, from_pressure=NO_PRESSURE_VALUE)
                log.debug("[pressure] no pressure data in this stroke — plotted at %s", NO_PRESSURE_VALUE)
            else:
                # Trailing spurious points — interpolate pressure down to 0. (A
                # rested pen never gets here: resting flushes these, and points
                # after it arrive with the pen back down.)
                _flush_pending_024(0.0)

        preview.broadcast({"type": "pen_up"})
        t = time.monotonic()
        if not _stroke_had_moves and not rested_dwell:
            _enqueue(postprocess.dot_dwell(t))
        # Goes through the effects even when the pen is already resting — this is
        # the one penup per stroke they see, and what their stroke-end work hangs on.
        _enqueue(postprocess.penup(t))
        _last_plot_pt = None
        _stroke_had_moves = False
        log.debug("[stroke end]")


def _state_block_seen():
    """
    Called by every state-block handler before it stores its value. iDraw sends
    the block before every new stroke, so whatever stroke is open has ended (and
    must end before the block's new colour/width/tool land in `state`). The first
    message of a block does the work; the rest find no stroke open.
    """
    if state["_pen_is_down"]:
        _end_stroke()


def _maybe_rest_pen():
    """Lift the pen off the paper if the point stream has paused, keeping the stroke open."""
    global _pen_rested, _rest_cmds, _rest_dwelled
    with _pen_lock:
        last = state["_last_point_time"]
        if (last is None or not state["_pen_is_down"] or _pen_rested
                or time.time() - last <= PEN_REST_SEC):
            return
        _pen_rested = True
        if not _stroke_has_good_pressure:
            return      # every point so far is buffered as spurious — pen never went down
        if _pending_024:
            _flush_pending_024(0.0)   # taper exactly as a real lift would
        if _effects_only:
            return      # the base stroke isn't being plotted, so there's no pen to rest
        # Raw, not through the effects: to them the stroke is still going.
        t = time.monotonic()
        cmds = []
        if not _stroke_had_moves:
            cmds.append(postprocess.dot_dwell(t))
            _rest_dwelled = True
        cmds.append(postprocess.penup(t))
        with _plot_lock:
            _plot_deque.extend(cmds)
        _rest_cmds = cmds
        log.debug("[pen rest — pause]")


def _resume_after_rest(t: float, pressure: float) -> None:
    """
    The stroke carried on after a rest. Take the lift back out of the queue if the
    plotter hasn't reached it yet — the usual case, since it runs behind — else
    lower the pen again where it left off. Called with _pen_lock held.
    """
    global _pen_rested, _rest_cmds, _rest_dwelled
    _pen_rested = False
    cmds, _rest_cmds = _rest_cmds, []
    if not cmds:
        return      # nothing was lifted (pen never down, or effects-only)
    with _plot_lock:
        n = len(cmds)
        if len(_plot_deque) >= n and all(_plot_deque[i - n] is c for i, c in enumerate(cmds)):
            for _ in range(n):
                _plot_deque.pop()
            _rest_dwelled = False
            log.debug("[pen rest taken back — still queued]")
            return
    if _effects_only:
        return
    lx, ly = _last_plot_pt
    _enqueue_raw(postprocess.pendown(t, pressure, lx, ly))
    log.debug("[pen down again — stroke continues]")


def _pen_rest_thread():
    """Polls for a quiet point stream and rests the pen once PEN_REST_SEC passes."""
    while True:
        time.sleep(PEN_REST_SEC / 2)
        _maybe_rest_pen()


# ──────────────────────────────────────────────────────────────────────────────
# OSC HANDLERS
# ──────────────────────────────────────────────────────────────────────────────


def _flush_pending_024(next_pressure: float, from_pressure: float | None = None) -> None:
    """
    Emit buffered spurious-pressure items with linearly interpolated pressures
    (from _stroke_last_good_pressure, or from_pressure if given, → next_pressure),
    then clear the buffer. Sets _stroke_had_moves for any lineto commands emitted.
    """
    global _stroke_had_moves
    n = len(_pending_024)
    if n == 0:
        return
    p_prev = _stroke_last_good_pressure if from_pressure is None else from_pressure
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
    global _last_plot_pt, _stroke_had_moves, _stroke_has_good_pressure, _stroke_last_good_pressure, \
           _pen_rested, _rest_cmds, _rest_dwelled, _spurious_run

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

    # Whole pen-state read-modify-write runs under _pen_lock so the rest thread
    # cannot lift the pen partway through it.
    with _pen_lock:
        # Only the state block (via _end_stroke) ends a stroke, so any point that
        # arrives while one is open belongs to it, however long the gap before it.
        was_down = state["_pen_is_down"]
        state["_pen_is_down"]     = True
        state["_last_point_time"] = now

        # Paper mapping + landscape rotation + flip H/V, all in one step — see
        # canvas_to_physical(). px ∈ [0, PAPER_HEIGHT_IN], py ∈ [0, PAPER_WIDTH_IN].
        px, py = canvas_to_physical(x, y, mapping)

        # Normalize pressure to 0–1 (raw iDraw range is 0–OSC_PRESSURE_MAX)
        pressure_norm = min(1.0, max(0.0, state["pressure"] / OSC_PRESSURE_MAX))
        spurious = (state["pressure"] == SPURIOUS_RAW_PRESSURE)

        # A run of placeholders too long to be a glitch means iDraw has no
        # pressure reading at all: plot the run, and the rest of it, at a fixed
        # value instead of holding the pen up waiting for a real one.
        _spurious_run = ((_spurious_run + 1) if was_down else 1) if spurious else 0
        if spurious and _spurious_run >= NO_PRESSURE_RUN:
            spurious      = False
            pressure_norm = NO_PRESSURE_VALUE
            if _pending_024:
                _flush_pending_024(pressure_norm, from_pressure=pressure_norm)
                log.debug("[pressure] no pressure data — plotting at %s", NO_PRESSURE_VALUE)
            _stroke_has_good_pressure  = True
            _stroke_last_good_pressure = pressure_norm

        if not was_down:
            # First point of a new stroke — reset filter state, queue travel, hold pen down
            _stroke_had_moves = False
            _stroke_has_good_pressure = False
            _stroke_last_good_pressure = 0.0
            _pen_rested   = False
            _rest_cmds    = []
            _rest_dwelled = False
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
            # Continuing after a pause: undo the rest before drawing on.
            if _pen_rested:
                _resume_after_rest(t, _stroke_last_good_pressure if spurious else pressure_norm)

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
        # So the page and the recorder draw replayed points without recording
        # them (else replaying a drawing records it a second time).
        "replay":       _in_replay(),
    })

    log.debug("[point] (%.1f, %.1f)  p=%.2f  tool=%s", x, y, pressure_norm, state["tool"])


def _handle_x(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["x"] = args[0]

def _handle_pressure(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["pressure"] = args[0]

# Every handler for a state-block field calls _state_block_seen() first — see
# STROKE BOUNDARIES.

def _handle_r(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["r"] = args[0]

def _handle_g(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["g"] = args[0]

def _handle_b(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["b"] = args[0]

def _handle_a(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["a"] = args[0]

def _handle_drawing_width(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["drawingWidth"] = args[0]

def _handle_eraser_width(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["eraserWidth"] = args[0]

def _handle_aspect(address, *args):
    if _show_raw_osc: _log_raw(address, *args)

def _handle_y(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["y"] = args[0]
    _emit_point()   # /y is always the last field per burst

def _handle_canvas_width(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["canvasWidth"] = args[0]
    _update_mapping()

def _handle_canvas_height(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    state["canvasHeight"] = args[0]
    _update_mapping()

def _update_mapping():
    w = state["canvasWidth"]
    h = state["canvasHeight"]
    if w and h:
        state["_mapping"] = compute_mapping(w, h)
        m = state["_mapping"]
        log.debug("[mapping] canvas %.0f×%.0fpx → draw area %.2f\"×%.2f\" on %s\"×%s\" paper  "
                  "(margins: x=%.2f\" y=%.2f\")", w, h, m["draw_w"], m["draw_h"],
                  PAPER_WIDTH_IN, PAPER_HEIGHT_IN, m["margin_x"], m["margin_y"])
        preview.broadcast({"type": "canvas_size", "width": w, "height": h})

def _handle_tool_flag(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    _state_block_seen()
    value     = args[0]
    tool_name = address.lstrip("/")
    state[tool_name] = value
    if value == 1.0:
        state["tool"] = tool_name
        preview.broadcast({"type": "tool_change", "tool": tool_name})
        log.debug("[tool] -> %s", tool_name)

def _handle_unknown(address, *args):
    if _show_raw_osc:
        _log_raw(address, *args)
    else:
        log.debug("[unknown] %s: %s", address, args)


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
# BROWSER → ENGINE MESSAGES
# ──────────────────────────────────────────────────────────────────────────────

def _handle_preview_message(msg):
    """Control messages from the page: settings, plotter commands, replay, quit."""
    global _opt_enabled, _opt_scale, _lag_threshold_sec, _limit_lag, _min_dist_in, \
           _variable_pressure, _pen_pos_up, _pen_down_min, _pen_down_max, _pressure_update_rate, \
           _x_tilt_deg, _y_tilt_deg, _effects_only
    t = msg.get("type")
    if t == "home":
        _enqueue_raw((time.monotonic(), "home"))
        log.info("[axidraw] home queued — will execute after current commands")
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
            log.info("[effect] %s %s", name, "on" if _EFFECT_SWITCHES[name] else "off")
    elif t == "set_effect_param":
        name = msg.get("name")
        attr = msg.get("attr")
        if name in _effect_params and attr in _effect_params[name]:
            _effect_params[name][attr] = msg.get("value")
            _rebuild_effect_chain()
    elif t == "set_effect_param_key":
        # A saved setting's "<effect>_<ATTR>" — both halves may contain "_".
        for name, params in _effect_params.items():
            for attr in params:
                if f"{name}_{attr}" == msg.get("key"):
                    params[attr] = msg.get("value")
                    _rebuild_effect_chain()
    elif t == "set_effects_only":
        _effects_only = bool(msg.get("enabled", False))
        log.info("[effect] effects-only %s", "on" if _effects_only else "off")
    elif t == "replay":
        rec = msg.get("recording") or {}
        threading.Thread(
            target=_replay_recording, args=(rec,), daemon=True
        ).start()
    elif t == "pen_test_up":
        _enqueue_raw((time.monotonic(), "pen_test_up"))
    elif t == "pen_test_min":
        _enqueue_raw((time.monotonic(), "pen_test_min"))
    elif t == "pen_test_max":
        _enqueue_raw((time.monotonic(), "pen_test_max"))
    elif t == "connect_plotter":
        connect_plotter()
    elif t == "restart_osc":
        port = msg.get("port")
        restart_osc_listener(int(port) if port else None)
    elif t == "quit":
        request_stop()
    elif t == "set_paper":
        return set_paper(float(msg.get("width", 8.5)), float(msg.get("height", 11.0)))
    elif t == "set_model":
        return set_model(int(msg.get("model", 1)))
    elif t == "save_settings":
        if _settings is not None:
            _settings.replace(msg.get("values") or {})
    elif t == "new_drawing":
        new_drawing()
    elif t == "save_svg":
        return _save_svg_reply(msg)


# ──────────────────────────────────────────────────────────────────────────────
# OSC LISTENER
# ──────────────────────────────────────────────────────────────────────────────
#
# One thread, handling packets in arrival order. iDraw sends every value as
# its own packet, and the state block only works as a stroke separator if it is
# handled in order with the points around it — a threaded server can run
# handlers out of order. The handlers are quick (motion lives on the plotter
# thread), so one thread keeps up; a bigger receive buffer absorbs bursts.

# listening | port_busy | stopped — for the UI's iPad status button
osc_status = {"state": "stopped", "port": OSC_PORT, "message": ""}
last_osc_time: float | None = None   # when any OSC packet last arrived (None = never)

_osc_server: BlockingOSCUDPServer | None = None


class _OSCServer(BlockingOSCUDPServer):
    """The single-threaded OSC server, noting when anything last arrived."""

    def process_request(self, request, client_address):
        global last_osc_time
        last_osc_time = time.time()
        super().process_request(request, client_address)


def _set_osc_status(state: str, message: str = "") -> None:
    osc_status.update(state=state, port=OSC_PORT, message=message)
    preview.broadcast({"type": "osc_status", **osc_status})
    level = logging.ERROR if state == "port_busy" else logging.INFO
    log.log(level, "[osc] %s on port %d%s", state, OSC_PORT, f" — {message}" if message else "")


def stop_osc_listener() -> None:
    global _osc_server
    if _osc_server is not None:
        _osc_server.shutdown()
        _osc_server.server_close()
        _osc_server = None
        _set_osc_status("stopped")


def restart_osc_listener(port: int | None = None) -> bool:
    """
    (Re)open the UDP listener, on a new port if given. Returns False — with
    osc_status saying why — if the port is taken, typically by another copy of
    this app or another OSC program.
    """
    global _osc_server, OSC_PORT
    stop_osc_listener()
    if port:
        OSC_PORT = port
    try:
        server = _OSCServer(("0.0.0.0", OSC_PORT), _build_dispatcher())
    except OSError as e:
        _set_osc_status("port_busy", f"port {OSC_PORT} is in use by another program ({e})")
        return False
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    _osc_server = server
    threading.Thread(target=server.serve_forever, name="osc", daemon=True).start()
    _set_osc_status("listening")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# PAPER AND MACHINE
# ──────────────────────────────────────────────────────────────────────────────

_axidraw_model   = 1
_paper_requested = (PAPER_WIDTH_IN, PAPER_HEIGHT_IN)   # what was asked for, before clamping


def _plot_busy() -> bool:
    with _plot_lock:
        return bool(_plot_deque)


def paper_info() -> dict:
    name, long_in, short_in = AXIDRAW_MODELS[_axidraw_model]
    return {"type": "paper", "width": PAPER_WIDTH_IN, "height": PAPER_HEIGHT_IN,
            "requested": list(_paper_requested), "model": _axidraw_model, "model_name": name,
            "clamped": (PAPER_WIDTH_IN, PAPER_HEIGHT_IN) != tuple(sorted(_paper_requested))}


def set_paper(width: float, height: float) -> dict:
    """
    Set the paper size (inches, either orientation — it's used portrait),
    clamped to what the AxiDraw model can reach. Refused mid-plot: the queue
    was planned for the old paper.
    """
    global PAPER_WIDTH_IN, PAPER_HEIGHT_IN, _effect_ctx, _paper_requested
    if _plot_busy():
        return {"type": "error", "message": "Finish or discard the current plot before changing the paper."}
    w, h = sorted((abs(width), abs(height)))
    if w <= 0:
        return {"type": "error", "message": "The paper needs a width and a height."}
    _paper_requested = (w, h)
    _, long_in, short_in = AXIDRAW_MODELS[_axidraw_model]
    PAPER_WIDTH_IN, PAPER_HEIGHT_IN = min(w, short_in), min(h, long_in)
    with _plot_lock:
        _effect_ctx = postprocess.Ctx(x_max=PAPER_HEIGHT_IN, y_max=PAPER_WIDTH_IN)
    _update_mapping()
    info = paper_info()
    if info["clamped"]:
        log.warning("[paper] %.2f\" × %.2f\" is larger than the %s reaches; using %.2f\" × %.2f\"",
                    w, h, info["model_name"], PAPER_WIDTH_IN, PAPER_HEIGHT_IN)
    else:
        log.info("[paper] %.2f\" × %.2f\"", PAPER_WIDTH_IN, PAPER_HEIGHT_IN)
    preview.broadcast(info)
    return info


def set_model(model: int) -> dict:
    """Set the AxiDraw model; re-applies the paper size against its reach."""
    global _axidraw_model
    if model not in AXIDRAW_MODELS:
        return {"type": "error", "message": f"Unknown AxiDraw model {model}."}
    if _plot_busy():
        return {"type": "error", "message": "Finish or discard the current plot before changing the model."}
    changed = model != _axidraw_model
    _axidraw_model = model
    log.info("[axidraw] model %d: %s", model, AXIDRAW_MODELS[model][0])
    info = set_paper(*_paper_requested)
    if changed and plotter_status["state"] == "connected":
        connect_plotter()        # reconnect so pyaxidraw applies the model's limits
    return info


# ──────────────────────────────────────────────────────────────────────────────
# THE SESSION: recording, autosave, saving
# ──────────────────────────────────────────────────────────────────────────────
#
# The drawing is recorded here, in Python, from the same messages the page
# gets — so closing or reloading the page loses nothing. autosave.svg in the
# drawings folder is rewritten every couple of seconds while drawing. A clean
# exit, or "New drawing", saves the session under its own name and removes
# autosave.svg; so an autosave.svg found at startup means the last session
# didn't end cleanly, and it's kept as recovered-<time>.svg.

APP_VERSION = "dev"          # set by main() from pyproject.toml
AUTOSAVE_NAME = "autosave.svg"

_recorder = recording.Recorder()
_drawings_dir = None         # pathlib.Path, set by start()
_settings = None             # PantographApp.settings.Settings, set by start()


def _autosave() -> None:
    if _drawings_dir is None or not _recorder.dirty:
        return
    _recorder.dirty = False
    if not _recorder.is_empty():
        recording.write_atomic(_drawings_dir / AUTOSAVE_NAME, _recorder.svg())


def _autosave_thread():
    while not _stop_event.wait(2.0):
        try:
            _autosave()
        except Exception:            # noqa: BLE001 — keep trying; log why
            log.exception("[autosave] failed")


def _recover_autosave() -> None:
    f = _drawings_dir / AUTOSAVE_NAME
    if f.exists():
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(f.stat().st_mtime))
        dest = recording.unique_path(_drawings_dir, f"recovered-{stamp}.svg")
        f.replace(dest)
        log.warning("[autosave] the last session didn't end cleanly — its drawing is saved as %s", dest.name)


def _save_session() -> str | None:
    """Save the current drawing under a new timestamped name. Returns the name, or None if empty."""
    if _drawings_dir is None or _recorder.is_empty():
        return None
    path = recording.unique_path(_drawings_dir, recording.timestamped_name())
    recording.write_atomic(path, _recorder.svg())
    log.info("[drawing] saved  →  %s", path)
    return path.name


def _end_session() -> None:
    """Shutdown hook: save the drawing, then drop autosave.svg (this was a clean exit)."""
    _save_session()
    if _drawings_dir is not None:
        (_drawings_dir / AUTOSAVE_NAME).unlink(missing_ok=True)


def new_drawing() -> None:
    """Save the current drawing (if any) and start a fresh one, on every open page."""
    _end_stroke()
    saved = _save_session()
    _recorder.clear()
    if _drawings_dir is not None:
        (_drawings_dir / AUTOSAVE_NAME).unlink(missing_ok=True)
    preview.broadcast({"type": "new_drawing", "saved": saved})


def _save_svg_reply(msg: dict) -> dict:
    """The page's "download → svg": build it from the recording and save it."""
    layers = msg.get("layers") or {}
    try:
        svg = _recorder.svg(include_raw=bool(layers.get("raw", True)),
                            optimized=bool(layers.get("optimized")),
                            effect=bool(layers.get("effect")))
        name = str(msg.get("filename") or "drawing.svg")
        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name.split("/")[-1].split("\\")[-1])
        path = recording.unique_path(_drawings_dir, name or "drawing.svg")
        recording.write_atomic(path, svg)
        log.info("[drawing] saved  →  %s", path)
        return {"type": "saved", "ok": True, "path": path.name}
    except Exception as exc:         # noqa: BLE001 — reported to the page
        log.exception("[drawing] save failed")
        return {"type": "saved", "ok": False, "error": str(exc)}


def _hello() -> dict:
    """Everything a page needs when it connects: settings, the drawing so far, statuses."""
    return {
        "type":           "hello",
        "version":        APP_VERSION,
        "settings":       dict(_settings.values) if _settings is not None else {},
        "drawing":        _recorder.messages(),
        "plotter_status": dict(plotter_status),
        "osc_status":     dict(osc_status),
        "paper":          paper_info(),
        "lag":            round(_current_lag_sec, 2),
    }


def _apply_setting(msg: dict) -> None:
    """Put one saved setting into effect, exactly as if the page had sent it."""
    if msg["type"] == "set_flip_x":
        preview.flip_x = msg["enabled"]
    elif msg["type"] == "set_flip_y":
        preview.flip_y = msg["enabled"]
    else:
        _handle_preview_message(msg)


# ──────────────────────────────────────────────────────────────────────────────
# START / STOP
# ──────────────────────────────────────────────────────────────────────────────

_stop_event    = threading.Event()   # set to end main()'s wait: Quit button, signals
_shutdown_lock = threading.Lock()
_shutdown_done = False
_shutdown_hooks: list = []           # extra cleanup run by shutdown(), e.g. forgetting instance.json


def request_stop() -> None:
    """Ask main() to shut the app down (the Quit button). Safe from any thread."""
    _stop_event.set()


def start(ui_ports=preview.DEFAULT_PORTS, drawings_dir=None, settings=None) -> str:
    """
    Start the engine: preview server, background threads, OSC listener. Returns
    immediately with the UI's URL; everything runs on background threads, so
    the caller's (main) thread stays free — Option 1B's window will need it.
    """
    global _plotter_thread_handle, _drawings_dir, _settings
    from pathlib import Path

    # Initial mapping from the default canvas size; recomputed as soon as iDraw
    # sends its real canvas dimensions.
    state["_mapping"] = compute_mapping(state["canvasWidth"], state["canvasHeight"])

    # Saved settings first, so the plotter starts configured even with no page open.
    _settings = settings
    if settings is not None:
        from PantographApp.settings import engine_messages
        for m in engine_messages(settings.values):
            _apply_setting(m)

    if drawings_dir is not None:
        _drawings_dir = Path(drawings_dir)
        _drawings_dir.mkdir(parents=True, exist_ok=True)
        _recover_autosave()
    preview.add_listener(_recorder.on_message)
    preview.register_hello_provider(_hello)

    ui_port = preview.start(ports=ui_ports, save_dir=drawings_dir)
    preview.register_message_callback(_handle_preview_message)

    # Plotter separate from OSC, so motors never block reception.
    _plotter_thread_handle = threading.Thread(target=_plotter_thread, name="plotter", daemon=True)
    _plotter_thread_handle.start()
    threading.Thread(target=_pen_rest_thread,      name="pen-rest", daemon=True).start()
    threading.Thread(target=_optimizer_thread,     name="optimizer", daemon=True).start()
    threading.Thread(target=_lag_broadcast_thread, name="lag",       daemon=True).start()
    if _drawings_dir is not None:
        threading.Thread(target=_autosave_thread, name="autosave", daemon=True).start()
        _shutdown_hooks.append(_end_session)

    restart_osc_listener()
    return f"http://127.0.0.1:{ui_port}"


def shutdown() -> None:
    """
    Stop everything and leave the AxiDraw safe: pen up, motors released so the
    carriage can be pushed home by hand. Runs once, however many exit paths
    call it, and from whichever thread calls it first.

    The machine is made safe *first*. Closing the console window gives this a
    few seconds before Windows ends the process, so saving the drawing and
    closing the servers come after (if they're cut short, the autosave from a
    moment ago is recovered on the next launch).
    """
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True

        log.info("[shutdown] lifting pen, disabling XY motors, and disconnecting...")
        _stop_event.set()
        stop_osc_listener()

        # Let the plotter finish the command it's on, then take the USB link over.
        _plotter_stop.set()
        if _plotter_thread_handle is not None:
            _plotter_thread_handle.join(timeout=2)
        with _plot_lock:
            _plot_deque.clear()
        if _ad:
            _make_axidraw_safe(_ad)

        preview.stop()
        for hook in _shutdown_hooks:
            try:
                hook()
            except Exception:        # noqa: BLE001 — a failed save mustn't stop the rest
                log.exception("[shutdown] hook failed")


def _make_axidraw_safe(ad) -> None:
    """Pen up and XY motors off, on the connection that's already open (fast)."""
    released = False
    try:
        ad.penup()
        ad.block()                   # wait for the lift to finish before cutting the motors
        from plotink import ebb_motion
        ebb_motion.sendDisableMotors(ad.plot_status.port, False)
        released = True
    except Exception as e:           # noqa: BLE001
        log.warning("[axidraw] couldn't release the motors directly (%s); trying a fresh connection", e)
    _close_axidraw(ad)
    if not released:
        # The slow way: a fresh connection running pyaxidraw's manual
        # "disable_xy" command. (This used to run mode "res_home2", which
        # pyaxidraw doesn't have: it did nothing, and the motors stayed on.)
        try:
            from pyaxidraw import axidraw as _axi_mod
            disarm = _axi_mod.AxiDraw()
            disarm.plot_setup()
            disarm.options.mode = "manual"
            disarm.options.manual_cmd = "disable_xy"
            disarm.plot_run()
            released = True
        except Exception as e:       # noqa: BLE001
            log.warning("[axidraw] WARNING: could not disengage XY motors (%s)", e)
    if released:
        log.info("[axidraw] XY motors disengaged — move carriage home manually")


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    global _show_raw_osc, DRY_RUN, OSC_PORT

    parser = argparse.ArgumentParser(
        prog="python listen_to_idraw.py",
        description="iDraw OSC → Python → AxiDraw live drawing pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python listen_to_idraw.py              # plot live\n"
            "  python listen_to_idraw.py --dry-run    # log moves, no USB\n"
            "  python listen_to_idraw.py --raw-osc    # show raw OSC stream"
        ),
    )
    parser.add_argument(
        "--raw-osc",
        action="store_true",
        help=(
            "Print every incoming OSC message verbatim (address + value). "
            "Useful for verifying what iDraw OSC is actually sending "
            "and discovering new message addresses."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Never connect to the AxiDraw; plotter moves are logged instead "
            "(visible with --verbose). Use this to verify coordinate mapping "
            "before putting pen to paper."
        ),
    )
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open the UI in a browser.")
    parser.add_argument("--port", type=int, metavar="N",
                        help="Serve the UI on port N only (default: the first free port "
                             "from 5810 to 5830).")
    parser.add_argument("--osc-port", type=int, metavar="N",
                        help=f"Listen for iDraw OSC on port N (default {OSC_PORT}).")
    parser.add_argument("--verbose", action="store_true",
                        help="Log every point and plotter move (the old per-point output).")
    parser.add_argument("--data-dir", metavar="PATH",
                        help="Keep drawings, settings, logs and runtime files under PATH "
                             "instead of the usual per-user folders.")
    cli = parser.parse_args(argv)
    _show_raw_osc = cli.raw_osc
    DRY_RUN       = cli.dry_run
    if cli.osc_port:
        OSC_PORT = cli.osc_port

    from PantographApp import shell
    paths = shell.resolve_paths(cli.data_dir)
    log_file = shell.setup_logging(paths.logs, verbose=cli.verbose)

    # Already running (say, the launcher was double-clicked twice)? Show that
    # copy instead of fighting it for the ports.
    existing = shell.running_instance(paths.runtime)
    if existing:
        log.info("Pantograph is already running at %s — opening it.", existing)
        shell.open_ui(existing, enabled=not cli.no_browser)
        return 0

    _fx = [e.name for e in _effect_chain]
    log.info("=" * 60)
    log.info("  Pantograph %s — iDraw OSC -> Python -> AxiDraw", shell.app_version())
    log.info("  OSC port     : %d", OSC_PORT)
    log.info("  Paper size   : %s\" × %s\"", PAPER_WIDTH_IN, PAPER_HEIGHT_IN)
    log.info("  Min pt dist  : %s\"", STREAM_MIN_DIST_IN)
    log.info("  AxiDraw      : %s", "DRY RUN" if DRY_RUN else "ENABLED")
    log.info("  Output mode  : %s", "RAW OSC" if _show_raw_osc else "pipeline")
    log.info("  Effects      : %s", ", ".join(_fx) if _fx else "none")
    log.info("  Drawings     : %s", paths.drawings)
    log.info("  Log file     : %s", log_file)
    log.info("=" * 60)
    log.info("")
    log.info("  In iDraw OSC (iPad):")
    log.info("    IP   -> your computer's local Wi-Fi IP")
    log.info("    Port -> %d", OSC_PORT)
    log.info("")

    global APP_VERSION
    APP_VERSION = shell.app_version()
    from PantographApp.settings import Settings
    ui_ports = [cli.port] if cli.port else preview.DEFAULT_PORTS
    try:
        url = start(ui_ports=ui_ports, drawings_dir=paths.drawings, settings=Settings(paths.config))
    except RuntimeError as e:
        log.error("Can't start: %s", e)
        return 1
    shell.record_instance(paths.runtime, preview.port)
    _shutdown_hooks.append(lambda: shell.forget_instance(paths.runtime))
    shell.install_exit_handlers(shutdown)
    shell.open_ui(url, enabled=not cli.no_browser)
    log.info("[ui] %s", url)

    try:
        # A timed wait, not a bare wait(): Ctrl+C only interrupts the main
        # thread between waits on Windows.
        while not _stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
