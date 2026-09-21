# Project Pantograph

An artist draws on an iPad with an Apple Pencil; an AxiDraw pen plotter
recreates the drawing on paper, live, while they draw. A browser page shows
what is happening and exposes every knob that matters.

*(The live-drawing pipeline itself is internally called* `draw2axi` *— that
name shows up throughout the code and the rest of this document.)*

```
Apple Pencil → iPad (iDraw OSC) → Wi-Fi/UDP → Python → AxiDraw
                                       │
                                       └──→ browser preview (127.0.0.1:5810)
```

Points are streamed to the plotter as they arrive — the pen starts moving
mid-stroke rather than waiting for the stroke to finish. When the plotter falls
behind, an optimizer thins the pending queue so it can catch up.

New here? **[Setup](#setup)** below walks through getting this running from
scratch, with or without an AxiDraw.

---

## Setup

Installs Python, the code, and iDraw OSC, then connects your iPad to this
program. No prior Python or command-line experience needed.

**An AxiDraw is optional.** Without one you still get a live drawing preview
and can export an SVG to plot later on a machine that has one. The steps
below are the same either way except where marked — look for **With an
AxiDraw:** / **No AxiDraw:** notes at those points. If the connection itself
doesn't work, see **Troubleshooting the connection** in step 8 — that part
is identical regardless of AxiDraw.

### What you'll need

- A computer (Windows or Mac) to run this program.
- An iPad (or iPhone) with Apple Pencil support, for the iDraw OSC app.
- Both devices on the same Wi-Fi network — or see Tailscale below if that's
  not possible (dorm/campus/shared networks often block it).
- Optionally, an [AxiDraw](https://axidraw.com) pen plotter. No AxiDraw yet?
  You can still draw, preview live, and export a plottable SVG to plot later
  on a machine that has one — this guide covers both, look for the **With an
  AxiDraw:** / **No AxiDraw:** notes below.

### Terminal basics (read this first if you're new to this)

A few steps below say to "open a terminal" and "run" a command. If you've
never done that:

- A **terminal** is a plain text window where you type commands instead of
  clicking buttons. On Mac it's called **Terminal**. On Windows 11 it's also
  called **Terminal**; on Windows 10, open **PowerShell** instead — same
  idea, older window.
- **"Terminal" vs "PowerShell" on Windows** — worth 20 seconds, because it
  confuses nearly everyone. They're two layers, not two choices. *Terminal*
  is the window: the tabs, the `+` button, and nothing else. *PowerShell* is
  the program running inside it that actually reads your commands. So when
  you open Terminal and the first line reads `Windows PowerShell`, nothing
  has gone wrong — that's just Terminal saying which one it started. Leave
  it on the default and every command in this guide works.
- To **run** a command: type it exactly as shown, or copy it and paste with
  **Ctrl+V** (Windows) / **Cmd+V** (Mac), then press **Enter**. Nothing
  happens until you press Enter.
- A terminal is always "in" one folder, and commands only see the files in
  that folder — e.g. `python listen_to_idraw.py` only works if the terminal
  is inside this project's folder. Step 3 below covers getting there.
- `cd` means "change directory" — it moves the terminal into a folder. For
  example, `cd Desktop` moves into a folder named "Desktop". You'll use this
  in Step 3.
- To see what's in the current folder (useful for double-checking you're in
  the right place): run `ls`. This works on both Mac and Windows. (On
  Windows, `dir` does the same thing if you've seen that one before.)

That's everything you need to follow the rest of this guide.

### 1. Get the code

- **Download ZIP (easiest)** — go to
  [github.com/MarcDunand/Project-Pantograph](https://github.com/MarcDunand/Project-Pantograph),
  click the green **Code** button → **Download ZIP**. It saves to your
  **Downloads** folder by default. Extract it:
  - **Windows**: right-click the downloaded file → **Extract All** →
    **Extract**.
  - **Mac**: double-click the downloaded file — it extracts next to itself.
  
  Then move the extracted folder somewhere you'll remember, e.g. your
  Desktop.
- **git clone**, if you already use git — open a terminal, `cd` to wherever
  you want the folder created (e.g. `cd Desktop`), and run:
  ```
  git clone https://github.com/MarcDunand/Project-Pantograph.git
  ```

### 2. Install Python

Skip this if you already have Python 3.10 or newer — check first:

- **Windows**: open a terminal (click Start, type "Terminal", press Enter —
  on Windows 10, type "PowerShell" instead) and run `python --version`
- **Mac**: open Terminal (press Cmd+Space, type "Terminal", press Enter) and
  run `python3 --version`

If that prints 3.10 or higher, move on. Otherwise install it from
[python.org/downloads](https://www.python.org/downloads/) — download and run
the installer. **On Windows, tick "Add python.exe to PATH"** on the
installer's first screen — it's easy to miss and everything below depends on
it.

After installing, close and reopen the terminal before continuing — it needs
a fresh window to pick up the new install.

**Mac note for the rest of this guide**: Python's commands on Mac are
`python3` and `pip3`, not `python`/`pip`. Wherever a command below starts
with `python` or `pip`, type `python3` / `pip3` instead.

### 3. Open a terminal in the project folder

- **Windows**: open the extracted/cloned folder in File Explorer, click once
  in the empty area of the address bar at the top, type `powershell`, and
  press Enter. This opens a terminal already "in" that folder — nothing more
  to do. (Type `powershell` here, not `terminal`: this box takes a program
  name, and there is no program actually named "terminal".)
  If you instead opened a terminal from the Start menu, it starts in your
  home folder and you'll need to walk to the project yourself: run `ls` to
  list the folders you can reach from where you are, then `cd [folder name]`
  to move into one. Repeat until you're in the `Project-Pantograph` folder.
- **Mac**: open Terminal, type `cd ` (note the trailing space, don't press
  Enter yet), then drag the project folder from Finder into the Terminal
  window — it fills in the folder's path — and press Enter.
- If you used `git clone` in Step 1, you already have a terminal open, one
  level above the new folder — just run `cd Project-Pantograph` in it
  instead of the above.

**Check it worked**: run `ls` — you should see `listen_to_idraw.py` in the
list it prints. If you don't, you're in the wrong folder — repeat the steps
above.

Run every command below from this same terminal window, in this folder.

### 4. Install the libraries

Run:

```
pip install python-osc websockets rdp numpy
```

This covers everything below.

**With an AxiDraw:** also install `pyaxidraw`, by following
[axidraw.com/doc/py_api](https://axidraw.com/doc/py_api/). That page is the
authoritative source and changes with AxiDraw's own software, so it isn't
duplicated here.
**No AxiDraw:** nothing else to install.

### 5. Install iDraw OSC on your iPad

On the iPad, open the **App Store**, search **"iDraw OSC"**, and install it
— this is the app that streams your Apple Pencil strokes to the computer.

### 6. Connect the AxiDraw (skip if you don't have one)

**With an AxiDraw:** connect it over USB; make sure its software is set up
per [axidraw.com/doc/py_api](https://axidraw.com/doc/py_api/).
**No AxiDraw:** nothing to do here — skip to the next step.

### 7. Run it

First, make sure that your terminal is in the correct folder. If you aren't,
refer to step 3 for how to get there.

In the terminal, run:
```
python listen_to_idraw.py
```
The same program works with or without an AxiDraw: it looks for one on USB,
and without one it's a live preview that records your drawing.

Either way, a browser tab opens automatically at http://127.0.0.1:5810 —
that's Pantograph. Until the iPad sends anything, it shows the iDraw setup
steps with this computer's IP and port.

### 8. Connect iDraw OSC to it

On the iPad, open iDraw OSC and set:
- **IP** → your computer's Wi-Fi IP address. Pantograph shows it in its top
  bar (**iDraw → 192.168.…**) with a Copy button.
- **Port** → `8800`

This step, and its troubleshooting below, are identical whether or not you
have an AxiDraw.

#### Troubleshooting the connection

The **iPad** button in Pantograph's top bar shows whether anything is
arriving (*Waiting*, *Receiving*, *Idle*, or *Problem*), and its
**Troubleshoot** list walks through the checks below with your real IP and
port filled in. **Copy diagnostics** there copies a summary to paste into a
message if you need help.

- Confirm both devices are on the **same Wi-Fi network** — not one on Wi-Fi
  and the other on cellular data or a different network.
- Double check the IP — it can change whenever a device reconnects to Wi-Fi,
  so re-check it if it's been a while since you last looked.
- The iPad button turns green only when something has actually arrived from
  the iPad. ("live", top right, only means the browser is talking to the
  program on your own computer.)
- In the terminal, run the same program from step 7 with `--raw-osc` added
  (e.g. `python listen_to_idraw.py --raw-osc`) and draw a stroke:
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
  3. On the computer, find its Tailscale IP: in the terminal, run
     `tailscale ip -4` (prints something like `100.x.y.z`).
  4. In iDraw OSC, use that address instead of the Wi-Fi IP. Port stays
     `8800`.
  5. Works on any network — dorm, coffee shop, home — since the two devices
     no longer need to reach each other directly.

### 9. Draw

**With an AxiDraw:** strokes appear in the browser preview and the AxiDraw
starts moving as you draw.
**No AxiDraw:** strokes appear in the browser preview only — nothing plots
yet. See step 10 for what to do with the drawing.

### 10. Using Pantograph

- **Top bar:** the **iPad** and **Plotter** buttons show each connection's
  state; click one to connect, restart the listener, or open a
  **Troubleshoot** checklist.
- **Menu bar:** **File** (New canvas, Import to canvas…, Save as…, Discard
  canvas) and **Preferences** (Plotter preferences…, Edit layout…, Heal
  dots). **Quit** lifts the pen, turns the motors off and saves the drawing.
- **The paper** in the middle is the canvas, at the paper size set in the
  plotter controller, with rulers in inches or millimetres (click the unit to
  switch). Two faint rectangles show where things sit on it: the drawing
  (turquoise) and the AxiDraw's reach (orange, with a house at its home
  corner). The chips above choose which layers show: the drawing itself, the
  pen's path (the machine's orange, drawn as the pen draws it), and what the
  effects add (blue). They change the view only — **a saved file is always the
  drawing alone**. The pen's path is the machine's record of one run, and
  effects are re-applied live from whatever is switched on, so neither is
  something you drew and neither is written to a file.
- **The sidebar** has two tabs: **Plotter controller** (the machine's controls,
  model and paper, pen positions) and **Effects**.
- **Preferences → Edit layout…** is where those rectangles move. Drag either
  one, use its ring to turn it, and the drawing's corner to resize it. Set the
  AxiDraw's rectangle to where the machine really sits on the sheet, and the
  plot lands where the screen shows it. The layout is remembered.
- **File → New canvas** starts a fresh one, offering to save first. **Save
  as…** writes an SVG wherever you choose. **Discard canvas** empties it
  without saving. The canvas is also autosaved while you draw and saved when
  you quit, so reloading or closing the browser tab loses nothing.
- **Plotter controller** (right): Walk Home, Set Home and Disengage XY
  Motors; the AxiDraw model and paper size; and the three pen positions, each
  with a Test button.
- **Plotter preferences…**: speed and acceleration, pressure updates, table
  tilt, and how hard to simplify lines when the plotter falls behind.
- **Effects**: postprocessing effects like zigzag or hatching. See
  [Post-processing effects](#post-processing-effects).
- **File → Import to canvas…** shows a saved drawing in a preview. **Import to
  canvas** plots it and adds it to the canvas, as if you had drawn it yourself
  — you can keep drawing on the iPad meanwhile, and your strokes plot after it.
  While it plots, a progress bar with **Pause** and **Cancel** appears in the
  sidebar.
- **Ctrl+C** in the terminal (or closing its window) also lifts the pen and
  releases the motors, so the carriage can be pushed home by hand.

**No AxiDraw?** Everything works except the moving plotter: the drawing is
recorded and saved just the same. Copy the SVG to a computer with an AxiDraw
and import it there (File → Import to canvas…), with whatever paper, effects
and settings you choose at that time.

### Other programs, and going deeper

That's setup. The rest of this README is technical
reference:

- **[Files](#files)** — what every file in the repo does
- **[Pipeline](#pipeline)** — how coordinates, strokes, and the plot queue
  actually work
- **[Post-processing effects](#post-processing-effects)** — the effects
  panel in depth, and how to write your own
- **[Offline tools](#offline-tools)** — the Tools tab's transforms, and
  `dot_healer.py` from the command line

---

## Files

*(From here down: technical reference — file layout and internals, for
anyone running the less common tools, maintaining this code, or curious how
it works. If you just want to draw, [Setup](#setup) above is everything you
need.)*

| File | What it is |
|------|------------|
| `listen_to_idraw.py` | Main entry point. OSC receiver, coordinate mapping, plot queue, plotter thread, adaptive optimizer, SVG replay. |
| `preview.py` | The one local server (127.0.0.1:5810, or the next free port to 5830): serves the page from `PantographApp/ui/` and the live feed on `/ws`. |
| `postprocess.py` | Post-processing effects — transforms over the plot command stream. Add new effects here. |
| `layout.py` | Where the drawing and the machine sit on the paper, and the one path from a tablet point to the machine. |
| `svg_transform.py` | Flip / minimum-width transforms on a saved SVG, from the command line. Running it with a file opens that file in Pantograph. |
| `dot_healer.py` | Offline CLI: rejoin strokes that a fast pen tore into a trail of dots. |
| `saved_drawings/` | Example drawings, kept for documentation and as test data. The app saves to `Documents/Pantograph/` instead. |
| `recording.py` | The drawing-recording format: records the live session, reads and writes plot SVGs. |
| `PantographApp/` | App-specific code: the page (`ui/`), settings, data folders, the tools, the system file windows, startup/shutdown, and the build guide. |
| `tests/` | `uv run pytest`: the recording format, stroke handling, and the whole app end to end. |
| `AGENTS.md` | Project background and the iDraw OSC message reference. |
| `MEETINGS.html` | Meeting history. |

---

## Pipeline

### Coordinates

The paper is the anchor. Two rectangles sit on it (`layout.py`): the **drawing**
(the tablet's canvas, in its own aspect ratio — movable, turnable, scalable)
and the **AxiDraw** (its travel, from the chosen model — movable and turnable,
with a home corner). A tablet point goes canvas → paper inches → the machine's
own coordinates, measured from that home corner:
`canvas_to_paper` then `paper_to_machine`. Anything outside the machine's reach
isn't drawn at all — a pen can't draw past what it can touch — so the stroke
stops at the edge and picks up again where it comes back within reach. The UI
says when part of a drawing falls outside.

The layout is a saved setting (`axi_layout`), edited in Preferences → Edit
layout. Until it's touched it fits itself to the paper: the drawing centred and
filling the sheet, the machine centred with its long axis along the paper's
long side — which is what earlier versions did with a fixed letterbox and a 90°
rotation. The page draws from the same arithmetic, so the preview shows where
the pen goes.

The paper size is a setting too (8.5 × 11 in by default) and is no longer
limited by the machine: a sheet bigger than the AxiDraw's reach is fine, and
the layout shows how much of it the machine covers.

### Stroke boundaries

iDraw OSC sends no pen-up/pen-down messages, but it does send a **state
block** (`/r /g /b /a`, the tool flags, `/canvasWidth`, `/canvasHeight`,
`/drawingWidth`, `/eraserWidth`) before every new stroke. That block, not
timing, is what ends one stroke and starts the next. Any point that arrives
while a stroke is open belongs to it, however long the pause before it, so a
fast stroke can no longer tear into dots.

Timing only *rests* the pen. After `PEN_REST_SEC` (0.15s) with no new point, a
watchdog thread lifts the pen so it doesn't sit on the paper bleeding ink, but
the stroke stays open. If the stroke carries on, the lift is taken back out of
the queue when the plotter hasn't reached it yet (the usual case, since the
plotter runs behind), or the pen goes back down where it left off.

Because iDraw sends nothing when the Pencil lifts, the *latest* stroke stays
open until the next one begins. Its pen is already off the paper, but the
effects that act at a stroke's end (zigzag's last corner, pressure hatch,
stroke connector) run when the next stroke starts. A stroke with no movement
in it is plotted as a dot (`dot_dwell`). OSC is received on a single thread, so
messages are handled in the order they arrive, which this relies on.

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

iDraw sends a placeholder raw value of exactly `1.0` (normalising to ≈0.24)
when it has no real reading. On its own, or in a short run, it's a glitch:
those points are buffered and given pressures linearly interpolated between
their good neighbours.

A run of `NO_PRESSURE_RUN` (5) or more placeholders means iDraw has no pressure
data at all (a finger, or a Pencil it isn't reading). The run is plotted at
`NO_PRESSURE_VALUE` (0.5 on the 0–1 scale). A whole stroke of placeholders too
short to reach 5 (a finger tap) gets the same value when it ends. Such strokes
used to be discarded; since 2026-09-19 they plot.

### Tilt compensation

If the drawing surface isn't level, **x tilt** / **y tilt** (degrees) nudge
`pen_pos_down` by position, via `TILT_SERVO_PER_INCH`. x tilt corrects along the
machine's physical short axis and y tilt along the long axis, matching how X/Y
read on the machine rather than the internal landscape naming.

---

## The page (127.0.0.1:5810)

The page lives in `PantographApp/ui/` (`index.html`, `app.css`, `app.js`) and
talks to the engine over one WebSocket. When it connects, the engine sends a
`hello` with everything it needs (settings, the drawing so far, statuses, the
effect list), so any number of tabs can be open and reloading loses nothing.

**Canvas** — four stacked layers: the drawing (each stroke's colour as a grey
of the same brightness, on white), the stroke in progress, the pen's path after
optimizing (orange), and only what the effect chain adds (blue). The two
engine-derived layers are drawn from the same commands that go to the plotter,
so the gap between the drawing and the orange path *is* the thinning.

**Settings** (Plotter preferences and Effects) are saved on the computer
(`settings.json` in the per-user config folder) and apply at startup, even
before a browser is open. The AxiDraw model and paper size are settings too;
paper larger than the model reaches is clamped to its travel, and neither can
change mid-plot. **Effects only** plots just what the effects add and drops the
base line, for re-running a finished drawing over a base layer already on the
paper.

**Importing a drawing** (Open file… → Import to live drawing) feeds it back
through the live pipeline, so the paper mapping, flips, tilt, the effect chain
and the optimizer all apply — and it's recorded into the drawing like anything
else. Strokes drawn on the iPad while it runs appear at once and are plotted
once the import has been fed in. **Pause** lifts the pen and stops both the
feed and the plotter; **Cancel** drops what's queued and lifts the pen.

**Disengage XY Motors** (plotter controller) lifts the pen and releases the
motors so the carriage pushes by hand; the AxiDraw stays connected, and moves are
ignored until you press **Re-engage XY Motors**. Home doesn't move — **Set Home**
does that, taking wherever the carriage is as (0, 0). **Walk Home** drives the
carriage back to it.

**Plotter behind** (top bar) is the gap between drawing a mark and the plotter
drawing it, measured on a clock that only runs while marks are being drawn. So
it climbs while you draw faster than the machine, falls whenever you stop, and
reads 0 once it's caught up. It's also what decides when lines get simplified
("Keeping up", Plot tab).

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

A **saved** file is a picture of the sheet instead: its points are where the
pen went on the paper, at 96 per inch, marked `"space": "paper"` with the
sheet's size in `paperIn`. That way the file matches what was plotted —
however the layout was turned or moved — and importing it puts the ink back in
the same place. Files without `space` are read as tablet coordinates, as
before.

`recording.py` is the one implementation of this format: it records the live
session (from the same messages the page receives), reads plot SVGs and writes
them; `svg_transform.py` and `dot_healer.py` use it. The other readers are
`listen_to_idraw.py` (`_replay_feed`) and `PantographApp/library.py` (the
Drawings and Tools tabs). Keep them in step.

---

## Offline tools

These work on a finished SVG, from the command line. They are deliberately
**not** in the app: Pantograph plots what a pen and the machine can actually
do, and doesn't edit finished drawings (the one exception is **Heal dots
automatically**, which changes what the pen does *while* drawing, by carrying on
instead of lifting). Each rewrites the recording and regenerates matching
visuals from it, so the picture and the plot stay in sync.

```
python svg_transform.py [input.svg]        # opens input.svg in Pantograph
python svg_transform.py --selftest f.svg   # headless checks

python dot_healer.py drawing.svg           # -> drawing_healed.svg
python dot_healer.py drawing.svg --max-gap 20 --dry-run
```

`dot_healer` fixes strokes that a fast pen tore into a run of one-point strokes
(the timeout fired *between* consecutive points): it finds `line, dots…, line`
runs and concatenates them back into one continuous stroke, with guards so
deliberate tap-dots are left alone.

---

## License

The code in this repository is released under the [MIT License](LICENSE).
