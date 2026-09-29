"""The console's fast path end to end: FakeEars partials -> server.on_partial /
server.on_final -> controller -> dry-run actions, leftovers -> a stub brain.
No audio, no uvicorn, no Claude process."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from actions import Actions  # noqa: E402
from apps import Apps  # noqa: E402
from intent import Controller  # noqa: E402
from jev import Jev  # noqa: E402
from stream_ears import FakeEars  # noqa: E402

CATALOG = [{"name": "Google Chrome", "id": "Chrome"}, {"name": "Notepad", "id": "np"}]


class StubBrain:
    def __init__(self):
        self.sent = []
        self.models = []
        self.model = "haiku"

    def send(self, text):
        self.sent.append(text)

    def set_model(self, model):
        if model != self.model:
            self.models.append(model)
            self.model = model


def _wire(tmp_path):
    events = []
    server.broadcast = lambda kind, payload: events.append((kind, payload))
    server.brain = StubBrain()
    apps = Apps(catalog=CATALOG, cache=tmp_path / "apps.json")
    actions = Actions(apps, dry_run=True)
    server.ctl = Controller(actions, Jev(mode="replay", cache_dir=tmp_path / "jev"),
                            to_brain=server.handle_user_text, app_names=apps.names())
    return events


def test_chain_then_leftover_to_brain(tmp_path):
    events = _wire(tmp_path)
    FakeEars(server.on_partial, server.on_final).say(
        "open chrome, then go to x dot com, then rename the last three screenshots")
    kinds = [k for k, _ in events]
    assert kinds.count("partial") >= 8
    actions = [p["action"] for k, p in events if k == "action"]
    assert actions == ["open_app", "open_url"]
    assert server.brain.sent == ["rename the last three screenshots"]
    assert ("you", "rename the last three screenshots") in events     # the brain-bound part is echoed


def test_all_handled_without_brain(tmp_path):
    events = _wire(tmp_path)
    FakeEars(server.on_partial, server.on_final).say("open notes and open chrome")
    assert [p["action"] for k, p in events if k == "action"] == ["open_app", "open_app"]
    assert server.brain.sent == []
    assert events[-2:] == [("you", "open notes and open chrome"),
                           ("state", {"status": "idle", "muted": server.STATE["muted"]})]


def test_chatter_goes_nowhere(tmp_path):
    events = _wire(tmp_path)
    FakeEars(server.on_partial, server.on_final).say("okay nice cool")
    assert [k for k, _ in events if k == "action"] == []
    assert server.brain.sent == []


def test_fast_path_off_falls_back_to_brain(tmp_path):
    events = _wire(tmp_path)
    server.ctl = None
    server.on_final("open chrome")
    assert server.brain.sent == ["open chrome"]
