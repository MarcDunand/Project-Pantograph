"""The session recorder, SVG output, saved settings, and paper/model handling."""
import json

import pytest

import recording
from PantographApp.settings import Settings, engine_messages


def point(x, y, raw=2.0, t=0.0, replay=False, **meta):
    m = {"type": "point", "t": t, "x": x, "y": y, "pressure": raw / recording.OSC_PRESSURE_MAX,
         "pressureRaw": raw, "r": 0.0, "g": 0.0, "b": 0.0, "a": 1.0, "tool": "pen",
         "drawingWidth": 2.0, "canvasWidth": 440.0, "canvasHeight": 956.0, "replay": replay}
    m.update(meta)
    return m


PEN_UP = {"type": "pen_up"}


# ── Recorder ─────────────────────────────────────────────────────────────────

def test_recorder_follows_the_page_rules():
    r = recording.Recorder()
    for i in range(3):
        r.on_message(point(10 + i, 20, t=i))
    r.on_message(PEN_UP)
    r.on_message(point(50, 60, r=1.0, g=0.0, b=0.0))       # still open: included in the recording
    rec = r.recording()
    assert rec["format"] == "draw2axi-recording" and rec["version"] == 1
    assert [len(s["points"]) for s in rec["strokes"]] == [3, 1]
    assert rec["strokes"][0]["points"][0] == [0, 10, 20, 2.0]
    assert rec["strokes"][1]["color"]["r"] == 1.0            # metadata latched per stroke
    assert not r.is_empty() and r.dirty


def test_replayed_strokes_are_drawn_but_not_recorded():
    r = recording.Recorder()
    r.on_message(point(1, 1, replay=True))
    r.on_message(point(2, 2, replay=True))
    r.on_message(PEN_UP)
    assert r.is_empty() and r.recording()["strokes"] == [] and r.messages() == []
    r.on_message(point(3, 3))
    r.on_message(PEN_UP)
    assert len(r.recording()["strokes"]) == 1
    assert [m["type"] for m in r.messages()] == ["point", "pen_up"]


def test_recorder_captures_layers_and_clears():
    r = recording.Recorder()
    def layer(kind, x=0.0, name="optimized"):
        return {"type": "layer", "layer": name, "kind": kind, "x": x, "y": 5.0,
                "pressure": 0.5, "drawingWidth": 2.0, "canvasWidth": 440.0}
    for kind, x in [("moveto", 0), ("pendown", 0), ("lineto", 1), ("lineto", 2), ("penup", 0)]:
        r.on_message(layer(kind, x))
    r.on_message(layer("lineto", 7, name="effect"))
    layers = r.layers()
    assert [len(s["points"]) for s in layers["optimized"]] == [3]
    assert [len(s["points"]) for s in layers["effect"]] == [1]
    r.clear()
    assert r.is_empty() and r.layers() == {"optimized": [], "effect": []} and r.messages() == []


# ── SVG output ───────────────────────────────────────────────────────────────

def test_svg_is_white_paper_with_greyscale_strokes_and_round_trips(tmp_path):
    r = recording.Recorder()
    for i, colour in enumerate([(0, 0, 0), (1, 1, 1), (1, 0, 0)]):
        rr, gg, bb = colour
        r.on_message(point(10 + i, 10, r=rr, g=gg, b=bb))
        r.on_message(point(20 + i, 30, r=rr, g=gg, b=bb))
        r.on_message(PEN_UP)
    svg = r.svg()
    assert '<rect width="440" height="956" fill="#fff"/>' in svg
    assert 'stroke="#000000"' in svg and 'stroke="#ffffff"' in svg
    assert 'stroke="#363636"' in svg              # red → its luminance grey (0.2126 × 255)
    f = tmp_path / "d.svg"
    f.write_text(svg, encoding="utf-8")
    rec, w, h = recording.load_svg(f)
    assert rec == r.recording() and (w, h) == (440.0, 956.0)


def test_layers_only_export_has_no_recording():
    r = recording.Recorder()
    r.on_message(point(1, 1)); r.on_message(PEN_UP)
    assert "<metadata>" not in r.svg(include_raw=False, optimized=True)
    with pytest.raises(ValueError):
        recording.parse_svg(r.svg(include_raw=False))


def test_thumbnail_is_light_and_has_no_metadata():
    rec = {"format": "draw2axi-recording", "version": 1, "strokes": [
        {"drawingWidth": 2, "color": {"r": 0, "g": 0, "b": 0, "a": 1},
         "points": [[i, i, i, 2.0] for i in range(20000)]}]}
    svg = recording.thumbnail_svg(rec, 440, 956)
    assert "<metadata>" not in svg and svg.count(",") < 9000


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_round_trip_and_ignore_junk(tmp_path):
    s = Settings(tmp_path)
    assert s.values == {}
    s.replace({"axi_penPosUp": 55, "not_ours": "x"})
    assert Settings(tmp_path).values == {"axi_penPosUp": "55"}
    (tmp_path / "settings.json").write_text("{nonsense", encoding="utf-8")
    assert Settings(tmp_path).values == {}


def test_engine_messages_cover_the_saved_settings():
    msgs = engine_messages({"axi_flipX": "1", "axi_penPosUp": "55", "axi_previewW": "400",
                            "axi_fx_en_pressure_hatch": "1", "axi_fx_p_pressure_hatch_SPACING_MM": "2.5",
                            "axi_model": "2", "axi_paperW": "11.69", "axi_paperH": "16.54"})
    assert {"type": "set_flip_x", "enabled": True} in msgs
    assert {"type": "set_pen_up_pos", "value": 55.0} in msgs
    assert {"type": "set_effect_enabled", "name": "pressure_hatch", "enabled": True} in msgs
    assert {"type": "set_effect_param_key", "key": "pressure_hatch_SPACING_MM", "value": 2.5} in msgs
    kinds = [m["type"] for m in msgs]
    assert kinds.index("set_model") < kinds.index("set_paper")      # the model's reach first
    assert not any("preview" in json.dumps(m) for m in msgs)          # page-only keys stay out


def test_saved_effect_param_reaches_the_engine(engine):
    engine._handle_preview_message({"type": "set_effect_param_key", "key": "pressure_hatch_SPACING_MM", "value": 3.25})
    assert engine._effect_params["pressure_hatch"]["SPACING_MM"] == 3.25


# ── paper and model ──────────────────────────────────────────────────────────

@pytest.fixture
def paper(engine):
    yield engine
    engine._plot_deque.clear()
    engine.set_model(1)
    engine.set_paper(8.5, 11)


def test_paper_is_clamped_to_the_models_reach(paper):
    info = paper.set_paper(16.54, 11.69)                  # A3, given landscape
    assert info["clamped"] and (paper.PAPER_WIDTH_IN, paper.PAPER_HEIGHT_IN) == (8.58, 11.81)
    info = paper.set_model(2)                              # SE/A3 reaches it
    assert not info["clamped"] and (paper.PAPER_WIDTH_IN, paper.PAPER_HEIGHT_IN) == (11.69, 16.54)
    assert paper._effect_ctx.x_max == 16.54 and paper._effect_ctx.y_max == 11.69
    m = paper.state["_mapping"]
    assert m["draw_h"] <= 16.54 and m["draw_w"] <= 11.69


def test_paper_change_refused_mid_plot(paper):
    paper._plot_deque.append((0.0, "penup"))
    assert paper.set_paper(8.27, 11.69)["type"] == "error"
    assert (paper.PAPER_WIDTH_IN, paper.PAPER_HEIGHT_IN) == (8.5, 11)
    assert paper.set_model(2)["type"] == "error"


def test_unknown_model_is_an_error(paper):
    assert paper.set_model(99)["type"] == "error"
