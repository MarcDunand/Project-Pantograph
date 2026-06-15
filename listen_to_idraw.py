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

import preview

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these values to match your setup
# ──────────────────────────────────────────────────────────────────────────────

OSC_PORT = 8800

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

_ad               = None   # live AxiDraw handle; used by shutdown cleanup
_last_plot_pt:    tuple | None = None
_stroke_had_moves: bool        = False

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
        ad.options.speed_pendown = 50
        ad.options.speed_penup = 105
        ad.options.accel = 90
        ad.options.pen_rate_lower = 90
        ad.options.pen_rate_raise = 90
        try:
            ad.connect()
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
                ad.penup()
                ad.moveto(x, y)
            elif not _show_raw_osc:
                print(f"  penup")
                print(f"  moveto  ({x:.3f}\", {y:.3f}\")")

        elif kind == "pendown":
            if ad:
                ad.pendown()
            elif not _show_raw_osc:
                print(f"  pendown")

        elif kind == "lineto":
            x, y = cmd[2], cmd[3]
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
                ad.penup()
            elif not _show_raw_osc:
                print(f"  penup")

        elif kind == "home":
            if not _show_raw_osc:
                print(f"[axidraw] homing → (0.000\", 0.000\")")
            if ad:
                ad.penup()
                ad.moveto(0, 0)
            elif not _show_raw_osc:
                print(f"  penup")
                print(f"  moveto  (0.000\", 0.000\")")


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
# OSC HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def _maybe_pen_up():
    """Fire a pen-up event if the point stream has gone quiet."""
    global _last_plot_pt, _stroke_had_moves
    last = state["_last_point_time"]
    if last is None:
        return
    if state["_pen_is_down"] and (time.time() - last) > PEN_UP_TIMEOUT_SEC:
        state["_pen_is_down"] = False
        preview.broadcast({"type": "pen_up"})
        t = time.monotonic()
        with _plot_lock:
            if not _stroke_had_moves:
                _plot_deque.append((t, "dot_dwell"))
            _plot_deque.append((t, "penup"))
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


def _emit_point():
    """
    Fires on every /y message (the last field in each OSC burst).
    Streams plot commands to the deque in real time and broadcasts to the preview.

    Two optimisation layers fire here:
      1. Adaptive distance filter — skips points too close to the last enqueued
         point; threshold grows with lag when optimisation is enabled.
      2. RDP on the deque backlog — handled in _optimizer_thread.
    """
    global _last_plot_pt, _stroke_had_moves

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
    _maybe_pen_up()

    was_down = state["_pen_is_down"]
    state["_pen_is_down"]     = True
    state["_last_point_time"] = now

    # Convert to portrait paper coords, then rotate 90° CW for landscape AxiDraw.
    # After rotation: px ∈ [0, PAPER_HEIGHT_IN], py ∈ [0, PAPER_WIDTH_IN].
    px, py = canvas_to_paper(x, y, mapping)
    px, py = py, PAPER_WIDTH_IN - px

    # Axis flips applied in landscape space
    if preview.flip_x:
        px = PAPER_HEIGHT_IN - px
    if preview.flip_y:
        py = PAPER_WIDTH_IN - py

    if not was_down:
        # First point of a new stroke — travel to start position then lower pen
        _stroke_had_moves = False
        _last_plot_pt = (px, py)
        with _plot_lock:
            _plot_deque.append((t, "moveto", px, py))
            _plot_deque.append((t, "pendown"))
    else:
        # Continuation — apply adaptive distance filter before enqueuing
        lx, ly = _last_plot_pt
        eff = _compute_effective_scale(_current_lag_sec)
        effective_min_dist = _min_dist_in * (1.0 + eff * 4.0)

        if math.hypot(px - lx, py - ly) >= effective_min_dist:
            with _plot_lock:
                _plot_deque.append((t, "lineto", px, py))
            _last_plot_pt = (px, py)
            _stroke_had_moves = True

    # Send to preview
    preview.broadcast({
        "type":         "point",
        "x":            x,
        "y":            y,
        "pressure":     state["pressure"],
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
            f"p={state['pressure']:.2f}  "
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
        global _opt_enabled, _opt_scale, _lag_threshold_sec, _limit_lag, _min_dist_in
        t = msg.get("type")
        if t == "home":
            with _plot_lock:
                _plot_deque.append((time.monotonic(), "home"))
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
