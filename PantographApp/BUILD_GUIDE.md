# Pantograph App: Build Guide

This is the step-by-step plan for turning draw2axi into one downloadable app,
plus the known gotchas and how each is handled. It is written for whoever
builds it (Marc, or a Claude session). Work through the phases in order. Each
phase ends with a checkpoint, and nothing moves on until that checkpoint passes.
**Keep this file current as work lands:** tick boxes, record decisions, add
gotchas.

**Status:** Phases −1 to 7 are done, apart from the checks below.
Marc's first UI review (2026-09-20) has been worked through — see "UI round 2"
in Phase 5. **Phase 8 (launchers and installers) is next.** The pressure-lag
fix is deferred until an Apple Pencil is available (see Phase −1).

**Hardware checks.** These can't be verified without the iPad and AxiDraw.
Marc's session on 2026-09-19 covered the first five:
- [x] Phase 1: plotting live behaves exactly as before (strokes, pen tests,
      Home, replay, effects).
- [x] Phase 1: unplugging the AxiDraw's USB mid-plot shows an error instead of
      freezing, and the app keeps running.
- [x] Phase 1: with the AxiDraw unplugged at startup, the console reports
      `not_found`.
- [x] Phase 1 (2026-09-20): closing the console window mid-plot lifts the pen
      **and** releases the motors. The `res_home2` bug is fixed.
- [x] Phase 3: plotting a saved SVG ("plot svg") plots it once, not twice.
- [x] **Phase 1, re-check after a fix (PASSED 2026-09-20):** closing the
      console window mid-plot lifts the pen **and releases the motors**.
  - **09-19 result:** the pen lifted but the motors stayed engaged, twice.
  - **Real cause, pre-existing since before the app work:** the shutdown
    code disengaged the motors by running pyaxidraw in mode `"res_home2"`,
    **a mode pyaxidraw doesn't have**. It did nothing, and the program then
    logged "XY motors disengaged" anyway. Every shutdown in the log says so,
    yet the motors stayed on. Confirmed on the AxiDraw by reading the
    controller's motor-enable pins (`plotink.ebb_motion.query_enable_motors`):
    `(1, 1)` before and after a `res_home2` run.
  - **Fix (`_make_axidraw_safe`):** pen up → `ad.block()` →
    `ebb_motion.sendDisableMotors(port)` on the already-open connection. The
    fallback is a fresh connection running pyaxidraw's real manual
    `disable_xy` command. `shutdown()` also now makes the machine safe
    *first* and saves/closes after, because a closing Windows console only
    gets a few seconds.
  - **Verified on the AxiDraw** (quit path): motors `(1, 1)` → app quits →
    `(0, 0)`; the `disable_xy` fallback also reads `(0, 0)`. This is kept as
    an opt-in test, `tests/test_hardware.py` (`PANTOGRAPH_HARDWARE=1 uv run
    pytest tests/test_hardware.py`; connects, lifts the pen, never moves the
    carriage). The console-close path runs the same function, and shutdown
    takes under a second, so the remaining check is Marc closing the window
    on a freshly started app.

**Checks from Phases 5–7 — run on the machine 2026-09-20, all passed**
unless marked otherwise:
- [x] **UI review (Phase 5 checkpoint).** The round of changes it produced is
      in "UI, round 4" below.
- [x] iPad button: *Waiting* before the first stroke, then *Receiving* while
      drawing and *Idle* between strokes. The first-run card (IP and port) goes
      away once the iPad sends anything.
- [x] Plotter controller: **Disengage XY Motors** → the carriage pushes freely
      (the AxiDraw stays connected) → **Re-engage XY Motors** → home is
      unchanged, so **Walk Home** returns to the old corner; **Set Home** makes
      the current spot home instead.
- [x] "Plotter behind" counts up while you draw faster than the plotter, and
      counts down whenever you stop, reaching 0 as it catches up.
- [x] The progress bar follows the *pen*, not the feed: it should still be
      climbing while the plotter works, and reach 100% as the pen finishes.
- [x] Open file… opens the system's Open window at the drawings folder (and at
      the last folder used, after that); the preview shows the drawing;
      **Import to canvas** plots it onto the canvas.
- [x] **Re-check after the G-49 fix (PASSED 2026-09-21):** drawing on the iPad
      while an import plots — the strokes appear at once and are plotted behind
      the import.
- [x] **Save as…** opens the system's Save window; **New drawing** on an
      unsaved drawing offers Save as… / Discard.
- [x] The canvas is the paper: the rulers match a real ruler held against the
      plot, in inches and in mm.
- [x] Heal dots automatically (Preferences): a line torn into dots comes out as
      one line.
- [x] **Edit layout** (Preferences): move and turn the orange AxiDraw rectangle
      to where the machine really sits on the sheet, and the turquoise drawing
      rectangle to where it should be plotted. Then plot: the ink lands where
      the screen said it would, measured with a ruler.
- [x] The layout survives quitting and restarting.
- [x] A drawing partly outside the machine's reach: the warning appears, and
      the part outside simply isn't drawn (the stroke stops at the edge and
      picks up again where it comes back within reach).
- [x] File → Import to canvas… → **Import**: the progress bar moves;
      **Pause** lifts the pen and stops the carriage; **Resume** puts the pen
      back down where it was and carries on; **Cancel** lifts the pen and
      leaves no stray dot. (The line of text under the bar always read
      "Finishing"; it has been removed rather than fixed, since the bar and the
      count already say everything.)
- [x] Paper and model dropdowns: pick A3 on the V3 model → the "reaches"
      note appears; switch the model to SE/A3 → it goes away.

**UI round 4 (2026-09-20).** From Marc's pass with the iPad and AxiDraw:

- **Speed and acceleration are settings** (Preferences → Speed), in the
  AxiDraw's own units: speeds 1–110, acceleration 1–100, defaulting to the
  machine's stock 25 / 75 / 75. They were hard-coded at 15 / 25 / 50 — slower
  and gentler than stock — so the defaults change what the machine does; the
  old numbers are typed back in to reproduce earlier plots. A change is applied
  by the plotter thread, which owns the USB handle (`_speeds_request`).
- **The machine controls read as machine commands**: Walk Home, Set Home,
  Disengage XY Motors, on one line, with the explanation under them removed.
- **The opened drawing is a window over the app**, not a screen that replaces
  it — the canvas it is joining stays visible behind it.
- **The line of text under the import's progress bar is gone.** It always read
  "Finishing", and the bar and the count already say what it said.
- **Resuming after a pause dwells 0.3 s** between putting the pen down and
  moving again (`RESUME_DWELL_SEC`), so the pen is properly seated before the
  line carries on. Nothing else dwells on pen down: a live stroke has to keep
  up with the hand drawing it. **Checked on the machine 2026-09-21:** the
  joint after a resume is clean.
- **Checked on the machine 2026-09-21:** the stock 25 / 75 / 75 plots fine, so
  the old hard-coded 15 / 25 / 50 was caution rather than a requirement.
- Each machine button is as wide as its own label. They were one shared width
  first, and "Re-engage XY Motors" spilled outside its border.

**UI round 5 (2026-09-21).** From Marc's second pass:

- **A saved file is the drawing alone**, whatever the layer checkboxes show.
  The pen path is the machine's account of one particular run, and the effect
  marks are re-applied live from whatever is switched on now — neither is
  something the artist drew, so neither is saved. The checkboxes are a view
  control only. `_paper_space_svg()` takes no arguments any more, and
  `test_a_saved_file_is_the_drawing_alone` pins it.
  - This also closes a trap: saving with "Drawing" unticked used to write a
    file with no `<metadata>`, which Pantograph could never import again.
- **Effects moved out of a window into a sidebar tab** beside the plotter
  controller, with an "i" for effects as a whole, one for "Effects only", and
  one per effect. Each effect's text is a `blurb` on its class in
  `postprocess.py`, served through `effect_specs()`, so a new effect brings its
  own explanation. The menu bar is File and Preferences only.
- **Rulers sit against the paper**, not against the canvas — which reaches
  MARGIN_IN past the sheet, so they had an inch and a half of empty table
  between them and what they measure. The side ruler counts up from the bottom,
  and the unit button rides along to the corner where the two meet.
- **The top bar reads `IP:` / `Port:`**, no Copy IP, with the labels and values
  on a shared baseline (see G-54).
- **Em and en dashes are out of the interface copy** — plain hyphens instead,
  ranges included (`0-100`, `Left-right`, `1024-65535`). `index.html` has
  neither character left; code comments keep them.
- The out-of-reach warning now appears only in the layout editor, where you'd
  act on it, not under the paper dropdown.

**Checked on the machine 2026-09-21, both passed:** effects driven from the new
sidebar tab plot as they did from the window, and the rulers still match a real
ruler held against the sheet after moving in and flipping the side scale.

`install-windows.ps1` points at `MarcDunand/Project-Pantograph`, taken from the
repo's own remote — the `OWNER/REPO` placeholder is gone.

**What Pantograph is (Marc, 2026-09-20).** A translator between physical and
digital media — a pantograph — not an SVG editor. A feature earns its place by
matching something you can do with a pen and the machine: you can't
retroactively remove lines or heal a broken one, so the app doesn't either.
This is why the Tools tab was deleted, why an import joins the one canvas
rather than opening a second document, and why the layout editor replaced the
flip settings (a machine can be turned on a desk; it can't be mirrored).

**Scope rule: this is a public demo, not a launch product.** Few users are
expected. When a choice is between saving time and adding polish, choose
saving time, unless the polish prevents a real failure: a lost drawing, a pen
stuck down on the paper, or being unable to install or connect at all. Items
cut under this rule are listed in §9 so they can be revived if the demo needs
them.

---

## 1. What we're building, and what's decided

**The app:** someone installs Pantograph with one pasted command (the primary,
preferred route), or by downloading a zip and double-clicking the launcher
(the alternative). Both routes exist on Windows and Mac. The first launch
installs everything it needs. The app then opens in the browser as one tool:
- live drawing from the iPad (iDraw OSC);
- live plotting when an AxiDraw is plugged in, or the preview plus recording
  when it isn't;
- the post-processing effects;
- the SVG tools (flip, width filter, dot healing);
- a list of saved drawings.

| Decision | Choice |
|---|---|
| Scope | A **demo**. Prefer saving time over polish (rule above). |
| Delivery | **Option 1A**: launchers that use `uv` to install Python and the dependencies. The UI is the normal browser. |
| Install routes | **Primary, preferred: one pasted command** (PowerShell on Windows, Terminal on Mac). **Alternative: click-download** a zip, extract it, double-click the launcher. The README shows the two **side by side**, so the alternative is visible immediately (Phase 10). |
| Future | **Option 1B**: a native window via pywebview, with the browser as fallback. It gets added later as a thin layer, and 1A must stay ready for it (§7). |
| Platforms | Windows x64 and Macs (Apple Silicon; Intel on a recent macOS). Windows on ARM isn't specifically handled, since it's rare and the risk is low. |
| Signing | None. The click route's install warnings are acceptable and documented. |
| Share button | Not now. A future "upload to the Pantograph website" is expected (§8). |
| Old commands | `python listen_to_idraw.py`, `python svg_transform.py` and `python dot_healer.py` keep working. |
| Layout | Pipeline Python stays at the **repo root**. App-specific files go in **`PantographApp/`**. No file moves. |
| Ports | The web page and the live WebSocket feed merge onto **one port**. |
| `iDraw_to_svg/` | Deleted. "No plotter" becomes a status, not a second program. |
| Name | **Pantograph**, for the launcher, the data folders and the tab title. |
| Drawings folder | Defaults to `Documents/Pantograph/`. The repo's `saved_drawings/` **stays in git** as documentation; the app doesn't write there, and it's left out of the release zip. |
| Look | **White background, greyscale strokes** (each stroke's colour turned into grey), matching iDraw OSC. Used for the live canvas, the UI theme and every export. |
| Paper size + AxiDraw model | Saved settings, like x/y tilt, in the Plot tab. |
| `python svg_transform.py` | Opens the app's **Tools** tab with that file loaded. The tkinter window is removed. |
| Connection status | Clickable **iPad** and **Plotter** status buttons, always on screen, each with a **Troubleshoot** checklist. |
| Stroke boundaries | iDraw's **state block** splits strokes; timing only rests the pen. Done in Phase −1. |
| OSC input | **Single thread**, in arrival order. Done in Phase −1. |
| No-pressure input | 5+ placeholder `1.0` values in a row plot at pressure 0.5. Done in Phase −1. |
| pyaxidraw | A **copy lives in the repo** (`PantographApp/vendor/`, see its README): the `axicli` package (which provides `pyaxidraw`) unpacked, plus the `axidrawinternal` wheel it needs (G-12). |
| Licence | **MIT** (`LICENSE` at the repo root). It covers the code. pyaxidraw keeps its own GPL licence (`PantographApp/vendor/`). |

**What doesn't change:** the drawing pipeline's core logic (mapping, the
optimizer, effects, replay). Behaviour changes are called out where they
happen and collected in §5.

---

## 2. Things found while reading the code

These are existing problems the app would make worse if left alone. Each
points to the phase that handles it.

1. **The drawing is recorded in the browser, not in Python.** `preview.py`'s
   JavaScript builds the recording (`completedStrokes`, `buildRecording`).
   **Reloading or closing the tab loses the drawing**, and a tab opened
   mid-session misses what came before it. Such a tab also stamps strokes with
   the default 440×956 canvas size if it missed iDraw's size message. →
   **Phase 3.**
2. **Any web page can control the plotter.** The WebSocket server accepts any
   connection, and browsers don't apply the same-origin policy to WebSockets.
   → **Phase 2.**
3. **pyaxidraw supports Python 3.8–3.12 only**, per its install docs (its
   package metadata only says `>=3.8`). → **Pin 3.12, in Phase 0.**
4. **pyaxidraw's download URL is unversioned**
   (`https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip`), so any
   lockfile hash breaks when Evil Mad Scientist publishes an update. As of
   2026-09-18 the URL serves version 3.9.6 (2023-12-12): a 383 KB zip,
   GPL-2.0-or-later, with **distribution name `axicli`** (it provides the
   `pyaxidraw` and `axicli` modules). Its dependencies are `ink_extensions`,
   `lxml`, `plotink`, `pyserial` and `requests`. → **Keep a copy in the repo
   (Phase 0).**
5. **The plotter connects only once, at startup.** `from pyaxidraw import
   axidraw` sits outside the `try`, so a missing pyaxidraw kills the plotter
   thread. An unplugged cable mid-plot also kills the thread, silently. →
   **Phase 1.**
6. **Every point is printed to the console,** which is slow on Windows. →
   **Phase 1.**
7. **A clean quit only happens on Ctrl+C.** Closing the console or Terminal
   window skips lifting the pen and disengaging the motors. → **Phase 1.**
8. **The startup code isn't callable.** It lives under `if __name__ ==
   "__main__":`, and the OSC server's `serve_forever()` blocks the main
   thread. → **Phase 1.**
9. **`iDraw_to_svg/` is a strict subset of the main version.** Deleting it
   loses nothing. → **Phase 4.**
10. **Two SVG renderers draw differently.** The browser's `layerSvgParts` and
    `svg_transform.build_svg` produce different visuals. Plotting is
    unaffected, since replay reads only the metadata. → **Phase 3 makes Python
    the only writer.**
11. **`__pycache__/` is committed, and there's no `.gitignore`.** → **Phase
    0.**
12. **Found 2026-09-19: shutdown never released the motors.** It ran
    pyaxidraw mode `"res_home2"`, which doesn't exist, then logged success.
    **Fixed**; see the hardware checks at the top.
13. **Pressure lags one point.** iDraw sends `/x`, `/y`, then `/pressure`,
    and a point is emitted when `/y` arrives. **A fix exists but is deferred**
    until a Pencil is available (Phase −1).
14. **Found in Phase 3: the replay tag was never sent.** The Aug 28 commit
    ("fixed … redrawing a prerecorded SVG doubles that drawing") added
    `_in_replay()` and page code that skips points tagged `replay`, but
    `_emit_point` never put the tag in its message, so replayed points were
    still recorded as if drawn. **Fixed in Phase 3** (`"replay": _in_replay()`).

---

## 3. Architecture

```
iPad (iDraw OSC) ──UDP :8800──► listen_to_idraw.py  (engine, background threads)
                                   │  points, layers, lag, status
                                   ▼
                                recording.py  (records the session in Python, autosaves)
                                   │
                                preview.py    (one port: static UI + /ws, 127.0.0.1 only)
                                   │  HTTP GET + WebSocket
                                   ▼
                    Browser tab (1A)  /  pywebview window (1B, later)
                                   │
                    PantographApp/ui/  (index.html, app.js, style.css)
```

- **The engine is the source of truth.** Settings, the current drawing and the
  plotter status live in Python. On connect, the page receives a `hello`
  snapshot, then live updates. A reload or a second tab loses nothing.
- **One port.** The default is `5810`, chosen to avoid 5000, which AirPlay
  uses on Macs. If it's busy, the app tries 5811–5830. The page connects to
  `ws://<its own host>/ws`.
- **HTTP serves GET only:** the UI files, saved drawings and thumbnails, and
  `/health`. Everything that changes state goes over the WebSocket, since the
  `websockets` library's HTTP support is GET-only. If that becomes limiting,
  switch to `aiohttp`.
- **The main thread is kept free.** The engine runs on background threads. In
  1A the main thread opens the browser and waits for a stop signal; in 1B the
  window takes over the main thread (§7).

### Repo layout after the build

```
(repo root)
  listen_to_idraw.py   engine + entry point. `python listen_to_idraw.py` = the app
  preview.py           server: one port, static files + WebSocket, security checks
  recording.py   NEW   the ONE Python implementation of draw2axi-recording:
                       live recorder, load, build SVG, thumbnails
  layout.py      NEW   where the drawing and the machine sit on the paper, and
                       the one path from a tablet point to the machine
  postprocess.py       unchanged
  svg_transform.py     transform functions unchanged; load/build re-exported from
                       recording.py; tkinter GUI removed; a command-line tool,
                       and running it with a file opens that file in the app
  dot_healer.py        unchanged apart from importing from recording.py
  pyproject.toml NEW   dependencies (shared by plain-Python runs and the app)
  uv.lock        NEW   exact pinned versions + hashes
  .python-version NEW  3.12
  .gitignore     NEW
  .gitattributes       + line-ending rules and release-zip exclusions
  tests/         NEW   pytest suite + small fixture SVGs
  .github/workflows/ NEW  CI on Windows + macOS; release zip
  PantographApp/
    BUILD_GUIDE.md     this file
    deferred/          saved patches waiting on hardware (pressure-lag-fix.patch)
    vendor/      NEW   pyaxidraw 3.9.6 (GPL): AxiDraw_API_396/ + the axidrawinternal
                       wheel, split apart so uv.lock stays portable (see its README)
    Pantograph.bat     Windows launcher
    Pantograph.command Mac launcher
    install-windows.ps1  Windows one-line installer (primary route)
    install-mac.sh     Mac one-line installer (primary route)
    __init__.py        makes the folder importable as `PantographApp`
    shell.py           data dirs, single-instance check, open_ui(), exit handlers, log file
    settings.py        settings store (JSON on disk, defaults)
    netinfo.py         local IP list
    dialogs.py   NEW   the system's own Open and Save windows (main thread only)
    ui/                index.html, app.js, app.css, icon.svg
```

`pyproject.toml` goes at the root because it serves plain-Python runs as well
as the app, and tools look for it there.

---

## 4. Phases

### Phase −1: Stroke boundaries and no-pressure input (DONE)

Done first, at Marc's request. Confirmed on the iPad and AxiDraw, in the plain
`python listen_to_idraw.py` version, on 2026-09-19.

- [x] Strokes end only when iDraw's **state block** arrives (any of `/r /g /b
      /a`, the tool flags, `/canvasWidth`, `/canvasHeight`, `/drawingWidth`,
      `/eraserWidth`), or where a replayed recording's stroke ends
      (`_state_block_seen`, `_end_stroke`).
- [x] Timing only **rests** the pen. After `PEN_REST_SEC` (0.15 s) without a
      point, the pen lifts, bypassing the effects. If the stroke continues,
      the lift is taken back out of the queue if the plotter hasn't reached
      it, or the pen is lowered again at the same spot (`_maybe_rest_pen`,
      `_resume_after_rest`).
- [x] **Single-threaded OSC** (`BlockingOSCUDPServer`, 1 MB receive buffer).
      The block logic depends on messages being handled in order.
- [x] **No-pressure input plots** instead of vanishing. A run of 5+
      placeholder `1.0`s is plotted at 0.5 (`NO_PRESSURE_RUN`,
      `NO_PRESSURE_VALUE`). A shorter all-`1.0` stroke, such as a finger tap,
      gets 0.5 when it ends. Shorter runs inside real pressure are still
      interpolated. The old "drop the stroke" behaviour was a side effect of
      glitch interpolation (MEETINGS.html, meeting 3), not a deliberate rule.
- [x] Docs updated: README (§ Stroke boundaries, § Pressure), AGENTS.md
      (§3.1, §3.4), and the `dot_healer.py` docstring.
- [x] Simulations against the real handlers pass, and Marc confirmed it on
      hardware.
- [ ] **Pressure-lag fix: DEFERRED** (written and reverted 2026-09-19, because
      there's no Apple Pencil on hand to test pressure).
  - **The bug:** iDraw sends `/x`, `/y`, `/pressure`, and a point is emitted
    on `/y`, so every point gets the *previous* point's pressure, and a
    stroke's first point gets the last pressure of the stroke before.
  - **The fix:** `/y` marks the point pending, and `/pressure` emits it
    (`_flush_point`). If a `/pressure` goes missing, the next `/x` or state
    block emits it anyway. It passed simulation in iDraw's real order.
  - **Saved as `PantographApp/deferred/pressure-lag-fix.patch`**, covering the
    code, README and AGENTS.md. To reapply:
    `git apply PantographApp/deferred/pressure-lag-fix.patch`. Then test with
    the Pencil and variable pressure on: pen depth should follow the Pencil,
    with no heavy or light blip at the start of a stroke (draw a light stroke
    right after a hard one). Delete the patch once it's applied. If Phase 1
    has reorganized the code by then, apply it by hand; it's small.
- **Known consequence:** iDraw sends nothing when the Pencil lifts, so the
  *latest* stroke stays open until the next one starts. Its pen is already
  rested, but the stroke-end effects (zigzag's last corner, pressure hatch,
  stroke connector) and the preview's `pen_up` run only then. If that
  matters, a long idle timeout (such as 2 s) could close the stroke. That
  would be a timing rule, but it would never split a continuous stroke.
- `iDraw_to_svg/` still uses the old timing rule. It's deleted in Phase 4.

### Phase 0: Groundwork and safety net

Done 2026-09-19.

- [x] `.gitignore` (`__pycache__/`, `*.pyc`, `.venv/`, `.pytest_cache/`); the
      committed `__pycache__/` files removed from git.
- [x] `.gitattributes`: line endings for `*.bat`/`*.ps1` (CRLF) and
      `*.command`/`*.sh` (LF), `*.zip binary`, and the release-zip
      `export-ignore` list from Phase 9 (added early, since it's the same
      file).
- [x] **pyaxidraw vendored.** Plain vendoring of the zip **didn't work**: the
      zip bundles an `axidrawinternal` wheel in `prebuilt_dependencies/`, and
      its `setup.py` injects that as a dependency by *absolute temporary
      path*, so `uv.lock` recorded a path inside this machine's uv cache and
      would have broken every other install. The fix: vendor the package
      unpacked (`PantographApp/vendor/AxiDraw_API_396/`, trimmed to what's
      needed, files unmodified) without that folder, plus the wheel on its own
      (`axidrawinternal-3.9.6-py2.py3-none-any.whl`, also GPL-2.0-or-later).
      `vendor/README.md` records the source, licence, changes and how to
      upgrade.
- [x] `pyproject.toml`: `python-osc`, `websockets>=13`, `numpy`, `rdp`,
      `platformdirs`, `axicli`, `axidrawinternal` (both from
      `[tool.uv.sources]` paths); `requires-python = ">=3.12,<3.13"`; a `dev`
      group with `pytest`; `[tool.uv] package = false` (it's an application).
- [x] `.python-version` = `3.12`; `uv lock` resolves 25 packages with
      repo-relative paths only. `from pyaxidraw import axidraw` works.
- [x] Tests (`uv run pytest`: **23 pass**):
  - `tests/test_recordings.py`: every drawing in `saved_drawings/` with a
    recording round-trips through `load_svg`/`build_svg`, and the two without
    one are rejected. **Real-data regressions:** healing `drawing_dense.svg`
    reproduces the committed `drawing_dense_healed.svg` exactly, and the 79%
    width filter on `drawing_raw.svg` reproduces
    `drawing_raw_minWidth79.svg` exactly. Flips are checked as involutions
    that don't touch their input. (No separate small fixtures were needed.)
  - `tests/test_strokes.py`: the Phase −1 behaviour, through the real
    handlers in iDraw's order, with the `engine` fixture (`tests/conftest.py`)
    resetting module state and capturing broadcasts. Includes real UDP through
    the single-threaded server.
- [x] **Checkpoint:** tests pass. `listen_to_idraw.py` itself wasn't changed
      in this phase.
- Dev setup used: uv 0.12.17 installed via `pip install --user uv`, run as
  `python -m uv …`; Python 3.12.14 is downloaded by uv.

### Phase 1: Restructure the engine

Done 2026-09-19 (hardware checks pending; see the list at the top). The
drawing logic is untouched.

- [x] `main(argv=None)` + `start()` + `shutdown()`; `__main__` is
      `sys.exit(main())`. `start()` returns the UI URL and leaves the main
      thread free; `main()` waits on `_stop_event` with a timed `wait(0.5)`
      loop, because a bare `wait()` isn't interruptible by Ctrl+C on Windows.
- [x] OSC listener on its own thread: `restart_osc_listener(port=None)` /
      `stop_osc_listener()`. `osc_status` (`listening | port_busy | stopped`)
      is broadcast as `osc_status`. **A busy port no longer crashes the app**:
      it starts anyway and reports `port_busy`.
- [x] `last_osc_time`, stamped per packet by a `BlockingOSCUDPServer`
      subclass (`_OSCServer.process_request`).
- [x] `shutdown()` runs once (lock + flag), from any thread: it stops OSC,
      stops the plotter thread and joins it, *then* takes over the USB link
      to lift the pen and run the `res_home2` disarm. Exit paths
      (`PantographApp/shell.install_exit_handlers`): `atexit`,
      SIGTERM/SIGHUP/SIGBREAK, the Windows console-close/logoff/shutdown
      events (`SetConsoleCtrlHandler`), Ctrl+C (KeyboardInterrupt in
      `main()`), and the page's new `quit` message. The autosave flush joins
      it in Phase 3.
- [x] Plotter robustness. **Confirmed a real bug:** pyaxidraw's `connect()`
      returns `False` (it doesn't raise) when no AxiDraw is on USB, and the
      old code ignored that, printed "connected", and sent moves to nothing.
      Now:
  - `plotter_status` (`connected | not_found | unavailable | dry_run |
    error`) is broadcast as `plotter_status`;
  - the pyaxidraw import is inside the `try`;
  - each command runs in a `try`. On failure the queue is discarded and the
    status becomes `error`; the plotter thread keeps running;
  - `connect_plotter()` (the page's `connect_plotter` message) reconnects on
    the plotter thread, which owns the USB link.
- [x] Logging: `log = logging.getLogger("pantograph")`, configured by
      `shell.setup_logging`. INFO to the console; DEBUG (every point, move,
      stroke end, rest, mapping, tool) only with `--verbose`; a rotating log
      file (`pantograph.log`, 1 MB × 3). stdout/stderr are reconfigured to
      UTF-8 (G-34). `print` remains only for `--raw-osc` output, which is its
      purpose.
- [x] `PantographApp/shell.py`: `resolve_paths()` (drawings, config, logs,
      runtime via `platformdirs`, or all under `--data-dir`), `setup_logging`,
      `open_ui(url)` (the 1B seam), `install_exit_handlers`.
- [x] Flags added: `--no-browser`, `--verbose`, `--data-dir PATH`. `--dry-run`
      now means "never touch USB", with moves logged at DEBUG. (`--port` and
      `--osc-port` arrived in Phase 2.)
- [x] New page → engine messages: `quit`, `connect_plotter`, `restart_osc`.
- **Checkpoint:**
  - [x] Tests pass (23).
  - [x] End-to-end, launching the real program in a subprocess (`--dry-run
        --no-browser --data-dir tmp`): it starts and reports its URL, OSC
        listens, a UDP stroke reaches the page as 20 points + `pen_up`, the
        `quit` message exits with code 0 after shutdown, the console shows no
        per-point lines, and the log file is written. A second run with port
        8800 held by another socket still starts and reports `port_busy`.
  - [x] Without `--dry-run`, it connected to the AxiDraw that happened to be
        plugged into the dev machine, and on Quit lifted the pen and released
        the motors (no drawing moves were sent).
  - [x] Hardware (2026-09-19): plotting as before, and unplugging mid-plot,
        both good.
  - [ ] Hardware: closing the console mid-plot. The pen lifted but the motors
        stayed on; the fix awaits a re-check (see the list at the top).
- **Still to come from the original list:** `--smoke-test` (Phase 9).

### Phase 2: One-port server, security, single instance

Done 2026-09-19. In `preview.py`, `PantographApp/shell.py` and
`listen_to_idraw.main()`.

- [x] **One server:** `websockets.asyncio.server.serve(...)` on `127.0.0.1`
      replaces `HTTPServer` + the separate WebSocket server.
      `_process_request` answers plain HTTP (`/` → the page, `/health` →
      `{"app": "pantograph", "pid": …}`, anything else → 404), always with
      `Cache-Control: no-store`. `/ws` continues into the WebSocket handshake.
      `max_size` stays 64 MB. The page connects to
      `ws://' + location.host + '/ws'`, so it never needs to know the port.
      Since Phase 5/6 it also serves `/ui/<name>` (the page's files),
      `/drawings/<name>` and `/thumb/<name>` (see Phase 6), each accepting
      only a plain file name in its own folder.
- [x] **Security:** the `Host` header must be `127.0.0.1:<port>` or
      `localhost:<port>` (else 403: DNS rebinding). WebSocket `origins` are
      those two plus `None` (a missing Origin means a local program).
- [x] **Ports:** `preview.DEFAULT_PORTS` = 5810–5830, first free wins;
      `preview.start()` returns the port it got. New flags: `--port N` (that
      port only; exits with "Can't start" if taken) and `--osc-port N`. OSC
      doesn't fall back automatically.
- [x] **Single instance:** `shell.record_instance` / `running_instance` /
      `forget_instance` (`runtime/instance.json` with port + PID, checked via
      `/health`). A second launch opens the first copy's UI and exits 0.
      `instance.json` is removed by shutdown (via a new `_shutdown_hooks`
      list). Since Phase 6, `--open FILE` hands the file to the running
      copy over its WebSocket (`shell.ask_running`) before opening its UI.
- [x] **Saving over the WebSocket:** `POST /save` is gone. The page sends
      `save_file {filename, b64}` (PNG and SVG both, for now) and gets
      `saved {ok, path}` back. File names are still sanitized to a basename,
      so `../../escape.svg` lands inside the drawings folder. Saves now go to
      the **drawings folder** (`Documents/Pantograph/`, or `--data-dir`) and
      no longer to the repo's `saved_drawings/`.
- [x] `preview.stop()` closes the server during shutdown; errors handling a
      page message are logged instead of silently swallowed.
- **Checkpoint:**
  - [x] `tests/test_app.py` (new, 10 tests) launches the real program in
        subprocesses on free ports (`--dry-run --no-browser --data-dir tmp`):
        page + `/health` + 404 + `no-store`; foreign `Host` → 403; foreign
        `Origin` WebSocket refused; a UDP stroke reaches the page; `save_file`
        stays in the drawings folder; Quit → exit 0, shutdown logged,
        `instance.json` removed, no per-point console lines; a second launch
        defers to the first; a busy OSC port is reported but not fatal; a busy
        explicit `--port` fails clearly.
  - [x] **Real browser:** the same file drives headless **Edge** (or Chrome)
        via Playwright, using the browser already installed, with no download.
        The page connects over `/ws` ("live"), draws a 30-point UDP stroke,
        saves an SVG, and throws no JavaScript errors. Playwright is in the
        `dev` group; the test skips if no Edge or Chrome is installed.
  - [x] Checked by hand once: with 5810 taken, the app falls back to 5811.
  - Total: **33 tests pass** (`uv run pytest`).

### Phase 3: Recording, settings and data folders move into Python

**Done 2026-09-19.** What was built is summarised here; the original plan
follows for reference.

- [x] **`recording.py`**: the one implementation of the format.
  - `parse_svg`/`load_svg` moved from `svg_transform.py` (re-exported there
    for old imports); `dot_healer.py` imports from `recording`.
  - `build_svg(rec, w, h, include_raw=, layers=)`: white paper, raw strokes in
    their luminance grey (alpha kept), optimized layer `#d9480f`, effect layer
    `#1c7ed6`. Metadata only when the raw layer is included.
  - `thumbnail_svg`, `write_atomic`, `unique_path`, `timestamped_name`.
  - **`Recorder`**: fed by a new `preview.add_listener()` hook, which sees
    every broadcast, browser or not. It copies the page's rules (new stroke
    after `pen_up`, metadata latched per stroke, `replay` strokes skipped).
    It records the optimized/effect layers, and keeps the message log that
    rebuilds the page's picture.
- [x] **Replay tag fixed** (§2 item 14).
- [x] **`hello`** (`preview.register_hello_provider`): settings, the drawing's
      message log, plotter/OSC status, paper, lag, version. The page clears
      and replays the log through its normal message handler, so a reload,
      a second tab, or reconnecting to a restarted app shows the drawing so
      far. Message callbacks can now **return a reply** for the page that
      asked.
- [x] **Autosave:** `autosave.svg` in the drawings folder, rewritten every 2 s
      while the drawing changes (atomically). **Clean exit and "New drawing"
      save the session as `drawing-<date>_<time>.svg` and remove
      `autosave.svg`.** So an `autosave.svg` found at startup means a crash,
      and it's kept as `recovered-<time>.svg`: no prompt, nothing lost.
- [x] **"clear" → "new drawing"**: saves on the computer, then every open page
      clears (`new_drawing` broadcast).
- [x] **SVG export from Python** (`save_svg {filename, layers}`); PNG still
      renders in the page (`save_file`). The old JS recording/SVG code
      (`buildRecording`, `layerSvgParts`, `allStrokes`, `xmlEscape`) was
      removed.
- [x] **Settings** (`PantographApp/settings.py`): `settings.json` holds the
      page's own `axi_*` key → string map, so the page code barely changed.
  - The page's storage calls go through a `store` wrapper that pushes every
    change (`save_settings`, debounced).
  - On `hello`, if the computer's copy differs, the page adopts it and
    **reloads once**, so every control is rebuilt. If the computer has none
    yet, the page's copy is adopted.
  - At startup, `engine_messages()` turns the saved keys into the same
    `set_*` messages the page sends, applied before any page connects.
    Effect-param keys (`<effect>_<ATTR>`, both halves can contain `_`) are
    resolved by the engine via `set_effect_param_key`.
- [x] **Paper size + AxiDraw model** (engine side): `AXIDRAW_MODELS` (7
      models: name, long/short travel), `set_paper(w, h)` (either
      orientation, clamped to the model's reach, refused mid-plot, recomputes
      the mapping + effect context), `set_model(n)` (re-clamps; reconnects
      if connected); `ad.options.model` set on connect. Saved as
      `axi_model`/`axi_paperW`/`axi_paperH`. **The page controls come in
      Phase 5.**
- [x] Drawings go to `Documents/Pantograph/` (`--data-dir` overrides); the
      version shown comes from `pyproject.toml` (`shell.app_version()`).
- [x] README updated (drawings folder, settings, new drawing, 127.0.0.1:5810,
      the format section, the Files table).
- **Checkpoint:**
  - [x] A reload mid-drawing loses nothing (real-browser test: 25-point
        stroke, reload, still there; then "new drawing" saves and clears).
  - [x] The exported SVG round-trips to exactly the recording; the format is
        unchanged, so it replays like the old files. Every
        `saved_drawings/` file still loads.
  - [x] Tests: `tests/test_session.py` (12, unit) + 8 new end-to-end tests
        (catch-up on connect, autosave → new drawing, quit saves, crash →
        `recovered-…svg`, `save_svg`, replay not recorded, settings persist
        and apply at startup) + a browser test where the page adopts the
        computer's settings. **53 tests pass.**

<details><summary>Original Phase 3 plan</summary>

The most important structural change. Read §2 items 1 and 10 first.

- [ ] `recording.py`, the single Python implementation of
      `draw2axi-recording` v1:
  - **`Recorder`** subscribes to the same `point`, `pen_up` and `layer`
    messages the browser receives, and copies the current JS rules:
    - a new stroke starts on the first point after a `pen_up`;
    - stroke metadata is latched once per stroke;
    - strokes tagged `replay` are excluded.

    The canvas size comes from engine state, which fixes the stale-size bug.
  - It records the optimized and effect layers too, for export.
  - `load_svg`, `build_svg` and `thumbnail_svg` (light, decimated, no
    metadata) move here. `svg_transform.py` re-exports them.
  - **The format stays compatible:** `format: "draw2axi-recording"`,
    `version: 1`, the same fields, and points as `[t, x, y, pressureRaw]`
    with unrounded floats. Old SVGs load, and new SVGs load in old code.
  - **Export style:** white background; raw strokes in greyscale (luminance
    `0.2126 R + 0.7152 G + 0.0722 B`, alpha kept); the optional optimized
    and effect layers in one accent colour each. The metadata keeps the
    original colours.
- [ ] **Autosave:** after each stroke ends (at most once every 2 s), write
      `autosave.svg` in the drawings folder, atomically (temporary file, then
      `os.replace`). It shows up in the drawings list like any other file.
      There's no restore prompt.
- [ ] "Clear" becomes **"New drawing"**. It saves the current drawing first if
      it has strokes.
- [ ] `PantographApp/settings.py`: a JSON file with defaults defined in Python.
      Unknown keys are ignored and missing ones take the default. It covers
      everything in `localStorage` today (the `axi_*` and `axi_fx_*` keys),
      plus the OSC port and the paper settings. It's applied at startup,
      before any browser connects.
- [ ] **Paper size and AxiDraw model** replace the hard-coded
      `PAPER_WIDTH_IN` / `PAPER_HEIGHT_IN`.
  - Presets: Letter, A4, A3, Tabloid, and Custom (inches).
  - The model sets the travel limits. Paper larger than the machine's travel
    is clamped, with a warning.
  - A change recomputes the mapping, the effect context (`Ctx(x_max, y_max)`)
    and the tilt centre. Find every place that reads the paper constants.
  - Changes are refused while the plot queue isn't empty.
  - Pass the model to pyaxidraw via `ad.options.model`.
- [ ] Data folders via `platformdirs`. Drawings go to `Documents/Pantograph/`,
      which handles a Documents folder redirected by OneDrive; `--data-dir`
      overrides it. Settings, logs and runtime go in the per-user app folders.
- [ ] `hello` contains: settings, effect specs, the current drawing (all
      layers), plotter status, lag, the IP list, the OSC port and the version.
- **Checkpoint:**
  - A reload mid-drawing loses nothing.
  - An exported SVG replays like one from the old browser code (compared in a
    test).
  - Every SVG in `saved_drawings/` still loads.

</details>

### Phase 4: Plotter optional, `iDraw_to_svg/` removed

Done 2026-09-19.

- [x] Plotter status: `connected | not_found | unavailable | dry_run |
      error(message)`, broadcast as `plotter_status` on change. (Built in
      Phase 1; it's also in `hello`.)
- [x] With no AxiDraw, everything else works: the plotter thread consumes
      and logs commands with nothing attached, and recording, saving and
      replay all run. **Greying out the plot buttons (with the reason) is
      page work, so it's in Phase 5.**
- [x] `iDraw_to_svg/` deleted. Its one sample drawing moved to
      `saved_drawings/drawing_from_iDraw_to_svg.svg` and is in the round-trip
      test, so files made by the old copy keep loading. README: setup step 7
      is one command with or without an AxiDraw, the "No AxiDraw" notes in
      step 10 are rewritten, the internals section and Files row are gone,
      and a link broken by Phase 3's heading change is fixed. The
      `remote-version-sync` project memory was deleted. (`MEETINGS.html` is
      history and still mentions it, correctly.)
- **Checkpoint:** [x] `--dry-run` runs the whole app with the plotter in
  `dry_run` (every end-to-end test does this); "not found" was seen in the
  Phase 1 code path; [x] **54 tests pass**.

### Phase 5: The UI

Done 2026-09-19, apart from Marc's review. The page is `PantographApp/ui/`
(`index.html`, `app.css`, `app.js`, `icon.svg`), served as plain files.

- [x] **5a, move.** The page left `preview.py`. The effect list, the IP list,
      the AxiDraw models and everything else now arrive in `hello`; the
      `EFFECT_SPECS_PLACEHOLDER` substitution is gone. *5a and 5b landed
      together:* commits are Marc's, so there was no point splitting them.
- [x] **5b, restyle and reorganize.** What was built:
  - **Look:** the drawing is white paper on a cool grey "drafting table";
    strokes are each stroke's colour as a grey of the same brightness (the
    same rule as the saved SVG). Accents are only the plotter's layers
    (orange pen path `#d9480f`, blue effects `#1c7ed6`, as in the SVGs);
    controls are dark ink. System font, monospace for numbers. Light only,
    on purpose: the page is paper. The logo is a small pantograph linkage,
    and the paper's caption gives its size in inches and the AxiDraw model.
  - **Top bar:** name; **iPad** and **Plotter** status buttons (coloured dot
    plus a word); `iDraw → <IP> port <port>` with Copy IP; plotter lag;
    "live"/"reconnecting…" (the page's own link); **Quit** (asks first; the
    page then says Pantograph has stopped).
  - **iPad panel:** *Waiting / Receiving / Idle / Problem / Stopped* with a
    short line of detail, the address to type in (plus Tailscale if present;
    other adapters fold away under "Other addresses", since they're rarely the
    answer), Restart listener, the port box — which applies itself two seconds
    after the number stops changing, no button, keyboard entry only (saved as
    `axi_oscPort`; `--osc-port` still wins) — and the Troubleshoot checklist (the 7 steps, with the live IP and port filled in;
    the Windows or Mac variant of the firewall steps, picked from the
    browser) and **Copy diagnostics**.
  - **Plotter panel:** *Connected / Not found / Error / No pyaxidraw / Dry
    run*, plus *Motors off* while connected, the engine's message, Connect
    (Reconnect when connected), Home, **Disengage / Re-engage motors**,
    Troubleshoot (the 6 steps) and Copy diagnostics.
  - **Stage:** the paper, sized to fit; layer chips (Drawing / Pen path /
    Effects) choose what's shown **and** what Save SVG/PNG includes (one
    control instead of the old separate download tickboxes), each with an
    **i** button explaining what that layer is. The pen path is
    off by default, since it sits exactly on top of the drawing. Below: New
    drawing, **Discard…** (in-page confirm), Save SVG, Save PNG (now on white).
  - **Sidebar tabs:** Plot (model, paper presets + custom inches with the
    "reaches" note, Home, pen positions with Test buttons, variable pressure,
    flip, tilt, "Keeping up" (the optimizer), preview size under a fold,
    Reset); Effects (built from `effect_specs`, **Effects only** at the top:
    it sits with the effects rather than in Plot); Tools; Drawings.
  - **Replay card** above the tabs while a saved drawing plots: name,
    `done / total`, a bar, Pause/Resume and Cancel (asks first).
  - **First run:** a card over the paper with the iDraw steps, this machine's
    IP and port in large type, and a link that opens the iPad Troubleshoot,
    shown until anything arrives from the iPad.
  - **Greyed out** when the plotter isn't connected (dry run counts as
    connected): pen tests, Home and Plot buttons; Home and Plot are also out
    while the motors are released, and Plot while another plot runs. Each
    says why on hover.
  - **Wording:** labels and messages are kept to a functional line —
    "Paused, pen up", "Last point 4 s ago" — after Marc found the first pass
    wordy (2026-09-20).
  - **Viewer:** Open (Drawings tab, a tool's result, an imported file) shows
    that SVG in place of the live paper, with "Plot it" and "Back to live
    drawing". A live stroke arriving switches back automatically.
- [x] 1B rules (§7): no `<a download>`, blob downloads, `window.open`,
      `alert()` or `confirm()` (the one `ask()` dialog is in-page); the
      clipboard has a textarea fallback for older webviews.
- [x] Checked in Edge (headless, `tests/test_app.py`) and in **WebKit**,
      Safari's engine, via Playwright (`test_page_works_in_webkit`; needs
      `python -m playwright install webkit` once, else skipped). Real Safari
      on a real Mac is still part of the Phase 10 Mac session.
- **Checkpoint:** Marc reviewed it on 2026-09-20; everything below came out
  of that review.

**UI round 2 (2026-09-20).** What changed, and why:
- **The canvas is the paper.** It used to be the iPad's canvas; now it's the
  paper at its set size, with the iPad's canvas fitted onto it the way the
  engine fits it, and rulers down the left and along the bottom that switch
  between inches and mm (`axi_units`). So the preview shows where the pen
  actually goes. The preview-size and origin settings are gone with it, and
  the page asks for the drawing again (`hello_again`) when the paper changes,
  since resizing a canvas clears it.
- **The pen path appears as the pen draws it.** The layer messages used to go
  out when a command was *queued*, so the orange path raced ahead of the
  machine. Each queued command now carries its own layer message and the
  plotter sends it as it runs the command (`Queued`). In effects-only mode the
  base line is never queued, so the pen path no longer shows there — right,
  since the pen doesn't follow it.
- **One canvas.** Opening a file shows it in a preview panel with **Import to
  live drawing** / **Back**. Importing plots it *and* records it, so it joins
  the drawing exactly as if it had been drawn. The Drawings tab, its
  thumbnails and its list are gone; Open file… sits in the Plot tab and uses
  the system's Open window (`PantographApp/dialogs.py`), starting at the
  drawings folder and then at the last folder used (`axi_lastOpenDir`).
- **Saving.** Save SVG / Save PNG are gone. One **Save as…** between New
  drawing and Clear opens the system's Save window and writes the SVG
  (`axi_lastSaveDir`); PNG export is dropped, since the SVG is the format that
  can be replotted and edited. **Clear** replaces Discard, and **New drawing**
  offers Save as… / Discard when the drawing isn't saved.
- **Motors and home.** Disengage/Re-engage no longer moves home; a separate
  **Set as home** in the Plotter panel makes the carriage's spot home.
- **The port box** applies itself two seconds after the number stops changing.
- **The i buttons** are small filled circles inside the control they explain.
- **Wording** is shorter throughout.

**UI round 3 (2026-09-20).** The second review, and the principle above:
- **A menu bar**, the standard layout for a creative tool: a slim second bar
  under the status bar. **File** (New canvas, Import to canvas…, Save as…,
  Discard canvas), **Preferences** (Plotter preferences…, Edit layout…, Heal
  dots automatically) and **Effects** (the effects window). "Drawing" became
  "canvas" throughout.
- **The right panel is the plotter controller** and nothing else: Home the
  carriage, the machine and paper, and the three pen positions with their
  tests. Everything else moved into the Plotter preferences window (pressure
  updates, tilt, keeping up, reset) or the Effects window.
- **The layout editor** (`layout.py`, and the same arithmetic in `app.js`).
  The paper is the anchor and never moves. Two rectangles sit on it: the
  **drawing** (turquoise, with its top edge marked; move, turn, scale) and the
  **AxiDraw** (orange, with a house at its home corner; move, turn — its size
  is the model's reach). Editing is behind Preferences → Edit layout…, with
  Fit to paper / Cancel / Done, and the layout is saved (`axi_layout`).
  Turning the machine's rectangle is what replaced flip H/V.
  - The canvases extend 1.5″ past the paper, so a machine bigger than the
    sheet — and the handles — are still on screen; the white sheet is its own
    element underneath.
  - Out of reach is shown while editing and in the controller, and those
    points **aren't drawn at all** (changed 2026-09-20 from clamping them to
    the edge, which piled ink up along it): the stroke ends at the edge and
    starts again where the pen comes back within reach, as it would if the
    paper ran out.
- **The Tools tab is gone** — flip, minimum-width filter and heal dots. They
  were retroactive edits to a finished drawing. `svg_transform.py` and
  `dot_healer.py` remain as command-line tools outside the app, and Heal dots
  stays, because it changes what the pen does *while* drawing.
- **A saved file is the sheet, not the tablet** (Marc, 2026-09-20: a turned
  layout plotted correctly but saved unturned). `_paper_space_svg` writes the
  points where the pen went on the paper, at 96 per inch, with `space:
  "paper"` and `paperIn` in the recording; the viewport is the sheet. So the
  file matches what was plotted, and `_to_canvas_space` puts an imported one
  back in exactly the same place whatever the layout is now. Files without
  `space` are read as tablet coordinates, as before.
- Smaller details from the same review: **Home machine** (was "Home the
  carriage"), **Heal dots** with its own i button, i buttons shrunk to a
  superscript, thinner rectangles with a curved double-arrow turn handle on a
  short stem and quiet "ipad"/"plotter" labels that turn with them, and the
  coarse ruler unit labelled **cm** (it was always centimetres, mislabelled
  mm).
- `thumbnail_svg` went with the drawings list that used it.

### Phase 6: Tools and drawings backend

Done 2026-09-19 (hardware checks at the top). In `listen_to_idraw.py` and
`PantographApp/library.py`.

- [x] **WebSocket messages:** `library_list`, `open_folder` (Explorer /
      Finder), `import_file` (`{filename, text}` from the page's picker, or
      `{path}` from `--open`; copied into the drawings folder under a
      non-clashing name, refused unless it holds a recording), `plot_drawing
      {name}`, `tool_apply {tool, source, percent}`, `discard_drawing`,
      `disengage_motors`, `diagnostics`, and `replay_pause` / `replay_resume`
      / `replay_cancel`. Replies: `library`, `imported`, `tool_result`,
      `diagnostics`, `error`; broadcasts: `library_changed`,
      `replay_progress`.
- [x] **Motors are separate from the connection** (`set_motors`, reworked
      2026-09-20 after Marc asked why disengaging disconnected). Releasing
      them lifts the pen, cuts the XY motors and drops the queue (those moves
      were planned from where the carriage used to be); the AxiDraw stays
      connected, and moves are dropped while they're off (the pen still works:
      it's a servo). Re-engaging calls pyaxidraw's `enable_motors()` and zeroes
      its tracked position, so wherever the carriage was pushed to is home —
      which is what it was pushed for. `plotter_status` carries
      `motors: true/false`.
- [x] **Names from the page** go through `library.resolve()`: a plain `.svg`
      file name that exists in the drawings folder, nothing else (tested with
      `../` and URL-encoded variants).
- [x] **Tools** run on the drawing shown: a saved one (`source` = its name)
      or the live session (`source` = null). They save
      `<stem>_flipH.svg`, `_flipV`, `_minWidth<N>`, `_healed`, never
      overwriting. (The guide said "as the CLIs use today", but only
      `dot_healer` had a suffix; svg_transform's GUI asked for a name. The
      heal output matches the committed `drawing_dense_healed.svg`
      exactly: tested.)
- [x] **Worker thread:** `tool_apply` returns a `Future`; `preview.py` sends
      its result when it's ready, so a 9 MB heal (~5 s) never blocks the
      WebSocket.
- [x] **Thumbnails:** `/thumb/<name>`, built with `thumbnail_svg()` and
      cached **in memory** by name and modification time. (Not next to the
      drawings, as planned: that folder is the user's, and cache files would
      clutter it.) `/drawings/<name>` serves the file for the viewer. Both
      are sent with `Content-Security-Policy: sandbox`, since an imported SVG
      could carry script.
- [x] **Pause/resume and cancel a replay.** One replay at a time
      (`start_replay` refuses a second). The replay thread checks
      `_replay_cancel` per point and waits while `_replay_pause` is set.
  - **Plotter while paused:** lifts the pen once and waits; on resume the pen
    goes back down only if the next command draws, so a cancel after a pause
    never leaves a dot. The optimizer skips while paused, and on resume the
    queued commands' timestamps move forward by the pause (the pen-rest
    commands, tracked by identity, are remapped too), or the pause would read
    as lag and thin the drawing.
  - **Cancel** empties the queue *before* un-pausing (else the plotter would
    draw what's queued in that moment), then the replay thread ends the
    stroke, clears the queue again, queues a pen-up and rebuilds the effects.
  - **The iPad keeps working during an import** (reversed 2026-09-20, when
    imports became part of the live drawing). Both would write the same pen
    state, so a stroke drawn during an import is shown and recorded
    immediately and *plotted* once the import has been fed in
    (`_capture_live_point` → `_flush_live_strokes`). The queue is in order, so
    it lands behind the import — "queued", like any stroke.
  - A replay counts as running until the plotter has drawn everything it
    was fed (`phase: "finishing"`). Progress counts points fed.
- [x] **Flags `--open FILE` and `--tab NAME`**, carried to the page as
      `#tab=…&open=…` (`shell.ui_url`). **`python svg_transform.py
      [file.svg]`** runs the app with `--tab tools --open file.svg`; with a
      copy already running, the file is handed to it (`shell.ask_running`).
      The tkinter GUI is removed; `--selftest` stays. `python dot_healer.py`
      is unchanged.
- **Checkpoint:** passed at the time. **Superseded on 2026-09-20:** the Tools
  tab was deleted (see round 3 in Phase 5), so `svg_transform.py` and
  `dot_healer.py` are command-line tools again, with their own tests
  (`tests/test_recordings.py`).

### Phase 7: IP list and port errors

Done 2026-09-19, in `PantographApp/netinfo.py`, pulled forward because the
top bar and the checklist need it.

- [x] **Local IPs:** the primary one via the UDP "connect" trick (no packet
      is sent), then the rest from `getaddrinfo(hostname)`. `100.64.0.0/10`
      is labelled Tailscale, other non-primary ones "other adapter (VPN,
      wired…)"; `127.*` and `169.254.*` are hidden. Offline, whatever the host
      name gives is listed. The engine caches the list for 10 s.
      Checked against `ipconfig` on Marc's PC: `10.0.0.173` (primary) and a
      `26.x` VPN-style adapter, both found.
- [x] **OSC port busy:** the iPad button shows *Problem* with the message,
      and its panel offers Change port (Phase 1 already reported
      `port_busy`; tested in `test_busy_osc_port_is_reported_not_fatal`).
- **Checkpoint:** passed on Marc's PC.

### Phase 8: Launchers and installers

**Windows only for the demo (Marc, 2026-09-21).** macOS is still wanted later,
so `Pantograph.command` and `install-mac.sh` stay specified below but unbuilt.
Anything shared — finding the project root, the uv runtime folder, the launch
arguments — is written platform-neutral, so the Mac versions are a
transcription rather than a redesign.

**One trap this phase introduces.** The launcher runs `uv run --locked`, which
refuses to start if `pyproject.toml` and `uv.lock` disagree. Adding a
dependency without running `uv lock` leaves the repo working and the installed
app dead on launch. Re-lock and commit the lock with any dependency change.

**Launchers** (`Pantograph.bat`, `Pantograph.command`) are what actually start
the app. Both installers and the click route end by running one. Both
launchers follow the same steps:
1. Find the project root from the launcher's own location (the parent of
   `PantographApp/`), never from the working directory. A double-clicked
   `.command` starts in the home folder.
2. Windows: refuse to run from inside a zip (G-7).
3. Make sure uv is present, in a **private** folder:
   - Windows: `%LOCALAPPDATA%\Pantograph\runtime\uv\`
   - Mac: `~/Library/Application Support/Pantograph/runtime/uv/`

   If it's missing, download the **pinned uv release** for the machine's
   architecture from GitHub with `curl`, and unpack it with `tar` (both are
   built into Windows 10+ and macOS). Don't use uv's own `irm | iex` or
   `curl | sh` installers: they change the PATH, and they're what antivirus
   software and IT policies block.
4. Keep everything in the runtime folder and out of OneDrive/iCloud: point
   `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR` and `UV_PROJECT_ENVIRONMENT` inside
   it, and set `UV_PYTHON_PREFERENCE=only-managed`.
5. On the first run, print: "First launch: downloading Python and libraries
   (about 150 MB). This only happens once." Then run
   `uv run --locked --project "<root>" python "<root>/listen_to_idraw.py"`,
   passing along any arguments the launcher was given (`%*` / `"$@"`; CI
   uses this for `--smoke-test`).
6. On failure, print a plain message plus the log path, and keep the window
   open.

**`Pantograph.bat` (Windows):**
- CRLF line endings, and every path quoted.
- Ctrl+C shows "Terminate batch job (Y/N)?". That's harmless.

**`Pantograph.command` (Mac):**
- LF endings, a `#!/bin/bash` first line, and the executable bit committed
  (`git update-index --chmod=+x`).
- `cd "$(dirname "$0")/.."`
- Closing the Terminal window sends SIGHUP, which `shutdown()` handles.

**One-line installers: the primary route.** They're what the README shows in
the "Recommended" column. Each downloads the release zip *without a browser*,
which is why neither platform shows a security prompt: browsers tag
downloads as "from the internet" (the macOS quarantine flag, the Windows
mark-of-the-web), and `curl` / `Invoke-WebRequest` don't. So Gatekeeper's
"Open Anyway" and Windows' "publisher could not be verified" never appear.
Both installers:
- extract the latest release zip into a fixed folder, replacing any previous
  version (re-running = updating; user data lives elsewhere, so it's
  untouched);
- create a shortcut to the launcher and start it;
- need no admin rights, and stay short and readable, since people pipe them
  straight into a shell.

**`install-windows.ps1`:**
- Run as `powershell -ExecutionPolicy Bypass -c "irm <release URL>/install-windows.ps1 | iex"`.
- Installs to `%LOCALAPPDATA%\Pantograph\app\`, with Start-menu and Desktop
  shortcuts to `PantographApp\Pantograph.bat`.
- This is the `irm | iex` pattern that launcher step 3 avoids. That's fine
  here, because the user types it deliberately and it points at our own URL.
  It may still be blocked on managed machines (G-14); the click route is the
  fallback.

**`install-mac.sh`:**
- Run as `curl -fsSL <release URL>/install-mac.sh | bash`.
- Installs to `~/Pantograph/`, with an optional Desktop shortcut to
  `PantographApp/Pantograph.command`. Never uses `sudo`.

**Built and tested 2026-09-21 (Windows).** `PantographApp/Pantograph.bat` and
`install-windows.ps1` both work, verified against a private runtime folder
(`LOCALAPPDATA` pointed at a scratch directory) so the real install was never
touched:

- Clean first run: fetches uv 0.12.17, then CPython 3.12.14 and 20 packages,
  then starts the app. Second launch: 0.7 s, no network.
- The app really runs through it — HTTP 200 on the UI port, the usual startup
  banner — not just `--help`.
- The installer unpacks to `%LOCALAPPDATA%\Pantograph\app\` and makes
  Start-menu and Desktop shortcuts with a real icon. Re-running it updates:
  a file left in the old `app` was gone afterwards, and `runtime\` (uv, Python,
  the venv) survived untouched.
- Both guards fire: running from a `\Temp\` or `.zip\` path says "extract the
  zip first" (G-7), and a launcher without the app beside it says so.

Two things found by building it, both now fixed:

- **`uv run` installed the dev group** — pytest and playwright, 37 MB of
  browser automation an end user has no use for. The launcher passes
  `--no-dev` (28 packages → 20).
- **`curl` and `tar` must be called by full path** from `%SystemRoot%\System32`.
  Git for Windows puts a GNU `tar` on PATH, which cannot read a zip at all: it
  reads the `C:` in the destination as a remote host and fails with "resolve
  failed". There's a PowerShell `Expand-Archive` fallback for Windows 10 builds
  before 17063, which have no `tar`.

`PantographApp/ui/icon.ico` is generated from the same drawing as `icon.svg` by
`PantographApp/deferred/make_icon.py` — seven sizes, drawn directly rather than
rasterised, since the mark is only lines and two dots and a rasteriser would be
a dependency.

- [ ] **Checkpoint (Marc):** on a Windows account with no Python or uv, the
  one-line installer and the click download both work end to end, and the
  second launch works offline. Needs a real release published first (Phase 9),
  since the installer's default URL points at `releases/latest`.
- [ ] `install-windows.ps1` has `$Repo = 'OWNER/REPO'` as a placeholder. It
  needs the real repository before release.

### Phase 9: CI and release

- [ ] GitHub Actions on `windows-latest` and `macos-latest`: `setup-uv`,
      `uv sync --locked`, `uv run pytest`, plus a **launcher smoke test**. The
      real launcher runs with `--smoke-test`: it starts the app, checks
      `/health` and `hello`, sends a scripted OSC stroke, saves, checks the
      SVG and quits. This proves the Mac install path without owning a Mac.
- [ ] Release on a version tag:
  - build `Pantograph-<version>.zip` with `git archive`. Mark dev-only and
    bulky files `export-ignore` in `.gitattributes`: `tests/`, `.github/`,
    `.claude/`, `saved_drawings/` (49 MB) and `PantographApp/deferred/`;
  - attach the zip, `install-windows.ps1` and `install-mac.sh`;
  - the README links to the latest release.
- [ ] A version number in `pyproject.toml`, shown in the UI.

### Phase 10: Docs and the real-hardware pass

- [ ] README setup rewritten so the **primary and alternative routes sit
      side by side**. Anyone who doesn't like the command line sees the
      click-download route immediately, not further down the page. One
      table, one row per OS:

      |             | **Recommended: paste one command** | **Or: download and double-click** |
      |-------------|------------------------------------|-----------------------------------|
      | **Windows** | Open PowerShell, paste `powershell -ExecutionPolicy Bypass -c "irm …/install-windows.ps1 \| iex"` | [Download the zip](…/releases/latest) → right-click → Extract All → open `PantographApp` → double-click `Pantograph.bat` → "More info" → "Run anyway" |
      | **Mac**     | Open Terminal, paste `curl -fsSL …/install-mac.sh \| bash` | [Download the zip](…/releases/latest) → double-click to extract → open `PantographApp` → double-click `Pantograph.command` → if blocked: System Settings → Privacy & Security → "Open Anyway" |

  - Right under the table: one sentence on how to open PowerShell or
    Terminal, and a note that the command route shows no security prompts.
  - The security-prompt screenshots go in a collapsed `<details>` block below,
    so they don't push the table apart.
  - A `|` inside a table cell must be escaped as `\|`. GitHub renders inline
    code in table cells, and long commands wrap.
  - Then: enter the IP and port shown in the app into iDraw.
- [ ] README Troubleshooting: **networks that isolate devices** (school, work,
      hotel and guest Wi-Fi), with a hotspot to test and Tailscale to fix;
      Windows Public networks; firewall.
- [ ] A "Developers" section: `uv run listen_to_idraw.py` and
      `uv run pytest`, plus the plain `pip install` route (including
      `pip install PantographApp/vendor/axidrawinternal-3.9.6-py2.py3-none-any.whl PantographApp/vendor/AxiDraw_API_396`).
- [ ] Run the §6 test list.
- [ ] Add a note to `MEETINGS.html` if relevant.

---

## 5. Behaviour changes users will notice

For the release notes:
- Fast strokes no longer tear into dots (Phase −1).
- Finger strokes plot (Phase −1).
- Drawings survive a page reload, and the current drawing is autosaved.
- "Clear" becomes File → New canvas, which offers to save first; Discard canvas
  clears without saving.
- A new UI: status buttons for the iPad and plotter with Troubleshoot
  checklists, a menu bar, and a plotter-controller panel.
- The motors can be released and taken back without disconnecting.
- "Plotter behind" replaces the old lag readout, and now falls as the plotter
  catches up.
- "plot svg" becomes Open file… → a preview → Import to live drawing, with
  pause, resume and cancel. An imported drawing becomes part of the drawing.
- Saving is one Save as… (SVG) through the system's Save window; PNG export is
  gone.
- The canvas shows the paper, with rulers, and the machine's reach on it.
- Flip H/V and the Tools tab (flip, minimum width, heal dots) are gone; where
  the plot lands is set in Preferences → Edit layout instead.
- Menus (File / Preferences / Effects) replace the sidebar tabs; the sidebar is
  the plotter controller.
- Saves go to `Documents/Pantograph/`, not `saved_drawings/`.
- The canvas, the UI and exports are white paper with greyscale strokes.
- Paper size and AxiDraw model are settings.
- `python svg_transform.py drawing.svg` opens that drawing in the app; its
  tkinter window is gone, and its transforms are command-line only.
- Settings live in a file, not the browser.
- The UI is at `127.0.0.1:5810` instead of `localhost:5000`.
- The plotter is detected automatically; `--dry-run` still forces
  "no plotter".
- The console is quieter; `--verbose` restores the per-point output.

---

## 6. Test list (before the first release)

On Windows 11 and one Mac:
- [ ] Fresh machine or account: **both** install routes work.
- [ ] iPad draws → preview → reload the tab → nothing lost; autosave is in the
      drawings list.
- [ ] With the AxiDraw: live plot, pen tests, Home, replay, effects; fast
      strokes stay continuous.
- [ ] Unplug the AxiDraw mid-plot → error → Connect works again.
- [ ] Close the console or Terminal mid-plot → the pen lifts.
- [ ] Launch twice → the second launch opens the first.
- [ ] Firewall: Allow works; the Troubleshoot steps fix a Cancel.
- [ ] Paper/model change → the mapping is correct; refused mid-plot.
- [ ] Old SVGs (including `iDraw_to_svg` ones) load, transform, heal and
      replay.
- [ ] Second launch with no internet.
- [ ] Re-running an installer updates the app and keeps settings and drawings.
- [ ] The UI works in Chrome/Edge and Safari. (Automated: Edge and WebKit
      pass. Real Safari is left for the Mac session.)

---

## 7. Staying ready for 1B (native window)

1. **One seam:** the UI only ever opens via `PantographApp.shell.open_ui(url)`.
   In 1B it tries pywebview, and on any exception falls back to the browser
   (`--browser` forces the browser).
2. **The main thread stays free** (Phase 1). pywebview needs it, since Cocoa
   requires it on macOS.
3. **No browser-only features** (the Phase 5 rules).
4. **Quit is a button, and `shutdown()` is idempotent.** Closing the 1B window
   calls it.
5. **Check the UI in Safari.**

The known 1B risks are in the project memory (`option-1b-risks`): native
dependencies (pythonnet/WebView2 on Windows, pyobjc on Mac), webview quirks,
the lingering Terminal window on Mac, and "Python" as the app name in the Dock.

---

## 8. Out of scope, but kept in mind

- **Uploading to the Pantograph website.** Marc's site is a static Astro site
  on Cloudflare Pages (`MarcDunand/Personal-Website`, deployed by pushing to
  `main`). Uploads would need Pages Functions plus R2 or KV storage (free
  tier), an anti-abuse design, and the Python side doing the upload (no CORS
  issues). For now: keep export as a clean `recording.build_svg()` call.
  Drawings are several MB, so a lighter "web" export may be wanted.
- **Option 2** (PyInstaller `.exe`/`.app` built in CI) stays possible later.
  The entry point and data folders carry over.
- **The iDraw OSC dependency.** It's a third-party app; if it changes or
  disappears, input breaks. A fallback would be our own iPad web page sending
  points.

---

## 9. Cut for the demo (revive if needed)

Dropped under the scope rule on 2026-09-19. Each can come back if real users
hit the problem it solved.

- **"Fix firewall" button** (an elevated `New-NetFirewallRule` via a UAC
  prompt) → replaced by Troubleshoot steps.
- **Automatic Windows Public-network detection** (`Get-NetConnectionProfile`)
  → replaced by a Troubleshoot step.
- **Step-by-step troubleshoot wizard** with per-step automatic checks → a
  one-panel checklist with the live status dot.
- **Automatic plotter reconnect retries, and pausing the queue on error** →
  manual Connect; the queue is discarded on error.
- **Crash-restore prompt** → the autosave simply appears in the drawings list.
- **Drawings list extras:** Duplicate, Delete-to-trash, Reveal-this-file,
  Import-into-library → Open, Plot, "Open drawings folder", "Open file…".
- **Live on-canvas preview of the width filter** → apply, then open the result.
- **Handing a file to an already-running copy** → a message instead.
- **Launcher extras:** SHA-256 check of the uv download, macOS version check,
  Windows-on-ARM fallback.
- **Hosting pyaxidraw as a separate GitHub release download** → a copy in the
  repo.
- **Update-available check.**
- **Extra IP filtering** (hiding Hyper-V/WSL/VirtualBox adapters).
- **Settings schema versioning.**
- **Broad test matrix** (Windows 10, OneDrive-redirected Documents, non-ASCII
  usernames) → the lean list in §6.

---

## 10. Gotcha register

The known risks, each with its mitigation and phase. Re-check before release.

| ID | Gotcha | Mitigation | Phase |
|---|---|---|---|
| G-1 | Closing or reloading the tab loses the drawing | Record in Python; autosave; `hello` restores the page | 3 |
| G-2 | Any website can drive the plotter via the WebSocket | Origin + Host checks; bind to 127.0.0.1 | 2 |
| G-3 | The firewall prompt says "Python"; users click Cancel and OSC fails silently | iPad Troubleshoot step 4; README | 5, 10 |
| G-4 | Windows treats new Wi-Fi as **Public**, and inbound traffic stays blocked even after "Allow" | iPad Troubleshoot step 3; README | 5, 10 |
| G-5 | OSC port 8800 busy (another program or a second copy) | Single-instance check; problem state + Change OSC port | 2, 7 |
| G-6 | Port 5000 is taken by AirPlay on Macs | Default 5810, with fallback to 5811–5830 | 2 |
| G-7 | A Windows user runs the `.bat` from inside the zip viewer | Launcher detects a temp or `.zip` path and says "Extract the zip first" | 8 |
| G-8 | Wrong line endings break the launchers or installers | `.gitattributes` rules; CI runs the real launchers | 0, 9 |
| G-9 | Mac blocks the click-downloaded `.command` ("Open Anyway" in System Settings since Sequoia) | The primary route (`install-mac.sh`) avoids the quarantine flag entirely. The click route gets screenshots and a `chmod +x` fallback | 8, 10 |
| G-10 | Windows shows "Publisher could not be verified" for the click-downloaded `.bat` | The primary route (`install-windows.ps1`) avoids the mark-of-the-web entirely. The click route: expected, one screenshot | 8, 10 |
| G-11 | pyaxidraw supports Python ≤ 3.12 | Pin 3.12 | 0 |
| G-12 | pyaxidraw's URL is unversioned; its distribution name is `axicli`; and its `setup.py` injects the bundled `axidrawinternal` wheel by absolute temp path, which poisons `uv.lock` | Vendored unpacked, without `prebuilt_dependencies/`, plus the wheel on its own; both via `[tool.uv.sources]`. Upgrade steps in `vendor/README.md`. **Done (Phase 0)** | 0 |
| G-13 | First launch needs internet and about 150 MB | Clear message; README says so | 8, 10 |
| G-14 | Locked-down school or work machines block downloads or scripts | Pinned uv binary avoids the common blocks; the click route is the fallback for a blocked installer; otherwise no fix | 8 |
| G-15 | Dependencies or data inside synced or replaced folders | Runtime in local app data; drawings in Documents; replacing the app folder loses nothing | 3, 8 |
| G-16 | Quitting leaves the pen down | `shutdown()` on every exit path | 1 |
| G-17 | The plotter thread dies silently (missing pyaxidraw, unplugged cable) | Import inside `try`; per-command `try`; status + reconnect | 1, 4 |
| G-18 | Printing every point slows Windows | DEBUG-level logging only with `--verbose` | 1 |
| G-19 | `localhost` resolves to IPv6 first on some systems | Use `127.0.0.1` | 2 |
| G-20 | Launched twice, two copies fight over ports | `instance.json` + `/health` | 2 |
| G-21 | Browser shows a stale cached UI after an update | `Cache-Control: no-store` | 2 |
| G-22 | Large drawings (several MB) make the list and tools slow | Thumbnails (in memory, lazy-loaded); worker thread. A 9 MB heal takes ~5 s. **Done (Phase 6)** | 6 |
| G-23 | Old SVGs must still load and replay | Format stays v1; tests | 0, 3 |
| G-24 | macOS 15+ Local Network privacy might affect iPad input when launched from Terminal | **Unverified.** Check in the real-Mac session | 10 |
| G-26 | Our download links disappear | uv pinned; pyaxidraw vendored; our own releases on GitHub | 0, 8, 9 |
| G-29 | The iPad can't be "reconnected" from our side | Restart listener + Troubleshoot | 1, 5 |
| G-30 | Paper or model change mid-plot | Refused while the queue isn't empty | 3 |
| G-31 | Networks that isolate devices: the iPad can never reach the computer | Troubleshoot step 6 + README; no fix on our side | 5, 10 |
| G-34 | Non-ASCII log characters (`≈`, `→`) crash with an encoding error when output isn't a real console; the crash lands mid-stroke | UTF-8 stdout reconfigure + UTF-8 log file | 1 |
| G-36 | Stroke splitting relies on iDraw sending the state block only *before* a stroke, never during one | Any block field counts, which tolerates lost packets. If iDraw changes this behaviour, strokes will split or merge wrongly; `--raw-osc` shows it | −1 |
| G-37 | The click route's launchers sit in `PantographApp/`, one folder down, among many repo files | README table says "open `PantographApp`" explicitly; the installers create shortcuts, so the primary route never needs it | 10 |
| G-38 | Release zip bloated by `saved_drawings/` (49 MB) | `export-ignore` in `.gitattributes` | 9 |
| G-39 | A page's broadcasts sent while its `hello` is being built never reach it | `hello` takes the drawing snapshot last, and the IP list is cached; the window is now microseconds. Tests wait for `hello` before drawing, as the page does | 5 |
| G-40 | An imported SVG could contain script, and `/drawings/` serves it from our origin | `Content-Security-Policy: sandbox` on `/drawings/` and `/thumb/`; the page shows them with `<img>` | 6 |
| G-41 | Pausing a replay leaves a full queue, which the optimizer would read as falling behind and thin | The optimizer skips while paused | 6 |
| G-42 | "Lag" was the oldest queued command's age: it climbed all through a drawing and only hit 0 at the end, even while the plotter was catching up (Marc, 2026-09-20) | It's now the gap between drawing a mark and the plotter drawing it, measured on a clock that only runs while marks are being drawn (`_pen_clock`, `current_lag`). It grows while the pen runs ahead, falls whenever drawing stops, and is 0 when caught up. The same number feeds the optimizer | 5 |
| G-43 | The progress bar filled in the first seconds: it counted points *fed*, and feeding runs far ahead of the pen | Each queued command carries the index of the point it came from; the plotter reports it as it runs (`Queued.mark`), so the bar follows the pen | 6 |
| G-44 | The system's file windows must open on the main thread (macOS), but messages arrive on the server's thread | `run_on_main` queues them; `main()`'s loop serves them every 0.1 s. A dialog blocks that loop while it's open, which is fine — everything else runs on other threads | 6 |
| G-45 | An AxiDraw reaches past the sheet, so its rectangle (and the layout handles) fell off the canvas | The canvases cover the paper plus 1.5″ all round; the sheet is a separate white element inside | 5 |
| G-46 | The page's arithmetic and `layout.py` must agree, or the preview lies about where the pen goes | One formula, written twice (Python and JS) with the same names, and `tests/test_layout.py` pins the Python side. If one changes, change both | 5 |
| G-47 | Changing the paper resizes the canvases, which clears them | The page asks for the drawing again (`hello_again`) and replays it | 5 |
| G-48 | A saved SVG held the tablet's coordinates, so a turned or moved layout plotted correctly but saved as though it were square on the page | Saved files are in paper space (`space: "paper"`), and importing converts back. The recording stays tablet-space while the drawing is live, since that's what the pipeline replays | 5 |
| G-49 | A stroke drawn while an import *plotted* was shown and recorded but never plotted (Marc, 2026-09-20). Strokes are held back during an import so two writers don't splice into one stroke — but feeding takes seconds and plotting takes minutes, and the hold was keyed to the whole import, so anything drawn during the catching-up sat in `_live_pending` for good | The hold is keyed to `_import_feeding`, set only while points are being fed. Once the feed is in, the flush drains what was held and clears it, and later strokes queue themselves behind the import the ordinary way. `_wait_for_plotter` waits on the import's own marked commands, not on the queue, so those later strokes don't keep it "running" | 6 |
| G-50 | Changing the paper reopened the preview for the last file opened (Marc, 2026-09-20) | Resizing the canvases asks for a fresh `hello` (G-47), and `hello` carries the open file. The page now shows that preview on the first `hello` only — a later one is a catch-up, not a request to open a window | 6 |
| G-51 | An image sized with `max-height: 100%` inside a `1fr` grid track doesn't fit it: the track has a used height but its *computed* height is `auto`, so the percentage never resolves, and the drawing overflowed across the preview window's header | The window has a definite `height`, and the drawing is absolutely placed inside its box, where percentages resolve against a real height | 5 |
| G-52 | The iPad read *Idle* with a green dot forever, including when iDraw had been closed since before the app started (Marc, 2026-09-21) | OSC is connectionless: the iPad sends into the air, there is no socket to drop, and silence is all we ever observe. Past `IPAD_QUIET_SEC` (2 min) the page says *Quiet* with a grey dot and "iDraw may be closed" — the honest claim, rather than implying a connection | 5 |
| G-53 | `--help` crashed on a fresh Windows console: the text contains `→`, and the UTF-8 stream reconfigure lived in `setup_logging`, which runs long after argparse has printed and exited | `shell.use_utf8_console()`, called first thing in `main()`. `tests/test_app.py` runs the real program under `PYTHONIOENCODING=cp1252` | 8 |
| G-54 | Labels and values in the top bar sat a pixel or two off each other | The row centred each flex item's *box*, and 14px sans labels and 13px mono values have different line-box heights. `align-items: baseline` aligns the text instead of the boxes | 5 |
| G-55 | A negative margin pulled the bottom ruler across the canvas margin but did nothing for the side one | Its grid column was sized by the unit button, and a grid item sits at its column's start, so there was nothing to pull against. `justify-self: end` anchors it to the column's end first | 5 |

(Resolved and removed: G-25 Windows on ARM, not handled by decision; G-27
update notices, cut; G-28 OSC threading, done; G-32 lxml builds, covered by
CI; G-33 old macOS, cut; G-35 no-pressure strokes, done.)

---

## 11. Decisions log

No questions are open. Recorded answers, all folded into §1:

| Date | Question | Answer |
|---|---|---|
| 2026-09-18 | Drawings folder | `Documents/Pantograph/`; `saved_drawings/` stays in git as documentation |
| 2026-09-18 | Record in Python? | Yes |
| 2026-09-18 | Look | White paper, greyscale strokes, matching iDraw OSC, across the app |
| 2026-09-18 | Paper size / AxiDraw model | Saved settings, like tilt |
| 2026-09-18 | `python svg_transform.py` | Opens the app's Tools tab (most consistent look) — *superseded 2026-09-20: no Tools tab; it opens the drawing in the app* |
| 2026-09-18 | Licence | MIT |
| 2026-09-18 | Name | Pantograph |
| 2026-09-18 | Windows on ARM | Not handled |
| 2026-09-18 | Stroke boundaries | iDraw's state block; built first (Phase −1) |
| 2026-09-18 | OSC threading | Single thread |
| 2026-09-19 | No-pressure input | Runs of 5+ `1.0`s plot at 0.5 |
| 2026-09-19 | Scope | Demo: prefer saving time over polish |
| 2026-09-19 | Install routes | Both routes on both platforms; command line primary; README shows them side by side |
| 2026-09-19 | Pressure lag | Fix it, but deferred until a Pencil is available; patch saved |
| 2026-09-19 | pyaxidraw | Vendored in the repo (decided under the scope rule: simpler than a hosted download); split into package + wheel to keep `uv.lock` portable |
| 2026-09-19 | Live drawing during a replay | Ignored until the replay ends or is cancelled (Phase 6; the plan left it open) |
| 2026-09-20 | Motors | A Disengage / Re-engage toggle, independent of the connection; home only moves when **Set as home** is pressed |
| 2026-09-20 | An opened drawing | Previewed in its own panel, then imported into the live drawing — there's only one canvas |
| 2026-09-20 | Saving | One Save as… (SVG, system Save window). No PNG export: the SVG is the format that replots and edits |
| 2026-09-20 | The canvas | Shows the paper, with rulers in inches or mm |
| 2026-09-20 | Heal automatically | Live: a stroke starting within 0.15″ and 0.3 s of the last one's end carries on instead of lifting |
| 2026-09-20 | What the app is | A translator between physical and digital media, not an SVG editor (see the top of this file) |
| 2026-09-20 | The Tools tab | Deleted: flip, minimum width and heal are retroactive edits. The command-line tools stay |
| 2026-09-20 | Flip H/V | Gone: turning the AxiDraw's rectangle in the layout covers orientation, and no mirror is offered |
| 2026-09-20 | The paper | No longer clamped to the machine's reach: it's the sheet, and the layout says what the machine covers |
| 2026-09-20 | Out of reach | Not drawn at all (first tried: clamped to the edge). A pen can't draw past what it can touch |
| 2026-09-20 | The pen path's colour | The machine's orange, since it's the machine's line |
| 2026-09-20 | UI shape | Menu bar (File / Preferences / Effects) + a plotter-controller panel, the usual creative-tool layout |
| 2026-09-20 | What a saved file holds | The sheet of paper: points where the pen went, at 96 per inch (`space: "paper"`) — the file is the analogue of the plot |
| 2026-09-20 | OSC port | Applies itself 2 s after the number stops changing; no Apply button, keyboard entry only |
| 2026-09-20 | Address list | Only the address to use (and Tailscale); other adapters fold away |

---

## 12. Remaining risks

The problems that can't be engineered away. Keep them in view.

1. **No Mac on hand.** CI proves the Mac install. The iPad → Mac → AxiDraw
   flow and macOS prompts (G-24) need **one real-Mac session before
   release**.
2. **Click-route security prompts.** Users who skip the command line face
   Windows' "Run anyway" and, on Mac, the 4-step "Open Anyway" process. Some
   will give up. The README puts the prompt-free command route beside it. The
   only other ways out: Apple's fee waiver (for accredited educational
   institutions and nonprofits; it's an organization enrolment) or the $99/yr
   developer account (plus Option 2 packaging).
3. **Locked-down computers** may block the install entirely.
4. **Networks that isolate devices** (G-31): common on campus Wi-Fi. There's
   no fix on our side, only documentation.
5. **iDraw OSC** is a third-party app, and stroke splitting now depends on its
   state-block behaviour (G-36).
6. **Support requests land on Marc.** Troubleshoot and "Copy diagnostics"
   reduce them.
