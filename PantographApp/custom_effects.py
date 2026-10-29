"""
Effects written in the app, with someone's own AI.

The page asks for a description, and prompt() turns it into a prompt carrying
everything an AI needs to write an effect that fits postprocess.py: the effect
API (read from postprocess.py itself, so it can't drift), worked examples,
what a pen plotter can and can't do, and the exact shape of the answer. No AI
runs here; the person copies the prompt into whichever one they use and pastes
its reply back.

install() takes that reply and, before anything joins the app:
  1. pulls the code out of it (a reply is usually prose around a code block);
  2. checks it reads as one effect (static_check);
  3. runs it on made-up strokes in a separate Python process (selftest), which
     is killed if it hangs, so a bad effect can't freeze Pantograph;
  4. saves it as <name>.py in the effects folder (Documents/Pantograph/effects),
     outside the app folder, which the installer replaces on every update.

Effects in that folder are loaded at startup (load_all), but only files that
install() itself saved: it records each file's SHA-256 in a trust list kept in
the config folder (trusted_effects.json), outside Documents. A file that turns
up any other way (sent by someone, synced from another computer, edited since)
is reported and not run, so no code gets in without passing the paste box and
its warning. The file name is the effect's name, and so the key its settings
are saved under.

None of this is a sandbox. The checks catch an AI's mistakes, not malice: the
code runs with the same access to the computer as Pantograph does, which is
what the page warns about before anything is pasted.
"""

import ast
import hashlib
import importlib.util
import inspect
import json
import math
import random
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import postprocess
import recording

ROOT = Path(__file__).resolve().parents[1]
PROMPT_TEMPLATE = Path(__file__).resolve().parent / "effect_prompt.txt"

FOLDER_NAME = "effects"
REMOVED_NAME = "removed"      # Remove moves a file in here rather than deleting it
TRUST_FILE_NAME = "trusted_effects.json"   # in the config folder, never the effects folder

# Modules an effect may import. Not a security boundary (see the top): it stops
# an AI reaching for a library that isn't installed, or for files and network.
ALLOWED_IMPORTS = {"postprocess", "math", "random", "itertools", "functools",
                   "statistics", "collections", "numpy"}
# Calls no effect has a reason to make.
FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "input",
                   "breakpoint", "exit", "quit"}

BUILTIN_NAMES = frozenset(postprocess.REGISTRY)

SELFTEST_TIMEOUT_SEC = 10
# Commands an effect may produce across the whole self-test. Far past what any
# real effect makes on these strokes; well short of a runaway.
SELFTEST_MAX_COMMANDS = 400_000


class CheckError(Exception):
    """The pasted code isn't usable yet. str() is written for the person and the AI both."""


# ──────────────────────────────────────────────────────────────────────────────
# READING A REPLY
# ──────────────────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", re.S)

# The prompt asks for these as the code's first and last lines. They're
# comments, so they survive however the reply is copied: the code block alone,
# or the whole message as plain text, where the ``` fences are gone.
START_MARKER = "# --- PANTOGRAPH EFFECT START ---"
END_MARKER = "# --- PANTOGRAPH EFFECT END ---"
_START = re.compile(r"^[ \t]*#\s*-+\s*PANTOGRAPH EFFECT START\s*-+[ \t]*$", re.M | re.I)
_END = re.compile(r"^[ \t]*#\s*-+\s*PANTOGRAPH EFFECT END\s*-+[ \t]*$", re.M | re.I)
# A line that can start an effect file, when there's prose before it.
_CODE_START = re.compile(r"^(import |from |class |def |@|#|[A-Z_]+ *=)")


def extract_code(reply: str) -> str:
    """
    The Python in an AI's reply. People copy the whole reply, so this takes,
    in order of preference: what lies between the markers; the fenced block
    that defines an Effect (or the biggest Python block); or, with neither,
    the reply with any prose before and after the code trimmed off.
    """
    text = (reply or "").replace("\r\n", "\n").strip()
    start = _START.search(text)
    end = _END.search(text, start.end()) if start else None
    if start and end:
        text = text[start.start():end.end()]
        if not _FENCE.search(text):
            return textwrap.dedent(text).strip() + "\n"
    blocks = [(lang.lower(), body) for lang, body in _FENCE.findall(text)]
    if not blocks:
        return _trim_prose(text) + "\n"
    effects = [b for lang, b in blocks if "Effect)" in b]
    pythons = [b for lang, b in blocks if lang in ("python", "py", "python3", "")]
    best = max(effects or pythons or [b for _, b in blocks], key=len)
    return best.strip() + "\n"


def _trim_prose(text: str) -> str:
    """
    Unfenced, unmarked text: from the first line that looks like code (a
    Markdown heading looks like a comment, so a few are tried), the longest
    run of lines that parses and defines an Effect. The whole text if none
    does, so the refusal points at the real problem.
    """
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if _CODE_START.match(line)][:20]
    for first in starts:
        for last in range(len(lines), max(first, len(lines) - 40), -1):
            candidate = "\n".join(lines[first:last]).strip()
            if "Effect)" not in candidate:
                break
            try:
                ast.parse(candidate)
            except SyntaxError:
                continue
            return candidate
    return text


def _snake(text: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text or "")
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    if s and s[0].isdigit():
        s = "fx_" + s
    return s[:40] or "my_effect"


def static_check(code: str) -> dict:
    """
    Reads the code without running it. Returns {"class": …, "name": …} for the
    one Effect subclass, or raises CheckError saying what's wrong.
    """
    if not code.strip():
        raise CheckError("There's no code in what was pasted.")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        where = "" if "line" in (e.msg or "") else f" (line {e.lineno})"
        raise CheckError(f"The code isn't valid Python: {e.msg}{where}.") from None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise CheckError(f"It imports {a.name}, which effects can't use. "
                                     f"Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}.")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if node.level or mod not in ALLOWED_IMPORTS:
                raise CheckError(f"It imports from {node.module or '.'}, which effects can't use. "
                                 f"Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}.")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in FORBIDDEN_CALLS:
            raise CheckError(f"It calls {node.func.id}(), which an effect has no reason to use.")

    def is_effect(cls: ast.ClassDef) -> bool:
        return any((isinstance(b, ast.Name) and b.id == "Effect")
                   or (isinstance(b, ast.Attribute) and b.attr == "Effect") for b in cls.bases)

    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and is_effect(n)]
    if not classes:
        raise CheckError("There's no effect in the code: no class that subclasses Effect.")
    if len(classes) > 1:
        raise CheckError(f"There are {len(classes)} effects in the code "
                         f"({', '.join(c.name for c in classes)}); one file holds one effect.")
    cls = classes[0]
    name = None
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "name" for t in stmt.targets) \
                and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            name = stmt.value.value
    return {"class": cls.name, "name": _snake(name or cls.name)}


# ──────────────────────────────────────────────────────────────────────────────
# LOADING
# ──────────────────────────────────────────────────────────────────────────────

def folder(drawings_dir) -> Path:
    return Path(drawings_dir) / FOLDER_NAME


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _trusted(trust_file) -> dict:
    """file name → SHA-256 of what install() saved. Empty if missing or unreadable."""
    if trust_file is None:
        return {}
    try:
        data = json.loads(Path(trust_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _set_trusted(trust_file, file_name: str, digest: str | None) -> None:
    """Record (or with digest None, forget) one file in the trust list."""
    if trust_file is None:
        return
    entries = _trusted(trust_file)
    if digest is None:
        entries.pop(file_name, None)
    else:
        entries[file_name] = digest
    recording.write_atomic(Path(trust_file), json.dumps(entries, indent=1, sort_keys=True))


def why_untrusted(path: Path, trust_file) -> str | None:
    """None if install() saved this exact file, else why it won't be run."""
    recorded = _trusted(trust_file).get(path.name)
    if recorded is not None and recorded == _digest(path):
        return None
    how = ("it has changed since Pantograph added it" if recorded is not None
           else "Pantograph didn't add it on this computer")
    return (f"Not loaded: {how}, and code from anywhere else could do anything to your files. "
            "If you trust where it came from, paste its code into Add new effect… instead.")


def load_file(path: Path) -> type:
    """
    Run an effect file and return its Effect class, named after the file and
    with its PARAMS checked. Raises CheckError (or whatever the file raises).
    """
    path = Path(path)
    name = path.stem.lstrip(".").split(".")[0]
    spec = importlib.util.spec_from_file_location(f"pantograph_effect_{name}_{time.monotonic_ns()}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except CheckError:
        raise
    except Exception as e:                    # noqa: BLE001 — report, in words
        raise CheckError(f"The code stopped with an error as it loaded: {type(e).__name__}: {e}") from e
    found = [v for v in vars(module).values()
             if isinstance(v, type) and issubclass(v, postprocess.Effect)
             and v is not postprocess.Effect and v.__module__ == module.__name__]
    if len(found) != 1:
        raise CheckError(f"The file should define one effect; it defines {len(found)}.")
    cls = found[0]
    cls.name = name
    cls.source_file = str(path)
    if not isinstance(getattr(cls, "label", None), str) or cls.label in ("", "effect"):
        cls.label = name.replace("_", " ")
    if not isinstance(getattr(cls, "blurb", None), str):
        cls.blurb = ""
    cls.PARAMS = _checked_params(cls)
    return cls


def _checked_params(cls) -> list:
    params = getattr(cls, "PARAMS", [])
    if not isinstance(params, (list, tuple)):
        raise CheckError("PARAMS should be a list of settings.")
    out = []
    for p in params:
        if not isinstance(p, dict) or not isinstance(p.get("attr"), str):
            raise CheckError(f"Each PARAMS entry needs an \"attr\": {p!r:.80}")
        attr = p["attr"]
        default = getattr(cls, attr, None)
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            raise CheckError(f"The setting {attr} needs a number as its class attribute, not {default!r:.40}.")
        lo, hi = p.get("min"), p.get("max")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lo, hi)) \
                or lo > hi:
            raise CheckError(f"The setting {attr} needs a numeric \"min\" and \"max\", min no bigger than max.")
        if not lo <= default <= hi:
            raise CheckError(f"The setting {attr} starts at {default}, outside its own range {lo}-{hi}.")
        step = p.get("step")
        if not isinstance(step, (int, float)) or isinstance(step, bool) or not step > 0:
            step = 1 if p.get("int") else (hi - lo) / 100 or 1
        out.append({"attr": attr, "label": str(p.get("label") or attr.lower().replace("_", " ")),
                    "min": lo, "max": hi, "step": step, "int": bool(p.get("int"))})
    return out


def load_all(drawings_dir, trust_file) -> tuple[list, list]:
    """
    Register every effect in the effects folder that install() saved (see the
    top). Returns (loaded classes, [(file name, problem)]): a broken or
    unrecognised file is reported and left out, never fatal.
    """
    loaded, problems = [], []
    d = folder(drawings_dir)
    if not d.is_dir():
        return loaded, problems
    for path in sorted(d.glob("*.py")):
        if path.stem in BUILTIN_NAMES or path.name.startswith("."):
            continue
        why = why_untrusted(path, trust_file)
        if why is not None:
            problems.append((path.name, why))
            continue
        try:
            cls = load_file(path)
        except Exception as e:                # noqa: BLE001
            problems.append((path.name, str(e)))
            continue
        postprocess.REGISTRY[cls.name] = cls
        loaded.append(cls)
    return loaded, problems


# ──────────────────────────────────────────────────────────────────────────────
# INSTALLING AND REMOVING
# ──────────────────────────────────────────────────────────────────────────────

def _free_name(wanted: str, d: Path, replaces: str | None) -> str:
    if wanted == replaces:
        return wanted
    taken = set(BUILTIN_NAMES) | {p.stem for p in d.glob("*.py")} | set(postprocess.REGISTRY)
    taken.discard(replaces)
    name, n = wanted, 2
    while name in taken:
        name, n = f"{wanted}_{n}", n + 1
    return name


def install(reply: str, drawings_dir, trust_file, replaces: str | None = None) -> dict:
    """
    Check, test and save the effect in an AI's reply, and record the saved file
    in trust_file so that it loads at the next start. Returns {"ok": True,
    "name", "label", "warning", "cls"} or {"ok": False, "error", "code"}.
    Registering cls is the caller's job (the engine does it under its lock).
    `replaces` names the effect this one revises: it keeps that name, and so
    its place in the list and its on/off setting.
    """
    code = extract_code(reply)
    try:
        info = static_check(code)
        d = folder(drawings_dir)
        d.mkdir(parents=True, exist_ok=True)
        name = _free_name(replaces or info["name"], d, replaces)
        with tempfile.TemporaryDirectory(prefix="pantograph-effect-") as tmp:
            trial = Path(tmp) / f"{name}.py"
            trial.write_text(code, encoding="utf-8")
            report = run_selftest(trial)
            cls = load_file(trial)            # in this process now that it's proven
        path = d / f"{name}.py"
        path.write_text(code, encoding="utf-8")
        _set_trusted(trust_file, path.name, _digest(path))
        cls.source_file = str(path)
    except CheckError as e:
        return {"ok": False, "error": str(e), "code": code}
    return {"ok": True, "name": name, "label": cls.label, "warning": report.get("warning"), "cls": cls}


def remove_file(name: str, drawings_dir, trust_file) -> bool:
    """
    Move a custom effect's file to effects/removed/ (not the bin, so it can be
    got back). Unregistering it is the caller's job. False for a built-in.
    """
    cls = postprocess.REGISTRY.get(name)
    if cls is None or cls.source_file is None:
        return False
    src = Path(cls.source_file)
    if src.exists():
        gone = folder(drawings_dir) / REMOVED_NAME
        gone.mkdir(parents=True, exist_ok=True)
        src.replace(gone / src.name)
    _set_trusted(trust_file, src.name, None)
    return True


def source_of(name: str) -> str | None:
    cls = postprocess.REGISTRY.get(name)
    if cls is None or cls.source_file is None:
        return None
    try:
        return Path(cls.source_file).read_text(encoding="utf-8")
    except OSError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# THE SELF-TEST  (runs in a child process: python -m PantographApp.custom_effects)
# ──────────────────────────────────────────────────────────────────────────────

def run_selftest(path: Path) -> dict:
    """Run the self-test on path in a fresh Python, with a time limit. CheckError on failure."""
    with tempfile.TemporaryDirectory(prefix="pantograph-selftest-") as tmp:
        out = Path(tmp) / "result.json"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "PantographApp.custom_effects", "--selftest", str(path), str(out)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=SELFTEST_TIMEOUT_SEC, creationflags=flags)
        except subprocess.TimeoutExpired:
            during = _read_json(out).get("running", "the test strokes")
            raise CheckError(f"It never finished: {during} took over {SELFTEST_TIMEOUT_SEC} s. "
                             "Probably an endless loop.") from None
        report = _read_json(out)
        if report.get("ok"):
            return report
        if report.get("error"):
            raise CheckError(report["error"])
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise CheckError("The test run crashed" + (": " + " ".join(tail) if tail else "."))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _test_strokes(count: int) -> list:
    """
    Commands for `count` made-up strokes on a letter-size AxiDraw area, the way
    the engine sends them: straight lines, curves, a scribble, a single tap,
    pressure from none to full. Points sit ~0.25 mm apart, like real ones.
    """
    rng = random.Random(7)
    t, cmds = 0.0, []
    for s in range(count):
        kind = s % 5
        x, y = rng.uniform(1, 10.5), rng.uniform(1, 7.5)
        n = {0: 60, 1: 200, 2: 12, 3: 1, 4: 400}[kind]
        heading = rng.uniform(0, 2 * math.pi)
        cmds.append(postprocess.moveto(t, x, y))
        cmds.append(postprocess.pendown(t, rng.random(), x, y))
        if n == 1:
            cmds.append(postprocess.dot_dwell(t))
        for i in range(1, n):
            heading += {0: 0.0, 1: 0.03, 2: 0.2, 4: rng.uniform(-0.4, 0.4)}.get(kind, 0.0)
            x = min(11.3, max(0.5, x + 0.01 * math.cos(heading)))
            y = min(8.1, max(0.5, y + 0.01 * math.sin(heading)))
            cmds.append(postprocess.lineto(t + i * 0.005, x, y, (i / n) if kind != 2 else 1.0))
        t += n * 0.005
        cmds.append(postprocess.penup(t))
        t += 0.3
    return cmds


def _advance(ctx, cmd) -> None:
    """listen_to_idraw._advance_effect_ctx's bookkeeping, for the test stream."""
    kind = cmd[1]
    if kind == "moveto":
        ctx.stroke_index += 1
        ctx.point_index = 0
    elif kind == "pendown":
        ctx.pen_is_down = True
    elif kind == "lineto":
        ctx.point_index += 1
    elif kind == "penup":
        ctx.pen_is_down = False


def selftest(path: Path, out: Path) -> dict:
    """
    Feed the test strokes through the effect, first with its default settings,
    then with each setting at its lowest and its highest, all through the same
    guarded path the engine uses. Writes progress to `out` as it goes, so a
    run that has to be killed still says which part never finished.
    """
    def write(d):
        out.write_text(json.dumps(d), encoding="utf-8")

    try:
        cls = load_file(path)
    except CheckError as e:
        write({"ok": False, "error": str(e)})
        return {}
    runs = [("its default settings", {}, 25)]
    for p in cls.PARAMS:
        for which in ("min", "max"):
            v = p[which]
            runs.append((f"{p['label']} at {v:g}", {p["attr"]: int(round(v)) if p["int"] else v}, 6))

    total, changed, added = 0, False, 0
    for label, overrides, strokes in runs:
        write({"running": label})
        try:
            eff = cls()
            for attr, v in overrides.items():
                setattr(eff, attr, v)
        except Exception as e:                # noqa: BLE001
            write({"ok": False, "error": f"With {label}, it couldn't start: {type(e).__name__}: {e}"})
            return {}
        ctx = postprocess.Ctx(x_max=11.81, y_max=8.58)
        for cmd in _test_strokes(strokes):
            _advance(ctx, cmd)
            try:
                res = postprocess.apply_chain([eff], cmd, ctx)
            except postprocess.EffectError as e:
                write({"ok": False, "error": f"With {label}, {e}."})
                return {}
            total += len(res)
            if not overrides:
                changed = changed or len(res) != 1 or res[0] is not cmd
                added += sum(1 for c in res if c is not cmd)
            if total > SELFTEST_MAX_COMMANDS:
                write({"ok": False, "error": f"With {label}, it made over {SELFTEST_MAX_COMMANDS} plot "
                                             "commands for a handful of strokes: hours of plotting."})
                return {}
            xy = postprocess.xy_of(cmd)
            if xy is not None:
                ctx.last_xy = xy
    warning = None if changed else ("On the test strokes it didn't change anything. It may only act "
                                    "now and then, or it may not work yet: try it on a drawing.")
    report = {"ok": True, "warning": warning, "added": added}
    write(report)
    return report


# ──────────────────────────────────────────────────────────────────────────────
# PROMPTS
# ──────────────────────────────────────────────────────────────────────────────

def _api_reference() -> str:
    """The parts of postprocess.py an effect is written against, as source."""
    P = postprocess
    parts = [f'"""{P.__doc__.strip()}"""', "MM_PER_IN = 25.4"]
    for obj in (P.moveto, P.pendown, P.lineto, P.penup, P.dot_dwell, P.time_of, P.kind_of,
                P.xy_of, P.pressure_of, P.with_xy, P.Ctx, P.clamp_xy, P.draw_path, P.Effect):
        parts.append(inspect.getsource(obj).rstrip())
    return "\n\n\n".join(parts)


def _examples() -> str:
    P = postprocess
    return "\n\n\n".join(inspect.getsource(c).rstrip() for c in (P.Mirror, P.PressureHatch, P.Zigzag))


def prompt(mode: str, description: str = "", code: str = "", error: str = "") -> str:
    """
    The text to paste into an AI. mode is "new" (from a description), "revise"
    (code plus what to change) or "fix" (code plus why it was refused).
    """
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    sections = dict(re.findall(r"^=== (\w+) ===\n(.*?)(?=^=== |\Z)", template, re.S | re.M))
    task = sections[mode.upper()]
    text = sections["MAIN"].replace("{{TASK}}", task.strip())
    for key, value in (("{{DESCRIPTION}}", description.strip() or "(not given)"),
                       ("{{CODE}}", code.strip()), ("{{ERROR}}", error.strip()),
                       ("{{API}}", _api_reference()), ("{{EXAMPLES}}", _examples()),
                       ("{{ALLOWED}}", ", ".join(sorted(ALLOWED_IMPORTS))),
                       ("{{START}}", START_MARKER), ("{{END}}", END_MARKER)):
        text = text.replace(key, value)
    return text.strip() + "\n"


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        selftest(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        print("usage: python -m PantographApp.custom_effects --selftest <effect.py> <result.json>")
