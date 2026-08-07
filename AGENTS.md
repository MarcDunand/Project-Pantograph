# Project Pantograph — Context Document

Background for the draw2axi project: what it is for, what the input protocol
looks like, and which approaches were considered and set aside. Hand it to a new
Claude session together with `README.md`.

**`README.md` is the description of the code** — architecture, run instructions,
controls, effects, the recording format. This file deliberately does not repeat
it, and should not be treated as a description of how the pipeline currently
works.

---

## 1. Project goal

**Live drawing recreation.** An artist draws on an iPad with an Apple Pencil,
and an AxiDraw pen plotter physically recreates the drawing in real time on
paper. The system is framed as a "collaborative drawing machine" — the human
leads, the machine follows.

The pipeline is:

```
Apple Pencil → iPad (iDraw OSC app) → Wi-Fi (OSC) → Python → AxiDraw
```

---

## 2. What OSC is

OSC = Open Sound Control. A lightweight UDP network message protocol common in
creative coding (TouchDesigner, Max/MSP, etc.). iDraw OSC packages Apple Pencil
position data into OSC messages and broadcasts them over Wi-Fi to a configured
IP and port. Python receives them directly — TouchDesigner is not required and
is not part of this pipeline.

---

## 3. Input: iDraw OSC

iDraw OSC is an iPad app that reads Apple Pencil input and streams it as
individual scalar OSC messages. Each "point" arrives as a burst of separate
messages, not as a single bundled packet.

**Current OSC port: 8800**

In the iDraw OSC app, set:
- IP → computer's local Wi-Fi IP (find with `ipconfig` on Windows, look for
  Wi-Fi adapter IPv4)
- Port → 8800

### 3.1 Position messages

| Address    | Value                                      |
|------------|--------------------------------------------|
| `/x`       | canvas x coordinate (e.g. 67.67)          |
| `/y`       | canvas y coordinate (e.g. 77.33)          |
| `/pressure`| pen pressure, 0 → ≈4.1667                 |
| `/aspectX` | normalized centered x ≈ x/canvasWidth - 0.5  — **not used** |
| `/aspectY` | normalized centered y ≈ 0.5 - y/canvasHeight — **not used** |

`/x` and `/y` are in canvas pixel coordinates. `/aspectX` and `/aspectY` are
derived from them and are ignored in favor of raw x/y.

The burst order observed is: `/x` → `/y` → `/pressure` → `/aspectX` →
`/aspectY`. The code emits a point on each `/y` arrival (treating it as the
last meaningful field in the burst).

**Pressure quirks.** Raw pressure runs 0 → `OSC_PRESSURE_MAX` (4.166666507720947,
Apple Pencil at full press); finger input reports a constant value. iDraw also
intermittently sends a raw value of *exactly* `1.0` as a placeholder — it is not
a real reading, and the pipeline treats it as spurious. See README § Pressure.

### 3.2 Canvas dimensions

| Address        | Example value |
|----------------|---------------|
| `/canvasWidth` | 440.0         |
| `/canvasHeight`| 956.0         |

These arrive repeatedly as part of the state block. The coordinate mapping is
recalculated whenever either value updates.

### 3.3 Tool and style state

| Address         | Meaning                                  |
|-----------------|------------------------------------------|
| `/r /g /b /a`   | current drawing color (RGBA, 0.0–1.0)   |
| `/pen`          | one-hot tool flag                        |
| `/pencil`       | one-hot tool flag                        |
| `/marker`       | one-hot tool flag                        |
| `/monoline`     | one-hot tool flag                        |
| `/crayon`       | one-hot tool flag                        |
| `/fountainPen`  | one-hot tool flag                        |
| `/waterColor`   | one-hot tool flag                        |
| `/bitmapEraser` | one-hot tool flag                        |
| `/vectorEraser` | one-hot tool flag                        |
| `/drawingWidth` | current brush width (e.g. 2.68)         |
| `/eraserWidth`  | current eraser width                     |

Tool flags are one-hot: exactly one is 1.0 at a time, the rest are 0.0.
Drawable tools (produce a physical mark): pen, pencil, marker, monoline,
crayon, fountainPen, waterColor. Eraser tools are tracked in state but
currently skipped — no physical erasing on the AxiDraw.

### 3.4 Stroke boundary inference

iDraw OSC does **not** send explicit strokeStart / strokeEnd / penUp / penDown
messages. Stroke boundaries are inferred by time gap: if no new point arrives
within `PEN_UP_TIMEOUT_SEC` (currently 0.15s), that is treated as a pen lift.

This is an inference, not a fact, and it has a known failure mode: dragging the
Pencil fast enough that consecutive points arrive more than the timeout apart
chops one continuous line into a run of single-point strokes. `dot_healer.py`
repairs an SVG that has been damaged this way.

---

## 4. Alternatives explored (not the current path)

These were considered earlier but set aside in favor of iDraw OSC:

- **Procreate**: no accessible live stroke API
- **PencilKit / UIKit**: viable fallback requiring a custom Swift iPad app;
  would give richer data (altitude, azimuth, strokeID, phase) but 2–3 week
  build timeline
- **Browser / WebSocket**: iPad Safari canvas → JS touch events → WebSocket →
  Python; simpler but less precise than native Apple Pencil data
- **Wacom + Krita**: alternate input hardware path; still viable if iPad OSC
  becomes limiting
- **Physical paper over iPad**: artist draws on paper laid over iPad, capturing
  capacitance — interesting hybrid but not pursued

iDraw OSC is the current and active approach.
