# draw2axi — Remote (no-AxiDraw) version

A standalone version of the draw2axi pipeline for use **without an AxiDraw
present**. You draw with iDraw OSC, watch a live preview, and download a
plottable SVG. The SVG is saved for later and plotted **asynchronously** on the
fully-tooled machine (the repo root's `listen_to_idraw.py` + `preview.py`).

This version keeps only what's needed to produce a correctly-formatted recording
SVG. It has **no** AxiDraw connection, plotter thread, path optimizer,
post-processing effects, paper coordinate mapping, or pen/tilt/flip controls —
all of that lives in the full version and is applied there, at plot time.

## Run

```
pip install python-osc websockets      # no numpy / rdp / pyaxidraw needed
python listen_to_idraw_remote.py
```

In iDraw OSC (iPad): set IP to your computer's Wi-Fi IP, Port to `8800`.
The preview opens at http://localhost:5000. Draw, then **download → svg**.
SVGs land in `iDraw_to_svg/saved_drawings/`.

## Plotting later

Copy the downloaded `.svg` to the AxiDraw machine and load it with the full
tool's **"plot svg"** button. Replay reads the raw OSC input embedded in the
SVG's `<metadata>` and re-applies paper mapping, flips, effects, and the
optimizer downstream — so the same recording can be plotted with different
settings.

## ⚠️ Shared recording contract — keep both versions in sync

The SVG produced here must stay byte-compatible with what the full tool reads
back. The contract is two things:

1. The `point` broadcast message shape (in `listen_to_idraw_remote.py`
   `_emit_point`) — per-point `t, x, y, pressureRaw` plus per-stroke tool /
   drawingWidth / color / canvas size, and `PEN_UP_TIMEOUT_SEC` stroke
   segmentation.
2. The `draw2axi-recording` v1 JSON in `<metadata>` and the surrounding SVG
   structure (in `preview_remote.py` `buildRecording` / `layerSvgParts`), copied
   verbatim from the full `preview.py`.

**If you change how the recording or its interpretation works in either the
remote or the full version, you must update BOTH.** The full version's readers/
writers of this format are: `preview.py` (`buildRecording`, `uploadSVG` replay),
`listen_to_idraw.py` (`_replay_recording`), and `svg_transform.py`. A
mismatch means SVGs recorded here will misplot there.
