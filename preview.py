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
import base64
import json
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets

import postprocess

# ─── configuration ────────────────────────────────────────────────────────────

HTTP_PORT  = 5000        # browser page
WS_PORT    = 5001        # WebSocket feed

# Downloads from the preview are written here — an existing folder next to this
# program — rather than the browser's Downloads folder (the browser controls
# that and a web page can't redirect it, so the page POSTs the bytes to us).
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_drawings")

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

    /* Stacked layers over the black base: raw (grey) at the bottom, then the raw
       in-progress overlay, the optimized path (white), and effects (blue) on top. */
    #overlay, #c-opt, #c-fx {
      position: absolute;
      top: 0; left: 0;
      pointer-events: none;
      background: transparent;
    }
    #overlay { z-index: 1; }
    #c-opt   { z-index: 2; }
    #c-fx    { z-index: 3; }

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
    #dl-menu { min-width: 128px; }
    .dl-layer {
      display: flex;
      align-items: center;
      gap: 6px;
      color: #999;
      font-size: 10px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      cursor: pointer;
      padding: 2px 2px;
    }
    .dl-layer input { accent-color: #777; cursor: pointer; margin: 0; }
    .dl-sw { width: 10px; height: 10px; border: 1px solid #555; display: inline-block; }
    .dl-div { width: 100%; border: none; border-top: 1px solid #333; margin: 3px 0; }

    /* ── settings panel (shared by the top-left settings and top-right effects) ── */
    #settings, #fx-settings {
      position: fixed;
      top: 12px;
      z-index: 100;
      font-size: 10px;
      letter-spacing: 0.07em;
      background: rgba(20, 20, 20, 0.92);
      border: 1px solid #2e2e2e;
      padding: 5px 8px;
      color: #666;
    }
    #settings    { left: 12px; }
    #fx-settings { right: 12px; }
    #settings summary, #fx-settings summary {
      cursor: pointer;
      list-style: none;
      outline: none;
      font-size: 16px;
      line-height: 1;
      color: #555;
    }
    /* The effects panel opens toward the left so its wider rows stay on-screen. */
    #fx-settings summary { text-align: right; }
    #settings summary:hover, #fx-settings summary:hover { color: #888; }
    #settings summary::-webkit-details-marker,
    #fx-settings summary::-webkit-details-marker { display: none; }

    /* Effect header rows: a checkbox + the effect's name, spanning the grid. */
    .fx-head {
      grid-column: 1 / -1;
      display: flex;
      align-items: center;
      gap: 7px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: #888;
      font-size: 10px;
      padding-top: 2px;
      cursor: pointer;
    }
    .fx-head input[type="checkbox"] { accent-color: #777; cursor: pointer; margin: 0; }
    /* A disabled effect's knobs dim but stay visible/editable. */
    .fx-off { opacity: 0.4; }

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

  <details id="fx-settings">
    <summary>&#10022;</summary>
    <div class="settings-grid" id="fx-grid">
      <span class="settings-section">Post-Processing</span>
      <label class="fx-head" title="Plot only the marks the effects add — skip the base line. Re-run a finished drawing (plot svg) to lay effects over an existing base layer.">
        <input type="checkbox" id="fx-effects-only"><span>effects only &mdash; skip base line</span>
      </label>
      <hr class="settings-divider">
      <!-- effect checkboxes + knobs are built here by buildEffectsPanel() -->
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
    <canvas id="c-opt"   width="440" height="586"></canvas>
    <canvas id="c-fx"    width="440" height="586"></canvas>
  </div>

  <div style="display:flex; gap:16px; align-items:center;">
    <span id="tool-label">tool: &#8212;</span>
    <button onclick="clearCanvas()">clear</button>
    <div id="dl-wrap">
      <button id="dl-btn" onclick="toggleDownloadMenu()">download</button>
      <div id="dl-menu">
        <label class="dl-layer"><input type="checkbox" id="dl-raw" checked><span class="dl-sw" style="background:#9a9a9a"></span>raw osc</label>
        <label class="dl-layer"><input type="checkbox" id="dl-opt" checked><span class="dl-sw" style="background:#f2f2f2"></span>optimized</label>
        <label class="dl-layer"><input type="checkbox" id="dl-fx" checked><span class="dl-sw" style="background:#8fbde0"></span>postproc</label>
        <hr class="dl-div">
        <button onclick="downloadPNG()">png</button>
        <button onclick="downloadSVG()">svg</button>
      </div>
    </div>
    <button onclick="uploadSVG()">plot svg</button>
    <input type="file" id="svg-file" accept=".svg,image/svg+xml" style="display:none">
    <span id="replay-status" style="font-size:10px; letter-spacing:0.08em;"></span>
  </div>

<script type="module">

// Layered canvas setup, bottom to top:
//   #c       — raw OSC layer; completed raw strokes, drawn here and never erased
//   #overlay — transient layer; the in-progress raw stroke, redrawn on every point
//   #c-opt   — optimized layer; the post-filter centerline the pen actually follows
//   #c-fx    — effect layer; only the marks the postprocessing chain adds
const canvas     = document.getElementById('c');
const overlay    = document.getElementById('overlay');
const ctx        = canvas.getContext('2d');
const overlayCtx = overlay.getContext('2d');

// Fixed per-layer colours (monochrome + one pale blue) — the raw layer no longer
// uses the drawing's own colour, so all three read as one coherent scheme.
const RAW_COLOR = '#8a8a8a';   // faded grey — raw OSC input
const OPT_COLOR = '#f2f2f2';   // white      — optimized centerline
const FX_COLOR  = '#8fbde0';   // pale blue  — postprocessing additions

// The two server-derived layers. Each accumulates its own strokes (for export)
// and draws incrementally onto its own canvas as commands stream in.
//   stroke: { points: [[screenX, screenY, pressure], ...], size }
const auxLayers = {
  optimized: { ctx: document.getElementById('c-opt').getContext('2d'), color: OPT_COLOR, strokes: [], cur: null },
  effect:    { ctx: document.getElementById('c-fx').getContext('2d'),  color: FX_COLOR,  strokes: [], cur: null },
};

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
let currentRaw    = [];           // [[t, canvasX, canvasY, pressureRaw], ...] — for replay
let currentMeta   = null;         // { tool, drawingWidth, color, canvasWidth, canvasHeight }
let currentColor  = 'rgb(255,255,255)';
let currentSize   = 4;            // stroke size in screen pixels
let isInStroke    = false;
// True when the in-progress stroke is being replayed from a recording rather
// than drawn live. Replayed strokes are animated and painted onto the canvas
// exactly like live ones, but are never pushed into completedStrokes: the
// recording is already the source of the replay, so recording it again would
// double the drawing and every SVG downloaded afterwards. Latched from the
// first point of the stroke so it holds however the pen-up arrives (replay
// thread or watchdog).
let currentIsReplay = false;

// All completed strokes, kept for SVG export.
// { points, raw, meta, color, size }
//
// `points` are screen pixels and exist only to draw with — they follow the
// viewport, so they change meaning if origin/size is edited. `raw` is the
// untouched OSC input and is what gets written to the SVG for replay.
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

// ── stroke rendering ──────────────────────────────────────────────────────────

// ── centerline rendering ──────────────────────────────────────────────────────
//
// A stroke is drawn as its centerline: one round-capped segment per pair of
// consecutive points, each segment's width taken from the average pressure of
// its two endpoints. The geometry on screen and in the SVG is therefore the pen
// path itself, not an outline around it.
//
// thinning sets how strongly pressure drives width. Keeping perfect-freehand's
// formula so widths stay familiar:
//   width = size * (1 - thinning + 2 * thinning * pressure)
// At 0.5 a stroke runs from 0.5x size at zero pressure to 1.5x at full press.
// Raise it toward 1 for a wider range, drop to 0 for uniform width.
const STROKE_THINNING = 0.5;

function widthFor(size, pressure) {
  const p = Math.max(0, Math.min(1, pressure));
  return Math.max(0.1, size * (1 - STROKE_THINNING + 2 * STROKE_THINNING * p));
}

// Width of the segment between two points: the average of their pressures.
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

// Redraw a whole stroke (used when repainting the permanent layer).
function drawStroke(c, points, size, color) {
  if (points.length === 1) {
    drawDot(c, points[0][0], points[0][1], widthFor(size, points[0][2]) / 2, color);
    return;
  }
  for (let i = 1; i < points.length; i++) drawSegment(c, points[i - 1], points[i], size, color);
}

// ── server-derived layers (optimized centerline + effect additions) ─────────────
//
// The Python server broadcasts the post-filter plot command stream (layer
// "optimized") and whatever the effect chain adds on top (layer "effect"), both
// already converted back to canvas pixels. We accumulate strokes per layer and
// draw them incrementally, with the exact same width function as the raw layer
// and the SVG export, so every layer's on-screen width matches its export.

function finalizeAux(L) {
  if (L.cur && L.cur.points.length > 0) L.strokes.push(L.cur);
  L.cur = null;
}

// Close any open in-progress strokes — called before an export reads them.
function finalizeAllAux() {
  for (const name in auxLayers) finalizeAux(auxLayers[name]);
}

function handleLayer(msg) {
  const L = auxLayers[msg.layer];
  if (!L) return;

  // A new optimized stroke begins (moveto): also close the effect layer's open
  // stroke, so the previous stroke's added marks don't bridge into this one.
  if (msg.layer === 'optimized' && msg.kind === 'moveto') finalizeAux(auxLayers.effect);

  if (msg.kind === 'penup' || msg.kind === 'dot_dwell') { finalizeAux(L); return; }

  // moveto / pendown / lineto all carry a canvas-space position.
  const sx = toScreenX(msg.x);
  const sy = toScreenY(msg.y);
  const lb = letterbox();
  const size = Math.max(1, (msg.drawingWidth || 1.5) * lb.drawW / (msg.canvasWidth || surfaceW));

  // moveto is a pen-up travel to the stroke start: open a fresh stroke, no ink.
  if (msg.kind === 'moveto') { finalizeAux(L); L.cur = { points: [], size }; return; }

  // pendown / lineto deposit ink.
  if (!L.cur) L.cur = { points: [], size };
  L.cur.size = size;
  const pts = L.cur.points;
  pts.push([sx, sy, msg.pressure]);
  if (pts.length === 1) drawDot(L.ctx, sx, sy, widthFor(size, msg.pressure) / 2, L.color);
  else                  drawSegment(L.ctx, pts[pts.length - 2], pts[pts.length - 1], size, L.color);
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
  const auxCanvases = Object.values(auxLayers).map(L => L.ctx.canvas);
  if (canvas.width  !== previewW) { canvas.width  = previewW;  overlay.width  = previewW;  auxCanvases.forEach(c => c.width  = previewW); }
  if (canvas.height !== previewH) { canvas.height = previewH;  overlay.height = previewH;  auxCanvases.forEach(c => c.height = previewH); }
  inpW.value = previewW;
  inpH.value = previewH;
  currentPoints = [];
  currentRaw    = [];
  currentMeta   = null;
  isInStroke = false;
  currentIsReplay = false;
}

function clearCanvas() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
  completedStrokes = [];
  currentPoints    = [];
  currentRaw       = [];
  currentMeta      = null;
  isInStroke       = false;
  currentIsReplay  = false;
  for (const name in auxLayers) {
    const L = auxLayers[name];
    L.ctx.clearRect(0, 0, L.ctx.canvas.width, L.ctx.canvas.height);
    L.strokes = [];
    L.cur = null;
  }
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

// Save bytes to the server's saved_drawings folder. `b64` is the base64 body
// of the file (no data: prefix); the server picks a non-clobbering name.
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

// The download button has no browser dialog anymore, so briefly report the
// result in its label.
function flashSaved(text) {
  const btn = document.getElementById('dl-btn');
  const prev = btn.textContent;
  btn.textContent = text;
  setTimeout(() => { btn.textContent = prev; }, 1800);
}

// Which of the three layers the download tickboxes have selected.
function selectedLayers() {
  return {
    raw:       document.getElementById('dl-raw').checked,
    optimized: document.getElementById('dl-opt').checked,
    effect:    document.getElementById('dl-fx').checked,
  };
}

// Filename tagged with the chosen layers, e.g. drawing_raw-fx.svg / drawing_fx.png.
function layerFilename(ext, sel) {
  const tags = [];
  if (sel.raw)       tags.push('raw');
  if (sel.optimized) tags.push('opt');
  if (sel.effect)    tags.push('fx');
  return 'drawing_' + (tags.join('-') || 'none') + '.' + ext;
}

function downloadPNG() {
  const sel = selectedLayers();
  const exp = document.createElement('canvas');
  exp.width = canvas.width; exp.height = canvas.height;
  const ec = exp.getContext('2d');
  ec.fillStyle = '#000';
  ec.fillRect(0, 0, exp.width, exp.height);
  // Composite selected layers bottom-to-top, matching the on-screen stack.
  if (sel.raw)       { ec.drawImage(canvas, 0, 0); ec.drawImage(overlay, 0, 0); }
  if (sel.optimized) ec.drawImage(auxLayers.optimized.ctx.canvas, 0, 0);
  if (sel.effect)    ec.drawImage(auxLayers.effect.ctx.canvas, 0, 0);
  const b64 = exp.toDataURL('image/png').split(',')[1];
  saveToServer(layerFilename('png', sel), b64);
}

// Every stroke including the one still in progress, so a download mid-stroke
// matches what the PNG export shows via the overlay.
function allStrokes() {
  const out = [...completedStrokes];
  // A replayed stroke is excluded for the same reason it never reaches
  // completedStrokes — downloading mid-replay must not capture the playback.
  if (currentPoints.length > 0 && !currentIsReplay) {
    out.push({ points: currentPoints, raw: currentRaw, meta: currentMeta,
               color: currentColor, size: currentSize });
  }
  return out;
}

function xmlEscape(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// The recording is the drawing's raw OSC input, verbatim. It is what replay
// reads; the rendered paths below are only there so the file looks right in a
// viewer. Numbers go in unrounded — JSON round-trips a float64 exactly, so a
// replayed point is bit-for-bit the point that was drawn.
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

// One layer's strokes as SVG parts, all in the given colour. Same width function
// as the on-screen render, so the file matches the preview exactly.
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
  finalizeAllAux();                  // close open optimized/effect strokes for export
  const sel = selectedLayers();
  const w = canvas.width, h = canvas.height;
  const rawStrokes = allStrokes();

  // Bottom-to-top: raw grey, optimized white, effect blue — only the selected ones.
  const parts = [];
  if (sel.raw)       parts.push(...layerSvgParts(rawStrokes,                 RAW_COLOR));
  if (sel.optimized) parts.push(...layerSvgParts(auxLayers.optimized.strokes, OPT_COLOR));
  if (sel.effect)    parts.push(...layerSvgParts(auxLayers.effect.strokes,    FX_COLOR));

  // The replay recording is the raw OSC input, so it only belongs in files that
  // include the raw layer; an optimized/effect-only export is a plain drawing.
  const meta = sel.raw
    ? [`  <metadata>${xmlEscape(JSON.stringify(buildRecording(rawStrokes)))}</metadata>`]
    : [];

  const svg = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">`,
    ...meta,
    `  <rect width="${w}" height="${h}" fill="#000"/>`,
    ...parts,
    `</svg>`
  ].join('\\n');

  // btoa handles Latin-1 only; encode first so non-ASCII survives.
  const b64 = btoa(unescape(encodeURIComponent(svg)));
  saveToServer(layerFilename('svg', sel), b64);
}

// ── replay: upload an SVG and plot it ─────────────────────────────────────────

function uploadSVG() {
  document.getElementById('svg-file').click();
}

async function handleSVGFile(e) {
  const file = e.target.files[0];
  e.target.value = '';            // let the same file be picked again
  if (!file) return;

  let rec;
  try {
    const doc = new DOMParser().parseFromString(await file.text(), 'image/svg+xml');
    if (doc.querySelector('parsererror')) throw new Error('not valid SVG');
    const md = doc.querySelector('metadata');
    if (!md || !md.textContent.trim()) throw new Error('no recording inside');
    rec = JSON.parse(md.textContent);
  } catch (err) {
    setReplayStatus('not a draw2axi SVG', true);
    return;
  }
  if (rec.format !== 'draw2axi-recording') {
    setReplayStatus('not a draw2axi SVG', true);
    return;
  }
  if (!_ws || _ws.readyState !== WebSocket.OPEN) {
    setReplayStatus('not connected', true);
    return;
  }
  const n = (rec.strokes || []).reduce((a, s) => a + (s.points || []).length, 0);
  _ws.send(JSON.stringify({ type: 'replay', recording: rec }));
  setReplayStatus(`plotting ${rec.strokes.length} strokes / ${n} pts`, false);
}

function setReplayStatus(text, isError) {
  const el = document.getElementById('replay-status');
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? '#884433' : '#557755';
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.textContent = ''; }, 6000);
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
  resetEffects();
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

// ── post-processing effects panel ───────────────────────────────────────────────
// Built from EFFECT_SPECS, injected by preview.py from postprocess.effect_specs()
// — so adding an effect (or a knob) in postprocess.py automatically grows the UI
// here with no edits to this file.
const EFFECT_SPECS = EFFECT_SPECS_PLACEHOLDER;

// Live enabled/knob state, seeded from the spec defaults then overridden by
// anything saved in localStorage. This is the source of truth the browser
// re-sends on every (re)connect, exactly like the settings panel does.
const fxState = {};

function fxEnKey(name)      { return 'axi_fx_en_' + name; }
function fxParamKey(name, a){ return 'axi_fx_p_' + name + '_' + a; }

function sendEffectEnabled(name, enabled) {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_effect_enabled', name, enabled }));
}
function sendEffectParam(name, attr, value) {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_effect_param', name, attr, value }));
}

// Re-send the whole effects state — called from connect()'s onopen so a server
// (re)start picks up whatever the panel currently shows.
function syncEffects() {
  for (const spec of EFFECT_SPECS) {
    const st = fxState[spec.name];
    if (!st) continue;
    sendEffectEnabled(spec.name, st.enabled);
    for (const p of spec.params) sendEffectParam(spec.name, p.attr, st.params[p.attr]);
  }
  sendEffectsOnly(fxOnlyCb.checked);
}

// "Effects only" plots just the postprocessing marks (no base line).
const fxOnlyCb = document.getElementById('fx-effects-only');
function sendEffectsOnly(v) {
  if (_ws && _ws.readyState === WebSocket.OPEN)
    _ws.send(JSON.stringify({ type: 'set_effects_only', enabled: v }));
}
fxOnlyCb.checked = localStorage.getItem('axi_fx_only') === '1';
fxOnlyCb.addEventListener('change', () => {
  localStorage.setItem('axi_fx_only', fxOnlyCb.checked ? '1' : '0');
  sendEffectsOnly(fxOnlyCb.checked);
});

function buildEffectsPanel() {
  const grid = document.getElementById('fx-grid');
  for (const spec of EFFECT_SPECS) {
    const st = { enabled: spec.enabled, params: {}, paramEls: [] };
    fxState[spec.name] = st;

    // Header: checkbox + effect name, spanning the grid.
    const head = document.createElement('label');
    head.className = 'fx-head';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id   = 'fx-' + spec.name;
    const savedEn = localStorage.getItem(fxEnKey(spec.name));
    st.enabled = savedEn === null ? spec.enabled : savedEn === '1';
    cb.checked = st.enabled;
    const nameSpan = document.createElement('span');
    nameSpan.textContent = spec.label;
    head.appendChild(cb);
    head.appendChild(nameSpan);
    grid.appendChild(head);

    const reflect = () => st.paramEls.forEach(el => el.classList.toggle('fx-off', !st.enabled));

    cb.addEventListener('change', () => {
      st.enabled = cb.checked;
      localStorage.setItem(fxEnKey(spec.name), cb.checked ? '1' : '0');
      reflect();
      sendEffectEnabled(spec.name, cb.checked);
    });

    // One number input per tunable knob.
    for (const p of spec.params) {
      const lab = document.createElement('label');
      lab.textContent = p.label;
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.id   = 'fxp-' + spec.name + '-' + p.attr;
      inp.min  = p.min; inp.max = p.max; inp.step = p.step;
      const savedP = localStorage.getItem(fxParamKey(spec.name, p.attr));
      const val = savedP === null ? p.default : parseFloat(savedP);
      inp.value = val;
      st.params[p.attr] = val;
      grid.appendChild(lab);
      grid.appendChild(inp);
      st.paramEls.push(lab, inp);

      inp.addEventListener('change', () => {
        let v = parseFloat(inp.value);
        if (isNaN(v)) { inp.value = st.params[p.attr]; return; }
        v = Math.max(p.min, Math.min(p.max, v));
        if (p.int) v = Math.round(v);
        inp.value = v;
        st.params[p.attr] = v;
        localStorage.setItem(fxParamKey(spec.name, p.attr), v);
        sendEffectParam(spec.name, p.attr, v);
      });
    }

    reflect();
  }
}

// Restore the effects panel to spec defaults; wired into resetSettings().
function resetEffects() {
  for (const spec of EFFECT_SPECS) {
    const st = fxState[spec.name];
    if (!st) continue;
    st.enabled = spec.enabled;
    const cb = document.getElementById('fx-' + spec.name);
    if (cb) cb.checked = spec.enabled;
    localStorage.removeItem(fxEnKey(spec.name));
    for (const p of spec.params) {
      st.params[p.attr] = p.default;
      const inp = document.getElementById('fxp-' + spec.name + '-' + p.attr);
      if (inp) inp.value = p.default;
      localStorage.removeItem(fxParamKey(spec.name, p.attr));
    }
    st.paramEls.forEach(el => el.classList.toggle('fx-off', !st.enabled));
  }
  fxOnlyCb.checked = false;
  localStorage.removeItem('axi_fx_only');
  syncEffects();
}

buildEffectsPanel();

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

    // Raw OSC layer is always faded grey now (the drawing's own colour is dropped
    // in favour of the three-layer monochrome scheme).
    currentColor = RAW_COLOR;

    // Scale drawingWidth from canvas units to screen pixels
    const lb = letterbox();
    currentSize = Math.max(1, (drawingWidth || 1.5) * lb.drawW / surfaceW);

    if (!isInStroke) {
      isInStroke = true;
      currentPoints = [];
      currentRaw    = [];
      // Latched once per stroke: the pen-up may be sent by a different thread
      // than the points, so it cannot be trusted to carry the flag itself.
      currentIsReplay = !!msg.replay;
      // Recorded once per stroke: these do not change while the pen is down.
      currentMeta = {
        tool: tool || 'pen',
        drawingWidth: drawingWidth,
        color: { r, g, b, a },
        canvasWidth: surfaceW,
        canvasHeight: surfaceH,
      };
      // The counters describe the recording, so a replay must not inflate them
      // — replay progress has its own readout (#replay-status).
      if (!currentIsReplay) {
        strokeCount++;
        strokeCntEl.textContent = strokeCount;
      }
    }

    // Screen points are needed either way to animate the stroke; the raw OSC
    // points are the recording, so a replay contributes none.
    currentPoints.push([sx, sy, pressure]);
    if (!currentIsReplay) currentRaw.push([t, x, y, pressureRaw]);

    // Centerline segments are final once drawn, so extend the overlay rather
    // than repainting the whole in-progress stroke on every point.
    if (currentPoints.length === 1) {
      drawDot(overlayCtx, sx, sy, widthFor(currentSize, pressure) / 2, currentColor);
    } else {
      drawSegment(overlayCtx, currentPoints[currentPoints.length - 2],
                  currentPoints[currentPoints.length - 1], currentSize, currentColor);
    }

    if (!currentIsReplay) {
      ptCount++;
      ptCountEl.textContent = ptCount;
    }
    pressureEl.textContent  = pressure.toFixed(2);
    toolLabelEl.textContent = 'tool: ' + (tool || '—');
    return;
  }

  if (msg.type === 'pen_up') {
    if (currentPoints.length > 0) {
      // Copy overlay pixels exactly — avoids any visual difference from re-rendering.
      // Replayed strokes are painted here too, so playback stays visible; only the
      // recording bookkeeping below is skipped for them.
      ctx.drawImage(overlay, 0, 0);
      if (!currentIsReplay) {
        completedStrokes.push({
          points: [...currentPoints],
          raw:    [...currentRaw],
          meta:   currentMeta,
          color:  currentColor,
          size:   currentSize,
        });
      }
    }
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    currentPoints = [];
    currentRaw    = [];
    currentMeta   = null;
    isInStroke    = false;
    currentIsReplay = false;
    return;
  }

  if (msg.type === 'layer') {
    handleLayer(msg);
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
    syncEffects();   // push the post-processing panel's state too
  };
  _ws.onmessage = (e) => { try { handleMessage(JSON.parse(e.data)); } catch(err) { console.error(err); } };
  _ws.onclose   = () => { dot.classList.remove('live'); connLabel.textContent = 'reconnecting…'; setTimeout(connect, 1500); };
}

// Expose functions used by onclick attributes (module scope is not global)
window.clearCanvas        = clearCanvas;
window.toggleDownloadMenu = toggleDownloadMenu;
window.downloadPNG        = downloadPNG;
window.downloadSVG        = downloadSVG;
window.uploadSVG          = uploadSVG;
document.getElementById('svg-file').addEventListener('change', handleSVGFile);
window.requestHome        = requestHome;
window.resetSettings      = resetSettings;
window.testPenUp          = testPenUp;
window.testPenDownMin     = testPenDownMin;
window.testPenDownMax     = testPenDownMax;

connect();
</script>
</body>
</html>
""".replace("WS_PORT_PLACEHOLDER", WS_PORT_STR) \
   .replace("EFFECT_SPECS_PLACEHOLDER", json.dumps(postprocess.effect_specs()))


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
    Returns immediately so listen_to_idraw.py can continue.
    """
    global _ws_loop

    def _run_ws():
        global _ws_loop
        _ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_ws_loop)

        async def _serve():
            # max_size: an uploaded SVG replay arrives as one frame carrying every
            # point of a drawing, which easily passes the 1 MB default.
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
