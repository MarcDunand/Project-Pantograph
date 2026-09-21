// Pantograph — the page.
//
// Everything lives on the computer (the canvas, the settings, the plotter);
// this page shows it and sends commands. When it connects, the engine sends a
// 'hello' with everything needed to catch up, so reloading or closing the page
// loses nothing.
//
// There is one canvas. A drawing opened from a file is shown in a preview
// panel until it's imported, and importing plots it onto that same canvas.
//
// The paper is the anchor: the drawing and the machine are two rectangles
// placed on it (see layout.py, mirrored in "the layout" below).
//
// Rules that keep a future native window (pywebview) easy: no alert(),
// confirm(), window.open() or <a download>; everything comes over the socket.

const $ = id => document.getElementById(id);

// ── connection ──────────────────────────────────────────────────────────────

let ws = null;
let quitting = false;

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(msg)); return true; }
  return false;
}

function connect() {
  // Same host and port the page came from: one server serves both.
  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onopen = () => setConn(true);
  ws.onmessage = e => { try { handleMessage(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    if (quitting) { $('stopped').hidden = false; return; }
    setConn(false);
    setTimeout(connect, 1500);
  };
}

function setConn(up) {
  const el = $('conn-label');
  el.textContent = up ? 'live' : 'reconnecting…';
  el.classList.toggle('down', !up);
}

// ── settings storage ────────────────────────────────────────────────────────
// The settings live in a file on the computer, which is the source of truth;
// the browser's storage is only this page's copy. Every change is pushed to the
// computer (debounced), and when the page connects the computer's copy wins
// (see 'hello').

const store = {
  get:    k      => localStorage.getItem(k),
  set:    (k, v) => { localStorage.setItem(k, String(v)); pushSettingsSoon(); },
  remove: k      => { localStorage.removeItem(k); pushSettingsSoon(); },
};
function localSettings() {
  const v = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k.startsWith('axi_')) v[k] = localStorage.getItem(k);
  }
  return v;
}
let pushTimer = null;
function pushSettingsSoon() {
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => send({ type: 'save_settings', values: localSettings() }), 300);
}
function sameSettings(a, b) {
  const ka = Object.keys(a).sort(), kb = Object.keys(b).sort();
  return ka.length === kb.length && ka.every((k, i) => k === kb[i] && a[k] === b[k]);
}

// ── small UI helpers ────────────────────────────────────────────────────────

let toastTimer = null;
function toast(text, bad = false) {
  const el = $('toast');
  el.textContent = text;
  el.classList.toggle('bad', bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3500);
}

// An in-page question (confirm() isn't allowed; see the top). Resolves to
// 'ok', 'other' or null, so one dialog can offer Save / Discard / Cancel.
function ask({ title, body, ok = 'OK', other = null, danger = false }) {
  return new Promise(resolve => {
    $('dialog-title').textContent = title;
    $('dialog-body').textContent = body;
    const okBtn = $('dialog-ok'), otherBtn = $('dialog-other');
    okBtn.textContent = ok;
    okBtn.className = 'btn ' + (danger ? 'danger solid' : 'primary');
    otherBtn.hidden = !other;
    if (other) { otherBtn.textContent = other; otherBtn.className = 'btn' + (danger ? '' : ' danger'); }
    $('dialog').hidden = false;
    okBtn.focus();
    const done = v => {
      $('dialog').hidden = true;
      okBtn.onclick = otherBtn.onclick = $('dialog-cancel').onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(v);
    };
    const onKey = e => { if (e.key === 'Escape') done(null); };
    document.addEventListener('keydown', onKey);
    okBtn.onclick = () => done('ok');
    otherBtn.onclick = () => done('other');
    $('dialog-cancel').onclick = () => done(null);
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older webviews: the textarea fallback.
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

let saveTimer = null;
function saveStatus(text, kind = '') {
  const el = $('save-status');
  el.textContent = text;
  el.className = kind;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { el.textContent = ''; el.className = ''; }, 4000);
}

const IS_WINDOWS = /Windows/i.test(navigator.userAgent);
document.querySelectorAll('.os-win').forEach(el => { el.hidden = !IS_WINDOWS; });
document.querySelectorAll('.os-mac').forEach(el => { el.hidden = IS_WINDOWS; });

// ── the layout ──────────────────────────────────────────────────────────────
//
// The same arithmetic as layout.py, so the page draws exactly where the pen
// goes. All in inches on the paper, whose top-left corner is (0, 0).

let paper  = { width: 8.5, height: 11 };
let travel = { long: 11.81, short: 8.58 };
let canvasSize = { width: 440, height: 956 };
let lay = { auto: true, ipad: { cx: 4.25, cy: 5.5, w: 5.06, h: 11, rot: 0 },
            axi: { cx: 4.25, cy: 5.5, rot: 90 } };
let modelName = '';

function rotate(x, y, deg) {
  const a = deg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
  return [x * c - y * s, x * s + y * c];
}
// A point on the tablet → where it lands on the paper, in inches.
function canvasToPaper(x, y) {
  const u = (x / canvasSize.width - 0.5) * lay.ipad.w;
  const v = (y / canvasSize.height - 0.5) * lay.ipad.h;
  const [dx, dy] = rotate(u, v, lay.ipad.rot);
  return [lay.ipad.cx + dx, lay.ipad.cy + dy];
}
function machineToPaper(mx, my) {
  const [dx, dy] = rotate(mx - travel.long / 2, my - travel.short / 2, lay.axi.rot);
  return [lay.axi.cx + dx, lay.axi.cy + dy];
}
function rectCorners(rect, w, h) {
  return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => {
    const [dx, dy] = rotate(sx * w / 2, sy * h / 2, rect.rot);
    return [rect.cx + dx, rect.cy + dy];
  });
}
function outOfReach() {
  const c = canvasSize;
  return [[0, 0], [c.width, 0], [c.width, c.height], [0, c.height]].some(([x, y]) => {
    const [px, py] = canvasToPaper(x, y);
    const [dx, dy] = rotate(px - lay.axi.cx, py - lay.axi.cy, -lay.axi.rot);
    const mx = dx + travel.long / 2, my = dy + travel.short / 2;
    return mx < -0.01 || mx > travel.long + 0.01 || my < -0.01 || my > travel.short + 0.01;
  });
}

// ── the paper on screen ─────────────────────────────────────────────────────
//
// Layers, bottom to top:
//   #c        — the canvas: completed strokes, drawn once and never erased
//   #overlay  — the stroke in progress, extended point by point
//   #c-opt    — the pen's path, drawn as the plotter draws it (orange)
//   #c-fx     — what the effects add, likewise (blue)
//   #c-layout — the two rectangles, and the layout editor's handles
// Strokes are centerlines: one round-capped segment per pair of points, its
// width from the average pressure of its ends (same as the saved SVG).

const canvas     = $('c');
const overlay    = $('overlay');
const ctx        = canvas.getContext('2d');
const overlayCtx = overlay.getContext('2d');
const layoutCanvas = $('c-layout');
const layoutCtx  = layoutCanvas.getContext('2d');

const FX_COLOR   = '#1c7ed6';
const IPAD_COLOR = '#0ca7a0';        // turquoise: the drawing's rectangle
const AXI_COLOR  = '#f76707';        // orange: the machine's rectangle
const OPT_COLOR  = AXI_COLOR;        // the pen's path belongs to the machine
const STROKE_THINNING = 0.5;
const MM_PER_IN = 25.4;
const PX_PER_IN = 96;                // the canvases' own resolution
// The canvases reach past the paper, so a machine bigger than the sheet — and
// the handles for moving it — are still on screen.
const MARGIN_IN = 1.5;

const auxLayers = {
  optimized: { ctx: $('c-opt').getContext('2d'), color: OPT_COLOR, cur: null },
  effect:    { ctx: $('c-fx').getContext('2d'),  color: FX_COLOR,  cur: null },
};

let currentPoints = [];     // [[screenX, screenY, pressure]] of the stroke in progress
let currentColor  = '#000';
let currentSize   = 4;
let isInStroke    = false;
let ptCount = 0, strokeCount = 0;
let canvasEmpty = true, unsaved = false;

const len = inches => inches * PX_PER_IN;                 // a length
const toPx = inches => (inches + MARGIN_IN) * PX_PER_IN;  // a place on the paper
function sheetPx() {
  return [Math.round(len(paper.width + 2 * MARGIN_IN)), Math.round(len(paper.height + 2 * MARGIN_IN))];
}
function widthFor(size, pressure) {
  const p = Math.max(0, Math.min(1, pressure));
  return Math.max(0.1, size * (1 - STROKE_THINNING + 2 * STROKE_THINNING * p));
}
function drawSegment(c, a, b, size, color) {
  c.strokeStyle = color;
  c.lineCap = 'round'; c.lineJoin = 'round';
  c.lineWidth = widthFor(size, (a[2] + b[2]) / 2);
  c.beginPath(); c.moveTo(a[0], a[1]); c.lineTo(b[0], b[1]); c.stroke();
}
function drawDot(c, sx, sy, r, color) {
  c.fillStyle = color;
  c.beginPath(); c.arc(sx, sy, r, 0, Math.PI * 2); c.fill();
}

// The stroke's colour as a grey of the same brightness (as in the saved SVG).
function greyFor(r, g, b) {
  const lum = 0.2126 * (r || 0) + 0.7152 * (g || 0) + 0.0722 * (b || 0);
  const v = Math.round(255 * Math.max(0, Math.min(1, lum)));
  return `rgb(${v},${v},${v})`;
}

// A stroke's width (in the tablet's units) as pixels on the paper.
function sizeOnPaper(widthInCanvasUnits) {
  return Math.max(1, len((widthInCanvasUnits || 1.5) * (lay.ipad.w / canvasSize.width)));
}

function handleLayer(msg) {
  const L = auxLayers[msg.layer];
  if (!L) return;
  // A new pen-path stroke also ends the effect layer's, so marks don't bridge.
  if (msg.layer === 'optimized' && msg.kind === 'moveto') auxLayers.effect.cur = null;
  if (msg.kind === 'penup' || msg.kind === 'dot_dwell') { L.cur = null; return; }
  const sx = toPx(msg.x), sy = toPx(msg.y);          // the engine sends paper inches
  const size = Math.max(1, len(msg.width || 0.02));
  if (msg.kind === 'moveto') { L.cur = { points: [], size }; return; }
  if (!L.cur) L.cur = { points: [], size };
  L.cur.size = size;
  const pts = L.cur.points;
  pts.push([sx, sy, msg.pressure]);
  if (pts.length === 1) drawDot(L.ctx, sx, sy, widthFor(size, msg.pressure) / 2, L.color);
  else drawSegment(L.ctx, pts[pts.length - 2], pts[pts.length - 1], size, L.color);
}

function allCanvases() {
  return [canvas, overlay, ...Object.values(auxLayers).map(L => L.ctx.canvas), layoutCanvas];
}

// The paper's size changed: resize the canvases (which clears them) and ask
// the computer to send the drawing again, so nothing is lost.
function applyPaperSize(refresh = true) {
  const [w, h] = sheetPx();
  if (canvas.width === w && canvas.height === h) { fitSheet(); return; }
  for (const c of allCanvases()) { c.width = w; c.height = h; }
  currentPoints = []; isInStroke = false;
  fitSheet();
  if (refresh) send({ type: 'hello_again' });
}

function clearCanvas() {
  for (const c of allCanvases()) {
    if (c !== layoutCanvas) c.getContext('2d').clearRect(0, 0, c.width, c.height);
  }
  for (const L of Object.values(auxLayers)) L.cur = null;
  currentPoints = []; isInStroke = false;
  ptCount = strokeCount = 0;
  $('pt-count').textContent = 0;
  $('stroke-count').textContent = 0;
  canvasEmpty = true;
  updateCards();
}

// Scale the paper to fit the table, keeping its shape, and draw the rulers.
let sheetScale = 1;
function fitSheet() {
  const table = $('table');
  const availW = table.clientWidth - 60, availH = table.clientHeight - 60;
  if (availW <= 0 || availH <= 0) return;
  sheetScale = Math.min(availW / canvas.width, availH / canvas.height);
  const wrap = $('canvas-wrap');
  const w = Math.max(40, Math.floor(canvas.width * sheetScale));
  const h = Math.max(40, Math.floor(canvas.height * sheetScale));
  wrap.style.width = w + 'px';
  wrap.style.height = h + 'px';
  // The white sheet itself, inside that margin.
  const inset = len(MARGIN_IN) * sheetScale;
  const sheet = $('paper-sheet');
  sheet.style.left = inset + 'px';
  sheet.style.top = inset + 'px';
  sheet.style.width = len(paper.width) * sheetScale + 'px';
  sheet.style.height = len(paper.height) * sheetScale + 'px';
  drawRulers(len(paper.width) * sheetScale, len(paper.height) * sheetScale, inset);
  drawLayout();
}
new ResizeObserver(fitSheet).observe($('table'));

// ── rulers ──────────────────────────────────────────────────────────────────

let units = 'in';
function tickStep() {
  // (minor, major) in inches: eighths and inches, or 5 mm and centimetres.
  return units === 'in' ? [1 / 8, 1] : [5 / MM_PER_IN, 10 / MM_PER_IN];
}
function drawRuler(el, lengthPx, inches, vertical) {
  const dpr = window.devicePixelRatio || 1;
  const thick = vertical ? 36 : 26;
  el.width = Math.round((vertical ? thick : lengthPx) * dpr);
  el.height = Math.round((vertical ? lengthPx : thick) * dpr);
  el.style.width = (vertical ? thick : lengthPx) + 'px';
  el.style.height = (vertical ? lengthPx : thick) + 'px';
  const c = el.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, el.width, el.height);
  const css = getComputedStyle(document.body);
  c.strokeStyle = css.getPropertyValue('--faint').trim() || '#8a939c';
  c.fillStyle = css.getPropertyValue('--muted').trim() || '#5c6670';
  c.font = '10px ' + (css.getPropertyValue('--mono').trim() || 'monospace');
  c.textAlign = vertical ? 'right' : 'center';
  c.textBaseline = vertical ? 'middle' : 'top';
  const [minor, major] = tickStep();
  const perIn = lengthPx / inches;
  for (let at = 0; at <= inches + 1e-9; at += minor) {
    const isMajor = Math.abs(at / major - Math.round(at / major)) < 1e-6;
    const p = at * perIn;
    const len = isMajor ? 9 : 4;
    c.beginPath();
    if (vertical) { c.moveTo(thick - len, p); c.lineTo(thick, p); }
    else          { c.moveTo(p, 0); c.lineTo(p, len); }
    c.stroke();
    if (isMajor) {
      const label = String(Math.round(units === 'in' ? at : at * MM_PER_IN / 10));
      if (vertical) c.fillText(label, thick - 12, Math.min(lengthPx - 6, Math.max(6, p)));
      else c.fillText(label, Math.min(lengthPx - 6, Math.max(6, p)), 11);
    }
  }
}
function drawRulers(w, h, inset) {
  drawRuler($('ruler-x'), w, paper.width, false);
  drawRuler($('ruler-y'), h, paper.height, true);
  // Line the rulers up with the sheet, not with the margin around it.
  $('ruler-x').style.marginLeft = inset + 'px';
  $('ruler-y').style.marginTop = inset + 'px';
  $('unit-toggle').textContent = units;
}
$('unit-toggle').onclick = () => {
  units = units === 'in' ? 'cm' : 'in';
  store.set('axi_units', units);
  fitSheet();
};

// ── the two rectangles, and the layout editor ───────────────────────────────

let editingLayout = false;
let layoutBefore = null;          // to put back on Cancel
let drag = null;                  // {what, mode, ...} while a handle is held

function drawLayout() {
  const c = layoutCtx;
  c.clearRect(0, 0, layoutCanvas.width, layoutCanvas.height);
  const dim = editingLayout ? 1 : 0.45;
  drawRect(c, lay.axi, travel.long, travel.short, AXI_COLOR, dim, 'axi');
  drawRect(c, lay.ipad, lay.ipad.w, lay.ipad.h, IPAD_COLOR, dim, 'ipad');
  if (editingLayout) {
    const warn = outOfReach();
    $('layout-warning').hidden = !warn;
  }
}

function drawRect(c, rect, w, h, color, alpha, which) {
  const pts = rectCorners(rect, w, h).map(([x, y]) => [toPx(x), toPx(y)]);
  const unit = 1 / Math.max(0.2, sheetScale);        // one screen pixel, in canvas units
  c.save();
  c.globalAlpha = alpha;
  c.strokeStyle = color;
  c.lineWidth = unit * (editingLayout ? 1.25 : 1);
  c.setLineDash(editingLayout ? [] : [5 * unit, 4 * unit]);
  c.beginPath();
  pts.forEach(([x, y], i) => (i ? c.lineTo(x, y) : c.moveTo(x, y)));
  c.closePath();
  c.stroke();
  c.setLineDash([]);

  if (which === 'ipad') {
    // The drawing's top edge, so it's clear which way up it goes.
    c.lineWidth = 3 * unit;
    c.beginPath(); c.moveTo(pts[0][0], pts[0][1]); c.lineTo(pts[1][0], pts[1][1]); c.stroke();
  } else {
    drawHomeMark(c, pts[0], color);
  }
  drawRectLabel(c, rect, h, which === 'ipad' ? 'ipad' : 'plotter', color, unit);

  if (editingLayout) {
    drawTurnHandle(c, rect, h, color, unit);
    if (which === 'ipad') {                                     // the resize corner
      const knob = 5 * unit;
      c.fillStyle = color;
      c.fillRect(pts[2][0] - knob, pts[2][1] - knob, knob * 2, knob * 2);
    }
  }
  c.restore();
}

// A quiet label along the bottom edge, turning with the rectangle but never
// growing with it.
function drawRectLabel(c, rect, h, text, color, unit) {
  const [dx, dy] = rotate(0, h / 2, rect.rot);
  c.save();
  c.translate(toPx(rect.cx + dx), toPx(rect.cy + dy));
  c.rotate(rect.rot * Math.PI / 180);
  c.fillStyle = color;
  c.globalAlpha *= 0.85;
  c.font = `${10 * unit}px ${getComputedStyle(document.body).getPropertyValue('--font')}`;
  c.textAlign = 'center';
  c.textBaseline = 'top';
  c.fillText(text, 0, 4 * unit);
  c.restore();
}

// The turn handle: a short stem from the edge, ending in the usual
// double-headed curved arrow.
function drawTurnHandle(c, rect, h, color, unit) {
  const [ex, ey] = rotate(0, -h / 2, rect.rot);               // where the stem leaves the edge
  const [kx, ky] = rotate(0, -h / 2 - 0.4, rect.rot);         // the handle itself
  c.save();
  c.strokeStyle = color;
  c.lineWidth = unit;
  c.beginPath();
  c.moveTo(toPx(rect.cx + ex), toPx(rect.cy + ey));
  c.lineTo(toPx(rect.cx + kx), toPx(rect.cy + ky));
  c.stroke();

  c.translate(toPx(rect.cx + kx), toPx(rect.cy + ky));
  c.rotate(rect.rot * Math.PI / 180);
  const r = 7 * unit;
  c.lineWidth = 1.75 * unit;
  c.lineCap = 'butt';
  c.beginPath();
  c.arc(0, 0, r, Math.PI, 2 * Math.PI);                        // half a turn, over the top
  c.stroke();
  // A head at each end, carrying on around the turn — so it reads "either way".
  c.fillStyle = color;
  for (const [at, way] of [[Math.PI, -1], [2 * Math.PI, 1]]) {
    const px = Math.cos(at) * r, py = Math.sin(at) * r;
    const tx = -Math.sin(at) * way, ty = Math.cos(at) * way;    // along the arc
    const nx = Math.cos(at), ny = Math.sin(at);                 // outwards
    c.beginPath();
    c.moveTo(px + tx * 5 * unit, py + ty * 5 * unit);           // the tip
    c.lineTo(px + nx * 3.5 * unit, py + ny * 3.5 * unit);
    c.lineTo(px - nx * 3.5 * unit, py - ny * 3.5 * unit);
    c.closePath();
    c.fill();
  }
  c.restore();
}

// A house at the machine's home corner: where its (0, 0) is. Drawn upright,
// and the same size on screen however far the paper is zoomed.
function drawHomeMark(c, at, color) {
  const r = 13 / Math.max(0.2, sheetScale);
  const [x, y] = at;
  c.save();
  c.fillStyle = color;
  c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
  c.fillStyle = '#fff';
  c.beginPath();
  const u = r / 13;                                  // the house, 26px across at 1:1
  c.moveTo(x - 6 * u, y + 1 * u); c.lineTo(x - 6 * u, y + 7 * u);
  c.lineTo(x + 6 * u, y + 7 * u); c.lineTo(x + 6 * u, y + 1 * u);
  c.lineTo(x, y - 7 * u); c.closePath();
  c.fill();
  c.restore();
}

// Pointer position in paper inches.
function pointerInches(e) {
  const r = $('canvas-wrap').getBoundingClientRect();
  const total = [paper.width + 2 * MARGIN_IN, paper.height + 2 * MARGIN_IN];
  return [(e.clientX - r.left) / r.width * total[0] - MARGIN_IN,
          (e.clientY - r.top) / r.height * total[1] - MARGIN_IN];
}
function handleAt(px, py) {
  // Nearest handle first, then the rectangles themselves (drawing on top).
  for (const which of ['ipad', 'axi']) {
    const rect = lay[which];
    const w = which === 'ipad' ? lay.ipad.w : travel.long;
    const h = which === 'ipad' ? lay.ipad.h : travel.short;
    const [hx, hy] = rotate(0, -h / 2 - 0.4, rect.rot);
    if (Math.hypot(px - (rect.cx + hx), py - (rect.cy + hy)) < 0.2) return { which, mode: 'turn' };
    if (which === 'ipad') {
      const corner = rectCorners(rect, w, h)[2];
      if (Math.hypot(px - corner[0], py - corner[1]) < 0.18) return { which, mode: 'scale' };
    }
    const [lx, ly] = rotate(px - rect.cx, py - rect.cy, -rect.rot);
    if (Math.abs(lx) <= w / 2 && Math.abs(ly) <= h / 2) return { which, mode: 'move' };
  }
  return null;
}

layoutCanvas.addEventListener('pointerdown', e => {
  if (!editingLayout) return;
  const [px, py] = pointerInches(e);
  const hit = handleAt(px, py);
  if (!hit) return;
  layoutCanvas.setPointerCapture(e.pointerId);
  const rect = lay[hit.which];
  drag = { ...hit, startX: px, startY: py, cx: rect.cx, cy: rect.cy, rot: rect.rot,
           w: lay.ipad.w, h: lay.ipad.h };
});
layoutCanvas.addEventListener('pointermove', e => {
  if (!editingLayout || !drag) return;
  const [px, py] = pointerInches(e);
  const rect = lay[drag.which];
  if (drag.mode === 'move') {
    rect.cx = drag.cx + (px - drag.startX);
    rect.cy = drag.cy + (py - drag.startY);
  } else if (drag.mode === 'turn') {
    const angle = Math.atan2(py - rect.cy, px - rect.cx) * 180 / Math.PI + 90;
    const snapped = Math.round(angle / 15) * 15;              // hold Alt for any angle
    rect.rot = e.altKey ? angle : (Math.abs(angle - snapped) < 4 ? snapped : angle);
  } else if (drag.mode === 'scale') {
    const [lx, ly] = rotate(px - rect.cx, py - rect.cy, -rect.rot);
    const k = Math.max(Math.abs(lx) / (drag.w / 2), Math.abs(ly) / (drag.h / 2));
    lay.ipad.w = Math.max(0.5, drag.w * k);
    lay.ipad.h = Math.max(0.5, drag.h * k);                    // the tablet's shape is kept
  }
  lay.auto = false;
  drawLayout();
});
for (const ev of ['pointerup', 'pointercancel']) {
  layoutCanvas.addEventListener(ev, () => { drag = null; });
}

function startLayoutEdit() {
  layoutBefore = JSON.parse(JSON.stringify(lay));
  editingLayout = true;
  $('layout-bar').hidden = false;
  layoutCanvas.classList.add('editing');
  drawLayout();
}
function endLayoutEdit(save) {
  editingLayout = false;
  $('layout-bar').hidden = true;
  layoutCanvas.classList.remove('editing');
  if (save) {
    store.set('axi_layout', JSON.stringify({ auto: lay.auto, ipad: lay.ipad, axi: lay.axi }));
    send({ type: 'set_layout', layout: { auto: lay.auto, ipad: lay.ipad, axi: lay.axi } });
  } else if (layoutBefore) {
    lay = layoutBefore;
  }
  layoutBefore = null;
  drawLayout();
}
$('layout-done').onclick = () => endLayoutEdit(true);
$('layout-cancel').onclick = () => endLayoutEdit(false);
$('layout-fit').onclick = () => {
  lay.auto = true;
  send({ type: 'set_layout', layout: { auto: true } });        // the engine re-fits and tells us
  store.remove('axi_layout');
};

// ── incoming drawing ────────────────────────────────────────────────────────

function onPoint(msg) {
  const { x, y, pressure, r, g, b, drawingWidth, tool } = msg;
  const [ix, iy] = canvasToPaper(x, y);
  const sx = toPx(ix), sy = toPx(iy);
  currentSize = sizeOnPaper(drawingWidth);
  if (!isInStroke) {
    isInStroke = true;
    currentPoints = [];
    currentColor = greyFor(r, g, b);
    strokeCount++;
    $('stroke-count').textContent = strokeCount;
  }
  currentPoints.push([sx, sy, pressure]);
  const n = currentPoints.length;
  if (n === 1) drawDot(overlayCtx, sx, sy, widthFor(currentSize, pressure) / 2, currentColor);
  else drawSegment(overlayCtx, currentPoints[n - 2], currentPoints[n - 1], currentSize, currentColor);
  ptCount++;
  $('pt-count').textContent = ptCount;
  if (canvasEmpty) { canvasEmpty = false; updateCards(); }
  unsaved = true;
  $('pressure-val').textContent = pressure.toFixed(2);
  $('tool-label').textContent = tool || '—';
}

function onPenUp() {
  // Copy the finished stroke's pixels down onto the canvas layer.
  if (currentPoints.length) ctx.drawImage(overlay, 0, 0);
  overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
  currentPoints = []; isInStroke = false;
}

// ── layers shown (and saved) ────────────────────────────────────────────────

const VIEW = [['view-raw', 'axi_view_raw', [canvas, overlay]],
              ['view-opt', 'axi_view_opt', [$('c-opt')]],
              ['view-fx',  'axi_view_fx',  [$('c-fx')]]];
function applyView() {
  for (const [id, , els] of VIEW) els.forEach(el => { el.style.visibility = $(id).checked ? '' : 'hidden'; });
}
for (const [id, key] of VIEW) {
  $(id).addEventListener('change', () => { store.set(key, $(id).checked ? '1' : '0'); applyView(); });
}
function loadView() {
  for (const [id, key] of VIEW) { const v = store.get(key); if (v !== null) $(id).checked = v === '1'; }
  applyView();
}
function shownLayers() {
  return { raw: $('view-raw').checked, optimized: $('view-opt').checked, effect: $('view-fx').checked };
}

// the little i buttons
const bubble = $('info-bubble');
document.querySelectorAll('.info').forEach(b => {
  b.onclick = e => {
    e.stopPropagation();
    e.preventDefault();
    const open = bubble.hidden || bubble.textContent !== b.dataset.info;
    document.querySelectorAll('.info').forEach(x => x.setAttribute('aria-pressed', 'false'));
    bubble.textContent = b.dataset.info;
    bubble.hidden = !open;
    b.setAttribute('aria-pressed', String(open));
    const inChips = !!b.closest('.layer-toggles');
    if (open && !inChips) {
      const r = b.getBoundingClientRect();
      bubble.style.position = 'fixed';
      bubble.style.top = (r.bottom + 6) + 'px';
      bubble.style.left = Math.max(8, Math.min(r.left - 100, window.innerWidth - 320)) + 'px';
      bubble.style.right = 'auto';
    } else {
      bubble.style.position = '';
      bubble.style.top = bubble.style.left = bubble.style.right = '';
    }
  };
});

// ── menus and windows ───────────────────────────────────────────────────────

const MENUS = [['menu-file-btn', 'menu-file'], ['menu-prefs-btn', 'menu-prefs'], ['menu-fx-btn', 'menu-fx']];
function closeMenus() {
  for (const [b, m] of MENUS) { $(m).hidden = true; $(b).setAttribute('aria-expanded', 'false'); }
}
for (const [btn, list] of MENUS) {
  $(btn).addEventListener('click', e => {
    e.stopPropagation();
    const open = $(list).hidden;
    closeMenus();
    $(list).hidden = !open;
    $(btn).setAttribute('aria-expanded', String(open));
  });
  $(list).addEventListener('click', e => { if (e.target.tagName === 'BUTTON') closeMenus(); });
}
function openWindow(id) { closeMenus(); $(id).hidden = false; }
document.querySelectorAll('.close-window').forEach(b => {
  b.onclick = () => { b.closest('.window').hidden = true; };
});
document.addEventListener('click', () => {
  closeMenus();
  closePopovers();
  bubble.hidden = true;
  document.querySelectorAll('.info').forEach(x => x.setAttribute('aria-pressed', 'false'));
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  closeMenus();
  closePopovers();
  document.querySelectorAll('.window').forEach(w => { w.hidden = true; });
  if (editingLayout) endLayoutEdit(false);
});

$('mi-prefs').onclick = () => openWindow('prefs-window');
$('mi-effects').onclick = () => openWindow('effects-window');
$('mi-layout').onclick = () => { closeMenus(); startLayoutEdit(); };

// ── File: new, import, save, discard ────────────────────────────────────────

function saveAs() {
  if (!send({ type: 'save_as', layers: shownLayers(), dir: store.get('axi_lastSaveDir') }))
    saveStatus('Not connected', 'bad');
  else saveStatus('Choose where to save…');
}
$('mi-save').onclick = saveAs;

$('mi-new').onclick = async () => {
  if (!canvasEmpty && unsaved) {
    const answer = await ask({ title: 'Start a new canvas?', body: "This one hasn't been saved.",
                               ok: 'Save as…', other: 'Discard' });
    if (!answer) return;
    if (answer === 'ok') { saveAs(); return; }
  }
  send({ type: 'new_drawing' });
};

$('mi-discard').onclick = async () => {
  if (canvasEmpty) { send({ type: 'discard_drawing' }); return; }
  if (await ask({ title: 'Discard the canvas?',
                  body: unsaved ? "It hasn't been saved. This can't be undone." : "This can't be undone.",
                  ok: 'Discard', danger: true }) === 'ok')
    send({ type: 'discard_drawing' });
};

$('mi-import').onclick = () => {
  send({ type: 'open_file', dir: store.get('axi_lastOpenDir') });
  saveStatus('Choose a drawing…');
};

// ── settings controls ───────────────────────────────────────────────────────
//
// One row per setting: its storage key, its control, and the engine message
// that applies it. Stored values are strings ("1"/"0" for switches), the same
// format the engine reads from settings.json at startup.

const SETTINGS = [
  { key: 'axi_penPosUp',           id: 'inp-pen-up',        type: 'int',  def: 60,   msg: 'set_pen_up_pos',           live: true, show: v => v },
  { key: 'axi_penDownMin',         id: 'inp-pen-down-min',  type: 'int',  def: 40,   msg: 'set_pen_down_min',         live: true, show: v => v },
  { key: 'axi_varPressure',        id: 'inp-var-pressure',  type: 'bool', def: false, msg: 'set_variable_pressure' },
  { key: 'axi_penDownMax',         id: 'inp-pen-down-max',  type: 'int',  def: 20,   msg: 'set_pen_down_max',         live: true, show: v => v },
  { key: 'axi_pressureUpdateRate', id: 'inp-pressure-rate', type: 'int',  def: 100,  msg: 'set_pressure_update_rate', min: 0, max: 100 },
  { key: 'axi_speedPenDown',       id: 'inp-speed-pendown', type: 'int',  def: 25,   msg: 'set_speed_pendown', min: 1, max: 110 },
  { key: 'axi_speedPenUp',         id: 'inp-speed-penup',   type: 'int',  def: 75,   msg: 'set_speed_penup',   min: 1, max: 110 },
  { key: 'axi_accel',              id: 'inp-accel',         type: 'int',  def: 75,   msg: 'set_accel',         min: 1, max: 100 },
  { key: 'axi_xTilt',              id: 'inp-x-tilt',        type: 'num',  def: 0,    msg: 'set_x_tilt', min: -10, max: 10 },
  { key: 'axi_yTilt',              id: 'inp-y-tilt',        type: 'num',  def: 0,    msg: 'set_y_tilt', min: -10, max: 10 },
  { key: 'axi_optEnabled',         id: 'inp-opt-en',        type: 'bool', def: true,  msg: 'set_opt_enabled' },
  { key: 'axi_optScale',           id: 'inp-opt-scale',     type: 'num',  def: 0.5,  msg: 'set_opt_scale', live: true, show: v => Math.round(v * 100) + '%' },
  { key: 'axi_minDist',            id: 'inp-min-dist',      type: 'num',  def: 0.01, msg: 'set_min_dist',  live: true, show: v => (v * 25.4).toFixed(2) + ' mm' },
  { key: 'axi_limitLag',           id: 'inp-maxlag-en',     type: 'bool', def: true,  msg: 'set_limit_lag' },
  { key: 'axi_lagThreshold',       id: 'inp-maxlag-sec',    type: 'num',  def: 3.0,  msg: 'set_lag_threshold', min: 0.5, max: 30 },
  { key: 'axi_healLive',           id: 'inp-heal-live',     type: 'bool', def: false, msg: 'set_heal_live' },
];
const settingValues = {};

function parseSetting(s, raw) {
  if (s.type === 'bool') return raw === '1' || raw === true;
  let v = s.type === 'int' ? parseInt(raw, 10) : parseFloat(raw);
  if (Number.isNaN(v)) return s.def;
  if (s.min !== undefined) v = Math.max(s.min, v);
  if (s.max !== undefined) v = Math.min(s.max, v);
  return v;
}
function settingMsg(s, v) {
  return s.type === 'bool' ? { type: s.msg, enabled: v } : { type: s.msg, value: v };
}
function showSetting(s, v) {
  const el = $(s.id);
  if (s.type === 'bool') el.checked = v; else el.value = v;
  if (s.show) $(s.id + '-val').textContent = s.show(v);
}
function loadSettings() {
  for (const s of SETTINGS) {
    const raw = store.get(s.key);
    settingValues[s.key] = raw === null ? s.def : parseSetting(s, raw);
    showSetting(s, settingValues[s.key]);
  }
  units = store.get('axi_units') === 'cm' ? 'cm' : 'in';
  dependentUI();
}
function dependentUI() {
  $('pressure-group').classList.toggle('dim', !settingValues.axi_varPressure);
  $('opt-group').classList.toggle('dim', !settingValues.axi_optEnabled);
}
for (const s of SETTINGS) {
  const el = $(s.id);
  const apply = () => {
    const v = parseSetting(s, s.type === 'bool' ? el.checked : el.value);
    settingValues[s.key] = v;
    showSetting(s, v);
    store.set(s.key, s.type === 'bool' ? (v ? '1' : '0') : v);
    send(settingMsg(s, v));
    dependentUI();
  };
  el.addEventListener(s.live ? 'input' : 'change', apply);
}

document.querySelectorAll('[data-test]').forEach(b => { b.onclick = () => send({ type: b.dataset.test }); });
$('btn-home').onclick = () => send({ type: 'home' });
$('plotter-set-home').onclick = () => { send({ type: 'set_home' }); toast('Home is where the carriage is now.'); };

$('reset-settings').onclick = async () => {
  if (await ask({ title: 'Reset settings?', body: 'Pen, speed, tilt and effects go back to defaults. The machine, paper and layout stay.', ok: 'Reset' }) !== 'ok') return;
  for (const s of SETTINGS) {
    settingValues[s.key] = s.def;
    showSetting(s, s.def);
    store.remove(s.key);
    send(settingMsg(s, s.def));
  }
  dependentUI();
  resetEffects();
};

// Push every setting this page shows to the engine on connecting (the same
// values it applied from settings.json at startup, so this is harmless).
function syncAllToServer() {
  for (const s of SETTINGS) send(settingMsg(s, settingValues[s.key]));
  syncEffects();
}

// ── machine and paper ───────────────────────────────────────────────────────

const PAPERS = [
  ['letter', 'Letter — 8.5 × 11 in', 8.5, 11],
  ['legal', 'Legal — 8.5 × 14 in', 8.5, 14],
  ['tabloid', 'Tabloid — 11 × 17 in', 11, 17],
  ['a5', 'A5 — 5.83 × 8.27 in', 5.83, 8.27],
  ['a4', 'A4 — 8.27 × 11.69 in', 8.27, 11.69],
  ['a3', 'A3 — 11.69 × 16.54 in', 11.69, 16.54],
];
let paperInfo = null;
let pendingPaper = null, pendingModel = null;

(function buildPaperSelect() {
  const sel = $('inp-paper');
  for (const [v, label] of PAPERS) sel.add(new Option(label, v));
  sel.add(new Option('Custom size…', 'custom'));
})();

function renderModels(models) {
  const sel = $('inp-model');
  sel.innerHTML = '';
  for (const [n, name] of Object.entries(models)) sel.add(new Option(name, n));
  if (paperInfo) sel.value = String(paperInfo.model);
}

function renderPaper(info) {
  paperInfo = info;
  modelName = info.model_name;
  const [w, h] = info.requested;
  const preset = PAPERS.find(p => Math.abs(p[2] - w) < 0.02 && Math.abs(p[3] - h) < 0.02);
  const sel = $('inp-paper');
  if (!(sel.value === 'custom' && !preset)) sel.value = preset ? preset[0] : 'custom';
  $('paper-custom').hidden = sel.value !== 'custom';
  $('inp-paper-w').value = w; $('inp-paper-h').value = h;
  $('inp-model').value = String(info.model);
  $('paper-caption').textContent = `${info.width}″ × ${info.height}″ · ${info.model_name}`;
  const changed = paper.width !== info.width || paper.height !== info.height;
  paper = { width: info.width, height: info.height };
  travel = { long: info.travel[0], short: info.travel[1] };
  applyPaperSize(changed && started);
}

function onLayout(msg) {
  lay = msg.layout;
  paper = msg.paper;
  travel = msg.travel;
  canvasSize = msg.canvas;
  const note = $('paper-note');
  note.hidden = !msg.out_of_reach;
  note.textContent = `Part of the drawing is outside what the ${modelName} reaches — see Preferences → Edit layout.`;
  $('layout-warning').hidden = !msg.out_of_reach;
  applyPaperSize(false);
  drawLayout();
}

function sendPaper(w, h) {
  if (!(w > 0 && h > 0)) return;
  pendingPaper = [w, h];
  send({ type: 'set_paper', width: w, height: h });
}
$('inp-paper').addEventListener('change', e => {
  const p = PAPERS.find(p => p[0] === e.target.value);
  $('paper-custom').hidden = e.target.value !== 'custom';
  if (p) sendPaper(p[2], p[3]);
});
for (const id of ['inp-paper-w', 'inp-paper-h'])
  $(id).addEventListener('change', () => sendPaper(parseFloat($('inp-paper-w').value), parseFloat($('inp-paper-h').value)));
$('inp-model').addEventListener('change', e => {
  pendingModel = e.target.value;
  send({ type: 'set_model', model: parseInt(e.target.value, 10) });
});

function onPaper(msg) {
  // Only the page that asked for the change saves it.
  if (pendingPaper) { store.set('axi_paperW', msg.requested[0]); store.set('axi_paperH', msg.requested[1]); pendingPaper = null; }
  if (pendingModel) { store.set('axi_model', msg.model); pendingModel = null; }
  renderPaper(msg);
}

// ── effects (built from the engine's list of effects) ───────────────────────

let EFFECT_SPECS = [];
const fxState = {};
const fxEnKey = name => 'axi_fx_en_' + name;
const fxParamKey = (name, a) => 'axi_fx_p_' + name + '_' + a;
const fxOnly = $('fx-effects-only');

function buildEffectsPanel(specs) {
  EFFECT_SPECS = specs;
  const list = $('fx-list');
  list.innerHTML = '';
  for (const spec of specs) {
    const saved = store.get(fxEnKey(spec.name));
    const st = { enabled: saved === null ? spec.enabled : saved === '1', params: {} };
    fxState[spec.name] = st;

    const box = document.createElement('section');
    box.className = 'fx';
    const head = document.createElement('label');
    head.className = 'check';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.id = 'fx-' + spec.name; cb.checked = st.enabled;
    head.append(cb, document.createTextNode(' ' + spec.label));
    box.append(head);
    const knobs = document.createElement('div');
    knobs.className = 'group';
    for (const p of spec.params) {
      const row = document.createElement('div');
      row.className = 'field';
      const lab = document.createElement('label');
      lab.textContent = p.label;
      const inp = document.createElement('input');
      inp.type = 'number'; inp.id = 'fxp-' + spec.name + '-' + p.attr;
      inp.min = p.min; inp.max = p.max; inp.step = p.step;
      lab.htmlFor = inp.id;
      const savedP = store.get(fxParamKey(spec.name, p.attr));
      const val = savedP === null ? p.default : parseFloat(savedP);
      inp.value = val; st.params[p.attr] = val;
      inp.addEventListener('change', () => {
        let v = parseFloat(inp.value);
        if (Number.isNaN(v)) { inp.value = st.params[p.attr]; return; }
        v = Math.max(p.min, Math.min(p.max, v));
        if (p.int) v = Math.round(v);
        inp.value = v; st.params[p.attr] = v;
        store.set(fxParamKey(spec.name, p.attr), v);
        send({ type: 'set_effect_param', name: spec.name, attr: p.attr, value: v });
      });
      row.append(lab, inp);
      knobs.append(row);
    }
    box.append(knobs);
    const reflect = () => knobs.classList.toggle('dim', !st.enabled);
    cb.addEventListener('change', () => {
      st.enabled = cb.checked;
      store.set(fxEnKey(spec.name), cb.checked ? '1' : '0');
      reflect();
      send({ type: 'set_effect_enabled', name: spec.name, enabled: cb.checked });
    });
    reflect();
    list.append(box);
  }
  fxOnly.checked = store.get('axi_fx_only') === '1';
}
fxOnly.addEventListener('change', () => {
  store.set('axi_fx_only', fxOnly.checked ? '1' : '0');
  send({ type: 'set_effects_only', enabled: fxOnly.checked });
});
function syncEffects() {
  for (const spec of EFFECT_SPECS) {
    const st = fxState[spec.name];
    if (!st) continue;
    send({ type: 'set_effect_enabled', name: spec.name, enabled: st.enabled });
    for (const p of spec.params) send({ type: 'set_effect_param', name: spec.name, attr: p.attr, value: st.params[p.attr] });
  }
  send({ type: 'set_effects_only', enabled: fxOnly.checked });
}
function resetEffects() {
  for (const spec of EFFECT_SPECS) {
    store.remove(fxEnKey(spec.name));
    for (const p of spec.params) store.remove(fxParamKey(spec.name, p.attr));
  }
  store.remove('axi_fx_only');
  buildEffectsPanel(EFFECT_SPECS);
  syncEffects();
}

// ── status: iPad and plotter ────────────────────────────────────────────────

const status = { osc: { state: 'stopped', port: 8800, message: '' }, oscAge: null,
                 plotter: { state: 'not_found', message: '', motors: false }, ips: [] };
let skipPlotter = false;        // "Use without a plotter" was pressed

function ipadState() {
  const o = status.osc;
  if (o.state === 'port_busy') return ['bad', 'Problem', o.message || `Port ${o.port} is in use.`];
  if (o.state !== 'listening') return ['bad', 'Stopped', 'Not listening. Press Restart listener.'];
  if (status.oscAge === null) return ['', 'Waiting', 'Nothing from the iPad yet.'];
  if (status.oscAge < 3) return ['ok', 'Receiving', 'Receiving points.'];
  return ['ok', 'Idle', `Last point ${fmtAge(status.oscAge)} ago.`];
}
function fmtAge(s) { return s < 90 ? Math.round(s) + ' s' : Math.round(s / 60) + ' min'; }

const PLOTTER_STATES = {
  connected:   ['ok',   'Connected',    'Connected and ready.'],
  not_found:   ['bad',  'Not found',    'No AxiDraw on USB.'],
  error:       ['bad',  'Error',        ''],
  unavailable: ['bad',  'No pyaxidraw', ''],
  dry_run:     ['warn', 'Dry run',      'Moves are logged, not plotted (--dry-run).'],
};
const MOTORS_OFF = ['warn', 'Motors off', 'Push the carriage where you want it, then re-engage.'];
function motorsOn() { return status.plotter.motors !== false; }

function primaryIP() {
  const p = status.ips.find(i => i.primary) || status.ips[0];
  return p ? p.ip : 'unknown';
}

function renderStatus() {
  const [cls, label, detail] = ipadState();
  for (const id of ['ipad-dot', 'ipad-dot2']) $(id).className = 'dot ' + cls;
  $('ipad-label').textContent = label;
  $('ipad-title').textContent = 'iPad: ' + label;
  $('ipad-detail').textContent = detail;

  const ip = primaryIP(), port = status.osc.port;
  $('addr-ip').textContent = ip;
  $('addr-port').textContent = port;
  $('fr-ip').textContent = ip;
  $('fr-port').textContent = port;
  document.querySelectorAll('.ts-ip').forEach(el => { el.textContent = ip; });
  document.querySelectorAll('.ts-port').forEach(el => { el.textContent = port; });
  if (document.activeElement !== $('osc-port-input')) $('osc-port-input').value = port;
  // The address to type in, plus Tailscale (the fix for networks that isolate
  // devices). Anything else is rarely the answer, so it folds away.
  const main = status.ips.filter(i => i.primary || i.label === 'Tailscale');
  const rest = status.ips.filter(i => !main.includes(i));
  const rows = (dl, ips) => {
    dl.innerHTML = '';
    for (const i of ips) {
      const dt = document.createElement('dt'); dt.textContent = i.label;
      const dd = document.createElement('dd'); dd.className = 'num'; dd.textContent = i.ip;
      dl.append(dt, dd);
    }
  };
  rows($('ipad-addresses'), main.length ? main : [{ ip: 'none found', label: 'On a network?' }]);
  rows($('ipad-addresses-more'), rest);
  $('ipad-more').hidden = !rest.length;

  const p = status.plotter;
  const off = plotterReady() && !motorsOn();
  const [pcls, plabel, pdetail] = off ? MOTORS_OFF : (PLOTTER_STATES[p.state] || ['bad', p.state, '']);
  for (const id of ['plotter-dot', 'plotter-dot2']) $(id).className = 'dot ' + pcls;
  $('plotter-label').textContent = plabel;
  $('plotter-title').textContent = 'Plotter: ' + plabel;
  $('plotter-detail').textContent = p.message ? capitalize(p.message) + '.' : pdetail;
  $('no-plotter-detail').textContent = p.message ? capitalize(p.message) + '.' : pdetail;
  $('plotter-connect').textContent = p.state === 'connected' ? 'Reconnect' : 'Connect';
  $('plotter-connect').disabled = p.state === 'dry_run';
  $('plotter-motors').textContent = motorsOn() ? 'Disengage XY Motors' : 'Re-engage XY Motors';
  updatePlotButtons();
  updateCards();
}
function capitalize(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

function plotterReady() { return ['connected', 'dry_run'].includes(status.plotter.state); }
function updatePlotButtons() {
  document.querySelectorAll('.needs-plotter, .needs-motors').forEach(b => {
    const needsMove = b.classList.contains('needs-motors');
    const blocked = !plotterReady() || (needsMove && !motorsOn());
    b.disabled = blocked;
    b.title = !plotterReady() ? 'Connect the plotter first (the Plotter button at the top).'
            : (needsMove && !motorsOn()) ? 'Re-engage the motors first.' : '';
  });
}

// The two cards over the paper: connect the iPad, then connect the plotter.
function updateCards() {
  const waitingForIpad = status.oscAge === null && canvasEmpty && status.osc.state === 'listening';
  $('first-run').hidden = !waitingForIpad || editingLayout;
  $('no-plotter').hidden = waitingForIpad || plotterReady() || skipPlotter || editingLayout;
}

// Popovers under the status buttons.
function setupPopover(btnId, panelId) {
  const btn = $(btnId), panel = $(panelId);
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const open = panel.hidden;
    closePopovers();
    closeMenus();
    if (open) openPopover(btnId, panelId);
  });
  panel.addEventListener('click', e => e.stopPropagation());
}
function openPopover(btnId, panelId) {
  const btn = $(btnId), panel = $(panelId);
  panel.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  const bar = btn.parentElement.getBoundingClientRect(), b = btn.getBoundingClientRect();
  panel.style.left = Math.max(8, Math.min(b.left - bar.left, bar.width - panel.offsetWidth - 8)) + 'px';
}
function closePopovers() {
  for (const [b, p] of [['ipad-btn', 'ipad-panel'], ['plotter-btn', 'plotter-panel']]) {
    $(p).hidden = true; $(b).setAttribute('aria-expanded', 'false');
  }
}
setupPopover('ipad-btn', 'ipad-panel');
setupPopover('plotter-btn', 'plotter-panel');
$('ipad-ts-btn').onclick = () => { $('ipad-ts').hidden = !$('ipad-ts').hidden; };
$('plotter-ts-btn').onclick = () => { $('plotter-ts').hidden = !$('plotter-ts').hidden; };
function openTroubleshoot(which) {
  closePopovers();
  $(which + '-ts').hidden = false;
  openPopover(which + '-btn', which + '-panel');
}
$('fr-troubleshoot').onclick = e => { e.stopPropagation(); openTroubleshoot('ipad'); };
$('no-plotter-ts').onclick = e => { e.stopPropagation(); openTroubleshoot('plotter'); };
$('no-plotter-connect').onclick = () => send({ type: 'connect_plotter' });
$('no-plotter-skip').onclick = () => { skipPlotter = true; updateCards(); };

$('osc-restart').onclick = () => send({ type: 'restart_osc' });

// Typing a port applies it once the number has been still for two seconds —
// no button, and no listener restart per keystroke.
let portTimer = null;
$('osc-port-input').addEventListener('input', e => {
  const note = $('osc-port-note');
  clearTimeout(portTimer);
  const port = parseInt(e.target.value, 10);
  if (String(port) === e.target.value.trim() && port >= 1024 && port <= 65535) {
    if (port === status.osc.port) { note.textContent = ''; return; }
    note.textContent = 'changing…';
    portTimer = setTimeout(() => {
      note.textContent = '';
      store.set('axi_oscPort', port);
      send({ type: 'restart_osc', port });
    }, 2000);
  } else {
    note.textContent = '1024–65535';
  }
});
$('plotter-connect').onclick = () => send({ type: 'connect_plotter' });
$('plotter-motors').onclick = async () => {
  const on = !motorsOn();
  if (!on && importing.active && await ask({ title: 'Stop the plot?', body: 'Disengaging cancels it.', ok: 'Stop and disengage', danger: true }) !== 'ok') return;
  send({ type: 'set_motors', on });
};
document.querySelectorAll('.copy-diag').forEach(b => { b.onclick = () => send({ type: 'diagnostics' }); });
$('copy-ip').onclick = async () => toast(await copyText(primaryIP()) ? `Copied ${primaryIP()}` : 'Copy failed — select the address and copy it by hand.', false);

$('quit-btn').onclick = async () => {
  if (await ask({ title: 'Quit Pantograph?', body: 'Pen up, motors off, canvas saved.', ok: 'Quit' }) !== 'ok') return;
  quitting = true;
  send({ type: 'quit' });
};

// ── importing a drawing ─────────────────────────────────────────────────────

let importing = { active: false };
function renderImport(msg) {
  const was = importing.active;
  importing = msg;
  const card = $('import-card');
  card.hidden = !msg.active;
  if (msg.active) {
    const pct = msg.total ? Math.round(100 * msg.plotted / msg.total) : 0;
    $('import-title').textContent = 'Plotting ' + (msg.name || 'a drawing');
    $('import-count').textContent = `${msg.plotted} / ${msg.total}`;
    $('import-bar').style.width = pct + '%';
    card.classList.toggle('paused', !!msg.paused);
    $('import-pause').textContent = msg.paused ? 'Resume' : 'Pause';
  }
  if (was && !msg.active && msg.outcome) {
    toast({ done: 'Import finished.', cancelled: 'Import cancelled.',
            failed: 'The import stopped with an error — see the console window.' }[msg.outcome] || 'Import ended.',
          msg.outcome === 'failed');
  }
}
$('import-pause').onclick = () => send({ type: importing.paused ? 'import_resume' : 'import_pause' });
$('import-cancel').onclick = async () => {
  if (await ask({ title: 'Cancel the import?', body: "The pen lifts; the rest isn't plotted.", ok: 'Cancel import', danger: true }) === 'ok')
    send({ type: 'import_cancel' });
};

// ── the preview panel: a drawing from a file, before it joins the canvas ────

function showOpened(msg) {
  if (msg.dir) store.set('axi_lastOpenDir', msg.dir);
  $('opened-name').textContent = msg.name;
  $('opened-meta').textContent =
    `${msg.strokes} strokes · ${msg.points} points · ${Math.round(msg.width)} × ${Math.round(msg.height)} canvas`;
  $('opened-img').src = '/opened.svg?v=' + Date.now();
  $('opened-panel').hidden = false;
}
$('opened-back').onclick = () => { $('opened-panel').hidden = true; };
$('opened-import').onclick = () => {
  $('opened-panel').hidden = true;
  send({ type: 'import_opened' });
};

// ── messages from the engine ────────────────────────────────────────────────

let started = false;

function handleMessage(msg) {
  switch (msg.type) {
  case 'hello': {
    const server = msg.settings || {};
    const local = localSettings();
    if (Object.keys(server).length && !sameSettings(server, local)) {
      // Adopt the computer's settings, and reload so every control is rebuilt
      // from them. (After the reload they match, so this happens once.)
      Object.keys(local).forEach(k => localStorage.removeItem(k));
      for (const [k, v] of Object.entries(server)) localStorage.setItem(k, v);
      location.reload();
      return;
    }
    if (!Object.keys(server).length && Object.keys(local).length) pushSettingsSoon();
    const firstHello = !started;
    if (!started) {
      loadSettings();
      loadView();
      buildEffectsPanel(msg.effect_specs || []);
    }
    syncAllToServer();
    status.osc = msg.osc_status; status.plotter = msg.plotter_status;
    status.oscAge = msg.osc_age; status.ips = msg.ips || [];
    renderModels(msg.models || {});
    renderPaper(msg.paper);
    started = true;
    onLayout(msg.layout);
    $('lag-display').textContent = (msg.lag || 0).toFixed(1);
    renderImport(msg.import || { active: false });
    renderStatus();
    // Catch up with the canvas so far: on first load, after a reload, after
    // reconnecting to a restarted app, or after the paper size changed.
    clearCanvas();
    for (const m of msg.drawing || []) handleMessage(m);
    unsaved = !canvasEmpty;
    // A file left open belongs on screen when the page first loads. A later
    // 'hello' is a catch-up — changing the paper asks for one — and reopening
    // the preview then would be a window nobody asked for.
    if (firstHello && msg.opened && msg.opened.ok) showOpened(msg.opened);
    return;
  }
  case 'point': onPoint(msg); return;
  case 'pen_up': onPenUp(); return;
  case 'layer': handleLayer(msg); return;
  case 'canvas_size':
    canvasSize = { width: msg.width, height: msg.height };
    return;
  case 'layout': onLayout(msg); return;
  case 'tool_change': $('tool-label').textContent = msg.tool; return;
  case 'new_drawing':
    clearCanvas();
    unsaved = false;
    saveStatus(msg.saved ? 'Saved ' + msg.saved : 'Started a new canvas', msg.saved ? 'good' : '');
    return;
  case 'saved':
    if (msg.cancelled) { saveStatus(''); return; }
    if (msg.ok) { unsaved = false; if (msg.dir) store.set('axi_lastSaveDir', msg.dir); }
    saveStatus(msg.ok ? 'Saved ' + msg.path : 'Save failed: ' + (msg.error || 'unknown error'), msg.ok ? 'good' : 'bad');
    return;
  case 'opened':
    if (msg.cancelled) { saveStatus(''); return; }
    if (msg.ok) { showOpened(msg); saveStatus(''); }
    else toast("Couldn't open that file: " + msg.error, true);
    return;
  case 'lag': {
    const el = $('lag-display');
    el.textContent = msg.seconds < 0.05 ? '0.0' : msg.seconds.toFixed(1);
    const cap = settingValues.axi_lagThreshold || 3;
    el.style.color = msg.seconds < cap * 0.5 ? '' : msg.seconds < cap ? 'var(--warn)' : 'var(--bad)';
    const changed = ipadBucket(msg.osc_age) !== ipadBucket(status.oscAge);
    status.oscAge = msg.osc_age ?? null;
    if (changed) renderStatus();
    return;
  }
  case 'osc_status': status.osc = msg; renderStatus(); return;
  case 'plotter_status': status.plotter = msg; renderStatus(); return;
  case 'paper': onPaper(msg); return;
  case 'import_progress': renderImport(msg); return;
  case 'diagnostics':
    copyText(msg.text).then(ok => toast(ok ? 'Diagnostics copied.' : 'Copy failed.', !ok));
    return;
  case 'error':
    toast(msg.message, true);
    if (paperInfo && (pendingPaper || pendingModel)) { pendingPaper = pendingModel = null; renderPaper(paperInfo); }
    return;
  }
}
// Re-render the iPad status only when its label would change (every tick otherwise).
function ipadBucket(age) { return age === null || age === undefined ? 'none' : age < 3 ? 'recv' : 'idle'; }

// ── start ───────────────────────────────────────────────────────────────────

applyPaperSize(false);
connect();

// For tests and the console.
window.pantograph = { newDrawing: () => send({ type: 'new_drawing' }), saveAs, showOpened,
                      startLayoutEdit, endLayoutEdit, layout: () => lay };
