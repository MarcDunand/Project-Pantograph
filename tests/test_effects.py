"""
Effects written in the app (PantographApp/custom_effects.py) and the guards
every effect runs behind (postprocess.run_effect): a reply becomes an effect,
a bad one is refused in words an AI can act on, a misbehaving one is switched
off mid-drawing without stopping the plot, and the page walks through it all.
"""
import json
import math
import os
import threading
import time

import pytest

import postprocess
from PantographApp import custom_effects as ce
from conftest import send_block, send_point
from pythonosc.udp_client import SimpleUDPClient

from test_app import App, hello, send, wait_until  # noqa: F401

GOOD = '''Here's an effect that circles the end of every stroke.

```python
import math
from postprocess import Effect, draw_path, MM_PER_IN

class EndCircle(Effect):
    name = "end_circle"
    label = "end circles"
    blurb = "Draws a small circle where each stroke ends."
    DIAMETER_MM = 2.0
    SIDES = 12
    PARAMS = [
        {"attr": "DIAMETER_MM", "label": "size (mm)", "min": 0.5, "max": 10, "step": 0.5},
        {"attr": "SIDES", "label": "sides", "min": 3, "max": 40, "step": 1, "int": True},
    ]

    def after_stroke(self, points, t, ctx):
        x, y, _ = points[-1]
        r = self.DIAMETER_MM / MM_PER_IN / 2
        n = self.SIDES
        return draw_path(t, [(x + r * math.cos(2 * math.pi * i / n),
                              y + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)], ctx)
```

Tweak the size to taste.'''

CRASHES = '''```python
from postprocess import Effect, lineto
class Bad(Effect):
    name = "bad"
    def on_lineto(self, cmd, ctx):
        return [lineto(cmd[0], cmd[2], cmd[3])]
```'''


@pytest.fixture
def registry(monkeypatch):
    """Whatever a test adds to the effect list is gone afterwards."""
    monkeypatch.setattr(postprocess, "REGISTRY", dict(postprocess.REGISTRY))


@pytest.fixture
def fx_engine(engine, registry, tmp_path, monkeypatch):
    """The engine, with a drawings folder of its own and its effect list restorable."""
    monkeypatch.setattr(engine, "_drawings_dir", tmp_path)
    monkeypatch.setattr(engine, "_effects_trust", tmp_path / "config" / ce.TRUST_FILE_NAME)
    monkeypatch.setattr(engine, "_EFFECT_SWITCHES", dict(engine._EFFECT_SWITCHES))
    monkeypatch.setattr(engine, "_effect_params", {k: dict(v) for k, v in engine._effect_params.items()})
    yield engine
    engine._rebuild_effect_chain()


def draw(L, n=20):
    send_block(L)
    for i in range(n):
        send_point(L, 100 + i * 5, 400)
    send_block(L)                       # the next block ends the stroke


def effect_cmds(L):
    return [q.cmd for q in L._plot_deque if q.layer and q.layer.get("layer") == "effect"]


# ── reading a reply ──────────────────────────────────────────────────────────

def test_the_code_is_found_in_a_chatty_reply():
    code = ce.extract_code(GOOD)
    assert code.startswith("import math") and "Tweak" not in code and "Here's" not in code
    assert ce.extract_code("class X(Effect):\n    pass") == "class X(Effect):\n    pass\n"


def test_the_markers_find_the_code_when_the_whole_message_is_copied_as_plain_text():
    code = ce.extract_code(GOOD)
    marked = f"{ce.START_MARKER}\n{code}{ce.END_MARKER}\n"
    # As a chat page copies it: the fences and their language label are gone,
    # and the prose around it has apostrophes that read as unclosed strings.
    plain = (f"# Spiral flourishes\nHere's the effect you asked for.\nPython\n{marked}"
             "I've added the circles with after_stroke, so the line itself isn't touched.\n")
    assert ce.extract_code(plain) == marked
    assert ce.extract_code(f"Here it is:\n```python\n{marked}```\nDone.") == marked
    # No markers either: the prose is trimmed off around the code.
    unmarked = f"# Spiral flourishes\nHere's the effect.\n{code}I've kept it short.\n"
    assert ce.extract_code(unmarked) == code
    assert ce.static_check(ce.extract_code(plain))["name"] == "end_circle"


def test_the_prompt_asks_for_the_markers():
    p = ce.prompt("new", "spirals")
    assert ce.START_MARKER in p and ce.END_MARKER in p


@pytest.mark.parametrize("code, says", [
    ("import os\nfrom postprocess import Effect\nclass E(Effect): pass", "imports os"),
    ("from postprocess import Effect\nclass E(Effect):\n    def f(self): open('x')", "open()"),
    ("x = 1", "no class that subclasses Effect"),
    ("from postprocess import Effect\nclass A(Effect): pass\nclass B(Effect): pass", "2 effects"),
    ("class E(Effect)\n    pass", "isn't valid Python"),
])
def test_code_that_cannot_be_an_effect_is_refused_in_words(code, says):
    with pytest.raises(ce.CheckError, match=says.replace("(", r"\(").replace(")", r"\)")):
        ce.static_check(code)


# ── the guards every effect runs behind ──────────────────────────────────────

class _Emits(postprocess.Effect):
    name = "emits"
    out = None

    def on_lineto(self, cmd, ctx):
        return self.out


def test_what_an_effect_makes_is_kept_on_the_paper():
    ctx = postprocess.Ctx(x_max=10, y_max=8)
    eff = _Emits()
    eff.out = [postprocess.lineto(0.0, 50, -3, 7.0)]
    got = postprocess.run_effect(eff, postprocess.lineto(0.0, 1, 1, 0.5), ctx)
    assert got == [(0.0, "lineto", 10, 0.0, 1.0)]


@pytest.mark.parametrize("out, says", [
    ([(0.0, "lineto", math.nan, 1.0, 0.5)], "nan"),
    ([(0.0, "fly", 1.0)], "isn't a plot command"),
    ([(0.0, "lineto", 1.0, 1.0)], "needs 3 numbers"),
    ((c for c in ()), "not a list"),
    ([postprocess.penup(0.0)] * (postprocess.MAX_OUTPUT_PER_COMMAND + 1), "from a single point"),
])
def test_an_effect_that_returns_nonsense_raises_effect_error(out, says):
    eff = _Emits()
    eff.out = out
    with pytest.raises(postprocess.EffectError, match=says):
        postprocess.run_effect(eff, postprocess.lineto(0.0, 1, 1, 0.5), postprocess.Ctx(10, 8))


def test_after_stroke_adds_marks_once_the_pen_lifts():
    class Echo(postprocess.Effect):
        def after_stroke(self, points, t, ctx):
            return postprocess.draw_path(t, [(x + 1, y, p) for x, y, p in points], ctx)

    ctx, eff, out = postprocess.Ctx(10, 8), Echo(), []
    for c in [postprocess.moveto(0, 1, 1), postprocess.pendown(0, 0.5, 1, 1),
              postprocess.lineto(0, 2, 1, 0.5), postprocess.penup(0)]:
        out += postprocess.apply_chain([eff], c, ctx)
    assert [c[1] for c in out] == ["moveto", "pendown", "lineto", "penup",
                                   "moveto", "pendown", "lineto", "penup"]
    assert postprocess.xy_of(out[-2]) == (3, 1)


# ── adding, revising, removing ───────────────────────────────────────────────

def test_a_reply_becomes_an_effect_that_plots(fx_engine, tmp_path):
    L = fx_engine
    r = L._effect_install({"reply": GOOD, "description": "circles"})
    assert r["ok"] and r["name"] == "end_circle" and r["label"] == "end circles"
    assert (tmp_path / "effects" / "end_circle.py").exists()
    spec = next(s for s in r["specs"] if s["name"] == "end_circle")
    assert spec["custom"] and [p["attr"] for p in spec["params"]] == ["DIAMETER_MM", "SIDES"]
    assert {"type": "effect_specs", "specs": r["specs"]} in L.sent

    L._handle_preview_message({"type": "set_effect_enabled", "name": "end_circle", "enabled": True})
    draw(L)
    assert len(effect_cmds(L)) == 2 + 12 + 1        # moveto, pendown, a 12-sided ring, penup


def test_the_same_name_twice_gets_a_new_name_but_a_revision_keeps_it(fx_engine, tmp_path):
    L = fx_engine
    assert L._effect_install({"reply": GOOD})["name"] == "end_circle"
    assert L._effect_install({"reply": GOOD})["name"] == "end_circle_2"
    r = L._effect_install({"reply": GOOD.replace("SIDES = 12", "SIDES = 6"), "replaces": "end_circle"})
    assert r["ok"] and r["name"] == "end_circle" and r["replaces"] == "end_circle"
    assert L._effect_params["end_circle"]["SIDES"] == 6         # back to the new default
    assert sorted(p.name for p in (tmp_path / "effects").glob("*.py")) == ["end_circle.py", "end_circle_2.py"]


def test_a_refused_reply_comes_with_a_prompt_to_fix_it(fx_engine, tmp_path):
    r = fx_engine._effect_install({"reply": CRASHES, "description": "wiggle the lines"})
    assert not r["ok"] and "lineto() missing 1 required positional argument" in r["error"]
    fix = r["fix_prompt"]
    assert r["error"] in fix and "wiggle the lines" in fix and "class Bad(Effect)" in fix
    assert not (tmp_path / "effects").exists() or not list((tmp_path / "effects").glob("*.py"))


def test_an_endless_loop_is_caught_before_it_joins_the_app(fx_engine, monkeypatch):
    monkeypatch.setattr(ce, "SELFTEST_TIMEOUT_SEC", 3)
    r = fx_engine._effect_install({"reply": GOOD.replace("x, y, _ = points[-1]", "while True: pass")})
    assert not r["ok"] and "never finished" in r["error"]


def test_removing_moves_the_file_aside(fx_engine, tmp_path):
    L = fx_engine
    L._effect_install({"reply": GOOD})
    L._handle_preview_message({"type": "set_effect_enabled", "name": "end_circle", "enabled": True})
    assert L._effect_remove("end_circle") is None
    assert "end_circle" not in L._EFFECT_SWITCHES and "end_circle" not in postprocess.REGISTRY
    assert (tmp_path / "effects" / "removed" / "end_circle.py").exists()
    assert "end_circle.py" not in ce._trusted(L._effects_trust)
    draw(L)                                          # and drawing still works
    assert L._effect_remove("zigzag")["type"] == "error"      # built-ins stay


def test_a_misbehaving_effect_is_switched_off_and_the_stroke_still_plots(fx_engine):
    L = fx_engine

    class Breaks(postprocess.Effect):
        name, label = "breaks", "breaks"

        def on_lineto(self, cmd, ctx):
            if ctx.point_index > 3:
                raise ZeroDivisionError("division by zero")
            return [cmd]

    postprocess.REGISTRY["breaks"] = Breaks
    L._sync_effect_registry()
    L._handle_preview_message({"type": "set_effect_enabled", "name": "breaks", "enabled": True})
    draw(L)
    assert L._EFFECT_SWITCHES["breaks"] is False
    failed = [m for m in L.sent if m.get("type") == "effect_failed"]
    assert failed and failed[0]["name"] == "breaks" and "ZeroDivisionError" in failed[0]["error"]
    assert [q.cmd[1] for q in L._plot_deque].count("lineto") >= 15


def test_stroke_end_marks_come_soon_after_the_pencil_lifts(fx_engine):
    """Not when the next stroke starts, or at New drawing, onto a fresh sheet."""
    L = fx_engine

    class EndDot(postprocess.Effect):
        name, label = "end_dot", "end dot"

        def after_stroke(self, points, t, ctx):
            x, y, _ = points[-1]
            return postprocess.draw_path(t, [(x, y)], ctx)

    postprocess.REGISTRY["end_dot"] = EndDot
    L._sync_effect_registry()
    L._handle_preview_message({"type": "set_effect_enabled", "name": "end_dot", "enabled": True})
    send_block(L)
    for i in range(10):
        send_point(L, 100 + i * 5, 400)
    assert effect_cmds(L) == []
    L.state["_last_point_time"] -= L.STROKE_END_SEC + 0.1
    L._maybe_end_stroke()
    assert [c[1] for c in effect_cmds(L)] == ["moveto", "pendown", "dot_dwell", "penup"]


def test_a_new_drawing_does_not_connect_back_to_the_old_sheet(fx_engine):
    L = fx_engine
    L._handle_preview_message({"type": "set_effect_enabled", "name": "stroke_connector", "enabled": True})
    draw(L)
    draw(L)
    assert effect_cmds(L)                            # the second stroke reached back to the first
    L.new_drawing()
    L._plot_deque.clear()
    draw(L)
    assert effect_cmds(L) == []                      # the first stroke on a fresh page has no partner


def test_an_effect_stuck_for_good_is_switched_off_for_next_time(fx_engine, tmp_path, monkeypatch):
    from PantographApp.settings import Settings
    L = fx_engine
    release = threading.Event()

    class Hangs(postprocess.Effect):
        name, label = "hangs", "hangs"

        def on_lineto(self, cmd, ctx):
            release.wait(10)              # stands in for an endless loop
            return [cmd]

    postprocess.REGISTRY["hangs"] = Hangs
    L._sync_effect_registry()
    L._handle_preview_message({"type": "set_effect_enabled", "name": "hangs", "enabled": True})
    monkeypatch.setattr(L, "_settings", Settings(tmp_path / "config"))
    monkeypatch.setattr(L, "_stuck_effect", None)
    monkeypatch.setattr(L, "EFFECT_STUCK_SEC", 0.2)
    drawing = threading.Thread(target=draw, args=(L,), daemon=True)
    drawing.start()
    try:
        assert wait_until(lambda: postprocess.running() is not None)
        L._check_stuck_effect()
        assert L._stuck_effect is None                  # not stuck long enough yet
        time.sleep(0.3)
        L._check_stuck_effect()
        assert L._stuck_effect == "hangs" and L._EFFECT_SWITCHES["hangs"] is False
        saved = json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))
        assert saved["values"]["axi_fx_en_hangs"] == "0"
        assert [m["name"] for m in L.sent if m.get("type") == "effect_stuck"] == ["hangs"]
    finally:
        release.set()
        drawing.join(10)


def test_only_files_the_app_saved_itself_are_run(tmp_path, registry):
    trust = tmp_path / "config" / ce.TRUST_FILE_NAME
    assert ce.install(GOOD, tmp_path, trust)["ok"]
    assert ce.install(GOOD.replace('"end_circle"', '"edited"'), tmp_path, trust)["ok"]
    d = tmp_path / "effects"
    with open(d / "edited.py", "a", encoding="utf-8") as f:
        f.write("# changed afterwards\n")
    # Code that leaves a mark if it runs, dropped in from outside.
    marker = tmp_path / "it_ran"
    (d / "sent_to_me.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    loaded, problems = ce.load_all(tmp_path, trust)
    assert [c.name for c in loaded] == ["end_circle"]
    why = dict(problems)
    assert "changed since Pantograph added it" in why["edited.py"]
    assert "didn't add it on this computer" in why["sent_to_me.py"]
    assert not marker.exists()
    assert ce.load_all(tmp_path, None)[0] == []      # no trust list: nothing runs


def test_prompts_carry_the_api_and_the_request():
    p = ce.prompt("new", "spirals at stroke ends")
    assert "spirals at stroke ends" in p and "def draw_path" in p and "class Mirror(Effect)" in p
    assert "{{" not in p
    r = ce.prompt("revise", "bigger", code="class X(Effect): pass")
    assert "Keep its `name` unchanged" in r and "class X(Effect): pass" in r


# ── the real app ─────────────────────────────────────────────────────────────

def test_effects_in_the_folder_load_at_startup(tmp_path, registry):
    trust = tmp_path / "config" / ce.TRUST_FILE_NAME   # where --data-dir puts it
    assert ce.install(GOOD, tmp_path, trust)["ok"]
    (tmp_path / "effects" / "broken.py").write_text("raise ValueError('nope')\n", encoding="utf-8")
    a = App(tmp_path)
    try:
        assert a.wait_for("[ui] http://127.0.0.1:"), "\n".join(a.lines)
        h = hello(a)
        names = {s["name"]: s for s in h["effect_specs"]}
        assert names["end_circle"]["custom"] and not names["zigzag"]["custom"]
        assert [p["file"] for p in h["effect_problems"]] == ["broken.py"]
    finally:
        a.proc.kill()


def test_a_stuck_effect_still_lets_the_app_quit(tmp_path, registry):
    """It hangs only past the self-test's longest stroke (400 points), as a real bug might."""
    trust = tmp_path / "config" / ce.TRUST_FILE_NAME
    hangs = GOOD.replace('name = "end_circle"', 'name = "hangs"').replace(
        "    def after_stroke(", "    def on_lineto(self, cmd, ctx):\n"
        "        while ctx.point_index > 450:\n            pass\n        return [cmd]\n\n"
        "    def after_stroke(")
    assert ce.install(hangs, tmp_path, trust)["ok"]
    a = App(tmp_path)
    try:
        assert a.wait_for("[ui] http://127.0.0.1:"), "\n".join(a.lines)
        send(a, {"type": "set_effect_enabled", "name": "hangs", "enabled": True}, wait=0.3)
        c = SimpleUDPClient("127.0.0.1", a.osc_port)
        for addr, v in [("/r", 0.0), ("/pen", 1.0), ("/canvasWidth", 440.0), ("/canvasHeight", 956.0)]:
            c.send_message(addr, v)
        for i in range(500):                 # a diagonal that stays on the canvas
            c.send_message("/x", 50.0 + i * 0.6)
            c.send_message("/y", 100.0 + i * 1.5)
            c.send_message("/pressure", 2.0)
        log = tmp_path / "logs" / "pantograph.log"
        assert wait_until(lambda: "[effect] hangs is stuck" in log.read_text(encoding="utf-8"), timeout=20), \
            log.read_text(encoding="utf-8")
        send(a, {"type": "quit"}, wait=0.3)
        assert a.finish() == 0, "\n".join(a.lines)
        saved = json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))
        assert saved["values"]["axi_fx_en_hangs"] == "0"
    finally:
        if a.proc.poll() is None:
            a.proc.kill()


def test_adding_an_effect_in_a_real_browser(tmp_path):
    """Headless Edge or Chrome; skipped if neither is installed."""
    sync_api = pytest.importorskip("playwright.sync_api")
    a = App(tmp_path)
    try:
        assert a.wait_for("[ui] http://127.0.0.1:"), "\n".join(a.lines)
        with sync_api.sync_playwright() as pw:
            browser = None
            for channel in ("msedge", "chrome"):
                try:
                    browser = pw.chromium.launch(channel=channel, headless=True)
                    break
                except Exception:               # noqa: BLE001 — try the next browser
                    pass
            if browser is None:
                pytest.skip("no Edge or Chrome installed")
            errors = []
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{a.ui_port}")
            page.wait_for_function("document.getElementById('conn-label').textContent === 'live'", timeout=10000)
            page.click("#tab-effects")
            assert page.inner_text(".risky") == "Potentially risky"
            page.click("#fx-add")
            assert page.is_visible("#fx-window") and not page.is_visible("#fx-step-paste")
            page.fill("#fx-description", "circles at the end of every stroke")
            page.click("#fx-make-prompt")
            page.wait_for_function("document.getElementById('fx-prompt').value.includes('circles at the end')")
            assert page.is_visible(".code-warning") and "WARNING" in page.inner_text(".code-warning")
            page.fill("#fx-reply", CRASHES)
            page.click("#fx-install")
            page.wait_for_selector("#fx-fix-row:not([hidden])", timeout=15000)
            assert page.inner_text("#fx-fail-text") == "Code didn't compile."
            assert "lineto()" in page.get_attribute("#fx-fail-text", "title")
            assert not page.is_visible("#fx-result")
            page.fill("#fx-reply", GOOD)
            page.click("#fx-install")
            page.wait_for_selector("#fx-result:not([hidden])", timeout=15000)
            assert not page.is_visible("#fx-fix-row") and page.inner_text("#fx-result-text") == "Effect added."
            page.click("#fx-done")
            assert page.is_checked("#fx-end_circle") and not page.is_checked("#fx-zigzag")
            assert "added by you" in page.inner_text("#fx-list")
            assert os.path.exists(tmp_path / "effects" / "end_circle.py")
            assert not errors, errors
            browser.close()
    finally:
        a.proc.kill()
