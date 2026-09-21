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
import concurrent.futures
import json
import queue
import typing
import logging
import math
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np
from rdp import rdp as _rdp_fn

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

import layout
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
# "Heal dots as I draw": a stroke starting this soon, and this close to where
# the last one ended, is taken as the same line torn in two, so the pen carries
# on instead of lifting. (The offline dot_healer does this by looking at the
# whole drawing; live there's nothing to look ahead at, so these two limits
# stand in for its line-dots-line test.) Off unless asked for.
HEAL_LIVE_GAP_IN:   float = 0.15
HEAL_LIVE_TIME_SEC: float = 0.30

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

# The paper is the anchor: the drawing and the machine are two rectangles
# placed on it (layout.py). This is the only path from a tablet point to the
# machine, and the preview draws from the same numbers.

_layout: dict = {}             # set by _update_layout(); see layout.py
_out_of_reach_logged = False


def travel() -> tuple[float, float]:
    """The chosen AxiDraw's reach: (long axis, short axis) in inches."""
    _, long_in, short_in = AXIDRAW_MODELS[_axidraw_model]
    return long_in, short_in


def _update_layout(refit: bool = False) -> None:
    """
    Rebuild the layout after the paper, the model, the tablet's canvas or the
    layout itself changed. `refit` re-fits a layout nobody has placed by hand.
    """
    global _layout
    long_in, short_in = travel()
    cw, ch = state["canvasWidth"], state["canvasHeight"]
    if refit:
        _layout["auto"] = True
    _layout = layout.normalize(_layout, PAPER_WIDTH_IN, PAPER_HEIGHT_IN, cw, ch, long_in, short_in)
    _set_effect_bounds(long_in, short_in)
    preview.broadcast(layout_msg())
    log.debug("[layout] paper %.2f×%.2f\", canvas %.0f×%.0f, machine %.2f×%.2f\"",
              PAPER_WIDTH_IN, PAPER_HEIGHT_IN, cw, ch, long_in, short_in)


def _set_effect_bounds(long_in: float, short_in: float) -> None:
    """Effects clamp to what the machine can reach, so they follow its rectangle."""
    global _effect_ctx
    with _plot_lock:
        _effect_ctx = postprocess.Ctx(x_max=long_in, y_max=short_in)


def layout_msg() -> dict:
    long_in, short_in = travel()
    return {"type": "layout", "layout": _layout,
            "paper": {"width": PAPER_WIDTH_IN, "height": PAPER_HEIGHT_IN},
            "travel": {"long": long_in, "short": short_in},
            "canvas": {"width": state["canvasWidth"], "height": state["canvasHeight"]},
            "out_of_reach": layout.out_of_reach(_layout, state["canvasWidth"], state["canvasHeight"],
                                                long_in, short_in)}


def set_layout(new: dict) -> dict:
    """Place the rectangles (the Edit layout tool). Refused mid-plot."""
    global _layout
    if _plot_busy():
        return {"type": "error", "message": "Finish or cancel the plot before moving things."}
    long_in, short_in = travel()
    _layout = layout.normalize({**new, "auto": False}, PAPER_WIDTH_IN, PAPER_HEIGHT_IN,
                               state["canvasWidth"], state["canvasHeight"], long_in, short_in)
    log.info("[layout] drawing %.2f×%.2f\" at (%.2f, %.2f) turned %.0f°; machine at (%.2f, %.2f) turned %.0f°",
             _layout["ipad"]["w"], _layout["ipad"]["h"], _layout["ipad"]["cx"], _layout["ipad"]["cy"],
             _layout["ipad"]["rot"], _layout["axi"]["cx"], _layout["axi"]["cy"], _layout["axi"]["rot"])
    msg = layout_msg()
    preview.broadcast(msg)
    return msg


def canvas_to_physical(x: float, y: float) -> tuple[float, float, bool]:
    """
    A tablet point → the machine's own coordinates, from its home corner:
    x along the long axis, y along the short one, plus whether the machine can
    reach it at all. What it can't reach isn't drawn — a pen can't draw past
    the edge of what it can touch — and the third value says so.
    """
    global _out_of_reach_logged
    long_in, short_in = travel()
    mx, my, ok = layout.canvas_to_machine(x, y, _layout, state["canvasWidth"], state["canvasHeight"],
                                          long_in, short_in)
    if not ok and not _out_of_reach_logged:
        _out_of_reach_logged = True
        log.warning("[layout] part of the drawing is outside what the %s reaches — "
                    "that part isn't drawn. Move things in Edit layout.",
                    AXIDRAW_MODELS[_axidraw_model][0])
    return mx, my, ok


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

# What sits in the queue: the command itself, the preview message for the
# layer it belongs to (sent when the plotter *executes* it, so the pen path
# appears as the pen draws it), and, for an imported drawing, which of its
# points this came from (for the progress bar).
class Queued(typing.NamedTuple):
    cmd:   tuple
    layer: dict | None = None
    mark:  int | None = None


_plot_deque: collections.deque = collections.deque()
_plot_lock  = threading.Lock()

# Serializes pen-state mutations (_pen_is_down, _last_plot_pt, _pending_024,
# the rest state) between the OSC thread, the pen-rest watchdog thread and the
# import thread. Reentrant because the stroke functions call each other under it.
_pen_lock   = threading.RLock()

# Set on the thread feeding an imported drawing (and on the one re-feeding the
# strokes drawn while it ran): `importing` marks it as the import rather than
# the iPad, `mark` is the point it's on (the progress bar), and `quiet` stops a
# second broadcast of strokes the page already has. Thread-local, because the
# iPad's points arrive on another thread at the same time.
_feed_local = threading.local()


def _importing() -> bool:
    return getattr(_feed_local, "importing", False)

_ad               = None   # live AxiDraw handle; used by shutdown cleanup
_last_plot_pt:    tuple | None = None
_stroke_had_moves: bool        = False

_pending_024:               list  = []     # stroke commands buffered while pressure is spurious
_stroke_has_good_pressure:  bool  = False  # True once a non-spurious point is seen this stroke
_stroke_last_good_pressure: float = 0.0   # last non-spurious normalised pressure
_spurious_run:              int   = 0     # placeholder values in a row, this stroke

_heal_live:    bool = False   # join a torn line as it's drawn (see HEAL_LIVE_GAP_IN)
_stroke_end_items: list = []  # what _end_stroke queued, so healing can take it back
_last_stroke_end: tuple | None = None   # (px, py, monotonic, pressure) of the last stroke's end

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
_effect_ctx   = postprocess.Ctx(x_max=AXIDRAW_MODELS[1][1], y_max=AXIDRAW_MODELS[1][2])

# When True, the plotter lays down only what the effect chain *adds* — the base
# centerline is dropped. Lets a finished drawing be run again (imported) with just
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
        mark = getattr(_feed_local, "mark", None)
        queued = []
        for c in out:
            if _effects_only and c is cmd:
                continue
            name = "effect" if c is not cmd else "optimized"
            queued.append(Queued(c, _layer_msg(name, c), mark))
        _plot_deque.extend(queued)
        # Only now does cmd's position become "the previous one", so that an
        # effect handling cmd sees the point it is moving *from* in last_xy.
        xy = postprocess.xy_of(cmd)
        if xy is not None:
            _effect_ctx.last_xy = xy


def _layer_msg(layer: str, cmd: tuple) -> dict:
    """Build a preview 'layer' message for one plot command, in paper inches."""
    kind = postprocess.kind_of(cmd)
    msg = {"type": "layer", "layer": layer, "kind": kind}
    xy = postprocess.xy_of(cmd)
    if xy is not None:
        long_in, short_in = travel()
        px, py = layout.machine_to_paper(xy[0], xy[1], _layout, long_in, short_in)
        p = postprocess.pressure_of(cmd)
        msg.update({
            "x": px,
            "y": py,
            "pressure": p if p is not None else 0.0,
            "width": state["drawingWidth"] * layout.inches_per_canvas_unit(_layout, state["canvasWidth"]),
        })
    return msg


def _enqueue_raw(cmd: tuple) -> None:
    """Push a command straight to the plot deque, bypassing effects and the preview."""
    with _plot_lock:
        _plot_deque.append(Queued(cmd))

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

# How fast the carriage moves, in the AxiDraw's own units: speeds are 1–110,
# acceleration 1–100. These are the machine's stock values; the page can change
# them, and _speeds_request has the plotter thread push them to the EBB.
_speed_pendown: int = 25
_speed_penup:   int = 75
_accel:         int = 75

# After a pause, the pen waits this long between going down and moving again, so
# it is properly seated before the line continues. Nothing else dwells on pen
# down — a live stroke has to keep up with the hand drawing it.
RESUME_DWELL_SEC = 0.30

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
    long_in, short_in = travel()
    offset_x = math.tan(math.radians(_x_tilt_deg)) * (short_in / 2.0 - py)
    offset_y = math.tan(math.radians(_y_tilt_deg)) * (long_in / 2.0 - px)
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
#
# "motors" is separate from the connection: they can be released (so the
# carriage pushes by hand) while the AxiDraw stays connected. Moves are dropped
# while they're off; the pen still works, since that's a servo.
plotter_status = {"state": "not_found", "message": "", "motors": False}

_plotter_connect_request = threading.Event()   # set by connect_plotter(); served by the plotter thread
_motors_request: bool | None = None            # set by set_motors(); served by the plotter thread
_set_home_request = False                      # set by set_home(); ditto
_speeds_request   = False                      # a speed/accel setting changed; ditto
_plotter_stop            = threading.Event()   # set by shutdown(); the plotter thread exits
_plotter_thread_handle: threading.Thread | None = None


def _set_plotter_status(state: str, message: str = "", motors: bool | None = None) -> None:
    if motors is not None:
        plotter_status["motors"] = motors
    plotter_status.update(state=state, message=message)
    preview.broadcast({"type": "plotter_status", **plotter_status})
    level = logging.ERROR if state == "error" else logging.INFO
    log.log(level, "[axidraw] %s%s", state, f" — {message}" if message else "")


def connect_plotter() -> None:
    """Ask the plotter thread to (re)connect the AxiDraw. Safe from any thread."""
    _plotter_connect_request.set()


def set_motors(on: bool) -> None:
    """
    Release the XY motors so the carriage can be pushed by hand, or take hold
    again. The AxiDraw stays connected either way, and home doesn't move: if
    the carriage was pushed somewhere else, press Set as home.

    Releasing drops whatever is queued (and stops an import): those moves were
    planned from where the carriage used to be.
    """
    global _motors_request
    if not on:
        cancel_import()
        with _plot_lock:
            _plot_deque.clear()
    _motors_request = on


def _apply_speeds(ad) -> None:
    """Put the current speed and acceleration settings on the handle."""
    ad.options.speed_pendown = _speed_pendown
    ad.options.speed_penup   = _speed_penup
    ad.options.accel         = _accel


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
        _apply_speeds(ad)
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
    _set_plotter_status("connected", motors=True)   # connect() enables them
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
    global _ad, _current_lag_sec, _motors_request, _set_home_request, _speeds_request

    if DRY_RUN:
        ad = None
        _set_plotter_status("dry_run", "--dry-run: moves are logged, not plotted", motors=True)
    else:
        ad = _open_axidraw()
    _ad = ad

    # Per-stroke state for mid-stroke pressure updates (local to this thread)
    lineto_counter    = 0
    last_applied_down = None   # last pen_pos_down sent to EBB this stroke

    # Replay pause: the pen is lifted while paused, and lowered again only if
    # the next command draws (after a cancel it's a pen-up, so no dot is left).
    pen_down       = False
    paused_lifted  = False
    relower        = False

    def run(cmd):
        nonlocal lineto_counter, last_applied_down, pen_down, relower
        kind = cmd[1]
        if kind in ("moveto", "lineto", "home") and not plotter_status["motors"] and not DRY_RUN:
            return               # motors released: the carriage is the user's to push
        if relower:
            relower = False
            if kind == "lineto":
                log.debug("  pendown  (after pause)")
                if ad:
                    ad.pendown()
                    # Only here: a pause can leave the pen dry or lifted a hair,
                    # so give the servo time to seat before the line carries on.
                    time.sleep(RESUME_DWELL_SEC)
                pen_down = True
        pen_down = {"pendown": True, "pen_test_min": True, "pen_test_max": True,
                    "moveto": False, "penup": False, "home": False,
                    "pen_test_up": False}.get(kind, pen_down)

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
        if _motors_request is not None:
            want, _motors_request = _motors_request, None
            if ad:
                _set_motors(ad, want)
            pen_down = paused_lifted = relower = False
        if _speeds_request:
            _speeds_request = False
            if ad:
                _apply_speeds(ad)
                ad.update()
                log.info("[axidraw] speeds: pen down %d, pen up %d, accel %d",
                         _speed_pendown, _speed_penup, _accel)
        if _set_home_request:
            _set_home_request = False
            if ad:
                ad.pen.phys.xpos = ad.pen.phys.ypos = 0
                ad.pen.turtle.xpos = ad.pen.turtle.ypos = 0
            log.info("[axidraw] home set to where the carriage is")
        if _plotter_connect_request.is_set():
            _plotter_connect_request.clear()
            if not DRY_RUN:
                if ad:
                    _close_axidraw(ad)
                ad = _open_axidraw()
                _ad = ad
            pen_down = paused_lifted = relower = False

        if _import_pause.is_set():
            if pen_down and not paused_lifted:
                log.debug("[axidraw] import paused — pen up")
                try:
                    if ad:
                        ad.penup()
                except Exception as e:   # noqa: BLE001 — handled on the next command
                    log.warning("[axidraw] couldn't lift the pen for the pause (%s)", e)
                paused_lifted = True
            time.sleep(0.05)
            continue
        if paused_lifted:
            paused_lifted = False
            relower = True

        item = None
        with _plot_lock:
            if _plot_deque:
                item = _plot_deque.popleft()

        if item is None:
            time.sleep(0.005)
            continue
        cmd = item.cmd

        # cmd = (enqueue_time, kind, *args)
        try:
            run(cmd)
            # The preview's pen-path and effect layers follow the pen, not the
            # queue: this is the moment the plotter actually drew it.
            if item.layer is not None:
                preview.broadcast(item.layer)
            if item.mark is not None:
                _import_state["plotted"] = item.mark
            _current_lag_sec = current_lag()
        except Exception as e:       # noqa: BLE001 — typically the USB cable was pulled
            # The rest of the queue was planned for a machine we no longer
            # control; drop it rather than replay it into an unknown position.
            with _plot_lock:
                _plot_deque.clear()
            if ad:
                _close_axidraw(ad)
            ad = _ad = None
            _set_plotter_status("error", f"lost the AxiDraw during '{cmd[1]}' ({e}); queue discarded",
                                motors=False)


# ── how far behind the plotter is ────────────────────────────────────────────
#
# "Behind by" is the gap between the moment a mark was drawn and the moment the
# plotter draws it — with the time nobody was drawing taken out. So it counts
# up while drawing runs ahead of the machine, counts down while the machine
# catches up (including between strokes), and reaches zero when it's done.
#
# That's measured on a clock that only runs while a pen is putting marks down:
# the pen clock. Each point notes the pen-clock reading at the moment it was
# drawn; the lag is the reading now minus the reading of the command the
# plotter is about to run.
PEN_IDLE_GAP_SEC = 0.25      # a longer gap between points is a pause, not drawing

_pen_clock = 0.0             # seconds of actual drawing since the app started
_pen_clock_wall = None       # time.monotonic() at the last point
_pen_clock_marks: collections.deque = collections.deque()   # (monotonic, pen clock)
_pen_clock_lock = threading.Lock()


def _pen_clock_tick(t: float) -> None:
    """Advance the pen clock to this point, and note where the clock stands."""
    global _pen_clock, _pen_clock_wall
    with _pen_clock_lock:
        if _pen_clock_wall is not None:
            gap = t - _pen_clock_wall
            if 0 < gap <= PEN_IDLE_GAP_SEC:
                _pen_clock += gap      # drawing; a longer gap is a pause, so it doesn't count
        _pen_clock_wall = t
        _pen_clock_marks.append((t, _pen_clock))


def _pen_clock_at(t: float) -> float:
    """What the pen clock read when the command queued at `t` was drawn."""
    with _pen_clock_lock:
        # Newest first: the marks are in order, and the plotter works from the
        # oldest, so the answer is usually near the front.
        while len(_pen_clock_marks) > 1 and _pen_clock_marks[1][0] <= t:
            _pen_clock_marks.popleft()          # done with: nothing queued is older
        return _pen_clock_marks[0][1] if _pen_clock_marks else _pen_clock


def current_lag() -> float:
    """Seconds of drawing the plotter still has to catch up on. 0 when it's caught up."""
    with _plot_lock:
        head = _plot_deque[0].cmd[0] if _plot_deque else None
    if head is None:
        return 0.0
    with _pen_clock_lock:
        now_clock = _pen_clock
    return round(max(0.0, now_clock - _pen_clock_at(head)), 2)


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
        if items[i].cmd[1] == "lineto":
            # Collect the full contiguous run of lineto commands
            run_start = i
            while i < len(items) and items[i].cmd[1] == "lineto":
                i += 1
            run = items[run_start:i]

            if len(run) >= 3:
                pts = np.array([[r.cmd[2], r.cmd[3]] for r in run])
                mask = _rdp_fn(pts, epsilon=epsilon, return_mask=True)
                surviving = [r for r, keep in zip(run, mask) if keep]
                removed += len(run) - len(surviving)
                result.extend(surviving)
            else:
                result.extend(run)
        else:
            result.append(items[i])
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
        if _import_pause.is_set():
            continue      # the queue is waiting on purpose, not falling behind

        # Always update lag — needed for the browser display and the adaptive
        # distance filter in _emit_point, regardless of opt_enabled.
        lag = _current_lag_sec = current_lag()

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


def _osc_age() -> float | None:
    """Seconds since the iPad last sent anything (None: nothing yet this run)."""
    return None if last_osc_time is None else round(time.time() - last_osc_time, 1)


def _lag_broadcast_thread():
    """Every 500 ms: plotter lag, and how long since the iPad last sent anything."""
    while True:
        time.sleep(0.5)
        preview.broadcast({"type": "lag", "seconds": round(_current_lag_sec, 2),
                           "osc_age": _osc_age()})


# ──────────────────────────────────────────────────────────────────────────────
# CLI FLAGS  (set by argparse in __main__ before the server starts)
# ──────────────────────────────────────────────────────────────────────────────

_show_raw_osc = False   # --raw-osc: print every OSC message verbatim
_osc_port_from_cli = False   # --osc-port given: it wins over the saved port
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

}

DRAWABLE_TOOLS = {"pen", "pencil", "marker", "monoline", "crayon", "fountainPen", "waterColor"}


# ──────────────────────────────────────────────────────────────────────────────
# SVG IMPORT
# ──────────────────────────────────────────────────────────────────────────────
#
# Every drawing Pantograph saves carries its raw input points in a <metadata>
# block. Importing one pushes those points back through _emit_point, exactly as
# if they had just arrived over OSC — so the layout,
# tilt, the effect chain and the optimizer all re-apply downstream. That is the
# point of importing at the input level: the same drawing can be plotted again
# with different post-processors switched on.

# Idle gaps longer than this are shortened on import, so a drawing with long
# pauses does not take its original wall-clock time to plot. Stroke boundaries
# come from the recording itself, so this only affects pacing (a gap above
# PEN_REST_SEC rests the pen mid-stroke, exactly as it would live).
IMPORT_MAX_GAP_SEC = 0.30

# One import at a time. Its points go through the live pipeline exactly as the
# iPad's do: they're drawn on the canvas, recorded into the drawing, and saved
# with it. Importing a drawing is meant to be indistinguishable from having
# drawn it yourself.
#
# The iPad keeps working while an import plots. While the import is *feeding*,
# both would write the same pen state and scramble each other into one spliced
# stroke, so a stroke drawn then is shown and recorded straight away and plotted
# once the feed is in (see _capture_live_point / _flush_live_strokes). The plot
# queue is in order, so it lands after the import — "queued", as any stroke
# would be.
#
# Feeding is quick; plotting is not. The import stays "active" for the minutes
# the machine spends catching up, and during that time nothing is feeding, so
# live strokes take the ordinary path and queue themselves behind the import.
# _import_feeding — not _import_state["active"] — is what holds them back.
_import_state = {"active": False, "paused": False, "plotted": 0, "total": 0,
                 "name": None, "phase": ""}
_import_lock    = threading.Lock()
_import_pause   = threading.Event()  # set while paused (the plotter thread checks it too)
_import_cancel  = threading.Event()
_import_feeding = threading.Event()  # set while the import is pushing points in

_live_pending: list = []             # strokes drawn while the import was feeding
_live_cur: dict | None = None        # the one being drawn right now
_live_lock = threading.Lock()


def _import_msg(**extra) -> dict:
    return {"type": "import_progress", **_import_state, **extra}


def start_import(rec: dict, name: str | None = None) -> dict | None:
    """Plot a recording into the live drawing. Returns an error for the page if one is running."""
    strokes = rec.get("strokes") or []
    total = sum(len(s.get("points") or []) for s in strokes)
    with _import_lock:
        if _import_state["active"]:
            return {"type": "error", "message": "A drawing is already being imported. Cancel it first."}
        if not total:
            return {"type": "error", "message": "That drawing has no strokes."}
        _import_cancel.clear()
        _import_pause.clear()
        _import_feeding.set()
        _import_state.update(active=True, paused=False, plotted=0, total=total,
                             name=name, phase="plotting")
    threading.Thread(target=_import_drawing, args=(_to_canvas_space(rec),),
                     name="import", daemon=True).start()
    return None


def pause_import(paused: bool) -> None:
    with _import_lock:
        if not _import_state["active"] or _import_state["paused"] == paused:
            return
        _import_state["paused"] = paused
    if paused:
        _import_pause.set()
        log.info("[import] paused")
    else:
        _import_pause.clear()
        log.info("[import] resumed")
    preview.broadcast(_import_msg())


def cancel_import() -> None:
    """Stop feeding, drop what's queued, lift the pen. The import thread finishes the job."""
    if _import_state["active"]:
        _import_cancel.set()
        # Empty the queue before un-pausing, or the plotter would draw what's
        # queued in the moment before the import thread gets to it.
        with _plot_lock:
            _plot_deque.clear()
        _import_pause.clear()


def _wait_if_paused() -> None:
    while _import_pause.is_set() and not _import_cancel.is_set():
        time.sleep(0.05)


# ── strokes drawn on the iPad while an import is feeding ────────────────────

def _capture_live_point(x, y, pressure_norm, now) -> None:
    """
    Show and record a live point now, and keep it to plot when the import has
    been fed in. Called from the OSC thread instead of the usual pen handling.
    """
    global _live_cur
    with _live_lock:
        if _live_cur is None:
            _live_cur = {"tool": state["tool"], "drawingWidth": state["drawingWidth"],
                         "color": {k: state[k] for k in ("r", "g", "b", "a")},
                         "canvasWidth": state["canvasWidth"], "canvasHeight": state["canvasHeight"],
                         "points": []}
        _live_cur["points"].append([now, x, y, state["pressure"]])
    preview.broadcast(_point_msg(x, y, pressure_norm, now))


def _end_live_capture() -> None:
    """The captured stroke ended (a state block arrived, or the import finished)."""
    global _live_cur
    with _live_lock:
        if _live_cur is None:
            return
        if _live_cur["points"]:
            _live_pending.append(_live_cur)
        _live_cur = None
    preview.broadcast({"type": "pen_up"})


def _flush_live_strokes() -> None:
    """
    Queue the strokes drawn while the import was feeding. They're already on the
    page and in the recording, so this only plots them — behind the import, in
    order, where they'd have landed if the machine were keeping up.

    Called when the feed is done, which is also when capture stops: the drain
    loops because the iPad is still live, and only once a round finds nothing
    waiting does it clear _import_feeding, so no stroke is left held back.
    """
    while True:
        _end_live_capture()          # a stroke still open has to be plotted too
        with _live_lock:
            strokes, _live_pending[:] = list(_live_pending), []
            if not strokes and _live_cur is None:
                _import_feeding.clear()
                return
        if not strokes:
            continue                 # one arrived as we looked; take it next round
        log.info("[import] plotting %d stroke(s) drawn while it ran", len(strokes))
        _feed_local.quiet = True     # the page and the recording already have them
        try:
            _feed_strokes(strokes)
        finally:
            _feed_local.quiet = False


# ── feeding a drawing through the live pipeline ─────────────────────────────

def _feed_strokes(strokes: list, marks: bool = False) -> str:
    """
    Push recorded strokes back through _emit_point, exactly as if they had just
    arrived over OSC — so the layout, tilt, the effect chain and the optimizer
    all apply. Returns "done" or "cancelled".
    """
    mark = 0
    for st in strokes:
        pts = st.get("points") or []
        if not pts:
            continue

        # Recompute whenever the recording's canvas differs from what is loaded —
        # and whenever no mapping exists yet, or every point would be dropped by
        # _emit_point's mapping guard.
        cw, ch = st.get("canvasWidth"), st.get("canvasHeight")
        if cw and ch and (cw != state["canvasWidth"] or ch != state["canvasHeight"]
                          ):
            state["canvasWidth"], state["canvasHeight"] = cw, ch
            _update_layout(refit=True)

        state["tool"]         = st.get("tool", "pen")
        state["drawingWidth"] = st.get("drawingWidth", 1.5)
        col = st.get("color") or {}
        state["r"] = col.get("r", 0.0)
        state["g"] = col.get("g", 0.0)
        state["b"] = col.get("b", 0.0)
        state["a"] = col.get("a", 1.0)

        prev_t = None
        for pt in pts:
            if marks:
                _wait_if_paused()
                if _import_cancel.is_set():
                    return "cancelled"
                mark += 1
                _feed_local.mark = mark
            t_pt, x, y, p_raw = pt[0], pt[1], pt[2], pt[3]
            if prev_t is not None:
                time.sleep(min(max(0.0, t_pt - prev_t), IMPORT_MAX_GAP_SEC))
            prev_t = t_pt
            state["x"]        = x
            state["y"]        = y
            state["pressure"] = p_raw
            _emit_point()

        # The recording already knows where each stroke ends — this stands in
        # for the state block iDraw would have sent before the next one.
        _end_stroke()
    _feed_local.mark = None
    return "done"


def _import_drawing(rec: dict) -> None:
    """Runs on its own thread: feed the drawing, then wait for the plotter to finish."""
    global _effect_chain, _effect_ctx
    _feed_local.importing = True
    outcome = "done"
    try:
        strokes = rec.get("strokes") or []
        log.info("[import] %d stroke(s), %d point(s) — starting",
                 len(strokes), _import_state["total"])
        # Rebuild the effects so an import never inherits state (or RNG
        # position) from whatever was drawn before it.
        with _plot_lock:
            _effect_chain = postprocess.build_chain(_EFFECT_SWITCHES, _effect_params)
            _effect_ctx   = postprocess.Ctx(x_max=travel()[0], y_max=travel()[1])
        _end_stroke()                # a live stroke left open would swallow the first one
        preview.broadcast(_import_msg())

        outcome = _feed_strokes(strokes, marks=True)
        if outcome == "cancelled":
            _end_stroke()
            with _plot_lock:
                _plot_deque.clear()
            _enqueue_raw(postprocess.penup(time.monotonic()))
            _rebuild_effect_chain()
        _flush_live_strokes()
        if outcome == "done":
            outcome = _wait_for_plotter()
    except Exception:                # noqa: BLE001 — never leave the import "running"
        log.exception("[import] failed")
        outcome = "failed"
    _feed_local.importing = False
    with _import_lock:
        _import_state.update(active=False, paused=False, phase="")
    _import_pause.clear()
    _import_cancel.clear()
    _import_feeding.clear()          # in case the flush never got to it
    log.info("[import] %s", outcome)
    preview.broadcast(_import_msg(outcome=outcome))


def _import_queued() -> bool:
    """Is any of the import still waiting in the queue? Only its commands carry a mark."""
    with _plot_lock:
        return any(q.mark is not None for q in _plot_deque)


def _wait_for_plotter() -> str:
    """
    Everything is fed; the import is still running until the plotter catches up.
    It waits on the import's own commands, not on the queue — strokes drawn while
    the machine catches up queue up behind it, and they aren't the import's to
    wait for.
    """
    last_report = time.monotonic()
    _import_state["phase"] = "finishing"
    preview.broadcast(_import_msg())
    while _import_queued():
        if _import_cancel.is_set():
            return "cancelled"
        time.sleep(0.1)
        if time.monotonic() - last_report > 0.4:
            last_report = time.monotonic()
            preview.broadcast(_import_msg())
    return "done"


# ──────────────────────────────────────────────────────────────────────────────
# STROKE BOUNDARIES
# ──────────────────────────────────────────────────────────────────────────────
#
# A stroke ends only when the next one's state block arrives (or, on import,
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
    global _stroke_end_items, _last_stroke_end
    # Held under _pen_lock so this never nulls _last_plot_pt in the middle of an
    # _emit_point read-modify-write on another thread (the crash during an import).
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
        if _last_plot_pt is not None:
            _last_stroke_end = (*_last_plot_pt, t, _stroke_last_good_pressure)
        with _plot_lock:
            before = len(_plot_deque)
        if not _stroke_had_moves and not rested_dwell:
            _enqueue(postprocess.dot_dwell(t))
        # Goes through the effects even when the pen is already resting — this is
        # the one penup per stroke they see, and what their stroke-end work hangs on.
        _enqueue(postprocess.penup(t))
        with _plot_lock:
            _stroke_end_items = list(_plot_deque)[before:]   # healing may take these back
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
    if _live_cur is not None:
        _end_live_capture()          # a stroke drawn while an import is feeding
    if state["_pen_is_down"]:
        _end_stroke()


def _heal_join(t: float, px: float, py: float) -> bool:
    """
    Carry on from the stroke that just ended, instead of lifting and travelling
    — for a line the pen tore into pieces. True if the lift was taken back,
    which only works while the plotter hasn't reached it (it runs behind, so
    usually it hasn't). Called with _pen_lock held.
    """
    global _stroke_end_items
    if not _heal_live or _last_stroke_end is None or not _stroke_end_items:
        return False
    lx, ly, ended_at, _ = _last_stroke_end
    if t - ended_at > HEAL_LIVE_TIME_SEC or math.hypot(px - lx, py - ly) > HEAL_LIVE_GAP_IN:
        return False
    items, _stroke_end_items = _stroke_end_items, []
    with _plot_lock:
        n = len(items)
        if len(_plot_deque) < n or not all(_plot_deque[i - n] is c for i, c in enumerate(items)):
            return False                      # already drawn: too late to join
        for _ in range(n):
            _plot_deque.pop()
    log.debug("[heal] joining a torn line")
    return True


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
        items = [Queued(c) for c in cmds]
        with _plot_lock:
            _plot_deque.extend(items)
        _rest_cmds = items
        log.debug("[pen rest — pause]")


def _resume_after_rest(t: float, pressure: float) -> None:
    """
    The stroke carried on after a rest. Take the lift back out of the queue if the
    plotter hasn't reached it yet — the usual case, since it runs behind — else
    lower the pen again where it left off. Called with _pen_lock held.
    """
    global _pen_rested, _rest_cmds, _rest_dwelled
    _pen_rested = False
    items, _rest_cmds = _rest_cmds, []
    if not items:
        return      # nothing was lifted (pen never down, or effects-only)
    with _plot_lock:
        n = len(items)
        if len(_plot_deque) >= n and all(_plot_deque[i - n] is c for i, c in enumerate(items)):
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

    now = time.time()
    t   = time.monotonic()
    pressure_norm = min(1.0, max(0.0, state["pressure"] / OSC_PRESSURE_MAX))
    _pen_clock_tick(t)

    # Onto the paper, then into the machine's reach — see layout.py. What the
    # machine can't reach isn't drawn at all: the stroke ends here and starts
    # again where the pen comes back within reach, exactly as it would if the
    # paper ran out.
    px, py, in_reach = canvas_to_physical(x, y)
    if not in_reach:
        if _live_cur is not None:
            _end_live_capture()
        elif state["_pen_is_down"]:
            _end_stroke()
        return

    # An import is feeding the plotter: show and record what's being drawn now,
    # and plot it once the feed is in (see _capture_live_point). Once it has been
    # fed — while the machine is still catching up — this stroke goes the
    # ordinary way and queues itself behind the import.
    if _import_feeding.is_set() and not _importing():
        _capture_live_point(x, y, pressure_norm, now)
        return

    # Whole pen-state read-modify-write runs under _pen_lock so the rest thread
    # cannot lift the pen partway through it.
    with _pen_lock:
        # Only the state block (via _end_stroke) ends a stroke, so any point that
        # arrives while one is open belongs to it, however long the gap before it.
        was_down = state["_pen_is_down"]
        state["_pen_is_down"]     = True
        state["_last_point_time"] = now

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

        joined = False
        if not was_down:
            # First point of a new stroke — reset filter state, queue travel, hold pen down
            _stroke_had_moves = False
            _stroke_has_good_pressure = False
            _stroke_last_good_pressure = 0.0
            _pen_rested   = False
            _rest_cmds    = []
            _rest_dwelled = False
            _pending_024.clear()
            joined = _heal_join(t, px, py)
            if joined:
                # The pen never left the paper: carry on from where it stopped.
                _last_plot_pt = _last_stroke_end[:2]
                _stroke_has_good_pressure  = True
                _stroke_last_good_pressure = _last_stroke_end[3]
            else:
                _last_plot_pt = (px, py)
                _enqueue(postprocess.moveto(t, px, py))
                if spurious:
                    _pending_024.append(("pendown", t, px, py))
                else:
                    _stroke_has_good_pressure = True
                    _stroke_last_good_pressure = pressure_norm
                    _enqueue(postprocess.pendown(t, pressure_norm, px, py))
        if was_down or joined:
            # Continuing after a pause: undo the rest before drawing on.
            if was_down and _pen_rested:
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

    # Send to preview — unless this is a stroke being re-fed for plotting, which
    # the page and the recording already have (see _flush_live_strokes).
    if not getattr(_feed_local, "quiet", False):
        preview.broadcast(_point_msg(x, y, pressure_norm, now))

    log.debug("[point] (%.1f, %.1f)  p=%.2f  tool=%s", x, y, pressure_norm, state["tool"])


def _point_msg(x: float, y: float, pressure_norm: float, now: float) -> dict:
    """
    One point, for the page and the recorder. `t` and `pressureRaw` are the
    verbatim input: raw is what the spurious-pressure logic keys on, and the
    normalised value can't be inverted back to it once it clamps at 1.0.
    """
    return {
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
    }


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
    """The tablet's canvas changed size: refit the drawing rectangle to the paper."""
    w = state["canvasWidth"]
    h = state["canvasHeight"]
    if w and h:
        _update_layout(refit=_layout.get("auto", True))
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
    """Control messages from the page: settings, plotter commands, imports, quit."""
    global _opt_enabled, _opt_scale, _lag_threshold_sec, _limit_lag, _min_dist_in, \
           _variable_pressure, _pen_pos_up, _pen_down_min, _pen_down_max, _pressure_update_rate, \
           _x_tilt_deg, _y_tilt_deg, _effects_only, OSC_PORT, _heal_live, \
           _speed_pendown, _speed_penup, _accel, _speeds_request
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
    elif t == "set_speed_pendown":
        _speed_pendown = max(1, min(110, int(round(float(msg.get("value", 25))))))
        _speeds_request = True
    elif t == "set_speed_penup":
        _speed_penup = max(1, min(110, int(round(float(msg.get("value", 75))))))
        _speeds_request = True
    elif t == "set_accel":
        _accel = max(1, min(100, int(round(float(msg.get("value", 75))))))
        _speeds_request = True
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
    elif t == "set_heal_live":
        _heal_live = bool(msg.get("enabled", False))
        log.info("[heal] joining torn lines as they're drawn: %s", "on" if _heal_live else "off")
    elif t == "set_effects_only":
        _effects_only = bool(msg.get("enabled", False))
        log.info("[effect] effects-only %s", "on" if _effects_only else "off")
    elif t == "import_drawing":
        return start_import(msg.get("recording") or {}, name=msg.get("name"))
    elif t == "import_opened":
        return _import_opened(msg)
    elif t == "import_pause":
        pause_import(True)
    elif t == "import_resume":
        pause_import(False)
    elif t == "import_cancel":
        cancel_import()
    elif t == "pen_test_up":
        _enqueue_raw((time.monotonic(), "pen_test_up"))
    elif t == "pen_test_min":
        _enqueue_raw((time.monotonic(), "pen_test_min"))
    elif t == "pen_test_max":
        _enqueue_raw((time.monotonic(), "pen_test_max"))
    elif t == "connect_plotter":
        connect_plotter()
    elif t == "set_motors":
        set_motors(bool(msg.get("on", True)))
    elif t == "set_home":
        set_home()
    elif t == "restart_osc":
        port = msg.get("port")
        restart_osc_listener(int(port) if port else None)
    elif t == "set_osc_port":
        # The saved port, applied at startup; --osc-port on the command line wins.
        if not _osc_port_from_cli and 1024 <= int(msg.get("port", 0)) <= 65535:
            OSC_PORT = int(msg["port"])
    elif t == "quit":
        request_stop()
    elif t == "set_layout":
        return set_layout(msg.get("layout") or {})
    elif t == "set_paper":
        return set_paper(float(msg.get("width", 8.5)), float(msg.get("height", 11.0)))
    elif t == "set_model":
        return set_model(int(msg.get("model", 1)))
    elif t == "save_settings":
        if _settings is not None:
            _settings.replace(msg.get("values") or {})
    elif t == "new_drawing":
        new_drawing()
    elif t == "discard_drawing":
        discard_drawing()
    elif t == "open_path":
        return open_path(msg.get("path") or "")
    elif t in _LIBRARY_MESSAGES:
        return _LIBRARY_MESSAGES[t](msg)
    elif t == "hello_again":
        # The page needs the drawing again (its canvas was resized for new paper).
        return _hello()
    elif t == "diagnostics":
        return {"type": "diagnostics", "text": diagnostics()}


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
            "travel": [long_in, short_in]}


def set_paper(width: float, height: float) -> dict:
    """
    Set the sheet's size (inches, either orientation — it's used portrait).
    The machine's reach no longer limits it: where the machine sits on the
    sheet, and how much of it that covers, is the layout's business. Refused
    mid-plot, since the queue was planned for the old sheet.
    """
    global PAPER_WIDTH_IN, PAPER_HEIGHT_IN, _paper_requested
    if _plot_busy():
        return {"type": "error", "message": "Finish or cancel the plot before changing the paper."}
    w, h = sorted((abs(width), abs(height)))
    if w <= 0:
        return {"type": "error", "message": "The paper needs a width and a height."}
    _paper_requested = (w, h)
    PAPER_WIDTH_IN, PAPER_HEIGHT_IN = w, h
    _update_layout(refit=_layout.get("auto", True))
    log.info("[paper] %.2f\" × %.2f\"", PAPER_WIDTH_IN, PAPER_HEIGHT_IN)
    info = paper_info()
    preview.broadcast(info)
    return info


def set_model(model: int) -> dict:
    """Set the AxiDraw model — which is how big its rectangle is on the paper."""
    global _axidraw_model
    if model not in AXIDRAW_MODELS:
        return {"type": "error", "message": f"Unknown AxiDraw model {model}."}
    if _plot_busy():
        return {"type": "error", "message": "Finish or cancel the plot before changing the model."}
    changed = model != _axidraw_model
    _axidraw_model = model
    log.info("[axidraw] model %d: %s", model, AXIDRAW_MODELS[model][0])
    _update_layout()
    if changed and plotter_status["state"] == "connected":
        connect_plotter()        # reconnect so pyaxidraw applies the model's limits
    info = paper_info()
    preview.broadcast(info)
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
        recording.write_atomic(_drawings_dir / AUTOSAVE_NAME, _paper_space_svg())


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
    recording.write_atomic(path, _paper_space_svg())
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


SVG_PX_PER_IN = 96          # the unit a saved file is drawn in


def _paper_space_svg() -> str:
    """
    The drawing as it sits on the paper — which is what was plotted, and so
    what gets saved. Points move from the tablet's coordinates onto the sheet
    through the layout.

    A saved file is the drawing and nothing else. The pen path and the effect
    marks are the machine's account of a particular run: the path is where the
    carriage went after simplifying, and the effects are re-applied live from
    whatever is switched on now. Neither is something you drew, so neither is
    saved — the same reason a plotted sheet carries ink, not a plan of the ink.
    """
    k = SVG_PX_PER_IN
    rec = _recorder.recording()
    per_unit = layout.inches_per_canvas_unit(_layout, state["canvasWidth"])
    for st in rec["strokes"]:
        cw = st.get("canvasWidth") or state["canvasWidth"]
        ch = st.get("canvasHeight") or state["canvasHeight"]
        for p in st["points"]:
            px, py = layout.canvas_to_paper(p[1], p[2], _layout, cw, ch)
            p[1], p[2] = px * k, py * k
        st["drawingWidth"] = (st.get("drawingWidth") or 1.5) * per_unit * k
        st["canvasWidth"], st["canvasHeight"] = PAPER_WIDTH_IN * k, PAPER_HEIGHT_IN * k
    rec["space"] = "paper"
    rec["paperIn"] = [PAPER_WIDTH_IN, PAPER_HEIGHT_IN]
    return recording.build_svg(rec, PAPER_WIDTH_IN * k, PAPER_HEIGHT_IN * k)


def _to_canvas_space(rec: dict) -> dict:
    """
    A saved file's points are places on the paper; the pipeline speaks the
    tablet's coordinates. Convert, so the ink lands exactly where it did when
    the file was written, whatever the layout is now.
    """
    if rec.get("space") != "paper":
        return rec
    k = SVG_PX_PER_IN
    cw, ch = state["canvasWidth"], state["canvasHeight"]
    per_unit = layout.inches_per_canvas_unit(_layout, cw) or 1.0
    out = json.loads(json.dumps(rec))
    for st in out.get("strokes") or []:
        for p in st.get("points") or []:
            x, y = layout.paper_to_canvas(p[1] / k, p[2] / k, _layout, cw, ch)
            p[1], p[2] = x, y
        st["drawingWidth"] = (st.get("drawingWidth") or 1.5) / k / per_unit
        st["canvasWidth"], st["canvasHeight"] = cw, ch
    out.pop("space", None)
    return out


def discard_drawing() -> None:
    """Throw the current drawing away (the page asks first) and start a fresh one."""
    _end_stroke()
    _recorder.clear()
    if _drawings_dir is not None:
        (_drawings_dir / AUTOSAVE_NAME).unlink(missing_ok=True)
    log.info("[drawing] discarded")
    preview.broadcast({"type": "new_drawing", "saved": None})


# ── opening a drawing, and the tools ───────────────────────────────────────
#
# A drawing opened from disk isn't a second canvas: it's shown in a preview
# panel, and "Import to live drawing" feeds it through the live pipeline so it
# becomes part of the one drawing (see SVG IMPORT). Tools work on the live
# drawing and leave their result in the same preview panel.
#
# The Open and Save windows are the system's own, which means they have to run
# on the main thread (macOS); main() serves them from there (run_on_main).

_worker = None
_opened: dict | None = None         # the drawing in the preview panel
_main_calls: queue.Queue = queue.Queue()


def run_on_main(fn):
    """Run fn() on the main thread; returns a Future with its result."""
    future: concurrent.futures.Future = concurrent.futures.Future()
    _main_calls.put((fn, future))
    return future


def serve_main_thread_calls() -> None:
    """Called by main()'s loop: run anything waiting for the main thread."""
    while True:
        try:
            fn, future = _main_calls.get_nowait()
        except queue.Empty:
            return
        try:
            future.set_result(fn())
        except Exception as e:       # noqa: BLE001 — reported to the page
            future.set_exception(e)


def _in_background(fn):
    global _worker
    if _worker is None:
        _worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tools")
    return _worker.submit(fn)


def _opened_msg(**extra) -> dict:
    if _opened is None:
        return {"type": "opened", "ok": False, "error": "nothing open", **extra}
    return {"type": "opened", "ok": True, "name": _opened["name"], "dir": _opened["dir"],
            "width": _opened["vw"], "height": _opened["vh"],
            "strokes": len(_opened["rec"].get("strokes") or []),
            "points": sum(len(st.get("points") or []) for st in _opened["rec"].get("strokes") or []),
            **extra}


def open_path(path) -> dict:
    """Load a drawing into the preview panel. Returns the page's reply."""
    global _opened
    path = Path(path)
    try:
        rec, vw, vh = recording.load_svg(path)
    except (ValueError, OSError) as e:
        return {"type": "opened", "ok": False, "error": str(e)}
    if not (rec.get("strokes") or []):
        return {"type": "opened", "ok": False, "error": f"{path.name} has no strokes."}
    _opened = {"name": path.name, "dir": str(path.parent), "rec": rec, "vw": vw, "vh": vh}
    log.info("[open] %s", path)
    return _opened_msg()


def _open_file(msg: dict):
    """Open window → the chosen drawing goes into the preview panel."""
    start_dir = Path(msg["dir"]) if msg.get("dir") else _drawings_dir

    def pick():
        from PantographApp import dialogs
        chosen = dialogs.ask_open(start_dir)
        if not chosen:
            return {"type": "opened", "ok": False, "cancelled": True}
        return open_path(chosen)

    return run_on_main(pick)


def _import_opened(msg: dict | None = None) -> dict | None:
    """Plot the previewed drawing into the live drawing."""
    if _opened is None:
        return {"type": "error", "message": "No drawing is open."}
    return start_import(_opened["rec"], name=_opened["name"])


def _save_as(msg: dict):
    """Save window → write the drawing there. Always the drawing alone."""
    start_dir = Path(msg["dir"]) if msg.get("dir") else _drawings_dir
    suggested = recording.timestamped_name()
    if _recorder.is_empty():
        return {"type": "saved", "ok": False, "error": "There's no drawing to save yet."}

    def write(chosen):
        try:
            svg = _paper_space_svg()
            recording.write_atomic(Path(chosen), svg)
        except OSError as e:
            return {"type": "saved", "ok": False, "error": str(e)}
        log.info("[drawing] saved  →  %s", chosen)
        return {"type": "saved", "ok": True, "path": Path(chosen).name,
                "dir": str(Path(chosen).parent)}

    if msg.get("path"):              # no window: for --open's sibling, and the tests
        return write(str(msg["path"]))

    def pick():
        from PantographApp import dialogs
        chosen = dialogs.ask_save(start_dir, suggested)
        return write(chosen) if chosen else {"type": "saved", "ok": False, "cancelled": True}

    return run_on_main(pick)


_LIBRARY_MESSAGES = {
    "open_file":     _open_file,
    "import_opened": _import_opened,
    "save_as":       _save_as,
}


def _serve_opened(_rest: str):
    """GET /opened.svg: the drawing in the preview panel, drawn from its recording."""
    if _opened is None:
        return None
    svg = recording.build_svg(_opened["rec"], _opened["vw"], _opened["vh"])
    return svg.encode(), "image/svg+xml", [("Content-Security-Policy", "sandbox")]


# ── what the page is told ────────────────────────────────────────────────────

_log_file = None           # set by main(); the diagnostics include its last lines
_ips_cache = (0.0, [])


def _ips() -> list:
    """This computer's addresses for iDraw, looked up at most every 10 s."""
    global _ips_cache
    from PantographApp import netinfo
    if time.monotonic() - _ips_cache[0] > 10:
        try:
            _ips_cache = (time.monotonic(), netinfo.local_ips())
        except Exception:                # noqa: BLE001 — the page shows "none found"
            log.exception("[net] couldn't list this computer's addresses")
            _ips_cache = (time.monotonic(), [])
    return _ips_cache[1]


def diagnostics() -> str:
    """A block of text for "Copy diagnostics": enough to answer "why isn't it working?"."""
    import platform
    ips = ", ".join(f"{i['ip']} ({i['label']})" for i in _ips()) or "none found"
    heard = ("nothing received yet" if last_osc_time is None
             else f"last message {_osc_age()} s ago")
    lines = [
        f"Pantograph {APP_VERSION}",
        f"OS: {platform.platform()}   Python {platform.python_version()}",
        f"UI: http://127.0.0.1:{preview.port}",
        f"IPs: {ips}",
        f"iPad (OSC): {osc_status['state']} on port {osc_status['port']}"
        + (f" ({osc_status['message']})" if osc_status["message"] else "") + f"; {heard}",
        f"Plotter: {plotter_status['state']}"
        + (f" ({plotter_status['message']})" if plotter_status["message"] else "")
        + f"; motors {'on' if plotter_status['motors'] else 'off'}",
        f"AxiDraw model {_axidraw_model} ({AXIDRAW_MODELS[_axidraw_model][0]}); "
        f"paper {PAPER_WIDTH_IN}\" x {PAPER_HEIGHT_IN}\"",
        f"Import: {'running' if _import_state['active'] else 'none'}; "
        f"behind by {_current_lag_sec:.1f} s",
    ]
    if _log_file is not None:
        try:
            tail = _log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            lines += ["", "Recent log:", *tail]
        except OSError:
            pass
    return "\n".join(lines)


def _hello() -> dict:
    """Everything a page needs when it connects: settings, the drawing so far, statuses."""
    hello = {
        "type":           "hello",
        "version":        APP_VERSION,
        "settings":       dict(_settings.values) if _settings is not None else {},
        "plotter_status": dict(plotter_status),
        "osc_status":     dict(osc_status),
        "osc_age":        _osc_age(),
        "ips":            _ips(),
        "paper":          paper_info(),
        "layout":         layout_msg(),
        "models":         {str(k): v[0] for k, v in AXIDRAW_MODELS.items()},
        "lag":            round(_current_lag_sec, 2),
        "import":         _import_msg(),
        "effect_specs":   postprocess.effect_specs(),
        "drawings_dir":   str(_drawings_dir or ""),
        "opened":         _opened_msg() if _opened is not None else None,
    }
    # Last, and quick: preview adds the page to its broadcast list right after
    # this returns, so anything drawn in between would reach neither.
    hello["drawing"] = _recorder.messages()
    return hello


def _apply_setting(msg: dict) -> None:
    """Put one saved setting into effect, exactly as if the page had sent it."""
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

    # A layout from the defaults; the saved one (if any) arrives with the
    # settings just below, and iDraw's real canvas size refits it.
    _update_layout(refit=True)

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
    preview.register_route("/opened.svg", _serve_opened)

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
        _import_cancel.set()
        _import_pause.clear()
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


def set_home() -> None:
    """Take the carriage's current spot as home (0, 0). Served by the plotter thread."""
    global _set_home_request
    _set_home_request = True


def _set_motors(ad, on: bool) -> None:
    """Take hold of the carriage, or let go of it. Runs on the plotter thread."""
    from plotink import ebb_motion
    try:
        if on:
            ad.enable_motors()       # resolution and speeds, then the EBB command
            _set_plotter_status("connected", "", motors=True)
            log.info("[axidraw] motors on")
        else:
            ad.penup()
            ad.block()               # let the lift finish before cutting the motors
            ebb_motion.sendDisableMotors(ad.plot_status.port, False)
            _set_plotter_status("connected", "", motors=False)
            log.info("[axidraw] motors off — the carriage can be pushed by hand")
    except Exception as e:           # noqa: BLE001 — the USB link is the usual reason
        _set_plotter_status("error", f"could not {'engage' if on else 'release'} the motors ({e})")


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
    global _show_raw_osc, DRY_RUN, OSC_PORT, _osc_port_from_cli

    # Before the parser: --help prints "→" and would otherwise crash on a fresh
    # Windows console, which is exactly the console a first-time user has.
    from PantographApp import shell
    shell.use_utf8_console()

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
    parser.add_argument("--open", metavar="FILE",
                        help="Open this drawing in the UI (it's copied into the drawings "
                             "folder if it isn't there already).")
    cli = parser.parse_args(argv)
    _show_raw_osc = cli.raw_osc
    DRY_RUN       = cli.dry_run
    if cli.osc_port:
        OSC_PORT = cli.osc_port
        _osc_port_from_cli = True

    global _log_file
    paths = shell.resolve_paths(cli.data_dir)
    log_file = _log_file = shell.setup_logging(paths.logs, verbose=cli.verbose)

    # Already running (say, the launcher was double-clicked twice)? Show that
    # copy instead of fighting it for the ports.
    existing = shell.running_instance(paths.runtime)
    if existing:
        log.info("Pantograph is already running at %s — opening it.", existing)
        if cli.open:
            reply = shell.ask_running(existing, {"type": "open_path",
                                                 "path": str(Path(cli.open).resolve())}, "opened")
            if not reply or not reply.get("ok"):
                log.error("Couldn't open %s: %s", cli.open,
                          (reply or {}).get("error", "the running copy didn't answer"))
                return 1
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
    if cli.open:
        reply = open_path(Path(cli.open).resolve())
        if not reply.get("ok"):
            log.error("Couldn't open %s: %s", cli.open, reply.get("error"))
    shell.open_ui(url, enabled=not cli.no_browser)
    log.info("[ui] %s", url)

    try:
        # A timed wait, not a bare wait(): Ctrl+C only interrupts the main
        # thread between waits on Windows.
        while not _stop_event.wait(0.1):
            serve_main_thread_calls()      # the Open and Save windows run here
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
