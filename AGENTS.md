# Project Pantograph — Context Document

This document is the full technical context for Project Pantograph. Hand it to
a new Claude session (e.g. Claude Code) to resume work without re-explaining
the project from scratch.

---

## 1. Project goal

**Live drawing recreation.** An artist draws on an iPad with an Apple Pencil,
and an AxiDraw pen plotter physically recreates the drawing in real time on
paper. The system is framed as a "collaborative drawing machine" — the human
leads, the machine follows.

The pipeline is:

```
Apple Pencil → iPad (iDraw OSC app) → Wi-Fi (OSC) → Python → AxiDraw
```

---

## 2. What OSC is

OSC = Open Sound Control. A lightweight UDP network message protocol common in
creative coding (TouchDesigner, Max/MSP, etc.). iDraw OSC packages Apple Pencil
position data into OSC messages and broadcasts them over Wi-Fi to a configured
IP and port. Python receives them directly — TouchDesigner is not required and
is not part of this pipeline.

---

## 3. Input: iDraw OSC

iDraw OSC is an iPad app that reads Apple Pencil input and streams it as
individual scalar OSC messages. Each "point" arrives as a burst of separate
messages, not as a single bundled packet.

**Current OSC port: 8800**

In the iDraw OSC app, set:
- IP → computer's local Wi-Fi IP (find with `ipconfig` on Windows, look for
  Wi-Fi adapter IPv4)
- Port → 8800

### 3.1 Position messages

| Address    | Value                                      |
|------------|--------------------------------------------|
| `/x`       | canvas x coordinate (e.g. 67.67)          |
| `/y`       | canvas y coordinate (e.g. 77.33)          |
| `/pressure`| pen pressure (observed always 1.0 so far) |
| `/aspectX` | normalized centered x ≈ x/canvasWidth - 0.5  — **not used** |
| `/aspectY` | normalized centered y ≈ 0.5 - y/canvasHeight — **not used** |

`/x` and `/y` are in canvas pixel coordinates. `/aspectX` and `/aspectY` are
derived from them and are ignored in favor of raw x/y.

The burst order observed is: `/x` → `/y` → `/pressure` → `/aspectX` →
`/aspectY`. The code emits a point on each `/y` arrival (treating it as the
last meaningful field in the burst).

### 3.2 Canvas dimensions

| Address        | Example value |
|----------------|---------------|
| `/canvasWidth` | 440.0         |
| `/canvasHeight`| 956.0         |

These arrive repeatedly as part of the state block. The coordinate mapping is
recalculated whenever either value updates.

### 3.3 Tool and style state

| Address         | Meaning                                  |
|-----------------|------------------------------------------|
| `/r /g /b /a`   | current drawing color (RGBA, 0.0–1.0)   |
| `/pen`          | one-hot tool flag                        |
| `/pencil`       | one-hot tool flag                        |
| `/marker`       | one-hot tool flag                        |
| `/monoline`     | one-hot tool flag                        |
| `/crayon`       | one-hot tool flag                        |
| `/fountainPen`  | one-hot tool flag                        |
| `/waterColor`   | one-hot tool flag                        |
| `/bitmapEraser` | one-hot tool flag                        |
| `/vectorEraser` | one-hot tool flag                        |
| `/drawingWidth` | current brush width (e.g. 2.68)         |
| `/eraserWidth`  | current eraser width                     |

Tool flags are one-hot: exactly one is 1.0 at a time, the rest are 0.0.
Drawable tools (produce a physical mark): pen, pencil, marker, monoline,
crayon, fountainPen, waterColor. Eraser tools are tracked in state but
currently skipped — no physical erasing on the AxiDraw.

### 3.4 Stroke boundary inference

iDraw OSC does **not** send explicit strokeStart / strokeEnd / penUp / penDown
messages. Stroke boundaries are inferred by time gap: if no new point arrives
within `PEN_UP_TIMEOUT_SEC` (currently 0.15s), that is treated as a pen lift.

---

## 4. Current file structure

```
project/
  listen_to_idraw.py   ← main entry point; OSC receiver + full pipeline
  preview.py           ← live browser preview server (called by listen_to_idraw)
  CONTEXT.md           ← this file
```

Run with:
```
python listen_to_idraw.py
```

Dependencies:
```
pip install python-osc websockets rdp
```

pyaxidraw is installed separately — see https://axidraw.com/doc/py_api/

---

## 5. listen_to_idraw.py — architecture

### 5.1 Configuration block (top of file)

```python
OSC_PORT            = 8800
PAPER_WIDTH_IN      = 9.0    # plotting area width in inches
PAPER_HEIGHT_IN     = 12.0   # plotting area height in inches
AXIDRAW_ENABLED     = False  # True = live plot, False = dry run (print moves only)
PEN_UP_TIMEOUT_SEC  = 0.15   # silence gap that triggers pen lift
RDP_TOLERANCE_IN    = 0.01   # RDP simplification threshold in paper inches
```

Change `PAPER_WIDTH_IN` / `PAPER_HEIGHT_IN` when switching paper sizes.
Flip `AXIDRAW_ENABLED` to `True` only after dry-run output looks correct.

### 5.2 State dict

A single `state` dict is updated by every incoming OSC message:

```python
state = {
    "x", "y", "pressure",          # current position
    "r", "g", "b", "a",            # current color
    "tool",                         # active tool name (string)
    "canvasWidth", "canvasHeight",  # canvas dimensions
    "drawingWidth", "eraserWidth",  # brush sizes
    "_last_point_time",             # timestamp of last received point
    "_pen_is_down",                 # bool: currently in a stroke
    "_mapping",                     # computed coordinate mapping dict
}
```

### 5.3 Coordinate mapping

Canvas coordinates are mapped to paper inches with aspect-ratio letterboxing:
the canvas is scaled to fit inside the paper area without stretching, and
centered. Leftover area becomes margin.

```python
def compute_mapping(canvas_w, canvas_h) -> dict:
    # returns: draw_w, draw_h, margin_x, margin_y, scale_x, scale_y
```

```python
def canvas_to_paper(x, y, mapping) -> (float, float):
    # returns paper x, y in inches
```

The mapping is recomputed every time `/canvasWidth` or `/canvasHeight` arrives,
and printed to the console:
```
[mapping] canvas 440×956px → draw area 5.52"×12.00" on 9"×12" paper
          (margins: x=1.74" y=0.00")
```

This means switching tablets (different canvas dimensions) just works.

### 5.4 Stroke buffering and RDP simplification

Raw canvas points accumulate in `_current_stroke_canvas` (a list of (x, y)
tuples) while a stroke is in progress.

When pen-up is detected, `_finalize_stroke()` runs:
1. Converts all buffered canvas points to paper inches
2. Runs RDP simplification (`rdp(points, epsilon=RDP_TOLERANCE_IN)`)
3. Pushes the simplified stroke onto `_stroke_queue`

**Why buffer the full stroke before simplifying:**
RDP needs the complete stroke to make good decisions. A point that looks
redundant mid-stroke may be important for the curve's shape. Per-point online
simplification produces worse results.

**Why not send points directly to AxiDraw from the OSC thread:**
AxiDraw motor calls are blocking. Calling them from the OSC thread would cause
incoming messages to queue or be dropped while the plotter moves. A separate
plotter thread with a queue decouples reception from execution.

**Tuning RDP_TOLERANCE_IN:**
- Too small (e.g. 0.001") → minimal simplification, plotter lags
- Too large (e.g. 0.05") → curves become visibly polygonal
- 0.01" (≈ 0.25mm) is a reasonable starting point

The console prints reduction stats per stroke:
```
[stroke] 847 pts → 23 pts after RDP (97% reduction)
```

### 5.5 Plotter thread

A background `threading.Thread` blocks on `_stroke_queue` and plots each stroke
serially (one complete stroke before the next):

```
pen up → moveto first point → pen down → lineto each subsequent point → pen up
```

Serial execution is intentional: simpler, predictable, no concurrency issues.
The plotter will lag behind a fast artist; strokes are queued, never dropped.

In dry-run mode (`AXIDRAW_ENABLED = False`), all moves are printed to console
instead of sent to the AxiDraw.

### 5.6 OSC dispatcher

All known addresses are explicitly mapped. `/aspectX` and `/aspectY` are mapped
to a no-op handler to suppress noise. Unknown addresses print to console so new
fields from iDraw OSC can be discovered.

---

## 6. preview.py — architecture

Serves a live browser-based drawing preview. Called by `listen_to_idraw.py`
via `preview.start()`, which launches two background threads:

| Server    | Port | Purpose                              |
|-----------|------|--------------------------------------|
| HTTP      | 5000 | Serves the HTML page                 |
| WebSocket | 5001 | Streams point/pen-up events to page  |

Open http://localhost:5000 in a browser. The page auto-reconnects if Python
restarts.

### 6.1 Message types (Python → browser)

| type          | fields                                              |
|---------------|-----------------------------------------------------|
| `canvas_size` | width, height                                       |
| `point`       | x, y, pressure, r, g, b, a, tool, drawingWidth, canvasWidth, canvasHeight |
| `pen_up`      | (none)                                              |
| `tool_change` | tool                                                |

### 6.2 Canvas rendering

- Canvas aspect ratio is set from `canvas_size` message on first arrival only
  (resizing a canvas element clears it — this was a bug that was fixed)
- Each point connects to the previous with a line segment using current color
  and scaled line width
- Strokes persist on the canvas until the "clear" button is pressed
- Status bar shows: connection state, point count, stroke count, pressure

### 6.3 Known issue / next priority

**Preview quality needs improvement.** The current rendering is functional but
basic — raw point-to-point line segments with no smoothing. The next planned
work is improving the visual quality of the preview canvas rendering.

Possible directions:
- Spline / bezier smoothing between points
- Variable line width based on pressure or speed
- Velocity-based tapering at stroke start/end
- Anti-aliasing or composite operations for a more natural ink feel

---

## 7. AxiDraw notes

- Work area: 9×12"
- Python API: pyaxidraw (`ad.options.units = 2` for inches)
- Key calls: `ad.penup()`, `ad.pendown()`, `ad.moveto(x, y)`, `ad.lineto(x, y)`
- Not yet tested live — dry run output should be validated first

---

## 8. Alternatives explored (not the current path)

These were considered earlier but set aside in favor of iDraw OSC:

- **Procreate**: no accessible live stroke API
- **PencilKit / UIKit**: viable fallback requiring a custom Swift iPad app;
  would give richer data (altitude, azimuth, strokeID, phase) but 2–3 week
  build timeline
- **Browser / WebSocket**: iPad Safari canvas → JS touch events → WebSocket →
  Python; simpler but less precise than native Apple Pencil data
- **Wacom + Krita**: alternate input hardware path; still viable if iPad OSC
  becomes limiting
- **Physical paper over iPad**: artist draws on paper laid over iPad, capturing
  capacitance — interesting hybrid but not pursued

iDraw OSC is the current and active approach.

---

## 9. Immediate next step

Improve the quality of the browser preview in `preview.py`. The current
rendering uses raw point-to-point line segments. The goal is a smoother,
more visually faithful representation of what the artist is drawing.
