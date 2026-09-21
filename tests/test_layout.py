"""
The layout: where the drawing and the machine sit on the paper, and the path
from a point on the tablet to a place on the machine.
"""
import math

import layout

LETTER = (8.5, 11.0)
CANVAS = (440.0, 956.0)
V3 = (11.81, 8.58)             # an AxiDraw V3's reach: long axis, short axis


def default():
    return layout.default_layout(*LETTER, *CANVAS, *V3)


def test_the_untouched_layout_centres_both_rectangles():
    lay = default()
    assert lay["auto"]
    # The drawing keeps the tablet's shape and fills the sheet.
    assert lay["ipad"]["h"] == 11.0
    assert math.isclose(lay["ipad"]["w"] / lay["ipad"]["h"], 440 / 956, rel_tol=1e-9)
    assert (lay["ipad"]["cx"], lay["ipad"]["cy"]) == (4.25, 5.5)
    # The machine's long axis runs along the paper's long side.
    assert lay["axi"]["rot"] == 90.0 and (lay["axi"]["cx"], lay["axi"]["cy"]) == (4.25, 5.5)


def test_a_point_goes_to_the_machine_and_back():
    lay = default()
    for x, y in ((0, 0), (440, 956), (123, 456)):
        px, py = layout.canvas_to_paper(x, y, lay, *CANVAS)
        mx, my, ok = layout.paper_to_machine(px, py, lay, *V3)
        assert ok
        back = layout.machine_to_paper(mx, my, lay, *V3)
        assert math.isclose(back[0], px, abs_tol=1e-9) and math.isclose(back[1], py, abs_tol=1e-9)


def test_home_is_the_machine_rectangles_own_corner():
    lay = default()
    home = layout.machine_to_paper(0, 0, lay, *V3)
    # Turned 90°, the machine's origin corner sits up and to the right.
    assert home[0] > lay["axi"]["cx"] and home[1] < lay["axi"]["cy"]
    far = layout.machine_to_paper(V3[0], V3[1], lay, *V3)
    assert math.isclose(math.dist(home, far), math.hypot(*V3), abs_tol=1e-9)


def test_turning_the_drawing_turns_what_lands_on_the_paper():
    lay = default()
    straight = layout.canvas_to_paper(440, 478, lay, *CANVAS)      # the drawing's right edge
    lay["ipad"]["rot"] = 90.0
    turned = layout.canvas_to_paper(440, 478, lay, *CANVAS)
    # What was to the right of centre is now below it.
    assert straight[0] > lay["ipad"]["cx"] and math.isclose(straight[1], lay["ipad"]["cy"], abs_tol=1e-9)
    assert turned[1] > lay["ipad"]["cy"] and math.isclose(turned[0], lay["ipad"]["cx"], abs_tol=1e-9)


def test_out_of_reach_is_reported_and_the_pen_stops_at_the_edge():
    lay = default()
    assert not layout.out_of_reach(lay, *CANVAS, *V3)
    lay["axi"]["cx"] -= 4.0                                        # the machine sits off to one side
    assert layout.out_of_reach(lay, *CANVAS, *V3)
    mx, my, ok = layout.canvas_to_machine(440, 956, lay, *CANVAS, *V3)
    assert not ok
    assert 0 <= mx <= V3[0] and 0 <= my <= V3[1]                   # clamped, never past the limit


def test_a_saved_layout_survives_a_round_trip_and_junk_is_ignored():
    lay = default()
    lay["auto"] = False
    lay["ipad"].update(cx=3.0, cy=4.0, rot=17.5)
    same = layout.normalize(lay, *LETTER, *CANVAS, *V3)
    assert same["ipad"]["cx"] == 3.0 and same["ipad"]["rot"] == 17.5 and not same["auto"]

    junk = layout.normalize({"ipad": {"cx": "nonsense", "nope": 1}, "auto": False},
                            *LETTER, *CANVAS, *V3)
    assert junk["ipad"]["cx"] == 4.25 and "nope" not in junk["ipad"]
    assert layout.normalize(None, *LETTER, *CANVAS, *V3)["auto"]


def test_a_placed_layout_stays_put_when_the_paper_changes():
    placed = layout.normalize({"auto": False, "ipad": {"cx": 2.0, "cy": 2.0, "w": 3.0, "h": 6.5},
                               "axi": {"cx": 5.0, "cy": 5.0, "rot": 0.0}},
                              11.0, 17.0, *CANVAS, *V3)
    assert (placed["ipad"]["cx"], placed["ipad"]["w"]) == (2.0, 3.0)
    assert placed["axi"]["rot"] == 0.0
