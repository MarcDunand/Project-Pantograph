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
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "Pantograph"


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
    The one place the UI gets opened. Today: the default browser. Option 1B
    replaces this with a pywebview window that falls back to the browser —
    which is why nothing else may open the UI itself.
    """
    if enabled:
        webbrowser.open(url)


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
