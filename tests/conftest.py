"""Shared test setup: repo root on the path, and a clean engine per test."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAVED = ROOT / "saved_drawings"


@pytest.fixture
def engine(monkeypatch):
    """
    listen_to_idraw with its module state reset, effects off, a mapping loaded
    and every preview broadcast captured in engine.sent instead of sent to a
    browser. No plotter thread runs: plot commands just collect in
    engine._plot_deque, which is what the tests inspect.
    """
    import listen_to_idraw as L
    import preview

    sent = []
    monkeypatch.setattr(preview, "broadcast", sent.append)

    L._end_stroke()
    L._plot_deque.clear()
    L._pending_024.clear()
    L._spurious_run = 0
    L._current_lag_sec = 0.0
    L._effects_only = False
    L.state.update(x=None, y=None, pressure=1.0, tool="pen", canvasWidth=440.0,
                   canvasHeight=956.0, _last_point_time=None, _pen_is_down=False)
    L.state["_mapping"] = L.compute_mapping(440.0, 956.0)
    for name in L._EFFECT_SWITCHES:
        monkeypatch.setitem(L._EFFECT_SWITCHES, name, False)
    L._rebuild_effect_chain()

    L.sent = sent
    yield L
    L._end_stroke()
    L._plot_deque.clear()
    L._rebuild_effect_chain()


# ── helpers that speak iDraw's message order ─────────────────────────────────

def send_block(L):
    """The state block iDraw sends before every stroke."""
    for addr, val in [("/r", 0.0), ("/g", 0.0), ("/b", 0.0), ("/a", 1.0)]:
        {"/r": L._handle_r, "/g": L._handle_g, "/b": L._handle_b, "/a": L._handle_a}[addr](addr, val)
    for tool in ("pen", "pencil", "marker", "monoline", "crayon", "fountainPen",
                 "waterColor", "bitmapEraser", "vectorEraser"):
        L._handle_tool_flag("/" + tool, 1.0 if tool == "pen" else 0.0)
    L._handle_canvas_width("/canvasWidth", 440.0)
    L._handle_canvas_height("/canvasHeight", 956.0)
    L._handle_drawing_width("/drawingWidth", 2.68)
    L._handle_eraser_width("/eraserWidth", 0.0)


def send_point(L, x, y, raw_pressure=2.0):
    """One point, in iDraw's order: /x, /y, /pressure."""
    L._handle_x("/x", x)
    L._handle_y("/y", y)
    L._handle_pressure("/pressure", raw_pressure)


def kinds(L):
    return [c[1] for c in L._plot_deque]


def pen_ups(L):
    return sum(1 for m in L.sent if m["type"] == "pen_up")
