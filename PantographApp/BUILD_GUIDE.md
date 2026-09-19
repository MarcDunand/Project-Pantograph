# Pantograph App: Build Guide

This is the step-by-step plan for turning draw2axi into one downloadable app,
plus the known gotchas and how each is handled. It is written for whoever
builds it (Marc, or a Claude session). Work through the phases in order. Each
phase ends with a checkpoint, and nothing moves on until that checkpoint passes.
**Keep this file current as work lands:** tick boxes, record decisions, add
gotchas.

**Status:** Phases −1 to 4 are done (2026-09-19), apart from checks that
need the real AxiDraw (listed just below). **Phase 5 (the UI) is next.** The
pressure-lag fix is deferred until an Apple Pencil is available (see
Phase −1).

**Hardware checks.** These can't be verified without the iPad and AxiDraw.
Marc's session on 2026-09-19 covered the first five:
- [x] Phase 1: plotting live behaves exactly as before (strokes, pen tests,
      Home, replay, effects).
- [x] Phase 1: unplugging the AxiDraw's USB mid-plot shows an error instead of
      freezing, and the app keeps running.
- [x] Phase 1: with the AxiDraw unplugged at startup, the console reports
      `not_found`.
- [x] Phase 3: plotting a saved SVG ("plot svg") plots it once, not twice.
- [ ] **Phase 1, re-check after a fix:** closing the console window mid-plot
      lifts the pen **and releases the motors** (the carriage can be pushed by
      hand). **Restart the app first**, so it runs the fixed code. (Marc's
      second try at 01:47 ran an app started at 01:40, before the fix was
      saved at 01:42.)
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
  postprocess.py       unchanged
  svg_transform.py     transform functions unchanged; load/build re-exported from
                       recording.py; tkinter GUI removed; the command opens the
                       app's Tools tab
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
    ui/                index.html, app.js, style.css
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
      **Still to come:** `/ui/*` (Phase 5, when the UI moves into files) and
      `/drawings/<name>` for thumbnails (Phase 6), each with the
      path-traversal guard.
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
      list). The `svg_transform.py` "already running" message comes with
      Phase 6.
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

First move the UI, then restyle it, as separate commits.

- [ ] **5a, move:** move the HTML, CSS and JS out of `preview.py` into
      `PantographApp/ui/`. Replace the placeholder substitution with `hello`.
      Change nothing visible.
- [ ] **5b, restyle and reorganize:**
  - **Top bar:** app name; **iPad** and **Plotter** status buttons; the IP and
    port for iDraw (copyable); lag; **Quit**.
  - **Status buttons:** a coloured dot and a label. A click opens a small
    panel.
    - **iPad:** *Waiting* (nothing received yet), *Receiving*, *Idle*
      (received before; normal between strokes), or *Problem* (port busy).
      Actions: Restart listener, Change OSC port (with a reminder to change
      it in iDraw too), Troubleshoot. There's nothing to "dial": the iPad
      sends to us over UDP.
    - **Plotter:** *Connected*, *Not found*, *Error* (with the message), *No
      pyaxidraw*, or *Dry run*. Actions: Connect, Home, Disengage motors,
      Troubleshoot.
  - **Troubleshoot (required in both panels):** a numbered checklist on one
    panel, with the live status dot at the top so the user sees the moment
    it's fixed, plus a "Copy diagnostics" button (version, OS, IPs, port,
    statuses, recent log lines).
    - **iPad checklist:**
      1. iDraw OSC is open, with this IP and port (shown, copyable).
      2. Both devices are on the same Wi-Fi.
      3. Windows: the Wi-Fi is set to **Private**, not Public. Say how to
         change it.
      4. The firewall prompt was answered "Allow". If it was cancelled, give
         the steps to allow Python in Windows Defender Firewall (or the Mac
         firewall, which is off by default).
      5. Nothing else is using port 8800.
      6. **Some school, work, hotel and guest networks block devices from
         reaching each other.** Try a phone hotspot to confirm; the fix is a
         different network or Tailscale (see the README).
      7. Restart the listener and draw a stroke.
    - **Plotter checklist:**
      1. USB is plugged in, **and** the AxiDraw's own power supply is on (USB
         alone doesn't power the motors).
      2. Try another USB port or cable (some cables are charge-only).
      3. Close other programs using the AxiDraw (Inkscape's extension, or a
         second Pantograph).
      4. pyaxidraw is installed (the app shows this).
      5. Press Connect, and read the exact error if it fails.
      6. Try Pen test up/down.
  - **Center:** the live canvas as white paper with greyscale strokes. The
    optimized and effect layers are overlays in two accent colours, with
    toggles.
  - **Sidebar tabs:**
    - **Plot:** paper and model, pen positions with tests, variable
      pressure, tilt, flip, optimizer, Home, effects only.
    - **Effects:** built from `postprocess.effect_specs()`, as today.
    - **Tools:** flip H/V, minimum-width filter, heal dots (with its report).
      Each saves a new file and opens it.
    - **Clear without saving** (added 2026-09-19, Marc): next to "new
      drawing", a way to clear the preview and start fresh *without*
      keeping what's there, since "new drawing" always saves. It needs an
      in-page confirmation ("Discard this drawing?"; no `confirm()`, per the
      1B rules), because it's the one action that deletes work. Backend: a
      `discard_drawing` message → `Recorder.clear()`, delete `autosave.svg`,
      broadcast `new_drawing` with `saved: null` (the pages already handle
      that).
    - **Replay controls** (added 2026-09-19, Marc): while a saved drawing is
      plotting ("plot svg"), show its progress with **Pause/Resume** and
      **Cancel** (backend in Phase 6).
    - **Drawings:** a list of saved drawings (name, date, thumbnail), with
      Open and Plot; an "Open drawings folder" button; and "Open file…" to
      load an SVG from elsewhere (it replaces today's "plot svg").
  - **First run:** until the first OSC message arrives, the canvas shows the
    iDraw setup steps with this machine's IP and port, plus a link to
    Troubleshoot.
  - **Look:** CSS variables for colours and spacing, the system font, a light
    paper-like theme, and consistent controls. Nothing beyond that is needed
    for the demo.
- [ ] Rules that keep 1B easy (§7): no `<a download>`, blob downloads,
      `window.open`, `alert()` or `confirm()`; everything the page needs
      comes from `hello`.
- [ ] Check it in Chrome/Edge and Safari (Safari's engine is the one 1B uses
      on Mac).
- **Checkpoint:** Marc reviews the UI on real hardware.

### Phase 6: Tools and drawings backend

- [ ] WebSocket handlers: `library_list`, `library_open`, `open_folder`
      (`explorer` / `open`), `open_file` (the file's contents, sent from the
      page's file picker), `tool_apply` (saves a new file, never overwriting,
      with suffixes `_flipped`, `_minWidth<N>` and `_healed` as the CLIs use
      today), and `plot_drawing` (the existing replay path).
- [ ] Tools run on the open drawing: the live session, or a saved one.
- [ ] **Pause/resume and cancel a replay plot** (added 2026-09-19, Marc).
      `_replay_recording` runs on its own thread and only *feeds* points; the
      plotter may be seconds behind it. So:
  - **Pause** must stop both the replay thread feeding points and the plotter
    thread taking commands (an `Event` each checks); the pen rests (lifts)
    while paused. **Resume** clears both.
  - **Cancel** stops the replay thread (a flag checked per point), discards the
    queue, lifts the pen (and ends the open stroke so its effects don't fire
    oddly), and leaves the app ready to draw. Live drawing during a paused
    replay shouldn't be possible, or should cancel the replay first. Decide
    this when building it.
  - Messages: `replay_pause`, `replay_resume`, `replay_cancel`;
    `replay_progress {done, total, paused}` broadcast every ~0.5 s for the
    progress bar.
- [ ] Transforms and heals run on a worker thread, so the WebSocket never
      blocks.
- [ ] Thumbnails: `thumbnail_svg()`, cached next to the drawing and rebuilt if
      the drawing is newer.
- [ ] Flags `--open <file>` and `--tab <name>`.
      **`python svg_transform.py [file.svg]`** starts the app with them (or
      prints the "already running" message from Phase 2). `--selftest` stays.
      The tkinter code is removed. `python dot_healer.py` stays a CLI,
      unchanged.
- **Checkpoint:** everything `svg_transform.py` and `dot_healer.py` do works
  from the UI, and the output files match the CLIs'.

### Phase 7: IP list and port errors

In `PantographApp/netinfo.py`.

- [ ] **Local IPs:**
  - the primary one via the UDP "connect" trick (no packet is sent);
  - all others from `socket.getaddrinfo(hostname)`, labelling `100.64.0.0/10`
    as Tailscale;
  - `169.254.*` hidden;
  - if there's no internet, the full list.
- [ ] **OSC port busy:** if binding 8800 fails, the iPad status shows
      *Problem: port 8800 in use*, and its panel offers Change OSC port.
- **Checkpoint:** the IP shown matches `ipconfig`; a busy 8800 shows the
  problem state.

### Phase 8: Launchers and installers

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

- **Checkpoint:** on a Windows account and a Mac that have no Python or uv,
  **both** routes work end to end: the one-line installer and the click
  download. The second launch works offline.

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
- "Clear" becomes "New drawing", which saves first.
- Saves go to `Documents/Pantograph/`, not `saved_drawings/`.
- The canvas, the UI and exports are white paper with greyscale strokes.
- Paper size and AxiDraw model are settings.
- `python svg_transform.py` opens the app's Tools tab.
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
- [ ] The UI works in Chrome/Edge and Safari.

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
| G-22 | Large drawings (several MB) make the list and tools slow | Thumbnails; worker thread | 6 |
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
| 2026-09-18 | `python svg_transform.py` | Opens the app's Tools tab (most consistent look) |
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
