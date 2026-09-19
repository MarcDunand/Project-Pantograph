"""The draw2axi-recording format, and the offline tools, against real drawings."""
import copy

import pytest

from conftest import SAVED
from dot_healer import heal_recording
from svg_transform import build_svg, load_svg, transform

WITH_RECORDING = ["drawing_dense.svg", "drawing_dense_healed.svg", "drawing_inkblot.svg",
                  "drawing_from_iDraw_to_svg.svg",   # made by the old no-AxiDraw copy
                  "drawing_raw.svg", "drawing_raw_1.svg", "drawing_raw_minWidth79.svg"]
# Exported without the raw layer, so they carry no recording.
WITHOUT_RECORDING = ["drawing_raw_flipped.svg", "drawing_tree.svg"]


@pytest.mark.parametrize("name", WITH_RECORDING)
def test_recording_round_trips(name, tmp_path):
    rec, w, h = load_svg(SAVED / name)
    assert rec["format"] == "draw2axi-recording" and rec["version"] == 1
    out = tmp_path / name
    out.write_text(build_svg(rec, w, h), encoding="utf-8")
    rec2, w2, h2 = load_svg(out)
    assert (rec2, w2, h2) == (rec, w, h)


@pytest.mark.parametrize("name", WITHOUT_RECORDING)
def test_svg_without_recording_is_rejected(name):
    with pytest.raises(ValueError):
        load_svg(SAVED / name)


def test_heal_reproduces_committed_output():
    """drawing_dense_healed.svg was made by dot_healer from drawing_dense.svg."""
    rec, w, h = load_svg(SAVED / "drawing_dense.svg")
    stats = heal_recording(rec, w, h)
    assert rec == load_svg(SAVED / "drawing_dense_healed.svg")[0]
    assert stats["runs"] > 0


def test_width_filter_reproduces_committed_output():
    """drawing_raw_minWidth79.svg was made by svg_transform at 79% from drawing_raw.svg."""
    rec, w, h = load_svg(SAVED / "drawing_raw.svg")
    out, stats = transform(rec, w, h, percent=79)
    assert out == load_svg(SAVED / "drawing_raw_minWidth79.svg")[0]
    assert stats["kept_strokes"] == len(out["strokes"]) < len(rec["strokes"])


def test_transform_leaves_input_untouched_and_flips_are_involutions():
    rec, w, h = load_svg(SAVED / "drawing_raw_1.svg")
    before = copy.deepcopy(rec)
    flipped, _ = transform(rec, w, h, flip_h=True, flip_v=True)
    assert rec == before
    assert flipped != rec
    back, _ = transform(flipped, w, h, flip_h=True, flip_v=True)
    assert back["strokes"] == rec["strokes"]
