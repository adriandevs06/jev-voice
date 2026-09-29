"""Per-request model routing for the brain (router.py)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import router  # noqa: E402
from jev import Jev  # noqa: E402


class FakeJev(Jev):
    def __init__(self, label, conf=0.9, delay=0.0, **kw):
        super().__init__(mode="replay", **kw)
        self.mode = "real"
        self.label, self.conf, self.delay, self.calls = label, conf, delay, 0

    def evaluate(self, state, questions):
        self.calls += 1
        time.sleep(self.delay)
        others = {k: (1 - self.conf) / 3 for k in router.KINDS if k != self.label}
        return {"kind": {"type": "choice", "choice": self.label, "probabilities": {self.label: self.conf, **others},
                         "confidence": self.conf}}

    def choice(self, answers, qid):
        a = answers[qid]
        return a["choice"], a["probabilities"][a["choice"]], a["confidence"]


RESEARCH = "research: finding, comparing or summarising information from the web"
STEPS = "a task with several steps across apps or files, or one that needs careful judgment"


def test_research_words_go_smart_without_jev(tmp_path):
    j = FakeJev(RESEARCH, cache_dir=tmp_path)
    for said in ("look for new AI tools for making UGC videos", "find me the best cheap mics",
                 "compare CapCut and Premiere for short ads", "what's new with Jev this week"):
        assert router.route(said, j)[0] == "smart", said
    assert j.calls == 0


def test_screen_verbs_go_fast_without_jev(tmp_path):
    j = FakeJev(STEPS, cache_dir=tmp_path)
    for said in ("click the third video", "okay so open the settings and turn on dark mode",
                 "scroll to the comments", "can you play the next one"):
        assert router.route(said, j)[0] == "fast", said
    assert j.calls == 0


def test_jev_decides_the_rest(tmp_path):
    j = FakeJev(STEPS, conf=0.85, cache_dir=tmp_path)
    tier, why = router.route("reply to mike on telegram that i'm running late", j)
    assert tier == "smart" and why.startswith("jev") and j.calls == 1


def test_unsure_or_slow_jev_falls_back_to_fast(tmp_path):
    assert router.route("reply to mike", FakeJev(STEPS, conf=0.4, cache_dir=tmp_path))[0] == "fast"
    t0 = time.time()
    assert router.route("reply to mike", FakeJev(STEPS, delay=2.0, cache_dir=tmp_path), wait_s=0.2)[0] == "fast"
    assert time.time() - t0 < 1.0                        # never waits on a slow Jev


def test_no_jev_means_fast():
    assert router.route("reply to mike", None) == ("fast", "default")


def test_look_for_is_no_longer_an_instant_google_tab(tmp_path):
    from apps import Apps
    from intent import Rules
    from test_intent import CATALOG
    r = Rules(Apps(catalog=CATALOG, cache=tmp_path / "a.json"))
    assert r.parse("look for new AI tools for me").intent != "web_search"
    assert r.parse("search for cheap flights").intent == "web_search"   # still instant


def test_server_switches_model_per_request(monkeypatch):
    import server
    from test_server_fastpath import StubBrain
    b = StubBrain()
    monkeypatch.setattr(server, "brain", b)
    monkeypatch.setattr(server, "ctl", None)
    monkeypatch.setattr(server, "broadcast", lambda *a: None)
    monkeypatch.setattr(server, "set_state", lambda *a: None)
    server.handle_user_text("look for new AI tools for making UGC videos")
    server.handle_user_text("click the third video")
    assert b.models == [server.BRAIN_SMART, server.BRAIN_MODEL]
    assert b.sent == ["look for new AI tools for making UGC videos", "click the third video"]


def test_brain_set_model_writes_a_control_request():
    import io
    import json
    import threading
    import server
    b = server.Brain.__new__(server.Brain)
    b.lock, b.model = threading.Lock(), "haiku"

    class P:
        stdin = io.StringIO()
    b.proc = P()
    b.set_model("sonnet")
    b.set_model("sonnet")                                  # no-op: already there
    lines = [json.loads(x) for x in P.stdin.getvalue().splitlines()]
    assert len(lines) == 1 and lines[0]["type"] == "control_request"
    assert lines[0]["request"] == {"subtype": "set_model", "model": "sonnet"}
