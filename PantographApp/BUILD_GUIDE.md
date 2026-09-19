# Pantograph App: Build Guide

This is the step-by-step plan for turning draw2axi into one downloadable app,
plus every known gotcha and how each is handled. It is written for whoever
builds it (Marc, or a Claude session). Work through the phases in order. Each
phase ends with a checkpoint, and nothing moves on until that checkpoint passes.

Status: **Phase −1 done and confirmed by Marc (2026-09-19). Phase 0 is next.**
Nothing from Phase 0 onwards has been built yet. Tick the boxes as phases
land.

---

## Phase −1: Stroke boundaries from iDraw's state block (done first, by request)

Marc asked for this to be the first code change, confirmed working in the
plain `python listen_to_idraw.py` version before anything else proceeds.

- [x] Strokes end only when iDraw's **state block** arrives (any of `/r /g /b
      /a`, the tool flags, `/canvasWidth`, `/canvasHeight`, `/drawingWidth`,
      `/eraserWidth`), or where a replayed recording's stroke ends. Timing no
      longer splits strokes (`_state_block_seen`, `_end_stroke`).
- [x] Timing only **rests** the pen: after `PEN_REST_SEC` (0.15 s) without a
      point, it lifts the pen, bypassing the effects. If the stroke continues,
      the lift is taken back out of the queue if it hasn't been executed, or
      the pen is lowered again at the same spot (`_maybe_rest_pen`,
      `_resume_after_rest`).
- [x] **Single-threaded OSC** (`BlockingOSCUDPServer` with a 1 MB receive
      buffer). This had to land together with the block logic, which depends
      on messages being handled in order (§2.12). It was pulled forward from
      Phase 1.
- [x] Docs updated: README § Stroke boundaries, AGENTS.md §3.4, and the
      `dot_healer.py` docstring.
- [x] **No-pressure input plots instead of vanishing** (G-35): 5+ placeholder
      `1.0` values in a row are plotted at pressure 0.5, and a shorter all-`1.0`
      stroke gets 0.5 when it ends. Simulated: finger stroke, short finger
      stroke, finger tap, a glitch inside real pressure (still interpolated), a
      long run inside real pressure (0.5). All pass.
- [x] Simulation against the real handlers passes: the block splits strokes,
      a pause doesn't, a rest is taken back or re-lowered, taps dwell once,
      replay stays separate, and 120 fast points over real UDP arrive in
      order.
- [x] **Marc confirmed it works (2026-09-19).** What was checked:
  - fast strokes stay continuous (no dots);
  - separate strokes still lift between them;
  - pausing mid-stroke lifts the pen, and it continues the line when you
    carry on;
  - taps still make dots;
  - effects still work;
  - finger strokes (or any with `p=0.24` on every point) now plot.
- **Known consequence:** iDraw sends nothing on lift, so the *latest* stroke
  stays open until the next one starts. The pen is already rested, but
  stroke-end effects and the preview's pen_up run then. If this matters, a
  long idle timeout (say 2 s) could close the stroke; that would be a timing
  rule, but it would never split a continuous stroke.
- `iDraw_to_svg/` still uses the old timing rule. It's being deleted in
  Phase 4, so it was left alone.

---

## 1. What we're building, and what's already decided

**The product:** someone downloads a zip, extracts it, and double-clicks
`Pantograph.bat` (Windows) or `Pantograph.command` (Mac). The first launch
installs everything it needs. The app then opens in the browser as one cohesive
tool:
- live drawing from the iPad (iDraw OSC);
- live plotting when an AxiDraw is plugged in, or just the preview plus
  recording when it isn't;
- the post-processing effects;
- the SVG tools (flip, width filter, dot healing);
- a library of saved drawings.

**Decisions so far (2026-09-18):**

| Decision | Choice |
|---|---|
| Delivery | **Option 1A**: a zip plus double-click launchers that use `uv` to install Python and the dependencies. The UI is the normal browser. |
| Future | **Option 1B**: a native window via pywebview, with the browser as fallback. Built later as a thin layer. 1A must be built so that layer is easy to add (see §9). |
| Platforms | Windows and macOS. |
| Signing | None. Install warnings are acceptable. They are documented, with screenshots. |
| Share button | Not now. A future "upload to the Pantograph website" is expected (see §10). |
| Old commands | `python listen_to_idraw.py`, `python svg_transform.py` and `python dot_healer.py` keep working. |
| Layout | Pipeline Python stays at the **repo root**. App-specific files go in **`PantographApp/`**. No file moves; we build in place. |
| Ports | The web page and the live WebSocket feed merge onto **one port**. |
| `iDraw_to_svg/` | Merged away. "No plotter" becomes a status, not a second program. |
| Name | **Pantograph** for the launcher, the data folders and the window/tab title. |
| Drawings folder | Defaults to `Documents/Pantograph/` (can be changed in settings). The repo's `saved_drawings/` **stays in git** as documentation, and the app just doesn't write there. |
| SVG / canvas look | **White background, strokes in greyscale** (each stroke's recorded colour turned into grey), matching how iDraw OSC looks. Used for every export and for the live canvas, so the whole app looks the same. |
| Paper size + AxiDraw model | Saved settings, like x/y tilt, editable in the Plot tab. |
| `python svg_transform.py` | Opens the app's **Tools** tab with that file loaded (chosen because it looks consistent with the rest of the app). The tkinter window is removed. |
| Connection status | Clickable **iPad** and **Plotter** status buttons are always on screen, showing the state and offering reconnect/help. |
| OSC input | Switches to a **single thread**, so messages are handled in the order they arrive (§2.12). |
| Licence | **MIT.** `LICENSE` added at the repo root on 2026-09-18, plus a README line. It covers the code. |
| Platforms, fine print | Windows x64 and Macs (Apple Silicon, plus Intel on a recent macOS). **Windows on ARM is not specifically handled.** It's rare, and the risk is low (§11 Q8). Revisit only if a user reports it. |

**What doesn't change:** the drawing pipeline itself. Stroke inference,
spurious-pressure handling, mapping, the optimizer, effects and replay keep
their current logic. This project is about structure, UI and delivery. Behaviour
changes are called out explicitly where they happen.

---

## 2. Things found while reading the code (these shape the plan)

These were found by reading the current code. Several are existing problems
that the app would make worse if left alone.

1. **The drawing is recorded in the browser, not in Python.** `preview.py`'s
   JavaScript builds the recording (`completedStrokes`, `buildRecording`) from
   the live feed. **Reloading or closing the tab loses the drawing**, and a tab
   opened mid-session misses everything drawn before it. The browser also
   stamps each stroke's `canvasWidth`/`canvasHeight` from the last
   `canvas_size` message *it* received. A tab opened after iDraw sent its
   canvas size records the default 440×956 instead of the real size.
   → **Phase 3 moves recording into Python.**
2. **Anyone's web page can control the plotter.** The WebSocket server accepts
   any connection, and browsers don't apply the same-origin policy to
   WebSockets. Any website open in the user's browser could connect to
   `ws://localhost:5001` and send `replay`, `home` or pen commands.
   → **Phase 2 adds Origin and Host checks.**
3. **pyaxidraw only supports Python 3.8–3.12** according to its install docs.
   Its package metadata only says `>=3.8`, but the docs list what's actually
   tested. uv would otherwise pick the newest Python.
   → **Pin Python 3.12 in `.python-version`.**
4. **pyaxidraw's download URL is unversioned**
   (`https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip`). `uv.lock`
   records a hash of that file. When Evil Mad Scientist publishes a new
   version, every new install fails with a hash mismatch. (As of 2026-09-18 the
   URL serves version 3.9.6, dated 2023-12-12, licensed **GPL-2.0 or later**
   per `pyaxidraw/LICENSE.txt` and the file headers.)
   → **Host a fixed copy** (see G-12 and question Q6).
5. **The plotter connects only once, at startup.**
   - `from pyaxidraw import axidraw` sits *outside* the `try` in
     `_plotter_thread`, so a missing pyaxidraw kills the plotter thread.
   - An unplugged cable mid-plot raises inside the loop and kills the thread
     silently. Nothing is surfaced to the UI.

   → **Phase 1** covers both.
6. **Every point is printed to the console.** On Windows, console output is
   slow enough that this can add lag during fast drawing.
   → **Per-point logging goes behind `--verbose`**, and a log file is written.
7. **Quitting properly only happens on Ctrl+C.** The shutdown block lifts the
   pen and disengages the motors, but only when `KeyboardInterrupt` is raised.
   Closing the console window (Windows) or the Terminal (Mac) skips it.
   → **Phase 1** adds console-close and signal handlers, plus a Quit button.
8. **Startup code isn't callable.** It lives directly under
   `if __name__ == "__main__":`, and the OSC server's `serve_forever()` blocks
   the main thread. Both have to change for the app, and for 1B.
9. **`iDraw_to_svg/` is a strict subset of the main version.** Every function
   it has also exists in the main files. Deleting it loses nothing.
10. **Two renderers already draw the SVG visuals differently.** The browser's
    `layerSvgParts` draws raw strokes grey in preview-pixel coordinates;
    `svg_transform.build_svg` draws white in canvas units. Replay reads only
    the metadata, so plotting is unaffected, but the files look different.
    → **Phase 3 makes Python the only SVG writer.**
11. **`__pycache__/` files are committed, and there is no `.gitignore`.** uv
    will add a `.venv/`.
    → **Phase 0.**
12. **OSC messages can be handled out of order.** The OSC listener is a
    `ThreadingOSCUDPServer` (`listen_to_idraw.py`, at the entry point), which
    starts a new thread for every UDP packet. iDraw sends `/x`, `/pressure` and
    `/y` as separate packets, so their handlers can race: `/y` could be handled
    before the matching `/x` and plot a point with a stale x.

    There's no need for threads here:
    - the handlers only store a value, or push a command onto the queue and
      broadcast it;
    - the slow work (plotter motion) already runs on its own thread;
    - the broadcast doesn't block.

    The one slow part inside a handler, printing every point to the console,
    goes away with §2.6.

    **Decision:** switch to `BlockingOSCUDPServer`, which is single-threaded
    and keeps messages in order, as its own commit in Phase 1 with before and
    after testing. Whether this contributed to past glitches is unproven.

---

## 3. Architecture

```
iPad (iDraw OSC) ──UDP :8800──► listen_to_idraw.py  (engine, background threads)
                                   │  points, layers, lag, status
                                   ▼
                                recording.py  (records the session in Python, autosaves)
                                   │
                                preview.py    (one port: static UI + /ws, localhost only)
                                   │  HTTP GET + WebSocket
                                   ▼
                    Browser tab (1A)  /  pywebview window (1B, later)
                                   │
                    PantographApp/ui/  (index.html, app.js, style.css)
```

- **The engine is the source of truth.** Settings, the current drawing, the
  plotter status and the library all live in Python. The page is a view: on
  connect it receives a `hello` snapshot of everything, then live updates. A
  reload, a second tab or a crashed tab loses nothing.
- **One port** (default `5810`, an arbitrary uncommon number, chosen to avoid
  5000, which AirPlay uses on Macs). If it's busy, the app tries 5811–5830.
  The page connects to `ws://<its own host>/ws`, so it never needs to be told
  the port.
- **Only GET requests are served over HTTP.** That covers the UI files, saved
  drawings, thumbnails and `/health`. Everything that changes state goes over
  the WebSocket. This is also what the `websockets` library supports, since
  its HTTP handling is GET-only. The fallback is to switch to `aiohttp` if
  this design becomes limiting.
- **The main thread is kept free.** The engine runs entirely on background
  threads. In 1A the main thread opens the browser, then waits on a stop
  event. In 1B the window takes over that thread (§9).

### Repo layout after the build

```
(repo root)
  listen_to_idraw.py   engine + entry point. `python listen_to_idraw.py` = the app
  preview.py           server: one port, static files + WebSocket, security checks
  recording.py   NEW   the ONE Python implementation of draw2axi-recording:
                       live recorder, load, save/build SVG, thumbnails
  postprocess.py       unchanged
  svg_transform.py     functions unchanged; load_svg/build_svg re-exported from
                       recording.py; tkinter GUI removed; the command now
                       opens the app's Tools tab
  dot_healer.py        unchanged apart from importing from recording.py
  pyproject.toml NEW   dependencies (shared by plain-Python runs and the app)
  uv.lock        NEW   exact pinned versions + hashes
  .python-version NEW  3.12
  .gitignore     NEW   __pycache__/, .venv/, *.pyc, local runtime dirs
  .gitattributes       + eol rules for launchers (see G-8)
  tests/         NEW   pytest suite + small fixture SVGs
  .github/workflows/ NEW  CI on Windows + macOS; release zip
  PantographApp/
    BUILD_GUIDE.md     this file
    Pantograph.bat     Windows launcher
    Pantograph.command Mac launcher
    __init__.py        makes the folder importable as `PantographApp`
    shell.py           entry glue: data dirs, single-instance, open_ui(),
                       signal/console-close handlers, log file
    settings.py        settings store (JSON on disk, defaults, schema version)
    netinfo.py         local IP list, Windows network-profile check
    ui/                index.html, app.js, style.css, icons
```

`pyproject.toml` goes at the root, not in `PantographApp/`, because it
describes the dependencies for plain-Python runs as well as the app, and uv and
editors look for it there.

---

## 4. Phases

Rough effort is in working days. The total is at the end of the section.

### Phase 0: Groundwork and safety net (1 day)

- [ ] Add `.gitignore` and remove the committed `__pycache__/` from git
      (`git rm -r --cached`).
- [ ] Update `.gitattributes`: `*.bat text eol=crlf`,
      `*.command text eol=lf`, `*.sh text eol=lf` (see G-8).
- [ ] Create `pyproject.toml`:
  - dependencies: `python-osc`, `websockets` (≥13, for the new asyncio server
    and `process_request`), `numpy`, `rdp`, `platformdirs`, and pyaxidraw from
    a pinned URL (see G-12);
  - a dev group with `pytest`;
  - no build backend, since this is an application, not a library.
- [ ] Add `.python-version` = `3.12` and run `uv lock`.
- [ ] Write the tests **against the current code**, before anything changes:
  - Load every SVG in `saved_drawings/` and check that the recording
    round-trips through `svg_transform.load_svg` / `build_svg` unchanged.
  - Snapshot tests for `svg_transform.transform` and
    `dot_healer.heal_recording` on fixed inputs.
  - A headless end-to-end test:
    1. start the engine in dry-run mode on free ports;
    2. send scripted OSC bursts (`/canvasWidth`, `/pen 1.0`, then
       `/x /pressure /y` with realistic timing) using
       `pythonosc.udp_client`;
    3. collect the broadcast `point` and `pen_up` messages and check that the
       stroke count and points match the script.

    This test becomes the automated Mac check in Phase 9.
  - Fixtures: the SVGs in `saved_drawings/` are about 6 MB each. Copy one or
    two small ones into `tests/fixtures/`, or generate small synthetic ones.
    Don't make the tests depend on the user's own drawings folder.
- **Checkpoint:** `uv run pytest` passes on the *unchanged* pipeline, and
  `python listen_to_idraw.py` behaves exactly as before.

### Phase 1: Restructure the engine (1–1.5 days)

All in `listen_to_idraw.py`. The drawing logic is untouched.

- [ ] Move the `__main__` block into `main(argv=None)`, and the startup code
      into `start(options) -> Engine handle`. `__main__` becomes
      `sys.exit(main())`.
- [ ] Run the OSC server on a background thread. Keep the server object so
      that `shutdown()` can stop it.
- [x] **Single-threaded OSC (§2.12): done early, in Phase −1.** Swap
      `ThreadingOSCUDPServer` for `BlockingOSCUDPServer`.
  - Before and after the swap, run the Phase 0 end-to-end test, then do a
    fast-scribble session on the real iPad and compare the results.
  - Make sure no handler does slow work, especially console printing.
  - Set a larger socket receive buffer (`SO_RCVBUF`, e.g. 1 MB) so a brief
    stall can't drop packets.
- [ ] `restart_osc_listener(port=None)` closes and re-opens the UDP socket.
      It's used by the iPad status button ("Restart listener") and when the
      OSC port setting changes.
- [ ] Track `last_osc_time` so the UI can tell three iPad states apart:
      *never heard*, *receiving*, and *quiet for a while*.
- [ ] Add a `stop_event`. `main()` starts everything, calls
      `shell.open_ui(url)`, then waits on `stop_event`. The main thread stays
      free for 1B.
- [ ] Add a `shutdown()` that runs exactly once, from any trigger (Quit
      button, Ctrl+C, console close, SIGTERM/SIGHUP, atexit). It:
  - stops accepting OSC input;
  - lifts the pen;
  - disengages the motors (the existing `res_home2` sequence);
  - flushes the autosave;
  - closes the server.

  The existing `finally:` block becomes part of this function.
- [ ] Make the plotter robust:
  - Move `from pyaxidraw import axidraw` inside the `try`. If it fails, the
    status becomes `plotter: unavailable (pyaxidraw missing)` and the app keeps
    running as a preview.
  - Check what `ad.connect()` returns. In pyaxidraw's interactive API it
    returns `False` on failure rather than raising. **Verify this.** The
    current code only handles exceptions.
  - Wrap each command in the plotter loop in a `try`. On a serial error, mark
    the plotter disconnected, broadcast the status, and pause the queue. Don't
    drop it silently, and let the UI offer "Reconnect" or "Discard queue".
  - Add a `connect_plotter()` that can be called again later (from a UI
    button, plus a slow automatic retry every 3 s while not connected, only
    when the plot queue is empty).
- [ ] Logging: route output through `logging`.
  - Per-point and per-move lines go to DEBUG, shown only with `--verbose`.
  - INFO covers connections, strokes, errors and saves.
  - Also write a rotating log file into the log directory (see G-15). This is
    what a user sends when they report a bug.
- [ ] Command-line flags: keep `--dry-run` (now meaning "never touch the
      plotter") and `--raw-osc`. Add `--no-browser`, `--port N`,
      `--verbose` and `--data-dir PATH` (useful for tests and development).
- **Checkpoint:**
  - All the Phase 0 tests pass.
  - With the AxiDraw plugged in, plotting behaves exactly as before.
  - Unplugging the cable mid-plot shows an error instead of freezing.
  - Closing the console window lifts the pen.

### Phase 2: Server on one port, with security and single-instance (1–1.5 days)

All in `preview.py` and `PantographApp/shell.py`.

- [ ] Replace `HTTPServer` + `websockets.serve` with **one**
      `websockets.asyncio.server.serve(...)` bound to **`127.0.0.1`**, not
      `localhost` (see G-19).
  - `process_request` serves GET requests: `/` and `/ui/*` from
    `PantographApp/ui/`, `/drawings/<name>`, `/thumbs/<name>` and `/health`.
    It sets correct `Content-Type` headers and uses `Cache-Control: no-store`
    for the UI, so users never run a stale page after an update.
  - Path traversal guard: resolve every requested path and reject anything
    outside the allowed folders.
  - The WebSocket lives at `/ws`, keeping the 64 MB `max_size` so replays
    still fit.
- [ ] **Security:**
  - Reject WebSocket handshakes whose `Origin` isn't
    `http://127.0.0.1:<port>` or `http://localhost:<port>`.
  - Handshakes with **no** `Origin` header are allowed. Browsers always send
    one, so a missing header means another local program, which already runs
    as the user and could do anything anyway. This is how the second launch
    and the `svg_transform.py` command talk to a running copy.
  - Reject HTTP requests whose `Host` header isn't one of those (this blocks
    DNS-rebinding attacks).
  - In 1B the webview loads the same URL, so the checks still pass.
- [ ] Port choice: try 5810, then 5811–5830. If all are taken, show a clear
      error. OSC port 8800 does **not** fall back automatically, because the
      iPad is configured for it (see G-5).
- [ ] **Single instance.** On startup, read `instance.json` from the runtime
      directory (see G-15), which holds the port and PID.
  - If `GET /health` answers there, another copy is already running: open the
    browser to it and exit. This also covers users double-clicking the
    launcher twice.
  - If the second launch came with a file to open (for example
    `python svg_transform.py drawing.svg`), it first sends
    `open_file {path, tab}` to the running copy over `/ws`, then exits.
  - Otherwise, write the file and continue.
- [ ] Replace the `POST /save` endpoint with a `save_png` WebSocket message.
      SVG export moves to Python in Phase 3; PNG still renders from the
      browser canvas.
- **Checkpoint:**
  - A second launch focuses the first copy instead of failing.
  - Test pages confirm that a WebSocket from another origin is refused.
  - Occupying port 5810 makes the app use 5811.

### Phase 3: Recording, settings and data folders move into Python (2 days)

This is the most important structural change. Read §2 items 1 and 10 first.

- [ ] Create `recording.py`, the single Python implementation of the
      `draw2axi-recording` v1 format:
  - **`Recorder`**, which subscribes to the same `point` and `pen_up`
    messages the browser receives, applying the same rules as the current JS
    `handleMessage`:
    - A new stroke starts on the first point after a `pen_up`.
    - Stroke metadata is latched once per stroke.
    - Strokes tagged `replay` are excluded.
    - Strokes whose plot was discarded for spurious pressure *are* still
      recorded, because the browser records them today.

    Recording from the broadcast stream guarantees that the new recordings
    match the old ones. Canvas size is taken from engine state, not from a
    late-joining browser, which fixes the stale-canvas-size bug (§2.1).
  - It also records the **optimized** and **effect** layers (from the `layer`
    messages), so the SVG export can include them as it does today.
  - `load_svg`, `build_svg(rec, layers=..., colors=...)` and
    `thumbnail_svg(rec)` (a light, metadata-free, decimated SVG for the
    Library grid) move here from `svg_transform.py`, which re-exports them so
    old imports keep working.
  - **The format stays byte-compatible:** `format: "draw2axi-recording"`,
    `version: 1`, the same stroke fields, and points as
    `[t, x, y, pressureRaw]` with unrounded floats. Old SVGs must load, and
    new SVGs must load in old copies of the code.
  - **One visual style for every exported SVG** (decided; see Q3): the live
    export, the tools output and the CLI tools all use it.
    - White background.
    - Raw strokes in **greyscale**: each stroke's recorded
      `color {r,g,b,a}` becomes its luminance grey
      (`0.2126 R + 0.7152 G + 0.0722 B`), keeping alpha. That matches iDraw
      OSC's look.
    - The optional optimized and effect layers keep one accent colour each
      (from the UI palette, readable on white).
    - Only the visuals change. The recording in `<metadata>` still stores the
      original colour.
- [ ] **Autosave:** after every pen-up (debounced to at most one write every
      2 s), write the current session to `autosave/current.svg` under the
      drawings folder.
  - Use an atomic write: write a temporary file, then `os.replace`.
  - On startup, if an autosave exists from a session that didn't end cleanly,
    offer "Restore last drawing?".
- [ ] "Clear" becomes **"New drawing"**. If the session has strokes, it saves
      them automatically before starting fresh. With the Library there's no
      need for an "are you sure?" dialog.
- [ ] `PantographApp/settings.py`:
  - a JSON file with a `schema_version`;
  - defaults defined in Python;
  - unknown keys ignored and missing keys defaulted;
  - atomic writes;
  - debounced saves while sliders are dragged.

  It covers everything that's in `localStorage` today (the `axi_*` keys and
  the `axi_fx_*` effect keys), plus the OSC port and the paper settings. The
  server applies the settings on startup, **before** any browser connects, so
  the plotter is configured correctly even with no tab open. Browser storage
  is kept only for UI-only preferences, such as which tab was open.
- [ ] **Paper size and AxiDraw model are saved settings** (decided; see Q4),
      replacing the hard-coded `PAPER_WIDTH_IN` / `PAPER_HEIGHT_IN`.
  - Presets: Letter, A4, A3, Tabloid, plus Custom (width × height in
    inches or mm).
  - Model presets set the machine's travel limits. Paper larger than the
    machine can reach is clamped, with a visible warning.
  - Changing either setting recomputes the mapping and rebuilds the effect
    context (`Ctx(x_max, y_max)`) and the tilt centre. That's all state
    derived from the paper constants today, so every place that reads those
    constants needs finding and converting to the setting.
  - Changes are refused while the plot queue isn't empty ("Finish or discard
    the current plot first").
  - Pass the model to pyaxidraw (`ad.options.model`) so its own travel limits
    match.
- [ ] Data folders, via `platformdirs` (see G-15). Drawings go to
      `Documents/Pantograph/` by default, using the real Documents folder even
      when OneDrive redirects it. This is a setting, and `--data-dir` overrides
      it. The repo's `saved_drawings/` isn't touched and stays in git.
- [ ] The `hello` message on connect contains:
  - the settings and effect specs;
  - the current drawing (all three layers);
  - the plotter status and lag;
  - the IP list and OSC port;
  - the app version.

  The page renders entirely from this, so a reload restores everything.
- **Checkpoint:**
  - Reloading the tab mid-drawing loses nothing.
  - Two tabs show the same state.
  - Killing the process mid-drawing and relaunching offers a restore.
  - The exported SVG replays identically to one exported by the old browser
    code (compare recordings in a test).
  - Every SVG in `saved_drawings/` still loads.

### Phase 4: Plotter becomes optional, and `iDraw_to_svg/` goes away (0.5 day)

- [ ] Plotter status model: `connected | not_found | unavailable | dry_run |
      error(message)`, broadcast on every change and shown in the top bar.
- [ ] With no AxiDraw, everything else still works: preview, recording,
      Library, Tools and export. "Plot" buttons are disabled with a tooltip
      saying why.
- [ ] Delete `iDraw_to_svg/`. Update the README (the setup "No AxiDraw" notes
      now say "just run the app") and the project memory note about keeping
      the two versions in sync.
- **Checkpoint:** a machine without pyaxidraw installed, or with no AxiDraw
  plugged in, runs the whole app with only the plot actions greyed out.

### Phase 5: The UI (3–4 days)

First move the UI, then redesign it, as two separate commits so regressions
are easy to spot.

- [ ] **5a, move (half a day):** move the HTML, CSS and JS out of the
      `preview.py` string into `PantographApp/ui/`. Replace the placeholder
      substitution (`WS_PORT_PLACEHOLDER`, `EFFECT_SPECS_PLACEHOLDER`) with the
      `hello` message. Change nothing visible. The Phase 0–3 checks must still
      pass.
- [ ] **5b, redesign:**
  - **Top bar:**
    - app name;
    - **iPad** and **Plotter** status buttons (detailed below);
    - the IP:port for iDraw, in large, copyable text;
    - lag readout;
    - **Quit**.
  - **Status buttons.** Each is a clickable pill with a coloured dot and a
    short label. A click opens a small panel with details and actions.
    - **iPad:**
      - *Waiting for iPad* (grey; nothing received since launch): the panel
        shows the setup steps with this machine's IPs and port filled in.
      - *Receiving* (green; a packet arrived in the last few seconds).
      - *Connected, idle* (green outline; received before, quiet now; this is
        normal between strokes).
      - *Problem* (amber): port busy, or Windows network set to Public.

      Actions: Restart listener, Show setup steps, Fix firewall (Windows;
      Phase 7), Change OSC port.

      **Note:** iDraw sends to us over UDP, and we can't "call" the iPad, so
      there's nothing to dial on our side. "Reconnect" means re-opening our
      listener and walking the user through the iPad side.
    - **Plotter:**
      - *Connected* (green);
      - *Not found* (grey; with Connect);
      - *Disconnected mid-plot* (red; with Reconnect and Discard queue);
      - *No pyaxidraw* (grey; explains why);
      - *Dry run* (blue).

      Actions: Connect/Reconnect, Home, Disengage motors.
  - **"Troubleshoot" in both panels (required).** It opens a step-by-step
    walkthrough: one check per screen, with "That worked" / "Still not
    working" buttons, never one wall of text.
    - Wherever the app can test a step itself, it does, and shows the result
      live. For example, the iPad status turns green the moment a packet
      arrives, and the plotter check finishes the moment Connect succeeds.
    - The walkthrough ends with "Copy diagnostics", which copies the version,
      OS, IPs, port, statuses and the last 200 log lines, so the user can send
      them in a bug report.
    - **iPad walkthrough:**
      1. Is iDraw OSC open, with the IP and port filled in exactly as shown?
         The values are displayed, with a copy button.
      2. Are both devices on the same Wi-Fi? Compare the network names.
      3. Windows: is the network set to Public? The app checks this itself
         and links the fix.
      4. Firewall: the Fix firewall button (Windows) or the steps for Mac.
      5. Is something else using the port? The app checks.
      6. Does the network block devices from seeing each other, as many
         campus, hotel and guest networks do? If so, offer the phone-hotspot
         test and the Tailscale guide from the README.
      7. Restart the listener and redraw a stroke.
    - **Plotter walkthrough:**
      1. Is the USB cable plugged in, and is the AxiDraw powered? Its power
         supply is separate from USB, and the motors need it.
      2. Try a different USB port or cable (some cables only carry power).
      3. Is another program, such as Inkscape's AxiDraw extension or a second
         Pantograph, holding the connection?
      4. Is pyaxidraw installed? The app checks.
      5. Connect again, and show the exact error if it fails.
      6. Try "Pen test up/down" to confirm the servo moves.
  - **Center:** the live canvas, drawn as **white paper with greyscale
    strokes** (the same look as the SVG export and iDraw OSC). The optimized
    and effect layers are overlays in two accent colours, with a legend and
    a toggle for each layer.
  - **Sidebar tabs:**
    - **Plot:** paper size and AxiDraw model, pen positions with test
      buttons, variable pressure, tilt, flip, optimizer, Home,
      "effects only".
    - **Effects:** built from `postprocess.effect_specs()` as today.
    - **Tools:** flip H/V, minimum-width filter (live preview on the canvas),
      heal dots (with a report such as "healed 4 torn runs, 37 dots"), save as
      a new file.
    - **Library:**
      - a grid of saved drawings using server-made thumbnails;
      - Open, Plot, Duplicate, Reveal in folder, Delete (moves to a
        `.trash/` folder rather than deleting permanently);
      - "Import SVG…" via a file picker, which copies the file into the
        library.
  - **Connection card:** shown over the canvas until the first OSC message
    arrives.
    - It shows the step-by-step iDraw setup with *this* machine's IPs and
      port filled in.
    - After 20 s with nothing received, it expands with firewall and network
      troubleshooting (see G-3 and G-4).
    - A "Skip, I'll open a saved drawing" link dismisses it.
  - **States to design, not just the happy path:** an empty library, no
    plotter, a plotter error, a replay in progress (progress bar and Stop), a
    lost connection to Python ("App stopped. Relaunch Pantograph."), a
    restore offer, and a busy OSC port.
  - **Visual system:**
    - CSS custom properties for colours, spacing and type;
    - a single system font stack;
    - a light, paper-like theme that matches the white canvas and exports
      (replacing today's dark UI);
    - consistent controls (one slider style, one button hierarchy);
    - it must work at 1280×720 as well as on a large screen.
- [ ] Rules that keep 1B easy (§9):
  - no `<a download>` or blob downloads (the server writes files);
  - no `window.open` pop-ups;
  - no `alert()`/`confirm()` (use in-page dialogs);
  - everything the page needs comes from `hello`.
- [ ] Test in Chrome, Edge **and Safari**. Safari uses the same engine as the
      1B Mac window.
- **Checkpoint:** Marc reviews the UI on real hardware before Phase 6
  finishes. That's a design sign-off, not just a correctness check.

### Phase 6: Tools and Library backend (1 day)

- [ ] WebSocket API, one handler each:
  - `library_list`, `library_open`, `library_import`, `library_delete`
    (moves the file to trash), `library_reveal` (opens the OS file manager:
    `explorer /select,` on Windows, `open -R` on Mac);
  - `tool_preview` (returns the transformed strokes without saving);
  - `tool_apply` (saves as a new file, never overwriting; names get suffixes
    such as `_flipped`, `_minWidth79`, `_healed`, matching today's CLI);
  - `plot_drawing` (the existing replay path).
- [ ] Tools run on the *open* drawing. That's either the live session or one
      opened from the Library.
- [ ] **`python svg_transform.py [file.svg]`** (decided; see Q5) keeps
      working, but now opens the app's Tools tab with the file loaded, instead
      of its tkinter window.
  - If the app is already running, the file is handed over (Phase 2).
    Otherwise the command starts the app with
    `--open <file> --tab tools`.
  - `--selftest` stays headless as before.
  - Remove the tkinter GUI code. The transform functions stay, since the
    app uses them.
  - `python dot_healer.py` stays a command-line tool, unchanged. Healing is
    also in the Tools tab.
- [ ] Large drawings: run heavy transforms and heals on a worker thread, and
      show a busy state, so the WebSocket loop never blocks.
- [ ] Thumbnails: generate them on demand into `thumbs/` under the drawings
      folder, rebuilding when the source file's modification time changes.
      Drawings are around 6 MB, so the grid must not load full SVGs.
- **Checkpoint:** every action that `svg_transform.py` and `dot_healer.py`
  offer works from the UI, and the output files are identical to what the CLI
  produces.

### Phase 7: Network help (0.5–1 day)

All in `PantographApp/netinfo.py`.

- [ ] **Local IPs:**
  - Find the primary one with the UDP "connect" trick (connect a UDP socket to
    a public address; no packet is actually sent).
  - Add all other IPv4 addresses from `socket.getaddrinfo(hostname)`.
  - Label `100.64.0.0/10` addresses as Tailscale, since the README supports
    it.
  - Hide link-local and virtual adapters (`169.254.*`, and the usual
    Hyper-V/WSL/VirtualBox ranges) unless nothing else exists.
  - Without internet the connect trick fails; fall back to the full list.
- [ ] **Windows network profile:** run `powershell -NoProfile -Command
      "Get-NetConnectionProfile | ConvertTo-Json"` once at startup, with a
      timeout.
  - If the active network is **Public**, show a warning card: "Windows is
    treating this Wi-Fi as Public, which blocks the iPad. Set it to Private:
    Settings → Network → Wi-Fi → Private, or allow Python on public networks
    in the firewall prompt." See G-4.
  - If the command fails, skip the check silently.
- [ ] **"Fix firewall" button (Windows)**, in the iPad status panel. It
      exists for the user who clicked Cancel on the first firewall prompt,
      which otherwise means digging through Windows Defender Firewall
      settings.
  - It runs one elevated command, and Windows shows its standard "Allow this
    app to make changes?" (UAC) prompt:
    `Start-Process powershell -Verb RunAs -ArgumentList '... New-NetFirewallRule -DisplayName "Pantograph (iPad input)" -Direction Inbound -Protocol UDP -LocalPort <osc_port> -Program "<python.exe path>" -Action Allow -Profile Private,Public'`
  - Show exactly what it will do before running it.
  - Report success or failure afterwards.
  - Remove any older "Pantograph" rule first, so rules don't pile up.
  - On a Mac, show the equivalent steps instead: System Settings → Network →
    Firewall → Options → allow Python. The macOS firewall is **off by
    default**, so most Mac users never see this.
- [ ] **OSC port busy:** if binding port 8800 fails, show "Port 8800 is in
      use by another program" with the option to change the OSC port. The
      card then reminds the user to update the port in iDraw as well.
- **Checkpoint:**
  - On a Public Windows network the warning appears.
  - Occupying port 8800 with a dummy socket shows the error card.
  - The IP shown matches `ipconfig`.

### Phase 8: Launchers (1–1.5 days)

Both launchers follow the same steps:
1. Find the project root: the parent of the launcher's own folder. Never rely
   on the working directory. A double-clicked `.command` starts in the home
   folder.
2. Refuse to run from inside a zip (see G-7).
3. Make sure uv is present, in a **private** location:
   - Windows: `%LOCALAPPDATA%\Pantograph\runtime\uv\`
   - Mac: `~/Library/Application Support/Pantograph/runtime/uv/`

   If it's missing, download the **pinned uv release binary** straight from
   GitHub releases with `curl` and unpack it with `tar`. Both tools are built
   into Windows 10 1803+ and macOS. Pick the build for the machine's
   architecture (x86_64 or aarch64) and check it against the release's
   `.sha256`.
   **Don't use `irm | iex` or `curl | sh`.** Those installers change the PATH,
   run a remote script, and are what antivirus heuristics and IT policies
   block. A pinned binary is reproducible and leaves the system untouched.
4. Set the environment so everything stays in the private runtime folder and
   out of OneDrive or iCloud-synced folders:
   - `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR` and `UV_PROJECT_ENVIRONMENT`
     point inside `runtime/`;
   - `UV_PYTHON_PREFERENCE=only-managed`, so a system Python is never
     accidentally used.
5. On the first run, print friendly progress lines. For example: "First
   launch: downloading Python and libraries (about 150 MB, 1–2 minutes). This
   only happens once." Then run
   `uv run --locked --project "<root>" python "<root>/listen_to_idraw.py" %*`.
6. On failure, print a plain-language message plus the log path, and keep the
   window open (`pause` on Windows; "Press Enter to close" on Mac).

**Windows-specific (`Pantograph.bat`):**
- CRLF line endings. The `.gitattributes` rule is essential here, because cmd
  misparses `goto` and labels in LF-only files.
- Quote every path. Test with a username containing a space and one with
  non-ASCII characters.
- Detect the architecture from `%PROCESSOR_ARCHITECTURE%` (`AMD64` / `ARM64`).
- Windows on ARM: numpy and pyaxidraw support is unverified (Q8). The fallback
  is to ask uv for x86_64 Python, which runs under emulation.
- Ctrl+C prints "Terminate batch job (Y/N)?" after Python exits. That's
  harmless and documented.

**Mac one-line installer (`PantographApp/install-mac.sh`)**, chosen as the
recommended main Mac route, pending Marc's OK (see §12.2):
- It's fetched with `curl -fsSL <release URL>/install-mac.sh | bash`.
- It downloads the release zip with `curl` (so there's no quarantine flag),
  extracts it to `~/Pantograph`, and checks that the launcher is executable.
- It offers a Desktop shortcut to `Pantograph.command`, then launches it.
- Re-running it updates in place: it replaces the app folder, while the data
  in the user folders stays untouched.
- It checks the macOS version first (G-33) and never uses `sudo`.
- It has to stay short and readable, since people are piping it into `bash`.

**Mac-specific (`Pantograph.command`):**
- LF line endings, a `#!/bin/bash` first line, and the executable bit
  committed with `git update-index --chmod=+x`. Check that the GitHub zip
  preserves it (G-9).
- `cd "$(dirname "$0")/.."`
- Handle SIGHUP (sent when the Terminal window closes) by running
  `shutdown()`, which Phase 1 wires up.

- **Checkpoint:** on a *fresh* Windows user account and a fresh Mac (no
  Python, no uv), downloading the release zip and double-clicking works end
  to end. That includes the "Open Anyway" step on the Mac, a second launch
  that's fast and works offline, and an update (new version extracted to a
  new folder) that keeps settings and drawings.

### Phase 9: CI and release (0.5–1 day)

- [ ] GitHub Actions on `windows-latest` and `macos-latest` (Apple Silicon):
  1. `astral-sh/setup-uv`
  2. `uv sync --locked`
  3. `uv run pytest`
  4. **Launcher smoke test:** run the real launcher with `--smoke-test`,
     which starts the app, checks that `/health` and a WebSocket `hello`
     work, sends a scripted OSC stroke, saves, checks the SVG, then quits.
     This tests the real first-run path on both operating systems without
     needing a Mac.
- [ ] Release workflow: on a version tag, build
      `Pantograph-<version>.zip` with `git archive`.
  - Mark dev-only files `export-ignore` in `.gitattributes` (`tests/`,
    `.github/`, `MEETINGS.html`, `AGENTS.md`, `.claude/`).
  - Attach the zip and the pinned pyaxidraw zip (G-12) to a GitHub Release.
  - The README links to "latest release", not "Code → Download ZIP", so users
    get the tested build.
- [ ] Version number: in `pyproject.toml`, shown in the UI footer and in
      `hello`. Optionally, a once-a-day "update available" check against the
      GitHub Releases API, failing silently when offline.

### Phase 10: Docs and real-hardware test pass (1–2 days)

- [ ] README Troubleshooting must explain **networks that isolate devices**
      (school, work, hotel and guest Wi-Fi), with the hotspot test and
      Tailscale as the fixes. See §12.4.
- [ ] Rewrite the README setup section as: download → extract → double-click
      → enter the IP/port shown in the app into iDraw. Add screenshots for
      the Windows "Run anyway" prompt, the Mac "Open Anyway" steps
      (System Settings → Privacy & Security) and the firewall prompts.
- [ ] Keep a "Developers" section: `uv run listen_to_idraw.py`, plus the
      plain `pip install ...` route. The old commands still work.
- [ ] Run the test matrix in §8 on real hardware.
- [ ] Add a meeting or release note to `MEETINGS.html` if relevant.

**Total: about 13–17 working days (roughly 3 weeks).** That's more than the
earlier estimate of 1.5–2 weeks. The difference is the work that reading the
code turned up: recording moving to Python (§2.1), security (§2.2), the
single instance, and the network help. On top of that come the decisions from
2026-09-18: the paper/model settings, the status buttons, the Fix firewall
button, single-threaded OSC, and the `svg_transform` hand-off, about one more
day in total. Of those, recording and security are must-haves. The network
help (Phase 7) and the update check could be dropped to save about a day.

---

## 5. Gotcha register

Every known risk, with its planned mitigation and the phase that handles it.
Re-check this list before release.

| ID | Gotcha | Mitigation | Phase |
|---|---|---|---|
| G-1 | Closing or reloading the tab loses the drawing (it's recorded in the browser) | Record in Python; autosave; `hello` restores the page | 3 |
| G-2 | Any website can drive the plotter via the WebSocket | Origin + Host checks; bind to 127.0.0.1 | 2 |
| G-3 | The firewall prompt says "Python", not "Pantograph"; users click Cancel and OSC fails silently | Connection card explains after 20 s; **"Fix firewall" button** (one UAC click adds the rule); README screenshots; the rule applies to uv's `python.exe` path, so pin Python's exact version so the path (and the rule) stays stable | 7, 10 |
| G-4 | Windows treats new Wi-Fi networks as **Public**, and the firewall blocks inbound traffic there even after "Allow" (Private is ticked by default) | Detect the Public profile and show the fix; explain in the README | 7 |
| G-5 | OSC port 8800 is busy (another program, or a second copy of the app) | Single-instance check first; then a clear error plus a setting to change the port (and a reminder to change iDraw too) | 2, 7 |
| G-6 | Port 5000 is taken by AirPlay Receiver on Macs | The default port is 5810, with fallback to 5811–5830 | 2 |
| G-7 | A Windows user double-clicks the `.bat` inside the zip viewer, so it runs from a temp folder | Launcher detects `\AppData\Local\Temp\` or a `.zip` in its path and says "Extract the zip first (right-click → Extract All)", then pauses | 8 |
| G-8 | `.bat` with LF endings misbehaves; `.command` with CRLF fails | `.gitattributes` eol rules; CI runs the real launchers | 0, 9 |
| G-9 | `.command` loses its executable bit, or macOS Gatekeeper blocks it ("Open Anyway" in System Settings since Sequoia; right-click → Open no longer bypasses it) | **Recommended main Mac route (pending OK): the one-line `curl` installer**, which leaves no quarantine flag, so no prompt appears. The zip route keeps: commit with `+x`, a check that the release zip keeps the mode, README screenshots, and a `chmod +x` fallback | 8, 10 |
| G-10 | Windows shows "Publisher could not be verified" for the downloaded `.bat` | Documented as expected; screenshot | 10 |
| G-11 | pyaxidraw supports Python ≤ 3.12 only | `.python-version` = 3.12; `requires-python = ">=3.12,<3.13"` | 0 |
| G-12 | pyaxidraw's URL is unversioned, so the lock hash breaks when Evil Mad Scientist updates it | Host a fixed copy as a GitHub Release asset and point `pyproject.toml` at that URL. Upgrade deliberately: download the new one, test, re-lock. Include its licence (see Q6). | 0, 9 |
| G-13 | First launch needs internet, takes 1–2 minutes and downloads about 150 MB | Clear progress messages; README says so up front; later launches work offline because uv reuses its cache and venv | 8 |
| G-14 | School or work machines block downloads or scripts | Pinned binary download (no `iex`/`sh`) avoids the most common blocks; README "if your computer is managed" note; no full fix | 8, 10 |
| G-15 | A dependency folder inside OneDrive/iCloud gets synced; user data inside the app folder gets lost on update | Runtime in local app data; settings in the per-user config folder; drawings in Documents via `platformdirs` (which resolves OneDrive-redirected Documents correctly). Updating means replacing the app folder, and nothing is lost | 3, 8 |
| G-16 | Quitting without lifting the pen (closing the console window or Terminal) | `shutdown()` runs once from every exit path: the Quit button, Ctrl+C, the Windows console-close handler (`SetConsoleCtrlHandler` via ctypes), SIGHUP/SIGTERM, atexit | 1 |
| G-17 | Plotter unplugged or missing leaves a dead plotter thread with nothing in the UI | Import inside `try`, check `connect()`'s return value, catch per-command errors, surface the status, reconnect | 1, 4 |
| G-18 | Printing every point slows Windows | Per-point logs go to DEBUG, shown only with `--verbose`; a log file for bug reports | 1 |
| G-19 | `localhost` resolves to IPv6 `::1` first on some systems, so the browser waits for the IPv4 fallback | Bind to and open `127.0.0.1` explicitly | 2 |
| G-20 | Launched twice, the second copy fails or two copies fight over ports | `instance.json` + `/health`; the second launch focuses the first | 2 |
| G-21 | Browser shows a stale cached UI after an update | `Cache-Control: no-store` on UI files | 2 |
| G-22 | Large drawings (about 6 MB SVGs) make the Library and tools slow | Server-made thumbnails; worker thread for transforms; busy states | 6 |
| G-23 | Old SVGs, including ones made by the old browser code or `iDraw_to_svg`, must still load and replay | Format stays v1; tests load every existing drawing; Recorder copies the JS rules exactly | 0, 3 |
| G-24 | Mac Local Network privacy prompts (macOS 15+) might affect receiving iPad traffic when launched from Terminal | **Unverified.** Test on a real Mac first; document whatever prompt appears | 10 |
| G-25 | Windows on ARM / Intel Macs | **Windows on ARM: not handled (decided 2026-09-18);** revisit only if reported. The launcher still picks the uv build matching the chip, since that costs nothing. Intel Macs: all dependencies ship x86_64 builds; see G-33 for old macOS | 8 |
| G-26 | Our dependency links disappear (uv release, pyaxidraw mirror, PyPI) | Everything pinned; uv binary + pyaxidraw both hosted on GitHub releases (uv's own, and ours); PyPI is the one accepted external dependency | 8, 9 |
| G-27 | Updates are manual | Version shown; optional update-available notice | 9 |
| G-28 | OSC packets are handled on separate threads and may race (§2.12) | Switch to single-threaded `BlockingOSCUDPServer`, with a bigger receive buffer; as its own commit, tested before and after | 1 |
| G-29 | The iPad can't be "reconnected" from our side (UDP is one-way; iDraw sends to us) | The iPad status button offers Restart listener, setup steps, Fix firewall and Change port, and shows *never heard* vs *idle* vs *receiving* | 1, 5 |
| G-30 | Changing paper size or model mid-plot would scramble coordinates | Refuse while the plot queue isn't empty; recompute mapping, effect context and tilt centre on change | 3 |
| G-31 | Networks that block devices from seeing each other (common on campus, hotel and guest Wi-Fi) mean the iPad can never reach the computer; no firewall setting fixes it | Troubleshoot walkthrough detects the case ("same network, firewall OK, still nothing"), suggests a phone hotspot to confirm, and links the README's Tailscale guide | 5 |
| G-32 | pyaxidraw's `setup.py` builds its dependency list at install time (`ink_extensions`, `lxml`, `plotink`, `pyserial`, `requests`), and lxml is compiled code | Lock and test on every platform in CI; lxml is one of the libraries to watch on Windows ARM (§11 Q8) | 0, 9 |
| G-33 | Very old macOS can't run current Python/numpy builds | Launcher checks the macOS version first and prints a plain "too old" message | 8 |
| G-34 | Printing non-ASCII characters (`≈`, `→`, `×`, `—` appear in log lines) raises `UnicodeEncodeError` when output isn't a real console (piped, redirected, or some launchers on Windows use cp1252). That crash lands inside an OSC handler, mid-stroke | Phase 1 logging: UTF-8 log file, and `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at startup | 1 |
| G-35 | Input with **no pressure data** (a finger, or a Pencil iDraw isn't reading) sends pressure exactly `1.0` on every point. The spurious-pressure rule used to drop the whole stroke: the plotter travelled to its start and never lowered the pen | **Done (Phase −1, 2026-09-19).** A run of 5+ placeholder `1.0`s is plotted at 0.5 (`NO_PRESSURE_RUN`, `NO_PRESSURE_VALUE`). A whole stroke of fewer than 5 (a finger tap) gets 0.5 when it ends. Shorter runs inside real pressure are still interpolated. Why the old drop existed: MEETINGS.html meeting 3. It was a side effect of glitch interpolation, not a response to phantom strokes | −1 |

---

## 6. Messages between page and Python

This is the first draft of the protocol. Every message is JSON over `/ws`, and
every message has a `type` field.

**Python → page:**
- **Snapshot:** `hello` (see Phase 3).
- **Existing messages, unchanged:** `point`, `pen_up`, `layer`,
  `canvas_size`, `tool_change`, `lag`.
- **Status:** `status` (iPad and plotter status, errors), `settings` (after
  any change, so every tab stays in sync).
- **Library:** `library` (the list), `drawing` (an opened drawing's layers).
- **Progress and results:** `replay_progress`, `tool_result`, `saved`,
  `error`.
- **Other:** `restore_available`.

**Page → Python:**
- **Existing messages, unchanged:** the `set_*` messages, `home`,
  `pen_test_*`, `replay`, `set_effect_*`, `set_effects_only`, `set_flip_*`.
- **New:**
  - `new_drawing`, `save_svg {layers}`, `save_png {b64}`;
  - `connect_plotter`, `discard_queue`, `disengage_motors`, `quit`;
  - `restart_osc`, `fix_firewall`, `set_osc_port`, `set_paper {preset | w,h}`,
    `set_model`;
  - `open_file {path, tab}` (from a second launch or `svg_transform.py`);
  - the `library_*` and `tool_*` messages (Phase 6);
  - `restore {accept}`.

**Rules:**
- Every handler validates its input: types, ranges, and file names that must
  resolve inside the drawings folder.
- Unknown message types are ignored.
- The server replies with `error` messages instead of failing silently. The
  current `except Exception: pass` goes away.

---

## 7. Behaviour changes users will notice

These are deliberate, and they'll be listed in the release notes:
- Drawings survive a page reload, and a restore is offered after a crash.
- "Clear" becomes "New drawing", and unsaved work is saved automatically.
- Downloads go to `Documents/Pantograph/` instead of `saved_drawings/` in the
  repo. That folder stays in git as documentation.
- The canvas, the UI and exported SVGs switch from dark to **white paper with
  greyscale strokes**.
- Paper size and AxiDraw model are now settings rather than constants in the
  code.
- `python svg_transform.py` opens the app's Tools tab instead of a separate
  window.
- Settings follow the user instead of the browser. They no longer reset if
  you switch browsers or the port changes.
- `python listen_to_idraw.py` opens the new UI at `127.0.0.1:5810` instead of
  `localhost:5000`.
- The plotter is detected automatically. `--dry-run` still forces
  "no plotter".
- The console is quieter; use `--verbose` for the old per-point output.

---

## 8. Test matrix (before the first release)

| Scenario | Win 11 | Win 10 | macOS (Apple Silicon) |
|---|---|---|---|
| Fresh account, no Python or uv: download → extract → double-click | ☐ | ☐ | ☐ |
| Path with spaces / non-ASCII username | ☐ | ☐ | ☐ |
| Documents folder redirected to OneDrive | ☐ | ☐ | n/a |
| Firewall prompt: Allow; then separately Deny → the app explains | ☐ | ☐ | ☐ |
| Network set to Public → warning shown | ☐ | ☐ | n/a |
| Denied firewall → "Fix firewall" → one UAC click → iPad input arrives | ☐ | ☐ | n/a |
| Status buttons: iPad waiting / receiving / idle; plotter connect, unplug, reconnect | ☐ | ☐ | ☐ |
| Paper size / model change → mapping correct; refused mid-plot | ☐ | ☐ | ☐ |
| Fast scribble after the single-thread OSC switch: no stale-x points, no dropped packets | ☐ | ☐ | ☐ |
| iPad draws → preview → autosave → reload tab → nothing lost | ☐ | ☐ | ☐ |
| With AxiDraw: live plot, pen tests, Home, replay, effects | ☐ | ☐ | ☐ |
| Unplug the AxiDraw mid-plot → error → reconnect | ☐ | ☐ | ☐ |
| Close the console/Terminal mid-plot → pen lifts, motors off | ☐ | ☐ | ☐ |
| Launch twice → second launch opens the first | ☐ | ☐ | ☐ |
| Port 5810 busy → falls back; port 8800 busy → error card | ☐ | ☐ | ☐ |
| Second launch with no internet | ☐ | ☐ | ☐ |
| Update: extract the new version beside the old one → settings and drawings kept | ☐ | ☐ | ☐ |
| Old SVGs (including `iDraw_to_svg` ones) load, transform, heal and replay | ☐ | ☐ | ☐ |
| Tools output identical to `svg_transform.py` / `dot_healer.py` | ☐ | ☐ | ☐ |
| UI works in Chrome, Edge, Safari | ☐ | ☐ | ☐ |

---

## 9. Staying ready for 1B (native window)

1B means adding pywebview plus about 20 lines. That only stays true if these
hold:

1. **One seam.** The UI only ever opens through `PantographApp.shell.open_ui(url)`.
   In 1A it calls `webbrowser.open(url)`. In 1B it tries
   `webview.create_window(...); webview.start()`, and on *any* exception
   (import or start) falls back to the browser. A `--browser` flag forces the
   browser.
2. **The main thread stays free** (Phase 1). pywebview must own the main
   thread, which Cocoa requires on macOS.
3. **No features that only work in a browser** (Phase 5 rules). Keep
   settings server-side and writes server-side, with no pop-ups or blocking
   dialogs.
4. **Quit is a button**, and `shutdown()` is idempotent. Closing the 1B
   window simply calls it.
5. **Test in Safari.** Its engine is what the Mac window uses.

The known 1B risks and mitigations are tracked in the project memory
(`option-1b-risks`). In summary:
- the platform-native dependencies (pythonnet/.NET + WebView2 on Windows,
  pyobjc on Mac);
- quirks of the webview engines;
- the lingering Terminal window on Mac;
- "Python" shown as the app name in the Dock.

---

## 10. Out of scope (now), but kept in mind

- **Uploading to the Pantograph website.** Marc's site (marcdunand.com) is a
  static Astro site on Cloudflare Pages, deployed by pushing to `main` of
  `MarcDunand/Personal-Website`. Uploads would need:
  - Pages Functions (`functions/api/upload.ts`) plus R2 or KV storage, all
    within the free tier;
  - an anti-abuse design (a moderation queue or per-user upload codes);
  - the Python side sending the upload, so there are no CORS issues.

  What to keep in mind now:
  - Keep export as a clean `recording.build_svg()` call, so an "upload"
    action is just another consumer of it.
  - Drawings are about 6 MB each, mostly per-segment `<path>` elements plus
    JSON. A lighter "web" export might be wanted then.
- **Option 2** (PyInstaller-packaged `.exe`/`.app`, built in CI) remains a
  later add-on. The single entry point and data-directory design above carry
  over unchanged.

---

## 11. Questions for Marc

Answers are recorded here and carried into §1.

| # | Question | Answer (2026-09-18) |
|---|---|---|
| Q1 | Where do drawings go? | `Documents/Pantograph/` by default. **Keep `saved_drawings/` in git** for documentation; the app doesn't write there. |
| Q2 | Recording moves into Python? | Yes. |
| Q3 | Exported SVG look? | White background, greyscale strokes, matching iDraw OSC. Applied across the app (canvas, UI theme, every export). |
| Q4 | Paper size / AxiDraw model? | A saved setting like x/y tilt, now (Phase 3). |
| Q5 | `python svg_transform.py`? | Whatever looks most consistent, so it opens the app's Tools tab (Phase 6). |
| Q6 | Licence? | **MIT.** Done: `LICENSE` + README line. (Reasoning below.) |
| Q7 | OSC threading? | Switch to a single thread in Phase 1 (§2.12). |
| Q8 | Windows on ARM / Intel Macs? | Don't handle Windows on ARM (rare, low risk). Intel Macs just work on a recent macOS. See the notes below. |
| Q9 | Name? | **Pantograph.** |

**Q6, licence notes.** This isn't legal advice, but it's the common
understanding.
- **Without a LICENSE file**, the code is "all rights reserved". People can
  look at it on GitHub but have no legal right to copy, modify or share it.
  Adding one is just a `LICENSE` text file at the repo root (GitHub's "Add
  file → Create new file → LICENSE" offers templates), plus a line in the
  README.
- **MIT (permissive):** anyone can use, change and redistribute the code,
  even in closed or commercial projects, as long as they keep the copyright
  notice. It's short, the most common choice, and it's compatible with
  pyaxidraw's GPL.
- **GPL-3.0 (copyleft):** the same freedoms, but anyone who *distributes* a
  modified version must release their source under the GPL too. That's
  compatible here because pyaxidraw is "GPL-2.0 **or later**".
- **The artwork is separate.** The code licence doesn't have to cover
  drawings or photos. They can stay "all rights reserved", or use a Creative
  Commons licence such as CC BY-NC. Say which in the README.
- **The pyaxidraw copy we host (G-12)** keeps its own GPL licence file inside
  the zip. That's all the GPL asks for when redistributing it unmodified,
  since it's already source code.

**Q8, what actually fails.** Most of our libraries are plain Python and run on
any chip. Three contain compiled code, which needs a ready-made build for each
chip type: **numpy** (ours), **lxml** (pulled in by pyaxidraw) and
**websockets** (whose compiled part is an optional speed-up). The failure case:
someone on a **Windows laptop with an ARM chip** double-clicks the launcher,
and one of those libraries has no ARM build for Python 3.12, so the first-time
install stops with an error.
- **Likelihood:** low and shrinking. Recent numpy and lxml releases appear to
  ship Windows-ARM builds (unverified; to be confirmed in CI). If one doesn't,
  the launcher falls back to the regular Intel/AMD Python, which Windows on
  ARM runs through its built-in translation. That's plenty fast for this app,
  and the USB serial link to the AxiDraw works the same way.
- **How common the machines are:** Windows-on-ARM laptops (Snapdragon "Copilot+
  PCs", Surface Pro 11 and similar) have only been mainstream since mid-2024,
  and are a small fraction of Windows PCs in use (low single-digit percent;
  estimate, not measured).
- **Intel Macs:** all of our libraries ship Intel-Mac builds, so nothing is
  expected to fail. The one real limit is **very old macOS**: current Python
  and numpy builds need a reasonably recent macOS (roughly 10.13+ for Python,
  newer for some numpy builds). A 2012-era Mac stuck on an old macOS may fail
  to install. The launcher should print "Your macOS is too old" rather than a
  raw error.
- **Why "best effort" is cheap:** the launcher already has to detect the chip
  to download the right uv. The fallback is a few lines, and CI can run a
  Windows-ARM job if GitHub's hosted ARM runner is available to this repo.

---

## 12. Remaining risks (after every mitigation above)

These are the problems that can't be engineered away, or only partly. Keep
them in view.

1. **No Mac on hand for testing.** CI proves the Mac install works. It can't
   test the iPad → Mac → AxiDraw flow, macOS permission prompts (G-24) or the
   "Open Anyway" experience. **One real-Mac session before release is
   required.**
2. **The Mac first-launch step is more than one click.** This is accepted by
   decision; it's the same G-9 as before, restated so its size is clear. Since
   macOS 15 (Sequoia), the first double-click on an unsigned downloaded file
   shows a dialog with only "Done" and "Move to Trash". There's no Open
   button, and right-click → Open no longer bypasses it. The user has to:
   1. go to System Settings → Privacy & Security;
   2. scroll down to "Pantograph.command was blocked", and click **Open
      Anyway**;
   3. enter their Mac password;
   4. double-click again and click **Open**.

   It's a one-time step, easy with screenshots, but some users will give up.

   **Recommended way around it (no $99), pending Marc's OK: a one-line Terminal installer for Mac.**
   macOS only blocks files carrying the "downloaded from the internet"
   (quarantine) flag. Browsers set that flag; `curl` doesn't. So the README's
   Mac instructions become "paste this into Terminal":
   `curl -fsSL https://github.com/MarcDunand/Project-Pantograph/releases/latest/download/install-mac.sh | bash`.
   The script:
   - downloads and extracts the release zip into `~/Pantograph`;
   - makes a double-clickable `Pantograph.command` there and optionally puts
     it on the Desktop;
   - runs it once.

   Nothing it creates is quarantined, so there are **no security prompts,
   then or ever**. The zip plus "Open Anyway" screenshots stays as a second
   route for people who won't use Terminal. Cost: about half a day
   (`install-mac.sh` in Phase 8, released as an asset in Phase 9). Many open
   source tools, including Homebrew, install this way.

   Other ways out, if ever needed:
   - **An Apple fee waiver:** Apple waives the $99 for accredited educational
     institutions and nonprofits. That's an *organization* enrolment, so it
     would go through a school's or lab's team rather than an individual.
   - **The $99 account itself,** which would also need Option 2 packaging.
3. **Locked-down computers** (school labs, work laptops) may block the
   download or the script entirely. There's no fix on our side; the README
   says so.
4. **Networks that isolate devices** (G-31). Common on exactly the campus
   networks students use. There's no fix on our side, so it **must** be
   covered in two places:
   - the iPad troubleshoot walkthrough (Phase 5, step 6), which detects the
     pattern: same network, firewall OK, still nothing received;
   - the README's Troubleshooting section (Phase 10), in plain words: "Some
     school, work, hotel and guest Wi-Fi networks block devices from talking
     to each other. Use a phone hotspot or Tailscale."
5. **iDraw OSC is a third-party app.** If it changes its messages, gets
   abandoned or leaves the App Store, input breaks. Out of our control. Worth
   noting in AGENTS.md; a future fallback would be our own iPad web page
   sending points.
6. **Stroke splitting by timing isn't fixed at the source.** Pen-up is still
   inferred from a 0.15 s gap, so fast strokes can still tear into dots. The
   healer repairs them after the fact. Single-threaded OSC may help a little
   (unproven).

   iDraw sends flat, individual values with no stroke IDs and no start/end
   messages (AGENTS.md §3.4), so there's no nesting to read.

   **Lead from Marc's `--raw-osc` capture (2026-09-18):** a "state block"
   (`/r /g /b /a`, the nine tool flags, `/canvasWidth`, `/canvasHeight`,
   `/drawingWidth`, `/eraserWidth`) comes before the first stroke, and
   again between strokes. It looks like a stroke-start marker, but the
   capture has two things that don't fit a simple "one block = one new
   stroke" rule:
   - **Five blocks in a row with no points between them.** These could be
     taps or touches that produced no points, a block sent on *both*
     touch-down and lift, or a periodic heartbeat while idle.
   - **A block between two points that are spatially continuous**
     (`198.3,423` → block → `195,421` → `195,421` duplicate → continues left).
     This is either a quick lift and re-touch, or the block also arrives
     mid-stroke.

   The capture has no timestamps, so it can't settle this. It's also worth
   noting that **every point in it has pressure exactly `1.0`**, the value the
   pipeline treats as spurious. A stroke made entirely of those is dropped
   from the plot. If this was drawn with a finger, that's expected (finger
   input reports a constant). If it was the Pencil, something is off.

   Two avenues are still open, and neither is part of the app build:
   - **A capture experiment (about 1 hour, needs the iPad):** log every raw
     packet with a precise arrival time, drawing slow strokes, fast strokes,
     taps and lifts. It would answer four questions:
     1. Does the "state block" (canvas size, tool, colour) arrive at the
        start of every stroke? If so, it's a free stroke-start marker.
     2. Does the last point before a lift carry a telltale pressure?
     3. Do messages ever arrive as OSC bundles with iDraw's own timestamps?
        Those would remove Wi-Fi timing jitter from the gap measurement.
     4. Does any unknown address show up?
   - **Deciding pen-up later:** the plotter usually runs seconds behind the
     drawing, so pen-up doesn't need to be decided instantly. Treat a 0.15 s
     gap as a *tentative* lift. If the next point arrives soon and close to
     the last one, cancel the lift while it's still waiting in the plot
     queue. That's the healer's logic, applied live.
   - **The two combine well.** If the capture confirms the state block marks
     a new touch, it becomes the tie-breaker: points after a gap *with no
     state block before them* continue the same stroke (cancel the tentative
     lift), and points after a state block start a new one. The timeout then
     only decides *when* the pen lifts, never *whether* a stroke was split.
   - **Capture protocol** (script logs each message with a millisecond
     arrival time; Pencil, not finger):
     1. one slow stroke, then wait 3 s;
     2. one very fast long stroke, then wait 3 s;
     3. a single tap, then wait 3 s;
     4. touch and hold still for 2 s, then lift;
     5. nothing at all for 10 s, to test for an idle heartbeat;
     6. a quick deliberate lift and re-touch at the same spot.
7. **First launch needs internet** (about 150 MB). Accepted, and stated up
   front.
8. **Support load lands on Marc.** Mitigated by the troubleshoot walkthroughs
   and "Copy diagnostics", but real users will still hit new cases.
9. **The UI redesign is the least predictable phase.** Design taste takes
   iteration, and the 3–4 day estimate assumes one review round.
