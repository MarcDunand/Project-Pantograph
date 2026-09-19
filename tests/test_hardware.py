"""
Checks that need a real AxiDraw on USB. Skipped unless asked for:

    PANTOGRAPH_HARDWARE=1 uv run pytest tests/test_hardware.py

They connect to the machine (the pen lifts) but never move the carriage.
"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("PANTOGRAPH_HARDWARE") != "1",
                                reason="needs an AxiDraw; set PANTOGRAPH_HARDWARE=1")

from test_app import App  # noqa: E402


def motor_state():
    """The EBB's motor-enable pins: (0, 0) means both XY motors are off."""
    from plotink import ebb_motion, ebb_serial
    for _ in range(10):                          # the app may still be letting go of the port
        port = ebb_serial.openPort()
        if port is not None:
            try:
                return ebb_motion.query_enable_motors(port, False)
            finally:
                ebb_serial.closePort(port)
        time.sleep(0.5)
    pytest.fail("no AxiDraw found on USB")


def test_quitting_releases_the_motors(tmp_path):
    from plotink import ebb_motion, ebb_serial
    port = ebb_serial.openPort()
    ebb_motion.sendEnableMotors(port, 1, False)  # on, so "off" afterwards is the app's doing
    ebb_serial.closePort(port)
    assert all(motor_state())

    app = App(tmp_path, dry_run=False)
    assert app.wait_for("[axidraw] connected"), "\n".join(app.lines)
    time.sleep(1.0)
    assert app.quit() == 0
    assert any("XY motors disengaged" in l for l in app.lines)
    assert motor_state() == (0, 0)
