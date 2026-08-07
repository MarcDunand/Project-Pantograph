"""
postprocess.py

Post-processing effects — additions and changes to the *plotted* version of a
drawing that were never in the original. The preview keeps showing what was
actually drawn; only what reaches the pen is transformed.

An effect is a transform over the plot command stream, not a point filter.
listen_to_idraw builds a naive stream of commands from the incoming OSC points:

    moveto → pendown → lineto × N → penup

Every one of those commands is passed through the enabled effects before it
lands on the plot deque. Each effect may pass a command through untouched,
rewrite it, drop it, or expand it into several commands — including commands of
a different kind than the one that triggered it.

Because effects run at enqueue time (not in the plotter loop), everything they
emit is still seen by the adaptive RDP optimizer downstream, and queue lag
stays accurate.


WRITING AN EFFECT
─────────────────
Subclass Effect. The base class defines the *default response* to every command
kind — pass it through unchanged. Override only the responses you want to
change:

    class Nudge(Effect):
        name = "nudge"

        def on_lineto(self, cmd, ctx):
            t, _, x, y, p = cmd
            return [lineto(t, x + 0.05, y, p)]

Anything the per-kind hooks can't express goes in handle(), which sees every
command and is free to trigger on whatever it likes — RNG, position, a counter,
elapsed time:

    class Sometimes(Effect):
        def handle(self, cmd, ctx):
            if kind_of(cmd) == "pendown" and self.rng.random() < 0.2:
                return []                    # 20% of strokes deposit no ink
            return super().handle(cmd, ctx)  # everything else: default response

Effects are plain objects, so per-stroke or per-drawing state just lives on
self (see StrokeConnector below, which remembers every stroke it has seen).
Each instance gets its own seeded self.rng so runs are reproducible.

Effects are assumed to run one at a time. build_chain() will happily compose
several, and they are applied in registry order, but they are not designed
against each other.


COORDINATES
───────────
Commands carry final AxiDraw physical inches, after paper mapping, landscape
rotation and flip H/V (see canvas_to_physical in listen_to_idraw.py):

    x ∈ [0, ctx.x_max]   physical long axis   (PAPER_HEIGHT_IN)
    y ∈ [0, ctx.y_max]   physical short axis  (PAPER_WIDTH_IN)

Effects that move the pen should clamp with clamp_xy() — the AxiDraw will
happily try to drive past the paper.


COMMAND FORMAT
──────────────
Commands are the tuples the plot deque already speaks: (enqueue_time, kind, *args).
Use the constructors and accessors below rather than indexing by hand.

    (t, "moveto",    x, y)              pen-up travel to stroke start
    (t, "pendown",   pressure, x, y)    lower pen (x, y = where, for tilt comp)
    (t, "lineto",    x, y, pressure)    pen-down move
    (t, "dot_dwell")                    pause briefly (single-tap dots)
    (t, "penup")                        lift pen

Control commands (home, pen_test_*) bypass the effect chain entirely — they are
machine operations, not part of the drawing.
"""

import math
import random
from dataclasses import dataclass, field

# Commands are in inches (see COORDINATES); effects sized in mm convert with this.
MM_PER_IN = 25.4


# ──────────────────────────────────────────────────────────────────────────────
# COMMAND CONSTRUCTORS + ACCESSORS
# ──────────────────────────────────────────────────────────────────────────────

def moveto(t: float, x: float, y: float) -> tuple:
    return (t, "moveto", x, y)

def pendown(t: float, pressure: float, x: float, y: float) -> tuple:
    return (t, "pendown", pressure, x, y)

def lineto(t: float, x: float, y: float, pressure: float) -> tuple:
    return (t, "lineto", x, y, pressure)

def penup(t: float) -> tuple:
    return (t, "penup")

def dot_dwell(t: float) -> tuple:
    return (t, "dot_dwell")


def time_of(cmd: tuple) -> float:
    return cmd[0]

def kind_of(cmd: tuple) -> str:
    return cmd[1]

def xy_of(cmd: tuple) -> tuple | None:
    """(x, y) for commands that carry a position, else None."""
    k = cmd[1]
    if k in ("moveto", "lineto"):
        return (cmd[2], cmd[3])
    if k == "pendown":
        return (cmd[3], cmd[4])
    return None

def pressure_of(cmd: tuple) -> float | None:
    """Normalised 0–1 pressure for commands that carry one, else None."""
    k = cmd[1]
    if k == "lineto":
        return cmd[4]
    if k == "pendown":
        return cmd[2]
    return None

def with_xy(cmd: tuple, x: float, y: float) -> tuple:
    """Same command, new position. Returns cmd unchanged if it carries no position."""
    k = cmd[1]
    if k in ("moveto", "lineto"):
        return (cmd[0], k, x, y, *cmd[4:])
    if k == "pendown":
        return (cmd[0], k, cmd[2], x, y)
    return cmd


# ──────────────────────────────────────────────────────────────────────────────
# CONTEXT
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Ctx:
    """
    Live state of the *incoming* (pre-effect) command stream, maintained by
    listen_to_idraw and handed to every effect. Read-only from an effect's
    point of view — mutating it will confuse the next effect in the chain.
    """
    x_max: float                       # paper extent along physical long axis, inches
    y_max: float                       # paper extent along physical short axis, inches
    stroke_index: int = 0              # 0-based; increments on each moveto
    point_index:  int = 0              # 0-based position within the current stroke
    pen_is_down:  bool = False

    # Position of the *previous* positioned command — i.e. while handling a
    # lineto, the point the pen is moving from. None before the first one.
    last_xy: tuple | None = None


def clamp_xy(x: float, y: float, ctx: Ctx) -> tuple:
    """Keep a position on the paper. Effects that move the pen should use this."""
    return (max(0.0, min(ctx.x_max, x)), max(0.0, min(ctx.y_max, y)))


# ──────────────────────────────────────────────────────────────────────────────
# BASE EFFECT
# ──────────────────────────────────────────────────────────────────────────────

class Effect:
    """
    Base class. Every hook returns a *list* of commands to enqueue in place of
    the one that came in:

        return [cmd]         pass through          (the default response)
        return []            drop it
        return [a, b, c]     expand into several

    The default response for every kind is pass-through, so a subclass only
    writes the responses it actually changes.
    """

    name = "effect"
    label = "effect"          # human-readable name for the browser's effects panel
    default_on = False        # initial checkbox state in the effects panel

    # Tunable numeric knobs exposed to the browser's effects panel. Each entry:
    #   {"attr": <class attribute name>, "label": <ui text>,
    #    "min", "max", "step", "int": <coerce to int?>}
    # The current class-attribute value is the default. Empty = no knobs.
    PARAMS: list = []

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed if seed is not None else 0xD2A)
        self.reset()

    def reset(self) -> None:
        """Clear per-drawing state. Called on construction."""

    def handle(self, cmd: tuple, ctx: Ctx) -> list:
        """
        Master dispatch: routes each command to its on_<kind> response.

        Override this for triggers that aren't a single command kind — RNG,
        position, counters, time — and delegate to super().handle(cmd, ctx) for
        the default response to everything else.
        """
        hook = getattr(self, f"on_{kind_of(cmd)}", None)
        return [cmd] if hook is None else hook(cmd, ctx)

    # ── default responses ────────────────────────────────────────────────────
    def on_moveto(self, cmd, ctx):    return [cmd]
    def on_pendown(self, cmd, ctx):   return [cmd]
    def on_lineto(self, cmd, ctx):    return [cmd]
    def on_dot_dwell(self, cmd, ctx): return [cmd]
    def on_penup(self, cmd, ctx):     return [cmd]


def apply_chain(effects: list, cmd: tuple, ctx: Ctx) -> list:
    """
    Feed one command through every effect in order. Commands one effect emits
    are fed to the next, so a chain composes — though effects are not designed
    to be combined (see module docstring).
    """
    cmds = [cmd]
    for eff in effects:
        out = []
        for c in cmds:
            out.extend(eff.handle(c, ctx))
        cmds = out
    return cmds


# ──────────────────────────────────────────────────────────────────────────────
# EFFECTS
# ──────────────────────────────────────────────────────────────────────────────

class Zigzag(Effect):
    """
    Squares off every pen-down move. Instead of travelling diagonally from
    (x1, y1) straight to (x2, y2), the pen goes fully along x first and then
    fully along y, so a stroke reads as a run of zigzag steps rather than a
    smooth line.
    """

    name = "zigzag"
    label = "zigzag"
    default_on = True

    # How many incoming points each edge of the zigzag spans.
    #   1 — turn a corner at every point: many small steps that track the stroke
    #       closely.
    #   3 — turn a corner at every 3rd point, dropping the two in between: fewer,
    #       larger steps.
    # Sampled points are the only ones plotted, so raising this coarsens the
    # stroke as well as enlarging the steps.
    SAMPLE_EVERY = 10

    # Tunable from the browser's effects panel; see effect_specs().
    PARAMS = [
        {"attr": "SAMPLE_EVERY", "label": "sample every", "min": 1, "max": 40, "step": 1, "int": True},
    ]

    def reset(self):
        self._anchor:  tuple | None = None   # point the next corner is turned from
        self._pending: tuple | None = None   # newest lineto that fell between samples
        self._count = 0                      # linetos seen since the last corner

    def _start(self, cmd, ctx):
        self._anchor  = xy_of(cmd)
        self._pending = None
        self._count   = 0
        return [cmd]

    on_moveto  = _start
    on_pendown = _start

    def on_lineto(self, cmd, ctx):
        self._count += 1
        if self._count % max(1, self.SAMPLE_EVERY):
            self._pending = cmd    # between samples: no corner, no move
            return []
        self._pending = None
        return self._corner(cmd)

    def on_penup(self, cmd, ctx):
        # A stroke rarely ends on an exact multiple of SAMPLE_EVERY, so the last
        # point usually falls between samples. Corner to it anyway, or the stroke
        # would stop short of where it was actually drawn.
        out = self._corner(self._pending) if self._pending else []
        self._pending = None
        self._anchor  = None
        return [*out, cmd]

    def _corner(self, cmd) -> list:
        """Square off the move from the anchor to cmd's point, and re-anchor there."""
        t, _, x, y, p = cmd
        anchor = self._anchor
        self._anchor = (x, y)
        if anchor is None:
            return [cmd]
        ax, ay = anchor
        if x == ax or y == ay:
            return [lineto(t, x, y, p)]   # already axis-aligned; a corner is a no-op
        # Full x travel at the anchor's y, then the full y travel.
        return [lineto(t, x, ay, p), lineto(t, x, y, p)]


class PressureHatch(Effect):
    """
    Leaves every stroke as drawn, then goes back over it once it is finished and
    adds a short line perpendicular to the stroke wherever the pen was pressed
    harder than THRESHOLD. The harder the press, the longer the line — so where
    the drawing was leaned into, the stroke grows a dense comb of marks.

    All four numbers below are meant to be edited.
    """

    name = "pressure_hatch"
    label = "pressure hatch"

    # Below this normalised pressure (0 = no press, 1 = hardest the Pencil
    # reports) a point gets no hatch at all.
    THRESHOLD = 0.30

    # Hatch length, in mm: MIN exactly at THRESHOLD, growing linearly with the
    # force applied beyond it, reaching MAX at full pressure.
    MIN_LENGTH_MM = 1.0
    MAX_LENGTH_MM = 3.0

    # Minimum gap along the stroke between consecutive hatches, in mm.
    # Set to 0 to hatch literally every point the plotter received — be aware
    # that points arrive about every 0.25 mm, so hatches would overlap into a
    # solid band and each one costs a pen up/down cycle. 1 mm reads as a comb.
    SPACING_MM = 1.0

    # Tunable from the browser's effects panel; see effect_specs().
    PARAMS = [
        {"attr": "THRESHOLD",     "label": "threshold",       "min": 0.0, "max": 1.0,  "step": 0.05},
        {"attr": "MIN_LENGTH_MM", "label": "min length (mm)", "min": 0.0, "max": 10.0, "step": 0.5},
        {"attr": "MAX_LENGTH_MM", "label": "max length (mm)", "min": 0.0, "max": 10.0, "step": 0.5},
        {"attr": "SPACING_MM",    "label": "spacing (mm)",    "min": 0.0, "max": 10.0, "step": 0.5},
    ]

    def reset(self):
        self._pts: list = []   # (x, y, pressure) for the stroke in progress

    def on_moveto(self, cmd, ctx):
        self._pts = []
        return [cmd]

    def on_pendown(self, cmd, ctx):
        x, y = xy_of(cmd)
        self._pts.append((x, y, pressure_of(cmd)))
        return [cmd]

    def on_lineto(self, cmd, ctx):
        x, y = xy_of(cmd)
        self._pts.append((x, y, pressure_of(cmd)))
        return [cmd]

    def on_penup(self, cmd, ctx):
        pts = self._pts
        self._pts = []
        if len(pts) < 2:
            return [cmd]

        t = time_of(cmd)
        spacing = self.SPACING_MM / MM_PER_IN
        out  = [cmd]      # lift off the stroke before going back over it
        prev = None       # last point that got a hatch

        for i, (x, y, p) in enumerate(pts):
            if p is None or p < self.THRESHOLD:
                continue
            if prev is not None and math.hypot(x - prev[0], y - prev[1]) < spacing:
                continue
            normal = self._normal(pts, i)
            if normal is None:
                continue
            nx, ny = normal
            half = self._length_in(p) / 2.0
            ax, ay = clamp_xy(x - nx * half, y - ny * half, ctx)
            bx, by = clamp_xy(x + nx * half, y + ny * half, ctx)
            out += [
                moveto(t, ax, ay),
                pendown(t, p, ax, ay),
                lineto(t, bx, by, p),
                penup(t),
            ]
            prev = (x, y)

        return out

    def _length_in(self, pressure: float) -> float:
        """MIN_LENGTH_MM at THRESHOLD → MAX_LENGTH_MM at full pressure, in inches."""
        span = 1.0 - self.THRESHOLD
        frac = 0.0 if span <= 0 else (pressure - self.THRESHOLD) / span
        frac = max(0.0, min(1.0, frac))
        mm = self.MIN_LENGTH_MM + (self.MAX_LENGTH_MM - self.MIN_LENGTH_MM) * frac
        return mm / MM_PER_IN

    def _normal(self, pts: list, i: int) -> tuple | None:
        """
        Unit vector perpendicular to the stroke at pts[i], from the direction
        between its neighbours (the ends use the one neighbour they have).
        None where the neighbours coincide and there is no direction to take.
        """
        ax, ay, _ = pts[max(0, i - 1)]
        bx, by, _ = pts[min(len(pts) - 1, i + 1)]
        dx, dy = bx - ax, by - ay
        d = math.hypot(dx, dy)
        if d < 1e-12:
            return None
        return (-dy / d, dx / d)   # rotate the tangent 90°


class StrokeConnector(Effect):
    """
    Leaves every stroke exactly as drawn, but once one is finished — before the
    next begins — draws a straight line from its midpoint to the midpoint of an
    earlier stroke, picked at random, with a small circle marking each end.

    Selection is weighted by 1/distance, so a stroke is most likely to be
    connected to whatever it was drawn nearest to, and long reaches across the
    paper stay rare but possible. The first stroke of a drawing has nothing to
    connect to and is left alone.

    Shows an effect accumulating state across the whole drawing, and one whose
    output is a stroke of its own rather than a change to the incoming one.
    """

    name = "stroke_connector"
    label = "stroke connector"

    # Pressure for the connecting line.
    LINE_PRESSURE = 0

    # Floor on distance when weighting, so near-coincident midpoints get a
    # large weight instead of dividing by zero.
    MIN_DIST_IN = 0.05

    # Circle marking each end of a connecting line. The AxiDraw has no arc
    # primitive, so it is plotted as a closed polygon of this many segments.
    # At 1 mm across, 12 sides keep the chords (~0.010") at STREAM_MIN_DIST_IN
    # rather than below it — finer would only feed the plotter moves too small
    # to execute smoothly — while sitting ~17 µm off a true circle, far under
    # what a pen nib can resolve.
    CIRCLE_DIAMETER_IN = 1.0 / MM_PER_IN
    CIRCLE_SEGMENTS    = 12

    # Tunable from the browser's effects panel; see effect_specs().
    PARAMS = [
        {"attr": "LINE_PRESSURE",   "label": "line pressure",   "min": 0.0,  "max": 1.0, "step": 0.05},
        {"attr": "MIN_DIST_IN",     "label": "min dist (in)",   "min": 0.01, "max": 1.0, "step": 0.01},
        {"attr": "CIRCLE_SEGMENTS", "label": "circle segments", "min": 3,    "max": 32,  "step": 1, "int": True},
    ]

    def reset(self):
        self._midpoints: list = []   # midpoint of every completed stroke, in order
        self._stroke:    list = []   # inked positions of the stroke in progress

    def on_moveto(self, cmd, ctx):
        self._stroke = []
        return [cmd]

    def on_pendown(self, cmd, ctx):
        self._stroke.append(xy_of(cmd))
        return [cmd]

    def on_lineto(self, cmd, ctx):
        self._stroke.append(xy_of(cmd))
        return [cmd]

    def on_penup(self, cmd, ctx):
        if not self._stroke:
            return [cmd]

        mid = self._midpoint(self._stroke)
        self._stroke = []

        # Pick from the strokes that came before, then record this one — so a
        # stroke is never connected to itself.
        target = self._pick_target(mid)
        self._midpoints.append(mid)
        if target is None:
            return [cmd]

        t = time_of(cmd)
        return [
            cmd,                                        # lift off the finished stroke
            *self._connector(t, mid, target, ctx),
        ]

    def _connector(self, t: float, start: tuple, end: tuple, ctx: Ctx) -> list:
        """
        Circle at `start`, straight line, circle at `end` — one continuous
        pen-down path.

        Each circle is entered at the point facing the other end, so tracing the
        first one leaves the pen exactly where the line begins, and the line
        arrives exactly where the second one begins. The line therefore runs rim
        to rim along the line of centres, and the pen neither lifts nor doubles
        back anywhere between the two circles.
        """
        theta = math.atan2(end[1] - start[1], end[0] - start[0])

        # The join between the two rings is the straight line: the last point of
        # the first circle and the first point of the second are the facing rims.
        pts = [
            *self._circle_pts(start, theta, ctx),
            *self._circle_pts(end, theta + math.pi, ctx),
        ]
        return [
            moveto(t, *pts[0]),
            pendown(t, self.LINE_PRESSURE, *pts[0]),
            *[lineto(t, x, y, self.LINE_PRESSURE) for x, y in pts[1:]],
            penup(t),
        ]

    def _circle_pts(self, center: tuple, start_angle: float, ctx: Ctx) -> list:
        """
        One full turn around `center`, opening and closing at `start_angle`.
        Returns SEGMENTS + 1 points — the last repeats the first to close the ring.
        """
        cx, cy = center
        r = self.CIRCLE_DIAMETER_IN / 2.0
        out = []
        for i in range(self.CIRCLE_SEGMENTS + 1):
            a = start_angle + 2.0 * math.pi * i / self.CIRCLE_SEGMENTS
            out.append(clamp_xy(cx + r * math.cos(a), cy + r * math.sin(a), ctx))
        return out

    def _midpoint(self, stroke: list) -> tuple:
        """
        The stroke's middle sample rather than its centroid: it lies on actual
        ink, so the connecting line meets the stroke it belongs to (a centroid
        can fall in empty space — think of a U). The upstream distance filter
        spaces points roughly evenly, so the middle sample sits near the true
        halfway point along the stroke.
        """
        return stroke[len(stroke) // 2]

    def _pick_target(self, mid: tuple) -> tuple | None:
        if not self._midpoints:
            return None
        weights = [
            1.0 / max(math.hypot(m[0] - mid[0], m[1] - mid[1]), self.MIN_DIST_IN)
            for m in self._midpoints
        ]
        return self.rng.choices(self._midpoints, weights=weights, k=1)[0]


class Mirror(Effect):
    """
    Now and then, doubles a short stroke with a left–right mirrored copy of
    itself. Each finished stroke short enough to qualify — total path length no
    more than MIRROR_THRESH of the page width — gets, with probability
    MIRROR_CHANCE, a second stroke laid down that is the first flipped left-to-
    right across its own horizontal centre, so the copy sits over the original as
    a reflection through the middle of the mark. Long strokes and unlucky ones
    pass through untouched.

    The flip is across the drawing's horizontal: plot commands are in physical
    inches where the drawing's left/right axis is y (the paper was rotated 90°),
    so mirroring left–right reflects y about the stroke's own y-centre. The page
    width in that direction is ctx.y_max (PAPER_WIDTH_IN).
    """

    name = "mirror"
    label = "mirror"

    # Longest stroke still eligible to be mirrored, as a fraction of the page
    # width (the drawing's left–right extent). 0.10 = up to a tenth of the page
    # wide. Compared against the stroke's total path length.
    MIRROR_THRESH = 0.10
    # Probability an eligible stroke actually gets its mirrored copy.
    MIRROR_CHANCE = 0.30

    PARAMS = [
        {"attr": "MIRROR_CHANCE", "label": "mirror chance",     "min": 0.0, "max": 1.0, "step": 0.05},
        {"attr": "MIRROR_THRESH", "label": "max len (× width)", "min": 0.0, "max": 2.0, "step": 0.05},
    ]

    def reset(self):
        self._pts: list = []   # (x, y, pressure) of the stroke in progress

    def on_moveto(self, cmd, ctx):
        self._pts = []
        return [cmd]

    def on_pendown(self, cmd, ctx):
        self._pts.append((*xy_of(cmd), pressure_of(cmd)))
        return [cmd]

    def on_lineto(self, cmd, ctx):
        self._pts.append((*xy_of(cmd), pressure_of(cmd)))
        return [cmd]

    def on_penup(self, cmd, ctx):
        pts = self._pts
        self._pts = []
        if len(pts) < 2:
            return [cmd]

        length = sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                     for i in range(1, len(pts)))
        max_len = self.MIRROR_THRESH * ctx.y_max        # fraction of page width → inches
        if length > max_len or self.rng.random() >= self.MIRROR_CHANCE:
            return [cmd]

        # Reflect every point about the stroke's horizontal (y) centre line.
        ys = [p[1] for p in pts]
        axis = (min(ys) + max(ys)) / 2.0
        t = time_of(cmd)

        x0, y0 = clamp_xy(pts[0][0], 2.0 * axis - pts[0][1], ctx)
        out = [cmd, moveto(t, x0, y0), pendown(t, pts[0][2], x0, y0)]
        for x, y, p in pts[1:]:
            mx, my = clamp_xy(x, 2.0 * axis - y, ctx)
            out.append(lineto(t, mx, my, p))
        out.append(penup(t))
        return out


# ──────────────────────────────────────────────────────────────────────────────
# REGISTRY
# ──────────────────────────────────────────────────────────────────────────────
#
# Keys here are the names listen_to_idraw's EFFECT_* switches map to.
# Add a new effect by writing the class above and listing it here.

REGISTRY: dict = {
    Zigzag.name:          Zigzag,
    PressureHatch.name:   PressureHatch,
    StrokeConnector.name: StrokeConnector,
    Mirror.name:          Mirror,
}


def build_chain(enabled: dict, params: dict | None = None) -> list:
    """
    Instantiate the enabled effects, in registry order.
    `enabled` maps registry name → bool; unknown names raise rather than
    silently doing nothing when a switch gets renamed.

    `params`, if given, maps registry name → {attr: value}; each value is set
    on the freshly built effect instance, overriding its class default. Only
    attributes listed in that effect's PARAMS are accepted (others are ignored,
    so a stale knob from an old UI can't reach into effect internals).
    """
    unknown = set(enabled) - set(REGISTRY)
    if unknown:
        raise KeyError(f"unknown post-processing effect(s): {sorted(unknown)}")
    params = params or {}
    chain = []
    for name, on in enabled.items():
        if not on:
            continue
        eff = REGISTRY[name]()
        specs = {p["attr"]: p for p in REGISTRY[name].PARAMS}
        for attr, value in (params.get(name) or {}).items():
            p = specs.get(attr)
            if p is None:
                continue
            try:
                value = int(round(float(value))) if p.get("int") else float(value)
            except (TypeError, ValueError):
                continue
            setattr(eff, attr, value)
        chain.append(eff)
    return chain


def effect_specs() -> list:
    """
    Serializable description of every registered effect and its tunable knobs,
    in registry order — the single source of truth the browser's effects panel
    is built from. Each knob's current class-attribute value is its default.
    """
    specs = []
    for name, cls in REGISTRY.items():
        knobs = []
        for p in cls.PARAMS:
            knobs.append({**p, "default": getattr(cls, p["attr"])})
        specs.append({
            "name":    name,
            "label":   getattr(cls, "label", name),
            "enabled": getattr(cls, "default_on", False),
            "params":  knobs,
        })
    return specs
