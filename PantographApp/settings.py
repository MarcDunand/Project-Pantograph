"""
Settings, kept in settings.json in the per-user config folder.

The file holds the same key → string map the page keeps in its browser storage
(`axi_penPosUp`, `axi_fx_en_zigzag`, …). This file is the source of truth: the
page pushes every change here (`save_settings`), and when a page connects it
adopts this copy (see preview.py's 'hello' handling). At startup the engine
applies it too, so the plotter is set up before any browser is open.
"""

import json
from pathlib import Path

import recording


class Settings:
    def __init__(self, config_dir: Path):
        self.path = Path(config_dir) / "settings.json"
        self.values: dict[str, str] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            # Unknown or oddly typed entries are dropped, not fatal.
            self.values = {str(k): str(v) for k, v in data.get("values", {}).items()
                           if str(k).startswith("axi_")}
        except FileNotFoundError:
            pass
        except (ValueError, AttributeError):
            pass        # a corrupt file starts over from defaults

    def replace(self, values: dict) -> None:
        """Store the page's full set of settings."""
        self.values = {str(k): str(v) for k, v in values.items() if str(k).startswith("axi_")}
        recording.write_atomic(self.path, json.dumps({"values": self.values}, indent=1, sort_keys=True))


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def engine_messages(values: dict) -> list[dict]:
    """
    The engine messages that put the saved settings into effect — the same ones
    the page sends when it connects. Keys that only matter to the page (which
    layers are shown, which tab is open) aren't included.
    """
    v = values
    msgs = []
    booleans = {"axi_optEnabled": "set_opt_enabled", "axi_limitLag": "set_limit_lag",
                "axi_varPressure": "set_variable_pressure", "axi_fx_only": "set_effects_only",
                "axi_healLive": "set_heal_live"}
    for key, t in booleans.items():
        if key in v:
            msgs.append({"type": t, "enabled": v[key] == "1"})
    numbers = {"axi_optScale": "set_opt_scale", "axi_lagThreshold": "set_lag_threshold",
               "axi_minDist": "set_min_dist", "axi_penPosUp": "set_pen_up_pos",
               "axi_penDownMin": "set_pen_down_min", "axi_penDownMax": "set_pen_down_max",
               "axi_pressureUpdateRate": "set_pressure_update_rate",
               "axi_speedPenDown": "set_speed_pendown", "axi_speedPenUp": "set_speed_penup",
               "axi_accel": "set_accel",
               "axi_xTilt": "set_x_tilt", "axi_yTilt": "set_y_tilt"}
    for key, t in numbers.items():
        if key in v:
            n = _num(v[key], None)
            if n is not None:
                msgs.append({"type": t, "value": n})
    if "axi_oscPort" in v:
        msgs.append({"type": "set_osc_port", "port": int(_num(v["axi_oscPort"], 0))})
    if "axi_model" in v:
        msgs.append({"type": "set_model", "model": int(_num(v["axi_model"], 1))})
    if "axi_paperW" in v and "axi_paperH" in v:
        msgs.append({"type": "set_paper", "width": _num(v["axi_paperW"], 8.5),
                     "height": _num(v["axi_paperH"], 11.0)})
    # Last of the three: the layout places the drawing and the machine on a
    # sheet whose size and machine the two above have just settled.
    if "axi_layout" in v:
        try:
            msgs.append({"type": "set_layout", "layout": json.loads(v["axi_layout"])})
        except (TypeError, ValueError):
            pass
    for key, val in v.items():
        if key.startswith("axi_fx_en_"):
            msgs.append({"type": "set_effect_enabled", "name": key[len("axi_fx_en_"):],
                         "enabled": val == "1"})
        elif key.startswith("axi_fx_p_"):
            # "<effect>_<ATTR>", where both halves can contain underscores
            # (pressure_hatch, SAMPLE_EVERY) — the engine matches it against its
            # real effect list.
            msgs.append({"type": "set_effect_param_key", "key": key[len("axi_fx_p_"):],
                         "value": _num(val, 0.0)})
    return msgs
