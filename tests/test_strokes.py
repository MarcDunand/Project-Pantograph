"""
Stroke boundaries and pressure handling in listen_to_idraw, driven through the
real OSC handlers in iDraw's message order. No plotter, no browser: plot
commands collect in the queue and preview broadcasts are captured.
"""
import socket
import threading
import time

from pythonosc.udp_client import SimpleUDPClient

from conftest import kinds, pen_ups, send_block, send_point


def pause(L, seconds=1.0):
    """Pretend the Pencil has been still this long, then let the rest watchdog look."""
    L.state["_last_point_time"] -= seconds
    L._maybe_rest_pen()


# ── stroke boundaries ────────────────────────────────────────────────────────

def test_state_block_splits_strokes(engine):
    send_block(engine); [send_point(engine, 100 + i * 5, 400) for i in range(5)]
    send_block(engine); [send_point(engine, 100 + i * 5, 500) for i in range(5)]
    k = kinds(engine)
    assert k.count("moveto") == 2 and k.count("penup") == 1 and pen_ups(engine) == 1


def test_block_with_no_stroke_open_does_nothing(engine):
    send_block(engine); send_block(engine); send_block(engine)
    assert kinds(engine) == [] and pen_ups(engine) == 0


def test_pause_rests_the_pen_but_keeps_the_stroke(engine):
    send_block(engine); [send_point(engine, 100 + i * 5, 400) for i in range(3)]
    pause(engine)
    assert kinds(engine)[-1] == "penup"             # rested while the plotter is behind
    [send_point(engine, 120 + i * 5, 400) for i in range(3)]
    k = kinds(engine)
    assert k.count("penup") == 0                    # the rest was taken back out of the queue
    assert k.count("moveto") == 1 and pen_ups(engine) == 0


def test_rest_already_executed_lowers_the_pen_again(engine):
    send_block(engine); [send_point(engine, 100 + i * 5, 400) for i in range(3)]
    pause(engine)
    engine._plot_deque.clear()                      # the plotter already ran everything
    [send_point(engine, 120 + i * 5, 400) for i in range(3)]
    assert kinds(engine) == ["pendown", "lineto", "lineto", "lineto"]
    assert pen_ups(engine) == 0


def test_tap_dwells_once_and_closes_on_next_block(engine):
    send_block(engine); send_point(engine, 200, 200)
    pause(engine)
    send_block(engine)
    k = kinds(engine)
    assert k.count("dot_dwell") == 1 and k[-1] == "penup" and pen_ups(engine) == 1


def test_imported_strokes_keep_their_boundaries(engine):
    rec = {"strokes": [
        {"canvasWidth": 440.0, "canvasHeight": 956.0, "points": [[0, 100, 100, 2.0], [0.01, 110, 100, 2.0]]},
        {"canvasWidth": 440.0, "canvasHeight": 956.0, "points": [[0.02, 100, 300, 2.0], [0.03, 110, 300, 2.0]]},
    ]}
    engine._feed_strokes(rec["strokes"])        # the feeding half; no plotter thread here
    k = kinds(engine)
    assert k.count("moveto") == 2 and k.count("penup") == 2 and pen_ups(engine) == 2


def _wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_stroke_drawn_while_an_import_feeds_is_plotted_after_it(engine):
    """Held back while the import feeds, then queued behind it — never dropped."""
    pts = [[i * 0.01, 100.0 + i, 100.0, 2.0] for i in range(40)]
    engine.start_import({"strokes": [{"canvasWidth": 440.0, "canvasHeight": 956.0,
                                      "points": pts}]})
    send_block(engine)
    for i in range(10):
        send_point(engine, 200.0 + i, 400.0)
    assert _wait_for(lambda: engine._import_state["phase"] == "finishing"), "never finished feeding"
    ys = {round(q.cmd[3], 2) for q in engine._plot_deque if q.cmd[1] == "lineto"}
    assert len(ys) > 1, "the live stroke was captured but never queued"


def test_a_stroke_drawn_while_the_machine_catches_up_is_plotted(engine):
    """
    Feeding takes seconds and plotting takes minutes. A stroke drawn during that
    catching-up has nothing to collide with, so it queues itself straight away —
    the bug was that it stayed held back, shown on the page but never plotted.
    """
    pts = [[i * 0.01, 100.0 + i, 100.0, 2.0] for i in range(20)]
    engine.start_import({"strokes": [{"canvasWidth": 440.0, "canvasHeight": 956.0,
                                      "points": pts}]})
    # No plotter thread runs here, so the queue never drains and the import sits
    # in "finishing" — exactly the window the drawing is made in.
    assert _wait_for(lambda: engine._import_state["phase"] == "finishing"), "never finished feeding"
    assert not engine._import_feeding.is_set(), "capture should stop when the feed does"
    before = len(engine._plot_deque)
    send_block(engine)
    for i in range(10):
        send_point(engine, 200.0 + i, 400.0)
    engine._end_stroke()
    assert len(engine._plot_deque) > before, "the live stroke never reached the queue"
    assert not engine._live_pending, "nothing should still be held back"


def test_udp_fast_strokes_arrive_in_order(engine):
    """Real UDP through the single-threaded server: 3 fast strokes, 120 points, in order."""
    srv = engine.BlockingOSCUDPServer(("127.0.0.1", 0), engine._build_dispatcher())
    srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = SimpleUDPClient("127.0.0.1", srv.server_address[1])

    def udp_block():
        for addr, val in [("/r", 0.0), ("/g", 0.0), ("/b", 0.0), ("/a", 1.0), ("/pen", 1.0),
                          ("/canvasWidth", 440.0), ("/canvasHeight", 956.0),
                          ("/drawingWidth", 2.68), ("/eraserWidth", 0.0)]:
            c.send_message(addr, val)

    try:
        for s in range(3):
            udp_block()
            for i in range(40):
                c.send_message("/x", 50.0 + i * 3)
                c.send_message("/y", 100.0 + s * 200)
                c.send_message("/pressure", 2.0)
        udp_block()                                 # closes stroke 3
        deadline = time.time() + 5
        while pen_ups(engine) < 3 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        srv.shutdown()
        srv.server_close()

    pts = [m for m in engine.sent if m["type"] == "point"]
    assert kinds(engine).count("moveto") == 3 and pen_ups(engine) == 3 and len(pts) == 120
    for s in range(3):
        xs = [p["x"] for p in pts[s * 40:(s + 1) * 40]]
        assert xs == sorted(xs)


# ── no-pressure input ────────────────────────────────────────────────────────

def _pressures(L, kind):
    return [round(q.cmd[2] if kind == "pendown" else q.cmd[4], 3)
            for q in L._plot_deque if q.cmd[1] == kind]


def _stroke(L, raws):
    send_block(L)
    for i, r in enumerate(raws):
        send_point(L, 100 + i * 5, 400, r)
    send_block(L)                                   # the next stroke closes this one


def test_finger_stroke_plots_at_half_pressure(engine):
    _stroke(engine, [1.0] * 10)
    assert _pressures(engine, "pendown") == [0.5]
    assert _pressures(engine, "lineto") == [0.5] * 9


def test_short_finger_stroke_plots_when_it_ends(engine):
    _stroke(engine, [1.0] * 3)
    assert _pressures(engine, "pendown") == [0.5]
    assert _pressures(engine, "lineto") == [0.5] * 2


def test_finger_tap_is_a_dot(engine):
    _stroke(engine, [1.0])
    assert _pressures(engine, "pendown") == [0.5] and "dot_dwell" in kinds(engine)


def test_glitch_inside_real_pressure_is_interpolated(engine):
    # Points are emitted on /y, before their /pressure arrives, so each point
    # carries the previous burst's pressure (the deferred pressure-lag fix).
    _stroke(engine, [2.0, 2.0, 1.0, 1.0, 2.0, 2.0, 2.0])
    assert 0.5 not in _pressures(engine, "lineto")


def test_long_placeholder_run_inside_real_pressure_plots_at_half(engine):
    _stroke(engine, [2.0, 2.0] + [1.0] * 6 + [2.0, 2.0])
    assert _pressures(engine, "lineto").count(0.5) >= 5


def test_lag_is_drawing_time_the_plotter_still_owes(engine):
    """
    "Behind by" is the gap between drawing a mark and the plotter drawing it,
    with the time nobody was drawing taken out: it grows while the pen runs
    ahead, falls as the plotter catches up, and is 0 once it's caught up.
    """
    # Three seconds of drawing, one point per 0.1 s (so no gap counts as a pause).
    for i in range(30):
        engine._pen_clock_tick(i * 0.1)
        engine._plot_deque.append(engine.Queued((i * 0.1, "lineto", i, i, 1.0)))
    assert engine.current_lag() == 2.9              # the queue head is the first mark

    for _ in range(15):                             # the plotter draws half of them
        engine._plot_deque.popleft()
    assert engine.current_lag() == 1.4

    # Nobody draws for a while: the pen clock stops, so the lag doesn't grow.
    engine._pen_clock_tick(30.0)
    assert engine.current_lag() == 1.4

    engine._plot_deque.clear()
    assert engine.current_lag() == 0.0
