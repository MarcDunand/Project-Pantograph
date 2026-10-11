# Project Pantograph

An artist draws on an iPad with an Apple Pencil; an AxiDraw pen plotter
recreates the drawing on paper, live, while they draw. The Pantograph app
shows what is happening and exposes every knob that matters.

*(The live-drawing pipeline itself is internally called* `draw2axi` *— that
name shows up throughout the code and the rest of this document.)*

```
Apple Pencil → iPad (iDraw OSC) → Wi-Fi/UDP → Python → AxiDraw
                                       │
                                       └──→ the Pantograph window
```

Points are streamed to the plotter as they arrive — the pen starts moving
mid-stroke rather than waiting for the stroke to finish. When the plotter falls
behind, an optimizer thins the pending queue so it can catch up.

**An AxiDraw is optional.** Without one you still get the live preview, and the
drawing is saved as an SVG you can plot later on a machine that has one.

**So is the iPad.** With only an AxiDraw you can plot a drawing that was saved
from Pantograph, your own or one someone sent you. (It can't plot SVGs made by
other programs: see [Using Pantograph](#using-pantograph).)

---

## Get started

You need a Windows computer, an iPad (or iPhone) on the same Wi-Fi, and
optionally an [AxiDraw](https://axidraw.com). On a Mac, see
[Running from source](#running-from-source) — the app works there, it just
isn't a one-click download yet.

### 1. Install Pantograph

Two ways, same result. Pick whichever you're more comfortable with.

**Recommended: paste one command.** Open PowerShell (press Start, type
`PowerShell`, press Enter; not Command Prompt), paste this line, and press
Enter:

```powershell
irm https://github.com/MarcDunand/Project-Pantograph/releases/latest/download/install-windows.ps1 | iex
```

It installs Pantograph, puts it in your Start menu and on your Desktop, and
starts it.

**Or: download and double-click.**
[Download `pantograph-windows.zip`](https://github.com/MarcDunand/Project-Pantograph/releases/latest)
→ right-click it → **Extract All** → **Extract** → open the `pantograph`
folder, then `PantographApp` → double-click **`Pantograph.bat`**. Keep the
extracted folder somewhere permanent: that folder *is* the app, and there's no
Start-menu entry on this route.

<details>
<summary>Windows warns about the file on the download route</summary>

Windows is cautious with files downloaded in a browser, and Pantograph isn't
signed by a registered publisher. You may see **"Windows protected your PC"**:
click **More info**, then **Run anyway**. Or **"The publisher could not be
verified"**: click **Run**. It only asks once. (The command route doesn't get
this prompt, because PowerShell's download isn't marked as coming from the
internet.)

</details>

**What the first launch looks like.** A black console window opens and
downloads Python and the libraries Pantograph needs (about 150 MB) into its
own folder, which takes a minute or two. Then the console closes and
**Pantograph opens in a window of its own**. From then on it starts in a
second or two, even with no internet; the console only flashes by. Closing the
Pantograph window quits it: the pen lifts, the motors let go, and the drawing
is saved.

(If a PC can't show that window — it uses the Edge engine built into Windows
10 and 11 — Pantograph opens in your browser instead and works the same; use
its **Quit** button to stop it.)

Nothing is installed system-wide and no admin rights are needed. To update,
run the command again; your drawings and settings are kept. Drawings are saved
in `Documents\Pantograph`.

**Windows may ask whether Python can use the network. Choose Allow.** (Updating
from a version before 0.2 asks once more.) That's
how the iPad's strokes get in. If you cancelled it, see
[Troubleshooting](#nothing-arrives-from-the-ipad).

### 2. Get iDraw OSC on the iPad

On the iPad, open the **App Store**, search **"iDraw OSC"**, and install it.
It streams your Apple Pencil strokes over Wi-Fi.

### 3. Connect the iPad

Pantograph's top bar shows **IP:** and **Port:**. In iDraw OSC on the iPad,
set the IP and port to exactly those. The iPad and the computer must be on the
same Wi-Fi network.

Draw a line on the iPad. When the **iPad** button in the top bar turns green
and says *Receiving*, it's working. If it stays on *Waiting*, see
[Troubleshooting](#nothing-arrives-from-the-ipad).

### 4. Plug in the AxiDraw (optional)

Connect it over USB **and** switch on its own power supply (USB alone doesn't
power the motors). There is no AxiDraw software to install: Pantograph carries
what it needs. The **Plotter** button in the top bar turns green when it's
connected; if it doesn't, press it and choose **Connect**.

No AxiDraw? Press **Use without a plotter** on the card that asks for one.
Everything else works, and File → **Save as…** gives you an SVG to plot later.

### 5. Draw

Strokes appear on the paper in the Pantograph window as you draw, and the AxiDraw starts
moving along with you.

---

## Using Pantograph

- **Top bar:** the **iPad** and **Plotter** buttons show each connection's
  state; click one to connect, restart the listener, or open a
  **Troubleshoot** checklist. **Plotter behind** is how many seconds of
  drawing the machine still has to catch up on. **Quit** lifts the pen, turns
  the motors off and saves the drawing.
- **Menu bar:** **File** (New canvas, Import to canvas…, Save as…, Discard
  canvas) and **Preferences** (Plotter preferences…, Edit layout…, Heal
  dots).
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
- **The sidebar** has two tabs. **Plotter controller**: Walk Home, Set Home
  and Disengage XY Motors; the AxiDraw model and paper size; and the three pen
  positions, each with a Test button. **Effects**: postprocessing effects like
  zigzag or hatching, each with an **i** button that explains it (see
  [Post-processing effects](#post-processing-effects)).
- **Preferences → Plotter preferences…**: speed and acceleration, pressure
  updates, table tilt, and how hard to simplify lines when the plotter falls
  behind.
- **Preferences → Edit layout…** is where those rectangles move. Drag either
  one, use its ring to turn it, and the drawing's corner to resize it. Set the
  AxiDraw's rectangle to where the machine really sits on the sheet, and the
  plot lands where the screen shows it. The layout is remembered.
- **File → New canvas** starts a fresh one, offering to save first. **Save
  as…** writes an SVG wherever you choose. **Discard canvas** empties it
  without saving. The canvas is also autosaved while you draw and saved when
  you quit, so nothing is lost by closing the window.
- **File → Import to canvas…** shows a saved drawing in a preview. **Import to
  canvas** plots it and adds it to the canvas, as if you had drawn it yourself
  — you can keep drawing on the iPad meanwhile, and your strokes plot after it.
  While it plots, a progress bar with **Pause** and **Cancel** appears in the
  sidebar.
- **Closing the window** does what Quit does: it lifts the pen and releases
  the motors, so the carriage can be pushed home by hand. Starting Pantograph
  while it's already running just brings its window forward. (In a browser,
  closing the tab leaves Pantograph running: use Quit, or open
  `http://127.0.0.1:5810` to get the page back.)

**No iPad?** Press **Use without an iPad** on the start-up card, then File →
Import to canvas… to plot a saved drawing. Only drawings saved from Pantograph
can be imported: what gets plotted is the record of the strokes that such a
file carries inside it, which an SVG from Inkscape or Illustrator doesn't
have. With neither an iPad nor a plotter, Pantograph says so: all that's left
is opening a saved drawing to look at it.

**No AxiDraw?** Everything works except the moving plotter: the drawing is
recorded and saved just the same. Copy the SVG to a computer with an AxiDraw
and import it there (File → Import to canvas…), with whatever paper, effects
and settings you choose at that time.

---

## Troubleshooting

### Nothing arrives from the iPad

The **iPad** button says what Pantograph has heard:

| Status | Meaning |
|---|---|
| *Waiting* | Nothing has arrived since Pantograph started. |
| *Receiving* | Points are arriving now. |
| *Idle* | The iPad sent something in the last two minutes. |
| *Quiet* | Nothing for two minutes. iDraw may have been closed. |
| *Problem* | Pantograph can't listen, usually because the port is busy. |

Click it and press **Troubleshoot** for this checklist with your own IP and
port filled in. In order:

1. **The IP and port in iDraw match the top bar.** The IP can change when
   either device reconnects to Wi-Fi, so re-check it after a break.
2. **Same Wi-Fi network**, and not one device on cellular data or a guest
   network.
3. **Windows: the network is Private, not Public.** Windows blocks incoming
   connections on Public networks even after you allowed Python. Settings →
   Network & internet → Wi-Fi → your network → Network profile type →
   **Private**.
4. **The firewall allows Python.** If you pressed Cancel on the firewall
   question: Start → type `Allow an app through Windows Firewall` → **Change
   settings** → find **pythonw.exe** (and **python.exe**, if it's there) and
   tick **Private**. (Neither in the list? Quit Pantograph, start it again,
   and choose **Allow** this time.)
5. **Nothing else is using the port.** If the status says the port is busy,
   pick another in the iPad panel and set the same one in iDraw.
6. **The network may stop devices reaching each other** — see below.
7. Press **Restart listener**, then draw a stroke.

#### School, work, hotel and guest networks

Many managed networks stop devices on them from talking to each other ("client
isolation"), even on the same Wi-Fi. It's the most common reason nothing
shows up, and nothing on the computer can change it.

- **To test:** turn on a phone's hotspot and put *both* the computer and the
  iPad on it. If Pantograph receives there, the original network was the
  problem.
- **To fix it** on any network, use **Tailscale**, which connects the two
  devices directly:
  1. Install Tailscale on the computer from
     [tailscale.com/download](https://tailscale.com/download), and on the
     iPad from the App Store.
  2. Sign in with the **same account** on both.
  3. In Pantograph, click the **iPad** button: the address list shows a
     Tailscale address (it starts with `100.`).
  4. Put that address in iDraw instead of the Wi-Fi one. The port stays the
     same.

#### Still nothing?

See what's actually reaching the computer. Quit Pantograph, then in
PowerShell run this, which starts it in a console with its log on screen
(any option after `Pantograph.bat` does that):

```powershell
& "$env:LOCALAPPDATA\Pantograph\app\PantographApp\Pantograph.bat" --raw-osc
```

and draw a stroke. **Nothing prints** → nothing reaches the computer, so it's
the network (above). **`/x /y /pressure` lines print but nothing draws** → the
connection is fine and something else is wrong; send the diagnostics (below).

### The install doesn't work

- **"Extract the zip first"**: the launcher was started from inside the zip.
  Right-click the zip → **Extract All**, then run it from the extracted
  folder.
- **Windows Security reports "Trojan:Win32/Commando.A!ml"**: that's Microsoft
  Defender reacting to an older form of the install command,
  `powershell -ExecutionPolicy Bypass -c "irm … | iex"` — the shape malware
  uses to fetch scripts, so Defender stops it on sight. Nothing was installed
  and nothing is infected. Use the command above, pasted into PowerShell, or
  the download route.
- **The one-line command fails**: run it again. A school or work computer may
  block it outright; the download route usually still works there.
- **The first launch fails**: it needs internet once to fetch Python. The
  console stays open with the reason.
- **Nothing appears after the console closes**: a message box normally says
  why. If there's none, look at `%LOCALAPPDATA%\Pantograph\Logs\pantograph.log`,
  or start it in a console to watch it: run `Pantograph.bat --verbose` (see
  "Still nothing?" above for the full path).
- **It opened in the browser, not its own window**: that PC is missing the
  Edge WebView2 runtime. Everything works in the browser; to get the window,
  install "WebView2 Runtime" from Microsoft.
- **"Couldn't replace the previous version"** when updating: Pantograph is
  still running. Quit it (close its window) and run the command again.

### The plotter doesn't connect

Click the **Plotter** button → **Troubleshoot**. Usually: the AxiDraw's power
supply is off, the USB cable is charge-only, or another program (Inkscape's
AxiDraw extension, a second Pantograph) has hold of it.

### Asking for help

**Copy diagnostics** in either Troubleshoot list copies a summary to paste
into a message. The log is at `%LOCALAPPDATA%\Pantograph\Logs\pantograph.log`.

---

## Running from source

For a Mac, or to change the code. It uses [uv](https://docs.astral.sh/uv/),
which fetches the right Python and the exact library versions by itself, so
you don't install Python separately. (The Mac route works, but hasn't yet been
through a real-Mac test session in this version; see the build guide.)

**New to the terminal?** A terminal is a window where you type commands. On a
Mac it's **Terminal** (Cmd+Space, type `Terminal`, Enter); on Windows,
**PowerShell**. To run a command, paste it and press **Enter**.

1. **Get the code.** On
   [the project page](https://github.com/MarcDunand/Project-Pantograph), click
   the green **Code** button → **Download ZIP**, and extract it (Mac:
   double-click it; Windows: right-click → Extract All). Or, if you use git:
   `git clone https://github.com/MarcDunand/Project-Pantograph.git`.
2. **Install uv** (once):
   - Mac: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

   Then close the terminal and open a new one.
3. **Open a terminal in the project folder.** Mac: type `cd ` (with the
   space), drag the project folder from Finder onto the Terminal window, and
   press Enter. Windows: open the folder in File Explorer, click the address
   bar, type `powershell`, and press Enter. Check with `ls`: you should see
   `listen_to_idraw.py` in the list.
4. **Run it:**
   ```
   uv run listen_to_idraw.py
   ```
   The first run downloads Python 3.12 and the libraries; later runs start
   straight away. On Windows Pantograph opens in its own window; on a Mac, in
   a browser tab at `http://127.0.0.1:5810`.

Then carry on from [step 2 of Get started](#2-get-idraw-osc-on-the-ipad).
**On a Mac**, macOS may ask whether Terminal (or Python) may find devices on
your local network or accept incoming connections: allow both, or the iPad
can't get through.

### Developers

```
uv run listen_to_idraw.py --dry-run     # never touch the AxiDraw; log moves instead
uv run listen_to_idraw.py --raw-osc     # print every OSC message as it arrives
uv run listen_to_idraw.py --browser     # the UI in a browser tab, not its own window
uv run listen_to_idraw.py --window-test # open the window, check the page loads, quit
uv run listen_to_idraw.py --help        # the rest: --verbose, --osc-port, --data-dir, --open…
uv run pytest                           # the test suite
uv run python -m pyflakes *.py PantographApp/*.py    # lint, as CI runs it
```

- The browser tests drive an installed Edge or Chrome through Playwright, and
  skip if neither is there. WebKit (for Safari) is optional:
  `uv run python -m playwright install webkit`.
- `tests/test_hardware.py` talks to a real AxiDraw (lifts the pen, never moves
  the carriage) and only runs with `PANTOGRAPH_HARDWARE=1` set.
- **After changing dependencies in `pyproject.toml`, run `uv lock` and commit
  `uv.lock` with it.** The launcher runs with `--locked` and refuses to start
  when the two disagree, so a forgotten lock breaks every install while the
  repo still seems fine.
- **Without uv**, in a Python 3.12 virtual environment:
  ```
  pip install python-osc "websockets>=13" numpy rdp platformdirs pywebview PantographApp/vendor/axidrawinternal-3.9.6-py2.py3-none-any.whl PantographApp/vendor/AxiDraw_API_396
  python listen_to_idraw.py
  ```
  (The AxiDraw API is vendored in `PantographApp/vendor/`; its README says
  why and how to upgrade it.)
- **CI** (`.github/workflows/ci.yml`) runs on every push to `main`: lint, the
  tests, then the real launcher with `--smoke-test` (the app starts, checks it
  can draw and save, and quits) and `--window-test` (its own window opens and
  the page comes up in it), then builds the release zip, installs from it and
  smoke-tests the installed copy.
- **Releasing:** set the new version in `pyproject.toml`, run `uv lock`,
  commit and push. Then tag that commit `v` + the version (e.g. `v0.1.1`) and
  push the tag — in GitHub Desktop, History → right-click the commit →
  **Create Tag…** → **Push origin**. `.github/workflows/release.yml` checks
  the tag matches the version, re-runs everything, and publishes the release
  with the zip and the installer attached.
- The full build history, gotchas and decisions are in
  [`PantographApp/BUILD_GUIDE.md`](PantographApp/BUILD_GUIDE.md).

**Where things live on Windows:** the installed app in
`%LOCALAPPDATA%\Pantograph\app`, its Python and libraries in `…\runtime`,
settings in `…\settings.json`, logs in `…\Logs`; drawings in
`Documents\Pantograph`. Replacing or deleting `app` loses nothing else.

---

## Files

*(From here down: technical reference — file layout and internals, for
anyone running the less common tools, maintaining this code, or curious how
it works. If you just want to draw, [Get started](#get-started) above is
everything you need.)*

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
| `PantographApp/` | App-specific code: the page (`ui/`), settings, data folders, the system file windows, startup/shutdown, the Windows launcher (`Pantograph.bat`), the vendored AxiDraw API (`vendor/`), and the build guide. |
| `install-windows.ps1` | The one-line installer: downloads the latest release into `%LOCALAPPDATA%\Pantograph\app` and makes the shortcuts. |
| `.github/workflows/` | CI on every push (`ci.yml`) and the release build on a `v*` tag (`release.yml`). |
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

iDraw sends each point as `/x`, `/y`, `/pressure`, so a point is emitted when
its `/pressure` arrives and carries its own reading. (If a `/pressure` is ever
lost, the next message emits the point with the last reading seen.) Raw iDraw
pressure is normalised by `OSC_PRESSURE_MAX` (≈4.167). With **variable
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

**Where it's shown.** On Windows the page opens in Pantograph's own window
(`shell.run_window`): pywebview hosting it in Edge WebView2, with the app's
icon and its own taskbar button. It is the same page from the same local
server, so a browser can still open `127.0.0.1:5810` alongside it. The window
takes the main thread until it's closed, and closing it runs the same
`shutdown()` as Quit. File → Import and Save as use the window's own file
dialogs; in a browser they are tkinter's, on the main thread. If the window
can't open (no WebView2, the library won't load, `--browser`), the default
browser is used instead. The launcher starts the app with `pythonw`, so there
is no console; started with any argument it runs in the console instead.

**Canvas** — four stacked layers: the drawing (each stroke's colour as a grey
of the same brightness, on white), the stroke in progress, the pen's path after
optimizing (orange), and only what the effect chain adds (blue). The two
engine-derived layers are drawn from the same commands that go to the plotter,
so the gap between the drawing and the orange path *is* the thinning.

**Settings** (Plotter preferences and Effects) are saved on the computer
(`settings.json` in the per-user config folder) and apply at startup, even
before the window is open. The AxiDraw model and paper size are settings too.
The paper isn't limited by the model's reach (the layout shows how much of it
the machine covers), and neither can change mid-plot. **Effects only** plots just what the effects add and drops the
base line, for re-running a finished drawing over a base layer already on the
paper.

**Importing a drawing** (File → Import to canvas…) feeds it back
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
("Keeping up", in Plotter preferences).

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
The Effects tab and the `_EFFECT_SWITCHES` block pick it up from there. Full
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

Importing pushes those points back through `_emit_point` exactly as if they
had just arrived over OSC, so paper mapping, flips, tilt, the effect chain and the
optimizer all re-apply downstream. That is the point of replaying at the *input*
level: the same drawing can be plotted again with different settings and
different post-processors switched on. Idle gaps are shortened to
`IMPORT_MAX_GAP_SEC` so a drawing with long pauses doesn't take its original
wall-clock time.

A **saved** file is a picture of the sheet instead: its points are where the
pen went on the paper, at 96 per inch, marked `"space": "paper"` with the
sheet's size in `paperIn`. That way the file matches what was plotted —
however the layout was turned or moved — and importing it puts the ink back in
the same place. Files without `space` are read as tablet coordinates, as
before.

`recording.py` is the one implementation of this format: it records the live
session (from the same messages the page receives), reads plot SVGs and writes
them; `svg_transform.py`, `dot_healer.py` and `listen_to_idraw.py` all go
through it. The one other place that walks a recording's points is
`_feed_strokes` in `listen_to_idraw.py`, which plays an import into the
pipeline. Keep the two in step.

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
