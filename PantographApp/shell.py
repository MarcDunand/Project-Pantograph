"""
The app shell around listen_to_idraw: where files live, logging, opening the
UI, and making sure every way the app can exit lifts the pen first.

Nothing here knows about drawing or plotting — it's called by
listen_to_idraw.main() and handed the engine's shutdown function.
"""

import atexit
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "Pantograph"
# Windows groups taskbar buttons by this id. Without one of our own, the window
# is filed under Python and wears Python's icon.
APP_ID = "MarcDunand.Pantograph"

log = logging.getLogger("pantograph")


def app_version() -> str:
    """The version in pyproject.toml (the one place it's written down)."""
    try:
        import tomllib
        root = Path(__file__).resolve().parents[1]
        with open(root / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:            # noqa: BLE001 — a missing version shouldn't stop the app
        return "dev"


# ── where files live ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Paths:
    drawings: Path   # saved and autosaved drawings (the user sees these)
    config:   Path   # settings.json
    logs:     Path   # pantograph.log
    runtime:  Path   # instance.json; the launchers also keep uv, Python and the venv here


def resolve_paths(data_dir: str | None = None) -> Paths:
    """
    The standard per-user folders, or everything under data_dir when given
    (--data-dir: tests and development). Folders are created when first used,
    not here.
    """
    if data_dir:
        root = Path(data_dir).expanduser().resolve()
        return Paths(drawings=root, config=root / "config", logs=root / "logs",
                     runtime=root / "runtime")
    # user_documents_dir resolves the real Documents folder, including when
    # OneDrive has redirected it.
    return Paths(
        drawings=Path(platformdirs.user_documents_dir()) / APP_NAME,
        config=Path(platformdirs.user_config_dir(APP_NAME, appauthor=False)),
        logs=Path(platformdirs.user_log_dir(APP_NAME, appauthor=False)),
        runtime=Path(platformdirs.user_data_dir(APP_NAME, appauthor=False)) / "runtime",
    )


# ── logging ──────────────────────────────────────────────────────────────────

def use_utf8_console() -> None:
    """
    Make stdout and stderr accept the characters we actually print.

    Log lines and the --help text contain ≈ → × —. Without this, printing them
    to anything that isn't a UTF-8 console (a pipe, a redirect, a fresh Windows
    console at cp1252) raises UnicodeEncodeError — inside an OSC handler
    mid-stroke, or on `--help` before anything has started.

    Safe to call more than once, and early: it touches nothing else.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):        # a stream that can't be changed
                pass


def setup_logging(logs_dir: Path, verbose: bool = False) -> Path:
    """
    Console + rotating log file for the "pantograph" logger. INFO and up by
    default; DEBUG (every point and plotter move) with --verbose. Returns the
    log file's path.
    """
    use_utf8_console()

    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger("pantograph")
    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)

    if sys.stdout is not None:           # None under pythonw: there's no console
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "pantograph.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.addHandler(file_handler)
    return log_file


# ── one copy at a time ───────────────────────────────────────────────────────
#
# A running copy records its UI port in runtime/instance.json. A second launch
# (say, double-clicking the launcher again) finds it, opens the UI it already
# has, and exits instead of fighting it for the OSC port.

def running_instance(runtime: Path) -> str | None:
    """The UI URL of a copy that's already running, or None."""
    try:
        port = int(json.loads((runtime / "instance.json").read_text())["port"])
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(url + "/health", timeout=1) as r:
            if json.load(r).get("app") == "pantograph":
                return url
    except Exception:            # noqa: BLE001 — no file, stale file, nothing answering
        pass
    return None


def record_instance(runtime: Path, port: int) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "instance.json").write_text(json.dumps({"port": port, "pid": os.getpid()}))


def forget_instance(runtime: Path) -> None:
    """Remove instance.json, if it's ours."""
    f = runtime / "instance.json"
    try:
        if json.loads(f.read_text()).get("pid") == os.getpid():
            f.unlink()
    except Exception:            # noqa: BLE001
        pass


# ── the UI ───────────────────────────────────────────────────────────────────

def ask_running(url: str, message: dict, reply_type: str, timeout: float = 10.0) -> dict | None:
    """
    Send one message to a running copy over its WebSocket (the way the page
    does) and return its reply of type `reply_type`, or None if it didn't answer.
    """
    from websockets.sync.client import connect
    try:
        with connect(url.replace("http://", "ws://") + "/ws", open_timeout=timeout) as ws:
            ws.send(json.dumps(message))
            while True:
                reply = json.loads(ws.recv(timeout=timeout))
                if reply.get("type") == reply_type:
                    return reply
    except Exception:            # noqa: BLE001 — not answering is all the caller needs to know
        return None


def open_ui(url: str, enabled: bool = True) -> None:
    """
    Open the UI in the default browser: the fallback when Pantograph's own
    window (run_window) isn't available, and what --browser asks for. Nothing
    else may open the UI itself.
    """
    if enabled:
        webbrowser.open(url)


# ── the window ───────────────────────────────────────────────────────────────
#
# Pantograph's own window: the same page, shown by pywebview in the system's
# web engine (Edge WebView2 on Windows, WKWebView on a Mac) instead of a
# browser tab. It needs the main thread — Cocoa insists — which is why
# everything else in the engine runs on other threads.

_window = None                  # the open window, or None (browser mode)


def current_window():
    """The open window, or None when the UI is in a browser (or not open)."""
    return _window


def run_window(url: str, storage: Path, stop: threading.Event,
               icon: Path | None = None, on_ready=None) -> bool:
    """
    Show the UI in a window of its own and block until that window is closed.
    Call it on the main thread.

    Returns True once the window has been open and is now closed, and False if
    it never opened — a missing library, no WebView2 on this PC — in which case
    the caller opens the browser instead. It doesn't raise: on a machine where
    the window can't work, the app must still start.

    `stop` being set closes the window (the Quit button). `on_ready(window)`,
    if given, is called on a worker thread once the window is up.
    """
    global _window
    try:
        import webview
    except Exception as e:                   # noqa: BLE001 — any failure means "use the browser"
        log.info("[window] not available (%s) — using the browser", e)
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:                    # noqa: BLE001 — cosmetic only
            pass

    window = None
    try:
        window = webview.create_window(APP_NAME, url, width=1280, height=860,
                                       min_size=(900, 600), text_select=True)

        def on_initialized(renderer):
            # Without the WebView2 runtime pywebview falls back to the
            # Internet Explorer engine, which can't run the page. Returning
            # False cancels the window, and start() below returns at once.
            if sys.platform == "win32" and renderer != "edgechromium":
                log.info("[window] no Edge WebView2 on this PC (%s) — using the browser", renderer)
                return False
            log.info("[window] opening (%s)", renderer)

        def while_open():
            if on_ready is not None:
                try:
                    on_ready(window)
                except Exception:            # noqa: BLE001
                    log.exception("[window] on_ready failed")
            stop.wait()
            try:
                window.destroy()
            except Exception:                # noqa: BLE001 — already closed
                pass

        window.events.initialized += on_initialized
        _window = window
        # private_mode: nothing is kept between runs (settings live in
        # settings.json). storage_path keeps the engine's working files in our
        # own folder rather than wherever pywebview would put them.
        webview.start(while_open, private_mode=True, storage_path=str(storage),
                      icon=str(icon) if icon and icon.is_file() else None)
        return window.events.shown.is_set()
    except Exception as e:                   # noqa: BLE001
        log.info("[window] couldn't open (%s) — using the browser", e)
        # If it was up when this happened, it has been the UI: don't open a
        # browser on top of a session that's ending.
        return window is not None and window.events.shown.is_set()
    finally:
        _window = None


def page_is_live(window, timeout: float = 30.0) -> bool:
    """
    True once the page in the window has loaded and connected to the engine
    (its status reads "live"). Used by --window-test.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if window.evaluate_js("document.getElementById('conn-label').textContent") == "live":
                return True
        except Exception:                    # noqa: BLE001 — not loaded yet
            pass
        time.sleep(0.25)
    return False


def focus_window() -> bool:
    """Bring the window to the front. False if the UI isn't in a window."""
    window = _window
    if window is None:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            user32 = ctypes.windll.user32
            user32.FindWindowW.restype = ctypes.c_void_p
            try:
                hwnd = int(window.native.Handle.ToInt64())
            except Exception:                # noqa: BLE001
                hwnd = user32.FindWindowW(None, APP_NAME)
            if not hwnd:
                return False
            hwnd = ctypes.c_void_p(hwnd)
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)   # SW_RESTORE; leaves a maximised window alone
            user32.SetForegroundWindow(hwnd)
        else:
            window.show()
        return True
    except Exception:                        # noqa: BLE001
        log.exception("[window] couldn't bring it to the front")
        return False


def show_running(url: str) -> bool:
    """
    Ask the copy that's already running to bring its window forward. False if
    it has no window (its UI is in a browser) or didn't answer.
    """
    if sys.platform == "win32":
        # Windows only lets a process take the foreground if the one that has
        # it — this one, just started by the user — says so.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)     # ASFW_ANY
        except Exception:                    # noqa: BLE001
            pass
    reply = ask_running(url, {"type": "focus_window"}, "focused", timeout=3.0)
    return bool(reply and reply.get("ok"))


def has_console() -> bool:
    """False when started without a console (pythonw, the launcher's default)."""
    return sys.stderr is not None


def alert(message: str) -> None:
    """
    Tell the user something went wrong when there is no console to print it
    in. With a console, the log line already did.
    """
    if has_console() or sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)   # MB_ICONERROR
    except Exception:                        # noqa: BLE001
        pass


# ── exiting ──────────────────────────────────────────────────────────────────

# Kept at module level: Windows calls this through a C pointer, and it must
# not be garbage-collected while installed.
_console_handler = None


def install_exit_handlers(shutdown) -> None:
    """
    Route every way the process can end through shutdown(), so the pen is
    lifted and the motors released even when the window is simply closed.
    Must be called from the main thread (signal handlers can only be set there).
    shutdown() must be safe to call more than once and from any thread.

    Ctrl+C isn't handled here: it raises KeyboardInterrupt in main(), which
    calls shutdown() itself.
    """
    global _console_handler
    atexit.register(shutdown)

    def on_signal(signum, _frame):
        shutdown()
        # Leave via the normal path, so the main loop's finally blocks run too.
        raise SystemExit(0)

    # SIGHUP: the Mac Terminal window was closed. SIGTERM: asked to quit.
    # SIGBREAK: Ctrl+Break on Windows.
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, on_signal)

    if sys.platform == "win32":
        # Closing the console window (or logging off / shutting down) sends a
        # control event, not a signal, and Windows ends the process a few
        # seconds after the handler returns. Run shutdown() before returning.
        import ctypes
        from ctypes import wintypes

        CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 2, 5, 6
        HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def on_console_event(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                shutdown()
                return True
            return False     # Ctrl+C etc.: let Python's default handling run

        _console_handler = HandlerRoutine(on_console_event)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
