"""
preview_remote.py

REMOTE (no-AxiDraw) live drawing preview + recording server.

Opens a local web page that displays strokes as they arrive from iDraw OSC and
lets you download a plottable SVG. Unlike the full preview.py this has no
optimized/effect layers, no plotting/optimization/pen/tilt controls, no effects
panel, and no "plot svg" replay — there is nothing to plot to here. It keeps
exactly one drawing layer (the raw OSC input) and one export: the recording SVG.

Run:   called automatically by listen_to_idraw_remote.py
       (or standalone: python preview_remote.py)

Then open:  http://localhost:5000

Dependencies:
  pip install websockets

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SHARED RECORDING CONTRACT — KEEP IN SYNC WITH THE FULL VERSION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The SVG this page downloads must stay byte-compatible with what the full tool
reads back (preview.py's uploadSVG replay, svg_transform.py, log_to_svg.py). The
`buildRecording()` JSON schema (`draw2axi-recording` v1), the STROKE_THINNING /
widthFor / segWidth geometry, and the surrounding SVG structure below are all
copied verbatim from the full preview.py.

If you change the recording format or how it is rendered in EITHER program, you
MUST update BOTH this remote version and the full version (preview.py plus
listen_to_idraw.py / log_to_svg.py / svg_transform.py). See the matching block
in listen_to_idraw_remote.py.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import base64
import json
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets

# ─── configuration ────────────────────────────────────────────────────────────

HTTP_PORT  = 5000        # browser page
WS_PORT    = 5001        # WebSocket feed

# Downloads from the preview are written here — a folder next to this program —
# rather than the browser's Downloads folder (a web page can't redirect that, so
# it POSTs the bytes to us). Self-contained to the remote folder; move the SVGs
# to the AxiDraw machine to plot them.
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_drawings")

# ─── shared broadcast set ─────────────────────────────────────────────────────

_ws_clients: set = set()
_ws_loop = None


# ─── WebSocket server ─────────────────────────────────────────────────────────

async def _ws_handler(websocket):
    """Accept a browser connection. This version has no browser → Python controls."""
    _ws_clients.add(websocket)
    try:
        async for _raw in websocket:
            pass   # remote preview sends no control messages back to Python
    finally:
        _ws_clients.discard(websocket)


def broadcast(message: dict):
    """
    Thread-safe broadcast of a dict to all connected browsers.
    Called from the OSC thread in listen_to_idraw_remote.py.
    """
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


# ─── HTML page ────────────────────────────────────────────────────────────────

WS_PORT_STR = str(WS_PORT)

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>draw2axi Remote — Recording Preview</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: #1a1a1a;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100vh;
      font-family: "SF Mono", "Fira Code", monospace;
      color: #888;
      gap: 12px;
    }

    #status-bar {
      display: flex;
      gap: 24px;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .stat { display: flex; gap: 6px; align-items: center; }
    .dot  { width: 6px; height: 6px; border-radius: 50%; background: #444; }
    .dot.live { background: #4caf50; box-shadow: 0 0 6px #4caf50; }

    #canvas-wrap {
      position: relative;
      display: inline-block;
      border: 1px solid #333;
    }

    canvas { display: block; cursor: crosshair; }
    #c       { background: #000; }

    /* The in-progress raw stroke overlay sits over the black base. */
    #overlay {
      position: absolute;
      top: 0; left: 0;
      pointer-events: none;
      background: transparent;
      z-index: 1;
    }

    #tool-label {
      font-size: 10px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: #555;
    }

    button {
      background: none;
      border: 1px solid #444;
      color: #888;
      font: inherit;
      font-size: 10px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      padding: 4px 12px;
      cursor: pointer;
    }
    button:hover { border-color: #888; color: #ccc; }

    /* ── download dropdown ──────────────────────────── */
    #dl-wrap { position: relative; display: inline-block; }
    #dl-menu {
      display: none;
      position: absolute;
      bottom: calc(100% + 4px);
      left: 0;
      flex-direction: column;
      gap: 2px;
      background: #111;
      border: 1px solid #444;
      padding: 4px;
      min-width: 128px;
    }
    #dl-menu.open { display: flex; }
    #dl-menu button { border-color: #333; }

    /* ── settings panel ────────────────────────────── */
    #settings {
      position: fixed;
      top: 12px;
      left: 12px;
      z-index: 100;
      font-size: 10px;
      letter-spacing: 0.07em;
      background: rgba(20, 20, 20, 0.92);
      border: 1px solid #2e2e2e;
      padding: 5px 8px;
      color: #666;
    }
    #settings summary {
      cursor: pointer;
      list-style: none;
      outline: none;
      font-size: 16px;
      line-height: 1;
      color: #555;
    }
    #settings summary:hover { color: #888; }
    #settings summary::-webkit-details-marker { display: none; }

    .settings-grid {
      display: grid;
      grid-template-columns: auto 72px;
      gap: 5px 8px;
      margin-top: 7px;
      align-items: center;
    }
    .settings-grid label {
      text-align: right;
      text-transform: uppercase;
      color: #555;
      white-space: nowrap;
    }
    .settings-grid input[type="number"] {
      width: 72px;
      background: #111;
      border: 1px solid #2a2a2a;
      color: #aaa;
      font: inherit;
      padding: 2px 5px;
      text-align: right;
      -moz-appearance: textfield;
    }
    .settings-grid input[type="number"]::-webkit-inner-spin-button,
    .settings-grid input[type="number"]::-webkit-outer-spin-button { -webkit-appearance: none; }
    .settings-grid input[type="number"]:focus { outline: none; border-color: #555; color: #ccc; }
    .settings-divider {
      grid-column: 1 / -1;
      border: none;
      border-top: 1px solid #2a2a2a;
      margin: 2px 0;
    }
    .settings-btn {
      grid-column: 1 / -1;
      background: none;
      border: 1px solid #333;
      color: #555;
      font: inherit;
      font-size: 9px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      padding: 4px 8px;
      cursor: pointer;
      margin-top: 2px;
    }
    .settings-btn:hover { border-color: #666; color: #999; }
  </style>
</head>
<body>

  <details id="settings">
    <summary>&#9776;</summary>
    <div class="settings-grid">
      <label>width px</label>   <input type="number" id="inp-w"  min="50" max="4000" step="1">
      <label>height px</label>  <input type="number" id="inp-h"  min="50" max="4000" step="1">
      <hr class="settings-divider">
      <label>origin x</label>   <input type="number" id="inp-ox" step="1">
      <label>origin y</label>   <input type="number" id="inp-oy" step="1">
      <hr class="settings-divider">
      <button class="settings-btn" onclick="resetSettings()">reset to defaults</button>
    </div>
  </details>

  <div id="status-bar">
    <div class="stat"><div class="dot" id="dot"></div><span id="conn-label">disconnected</span></div>
    <div class="stat">points&nbsp;<span id="pt-count">0</span></div>
    <div class="stat">strokes&nbsp;<span id="stroke-count">0</span></div>
    <div class="stat">pressure&nbsp;<span id="pressure-val">&#8212;</span></div>
  </div>

  <div id="canvas-wrap">
    <canvas id="c"       width="440" height="586"></canvas>
    <canvas id="overlay" width="440" height="586"></canvas>
  </div>

  <div style="display:flex; gap:16px; align-items:center;">
    <span id="tool-label">tool: &#8212;</span>
    <button onclick="clearCanvas()">clear</button>
    <div id="dl-wrap">
      <button id="dl-btn" onclick="toggleDownloadMenu()">download</button>
      <div id="dl-menu">
        <button onclick="downloadPNG()">png</button>
        <button onclick="downloadSVG()">svg</button>
      </div>
    </div>
  </div>

<script type="module">

// Two stacked canvases:
//   #c       — completed raw strokes, drawn here and never erased
//   #overlay — the in-progress raw stroke, redrawn as points arrive
const canvas     = document.getElementById('c');
const overlay    = document.getElementById('overlay');
const ctx        = canvas.getContext('2d');
const overlayCtx = overlay.getContext('2d');

// Single monochrome layer — the drawing's own colour is dropped, matching the
// full tool's raw layer.
const RAW_COLOR = '#8a8a8a';

// Drawing surface dimensions in iDraw canvas coordinate units (from OSC canvas_size)
let surfaceW = 440;
let surfaceH = 586;

// Preview canvas pixel size — auto-updated from OSC until the user edits an input
let previewW = 440;
let previewH = 586;
let previewSizeManual = false;

// Viewport origin: the canvas coordinate that maps to the top-left of the preview
let originX = 0;
let originY = 0;

// Current in-progress stroke
let currentPoints = [];           // [[screenX, screenY, pressure], ...] — for drawing
let currentRaw    = [];           // [[t, canvasX, canvasY, pressureRaw], ...] — for the recording
let currentMeta   = null;         // { tool, drawingWidth, color, canvasWidth, canvasHeight }
let currentColor  = RAW_COLOR;
let currentSize   = 4;            // stroke size in screen pixels
let isInStroke    = false;

// All completed strokes, kept for SVG export.
// `points` are screen pixels (for drawing); `raw` is the untouched OSC input,
// which is what gets written to the SVG recording for replay.
let completedStrokes = [];

let ptCount     = 0;
let strokeCount = 0;

const dot         = document.getElementById('dot');
const connLabel   = document.getElementById('conn-label');
const ptCountEl   = document.getElementById('pt-count');
const strokeCntEl = document.getElementById('stroke-count');
const pressureEl  = document.getElementById('pressure-val');
const toolLabelEl = document.getElementById('tool-label');
const inpW        = document.getElementById('inp-w');
const inpH        = document.getElementById('inp-h');
const inpOX       = document.getElementById('inp-ox');
const inpOY       = document.getElementById('inp-oy');

// ── coordinate system ─────────────────────────────────────────────────────────

function letterbox() {
  const sAspect = surfaceW / surfaceH;
  const pAspect = previewW / previewH;
  let drawW, drawH;
  if (sAspect > pAspect) { drawW = previewW; drawH = previewW / sAspect; }
  else                   { drawH = previewH; drawW = previewH * sAspect; }
  return { drawW, drawH, offsetX: (previewW - drawW) / 2, offsetY: (previewH - drawH) / 2 };
}

function toScreenX(cx) { const lb = letterbox(); return lb.offsetX + (cx - originX) / surfaceW * lb.drawW; }
function toScreenY(cy) { const lb = letterbox(); return lb.offsetY + (cy - originY) / surfaceH * lb.drawH; }

// ── centerline rendering ──────────────────────────────────────────────────────
//
// A stroke is drawn as its centerline: one round-capped segment per pair of
// consecutive points, each segment's width taken from the average pressure of
// its two endpoints. Copied verbatim from the full preview.py so the picture and
// the recording geometry match what the full tool produces.
//   width = size * (1 - thinning + 2 * thinning * pressure)
const STROKE_THINNING = 0.5;

function widthFor(size, pressure) {
  const p = Math.max(0, Math.min(1, pressure));
  return Math.max(0.1, size * (1 - STROKE_THINNING + 2 * STROKE_THINNING * p));
}

function segWidth(size, p1, p2) {
  return widthFor(size, (p1 + p2) / 2);
}

function drawSegment(c, a, b, size, color) {
  c.strokeStyle = color;
  c.lineCap  = 'round';
  c.lineJoin = 'round';
  c.lineWidth = segWidth(size, a[2], b[2]);
  c.beginPath();
  c.moveTo(a[0], a[1]);
  c.lineTo(b[0], b[1]);
  c.stroke();
}

function drawDot(c, sx, sy, r, color) {
  c.fillStyle = color;
  c.beginPath();
  c.arc(sx, sy, r, 0, Math.PI * 2);
  c.fill();
}

// ── canvas management ─────────────────────────────────────────────────────────

function applyPreviewSize() {
  if (canvas.width  !== previewW) { canvas.width  = previewW;  overlay.width  = previewW; }
  if (canvas.height !== previewH) { canvas.height = previewH;  overlay.height = previewH; }
  inpW.value = previewW;
  inpH.value = previewH;
  currentPoints = [];
  currentRaw    = [];
  currentMeta   = null;
  isInStroke    = false;
}

function clearCanvas() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
  completedStrokes = [];
  currentPoints    = [];
  currentRaw       = [];
  currentMeta      = null;
  isInStroke       = false;
  ptCount = strokeCount = 0;
  ptCountEl.textContent   = 0;
  strokeCntEl.textContent = 0;
}

// ── download ──────────────────────────────────────────────────────────────────

function toggleDownloadMenu() {
  document.getElementById('dl-menu').classList.toggle('open');
}
document.addEventListener('click', e => {
  if (!document.getElementById('dl-wrap').contains(e.target))
    document.getElementById('dl-menu').classList.remove('open');
});

// Save bytes to the server's saved_drawings folder. `b64` is the base64 body of
// the file (no data: prefix); the server picks a non-clobbering name.
async function saveToServer(filename, b64) {
  document.getElementById('dl-menu').classList.remove('open');
  try {
    const r = await fetch('/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, b64 }),
    });
    const j = await r.json();
    if (j.ok) flashSaved('saved ' + j.path);
    else      flashSaved('save failed');
  } catch (err) {
    flashSaved('save failed');
  }
}

function flashSaved(text) {
  const btn = document.getElementById('dl-btn');
  const prev = btn.textContent;
  btn.textContent = text;
  setTimeout(() => { btn.textContent = prev; }, 1800);
}

function downloadPNG() {
  const exp = document.createElement('canvas');
  exp.width = canvas.width; exp.height = canvas.height;
  const ec = exp.getContext('2d');
  ec.fillStyle = '#000';
  ec.fillRect(0, 0, exp.width, exp.height);
  ec.drawImage(canvas, 0, 0);
  ec.drawImage(overlay, 0, 0);
  const b64 = exp.toDataURL('image/png').split(',')[1];
  saveToServer('drawing.png', b64);
}

// Every stroke including the one still in progress, so a download mid-stroke
// matches what the PNG export shows via the overlay.
function allStrokes() {
  const out = [...completedStrokes];
  if (currentPoints.length > 0) {
    out.push({ points: currentPoints, raw: currentRaw, meta: currentMeta,
               color: currentColor, size: currentSize });
  }
  return out;
}

function xmlEscape(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// The recording is the drawing's raw OSC input, verbatim — what the full tool's
// replay reads. Numbers go in unrounded so a replayed point is bit-for-bit the
// point that was drawn. SCHEMA MUST MATCH the full preview.py's buildRecording().
function buildRecording(strokes) {
  return {
    format: 'draw2axi-recording',
    version: 1,
    strokes: strokes
      .filter(s => s.raw && s.raw.length && s.meta)
      .map(s => ({
        tool:         s.meta.tool,
        drawingWidth: s.meta.drawingWidth,
        color:        s.meta.color,
        canvasWidth:  s.meta.canvasWidth,
        canvasHeight: s.meta.canvasHeight,
        points:       s.raw,
      })),
  };
}

// The raw strokes as SVG parts. Same width function as the on-screen render, so
// the file matches the preview exactly.
function layerSvgParts(strokes, color) {
  const parts = [];
  for (const s of strokes) {
    const p = s.points;
    if (!p || p.length === 0) continue;
    if (p.length === 1) {
      parts.push(`  <circle cx="${p[0][0].toFixed(2)}" cy="${p[0][1].toFixed(2)}"`
               + ` r="${(widthFor(s.size, p[0][2]) / 2).toFixed(3)}" fill="${color}"/>`);
      continue;
    }
    parts.push(`  <g stroke="${color}" fill="none" stroke-linecap="round">`);
    for (let i = 1; i < p.length; i++) {
      const a = p[i - 1], b = p[i];
      parts.push(`    <path d="M${a[0].toFixed(2)},${a[1].toFixed(2)}`
               + `L${b[0].toFixed(2)},${b[1].toFixed(2)}"`
               + ` stroke-width="${segWidth(s.size, a[2], b[2]).toFixed(3)}"/>`);
    }
    parts.push(`  </g>`);
  }
  return parts;
}

function downloadSVG() {
  const w = canvas.width, h = canvas.height;
  const rawStrokes = allStrokes();

  const parts = layerSvgParts(rawStrokes, RAW_COLOR);
  const meta  = `  <metadata>${xmlEscape(JSON.stringify(buildRecording(rawStrokes)))}</metadata>`;

  const svg = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">`,
    meta,
    `  <rect width="${w}" height="${h}" fill="#000"/>`,
    ...parts,
    `</svg>`
  ].join('\\n');

  // btoa handles Latin-1 only; encode first so non-ASCII survives.
  const b64 = btoa(unescape(encodeURIComponent(svg)));
  saveToServer('drawing.svg', b64);
}

// ── persistence (viewport only) ─────────────────────────────────────────────

const _STORAGE_KEYS = [
  'axir_previewW','axir_previewH','axir_previewSizeManual',
  'axir_originX','axir_originY',
];

function saveSettings() {
  if (previewSizeManual) {
    localStorage.setItem('axir_previewW', previewW);
    localStorage.setItem('axir_previewH', previewH);
    localStorage.setItem('axir_previewSizeManual', '1');
  } else {
    localStorage.removeItem('axir_previewW');
    localStorage.removeItem('axir_previewH');
    localStorage.removeItem('axir_previewSizeManual');
  }
  localStorage.setItem('axir_originX', originX);
  localStorage.setItem('axir_originY', originY);
}

function loadSettings() {
  if (localStorage.getItem('axir_previewSizeManual')) {
    previewSizeManual = true;
    const w = parseInt(localStorage.getItem('axir_previewW'));
    const h = parseInt(localStorage.getItem('axir_previewH'));
    if (w > 0) previewW = w;
    if (h > 0) previewH = h;
  }
  const ox = parseFloat(localStorage.getItem('axir_originX'));
  const oy = parseFloat(localStorage.getItem('axir_originY'));
  if (!isNaN(ox)) originX = ox;
  if (!isNaN(oy)) originY = oy;
}

function resetSettings() {
  previewSizeManual = false;
  previewW = surfaceW; previewH = surfaceH;
  originX = 0; originY = 0;
  applyPreviewSize();
  inpOX.value = 0; inpOY.value = 0;
  _STORAGE_KEYS.forEach(k => localStorage.removeItem(k));
}

// ── settings inputs ───────────────────────────────────────────────────────────

inpW.addEventListener('change', e => {
  const v = parseInt(e.target.value);
  if (v > 0) { previewW = v; previewSizeManual = true; applyPreviewSize(); saveSettings(); }
});
inpH.addEventListener('change', e => {
  const v = parseInt(e.target.value);
  if (v > 0) { previewH = v; previewSizeManual = true; applyPreviewSize(); saveSettings(); }
});
inpOX.addEventListener('change', e => { originX = parseFloat(e.target.value) || 0; saveSettings(); });
inpOY.addEventListener('change', e => { originY = parseFloat(e.target.value) || 0; saveSettings(); });

// Restore saved settings, then apply to canvas and inputs
loadSettings();
applyPreviewSize();
inpOX.value = originX; inpOY.value = originY;

// ── OSC message handler ───────────────────────────────────────────────────────

function handleMessage(msg) {
  if (msg.type === 'canvas_size') {
    surfaceW = msg.width;
    surfaceH = msg.height;
    if (!previewSizeManual && (previewW !== msg.width || previewH !== msg.height)) {
      previewW = msg.width;
      previewH = msg.height;
      applyPreviewSize();
    }
    return;
  }

  if (msg.type === 'point') {
    const { t, x, y, pressure, pressureRaw, r, g, b, a, drawingWidth, tool } = msg;
    const sx = toScreenX(x);
    const sy = toScreenY(y);

    currentColor = RAW_COLOR;

    // Scale drawingWidth from canvas units to screen pixels
    const lb = letterbox();
    currentSize = Math.max(1, (drawingWidth || 1.5) * lb.drawW / surfaceW);

    if (!isInStroke) {
      isInStroke = true;
      currentPoints = [];
      currentRaw    = [];
      // Recorded once per stroke: these do not change while the pen is down.
      currentMeta = {
        tool: tool || 'pen',
        drawingWidth: drawingWidth,
        color: { r, g, b, a },
        canvasWidth: surfaceW,
        canvasHeight: surfaceH,
      };
      strokeCount++;
      strokeCntEl.textContent = strokeCount;
    }

    currentPoints.push([sx, sy, pressure]);
    currentRaw.push([t, x, y, pressureRaw]);

    if (currentPoints.length === 1) {
      drawDot(overlayCtx, sx, sy, widthFor(currentSize, pressure) / 2, currentColor);
    } else {
      drawSegment(overlayCtx, currentPoints[currentPoints.length - 2],
                  currentPoints[currentPoints.length - 1], currentSize, currentColor);
    }

    ptCount++;
    ptCountEl.textContent   = ptCount;
    pressureEl.textContent  = pressure.toFixed(2);
    toolLabelEl.textContent = 'tool: ' + (tool || '—');
    return;
  }

  if (msg.type === 'pen_up') {
    if (currentPoints.length > 0) {
      // Copy overlay pixels exactly — avoids any visual difference from re-rendering
      ctx.drawImage(overlay, 0, 0);
      completedStrokes.push({
        points: [...currentPoints],
        raw:    [...currentRaw],
        meta:   currentMeta,
        color:  currentColor,
        size:   currentSize,
      });
    }
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    currentPoints = [];
    currentRaw    = [];
    currentMeta   = null;
    isInStroke    = false;
    return;
  }

  if (msg.type === 'tool_change') {
    toolLabelEl.textContent = 'tool: ' + msg.tool;
    return;
  }
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

let _ws = null;

function connect() {
  _ws = new WebSocket('ws://localhost:WS_PORT_PLACEHOLDER');
  _ws.onopen    = () => { dot.classList.add('live'); connLabel.textContent = 'live'; };
  _ws.onmessage = (e) => { try { handleMessage(JSON.parse(e.data)); } catch(err) { console.error(err); } };
  _ws.onclose   = () => { dot.classList.remove('live'); connLabel.textContent = 'reconnecting…'; setTimeout(connect, 1500); };
}

// Expose functions used by onclick attributes (module scope is not global)
window.clearCanvas        = clearCanvas;
window.toggleDownloadMenu = toggleDownloadMenu;
window.downloadPNG        = downloadPNG;
window.downloadSVG        = downloadSVG;
window.resetSettings      = resetSettings;

connect();
</script>
</body>
</html>
""".replace("WS_PORT_PLACEHOLDER", WS_PORT_STR)


# ─── HTTP server ──────────────────────────────────────────────────────────────

class _HTMLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def do_POST(self):
        if self.path != "/save":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            name = _save_drawing(payload["filename"], payload["b64"])
            self._send_json(200, {"ok": True, "path": name})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


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
    print(f"[preview] saved  →  {os.path.join(SAVE_DIR, name)}")
    return name


# ─── public start function ────────────────────────────────────────────────────

def start(open_browser: bool = True):
    """
    Starts both the HTTP server and the WebSocket server in background threads.
    Returns immediately so listen_to_idraw_remote.py can continue.
    """
    global _ws_loop

    def _run_ws():
        global _ws_loop
        _ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_ws_loop)

        async def _serve():
            async with websockets.serve(
                _ws_handler, "localhost", WS_PORT, max_size=64 * 1024 * 1024
            ):
                await asyncio.Future()

        _ws_loop.run_until_complete(_serve())

    threading.Thread(target=_run_ws, daemon=True).start()

    def _run_http():
        httpd = HTTPServer(("localhost", HTTP_PORT), _HTMLHandler)
        httpd.serve_forever()

    threading.Thread(target=_run_http, daemon=True).start()

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{HTTP_PORT}")).start()

    print(f"[preview] HTTP  →  http://localhost:{HTTP_PORT}")
    print(f"[preview] WS    →  ws://localhost:{WS_PORT}")


if __name__ == "__main__":
    # Standalone: serve the page without an OSC feed (useful to check the UI).
    start(open_browser=True)
    import time
    while True:
        time.sleep(1)
