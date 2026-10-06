"""
The system's own Open and Save windows, so a drawing can be opened from
anywhere and saved wherever the user wants — the way any other app does it.

Two ways to show them, depending on where the UI is:

- **In Pantograph's own window** (shell.run_window), the window shows them
  itself, attached to it, and can be asked from any thread.
- **In a browser**, tkinter shows them. tkinter is only imported when a window
  is actually opened: it's in the standard library, but a missing one (a
  stripped Python) must not stop the app starting. These calls have to run on
  the **main thread** — macOS requires it — which listen_to_idraw.main()
  arranges (see run_on_main).
"""

from pathlib import Path

from PantographApp import shell


def _root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)       # in front of the browser, not behind it
    return root


SVG_TYPES = [("Pantograph drawing", "*.svg"), ("All files", "*.*")]
# The same list in the form the window's dialogs take.
WINDOW_TYPES = ("Pantograph drawing (*.svg)", "All files (*.*)")


def _first(chosen) -> str | None:
    """The window's dialogs answer with a tuple, a string, or None."""
    if not chosen:
        return None
    return chosen if isinstance(chosen, str) else chosen[0]


def ask_open(initial_dir: Path | None = None) -> str | None:
    """Pick a drawing to open. Returns its path, or None if cancelled."""
    window = shell.current_window()
    if window is not None:
        from webview import FileDialog
        return _first(window.create_file_dialog(
            FileDialog.OPEN, directory=str(initial_dir) if initial_dir else "",
            file_types=WINDOW_TYPES))

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
    window = shell.current_window()
    if window is not None:
        from webview import FileDialog
        chosen = _first(window.create_file_dialog(
            FileDialog.SAVE, directory=str(initial_dir) if initial_dir else "",
            save_filename=initial_name, file_types=WINDOW_TYPES))
        # tkinter adds the extension itself (defaultextension); here it's ours to do.
        if chosen and not Path(chosen).suffix:
            chosen += ".svg"
        return chosen

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
