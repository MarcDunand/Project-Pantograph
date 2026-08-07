"""
listen_to_idraw_remote.py

REMOTE (no-AxiDraw) version of the draw2axi pipeline.

Purpose
───────
Draw with iDraw OSC and record the drawing to a plottable SVG that can be saved
now and plotted asynchronously later, on the fully-tooled version. There is no
AxiDraw here: no plotter thread, no path optimizer, no post-processing effects,
no paper coordinate mapping, no pen/tilt/flip controls. The one job this program
keeps is producing a *correctly-formatted* recording SVG.

Pipeline:
  1. Receive raw OSC scalar messages from iDraw OSC
  2. Reconstruct drawing points and infer stroke boundaries (pen-up on quiet)
  3. Broadcast raw points to the browser preview, which draws them and records
     the raw OSC input verbatim
  4. The browser downloads an SVG whose <metadata> carries that recording

Usage:
  python listen_to_idraw_remote.py

In iDraw OSC (iPad):
  Set IP   -> your computer's local Wi-Fi IP  (e.g. 192.168.1.57)
  Set Port -> 8800

Dependencies:
  pip install python-osc websockets
  (No numpy / rdp / pyaxidraw — this version never talks to a plotter.)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SHARED RECORDING CONTRACT — KEEP IN SYNC WITH THE FULL VERSION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The SVG this program produces must stay byte-compatible with what the full tool
(listen_to_idraw.py + preview.py) reads back for replay/plotting. The contract
is exactly two things:

  1. The `point` broadcast message shape (see _emit_point below) — the browser
     records `t, x, y, pressureRaw` per point plus per-stroke tool / drawingWidth
     / color / canvasWidth / canvasHeight.
  2. The `draw2axi-recording` v1 JSON embedded in <metadata> (see preview_remote's
     buildRecording) and the surrounding SVG structure.

If you change how the recording or its interpretation works in EITHER program —
the OSC point fields, the pressure encoding, PEN_UP_TIMEOUT_SEC stroke
segmentation, or the recording JSON schema — you MUST update BOTH this remote
version and the full version (listen_to_idraw.py / preview.py / log_to_svg.py /
svg_transform.py), or SVGs recorded here will misplot there.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import threading
import time

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

import preview_remote as preview

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

OSC_PORT = 8800

# Maximum pressure value reported by iDraw OSC (Apple Pencil at full press).
# Finger input always reports 1.0. The browser records the *raw* value; this
# constant is only used to display a normalised 0–1 pressure in the preview.
# MUST match OSC_PRESSURE_MAX in the full version's listen_to_idraw.py.
OSC_PRESSURE_MAX = 4.166666507720947

# How long a gap between received points (in seconds) counts as a pen lift.
# This is the stroke-segmentation boundary and is part of the recording contract:
# it decides where one stroke ends and the next begins. MUST match the full
# version's PEN_UP_TIMEOUT_SEC.
PEN_UP_TIMEOUT_SEC = 0.15

# Tools whose points are drawn/recorded. Erasers and non-drawing tools are ignored.
# MUST match DRAWABLE_TOOLS in the full version.
DRAWABLE_TOOLS = {"pen", "pencil", "marker", "monoline", "crayon", "fountainPen", "waterColor"}


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

# Serializes pen-state reads/writes between the OSC handler threads (python-osc
# dispatches each datagram on its own thread) and the pen-up watchdog thread.
# Reentrant because _emit_point holds it while calling _maybe_pen_up.
_pen_lock = threading.RLock()

_show_raw_osc = False   # --raw-osc: print every OSC message verbatim


# ──────────────────────────────────────────────────────────────────────────────
# STROKE SEGMENTATION + PREVIEW BROADCAST
# ──────────────────────────────────────────────────────────────────────────────

def _maybe_pen_up():
    """Fire a pen-up event (ends the current stroke) if the point stream is quiet."""
    with _pen_lock:
        last = state["_last_point_time"]
        if last is None:
            return
        if state["_pen_is_down"] and (time.time() - last) > PEN_UP_TIMEOUT_SEC:
            state["_pen_is_down"] = False
            preview.broadcast({"type": "pen_up"})
            if not _show_raw_osc:
                print("[pen up — timeout]")


def _pen_watchdog_thread():
    """
    Polls for a quiet point stream and fires pen-up when the timeout elapses.
    Ensures the final stroke of a session is finalized even when the user simply
    stops drawing and never starts another stroke.
    """
    while True:
        time.sleep(PEN_UP_TIMEOUT_SEC / 2)
        _maybe_pen_up()


def _emit_point():
    """
    Fires on every /y message (the last field in each OSC burst).

    Broadcasts the raw point to the browser preview, which both draws it and
    records it for the SVG. `t` and `pressureRaw` are what the browser writes to
    the recording verbatim — raw pressure cannot be reconstructed from the
    normalised value once it clamps, so both are sent.

    This message shape is half of the recording contract (see the header block).
    """
    x = state["x"]
    y = state["y"]
    if x is None or y is None:
        return
    if state["tool"] not in DRAWABLE_TOOLS:
        return

    now = time.time()

    with _pen_lock:
        _maybe_pen_up()
        state["_pen_is_down"]     = True
        state["_last_point_time"] = now

    pressure_norm = min(1.0, max(0.0, state["pressure"] / OSC_PRESSURE_MAX))

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


# ──────────────────────────────────────────────────────────────────────────────
# OSC HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def _log_raw(address, *args):
    val = args[0] if len(args) == 1 else list(args)
    print(f"[osc]  {address:<20}  {val}")


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
    _broadcast_canvas_size()

def _handle_canvas_height(address, *args):
    if _show_raw_osc: _log_raw(address, *args)
    state["canvasHeight"] = args[0]
    _broadcast_canvas_size()

def _broadcast_canvas_size():
    w = state["canvasWidth"]
    h = state["canvasHeight"]
    if w and h:
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
        prog="python listen_to_idraw_remote.py",
        description="iDraw OSC → Python → recording SVG (no AxiDraw).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python listen_to_idraw_remote.py            # record drawings to SVG\n"
            "  python listen_to_idraw_remote.py --raw-osc  # show raw OSC stream"
        ),
    )
    parser.add_argument(
        "--raw-osc",
        action="store_true",
        help=(
            "Print every incoming OSC message verbatim (address + value) "
            "instead of the normal pipeline output."
        ),
    )
    cli = parser.parse_args()
    _show_raw_osc = cli.raw_osc

    print("=" * 60)
    print("  iDraw OSC -> Python -> recording SVG   (REMOTE / no AxiDraw)")
    print(f"  OSC port     : {OSC_PORT}")
    print(f"  Output mode  : {'RAW OSC' if _show_raw_osc else 'pipeline'}")
    print("=" * 60)
    print()
    print("  In iDraw OSC (iPad):")
    print("    IP   -> your computer's local Wi-Fi IP")
    print(f"   Port -> {OSC_PORT}")
    print()
    print("  Draw, then use the preview's 'download' button to save an SVG.")
    print("  Transfer that SVG to the AxiDraw machine and load it there with")
    print("  the full tool's 'plot svg' button to plot it.")
    print()

    # Start preview server (opens browser)
    preview.start(open_browser=True)

    # Watchdog: fires pen-up when the point stream goes quiet
    threading.Thread(target=_pen_watchdog_thread, daemon=True).start()

    # Start OSC listener — blocks until Ctrl+C
    dispatcher = _build_dispatcher()
    server = ThreadingOSCUDPServer(("0.0.0.0", OSC_PORT), dispatcher)
    print(f"[osc] Listening on 0.0.0.0:{OSC_PORT} ...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[shutdown] bye")
