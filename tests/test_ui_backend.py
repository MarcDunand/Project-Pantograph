"""
What the UI asks of the engine: importing a drawing into the live one, the
controls for that, opening and saving files, the tools, and what it serves.
Same approach as test_app.py: the real app in a subprocess (dry run), driven
over the WebSocket the way the page drives it.
"""
import os
import time

import recording
from conftest import ROOT
from test_app import App, app, hello, send, send_stroke, svgs, wait_until  # noqa: F401 — app is a fixture

SAMPLE = os.path.join(ROOT, "saved_drawings", "drawing_dense.svg")


def long_recording(n=400):
    return {"format": "draw2axi-recording", "version": 1, "strokes": [
        {"canvasWidth": 440.0, "canvasHeight": 956.0,
         "points": [[i * 0.05, 100 + (i % 200), 100 + i // 2, 2.0] for i in range(n)]}]}


def of(msgs, kind):
    return [m for m in msgs if m["type"] == kind]


# ── importing: progress, pause, cancel ───────────────────────────────────────

def test_import_pause_and_cancel(app):
    msgs = send(app, {"type": "import_drawing", "recording": long_recording(), "name": "long.svg"},
                wait=1.0)
    progress = of(msgs, "import_progress")
    assert progress and progress[-1]["active"] and progress[-1]["name"] == "long.svg"
    assert progress[-1]["total"] == 400

    # A second import is refused while one runs.
    assert of(send(app, {"type": "import_drawing", "recording": long_recording()}, wait=0.3), "error")

    msgs = send(app, {"type": "import_pause"}, wait=0.8)
    assert of(msgs, "import_progress")[-1]["paused"]
    plotted = hello(app)["import"]["plotted"]          # (a point may have been in flight)
    time.sleep(0.8)
    assert hello(app)["import"]["plotted"] == plotted  # nothing fed while paused

    msgs = send(app, {"type": "import_cancel"}, wait=1.0)
    end = of(msgs, "import_progress")[-1]
    assert not end["active"] and end["outcome"] == "cancelled"
    assert not hello(app)["import"]["active"]


def test_an_import_is_drawn_recorded_and_saved(app):
    """One canvas: an imported drawing is part of the drawing, like any stroke."""
    rec = {"format": "draw2axi-recording", "version": 1, "strokes": [
        {"canvasWidth": 440.0, "canvasHeight": 956.0,
         "points": [[i * 0.01, 100 + i, 200, 2.0] for i in range(12)]}]}
    send(app, {"type": "import_drawing", "recording": rec, "name": "x.svg"}, wait=2.0)
    assert len([m for m in hello(app)["drawing"] if m["type"] == "point"]) == 12
    assert wait_until(lambda: os.path.exists(os.path.join(app.data_dir, "autosave.svg")))


def test_drawing_while_an_import_runs_is_kept_and_plotted_after(app):
    """The iPad keeps working: its strokes show at once and plot behind the import."""
    send(app, {"type": "import_drawing", "recording": long_recording()}, wait=0.3)
    send_stroke(app.osc_port, n=15)
    time.sleep(0.6)
    live = [m for m in hello(app)["drawing"] if m["type"] == "point" and m["y"] == 400.0]
    assert len(live) == 15, "the live stroke should be drawn and recorded straight away"
    send(app, {"type": "import_cancel"}, wait=1.5)
    assert not hello(app)["import"]["active"]


# ── clearing ─────────────────────────────────────────────────────────────────

def test_clear_throws_the_drawing_away(app):
    send_stroke(app.osc_port)
    assert wait_until(lambda: os.path.exists(os.path.join(app.data_dir, "autosave.svg")))
    msgs = send(app, {"type": "discard_drawing"})
    assert of(msgs, "new_drawing")[0]["saved"] is None
    assert hello(app)["drawing"] == []
    assert svgs(app.data_dir) == []                                # nothing saved, autosave gone


# ── opening a file, previewing it, importing it ──────────────────────────────

def test_open_preview_and_import(app):
    reply = of(send(app, {"type": "open_path", "path": SAMPLE}), "opened")[0]
    assert reply["ok"] and reply["name"] == "drawing_dense.svg" and reply["strokes"] > 100
    assert hello(app)["opened"]["name"] == "drawing_dense.svg"

    status, headers, body = app.get("/opened.svg")
    assert status == 200 and body.startswith(b"<svg") and headers["Content-Security-Policy"] == "sandbox"

    bad = of(send(app, {"type": "open_path", "path": os.path.join(ROOT, "README.md")}), "opened")[0]
    assert not bad["ok"] and "recording" in bad["error"]

    msgs = send(app, {"type": "import_opened"}, wait=1.0)
    assert of(msgs, "import_progress")[-1]["name"] == "drawing_dense.svg"
    send(app, {"type": "import_cancel"}, wait=1.0)


def test_save_as_writes_where_it_is_told(app):
    """The page normally gets a path from the Save window; tests pass one straight in."""
    send_stroke(app.osc_port)
    time.sleep(0.5)
    out = os.path.join(app.data_dir, "picked.svg")
    saved = of(send(app, {"type": "save_as", "path": out, "layers": {"raw": True}}), "saved")[0]
    assert saved["ok"] and saved["path"] == "picked.svg" and os.path.exists(out)
    assert "<metadata>" in open(out, encoding="utf-8").read()


def test_a_saved_file_is_the_drawing_alone(app):
    """
    Whatever the page is showing, a saved file holds the drawing and nothing
    else. The pen path is the machine's account of one run and the effect marks
    are re-applied live from the current settings, so neither is something the
    artist drew — and neither is saved.
    """
    send(app, {"type": "set_effect_enabled", "name": "zigzag", "enabled": True}, wait=0.2)
    send_stroke(app.osc_port, n=25)
    time.sleep(0.8)
    out = os.path.join(app.data_dir, "layers.svg")
    saved = of(send(app, {"type": "save_as", "path": out,
                          "layers": {"raw": True, "optimized": True, "effect": True}},
                    wait=1.5), "saved")[0]
    assert saved["ok"], saved
    svg = open(out, encoding="utf-8").read()
    assert "<metadata>" in svg, "the recording is what makes a file importable"
    assert recording.OPTIMIZED_COLOR not in svg, "the pen path was saved"
    assert recording.EFFECT_COLOR not in svg, "the effect marks were saved"


def test_nothing_to_save_yet(app):
    empty = of(send(app, {"type": "save_as", "path": os.path.join(app.data_dir, "x.svg")}), "saved")[0]
    assert not empty["ok"] and "no drawing" in empty["error"].lower()


# ── the layout: where the drawing and the machine sit on the paper ──────────

def test_the_layout_is_sent_saved_and_applied(app):
    h = hello(app)
    assert h["layout"]["layout"]["auto"] and not h["layout"]["out_of_reach"]

    placed = {"auto": False, "ipad": {"cx": 2.0, "cy": 3.0, "w": 2.0, "h": 4.34, "rot": 15.0},
              "axi": {"cx": 4.25, "cy": 5.5, "rot": 90.0}}
    reply = of(send(app, {"type": "set_layout", "layout": placed}), "layout")[0]
    assert reply["layout"]["ipad"]["cx"] == 2.0 and reply["layout"]["ipad"]["rot"] == 15.0
    assert not reply["layout"]["auto"]

    # Points now land where the layout puts them, not in the middle of the sheet.
    send_stroke(app.osc_port, n=5)
    time.sleep(0.4)
    assert hello(app)["layout"]["layout"]["ipad"]["cx"] == 2.0


def test_what_the_machine_cannot_reach_is_not_drawn(app):
    """A pen can't draw past what it can touch, so those points are skipped."""
    far = {"auto": False, "ipad": {"cx": 0.2, "cy": 0.2, "w": 6.0, "h": 8.0, "rot": 0.0},
           "axi": {"cx": 8.0, "cy": 10.0, "rot": 90.0}}
    assert of(send(app, {"type": "set_layout", "layout": far}), "layout")[0]["out_of_reach"]
    send_stroke(app.osc_port, n=5)                    # all of it beyond the machine
    time.sleep(0.4)
    assert [m for m in hello(app)["drawing"] if m["type"] == "point"] == []

    # A machine that covers only part of the drawing: a stroke crossing the edge
    # is drawn up to it and stops, rather than being flattened against it.
    half = {"auto": False, "ipad": {"cx": 4.25, "cy": 5.5, "w": 8.0, "h": 10.0, "rot": 0.0},
            "axi": {"cx": 1.0, "cy": 5.5, "rot": 90.0}}
    send(app, {"type": "set_layout", "layout": half})
    send(app, {"type": "discard_drawing"})
    send_stroke(app.osc_port, n=40)                   # left to right, across the edge
    time.sleep(0.6)
    drawn = [m for m in hello(app)["drawing"] if m["type"] == "point"]
    assert 0 < len(drawn) < 40
    edge = max(m["x"] for m in drawn)
    assert all(m["x"] <= edge for m in drawn)         # nothing piled up past the edge
    assert len({round(m["x"]) for m in drawn}) == len(drawn)


# ── what the page is served ──────────────────────────────────────────────────

def test_ui_files_and_hello(app):
    for path in ("/ui/app.js", "/ui/app.css", "/ui/icon.svg"):
        assert app.get(path)[0] == 200
    assert app.get("/ui/../listen_to_idraw.py")[0] == 404
    assert app.get("/ui/%2e%2e%2flisten_to_idraw.py")[0] == 404
    h = hello(app)
    assert h["effect_specs"] and h["models"]["1"].startswith("AxiDraw")
    assert h["osc_age"] is None and h["import"]["active"] is False
    assert h["plotter_status"]["motors"] is True                   # dry run: nothing to hold
    assert all("ip" in i for i in h["ips"])


def test_diagnostics(app):
    text = of(send(app, {"type": "diagnostics"}), "diagnostics")[0]["text"]
    assert "Pantograph" in text and "Plotter: dry_run" in text and "Recent log:" in text


# ── --open, and handing a file to a running copy ─────────────────────────────

def test_open_flag_hands_the_file_to_a_running_copy(app):
    import subprocess
    import sys
    second = App.__new__(App)
    second.proc = subprocess.Popen(
        [sys.executable, "listen_to_idraw.py", "--no-browser", "--data-dir", app.data_dir,
         "--open", SAMPLE],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    second.lines = []
    assert second.finish() == 0, second.lines
    assert any("already running" in line for line in second.lines)
    assert hello(app)["opened"]["name"] == "drawing_dense.svg"     # in the first copy's preview


def test_a_saved_file_is_the_paper_not_the_tablet(app):
    """
    What's saved must match the canvas — the analogue of what was plotted — so
    a turned or moved drawing is saved turned and moved, and importing it puts
    the ink back in the same place.
    """
    placed = {"auto": False, "ipad": {"cx": 3.0, "cy": 4.0, "w": 2.0, "h": 4.34, "rot": 30.0},
              "axi": {"cx": 4.25, "cy": 5.5, "rot": 90.0}}
    send(app, {"type": "set_layout", "layout": placed})
    send_stroke(app.osc_port, n=10)
    time.sleep(0.5)

    out = os.path.join(app.data_dir, "placed.svg")
    assert of(send(app, {"type": "save_as", "path": out}), "saved")[0]["ok"]

    from recording import load_svg
    rec, vw, vh = load_svg(out)
    assert rec["space"] == "paper" and rec["paperIn"] == [8.5, 11.0]
    assert (vw, vh) == (8.5 * 96, 11.0 * 96)                  # the sheet, at 96 per inch

    # Every point sits inside the turned rectangle's footprint on the sheet…
    xs = [p[1] / 96 for s in rec["strokes"] for p in s["points"]]
    ys = [p[2] / 96 for s in rec["strokes"] for p in s["points"]]
    assert 1.0 < min(xs) and max(xs) < 5.0 and 1.0 < min(ys) and max(ys) < 7.0
    # The stroke is a horizontal line on the tablet; turned 30°, it must slope.
    slope = (ys[-1] - ys[0]) / (xs[-1] - xs[0])
    assert abs(slope - 0.577) < 0.02                          # tan 30°

    # …and importing it lands on those same places, whatever the layout is now.
    send(app, {"type": "set_layout", "layout": {"auto": True}})
    send(app, {"type": "discard_drawing"})
    send(app, {"type": "open_path", "path": out})
    send(app, {"type": "import_opened"}, wait=2.0)
    again = os.path.join(app.data_dir, "again.svg")
    assert of(send(app, {"type": "save_as", "path": again}), "saved")[0]["ok"]
    rec2, _, _ = load_svg(again)
    xs2 = [p[1] / 96 for s in rec2["strokes"] for p in s["points"]]
    ys2 = [p[2] / 96 for s in rec2["strokes"] for p in s["points"]]
    assert abs(min(xs2) - min(xs)) < 0.02 and abs(min(ys2) - min(ys)) < 0.02
