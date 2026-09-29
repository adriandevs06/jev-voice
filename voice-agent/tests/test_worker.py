"""The async Jev path: the mic thread must never wait on a decision.

No network and no key -- a fake Jev with a controllable delay stands in for the
live one, so the debounce, in-flight cap, staleness drop and late-fire path are
all exercised offline.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import Actions  # noqa: E402
from apps import Apps  # noqa: E402
from intent import Controller, NONE_APP  # noqa: E402
from jev import Jev, JevWorker  # noqa: E402
from test_intent import CATALOG, jev_answer  # noqa: E402

QS = {"q": {"type": "noul", "instructions": "?"}}


class FakeJev(Jev):
    """Answers after `delay` seconds from a table, counting real calls."""

    def __init__(self, table=None, delay=0.05, **kw):
        super().__init__(mode="replay", **kw)
        self.mode = "real"                 # take the live path without a key
        self.table = table or {}
        self.delay = delay
        self.calls = []
        self.started = threading.Event()

    def evaluate(self, state, questions):
        key = state["transcript"] if isinstance(state, dict) and "transcript" in state else str(state)
        self.calls.append(key)
        self.started.set()
        time.sleep(self.delay)
        return self.table.get(key)

    def cached(self, state, questions):
        return None                        # nothing on disk in these tests


# -- worker mechanics ---------------------------------------------------------

def test_cache_hit_answers_synchronously(tmp_path):
    jev = Jev(mode="replay", cache_dir=tmp_path)
    jev.seed({"transcript": "open chrome"}, QS, {"q": {"type": "noul", "noul": 0.9}})
    w = JevWorker(jev)
    assert w.submit({"transcript": "open chrome"}, QS)["q"]["noul"] == 0.9
    assert w.seq == 0                       # nothing was scheduled


def test_miss_schedules_and_calls_back(tmp_path):
    got = []
    jev = FakeJev({"hello": {"q": {"type": "noul", "noul": 0.7}}}, cache_dir=tmp_path)
    w = JevWorker(jev, on_answers=lambda seq, st, a: got.append((seq, st, a)), debounce_s=0.01)
    assert w.submit({"transcript": "hello"}, QS) is None      # returns at once
    assert w.drain()
    assert len(got) == 1 and got[0][2]["q"]["noul"] == 0.7
    assert jev.calls == ["hello"]


def test_debounce_keeps_only_the_newest(tmp_path):
    got = []
    jev = FakeJev({"open chrome now": {"q": {}}}, cache_dir=tmp_path)
    w = JevWorker(jev, on_answers=lambda seq, st, a: got.append(st["transcript"]), debounce_s=0.05)
    for t in ("open", "open chrome", "open chrome now"):
        w.submit({"transcript": t}, QS)
    assert w.drain()
    assert jev.calls == ["open chrome now"]                   # the two older ones never went out
    assert w.dropped["debounced"] == 2


def test_late_answer_is_delivered_and_counted(tmp_path):
    """A late answer is about a prefix of what is being said now; the controller
    decides whether it still applies, so the worker must not throw it away."""
    got = []
    jev = FakeJev({"old": {"q": {"type": "noul", "noul": 0.1}},
                   "new": {"q": {"type": "noul", "noul": 0.9}}}, delay=0.15, cache_dir=tmp_path)
    w = JevWorker(jev, on_answers=lambda seq, st, a: got.append(st["transcript"]), debounce_s=0.01)
    w.submit({"transcript": "old"}, QS)
    jev.started.wait(2)                                       # "old" is in flight
    w.submit({"transcript": "new"}, QS)                       # newer text arrives
    assert w.drain(timeout=5)
    assert got == ["old", "new"] and w.dropped["stale"] == 1


def test_inflight_cap_queues_instead_of_dropping(tmp_path):
    """At the cap, the newest question waits for a free slot -- it is not lost."""
    jev = FakeJev({}, delay=0.3, cache_dir=tmp_path)
    w = JevWorker(jev, debounce_s=0.01, max_inflight=1)
    w.submit({"transcript": "a"}, QS)
    jev.started.wait(2)
    w.submit({"transcript": "b"}, QS)
    w._flush()                                                 # would exceed the cap
    assert w.dropped["full"] >= 1
    assert w.drain(timeout=5)
    assert jev.calls == ["a", "b"]                             # b was asked once a slot freed


def test_worker_swallows_a_throwing_client(tmp_path):
    class Boom(FakeJev):
        def evaluate(self, state, questions):
            raise RuntimeError("upstream is down")

    got = []
    w = JevWorker(Boom({}, cache_dir=tmp_path), on_answers=lambda *a: got.append(a), debounce_s=0.01)
    w.submit({"transcript": "x"}, QS)
    assert w.drain() and got == []                             # no decision, no crash


# -- the controller path ------------------------------------------------------

def _world(tmp_path, table, delay=0.05):
    apps = Apps(catalog=CATALOG, cache=tmp_path / "apps.json")
    actions = Actions(apps, dry_run=True, foreground=lambda: "Notepad", stacks={})
    jev = FakeJev(table, delay=delay, cache_dir=tmp_path / "c")
    late, handed = [], []
    ctl = Controller(actions, jev, to_brain=handed.append, app_names=apps.names(),
                     on_fire=late.extend)
    ctl.worker.debounce_s = 0.01
    return actions, jev, ctl, late, handed


def test_partial_returns_immediately_then_fires_late(tmp_path):
    seg = "bring up my code editor"
    # The delay is deliberately far longer than any plausible scheduling hiccup
    # on a loaded machine, so the assertion below tests the property (the call
    # does not wait for Jev) rather than the absolute speed of this laptop.
    actions, jev, ctl, late, handed = _world(
        tmp_path, {seg: jev_answer(0.95, "open_app", app="Cursor")}, delay=3.0)

    t0 = time.monotonic()
    assert ctl.on_partial(seg) == []                           # nothing yet...
    assert time.monotonic() - t0 < 1.0                         # ...and it did not wait 3 s

    assert ctl.worker.drain(timeout=15)
    time.sleep(0.1)
    assert [r["app"] for r in late] == ["Cursor"]              # the answer fired it
    assert [r["action"] for r in actions.log] == ["open_app"]
    assert ctl.on_partial(seg) == [] and len(actions.log) == 1  # never twice


def test_answer_that_lands_within_the_final_wait_is_used(tmp_path):
    """2026-09-24: Jev had 'open google pro' right, ~300 ms after the last
    word -- and the final had already handed the sentence to the brain. The
    final now waits up to FINAL_WAIT_S for it."""
    seg = "i need the spreadsheet thing"
    actions, jev, ctl, late, handed = _world(
        tmp_path, {seg: jev_answer(0.95, "open_app", app="Cursor")}, delay=0.3)
    ctl.on_final(seg)
    assert [r["app"] for r in actions.log] == ["Cursor"] and handed == []


def test_late_answer_is_ignored_once_the_utterance_ended(tmp_path):
    from intent import FINAL_WAIT_S
    seg = "i need the spreadsheet thing"                       # no verb the rules know
    actions, jev, ctl, late, handed = _world(
        tmp_path, {seg: jev_answer(0.95, "open_app", app="Cursor")}, delay=FINAL_WAIT_S + 0.5)
    ctl.on_partial(seg)
    ctl.on_final(seg)                                          # rules cannot place it -> brain
    assert ctl.worker.drain(timeout=5)
    time.sleep(0.05)
    assert late == [] and handed == [seg]                      # no action after the fact
    assert actions.log == []


def test_answers_are_reused_within_an_utterance_then_cleared(tmp_path):
    seg = "open chrome"
    actions, jev, ctl, late, handed = _world(
        tmp_path, {seg: jev_answer(0.99, "open_app", app="Google Chrome")})
    ctl.on_partial(seg)
    assert ctl.worker.drain(timeout=5)
    time.sleep(0.05)
    ctl.on_partial(seg)
    assert jev.calls == [seg]                                  # second partial reused the answer
    ctl.on_final(seg)
    assert ctl.answers == {}                                   # a new utterance starts clean


def test_prefix_answer_fires_while_the_speaker_keeps_talking(tmp_path):
    """The answer for 'pull up the calculator' lands after the transcript has
    become '...calculator for me'. It must still fire -- that is the whole
    mid-sentence behaviour."""
    judged = "pull up the calculator"
    table = {judged: jev_answer(0.95, "open_app", app="Calculator")}
    apps = Apps(catalog=CATALOG + [{"name": "Calculator", "id": "calc"}], cache=tmp_path / "a.json")
    actions = Actions(apps, dry_run=True, foreground=lambda: "Notepad", stacks={})
    late = []
    jev = FakeJev(table, delay=0.3, cache_dir=tmp_path / "c")
    ctl = Controller(actions, jev, to_brain=lambda t: None, app_names=apps.names(),
                     on_fire=late.extend)
    ctl.worker.debounce_s = 0.01
    ctl.on_partial(judged)                                    # asks Jev about this text
    assert jev.started.wait(5)                                # ...the question is in flight
    ctl.on_partial(judged + " for")                           # and the speaker keeps going
    ctl.on_partial(judged + " for me")
    assert ctl.worker.drain(timeout=10)
    time.sleep(0.1)
    assert [r["app"] for r in late] == ["Calculator"]
    assert "on 'pull up the calculator'" in late[0]["why"]


def test_prefix_answer_does_not_fire_when_the_target_changed(tmp_path):
    """'open paint' was complete -- but the speaker said '3d' next. The grown
    segment resolves to a different app, so the prefix answer must not fire
    the wrong one."""
    judged = "open paint"
    cat = CATALOG + [{"name": "Paint", "id": "p"}, {"name": "Paint 3D", "id": "p3"}]
    apps = Apps(catalog=cat, cache=tmp_path / "a.json")
    actions = Actions(apps, dry_run=True, foreground=lambda: "Notepad", stacks={})
    late = []
    table = {judged: jev_answer(0.95, "open_app", app="Paint")}
    jev = FakeJev(table, delay=0.3, cache_dir=tmp_path / "c")
    ctl = Controller(actions, jev, to_brain=lambda t: None, app_names=apps.names(),
                     on_fire=late.extend)
    ctl.worker.debounce_s = 0.01
    ctl.on_partial(judged)
    assert jev.started.wait(5)
    ctl.on_partial(judged + " 3d")
    assert ctl.worker.drain(timeout=10)
    time.sleep(0.1)
    assert late == [] and actions.log == []


def test_prefix_fires_through_a_trailing_connector(tmp_path):
    judged = "bring up spotify real quick"
    apps = Apps(catalog=CATALOG, cache=tmp_path / "a.json")
    actions = Actions(apps, dry_run=True, foreground=lambda: "Notepad", stacks={})
    late = []
    jev = FakeJev({judged: jev_answer(0.92, "open_app", app="Spotify")}, delay=0.3, cache_dir=tmp_path / "c")
    ctl = Controller(actions, jev, to_brain=lambda t: None, app_names=apps.names(), on_fire=late.extend)
    ctl.worker.debounce_s = 0.01
    ctl.on_partial(judged)
    assert jev.started.wait(5)
    ctl.on_partial(judged + " and")                            # dangling connector
    assert ctl.worker.drain(timeout=10)
    time.sleep(0.1)
    assert [r["app"] for r in late] == ["Spotify"]
