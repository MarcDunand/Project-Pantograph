"""
The whole app, end to end: the real program launched as a subprocess (dry run,
no browser, a temporary data folder, free ports), driven over UDP, HTTP and
the WebSocket the way iDraw and the page drive it.
"""
import asyncio
import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest
import websockets
from pythonosc.udp_client import SimpleUDPClient

from conftest import ROOT


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self, data_dir, ui_port=None, osc_port=None, dry_run=True):
        self.data_dir = str(data_dir)
        self.ui_port = ui_port or free_port()
        self.osc_port = osc_port or free_port(socket.SOCK_DGRAM)
        self.proc = subprocess.Popen(
            [sys.executable, "listen_to_idraw.py", *(["--dry-run"] if dry_run else []), "--no-browser",
             "--data-dir", self.data_dir, "--port", str(self.ui_port), "--osc-port", str(self.osc_port)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED="1"), text=True, encoding="utf-8")
        self.lines = []

    def wait_for(self, text, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            line = self.proc.stdout.readline()
            if not line:
                return False
            self.lines.append(line.rstrip())
            if text in line:
                return True
        return False

    def finish(self, timeout=15):
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return "timeout"
        self.lines += self.proc.stdout.read().splitlines()
        return rc

    def get(self, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.ui_port, timeout=5)
        c.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.ui_port}"})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.ui_port}/ws"

    def quit(self):
        async def go():
            async with websockets.connect(self.ws_url) as ws:
                await ws.send(json.dumps({"type": "quit"}))
        asyncio.run(go())
        return self.finish()


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path)
    assert a.wait_for("[ui] http://127.0.0.1:"), "\n".join(a.lines)
    yield a
    if a.proc.poll() is None:
        a.proc.kill()


def send_stroke(osc_port, n=20):
    c = SimpleUDPClient("127.0.0.1", osc_port)
    for addr, v in [("/r", 0.0), ("/pen", 1.0), ("/canvasWidth", 440.0), ("/canvasHeight", 956.0)]:
        c.send_message(addr, v)
    for i in range(n):
        c.send_message("/x", 100.0 + i * 5)
        c.send_message("/y", 400.0)
        c.send_message("/pressure", 2.0)
    c.send_message("/r", 0.0)                 # the next stroke's block closes this one


async def collect(ws, seconds):
    got, end = [], time.time() + seconds
    while time.time() < end:
        try:
            got.append(json.loads(await asyncio.wait_for(ws.recv(), 0.2)))
        except asyncio.TimeoutError:
            pass
    return got


# ── startup, HTTP, security ──────────────────────────────────────────────────

def test_startup_reports_ports_and_dry_run(app):
    assert any(f"[osc] listening on port {app.osc_port}" in l for l in app.lines)
    assert any("[axidraw] dry_run" in l for l in app.lines)
    assert os.path.exists(os.path.join(app.data_dir, "runtime", "instance.json"))


def test_http_serves_page_and_health(app):
    status, headers, body = app.get("/")
    assert status == 200 and b"<!DOCTYPE html>" in body and headers["Cache-Control"] == "no-store"
    status, _, body = app.get("/health")
    assert status == 200 and json.loads(body)["app"] == "pantograph"
    assert app.get("/nope")[0] == 404


def test_foreign_host_header_is_refused(app):
    """DNS rebinding: a page from a name that resolves to 127.0.0.1."""
    assert app.get("/", host=f"evil.example:{app.ui_port}")[0] == 403


def test_foreign_origin_websocket_is_refused(app):
    async def go():
        async with websockets.connect(app.ws_url, origin="http://evil.example"):
            pass
    with pytest.raises(Exception):
        asyncio.run(go())


# ── drawing, saving, quitting ────────────────────────────────────────────────

def test_stroke_reaches_page_and_save_stays_in_drawings_folder(app):
    async def go():
        async with websockets.connect(app.ws_url, origin=f"http://127.0.0.1:{app.ui_port}") as ws:
            send_stroke(app.osc_port)
            await ws.send(json.dumps({"type": "save_file", "filename": "../../escape.svg",
                                      "b64": base64.b64encode(b"<svg/>").decode()}))
            return await collect(ws, 2.0)
    msgs = asyncio.run(go())
    assert sum(m["type"] == "point" for m in msgs) == 20
    assert any(m["type"] == "pen_up" for m in msgs)
    saved = [m for m in msgs if m["type"] == "saved"]
    assert saved and saved[0]["ok"] and saved[0]["path"] == "escape.svg"
    assert os.path.exists(os.path.join(app.data_dir, "escape.svg"))


def test_quit_exits_cleanly_and_forgets_instance(app):
    assert app.quit() == 0
    assert any("[shutdown]" in l for l in app.lines)
    assert not any("[point]" in l for l in app.lines)          # quiet without --verbose
    assert not os.path.exists(os.path.join(app.data_dir, "runtime", "instance.json"))
    log = open(os.path.join(app.data_dir, "logs", "pantograph.log"), encoding="utf-8").read()
    assert "[shutdown]" in log


# ── more than one copy, busy ports ───────────────────────────────────────────

def test_second_launch_defers_to_the_first(app):
    second = App(app.data_dir)
    assert second.finish() == 0
    assert any("already running" in l for l in second.lines)


def test_busy_osc_port_is_reported_not_fatal(tmp_path):
    osc_port = free_port(socket.SOCK_DGRAM)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", osc_port))
    try:
        a = App(tmp_path, osc_port=osc_port)
        assert a.wait_for("[ui] http://127.0.0.1:")
        assert any("port_busy" in l for l in a.lines)
        assert a.quit() == 0
    finally:
        blocker.close()


def test_busy_explicit_ui_port_fails_clearly(tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        a = App(tmp_path, ui_port=blocker.getsockname()[1])
        assert a.finish() == 1
        assert any("Can't start" in l for l in a.lines)
    finally:
        blocker.close()


# ── the session lives in Python ──────────────────────────────────────────────

def hello(app):
    """Connect as a fresh page and return the 'hello' it gets."""
    async def go():
        async with websockets.connect(app.ws_url) as ws:
            return json.loads(await asyncio.wait_for(ws.recv(), 5))
    return asyncio.run(go())


def send(app, *msgs, wait=1.0):
    async def go():
        async with websockets.connect(app.ws_url) as ws:
            await asyncio.wait_for(ws.recv(), 5)          # hello
            for m in msgs:
                await ws.send(json.dumps(m))
            return await collect(ws, wait)
    return asyncio.run(go())


def wait_until(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def svgs(folder):
    return sorted(f for f in os.listdir(folder) if f.endswith(".svg"))


def test_a_new_page_catches_up_with_the_drawing_so_far(app):
    send_stroke(app.osc_port)
    time.sleep(0.5)
    h = hello(app)
    assert h["type"] == "hello" and h["osc_status"]["state"] == "listening"
    kinds = [m["type"] for m in h["drawing"]]
    assert kinds.count("point") == 20 and kinds.count("pen_up") == 1


def test_autosave_then_new_drawing_saves_and_starts_fresh(app):
    send_stroke(app.osc_port)
    autosave = os.path.join(app.data_dir, "autosave.svg")
    assert wait_until(lambda: os.path.exists(autosave))
    msgs = send(app, {"type": "new_drawing"})
    new = [m for m in msgs if m["type"] == "new_drawing"]
    assert new and new[0]["saved"].startswith("drawing-")
    from recording import load_svg
    rec, _, _ = load_svg(os.path.join(app.data_dir, new[0]["saved"]))
    assert len(rec["strokes"]) == 1 and len(rec["strokes"][0]["points"]) == 20
    assert not os.path.exists(autosave)
    assert hello(app)["drawing"] == []


def test_quit_saves_the_drawing(app):
    send_stroke(app.osc_port)
    time.sleep(0.5)
    assert app.quit() == 0
    files = svgs(app.data_dir)
    assert len(files) == 1 and files[0].startswith("drawing-")        # no autosave.svg left


def test_a_crash_is_recovered_on_the_next_launch(app):
    send_stroke(app.osc_port)
    assert wait_until(lambda: os.path.exists(os.path.join(app.data_dir, "autosave.svg")))
    app.proc.kill()                                                    # no clean shutdown
    app.proc.wait()
    again = App(app.data_dir)
    try:
        assert again.wait_for("[ui] http://127.0.0.1:")
        assert any(f.startswith("recovered-") for f in svgs(app.data_dir))
        assert not os.path.exists(os.path.join(app.data_dir, "autosave.svg"))
    finally:
        again.quit()


def test_save_svg_builds_from_the_recording(app):
    send_stroke(app.osc_port)
    time.sleep(0.5)
    msgs = send(app, {"type": "save_svg", "filename": "drawing_raw.svg",
                      "layers": {"raw": True, "optimized": False, "effect": False}})
    saved = [m for m in msgs if m["type"] == "saved"]
    assert saved and saved[0]["ok"]
    text = open(os.path.join(app.data_dir, saved[0]["path"]), encoding="utf-8").read()
    assert "<metadata>" in text and 'fill="#fff"' in text


def test_replayed_drawing_is_not_recorded(app):
    rec = {"format": "draw2axi-recording", "version": 1, "strokes": [
        {"canvasWidth": 440.0, "canvasHeight": 956.0, "points": [[i * 0.01, 100 + i, 100, 2.0] for i in range(10)]}]}
    msgs = send(app, {"type": "replay", "recording": rec}, wait=2.0)
    pts = [m for m in msgs if m["type"] == "point"]
    assert len(pts) == 10 and all(p["replay"] for p in pts)
    # What the plotter drew for the replay (the layers) is kept for the page;
    # the replayed input itself is not recorded a second time.
    drawing = hello(app)["drawing"]
    assert not [m for m in drawing if m["type"] in ("point", "pen_up")]
    assert any(m["type"] == "layer" for m in drawing)
    assert not os.path.exists(os.path.join(app.data_dir, "autosave.svg"))


def test_settings_persist_across_launches(app):
    send(app, {"type": "save_settings", "values": {"axi_penPosUp": "55", "axi_model": "2",
                                                    "axi_paperW": "11.69", "axi_paperH": "16.54"}}, wait=0.5)
    assert app.quit() == 0
    again = App(app.data_dir)
    try:
        assert again.wait_for("[ui] http://127.0.0.1:")
        h = hello(again)
        assert h["settings"]["axi_penPosUp"] == "55"
        # Applied at startup, before any page: the A3 paper fits the SE/A3 model.
        assert h["paper"]["model"] == 2 and h["paper"]["width"] == 11.69 and not h["paper"]["clamped"]
    finally:
        again.quit()


# ── in a real browser ────────────────────────────────────────────────────────

def _browser(pw):
    for channel in ("msedge", "chrome"):
        try:
            return pw.chromium.launch(channel=channel, headless=True)
        except Exception:                        # noqa: BLE001 — try the next browser
            pass
    pytest.skip("no Edge or Chrome installed")


def test_reloading_the_page_keeps_the_drawing_and_settings_follow_the_computer(app):
    sync_api = pytest.importorskip("playwright.sync_api")
    send(app, {"type": "save_settings", "values": {"axi_penPosUp": "55"}}, wait=0.3)
    with sync_api.sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{app.ui_port}")
        # The page adopts the computer's settings (one reload), then shows them.
        page.wait_for_function("document.getElementById('pen-up-val').textContent === '55'", timeout=10000)
        page.wait_for_function("document.getElementById('conn-label').textContent === 'live'", timeout=10000)
        send_stroke(app.osc_port, n=25)
        page.wait_for_function("document.getElementById('stroke-count').textContent === '1'", timeout=5000)
        page.reload()
        page.wait_for_function("document.getElementById('pt-count').textContent === '25'", timeout=10000)
        assert page.evaluate("document.getElementById('stroke-count').textContent") == "1"
        page.evaluate("newDrawing()")
        page.wait_for_function("document.getElementById('pt-count').textContent === '0'", timeout=5000)
        assert any(f.startswith("drawing-") for f in svgs(app.data_dir))
        assert not errors, errors
        browser.close()

def test_page_works_in_a_real_browser(app):
    """Headless Edge or Chrome, whichever is installed; skipped if neither."""
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = None
        for channel in ("msedge", "chrome"):
            try:
                browser = pw.chromium.launch(channel=channel, headless=True)
                break
            except Exception:                   # noqa: BLE001 — try the next browser
                pass
        if browser is None:
            pytest.skip("no Edge or Chrome installed")
        errors = []
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{app.ui_port}")
        page.wait_for_function("document.getElementById('conn-label').textContent === 'live'", timeout=10000)
        send_stroke(app.osc_port, n=30)
        page.wait_for_function("document.getElementById('stroke-count').textContent === '1'", timeout=5000)
        assert page.evaluate("document.getElementById('pt-count').textContent") == "30"
        page.evaluate("downloadSVG()")
        page.wait_for_function("document.getElementById('dl-btn').textContent.startsWith('saved')", timeout=5000)
        assert [f for f in os.listdir(app.data_dir) if f.endswith(".svg")]
        assert not errors, errors
        browser.close()
