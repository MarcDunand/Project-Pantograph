"""
recording.py

The one Python implementation of the draw2axi-recording format: reading it
from an SVG, writing an SVG around it, and recording a live session from the
same message stream the browser preview receives.

THE FORMAT (version 1 — unchanged, so old files load and new files load in
old code)
------------------------------------------------------------------------------
A plot SVG carries a <metadata> block holding the recording as JSON:

    {"format": "draw2axi-recording", "version": 1,
     "space": "paper", "paperIn": [8.5, 11],          (saved files only)
     "strokes": [{"tool", "drawingWidth", "color": {r,g,b,a},
                  "canvasWidth", "canvasHeight",
                  "points": [[t, x, y, pressureRaw], ...]}, ...]}

While a drawing is being made, points are the raw OSC input, verbatim and
unrounded, in that stroke's own canvas units. A **saved** file is a picture of
the sheet of paper instead: its points are where the pen went on the paper, at
96 per inch, and `space: "paper"` says so (with `paperIn`, the sheet's size).
That way the file matches what was plotted, whatever the layout was, and
importing it puts the ink back in the same place. Older files have no `space`
and are read as tablet coordinates, as before. **The recording is the source of truth**: importing reads only the
metadata. The <path>/<circle> elements are cosmetic, so the file looks right in
a viewer — drawn here as greyscale strokes on white paper, like iDraw OSC.
"""

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

# ── constants shared with the pipeline ───────────────────────────────────────
OSC_PRESSURE_MAX      = 4.166666507720947
STROKE_THINNING       = 0.5          # matches preview.py's widthFor
DEFAULT_DRAWING_WIDTH = 1.5

PAPER_COLOR     = "#fff"
OPTIMIZED_COLOR = "#f76707"          # the pen's path — the machine's own colour
EFFECT_COLOR    = "#1c7ed6"          # what the effects add

_METADATA_RE = re.compile(r"<metadata>(.*?)</metadata>", re.S)
_SVG_SIZE_RE = re.compile(r'<svg[^>]*\bwidth="([\d.]+)"[^>]*\bheight="([\d.]+)"')


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1.0 else v


def width_for(size: float, pressure_norm: float) -> float:
    """Rendered/plotted line width for a point. Identical to preview.widthFor."""
    p = _clamp01(pressure_norm)
    return max(0.1, size * (1 - STROKE_THINNING + 2 * STROKE_THINNING * p))


# ── reading ──────────────────────────────────────────────────────────────────

def parse_svg(text: str) -> tuple[dict, float, float]:
    """
    Parse plot-SVG text. Returns (recording, viewport_w, viewport_h).
    Raises ValueError if it carries no draw2axi recording.
    """
    m = _METADATA_RE.search(text)
    if not m or not m.group(1).strip():
        raise ValueError("no <metadata> recording found — not a draw2axi plot SVG")
    meta = (m.group(1)
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
    rec = json.loads(meta)
    if rec.get("format") != "draw2axi-recording":
        raise ValueError(f"unexpected recording format: {rec.get('format')!r}")

    sm = _SVG_SIZE_RE.search(text)
    if sm:
        vw, vh = float(sm.group(1)), float(sm.group(2))
    else:
        # Fall back to the largest per-stroke canvas.
        cws = [s.get("canvasWidth")  or 0 for s in rec.get("strokes", [])]
        chs = [s.get("canvasHeight") or 0 for s in rec.get("strokes", [])]
        vw, vh = (max(cws, default=0) or 1.0), (max(chs, default=0) or 1.0)
    return rec, vw, vh


def load_svg(path) -> tuple[dict, float, float]:
    """Read a plot SVG file. Returns (recording, viewport_w, viewport_h)."""
    return parse_svg(Path(path).read_text(encoding="utf-8", errors="replace"))


# ── writing ──────────────────────────────────────────────────────────────────

def _grey(color: dict | None) -> tuple[str, float]:
    """A stroke's recorded colour as a grey of the same luminance, plus its alpha."""
    c = color or {}
    lum = 0.2126 * c.get("r", 0.0) + 0.7152 * c.get("g", 0.0) + 0.0722 * c.get("b", 0.0)
    v = round(255 * _clamp01(lum))
    return f"#{v:02x}{v:02x}{v:02x}", float(c.get("a", 1.0))


def _raw_parts(rec: dict) -> list[str]:
    """Each recorded stroke, per-segment widths from pressure, in its greyscale colour."""
    parts = []
    for s in rec.get("strokes", []):
        size = s.get("drawingWidth", DEFAULT_DRAWING_WIDTH)
        pts = s.get("points") or []
        if not pts:
            continue
        grey, alpha = _grey(s.get("color"))
        opacity = f' opacity="{alpha:.3g}"' if alpha < 1.0 else ""
        if len(pts) == 1:
            x, y, praw = pts[0][1], pts[0][2], pts[0][3]
            r = width_for(size, praw / OSC_PRESSURE_MAX) / 2
            parts.append(f'  <circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.3f}" fill="{grey}"{opacity}/>')
            continue
        parts.append(f'  <g stroke="{grey}" fill="none" stroke-linecap="round"{opacity}>')
        for a, b in zip(pts, pts[1:]):
            pa = a[3] / OSC_PRESSURE_MAX
            pb = b[3] / OSC_PRESSURE_MAX
            w = width_for(size, (pa + pb) / 2.0)
            parts.append(f'    <path d="M{a[1]:.2f},{a[2]:.2f}L{b[1]:.2f},{b[2]:.2f}"'
                         f' stroke-width="{w:.3f}"/>')
        parts.append('  </g>')
    return parts


def _layer_parts(strokes: list, color: str) -> list[str]:
    """A derived layer (optimized / effect): strokes of [x, y, pressure] in canvas units."""
    parts = []
    for s in strokes:
        pts, size = s["points"], s["size"]
        if not pts:
            continue
        if len(pts) == 1:
            x, y, p = pts[0]
            parts.append(f'  <circle cx="{x:.2f}" cy="{y:.2f}" r="{width_for(size, p) / 2:.3f}" fill="{color}"/>')
            continue
        parts.append(f'  <g stroke="{color}" fill="none" stroke-linecap="round">')
        for a, b in zip(pts, pts[1:]):
            w = width_for(size, (a[2] + b[2]) / 2.0)
            parts.append(f'    <path d="M{a[0]:.2f},{a[1]:.2f}L{b[0]:.2f},{b[1]:.2f}"'
                         f' stroke-width="{w:.3f}"/>')
        parts.append('  </g>')
    return parts


def build_svg(rec: dict, viewport_w: float, viewport_h: float, *,
              include_raw: bool = True, layers: dict | None = None) -> str:
    """
    Serialize a recording to a plot SVG: white paper, the raw strokes in
    greyscale, and optionally the derived layers ({"optimized": [...],
    "effect": [...]}) on top in their accent colours. The recording goes in
    <metadata> only when the raw layer is included — a layers-only export is a
    plain drawing, not something that can be imported and plotted.
    """
    layers = layers or {}
    parts = []
    if include_raw:
        parts += _raw_parts(rec)
    parts += _layer_parts(layers.get("optimized", []), OPTIMIZED_COLOR)
    parts += _layer_parts(layers.get("effect", []), EFFECT_COLOR)

    head = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{viewport_w:.0f}" height="{viewport_h:.0f}">']
    if include_raw:
        meta = (json.dumps(rec, separators=(",", ":"))
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        head.append(f'  <metadata>{meta}</metadata>')
    head.append(f'  <rect width="{viewport_w:.0f}" height="{viewport_h:.0f}" fill="{PAPER_COLOR}"/>')
    return "\n".join([*head, *parts, '</svg>'])


# ── files ────────────────────────────────────────────────────────────────────

def write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file and a rename, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def unique_path(folder: Path, name: str) -> Path:
    """folder/name, or folder/name_1, name_2 … — never an existing file."""
    stem, ext = os.path.splitext(name)
    candidate, n = folder / name, 1
    while candidate.exists():
        candidate, n = folder / f"{stem}_{n}{ext}", n + 1
    return candidate


def timestamped_name(prefix: str = "drawing") -> str:
    return time.strftime(f"{prefix}-%Y-%m-%d_%H-%M-%S.svg")


# ── recording a live session ─────────────────────────────────────────────────

class Recorder:
    """
    Records the session from the preview's message stream (the `point`,
    `pen_up`, `layer` and `canvas_size` messages the page also receives), with
    the same rules the page's JavaScript used when it did this job:

      * a new stroke starts on the first point after a pen_up;
      * a stroke's metadata (tool, width, colour, canvas) is latched at its
        first point;
      * an imported drawing arrives as ordinary points, so it becomes part of
        the drawing, exactly as if it had been drawn on the iPad.

    It also keeps every message that built the picture, so a page that
    (re)connects can be brought up to date by replaying them (`messages()`).
    Thread-safe: messages arrive from the OSC, plotter and replay threads.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self._strokes: list = []           # completed recorded strokes
        self._cur: dict | None = None      # the open stroke, if any
        self._layers = {"optimized": [], "effect": []}   # strokes of [x, y, pressure] in canvas units
        self._layer_cur = {"optimized": None, "effect": None}
        self._log: list = []               # messages that rebuild the page's picture
        self.dirty = False                 # changed since the last autosave

    # -- input ---------------------------------------------------------------

    def on_message(self, msg: dict) -> None:
        t = msg.get("type")
        with self._lock:
            if t == "point":
                self._on_point(msg)
            elif t == "pen_up":
                self._on_pen_up(msg)
            elif t == "layer":
                self._on_layer(msg)
                self._log.append(msg)
            elif t == "canvas_size":
                self._log.append(msg)

    def _on_point(self, msg):
        if self._cur is None:
            self._cur = {
                "tool":         msg.get("tool") or "pen",
                "drawingWidth": msg.get("drawingWidth"),
                "color":        {k: msg.get(k) for k in ("r", "g", "b", "a")},
                "canvasWidth":  msg.get("canvasWidth"),
                "canvasHeight": msg.get("canvasHeight"),
                "points":       [],
            }
        self._cur["points"].append([msg["t"], msg["x"], msg["y"], msg["pressureRaw"]])
        self._log.append(msg)
        self.dirty = True

    def _on_pen_up(self, msg):
        if self._cur is not None:
            if self._cur["points"]:
                self._strokes.append(self._cur)
            self._log.append(msg)
            self.dirty = True
        self._cur = None

    def _on_layer(self, msg):
        # Mirrors the page's handleLayer, in canvas units instead of screen pixels.
        name = msg.get("layer")
        if name not in self._layers:
            return
        kind = msg.get("kind")
        if name == "optimized" and kind == "moveto":
            self._finish_layer("effect")
        if kind in ("penup", "dot_dwell"):
            self._finish_layer(name)
            return
        size = msg.get("width") or msg.get("drawingWidth") or DEFAULT_DRAWING_WIDTH
        if kind == "moveto":
            self._finish_layer(name)
            self._layer_cur[name] = {"size": size, "points": []}
            return
        cur = self._layer_cur[name]
        if cur is None:
            cur = self._layer_cur[name] = {"size": size, "points": []}
        cur["size"] = size
        cur["points"].append([msg["x"], msg["y"], msg.get("pressure", 0.0)])
        self.dirty = True

    def _finish_layer(self, name):
        cur = self._layer_cur[name]
        if cur and cur["points"]:
            self._layers[name].append(cur)
        self._layer_cur[name] = None

    # -- output --------------------------------------------------------------

    def is_empty(self) -> bool:
        with self._lock:
            return not self._strokes and not (self._cur and self._cur["points"])

    def recording(self) -> dict:
        """The session so far, including a stroke still in progress."""
        with self._lock:
            strokes = list(self._strokes)
            if self._cur and self._cur["points"]:
                strokes.append(self._cur)
            return {"format": "draw2axi-recording", "version": 1,
                    "strokes": json.loads(json.dumps(strokes))}

    def layers(self) -> dict:
        with self._lock:
            out = {}
            for name, done in self._layers.items():
                cur = self._layer_cur[name]
                out[name] = json.loads(json.dumps(done + ([cur] if cur and cur["points"] else [])))
            return out

    def viewport(self) -> tuple[float, float]:
        """The canvas size the drawing was made on (the last stroke's), for the SVG."""
        rec = self.recording()
        for s in reversed(rec["strokes"]):
            if s.get("canvasWidth") and s.get("canvasHeight"):
                return float(s["canvasWidth"]), float(s["canvasHeight"])
        return 440.0, 956.0

    def svg(self, include_raw: bool = True, optimized: bool = False, effect: bool = False) -> str:
        layers = self.layers()
        chosen = {k: v for k, v in layers.items() if (k == "optimized" and optimized) or (k == "effect" and effect)}
        vw, vh = self.viewport()
        return build_svg(self.recording(), vw, vh, include_raw=include_raw, layers=chosen)

    def messages(self) -> list:
        """Every message that built the current picture, in order."""
        with self._lock:
            return list(self._log)

    def clear(self) -> None:
        with self._lock:
            self._reset()
