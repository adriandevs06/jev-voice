"""Double-tap Alt: what counts, what never does, and the server side of it."""
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hotkey import DoubleTap  # noqa: E402

ALT, TAB, CTRL, X = 0xA4, 0x09, 0xA2, 0x58


def play(events, tap=None):
    """events: (vk, down, t_ms[, injected]) -> list of t where it fired."""
    tap = tap or DoubleTap()
    fired = []
    for e in events:
        vk, down, t = e[:3]
        injected = e[3] if len(e) > 3 else False
        if tap.feed(vk, down, injected, t):
            fired.append(t)
    return fired


def taps(*starts, hold=80):
    out = []
    for s in starts:
        out += [(ALT, True, s), (ALT, False, s + hold)]
    return out


def test_double_tap_fires_once():
    assert play(taps(0, 250)) == [330]


def test_single_tap_does_nothing():
    assert play(taps(0)) == []


def test_too_slow_is_two_single_taps():
    assert play(taps(0, 700)) == []


def test_holding_alt_is_not_a_tap():
    assert play(taps(0, 250, hold=500)) == []


def test_alt_tab_twice_never_toggles():
    ev = []
    for s in (0, 300):
        ev += [(ALT, True, s), (TAB, True, s + 30), (TAB, False, s + 60), (ALT, False, s + 90)]
    assert play(ev) == []


def test_a_key_between_taps_breaks_the_pair():
    ev = taps(0) + [(X, True, 150), (X, False, 170)] + taps(250)
    assert play(ev) == []


def test_ctrl_held_across_both_taps_does_not_count():
    ev = [(CTRL, True, 0)] + taps(50, 300) + [(CTRL, False, 500)]
    assert play(ev) == []


def test_injected_alt_taps_are_ignored():
    """actions.py taps Alt for the focus-steal trick; the agent must never
    be able to toggle its own mic."""
    ev = [(ALT, True, 0, True), (ALT, False, 50, True), (ALT, True, 200, True), (ALT, False, 250, True)]
    assert play(ev) == []


def test_auto_repeat_while_holding_is_one_press():
    ev = [(ALT, True, 0), (ALT, True, 30), (ALT, True, 60), (ALT, False, 90)] + taps(250)
    assert play(ev) == [330]


def test_four_taps_toggle_twice():
    assert play(taps(0, 250, 1500, 1750)) == [330, 1830]


def test_triple_tap_toggles_once():
    assert play(taps(0, 250, 500)) == [330]


# -- server: token-gated toggle, console-only socket --------------------------

class _Ears:
    muted = True


class _Speaker:
    def shutup(self):
        pass


def _client(monkeypatch):
    import server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "ears", _Ears())
    monkeypatch.setattr(server, "speaker", _Speaker())
    monkeypatch.setattr(server, "_beep", lambda *a: None)
    server.STATE["muted"] = True
    return server, TestClient(server.app)          # no `with`: startup (models, brain) never runs


def test_toggle_needs_the_token(monkeypatch):
    server, c = _client(monkeypatch)
    assert c.post("/toggle").status_code == 403
    assert c.post("/toggle", headers={"X-Voice-Token": "guess"}).status_code == 403
    assert server.STATE["muted"] is True


def test_toggle_flips_the_mic(monkeypatch):
    server, c = _client(monkeypatch)
    h = {"X-Voice-Token": server.HOTKEY_TOKEN}
    assert c.post("/toggle", headers=h).json() == {"muted": False}
    time.sleep(0.4)                                 # the mic opens after the beep
    assert server.ears.muted is False
    assert c.post("/toggle", headers=h).json() == {"muted": True}
    assert server.ears.muted is True               # closing is immediate


def test_close_during_the_open_beep_wins(monkeypatch):
    server, c = _client(monkeypatch)
    monkeypatch.setattr(server, "_beep", lambda *a: time.sleep(0.2))
    h = {"X-Voice-Token": server.HOTKEY_TOKEN}
    c.post("/toggle", headers=h)
    c.post("/toggle", headers=h)                    # before the open finished
    time.sleep(0.6)
    assert server.ears.muted is True


def test_socket_refuses_other_origins(monkeypatch):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    server, c = _client(monkeypatch)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    with c.websocket_connect("/ws", headers={"origin": f"http://127.0.0.1:{server.PORT}"}) as ws:
        assert '"muted": true' in ws.receive_text()


# -- ears: closing the mic mid-sentence finishes the sentence -----------------

def test_closing_mid_sentence_acts_on_what_was_said():
    from stream_ears import FRAME, SAMPLE_RATE, StreamEars
    finals, done = [], threading.Event()

    def on_final(text, provisional=False):
        finals.append((text, provisional))
        if not provisional:
            done.set()

    ears = StreamEars(lambda t: None, on_final, lambda s: None, load_models=False)
    ears.fast = ears.big = object()
    ears._decode = lambda audio, model: "open chrome"
    ears.muted, ears.running = False, True
    threading.Thread(target=ears._loop, daemon=True).start()
    loud = (0.2 * np.sin(np.arange(FRAME) / 3)).astype("float32").reshape(-1, 1)
    for _ in range(int(0.9 * SAMPLE_RATE / FRAME)):   # 0.9 s of speech, no pause
        ears.frames.put(loud)
    time.sleep(0.2)
    ears.muted = True                                   # double-tap Alt
    ears.frames.put(loud)
    assert done.wait(5)
    ears.running = False
    assert finals == [("open chrome", True), ("open chrome", False)]


def test_a_blip_under_the_minimum_is_still_dropped():
    from stream_ears import FRAME, StreamEars
    finals = []
    ears = StreamEars(lambda t: None, lambda *a: finals.append(a), lambda s: None, load_models=False)
    ears._decode = lambda audio, model: "x"
    ears.muted, ears.running = False, True
    threading.Thread(target=ears._loop, daemon=True).start()
    loud = (0.2 * np.ones(FRAME)).astype("float32").reshape(-1, 1)
    for _ in range(3):                                  # 90 ms: a cough, a key click
        ears.frames.put(loud)
    time.sleep(0.1)
    ears.muted = True
    ears.frames.put(loud)
    time.sleep(0.3)
    ears.running = False
    assert finals == []


def test_hotkey_process_posts_the_token():
    import http.server
    import hotkey
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"], seen["token"] = self.path, self.headers.get("X-Voice-Token")
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    hotkey.toggle(srv.server_address[1], "tok123")
    srv.server_close()
    assert seen == {"path": "/toggle", "token": "tok123"}


# -- the listening bar --------------------------------------------------------

def test_bar_describes_receipts():
    from overlay import describe
    assert describe({"action": "open_app", "ok": True, "app": "Google Chrome"}) == "✓ Opened Google Chrome"
    assert describe({"action": "web_search", "ok": True, "query": "cheap flights"}) == "✓ Searched “cheap flights”"
    assert describe({"action": "press_keys", "ok": False, "combo": "ctrl+t", "error": "no window"}) \
        == "✗ Pressed ctrl+t · no window"
    assert describe({"action": "multi-step", "ok": False, "held": "not signed in"}) == "Held: not signed in"


def test_bar_keeps_the_end_of_a_long_sentence():
    from overlay import tail
    assert tail("open chrome", 20) == "open chrome"
    t = tail("open chrome and search cheap flights to miami", 20)
    assert len(t) == 20 and t.endswith("to miami") and t.startswith("…")


def test_bar_level_scale():
    from overlay import level_to_bar
    assert level_to_bar(0.0, 0.012) == 0.0
    assert level_to_bar(0.012, 0.012) == 0.25          # at the gate: low
    assert level_to_bar(1.0, 0.012) == 1.0


def test_server_forwards_to_the_bar_without_blocking(monkeypatch):
    import queue as q
    import server
    sent = q.Queue(maxsize=2)
    monkeypatch.setattr(server, "_overlay_q", sent)
    monkeypatch.setattr(server, "overlay_proc", object())
    monkeypatch.setitem(server.loop_ref, "loop", None)         # no console clients
    server.broadcast("partial", "open chr")
    server.broadcast("claude", "long reply")            # not a bar kind
    server.broadcast("level", {"rms": 0.02, "gate": 0.012})
    server.broadcast("level", {"rms": 0.02, "gate": 0.012})   # queue full: dropped, no block
    assert sent.qsize() == 2 and '"partial"' in sent.get()


def test_ears_report_levels_only_while_open():
    from stream_ears import FRAME, StreamEars
    got = []
    ears = StreamEars(lambda t: None, lambda *a: None, lambda s: None, load_models=False,
                      on_level=lambda rms, gate: got.append(rms))
    ears.running = True
    threading.Thread(target=ears._loop, daemon=True).start()
    quiet = np.zeros((FRAME, 1), dtype="float32")
    for _ in range(10):
        ears.frames.put(quiet)                          # muted: nothing reported
    time.sleep(0.2)
    assert got == []
    ears.muted = False
    for _ in range(10):
        ears.frames.put(quiet)
    time.sleep(0.2)
    ears.running = False
    assert len(got) == 5                                # every 2nd frame


# -- endpointing with a video playing ------------------------------------------

def _ears_with(text_fn):
    from stream_ears import StreamEars
    finals = []
    ears = StreamEars(lambda t: None, lambda text, provisional=False: finals.append((text, provisional)),
                      lambda s: None, load_models=False)
    ears.fast = ears.big = object()
    ears._decode = lambda audio, model: text_fn()
    return ears, finals


def test_sentence_ends_when_the_words_stop_even_if_the_room_is_loud(monkeypatch):
    """2026-09-24: YouTube audio kept the energy above the gate, the 750 ms
    hang never came, and 'open chrome' waited 14 s for the mic to close."""
    import stream_ears
    from stream_ears import FRAME, SAMPLE_RATE
    monkeypatch.setattr(stream_ears, "STALL_END_S", 0.5)
    ears, finals = _ears_with(lambda: "open chrome")
    ears.muted, ears.running = False, True
    threading.Thread(target=ears._loop, daemon=True).start()
    loud = (0.2 * np.sin(np.arange(FRAME) / 3)).astype("float32").reshape(-1, 1)
    t0 = time.time()
    while time.time() - t0 < 4 and not finals:              # real-time loud audio, no pause
        ears.frames.put(loud)
        time.sleep(FRAME / SAMPLE_RATE)
    ears.running = False
    assert finals and finals[0] == ("open chrome", True)
    assert time.time() - t0 < 3                               # not the 30 s cap, not the mic closing


def test_still_talking_does_not_end_the_sentence(monkeypatch):
    import stream_ears
    from stream_ears import FRAME, SAMPLE_RATE
    monkeypatch.setattr(stream_ears, "STALL_END_S", 0.5)
    words = iter(f"open chrome and search for {'x ' * i}" for i in range(1000))
    ears, finals = _ears_with(lambda: next(words))            # every decode adds a word
    ears.muted, ears.running = False, True
    threading.Thread(target=ears._loop, daemon=True).start()
    loud = (0.2 * np.sin(np.arange(FRAME) / 3)).astype("float32").reshape(-1, 1)
    t0 = time.time()
    while time.time() - t0 < 2.0:
        ears.frames.put(loud)
        time.sleep(FRAME / SAMPLE_RATE)
    ears.running = False
    assert finals == []


def test_gate_is_retuned_to_the_room_when_the_mic_opens():
    from stream_ears import FRAME, GATE_MAX, GATE_MIN
    ears, _ = _ears_with(lambda: "")
    logs = []
    ears.on_log = logs.append
    ears.running = True
    threading.Thread(target=ears._loop, daemon=True).start()
    room = (0.02 * np.sin(np.arange(FRAME) / 2)).astype("float32").reshape(-1, 1)   # rms ~0.014
    for _ in range(60):
        ears.frames.put(room)                                 # muted: just listening to the room
    time.sleep(0.3)
    ears.muted = False
    ears.frames.put(room)
    time.sleep(0.3)
    ears.running = False
    assert GATE_MIN < ears.threshold <= GATE_MAX and logs and "gate" in logs[0]
