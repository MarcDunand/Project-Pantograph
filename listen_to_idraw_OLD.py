"""
listen_to_idraw.py

Receives live OSC messages from iDraw OSC and:
  1. Maintains a running state (current tool, color, canvas size, brush width)
  2. Reconstructs drawing points from the individual scalar messages
  3. Infers stroke boundaries using time gaps
  4. Broadcasts each point and pen-up event to preview.py for live display

Usage:
  python listen_to_idraw.py

In iDraw OSC (iPad):
  Set IP   -> your computer's local Wi-Fi IP  (e.g. 192.168.1.57)
  Set Port -> 8800

Dependencies:
  pip install python-osc websockets
"""

import time
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

import preview

# ─── configuration ────────────────────────────────────────────────────────────

OSC_PORT            = 8800
PEN_UP_TIMEOUT_SEC  = 0.15   # gap longer than this → treat as pen lift

# ─── drawing state ────────────────────────────────────────────────────────────

state = {
    # position
    "x":            None,
    "y":            None,
    "pressure":     1.0,

    # color
    "r":            0.0,
    "g":            0.0,
    "b":            0.0,
    "a":            1.0,

    # active tool (string name, resolved from one-hot flags)
    "tool":         "pen",

    # canvas / brush
    "canvasWidth":  440.0,
    "canvasHeight": 956.0,
    "drawingWidth": 1.5,
    "eraserWidth":  0.0,

    # internal
    "_last_point_time": None,
    "_pen_is_down":     False,
}

# Tools that produce a physical mark
DRAWABLE_TOOLS = {"pen", "pencil", "marker", "monoline", "crayon", "fountainPen", "waterColor"}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _maybe_pen_up():
    """Signal a pen lift if no point has arrived within the timeout window."""
    last = state["_last_point_time"]
    if last is None:
        return
    if state["_pen_is_down"] and (time.time() - last) > PEN_UP_TIMEOUT_SEC:
        state["_pen_is_down"] = False
        preview.broadcast({"type": "pen_up"})
        print("[pen up  — timeout]")


def _emit_point():
    """
    Package the current state into a point dict and send it to the preview.
    Fires whenever /y arrives (last field in each point burst).
    """
    x = state["x"]
    y = state["y"]
    if x is None or y is None:
        return

    tool = state["tool"]
    if tool not in DRAWABLE_TOOLS:
        return   # eraser — skip for now

    now = time.time()
    _maybe_pen_up()

    state["_pen_is_down"]     = True
    state["_last_point_time"] = now

    point = {
        "type":         "point",
        "x":            x,
        "y":            y,
        "pressure":     state["pressure"],
        "r":            state["r"],
        "g":            state["g"],
        "b":            state["b"],
        "a":            state["a"],
        "tool":         tool,
        "drawingWidth": state["drawingWidth"],
        "canvasWidth":  state["canvasWidth"],
        "canvasHeight": state["canvasHeight"],
    }

    preview.broadcast(point)

    print(
        f"[point] ({x:.1f}, {y:.1f})  "
        f"p={state['pressure']:.2f}  "
        f"tool={tool}  "
        f"rgb=({state['r']:.2f},{state['g']:.2f},{state['b']:.2f})"
    )


# ─── OSC message handlers ─────────────────────────────────────────────────────

def _handle_x(address, *args):       state["x"] = args[0]

def _handle_y(address, *args):
    state["y"] = args[0]
    _emit_point()   # /y is the last field in each burst — emit here

def _handle_pressure(address, *args): state["pressure"] = args[0]

def _handle_r(address, *args): state["r"] = args[0]
def _handle_g(address, *args): state["g"] = args[0]
def _handle_b(address, *args): state["b"] = args[0]
def _handle_a(address, *args): state["a"] = args[0]

def _handle_canvas_width(address, *args):
    state["canvasWidth"] = args[0]
    _send_canvas_size()

def _handle_canvas_height(address, *args):
    state["canvasHeight"] = args[0]
    _send_canvas_size()

def _send_canvas_size():
    w = state["canvasWidth"]
    h = state["canvasHeight"]
    if w and h:
        preview.broadcast({"type": "canvas_size", "width": w, "height": h})

def _handle_drawing_width(address, *args): state["drawingWidth"] = args[0]
def _handle_eraser_width(address, *args):  state["eraserWidth"]  = args[0]

def _handle_tool_flag(address, *args):
    """One-hot tool flags: when any flag hits 1.0, that becomes the active tool."""
    value     = args[0]
    tool_name = address.lstrip("/")   # "/pen" -> "pen"
    state[tool_name] = value
    if value == 1.0:
        state["tool"] = tool_name
        preview.broadcast({"type": "tool_change", "tool": tool_name})
        print(f"[tool] -> {tool_name}")

def _handle_aspect(address, *args):
    pass   # /aspectX and /aspectY are derived; not needed

def _handle_unknown(address, *args):
    print(f"[unknown] {address}: {args}")


# ─── dispatcher wiring ────────────────────────────────────────────────────────

def _build_dispatcher() -> Dispatcher:
    d = Dispatcher()

    d.map("/x",        _handle_x)
    d.map("/y",        _handle_y)
    d.map("/pressure", _handle_pressure)
    d.map("/aspectX",  _handle_aspect)
    d.map("/aspectY",  _handle_aspect)

    d.map("/r", _handle_r)
    d.map("/g", _handle_g)
    d.map("/b", _handle_b)
    d.map("/a", _handle_a)

    d.map("/canvasWidth",   _handle_canvas_width)
    d.map("/canvasHeight",  _handle_canvas_height)
    d.map("/drawingWidth",  _handle_drawing_width)
    d.map("/eraserWidth",   _handle_eraser_width)

    for tool in ("pen", "pencil", "marker", "monoline", "crayon",
                 "fountainPen", "waterColor", "bitmapEraser", "vectorEraser"):
        d.map(f"/{tool}", _handle_tool_flag)

    d.set_default_handler(_handle_unknown)
    return d


# ─── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 52)
    print("  iDraw OSC -> Python listener")
    print(f"  Listening on port {OSC_PORT}")
    print("=" * 52)
    print()
    print("  In iDraw OSC (iPad):")
    print("    IP   -> your computer's local Wi-Fi IP")
    print(f"   Port -> {OSC_PORT}")
    print()

    preview.start(open_browser=True)

    dispatcher = _build_dispatcher()
    server = ThreadingOSCUDPServer(("0.0.0.0", OSC_PORT), dispatcher)
    print(f"[osc] Listening on 0.0.0.0:{OSC_PORT} ...")
    server.serve_forever()