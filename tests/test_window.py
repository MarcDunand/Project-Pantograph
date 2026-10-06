"""
Pantograph's own window (shell.run_window): the parts that can be checked
without opening one. That it really opens, loads the page and closes is what
`--window-test` is for; CI runs it through the launcher.
"""
import sys
import threading

import listen_to_idraw as L
from PantographApp import dialogs, shell
from test_app import app, send  # noqa: F401 — app is a fixture


class FakeWindow:
    """Stands in for a pywebview window: records what it was asked."""
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def create_file_dialog(self, kind, **kw):
        self.asked.append((kind, kw))
        return self.answer


def test_no_window_library_means_the_browser(monkeypatch, tmp_path):
    """A machine where pywebview can't load must still start: run_window says
    it didn't open, and the caller opens the browser."""
    monkeypatch.setitem(sys.modules, "webview", None)      # makes `import webview` fail
    assert shell.run_window("http://127.0.0.1:1", storage=tmp_path, stop=threading.Event()) is False
    assert shell.current_window() is None


def test_dialogs_come_from_the_window_when_there_is_one(monkeypatch, tmp_path):
    from webview import FileDialog
    w = FakeWindow((str(tmp_path / "a.svg"),))
    monkeypatch.setattr(shell, "_window", w)

    assert dialogs.ask_open(tmp_path) == str(tmp_path / "a.svg")
    kind, kw = w.asked[-1]
    assert kind == FileDialog.OPEN and kw["directory"] == str(tmp_path)

    # Saving: the name typed without an extension gets .svg, as tkinter did it.
    w.answer = (str(tmp_path / "mine"),)
    assert dialogs.ask_save(tmp_path, "drawing.svg") == str(tmp_path / "mine.svg")
    kind, kw = w.asked[-1]
    assert kind == FileDialog.SAVE and kw["save_filename"] == "drawing.svg"

    # Cancelled, and the plain-string answer some platforms give.
    w.answer = None
    assert dialogs.ask_open(tmp_path) is None and dialogs.ask_save(tmp_path) is None
    w.answer = str(tmp_path / "b.svg")
    assert dialogs.ask_save(tmp_path) == str(tmp_path / "b.svg")


def test_with_a_window_file_dialogs_dont_wait_for_the_main_thread(monkeypatch):
    """The window owns the main thread, so nothing serves the main-thread
    queue: the call has to run by itself, and not on the caller's thread."""
    monkeypatch.setattr(shell, "_window", object())
    ran_on = L.run_on_main(threading.current_thread).result(timeout=5)
    assert ran_on is not threading.current_thread()
    assert L._main_calls.empty()

    monkeypatch.setattr(shell, "_window", None)             # in a browser: queued, as before
    future = L.run_on_main(lambda: 7)
    assert not future.done()
    L.serve_main_thread_calls()
    assert future.result(timeout=1) == 7


def test_a_second_launch_gets_no_from_a_copy_without_a_window(app):
    """`focus_window` is what a second launch sends. A copy whose UI is in a
    browser answers ok: False, and the second launch opens a tab instead."""
    replies = [m for m in send(app, {"type": "focus_window"}, wait=0.5) if m["type"] == "focused"]
    assert replies and replies[0]["ok"] is False
    assert shell.show_running(f"http://127.0.0.1:{app.ui_port}") is False
