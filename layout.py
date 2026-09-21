"""
layout.py

Where things sit on the paper, and the one path from an iPad point to a place
on the machine.

Everything is in inches on the sheet of paper, whose top-left corner is (0, 0)
with x to the right and y down — the paper is the anchor, and it never moves.
Two rectangles sit on it:

  * **the iPad rectangle** — the drawing surface, in the tablet's own aspect
    ratio. Move it, turn it, or scale it, and the drawing moves with it.
  * **the AxiDraw rectangle** — the machine's travel, the size its model can
    reach. Move it and turn it to match where the machine really sits on the
    paper. Its home corner (the machine's 0, 0) is drawn with a home mark.

An angle is in degrees, turning the rectangle's own +x axis clockwise from the
paper's +x (clockwise because y runs down, as on screen and on the page).

Machine coordinates come out of `paper_to_machine`: x along the machine's long
axis, y along its short one, both from the home corner — which is what
pyaxidraw and the plot commands use. It also says whether the machine can reach
the point at all: what it can't reach isn't drawn, because a pen can't draw
past what it can touch.
"""

import math

# The iPad rectangle is fitted inside the paper with this much of a border when
# a layout is worked out from scratch.
DEFAULT_MARGIN_IN = 0.0

# A drawing that lands within this of the machine's limit counts as reachable:
# the numbers are inches on paper, and a quarter of a millimetre either way is
# neither a real problem nor worth a warning.
REACH_EPS_IN = 0.01


def _rotate(x: float, y: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def default_layout(paper_w: float, paper_h: float, canvas_w: float, canvas_h: float,
                   travel_long: float, travel_short: float) -> dict:
    """
    The layout nobody has touched: the drawing fitted to the paper, and the
    machine centred on it, its long axis along the paper's long side.
    """
    lay = {"auto": True,
           "ipad": {"cx": paper_w / 2, "cy": paper_h / 2, "w": 0.0, "h": 0.0, "rot": 0.0},
           "axi":  {"cx": paper_w / 2, "cy": paper_h / 2, "rot": 0.0}}
    fit(lay, paper_w, paper_h, canvas_w, canvas_h)
    return lay


def fit(lay: dict, paper_w: float, paper_h: float, canvas_w: float, canvas_h: float) -> None:
    """Centre both rectangles on the paper: the layout nobody has placed by hand."""
    fit_ipad(lay, paper_w, paper_h, canvas_w, canvas_h)
    # The machine's long axis along the paper's long side, as it would be set up.
    lay["axi"].update(cx=paper_w / 2, cy=paper_h / 2, rot=90.0 if paper_h >= paper_w else 0.0)


def fit_ipad(lay: dict, paper_w: float, paper_h: float, canvas_w: float, canvas_h: float) -> None:
    """Size the iPad rectangle to fill the paper, keeping the tablet's shape."""
    aspect = (canvas_w / canvas_h) if canvas_h else 1.0
    avail_w, avail_h = max(0.1, paper_w - 2 * DEFAULT_MARGIN_IN), max(0.1, paper_h - 2 * DEFAULT_MARGIN_IN)
    if aspect < avail_w / avail_h:
        h, w = avail_h, avail_h * aspect
    else:
        w, h = avail_w, avail_w / aspect
    lay["ipad"].update(cx=paper_w / 2, cy=paper_h / 2, w=w, h=h, rot=lay["ipad"].get("rot", 0.0))


def normalize(lay: dict | None, paper_w: float, paper_h: float, canvas_w: float, canvas_h: float,
              travel_long: float, travel_short: float) -> dict:
    """A complete layout from whatever was saved (or nothing at all)."""
    base = default_layout(paper_w, paper_h, canvas_w, canvas_h, travel_long, travel_short)
    if not isinstance(lay, dict):
        return base
    out = {"auto": bool(lay.get("auto", False))}
    for part in ("ipad", "axi"):
        got = lay.get(part) if isinstance(lay.get(part), dict) else {}
        out[part] = dict(base[part])
        for key, value in got.items():
            if key in out[part]:
                try:
                    out[part][key] = float(value)
                except (TypeError, ValueError):
                    pass
    if out["auto"] or not (out["ipad"]["w"] > 0 and out["ipad"]["h"] > 0):
        fit(out, paper_w, paper_h, canvas_w, canvas_h)
        out["auto"] = True
    return out


# ── the one path from a drawing to the machine ───────────────────────────────

def canvas_to_paper(x: float, y: float, lay: dict, canvas_w: float, canvas_h: float) -> tuple[float, float]:
    """A point on the tablet → where it lands on the paper, in inches."""
    ipad = lay["ipad"]
    u = (x / canvas_w - 0.5) * ipad["w"] if canvas_w else 0.0
    v = (y / canvas_h - 0.5) * ipad["h"] if canvas_h else 0.0
    dx, dy = _rotate(u, v, ipad["rot"])
    return ipad["cx"] + dx, ipad["cy"] + dy


def paper_to_canvas(px: float, py: float, lay: dict, canvas_w: float, canvas_h: float) -> tuple[float, float]:
    """The inverse of canvas_to_paper: a place on the paper → a point on the tablet."""
    ipad = lay["ipad"]
    dx, dy = _rotate(px - ipad["cx"], py - ipad["cy"], -ipad["rot"])
    x = (dx / ipad["w"] + 0.5) * canvas_w if ipad["w"] else 0.0
    y = (dy / ipad["h"] + 0.5) * canvas_h if ipad["h"] else 0.0
    return x, y


def paper_to_machine(px: float, py: float, lay: dict, travel_long: float, travel_short: float,
                     clamp: bool = True) -> tuple[float, float, bool]:
    """
    A place on the paper → the machine's own coordinates, measured from its
    home corner. Returns (x, y, reachable); the caller skips what isn't
    reachable, and the clamp is only a backstop against a stray value.
    """
    axi = lay["axi"]
    dx, dy = _rotate(px - axi["cx"], py - axi["cy"], -axi["rot"])
    mx, my = dx + travel_long / 2, dy + travel_short / 2
    reachable = (-REACH_EPS_IN <= mx <= travel_long + REACH_EPS_IN
                 and -REACH_EPS_IN <= my <= travel_short + REACH_EPS_IN)
    if clamp:
        mx = min(max(mx, 0.0), travel_long)
        my = min(max(my, 0.0), travel_short)
    return mx, my, reachable


def machine_to_paper(mx: float, my: float, lay: dict, travel_long: float, travel_short: float) -> tuple[float, float]:
    """The inverse: where a machine position falls on the paper."""
    axi = lay["axi"]
    dx, dy = _rotate(mx - travel_long / 2, my - travel_short / 2, axi["rot"])
    return axi["cx"] + dx, axi["cy"] + dy


def canvas_to_machine(x: float, y: float, lay: dict, canvas_w: float, canvas_h: float,
                      travel_long: float, travel_short: float) -> tuple[float, float, bool]:
    px, py = canvas_to_paper(x, y, lay, canvas_w, canvas_h)
    return paper_to_machine(px, py, lay, travel_long, travel_short)


def inches_per_canvas_unit(lay: dict, canvas_w: float) -> float:
    """How big one tablet unit is on the paper — for stroke widths."""
    return (lay["ipad"]["w"] / canvas_w) if canvas_w else 0.0


def corners(rect: dict, w: float, h: float) -> list[tuple[float, float]]:
    """A rectangle's four corners on the paper, starting at its local origin (home)."""
    return [(rect["cx"] + _rotate(sx * w / 2, sy * h / 2, rect["rot"])[0],
             rect["cy"] + _rotate(sx * w / 2, sy * h / 2, rect["rot"])[1])
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def out_of_reach(lay: dict, canvas_w: float, canvas_h: float,
                 travel_long: float, travel_short: float) -> bool:
    """True when part of the drawing area falls outside what the machine reaches."""
    for x, y in ((0, 0), (canvas_w, 0), (canvas_w, canvas_h), (0, canvas_h)):
        if not canvas_to_machine(x, y, lay, canvas_w, canvas_h, travel_long, travel_short)[2]:
            return True
    return False
