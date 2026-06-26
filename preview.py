"""
preview.py

Live drawing preview server.
Opens a local web page that displays strokes as they arrive from iDraw OSC.
Communicates with listen_to_idraw.py via a simple WebSocket broadcast.

Run:   called automatically by listen_to_idraw.py
       (or standalone: python preview.py)

Then open:  http://localhost:5000
"""

import asyncio
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets

# ─── configuration ────────────────────────────────────────────────────────────

HTTP_PORT  = 5000        # browser page
WS_PORT    = 5001        # WebSocket feed

# ─── shared broadcast set ─────────────────────────────────────────────────────

_ws_clients: set = set()
_ws_loop = None
flip_x = False     # set by browser via WebSocket; mirrors plotter X axis
flip_y = False     # set by browser via WebSocket; mirrors plotter Y axis

_message_callback = None   # called for browser → Python messages listen_to_idraw registers


def register_message_callback(cb):
    """Let listen_to_idraw.py receive browser control messages (e.g. home)."""
    global _message_callback
    _message_callback = cb


# ─── WebSocket server ─────────────────────────────────────────────────────────

async def _ws_handler(websocket):
    """Accept a browser connection; handle incoming control messages."""
    global flip_x, flip_y
    _ws_clients.add(websocket)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if msg.get("type") == "set_flip_x":
                    flip_x = bool(msg.get("enabled", False))
                elif msg.get("type") == "set_flip_y":
                    flip_y = bool(msg.get("enabled", False))
                elif _message_callback:
                    _message_callback(msg)
            except Exception:
                pass
    finally:
        _ws_clients.discard(websocket)


def broadcast(message: dict):
    """
    Thread-safe broadcast of a dict to all connected browsers.
    Called from the OSC thread in listen_to_idraw.py.
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
  <title>AxiDraw Live Preview</title>
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

    #overlay {
      position: absolute;
      top: 0; left: 0;
      pointer-events: none;
      background: transparent;
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
      min-width: 100%;
    }
    #dl-menu.open { display: flex; }
    #dl-menu button { border-color: #333; }

    /* ── settings panel ─────────────────────────────── */
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
    .settings-section {
      grid-column: 1 / -1;
      text-transform: uppercase;
      color: #444;
      font-size: 9px;
      letter-spacing: 0.15em;
      padding-top: 2px;
    }
    .settings-grid input[type="checkbox"] {
      justify-self: start;
      accent-color: #777;
      cursor: pointer;
      margin: 0;
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

    /* ── path optimization controls ────────────────── */
    .settings-full {
      grid-column: 1 / -1;
    }
    .opt-slider {
      width: 100%;
      accent-color: #777;
      cursor: pointer;
      margin: 2px 0 0;
    }
    .opt-slider-labels {
      display: flex;
      justify-content: space-between;
      font-size: 8px;
      color: #444;
      margin-top: 1px;
    }
    .lag-readout {
      text-align: right;
      color: #444;
      font-size: 9px;
      letter-spacing: 0.04em;
    }
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
      <span class="settings-section">Plotting</span>
      <label>flip H</label>     <input type="checkbox" id="inp-flipx">
      <label>flip V</label>     <input type="checkbox" id="inp-flipy">
      <label>x tilt °</label>   <input type="number" id="inp-x-tilt" min="-10" max="10" step="0.1" value="0">
      <label>y tilt °</label>   <input type="number" id="inp-y-tilt" min="-10" max="10" step="0.1" value="0">
      <hr class="settings-divider">
      <span class="settings-section" style="padding-left:10px">pen</span>
      <div class="settings-full">
        <div style="display:flex;justify-content:space-between;color:#555;font-size:9px;margin:2px 0">
          <span>pen up</span><span id="pen-up-val">60</span>
        </div>
        <div style="display:flex;align-items:center;gap:4px">
          <input type="range" id="inp-pen-up" class="opt-slider" min="0" max="100" step="1" value="60" style="flex:1">
          <button class="settings-btn" style="margin:0;padding:2px 5px;font-size:8px;white-space:nowrap" onclick="testPenUp()">test</button>
        </div>
      </div>
      <div class="settings-full">
        <div style="display:flex;justify-content:space-between;color:#555;font-size:9px;margin:2px 0">
          <span>min pen down</span><span id="pen-down-min-val">40</span>
        </div>
        <div style="display:flex;align-items:center;gap:4px">
          <input type="range" id="inp-pen-down-min" class="opt-slider" min="0" max="100" step="1" value="40" style="flex:1">
          <button class="settings-btn" style="margin:0;padding:2px 5px;font-size:8px;white-space:nowrap" onclick="testPenDownMin()">test</button>
        </div>
      </div>
      <label>variable pressure</label> <input type="checkbox" id="inp-var-pressure">
      <label id="pressure-rate-label">update rate %</label>
      <input type="number" id="inp-pressure-rate" min="0" max="100" step="1" value="100">
      <div class="settings-full" id="pen-down-max-block">
        <div style="display:flex;justify-content:space-between;color:#555;font-size:9px;margin:2px 0">
          <span>max pen down</span><span id="pen-down-max-val">20</span>
        </div>
        <div style="display:flex;align-items:center;gap:4px">
          <input type="range" id="inp-pen-down-max" class="opt-slider" min="0" max="100" step="1" value="20" style="flex:1">
          <button class="settings-btn" style="margin:0;padding:2px 5px;font-size:8px;white-space:nowrap" onclick="testPenDownMax()">test</button>
        </div>
      </div>
      <hr class="settings-divider">
      <span class="settings-section">Path Optimization</span>
      <label>enable</label>     <input type="checkbox" id="inp-opt-en" checked>
      <div class="settings-full" id="opt-scale-block">
        <div style="display:flex;justify-content:space-between;color:#555;font-size:9px;margin:2px 0">
          <span>aggressiveness</span><span id="opt-scale-val">50%</span>
        </div>
        <input type="range" id="inp-opt-scale" class="opt-slider" min="0" max="1" step="0.01" value="0.5">
        <div class="opt-slider-labels"><span>subtle</span><span>strong</span></div>
      </div>
      <div class="settings-full" id="opt-mindist-block">
        <div style="display:flex;justify-content:space-between;color:#555;font-size:9px;margin:2px 0">
          <span>min point dist</span><span id="opt-mindist-val">0.25mm</span>
        </div>
        <input type="range" id="inp-min-dist" class="opt-slider" min="0.001" max="0.05" step="0.001" value="0.01">
        <div class="opt-slider-labels"><span>fine</span><span>coarse</span></div>
      </div>
      <label id="lagthresh-label" for="inp-maxlag-sec">lag threshold</label>
      <input type="number" id="inp-maxlag-sec" min="0.5" max="30" step="0.5" value="3">
      <div class="settings-full lag-readout" id="lag-readout">lag: <span id="lag-display">&#8212;</span>s</div>
      <label>limit lag</label>  <input type="checkbox" id="inp-maxlag-en" checked>
      <hr class="settings-divider">
      <button class="settings-btn" onclick="requestHome()">home axidraw</button>
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
      <button onclick="toggleDownloadMenu()">download</button>
      <div id="dl-menu">
        <button onclick="downloadPNG()">png</button>
        <button onclick="downloadSVG()">svg</button>
      </div>
    </div>
  </div>

<script type="module">
import getStroke from 'https://esm.sh/perfect-freehand';

// Two-canvas setup:
//   #c       — permanent layer; completed strokes are drawn here and never erased
//   #overlay — transient layer; the in-progress stroke is redrawn here on every point
const canvas     = document.getElementById('c');
const overlay    = document.getElementById('overlay');
const ctx        = canvas.getContext('2d');
const overlayCtx = overlay.getContext('2d');

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
let currentPoints = [];           // [[screenX, screenY, pressure], ...]
let currentColor  = 'rgb(255,255,255)';
let currentSize   = 4;            // stroke size in screen pixels
let isInStroke    = false;

// All completed strokes, stored for SVG export
// { type:'stroke', points, color, size } | { type:'dot', x, y, r, color }
let completedStrokes = [];

let ptCount     = 0;
let strokeCount = 0;

// Path optimization settings (mirrored to Python server via WebSocket)
let optEnabled   = true;
let optScale     = 0.5;
let lagThreshold = 3.0;
let limitLag     = true;
let minDist      = 0.01;

// Pen position settings (0-100, direct AxiDraw servo %; lower = pen further down)
let varPressure        = false;
let penPosUp           = 60;
let penDownMin         = 40;
let penDownMax         = 20;
let pressureUpdateRate = 100;

// Canvas tilt compensation (degrees)
let xTiltDeg = 0.0;
let yTiltDeg = 0.0;

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
const inpFlipX    = document.getElementById('inp-flipx');
const inpFlipY    = document.getElementById('inp-flipy');
const inpOptEn    = document.getElementById('inp-opt-en');
const inpOptScale = document.getElementById('inp-opt-scale');
const inpLimitLag     = document.getElementById('inp-maxlag-en');
const inpLagThreshold = document.getElementById('inp-maxlag-sec');
const inpMinDist      = document.getElementById('inp-min-dist');
const inpVarPressure      = document.getElementById('inp-var-pressure');
const inpPressureRate     = document.getElementById('inp-pressure-rate');
const inpPenUp            = document.getElementById('inp-pen-up');
const inpPenDownMin       = document.getElementById('inp-pen-down-min');
const inpPenDownMax       = document.getElementById('inp-pen-down-max');
const inpXTilt            = document.getElementById('inp-x-tilt');
const inpYTilt            = document.getElementById('inp-y-tilt');

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

// ── perfect-freehand rendering ────────────────────────────────────────────────

function buildPath(points, size, last) {
  const outline = getStroke(points, {
    size,
    thinning: 0.5,
    smoothing: 0.5,
    streamline: 0.5,
    simulatePressure: true,
    last,
  });
  if (!outline.length) return null;
  const path = new Path2D();
  path.moveTo(outline[0][0], outline[0][1]);
  for (let i = 1; i < outline.length; i++) path.lineTo(outline[i][0], outline[i][1]);
  path.closePath();
  return path;
}

// Invert only HSL lightness (L → 1-L), keeping hue and saturation intact.
// iDraw OSC sends colours with inverted brightness relative to a dark canvas.
function invertLightness(r, g, b) {
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  const l2 = 1 - l;
  if (max === min) { const v = Math.round(l2 * 255); return `rgb(${v},${v},${v})`; }
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  if      (max === r) h = (g - b) / d + (g < b ? 6 : 0);
  else if (max === g) h = (b - r) / d + 2;
  else                h = (r - g) / d + 4;
  h /= 6;
  const q = l2 < 0.5 ? l2 * (1 + s) : l2 + s - l2 * s;
  const p = 2 * l2 - q;
  function hsl(t) {
    if (t < 0) t += 1; if (t > 1) t -= 1;
    if (t < 1/6) return p + (q - p) * 6 * t;
    if (t < 1/2) return q;
    if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
    return p;
  }
  return `rgb(${Math.round(hsl(h+1/3)*255)},${Math.round(hsl(h)*255)},${Math.round(hsl(h-1/3)*255)})`;
}

function drawDot(c, sx, sy, r, color) {
  c.fillStyle = color;
  c.beginPath();
  c.arc(sx, sy, r, 0, Math.PI * 2);
  c.fill();
}

// ── canvas management ─────────────────────────────────────────────────────────

function applyPreviewSize() {
  if (canvas.width  !== previewW) { canvas.width  = previewW;  overlay.width  = previewW;  }
  if (canvas.height !== previewH) { canvas.height = previewH;  overlay.height = previewH; }
  inpW.value = previewW;
  inpH.value = previewH;
  currentPoints = [];
  isInStroke = false;
}

function clearCanvas() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
  completedStrokes = [];
  currentPoints    = [];
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

function downloadPNG() {
  const exp = document.createElement('canvas');
  exp.width = canvas.width; exp.height = canvas.height;
  const ec = exp.getContext('2d');
  ec.fillStyle = '#000';
  ec.fillRect(0, 0, exp.width, exp.height);
  ec.drawImage(canvas, 0, 0);
  ec.drawImage(overlay, 0, 0);  // include any in-progress stroke
  const a = Object.assign(document.createElement('a'), { href: exp.toDataURL('image/png'), download: 'drawing.png' });
  a.click();
  document.getElementById('dl-menu').classList.remove('open');
}

function downloadSVG() {
  const w = canvas.width, h = canvas.height;
  // Include any in-progress stroke not yet committed via pen_up (mirrors what PNG does via overlay)
  const allStrokes = [...completedStrokes];
  if (currentPoints.length === 1) {
    const [sx, sy] = currentPoints[0];
    allStrokes.push({ type: 'dot', x: sx, y: sy, r: currentSize / 2, color: currentColor });
  } else if (currentPoints.length > 1) {
    allStrokes.push({ type: 'stroke', points: currentPoints, color: currentColor, size: currentSize });
  }
  const parts = allStrokes.map(s => {
    if (s.type === 'dot') {
      return `  <circle cx="${s.x.toFixed(1)}" cy="${s.y.toFixed(1)}" r="${s.r.toFixed(1)}" fill="${s.color}"/>`;
    }
    const outline = getStroke(s.points, { size: s.size, thinning: 0.5, smoothing: 0.5, streamline: 0.5, last: true });
    if (!outline.length) return '';
    const d = outline.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ') + 'Z';
    return `  <path d="${d}" fill="${s.color}"/>`;
  });
  const svg = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">`,
    `  <rect width="${w}" height="${h}" fill="#000"/>`,
    ...parts,
    `</svg>`
  ].join('\\n');
  const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
  const a = Object.assign(document.createElement('a'), { href: url, download: 'drawing.svg' });
  a.click();
  URL.revokeObjectURL(url);
  document.getElementById('dl-menu').classList.remove('open');
}

// ── persistence ───────────────────────────────────────────────────────────────

const _STORAGE_KEYS = [
  'axi_previewW','axi_previewH','axi_previewSizeManual',
  'axi_originX','axi_originY',
  'axi_flipX','axi_flipY',
  'axi_optEnabled','axi_optScale','axi_lagThreshold','axi_limitLag','axi_minDist',
  'axi_varPressure','axi_penPosUp','axi_penDownMin','axi_penDownMax','axi_pressureUpdateRate',
  'axi_xTilt','axi_yTilt',
];

function saveSettings() {
  if (previewSizeManual) {
    localStorage.setItem('axi_previewW', previewW);
    localStorage.setItem('axi_previewH', previewH);
    localStorage.setItem('axi_previewSizeManual', '1');
  } else {
    localStorage.removeItem('axi_previewW');
    localStorage.removeItem('axi_previewH');
    localStorage.removeItem('axi_previewSizeManual');
  }
  localStorage.setItem('axi_originX', originX);
  localStorage.setItem('axi_originY', originY);
  localStorage.setItem('axi_flipX', inpFlipX.checked ? '1' : '0');
  localStorage.setItem('axi_flipY', inpFlipY.checked ? '1' : '0');
  localStorage.setItem('axi_optEnabled',   optEnabled ? '1' : '0');
  localStorage.setItem('axi_optScale',     optScale);
  localStorage.setItem('axi_lagThreshold', lagThreshold);
  localStorage.setItem('axi_limitLag',     limitLag ? '1' : '0');
  localStorage.setItem('axi_minDist',      minDist);
  localStorage.setItem('axi_varPressure',        varPressure ? '1' : '0');
  localStorage.setItem('axi_penPosUp',           penPosUp);
  localStorage.setItem('axi_penDownMin',         penDownMin);
  localStorage.setItem('axi_penDownMax',         penDownMax);
  localStorage.setItem('axi_pressureUpdateRate', pressureUpdateRate);
  localStorage.setItem('axi_xTilt',             xTiltDeg);
  localStorage.setItem('axi_yTilt',             yTiltDeg);
}

function loadSettings() {
  if (localStorage.getItem('axi_previewSizeManual')) {
    previewSizeManual = true;
    const w = parseInt(localStorage.getItem('axi_previewW'));
    const h = parseInt(localStorage.getItem('axi_previewH'));
    if (w > 0) previewW = w;
    if (h > 0) previewH = h;
  }
  const ox = parseFloat(localStorage.getItem('axi_originX'));
  const oy = parseFloat(localStorage.getItem('axi_originY'));
  if (!isNaN(ox)) originX = ox;
  if (!isNaN(oy)) originY = oy;
  const fx = localStorage.getItem('axi_flipX');
  const fy = localStorage.getItem('axi_flipY');
  if (fx !== null) inpFlipX.checked = fx === '1';
  if (fy !== null) inpFlipY.checked = fy === '1';
  const oe  = localStorage.getItem('axi_optEnabled');
  const os  = localStorage.getItem('axi_optScale');
  const lt  = localStorage.getItem('axi_lagThreshold');
  const ll  = localStorage.getItem('axi_limitLag');
  if (oe !== null) { optEnabled   = oe === '1'; inpOptEn.checked       = optEnabled; }
  if (os !== null) { optScale     = parseFloat(os) || 0.5; inpOptScale.value   = optScale; }
  if (lt !== null) { lagThreshold = parseFloat(lt) || 3.0; inpLagThreshold.value = lagThreshold; }
  if (ll !== null) { limitLag     = ll === '1'; inpLimitLag.checked    = limitLag; }
  const md = localStorage.getItem('axi_minDist');
  if (md !== null) {
    minDist = parseFloat(md) || 0.01;
    inpMinDist.value = minDist;
    document.getElementById('opt-mindist-val').textContent = (minDist * 25.4).toFixed(2) + 'mm';
  }
  const vp = localStorage.getItem('axi_varPressure');
  const pu = localStorage.getItem('axi_penPosUp');
  const pd = localStorage.getItem('axi_penDownMin');
  const pm = localStorage.getItem('axi_penDownMax');
  const pr = localStorage.getItem('axi_pressureUpdateRate');
  if (vp !== null) { varPressure = vp === '1'; inpVarPressure.checked = varPressure; }
  if (pu !== null) { penPosUp   = parseInt(pu) || 60;  inpPenUp.value          = penPosUp;           document.getElementById('pen-up-val').textContent       = penPosUp; }
  if (pd !== null) { penDownMin = parseInt(pd) || 40;  inpPenDownMin.value     = penDownMin;          document.getElementById('pen-down-min-val').textContent = penDownMin; }
  if (pm !== null) { penDownMax = parseInt(pm) || 20;  inpPenDownMax.value     = penDownMax;          document.getElementById('pen-down-max-val').textContent = penDownMax; }
  if (pr !== null) { pressureUpdateRate = parseInt(pr) ?? 100; inpPressureRate.value = pressureUpdateRate; }
  const xt = localStorage.getItem('axi_xTilt');
  const yt = localStorage.getItem('axi_yTilt');
  if (xt !== null) { xTiltDeg = parseFloat(xt) || 0.0; inpXTilt.value = xTiltDeg; }
  if (yt !== null) { yTiltDeg = parseFloat(yt) || 0.0; inpYTilt.value = yTiltDeg; }
}

function resetSettings() {
  previewSizeManual = false;
  previewW = surfaceW; previewH = surfaceH;
  originX = 0; originY = 0;
  inpFlipX.checked = false;
  inpFlipY.checked = false;
  optEnabled   = true;  inpOptEn.checked       = true;
  optScale     = 0.5;   inpOptScale.value       = 0.5;
  lagThreshold = 3.0;   inpLagThreshold.value   = 3.0;
  limitLag     = true;  inpLimitLag.checked     = true;
  minDist      = 0.01;  inpMinDist.value         = 0.01;
  document.getElementById('opt-scale-val').textContent  = '50%';
  document.getElementById('opt-mindist-val').textContent = '0.25mm';
  varPressure        = false; inpVarPressure.checked = false;
  penPosUp           = 60;   inpPenUp.value          = 60;
  penDownMin         = 40;   inpPenDownMin.value      = 40;
  penDownMax         = 20;   inpPenDownMax.value      = 20;
  pressureUpdateRate = 100;  inpPressureRate.value    = 100;
  document.getElementById('pen-up-val').textContent       = 60;
  document.getElementById('pen-down-min-val').textContent = 40;
  document.getElementById('pen-down-max-val').textContent = 20;
  xTiltDeg = 0.0; inpXTilt.value = 0;
  yTiltDeg = 0.0; inpYTilt.value = 0;
  applyPreviewSize();
  inpOX.value = 0; inpOY.value = 0;
  _STORAGE_KEYS.forEach(k => localStorage.removeItem(k));
  updateOptUI();
  updatePenUI();
  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({ type: 'set_flip_x',             enabled: false }));
    _ws.send(JSON.stringify({ type: 'set_flip_y',             enabled: false }));
    _ws.send(JSON.stringify({ type: 'set_opt_enabled',        enabled: true  }));
    _ws.send(JSON.stringify({ type: 'set_opt_scale',          value:   0.5   }));
    _ws.send(JSON.stringify({ type: 'set_lag_threshold',      value:   3.0   }));
    _ws.send(JSON.stringify({ type: 'set_limit_lag',          enabled: true  }));
    _ws.send(JSON.stringify({ type: 'set_min_dist',           value:   0.01  }));
    _ws.send(JSON.stringify({ type: 'set_variable_pressure',  enabled: false }));
    _ws.send(JSON.stringify({ type: 'set_pen_up_pos',         value:   60    }));
    _ws.send(JSON.stringify({ type: 'set_pen_down_min',       value:   40    }));
    _ws.send(JSON.stringify({ type: 'set_pen_down_max',       value:   20    }));
    _ws.send(JSON.stringify({ type: 'set_x_tilt',             value:   0.0   }));
    _ws.send(JSON.stringify({ type: 'set_y_tilt',             value:   0.0   }));
  }
}

function updateOptUI() {
  const scaleBlock     = document.getElementById('opt-scale-block');
  const lagReadout     = document.getElementById('lag-readout');
  const lagThreshLabel = document.getElementById('lagthresh-label');
  const opacity = optEnabled ? '1' : '0.35';
  if (scaleBlock)     scaleBlock.style.opacity      = opacity;
  if (lagThreshLabel) lagThreshLabel.style.opacity   = opacity;
  if (inpLagThreshold) inpLagThreshold.style.opacity = opacity;
  if (lagReadout)     lagReadout.style.opacity       = opacity;
  if (inpLimitLag)    inpLimitLag.style.opacity      = opacity;
  const minDistBlock = document.getElementById('opt-mindist-block');
  if (minDistBlock)   minDistBlock.style.opacity      = opacity;
}

function updatePenUI() {
  const opacity = varPressure ? '1' : '0.35';
  const maxBlock    = document.getElementById('pen-down-max-block');
  const rateLabel   = document.getElementById('pressure-rate-label');
  if (maxBlock)  maxBlock.style.opacity  = opacity;
  if (rateLabel) rateLabel.style.opacity = opacity;
  if (inpPressureRate) inpPressureRate.style.opacity = opacity;
}

function testPenUp() {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'pen_test_up' }));
}
function testPenDownMin() {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'pen_test_min' }));
}
function testPenDownMax() {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'pen_test_max' }));
}

function requestHome() {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'home' }));
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
document.getElementById('opt-scale-val').textContent = Math.round(optScale * 100) + '%';
updateOptUI();
updatePenUI();

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
    const { x, y, pressure, r, g, b, drawingWidth, tool } = msg;
    const sx = toScreenX(x);
    const sy = toScreenY(y);

    currentColor = invertLightness(r, g, b);

    // Scale drawingWidth from canvas units to screen pixels
    const lb = letterbox();
    currentSize = Math.max(1, (drawingWidth || 1.5) * lb.drawW / surfaceW);

    if (!isInStroke) {
      isInStroke = true;
      currentPoints = [];
      strokeCount++;
      strokeCntEl.textContent = strokeCount;
    }

    currentPoints.push([sx, sy, pressure]);

    // Redraw in-progress stroke on overlay
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    overlayCtx.fillStyle = currentColor;
    if (currentPoints.length === 1) {
      drawDot(overlayCtx, sx, sy, currentSize / 2, currentColor);
    } else {
      const path = buildPath(currentPoints, currentSize, false);
      if (path) overlayCtx.fill(path);
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
      if (currentPoints.length === 1) {
        const [sx, sy] = currentPoints[0];
        completedStrokes.push({ type: 'dot', x: sx, y: sy, r: currentSize / 2, color: currentColor });
      } else {
        completedStrokes.push({ type: 'stroke', points: [...currentPoints], color: currentColor, size: currentSize });
      }
    }
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    currentPoints = [];
    isInStroke    = false;
    return;
  }

  if (msg.type === 'tool_change') {
    toolLabelEl.textContent = 'tool: ' + msg.tool;
    return;
  }

  if (msg.type === 'lag') {
    const lagEl = document.getElementById('lag-display');
    if (!lagEl) return;
    const sec = msg.seconds;
    lagEl.textContent = sec < 0.05 ? '0.0' : sec.toFixed(1);
    // Colour: neutral → amber → muted red as lag approaches / exceeds threshold
    if (sec < lagThreshold * 0.5) {
      lagEl.style.color = '#555';
    } else if (sec < lagThreshold) {
      lagEl.style.color = '#887733';
    } else {
      lagEl.style.color = '#884433';
    }
    return;
  }
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

inpFlipX.addEventListener('change', e => {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_flip_x', enabled: e.target.checked }));
  saveSettings();
});
inpFlipY.addEventListener('change', e => {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_flip_y', enabled: e.target.checked }));
  saveSettings();
});
inpOptEn.addEventListener('change', e => {
  optEnabled = e.target.checked;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_opt_enabled', enabled: optEnabled }));
  updateOptUI();
  saveSettings();
});
inpOptScale.addEventListener('input', e => {
  optScale = parseFloat(e.target.value);
  document.getElementById('opt-scale-val').textContent = Math.round(optScale * 100) + '%';
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_opt_scale', value: optScale }));
  saveSettings();
});
inpLagThreshold.addEventListener('change', e => {
  lagThreshold = parseFloat(e.target.value) || 3.0;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_lag_threshold', value: lagThreshold }));
  saveSettings();
});
inpLimitLag.addEventListener('change', e => {
  limitLag = e.target.checked;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_limit_lag', enabled: limitLag }));
  saveSettings();
});
inpMinDist.addEventListener('input', e => {
  minDist = parseFloat(e.target.value);
  document.getElementById('opt-mindist-val').textContent = (minDist * 25.4).toFixed(2) + 'mm';
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_min_dist', value: minDist }));
  saveSettings();
});
inpVarPressure.addEventListener('change', e => {
  varPressure = e.target.checked;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_variable_pressure', enabled: varPressure }));
  updatePenUI();
  saveSettings();
});
inpPenUp.addEventListener('input', e => {
  penPosUp = parseInt(e.target.value);
  document.getElementById('pen-up-val').textContent = penPosUp;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_pen_up_pos', value: penPosUp }));
  saveSettings();
});
inpPenDownMin.addEventListener('input', e => {
  penDownMin = parseInt(e.target.value);
  document.getElementById('pen-down-min-val').textContent = penDownMin;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_pen_down_min', value: penDownMin }));
  saveSettings();
});
inpPenDownMax.addEventListener('input', e => {
  penDownMax = parseInt(e.target.value);
  document.getElementById('pen-down-max-val').textContent = penDownMax;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_pen_down_max', value: penDownMax }));
  saveSettings();
});
inpPressureRate.addEventListener('change', e => {
  pressureUpdateRate = Math.max(0, Math.min(100, parseInt(e.target.value) || 0));
  inpPressureRate.value = pressureUpdateRate;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_pressure_update_rate', value: pressureUpdateRate }));
  saveSettings();
});
inpXTilt.addEventListener('change', e => {
  xTiltDeg = parseFloat(e.target.value) || 0.0;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_x_tilt', value: xTiltDeg }));
  saveSettings();
});
inpYTilt.addEventListener('change', e => {
  yTiltDeg = parseFloat(e.target.value) || 0.0;
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_y_tilt', value: yTiltDeg }));
  saveSettings();
});

let _ws = null;

function connect() {
  _ws = new WebSocket('ws://localhost:WS_PORT_PLACEHOLDER');
  _ws.onopen    = () => {
    dot.classList.add('live'); connLabel.textContent = 'live';
    // Re-sync all settings in case server restarted
    _ws.send(JSON.stringify({ type: 'set_flip_x',          enabled: inpFlipX.checked }));
    _ws.send(JSON.stringify({ type: 'set_flip_y',          enabled: inpFlipY.checked }));
    _ws.send(JSON.stringify({ type: 'set_opt_enabled',     enabled: optEnabled }));
    _ws.send(JSON.stringify({ type: 'set_opt_scale',       value:   optScale   }));
    _ws.send(JSON.stringify({ type: 'set_lag_threshold', value:   lagThreshold }));
    _ws.send(JSON.stringify({ type: 'set_limit_lag',     enabled: limitLag     }));
    _ws.send(JSON.stringify({ type: 'set_min_dist',           value:   minDist      }));
    _ws.send(JSON.stringify({ type: 'set_variable_pressure',    enabled: varPressure        }));
    _ws.send(JSON.stringify({ type: 'set_pen_up_pos',           value:   penPosUp           }));
    _ws.send(JSON.stringify({ type: 'set_pen_down_min',         value:   penDownMin         }));
    _ws.send(JSON.stringify({ type: 'set_pen_down_max',         value:   penDownMax         }));
    _ws.send(JSON.stringify({ type: 'set_pressure_update_rate', value:   pressureUpdateRate }));
    _ws.send(JSON.stringify({ type: 'set_x_tilt',               value:   xTiltDeg          }));
    _ws.send(JSON.stringify({ type: 'set_y_tilt',               value:   yTiltDeg          }));
  };
  _ws.onmessage = (e) => { try { handleMessage(JSON.parse(e.data)); } catch(err) { console.error(err); } };
  _ws.onclose   = () => { dot.classList.remove('live'); connLabel.textContent = 'reconnecting…'; setTimeout(connect, 1500); };
}

// Expose functions used by onclick attributes (module scope is not global)
window.clearCanvas        = clearCanvas;
window.toggleDownloadMenu = toggleDownloadMenu;
window.downloadPNG        = downloadPNG;
window.downloadSVG        = downloadSVG;
window.requestHome        = requestHome;
window.resetSettings      = resetSettings;
window.testPenUp          = testPenUp;
window.testPenDownMin     = testPenDownMin;
window.testPenDownMax     = testPenDownMax;

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

    def log_message(self, *_):
        pass


# ─── public start function ────────────────────────────────────────────────────

def start(open_browser: bool = True):
    """
    Starts both the HTTP server and the WebSocket server in background threads.
    Returns immediately so listen_to_idraw.py can continue.
    """
    global _ws_loop

    def _run_ws():
        global _ws_loop
        _ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_ws_loop)

        async def _serve():
            async with websockets.serve(_ws_handler, "localhost", WS_PORT):
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
