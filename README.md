# Project Pantograph

An artist draws on an iPad with an Apple Pencil; an AxiDraw pen plotter
recreates the drawing on paper, live, while they draw. A browser page shows
what is happening and exposes every knob that matters.

*(The live-drawing pipeline itself is internally called* `draw2axi` *— that
name shows up throughout the code and the rest of this document.)*

```
Apple Pencil → iPad (iDraw OSC) → Wi-Fi/UDP → Python → AxiDraw
                                       │
                                       └──→ browser preview (localhost:5000)
```

Points are streamed to the plotter as they arrive — the pen starts moving
mid-stroke rather than waiting for the stroke to finish. When the plotter falls
behind, an optimizer thins the pending queue so it can catch up.

New here? **[Setup](#setup)** below walks through getting this running from
scratch, with or without an AxiDraw.

---

## Setup

Installs Python, the code, and iDraw OSC, then connects your iPad to this
program. No prior Python or command-line experience needed. If something
doesn't work, see **Troubleshooting the connection** under Path A — it
applies to both paths.

### What you'll need

- A computer (Windows or Mac) to run this program.
- An iPad (or iPhone) with Apple Pencil support, for the iDraw OSC app.
- Both devices on the same Wi-Fi network — or see Tailscale below if that's
  not possible (dorm/campus/shared networks often block it).
- Optionally, an [AxiDraw](https://axidraw.com) pen plotter. No AxiDraw yet?
  You can still draw, preview live, and export a plottable SVG to run later —
  see **Path B** below.

### 1. Get the code

- **Download ZIP (easiest)** — go to
  [github.com/MarcDunand/Project-Pantograph](https://github.com/MarcDunand/Project-Pantograph),
  click the green **Code** button → **Download ZIP**, then extract it
  somewhere you'll remember (e.g. your Desktop).
- **git clone** — open a terminal (Windows: PowerShell, Mac: Terminal),
  anywhere, e.g. your Desktop, and run:
  ```
  git clone https://github.com/MarcDunand/Project-Pantograph.git
  ```

### 2. Install Python

Skip this if you already have Python 3.10 or newer — check first:

- **Windows**: open PowerShell (search "PowerShell" in the Start menu) and
  run `python --version`
- **Mac**: open Terminal (search "Terminal" in Spotlight) and run
  `python3 --version`

If that prints 3.10 or higher, move on. Otherwise install it from
[python.org/downloads](https://www.python.org/downloads/). **On Windows,
tick "Add python.exe to PATH"** on the installer's first screen — it's easy
to miss and everything below depends on it.

### 3. Open a terminal in the project folder

- **Windows**: open the extracted/cloned folder in File Explorer, click the
  address bar, type `powershell`, and press Enter.
- **Mac**: open Terminal, type `cd ` (note the trailing space), then drag the
  project folder from Finder into the Terminal window and press Enter.
- If you used `git clone` above, you already have a terminal open — just run
  `cd Project-Pantograph` in it instead.

Run every command below from this terminal, in this folder.

### 4. Install the libraries

In the terminal, run:

```
pip install python-osc websockets rdp numpy
```

(Mac: use `pip3` instead of `pip` if `pip` isn't found.) This covers both
paths below.

If you have an AxiDraw, you also need `pyaxidraw` — install it by following
[axidraw.com/doc/py_api](https://axidraw.com/doc/py_api/). That page is the
authoritative source and changes with AxiDraw's own software, so it isn't
duplicated here.

### 5. Install iDraw OSC on your iPad

Search **"iDraw OSC"** in the App Store and install it — this is the app
that streams your Apple Pencil strokes to the computer.

### 6. Choose your path

- **Have an AxiDraw, set up and ready?** → **Path A** — `listen_to_idraw.py`
  plots live as you draw.
- **No AxiDraw (yet)?** → **Path B** — `iDraw_to_svg/listen_to_idraw_remote.py`
  gives you the same live preview and lets you download a plottable SVG to
  run on an AxiDraw later.

### Path A — with an AxiDraw

1. Connect the AxiDraw over USB; make sure its software is set up per
   [axidraw.com/doc/py_api](https://axidraw.com/doc/py_api/).
2. In the terminal, run:
   ```
   python listen_to_idraw.py
   ```
   A browser tab opens automatically at http://localhost:5000 — that's your
   live preview.
3. **Connect iDraw OSC to it.** On the iPad, open iDraw OSC and set:
   - **IP** → your computer's Wi-Fi IP address
     - Windows: PowerShell → run `ipconfig` → under "Wireless LAN adapter
       Wi-Fi", read **IPv4 Address**
     - Mac: System Settings → Wi-Fi → your network → Details (or Terminal →
       run `ipconfig getifaddr en0`)
   - **Port** → `8800`
4. Draw. Strokes appear in the browser preview and the AxiDraw starts moving.

**Troubleshooting the connection**

- Confirm both devices are on the **same Wi-Fi network** — not one on Wi-Fi
  and the other on cellular data or a different network.
- Double check the IP — it can change whenever a device reconnects to Wi-Fi,
  so re-check it if it's been a while since you last looked.
- The green dot in the preview only means the **browser** is talking to the
  Python program on your own computer. It lights up even if nothing from the
  iPad has ever arrived — it is not proof the iPad↔computer link works.
- In the terminal, run `python listen_to_idraw.py --raw-osc` and draw a
  stroke:
  - **Nothing prints** → no data is reaching the computer — a network
    problem, see below.
  - **`/x /y /pressure` messages print but nothing draws** → the connection
    is fine, the bug is elsewhere.
- **On a dorm, campus, or other shared/managed network**: these often block
  devices from reaching each other directly, even on the same Wi-Fi. This is
  the most common cause of "nothing shows up." If the checks above don't fix
  it, use **Tailscale**:
  1. Install Tailscale on the computer from
     [tailscale.com/download](https://tailscale.com/download), and on the
     iPad from the App Store.
  2. Sign in with the **same account** on both.
  3. On the computer, find its Tailscale IP: in PowerShell/Terminal, run
     `tailscale ip -4` (prints something like `100.x.y.z`).
  4. In iDraw OSC, use that address instead of the Wi-Fi IP. Port stays
     `8800`.
  5. Works on any network — dorm, coffee shop, home — since the two devices
     no longer need to reach each other directly.

**Using the preview**

- **settings** (☰, top left) — flip/tilt the output, pen up/down positions,
  variable pressure, path optimization, home the AxiDraw. Full reference:
  [Browser controls](#browser-controls-localhost5000) below.
- **download** — save the current drawing as PNG or SVG, written to
  `saved_drawings/`.
- **effects** (✦, top right) — turn on postprocessing effects like zigzag or
  hatching. Full reference: [Post-processing effects](#post-processing-effects)
  below.
- **Ctrl+C** in the terminal lifts the pen and disengages the XY motors so
  the carriage can be pushed home by hand.

### Path B — no AxiDraw

In the terminal, run:

```
python iDraw_to_svg/listen_to_idraw_remote.py
```

Everything above in Path A about connecting iDraw OSC (IP/port) and
troubleshooting the connection — including Tailscale — applies the same
here. What differs is what the program does with the drawing afterward.

This is `listen_to_idraw.py` with every plotter-specific piece removed: no
AxiDraw connection, no plotter thread, no path optimizer, no post-processing
effects, no paper coordinate mapping, no pen/tilt/flip controls. It produces
a downloadable SVG (**download → svg** in the preview). Take that file to a
computer with an AxiDraw set up and load it with the full version's
**plot svg** button — it plots with whatever paper size, effects, and
settings you choose then.

### Other programs, and going deeper

That's setup for both programs. The rest of this README is technical
reference:

- **[Files](#files)** — what every file in the repo does
- **[Pipeline](#pipeline)** — how coordinates, strokes, and the plot queue
  actually work
- **[Post-processing effects](#post-processing-effects)** — the effects
  panel in depth, and how to write your own
- **[Offline tools](#offline-tools)** — `svg_transform.py`, `dot_healer.py`,
  and other one-off scripts you run by hand on a finished SVG
- **[iDraw_to_svg internals](#idraw_to_svg-no-axidraw-internals)** —
  technical notes on Path B, for anyone maintaining it

---

## Files

*(From here down: technical reference — file layout and internals, for
anyone running the less common tools, maintaining this code, or curious how
it works. If you just want to draw, [Setup](#setup) above is everything you
need.)*

| File | What it is |
|------|------------|
| `listen_to_idraw.py` | Main entry point. OSC receiver, coordinate mapping, plot queue, plotter thread, adaptive optimizer, SVG replay. |
| `preview.py` | Live browser preview + control panel. HTTP on 5000, WebSocket on 5001. Also the SVG/PNG exporter. |
| `postprocess.py` | Post-processing effects — transforms over the plot command stream. Add new effects here. |
| `svg_transform.py` | Offline GUI (tkinter): flip / filter an exported SVG, write a new one. |
| `dot_healer.py` | Offline CLI: rejoin strokes that a fast pen tore into a trail of dots. |
| `iDraw_to_svg/` | Standalone no-AxiDraw version — record now, plot later. See `iDraw_to_svg/README.md`. |
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
`listen_to_idraw.py` (`_replay_recording`), `svg_transform.py`,
`dot_healer.py`, and both files in `iDraw_to_svg/`. **Change one, change all
of them.**

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
```

`dot_healer` fixes strokes that a fast pen tore into a run of one-point strokes
(the timeout fired *between* consecutive points): it finds `line, dots…, line`
runs and concatenates them back into one continuous stroke, with guards so
deliberate tap-dots are left alone.

---

## iDraw_to_svg (no-AxiDraw) internals

What it is and how to run it: [Path B](#path-b--no-axidraw) above.

- Only needs `python-osc` and `websockets` — no `rdp`, `numpy`, or `pyaxidraw`.
- Its recording output must stay byte-compatible with what the full version
  reads back — see the sync warning in `iDraw_to_svg/README.md`.
