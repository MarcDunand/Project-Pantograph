# draw2axi

An artist draws on an iPad with an Apple Pencil; an AxiDraw pen plotter
recreates the drawing on paper, live, while they draw. A browser page shows
what is happening and exposes every knob that matters.

```
Apple Pencil → iPad (iDraw OSC) → Wi-Fi/UDP → Python → AxiDraw
                                       │
                                       └──→ browser preview (localhost:5000)
```

Points are streamed to the plotter as they arrive — the pen starts moving
mid-stroke rather than waiting for the stroke to finish. When the plotter falls
behind, an optimizer thins the pending queue so it can catch up.

---

## Run

```
pip install python-osc websockets rdp numpy
python listen_to_idraw.py
```

`pyaxidraw` installs separately — see https://axidraw.com/doc/py_api/

In iDraw OSC on the iPad, set **IP** to your computer's Wi-Fi IPv4 (`ipconfig`,
Wi-Fi adapter) and **Port** to `8800`. The preview opens automatically at
http://localhost:5000.

| Flag | Effect |
|------|--------|
| *(none)* | plot live on the AxiDraw |
| `--dry-run` | compute and print every move, never touch USB |
| `--raw-osc` | print each incoming OSC message verbatim instead of pipeline output |

Ctrl+C lifts the pen and disengages the XY motors so the carriage can be pushed
home by hand.

---

## Files

| File | What it is |
|------|------------|
| `listen_to_idraw.py` | Main entry point. OSC receiver, coordinate mapping, plot queue, plotter thread, adaptive optimizer, SVG replay. |
| `preview.py` | Live browser preview + control panel. HTTP on 5000, WebSocket on 5001. Also the SVG/PNG exporter. |
| `postprocess.py` | Post-processing effects — transforms over the plot command stream. Add new effects here. |
| `svg_transform.py` | Offline GUI (tkinter): flip / filter an exported SVG, write a new one. |
| `dot_healer.py` | Offline CLI: rejoin strokes that a fast pen tore into a trail of dots. |
| `log_to_svg.py` | Offline CLI: rebuild a plottable SVG from a saved terminal log. |
| `remote/` | Standalone no-AxiDraw version — record now, plot later. See `remote/README.md`. |
| `saved_drawings/` | Where preview downloads land (PNG/SVG), written by the server, not the browser. |
| `AGENTS.md` | Project background and the iDraw OSC message reference. |
| `MEETINGS.html` | Meeting history. |

---

## Pipeline

### Coordinates

Canvas pixels → paper inches → physical AxiDraw inches, in one step
(`canvas_to_physical`): aspect-preserving letterbox onto the paper, then a 90°
landscape rotation, then optional flip H/V. Flip is applied last so every
downstream setting (tilt compensation, effects) always sees the same physical
axes. The mapping is recomputed whenever `/canvasWidth` or `/canvasHeight`
arrives, so switching tablets just works.

Paper size lives at the top of `listen_to_idraw.py` (`PAPER_WIDTH_IN`,
`PAPER_HEIGHT_IN`; currently 8.5 × 11).

### Stroke boundaries

iDraw OSC sends no pen-up/pen-down messages. A gap of `PEN_UP_TIMEOUT_SEC`
(0.15s) with no new point is treated as a pen lift, enforced both inline and by
a watchdog thread so the last stroke of a session always closes. A stroke with
no movement in it is plotted as a dot (`dot_dwell`).

### Plot command stream

Everything downstream speaks one command tuple format,
`(enqueue_time, kind, *args)`:

```
(t, "moveto",  x, y)              pen-up travel to stroke start
(t, "pendown", pressure, x, y)    lower pen (x/y carried for tilt comp)
(t, "lineto",  x, y, pressure)    pen-down move
(t, "dot_dwell")                  brief pause, for single-tap dots
(t, "penup")                      lift pen
(t, "home")                       travel to (0,0), pen up
```

Commands go onto a `deque` (not a `Queue`) so the optimizer can reach in and
rewrite pending runs. A dedicated plotter thread drains it, so blocking motor
calls never stall OSC reception.

### Adaptive optimization

Lag is measured from the age of the oldest pending command. Two layers respond
to it, both driven by the same `_compute_effective_scale(lag)` ramp:

1. **Distance filter** (upstream, in `_emit_point`) — drops incoming points
   closer than `min_dist` to the last one; the threshold grows with lag.
2. **RDP on the backlog** (downstream, `_optimizer_thread` at 10 Hz) — reclaims
   points already queued while the plotter is busy on earlier moves. Epsilon
   scales from `min_dist` up to 15× it.

With **limit lag** on, aggressiveness keeps climbing past the threshold at 2×
rate to chase the plotter back down; off, it caps at the configured
aggressiveness for a predictable ceiling.

### Pressure

Raw iDraw pressure is normalised by `OSC_PRESSURE_MAX` (≈4.167). With **variable
pressure** on, normalised pressure maps between the min and max pen-down servo
positions, updated mid-stroke at the configured rate.

iDraw intermittently sends a placeholder raw value of exactly `1.0`
(normalising to ≈0.24). Those points are buffered and given pressures linearly
interpolated between their good neighbours; a stroke where *every* point is
spurious is discarded before `pendown` is ever issued, so no ink lands.

### Tilt compensation

If the drawing surface isn't level, **x tilt** / **y tilt** (degrees) nudge
`pen_pos_down` by position, via `TILT_SERVO_PER_INCH`. x tilt corrects along the
machine's physical short axis and y tilt along the long axis, matching how X/Y
read on the machine rather than the internal landscape naming.

---

## Browser controls (localhost:5000)

**Canvas** — four stacked layers in one fixed colour scheme: raw OSC input
(grey), the in-progress stroke (transient overlay), the optimized centerline the
pen actually follows (white), and only what the effect chain adds (pale blue).
The two server-derived layers are drawn from the same commands that go to the
plotter, so the gap between grey and white *is* the thinning.

**settings** — preview viewport width/height/origin in canvas units, flip H/V,
x/y tilt, pen up / min pen down / max pen down servo positions (each with a
*test* button that moves the pen there), variable pressure + update rate,
optimizer enable / aggressiveness / min point distance / lag threshold / limit
lag, live lag readout, home AxiDraw, reset to defaults. Settings persist in
`localStorage`.

**effects** — one block per registered effect, built automatically from
`postprocess.effect_specs()`, with a live slider per tunable knob. Changing a
knob rebuilds the chain immediately. **effects only** plots just what the
effects add and drops the base centerline — for re-running a finished drawing
over a base layer already on the paper.

**download** — pick which layers to include, then PNG or SVG. Files are written
by the server into `saved_drawings/`.

**plot svg** — load an exported SVG and replay it.

---

## Post-processing effects

An effect is a transform over the plot command stream, not a point filter. Each
command is passed through the enabled effects before it lands on the deque, so
whatever they emit is still seen by the optimizer downstream. The preview always
shows the drawing as it was actually drawn — only the pen is affected.

| Effect | What it does |
|--------|--------------|
| `zigzag` | Squares off every move: full travel in x, then full travel in y, so lines come out as staircase steps. |
| `pressure_hatch` | Goes back over each finished stroke and adds perpendicular hatch marks wherever the pen was pressed past a threshold; harder press = longer mark. |
| `stroke_connector` | After each stroke, draws a line from its midpoint to an earlier stroke's midpoint, chosen at random weighted by 1/distance. |
| `mirror` | Now and then doubles a short stroke with a left–right mirrored copy of itself. |

To write one: subclass `Effect`, override only the `on_<kind>` hooks you care
about, list tunable class attributes in `PARAMS`, and register it in `REGISTRY`.
The browser panel and the `_EFFECT_SWITCHES` block pick it up from there. Full
guide in the `postprocess.py` module docstring.

Effects are applied in registry order but are not designed against each other —
turn on one at a time.

---

## Recording and replay

Every exported SVG carries a `draw2axi-recording` v1 JSON blob in `<metadata>`:
per-stroke tool, drawingWidth, color, canvas size, and points as
`[t, x, y, pressureRaw]` in that stroke's own canvas units. **The recording is
the source of truth** — the `<path>`/`<circle>` elements are cosmetic, so the
file looks right in a viewer.

Replay pushes those points back through `_emit_point` exactly as if they had
just arrived over OSC, so paper mapping, flips, tilt, the effect chain and the
optimizer all re-apply downstream. That is the point of replaying at the *input*
level: the same drawing can be plotted again with different settings and
different post-processors switched on. Idle gaps are shortened to
`REPLAY_MAX_GAP_SEC` so a drawing with long pauses doesn't take its original
wall-clock time.

Readers/writers of this format: `preview.py` (`buildRecording`, `uploadSVG`),
`listen_to_idraw.py` (`_replay_recording`), `log_to_svg.py`, `svg_transform.py`,
`dot_healer.py`, and both files in `remote/`. **Change one, change all of
them.**

---

## Offline tools

These run by hand on a finished SVG — siblings to the post-processors, not part
of the live pipeline. Each rewrites the recording and regenerates matching
visuals from it, so the picture and the plot stay in sync. Load the result with
the preview's **plot svg** button.

```
python svg_transform.py [input.svg]        # GUI: flip H/V, filter by min stroke width
python svg_transform.py --selftest f.svg   # no GUI

python dot_healer.py drawing.svg           # -> drawing_healed.svg
python dot_healer.py drawing.svg --max-gap 20 --dry-run

python log_to_svg.py session.log           # -> session.svg
```

`dot_healer` fixes strokes that a fast pen tore into a run of one-point strokes
(the timeout fired *between* consecutive points): it finds `line, dots…, line`
runs and concatenates them back into one continuous stroke, with guards so
deliberate tap-dots are left alone.

---

## Remote (no-AxiDraw) version

`remote/` is a standalone cut of the pipeline for machines with no plotter
attached: draw, preview, download SVG, plot it later on the real machine. No
AxiDraw connection, no optimizer, no effects, no paper mapping. It only needs
`python-osc` and `websockets`.

Its recording output must stay byte-compatible with what the full version reads
back — see the sync warning in `remote/README.md`.
