"""
The system's own Open and Save windows, so a drawing can be opened from
anywhere and saved wherever the user wants — the way any other app does it.

tkinter is only imported when a window is actually opened: it's in the standard
library, but a missing one (a stripped Python) must not stop the app starting.
Every call here has to run on the **main thread** — macOS requires it — which
listen_to_idraw.main() arranges (see run_on_main).
"""

from pathlib import Path


def _root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)       # in front of the browser, not behind it
    return root


SVG_TYPES = [("Pantograph drawing", "*.svg"), ("All files", "*.*")]


def ask_open(initial_dir: Path | None = None) -> str | None:
    """Pick a drawing to open. Returns its path, or None if cancelled."""
    from tkinter import filedialog
    root = _root()
    try:
        return filedialog.askopenfilename(
            parent=root, title="Open drawing",
            initialdir=str(initial_dir) if initial_dir else None,
            filetypes=SVG_TYPES) or None
    finally:
        root.destroy()


def ask_save(initial_dir: Path | None = None, initial_name: str = "drawing.svg") -> str | None:
    """Pick where to save a drawing. Returns the path, or None if cancelled."""
    from tkinter import filedialog
    root = _root()
    try:
        return filedialog.asksaveasfilename(
            parent=root, title="Save drawing as",
            initialdir=str(initial_dir) if initial_dir else None,
            initialfile=initial_name, defaultextension=".svg",
            filetypes=SVG_TYPES) or None
    finally:
        root.destroy()
