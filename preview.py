"""
preview.py

Live drawing preview server.
Serves a local web page that displays strokes as they arrive from iDraw OSC,
and a WebSocket feed (/ws) between that page and listen_to_idraw.py — both on
one port, bound to 127.0.0.1 only.

Run:   started by listen_to_idraw.py, which opens the page
       (http://127.0.0.1:5810, or the next free port up to 5830)
"""

import asyncio
import base64
import concurrent.futures
import http
import json
import logging
import os
import re
import threading

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

log = logging.getLogger("pantograph.preview")

# ─── configuration ────────────────────────────────────────────────────────────

# The UI's port: the first free one in this range. Not 5000, which AirPlay
# Receiver holds on many Macs.
DEFAULT_PORTS = range(5810, 5831)

# Downloads from the preview are written here rather than the browser's
# Downloads folder (the browser controls that and a web page can't redirect
# it, so the page sends the bytes to us). listen_to_idraw points this at the
# user's drawings folder; this default only applies when used on its own.
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_drawings")

port: int | None = None   # the port in use, once start() has run
_server = None

# ─── shared broadcast set ─────────────────────────────────────────────────────

_ws_clients: set = set()
_ws_loop = None

_message_callback = None   # called for browser → Python messages listen_to_idraw registers
_hello_provider   = None   # builds the snapshot sent to each page as it connects
_listeners: list  = []     # called with every broadcast message (the session recorder)
_routes: dict     = {}     # "/prefix/" → fn(rest) returning (status, bytes, content type, headers) or None


def register_message_callback(cb):
    """
    Let listen_to_idraw.py receive browser control messages (e.g. home). If the
    callback returns a dict, it's sent back to the page that sent the message.
    If it returns a concurrent.futures.Future (slow work on another thread), the
    dict it resolves to is sent back when it's ready.
    """
    global _message_callback
    _message_callback = cb


def register_hello_provider(fn):
    """fn() → the 'hello' message a page gets on connecting: everything it needs to catch up."""
    global _hello_provider
    _hello_provider = fn


def add_listener(fn):
    """fn(message) is called with every broadcast message, on the broadcasting thread."""
    _listeners.append(fn)


def register_route(prefix: str, fn):
    """
    Serve GET requests under `prefix` (e.g. "/drawings/"): fn(rest_of_path) returns
    (bytes, content type) or (bytes, content type, extra headers), or None for 404.
    Runs on the server's thread, so it should be quick.
    """
    _routes[prefix] = fn


# ─── WebSocket server ─────────────────────────────────────────────────────────

async def _ws_handler(websocket):
    """Accept a browser connection; handle incoming control messages."""
    # Snapshot and join in one step (no await between), so nothing broadcast
    # meanwhile is lost; at worst a message lands in both, which redraws harmlessly.
    hello = _hello_provider() if _hello_provider else None
    _ws_clients.add(websocket)
    try:
        if hello is not None:
            await websocket.send(json.dumps(hello))
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if msg.get("type") == "save_file":
                    await websocket.send(json.dumps(_save_reply(msg)))
                elif _message_callback:
                    reply = _message_callback(msg)
                    if isinstance(reply, dict):
                        await websocket.send(json.dumps(reply))
                    elif isinstance(reply, concurrent.futures.Future):
                        asyncio.ensure_future(_send_when_done(websocket, reply))
            except Exception:
                log.exception("[preview] error handling a message from the page")
    finally:
        _ws_clients.discard(websocket)


async def _send_when_done(websocket, future):
    try:
        reply = await asyncio.wrap_future(future)
        if isinstance(reply, dict):
            await websocket.send(json.dumps(reply))
    except Exception:
        log.exception("[preview] error finishing a reply")


def broadcast(message: dict):
    """
    Thread-safe broadcast of a dict to all connected browsers (and to every
    listener, whether or not a browser is open). Called from the engine's
    threads.
    """
    for fn in _listeners:
        try:
            fn(message)
        except Exception:
            log.exception("[preview] listener failed")
    if _ws_loop is None or not _ws_clients:
        return
    payload = json.dumps(message)
    asyncio.run_coroutine_threadsafe(_broadcast_async(payload), _ws_loop)


async def _broadcast_async(payload: str):
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ─── the page ────────────────────────────────────────────────────────────────
#
# The UI is plain files in PantographApp/ui/, served as they are: "/" is
# index.html, "/ui/<name>" everything else. Everything the page needs from the
# engine (settings, the drawing so far, statuses, the effect list) arrives in
# the 'hello' message when it connects.

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PantographApp", "ui")
_UI_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


def _ui_file(name: str):
    """(bytes, content type) for a file in UI_DIR, or None. Only plain names in that folder."""
    ext = os.path.splitext(name)[1].lower()
    if name != os.path.basename(name) or name.startswith(".") or ext not in _UI_TYPES:
        return None
    path = os.path.join(UI_DIR, name)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read(), _UI_TYPES[ext]


# ─── saving ───────────────────────────────────────────────────────────────────

def _save_drawing(filename: str, b64: str) -> str:
    """
    Write base64-encoded bytes into SAVE_DIR, returning the file name used.
    The requested name is sanitized to its basename and, if a file already
    exists, gets a numeric suffix so saves never clobber earlier drawings.
    """
    os.makedirs(SAVE_DIR, exist_ok=True)
    base = os.path.basename(filename) or "drawing"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    stem, ext = os.path.splitext(base)

    name = base
    n = 1
    while os.path.exists(os.path.join(SAVE_DIR, name)):
        name = f"{stem}_{n}{ext}"
        n += 1

    with open(os.path.join(SAVE_DIR, name), "wb") as f:
        f.write(base64.b64decode(b64))
    log.info("[preview] saved  →  %s", os.path.join(SAVE_DIR, name))
    return name


def _save_reply(msg: dict) -> dict:
    """Handle the page's save_file message; the reply goes back to that page."""
    try:
        name = _save_drawing(str(msg["filename"]), str(msg["b64"]))
        return {"type": "saved", "ok": True, "path": name}
    except Exception as exc:   # noqa: BLE001 — reported to the page
        log.error("[preview] save failed: %s", exc)
        return {"type": "saved", "ok": False, "error": str(exc)}


# ─── one server: the page over HTTP, the live feed over /ws ───────────────────

def _response(status: int, body: bytes, content_type: str, extra=()) -> Response:
    headers = Headers([
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        # Never serve a stale page after an update.
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        *extra,
    ])
    return Response(status, http.HTTPStatus(status).phrase, headers, body)


def _process_request(connection, request):
    """
    Every request passes through here first. Plain HTTP gets its response here;
    /ws carries on into the WebSocket handshake, where `origins` checks that the
    page connecting is ours.
    """
    # Blocks DNS rebinding: a web page served from a name that resolves to
    # 127.0.0.1 would arrive here with that name in its Host header.
    if request.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return _response(403, b"forbidden", "text/plain")
    path = request.path.split("?", 1)[0]
    if path == "/ws":
        return None
    if path == "/" or path.startswith("/ui/"):
        found = _ui_file("index.html" if path == "/" else path[len("/ui/"):])
        if found:
            return _response(200, *found)
    for prefix, fn in _routes.items():
        if path.startswith(prefix):
            from urllib.parse import unquote
            try:
                found = fn(unquote(path[len(prefix):]))
            except Exception:
                log.exception("[preview] error serving %s", path)
                return _response(500, b"error", "text/plain")
            if found:
                return _response(200, *found)
            break
    if path == "/health":
        body = json.dumps({"app": "pantograph", "pid": os.getpid()}).encode()
        return _response(200, body, "application/json")
    return _response(404, b"not found", "text/plain")


def start(ports=DEFAULT_PORTS, save_dir=None) -> int:
    """
    Start the server on the first free port in `ports`, on a background thread.
    Returns the port once it's listening. Raises RuntimeError if none is free.
    """
    global SAVE_DIR
    if save_dir is not None:
        SAVE_DIR = str(save_dir)
    ports = list(ports)
    ready = threading.Event()

    async def _open():
        global port, _server
        for p in ports:
            # Browsers always send Origin, and it must be this page. A missing
            # Origin means another local program (a second launch, the
            # svg_transform command), which runs as the user anyway.
            origins = [f"http://127.0.0.1:{p}", f"http://localhost:{p}", None]
            try:
                # max_size: an uploaded SVG replay arrives as one frame carrying
                # every point of a drawing, which easily passes the 1 MB default.
                _server = await serve(_ws_handler, "127.0.0.1", p, origins=origins,
                                      process_request=_process_request,
                                      max_size=64 * 1024 * 1024)
            except OSError:
                continue
            port = p
            return

    def _run():
        global _ws_loop
        _ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_ws_loop)
        try:
            _ws_loop.run_until_complete(_open())
        finally:
            ready.set()
        if _server is not None:
            _ws_loop.run_forever()

    threading.Thread(target=_run, name="preview", daemon=True).start()
    ready.wait()
    if port is None:
        raise RuntimeError(f"no free port for the UI in {ports[0]}–{ports[-1]}")
    log.info("[preview] http://127.0.0.1:%d  (live feed on /ws)", port)
    return port


def stop() -> None:
    """Stop accepting connections. Safe to call from any thread, or twice."""
    if _server is not None and _ws_loop is not None:
        _ws_loop.call_soon_threadsafe(_server.close)
