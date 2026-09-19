# Pantograph App: Build Guide

This is the step-by-step plan for turning draw2axi into one downloadable app,
plus every known gotcha and how each is handled. It is written for whoever
builds it (Marc, or a Claude session). Work through the phases in order. Each
phase ends with a checkpoint, and nothing moves on until that checkpoint passes.

Status: **planning.** Nothing below has been built yet. Tick the boxes as
phases land.

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
   uv would otherwise pick the newest Python.
   → **Pin Python 3.12 in `.python-version`.**
4. **pyaxidraw's download URL is unversioned**
   (`https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip`). `uv.lock`
   records a hash of that file. When Evil Mad Scientist publishes a new
   version, every new install fails with a hash mismatch.
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
12. **Possible latent bug, out of scope, flagged only.** The OSC listener is a
    `ThreadingOSCUDPServer`, which handles every UDP packet on its own thread.
    iDraw sends `/x`, `/pressure` and `/y` as separate messages, so their
    handlers can race: `/y` could be handled before the matching `/x` and plot
    a point with a stale x. A single-threaded `BlockingOSCUDPServer` would
    preserve order, since the handlers are fast. This would be a behaviour
    change, so it isn't part of this build (see Q7). It could be related to
    glitches like the torn-dot strokes that `dot_healer` repairs, but that's
    unproven.

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
                       recording.py; tkinter GUI kept for the old command only
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
  - Pick **one** visual style for exported SVGs and use it everywhere: the
    live export, the tools output and the CLI tools. Record the choice here
    (see Q3).
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
  the `axi_fx_*` effect keys), plus the OSC port and paper settings. The
  server applies the settings on startup, **before** any browser connects, so
  the plotter is configured correctly even with no tab open. Browser storage
  is kept only for UI-only preferences, such as which tab was open.
- [ ] Data folders, via `platformdirs` (see G-15). Drawings go to
      `Documents/Pantograph/`, using the real Documents folder even when
      OneDrive redirects it.
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
    - **iPad** status dot (waiting / receiving; turns green on the first OSC
      message);
    - **Plotter** status dot, with Connect / Reconnect;
    - the IP:port for iDraw, in large, copyable text;
    - lag readout;
    - **Quit**.
  - **Center:** the live canvas, with the current layered rendering (raw
    grey, optimized white, effects blue), plus a legend and layer toggles.
  - **Sidebar tabs:**
    - **Plot:** pen positions with test buttons, variable pressure, tilt,
      flip, optimizer, Home, "effects only".
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
    - a dark theme built around the black canvas;
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

- [ ] Rewrite the README setup section as: download → extract → double-click
      → enter the IP/port shown in the app into iDraw. Add screenshots for
      the Windows "Run anyway" prompt, the Mac "Open Anyway" steps
      (System Settings → Privacy & Security) and the firewall prompts.
- [ ] Keep a "Developers" section: `uv run listen_to_idraw.py`, plus the
      plain `pip install ...` route. The old commands still work.
- [ ] Run the test matrix in §8 on real hardware.
- [ ] Add a meeting or release note to `MEETINGS.html` if relevant.

**Total: about 12–16 working days (2.5–3 weeks).** That's more than the
earlier estimate of 1.5–2 weeks. The difference is the work that reading the
code turned up: recording moving to Python (§2.1), security (§2.2), the
single instance, and the network help. Of those, recording and security are
must-haves. The network help (Phase 7) and the update check could be dropped
to save about a day.

---

## 5. Gotcha register

Every known risk, with its planned mitigation and the phase that handles it.
Re-check this list before release.

| ID | Gotcha | Mitigation | Phase |
|---|---|---|---|
| G-1 | Closing or reloading the tab loses the drawing (it's recorded in the browser) | Record in Python; autosave; `hello` restores the page | 3 |
| G-2 | Any website can drive the plotter via the WebSocket | Origin + Host checks; bind to 127.0.0.1 | 2 |
| G-3 | The firewall prompt says "Python", not "Pantograph"; users click Deny and OSC fails silently | Connection card explains after 20 s; README screenshots; the rule applies to uv's `python.exe` path, so pin Python's exact version so the path (and the rule) stays stable | 7, 10 |
| G-4 | Windows treats new Wi-Fi networks as **Public**, and the firewall blocks inbound traffic there even after "Allow" (Private is ticked by default) | Detect the Public profile and show the fix; explain in the README | 7 |
| G-5 | OSC port 8800 is busy (another program, or a second copy of the app) | Single-instance check first; then a clear error plus a setting to change the port (and a reminder to change iDraw too) | 2, 7 |
| G-6 | Port 5000 is taken by AirPlay Receiver on Macs | The default port is 5810, with fallback to 5811–5830 | 2 |
| G-7 | A Windows user double-clicks the `.bat` inside the zip viewer, so it runs from a temp folder | Launcher detects `\AppData\Local\Temp\` or a `.zip` in its path and says "Extract the zip first (right-click → Extract All)", then pauses | 8 |
| G-8 | `.bat` with LF endings misbehaves; `.command` with CRLF fails | `.gitattributes` eol rules; CI runs the real launchers | 0, 9 |
| G-9 | `.command` loses its executable bit, or macOS Gatekeeper blocks it ("Open Anyway" in System Settings since Sequoia; right-click → Open no longer bypasses it) | Commit with `+x`; verify the release zip keeps the mode; README screenshots; fallback instruction: `chmod +x` in Terminal | 8, 10 |
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
| G-25 | Windows on ARM / Intel Macs | Launcher picks the matching uv build. Intel Macs: uv + numpy support x86_64; test if one is available. Windows on ARM: see Q8 | 8 |
| G-26 | Our dependency links disappear (uv release, pyaxidraw mirror, PyPI) | Everything pinned; uv binary + pyaxidraw both hosted on GitHub releases (uv's own, and ours); PyPI is the one accepted external dependency | 8, 9 |
| G-27 | Updates are manual | Version shown; optional update-available notice | 9 |
| G-28 | OSC packets are handled on separate threads and may race (§2.12) | Out of scope; flagged; see Q7 | n/a |

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
  - `connect_plotter`, `discard_queue`, `quit`;
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
  repo (see Q1).
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
- **Paper size and AxiDraw model settings.** Currently hard-coded to 8.5×11",
  which is wrong for anyone on A4 (see Q4).
- **Option 2** (PyInstaller-packaged `.exe`/`.app`, built in CI) remains a
  later add-on. The single entry point and data-directory design above carry
  over unchanged.

---

## 11. Open questions for Marc

Answers get recorded here, and in §1 once decided.

- **Q1: where do drawings go?** The proposal is `Documents/Pantograph/` for
  everyone, including development runs from the repo, with a setting to
  change it. And what happens to the repo's existing `saved_drawings/`? The
  proposal is to move the files into `Documents/Pantograph/`, keep one or two
  small ones as test fixtures, and stop committing drawings to git.
- **Q2: recording in Python (§2.1 / Phase 3).** This is a real behaviour
  change, and I recommend it strongly. OK?
- **Q3: exported SVG look.** One style for every export. Grey raw strokes as
  the preview shows them, or white-on-black as `svg_transform` writes? This
  only affects how files look in a viewer; plotting is unaffected.
- **Q4: paper size / AxiDraw model setting.** Add it now (about half a day:
  presets for Letter, A4, A3 and custom, clamped to the machine's travel), or
  later?
- **Q5: `python svg_transform.py`.** Keep its tkinter window for the old
  command (zero effort; the functions are shared), or have the command open
  the app's Tools tab instead?
- **Q6: licence.** The repo has no LICENSE file. The AxiDraw software's GitHub
  repo is GPL-2.0. Hosting a copy of pyaxidraw (G-12) is fine under the GPL
  if we include its licence and source (it *is* source). Choosing a licence
  for this project (for example GPL-3.0 or MIT) should happen before the
  first public release.
- **Q7: OSC ordering (§2.12).** Investigate the threading race as a separate
  task, before or after the app?
- **Q8: which machines to support.** Windows on ARM (Snapdragon laptops)
  and Intel Macs: support officially, best effort, or ignore?
- **Q9: naming.** "Pantograph" for the launcher, the data folders and the
  window title?
