"""End-to-end tests for the voice decision layer at zero cost.

No network, no desktop: the app catalog is a fixture, actions run dry, and Jev
runs in replay mode against answers seeded here (the same JSON shape the
gateway returns). Run:  python -m pytest tests -q
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from actions import Actions, normalise_url  # noqa: E402
from apps import Apps  # noqa: E402
from intent import (Controller, INTENTS, INTENT_BY_KEY, NONE_APP, Rules,  # noqa: E402
                    split_segments, questions)
from jev import Jev  # noqa: E402

CATALOG = [
    {"name": "Google Chrome", "id": "Chrome"},
    {"name": "Notepad", "id": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
    {"name": "Camera", "id": "Microsoft.WindowsCamera_8wekyb3d8bbwe!App"},
    {"name": "Claude", "id": "Claude_pzs8sxrjxfjjc!Claude"},
    {"name": "Cursor", "id": "Anysphere.Cursor"},
    {"name": "Windows Terminal", "id": "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"},
    {"name": "File Explorer", "id": "Microsoft.Windows.Explorer"},
    {"name": "7-Zip Help", "id": "x\\7-zip.chm"},
    {"name": "Spotify", "id": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"},
]


def jev_answer(ready, intent_key, app=None, irreversible=0.02, p_intent=0.93, p_app=0.9,
               command=0.97):
    """Build an `answers` dict in TypeSafe's wire shape (docs.typesafe.ai/api)."""
    label = INTENT_BY_KEY[intent_key]
    intent_probs = {k: 0.01 for k in INTENTS}
    intent_probs[label] = p_intent
    a = {
        "command": {"type": "noul", "noul": command},
        "ready": {"type": "noul", "noul": ready},
        "intent": {"type": "choice", "choice": label, "probabilities": intent_probs,
                   "confidence": p_intent},
        "irreversible": {"type": "noul", "noul": irreversible},
    }
    if app:
        a["app"] = {"type": "choice", "choice": app, "probabilities": {app: p_app}, "confidence": p_app}
    else:
        a["app"] = {"type": "choice", "choice": NONE_APP, "probabilities": {NONE_APP: 0.8},
                    "confidence": 0.8}
    return a


@pytest.fixture
def world(tmp_path):
    apps = Apps(catalog=CATALOG, cache=tmp_path / "apps.json")
    # a fixed foreground: the real one made search results depend on whether
    # a YouTube window happened to be focused while the tests ran
    actions = Actions(apps, dry_run=True, foreground=lambda: "Notepad")
    jev = Jev(mode="replay", cache_dir=tmp_path / "jev")
    handed = []
    ctl = Controller(actions, jev, to_brain=handed.append, app_names=apps.names())
    return apps, actions, jev, ctl, handed


# -- apps ------------------------------------------------------------------

def test_resolve_aliases_and_fuzzy(world):
    apps = world[0]
    assert apps.resolve("chrome") == "Google Chrome"
    assert apps.resolve("the notes app") == "Notepad"
    assert apps.resolve("photo booth") == "Camera"
    assert apps.resolve("Google Chrome") == "Google Chrome"
    assert apps.resolve("terminal") == "Windows Terminal"
    assert apps.resolve("chrome and clear the history") is None      # not a name
    assert apps.resolve("arc") is None                                # not installed
    assert "7-Zip Help" not in apps.names()                           # noise filtered
    assert apps.names()[0] == "Google Chrome"                         # alias targets first


def test_launch_dry_run(world):
    apps = world[0]
    r = apps.launch("Notepad", dry_run=True)
    assert r["ok"] and r["target"].startswith("shell:AppsFolder\\")
    assert apps.launch("Nope", dry_run=True)["ok"] is False


# -- actions ---------------------------------------------------------------

def test_url_normalisation():
    assert normalise_url("x dot com") == "https://x.com"
    assert normalise_url("x.com") == "https://x.com"
    assert normalise_url("https://github.com/foo") == "https://github.com/foo"
    assert normalise_url("github dot com slash foo") == "https://github.com/foo"
    assert normalise_url("the notes app") is None
    assert normalise_url("") is None


def test_actions_receipts(world):
    actions = world[1]
    assert actions.web_search("norbert wiener")["url"].endswith("q=norbert+wiener")
    assert actions.open_url("x dot com")["url"] == "https://x.com"
    assert actions.open_app("chrome")["app"] == "Google Chrome"
    assert actions.press_keys("ctrl+t")["ok"]
    assert actions.press_keys("ctrl+bogus")["ok"] is False
    assert actions.run("delete_everything")["ok"] is False           # not a primitive
    assert len(actions.log) == 6


# -- rules -----------------------------------------------------------------

def test_rules_parse(world):
    rules = Rules(world[0])
    assert rules.parse("all right, can you open up the notes app for me").intent == "open_app"
    assert rules.parse("open up the notes app for me").slot["app"] == "Notepad"
    assert rules.parse("can you google search norbert wiener").slot["query"] == "norbert wiener"
    assert rules.parse("now can you open up x dot com").slot["url"] == "https://x.com"
    assert rules.parse("let's make the title say hello").slot["text"] == "hello"
    assert rules.parse("press control plus t").slot["combo"] == "control+t"
    assert rules.parse("switch to the chrome window").intent == "focus_window"
    assert rules.parse("open the arc browser").intent == "unparsed"    # not installed
    assert rules.parse("great great okay").intent == "none"
    assert rules.parse("what time is it").intent == "unparsed"


def test_segments():
    assert split_segments("open chrome, then search for cats and then go to x dot com") == \
        ["open chrome", "search for cats", "go to x dot com"]
    assert split_segments("open chrome and search for cats") == ["open chrome", "search for cats"]
    assert split_segments("open chrome and clear the history") == ["open chrome and clear the history"]
    assert split_segments("") == []


# -- controller, rules only (no Jev answers in the cache) -------------------

def test_rules_only_fires_at_pause_not_early(world):
    _, actions, _, ctl, handed = world
    assert ctl.on_partial("open chrome") == []                 # no Jev: nothing says it is over
    ctl.seen[0] = ("open chrome", time.time() - 1.0)          # the words stopped changing
    fired = ctl.on_partial("open chrome")
    assert [f["action"] for f in fired] == ["open_app"] and fired[0]["why"] == "stable"
    assert ctl.on_partial("open chrome") == []                 # never twice
    out = ctl.on_final("open chrome")
    assert out["fired"] == [] and out["to_brain"] is None


def test_chain_fires_completed_segments_immediately(world):
    _, actions, _, ctl, handed = world
    fired = ctl.on_partial("open chrome, then search for norbert wiener, then go to x")
    assert [f["action"] for f in fired] == ["open_app", "web_search"]
    assert fired[0]["why"] == "connector" and fired[1]["query"] == "norbert wiener"
    out = ctl.on_final("open chrome, then search for norbert wiener, then go to x dot com")
    assert [f["action"] for f in out["fired"]] == ["open_url"]
    assert out["fired"][0]["url"] == "https://x.com" and out["to_brain"] is None


def test_unparsed_goes_to_brain_whole(world):
    _, actions, _, ctl, handed = world
    out = ctl.on_final("rename the last three screenshots by date")
    assert out["fired"] == [] and handed == ["rename the last three screenshots by date"]


def test_mixed_hands_only_leftovers(world):
    _, actions, _, ctl, handed = world
    out = ctl.on_final("open chrome, then rename the last three screenshots")
    assert [f["action"] for f in out["fired"]] == ["open_app"]
    assert handed == ["rename the last three screenshots"]


# -- controller with Jev (replay) --------------------------------------------

def test_jev_ready_fires_app_mid_sentence(world):
    apps, actions, jev, ctl, handed = world
    jev.seed({"transcript": "all right can you open up the"}, ctl.q, jev_answer(0.08, "open_app"))
    jev.seed({"transcript": "all right can you open up the notes app"}, ctl.q,
             jev_answer(0.97, "open_app", app="Notepad"))
    assert ctl.on_partial("all right can you open up the") == []
    fired = ctl.on_partial("all right can you open up the notes app")
    assert fired and fired[0]["app"] == "Notepad" and fired[0]["why"].startswith("ready 0.97")
    assert fired[0]["source"] == "jev"
    # the sentence keeps going; nothing fires twice and the tail is not a command
    jev.seed({"transcript": "all right can you open up the notes app for me"}, ctl.q,
             jev_answer(0.97, "open_app", app="Notepad"))
    assert ctl.on_partial("all right can you open up the notes app for me") == []


def test_jev_open_text_waits_for_pause(world):
    apps, actions, jev, ctl, handed = world
    jev.seed({"transcript": "google search norbert"}, ctl.q, jev_answer(0.9, "web_search"))
    jev.seed({"transcript": "google search norbert wiener"}, ctl.q, jev_answer(0.95, "web_search"))
    assert ctl.on_partial("google search norbert") == []          # ready, but free text: wait
    assert ctl.on_partial("google search norbert wiener") == []
    ctl.seen[0] = ("google search norbert wiener", time.time() - 1.0)
    fired = ctl.on_partial("google search norbert wiener")
    assert fired and fired[0]["query"] == "norbert wiener" and fired[0]["why"] == "stable"


def test_jev_picks_app_the_rules_could_not(world):
    apps, actions, jev, ctl, handed = world
    jev.seed({"transcript": "bring up my code editor"}, ctl.q,
             jev_answer(0.95, "open_app", app="Cursor"))
    fired = ctl.on_partial("bring up my code editor")
    assert fired and fired[0]["app"] == "Cursor" and fired[0]["source"] == "jev"


def test_jev_complex_overrides_regex_without_a_clean_split(world):
    apps, actions, jev, ctl, handed = world
    seg = "type up a summary of the deck"                 # rules see "type", Jev sees a task
    jev.seed({"transcript": seg}, ctl.q, jev_answer(0.9, "complex"))
    assert ctl.on_partial(seg) == []
    out = ctl.on_final(seg)
    assert out["fired"] == [] and handed == [seg]
    assert actions.log == []                              # nothing typed


def test_complex_keeps_the_rules_app_plus_remainder_split(world):
    """Calibration 2026-09-23: throwing the split away meant Chrome never
    opened. The app fires; only the in-app remainder goes to the brain."""
    apps, actions, jev, ctl, handed = world
    seg = "open chrome and clear the history"
    jev.seed({"transcript": seg}, ctl.q, jev_answer(0.9, "complex", irreversible=0.8))
    out = ctl.on_final(seg)
    assert [f["app"] for f in out["fired"]] == ["Google Chrome"]
    assert handed == ["clear the history"]                # the risky half: brain asks first


def test_early_fired_app_keeps_its_remainder(world):
    """The app fires on an early partial; the words spoken after it must still
    reach the brain (calibration: 'open telegram and reply...' lost the reply)."""
    apps, actions, jev, ctl, handed = world
    for t in ("open chrome", "open chrome and", "open chrome and clear the history"):
        ctl.seen[0] = (t, time.time() - 1.0)              # every partial already stable
        ctl.on_partial(t)
    assert [r["app"] for r in actions.log] == ["Google Chrome"]
    ctl.on_final("open chrome and clear the history")
    assert handed == ["clear the history"]


def test_spoken_url_waits_for_the_pause(world):
    apps, actions, jev, ctl, handed = world
    seg = "go to github dot com"
    jev.seed({"transcript": seg}, ctl.q, jev_answer(0.99, "open_url"))
    assert ctl.on_partial(seg) == []                      # "slash anthropics" may still be coming
    out = ctl.on_final("go to github dot com slash anthropics")
    assert [f["url"] for f in out["fired"]] == ["https://github.com/anthropics"]


def test_questions_reach_the_brain_and_tabs_are_keys(world):
    apps, actions, jev, ctl, handed = world
    ctl.on_final("what time is it")
    assert handed == ["what time is it"]
    out = ctl.on_final("open a new tab")
    assert out["fired"][0]["combo"] == "ctrl+t"
    out = ctl.on_final("open chrome open cursor")         # no connector, two commands
    assert [f["app"] for f in out["fired"]] == ["Google Chrome", "Cursor"]
    out = ctl.on_final("search for open source tools")    # ...but a search stays one search
    assert [f.get("query") for f in out["fired"]] == ["open source tools"]


def test_jev_irreversible_is_held(world):
    apps, actions, jev, ctl, handed = world
    seg = "type sudo rm dash rf slash"
    jev.seed({"transcript": seg}, ctl.q, jev_answer(0.95, "type_text", irreversible=0.91))
    ctl.seen[0] = (seg, time.time() - 1.0)
    assert ctl.on_partial(seg) == []                                  # nothing fires...
    assert ctl.held[0]["held"] == "irreversible"
    out = ctl.on_final(seg)
    assert out["fired"] == [] and handed == [seg]                    # ...the brain asks instead
    assert actions.log == []                                         # nothing typed


def test_jev_never_raises_and_cache_roundtrip(tmp_path):
    jev = Jev(mode="replay", cache_dir=tmp_path)
    assert jev.evaluate("x", {"q": {"type": "boolean", "instructions": "?"}}) is None
    key = jev.seed("x", {"q": {"type": "boolean", "instructions": "?"}},
                   {"q": {"type": "noul", "noul": 0.5}})
    assert (tmp_path / f"{key}.json").exists()
    assert jev.noul(jev.evaluate("x", {"q": {"type": "boolean", "instructions": "?"}}), "q") == 0.5
    assert jev.stats == {"calls": 2, "hits": 1, "misses": 1, "errors": 0, "input_tokens": 0,
                         "latency_ms": [], "status": {}}
    assert jev.cost_usd == 0.0
    # a "real" client without a key downgrades to replay instead of failing
    import os
    os.environ.pop("JEV_API_KEY", None); os.environ.pop("AI_GATEWAY_API_KEY", None)
    assert Jev(mode="real", cache_dir=tmp_path).mode in ("replay", "real")


def test_question_set_shape(world):
    q = world[3].q
    assert set(q) == {"command", "ready", "intent", "app", "irreversible"}
    assert list(q["intent"]["criteria"]) == list(INTENTS)
    assert q["app"]["criteria"] and list(q["app"]["criteria"])[-1] == NONE_APP
    assert all(q[k]["type"] == "noul" and set(q[k]["criteria"]) == {"true", "false"}
               for k in ("command", "ready", "irreversible"))
    big = questions([f"App {i}" for i in range(300)])["app"]["criteria"]
    assert len(big) == 255 and list(big)[-1] == NONE_APP                # API cap on choices


def test_jev_not_a_command_is_ignored(world):
    apps, actions, jev, ctl, handed = world
    seg = "i should probably open chrome later"
    jev.seed({"transcript": seg}, ctl.q, jev_answer(0.9, "open_app", app="Google Chrome", command=0.12))
    ctl.seen[0] = (seg, time.time() - 1.0)
    assert ctl.on_partial(seg) == []
    out = ctl.on_final(seg)
    assert out["fired"] == [] and handed == []                          # not even for the brain


def test_code_level_danger_list_holds_without_jev(world):
    _, actions, _, ctl, handed = world
    out = ctl.on_final("press alt f4")
    assert out["fired"] == [] and out["held"][0]["held"] == "dangerous keys" and handed == ["press alt f4"]
    out = ctl.on_final("type sudo rm dash rf slash")
    assert out["fired"] == [] and handed[-1] == "type sudo rm dash rf slash"
    assert actions.log and all(r.get("held") for r in actions.log)


def test_open_app_and_rest_goes_to_brain(world):
    _, actions, _, ctl, handed = world
    out = ctl.on_final("open chrome and reply to the last message")
    assert [f["action"] for f in out["fired"]] == ["open_app"]
    assert handed == ["reply to the last message"]


def test_mishearings_and_fuzzy_guard(world):
    apps = world[0]
    assert apps.resolve("clawed") == "Claude"
    assert apps.resolve("code editor") == "Cursor"          # alias candidates, first installed wins
    assert apps.resolve("registry") is None                 # no such app in the fixture


def test_jev_choice_helper_shapes():
    ans = {"a": {"type": "choice", "choice": "x", "probabilities": {"x": 0.7, "y": 0.3}, "confidence": 0.61},
           "b": {"type": "choice", "choice": "z", "probabilities": {"z": 0.5}}}
    assert Jev.choice(ans, "a") == ("x", 0.7, 0.61)
    assert Jev.choice(ans, "b") == ("z", 0.5, 0.5)                       # confidence falls back to p
    assert Jev.choice(ans, "missing") == (None, 0.0, 0.0)
    assert Jev.noul({"n": {"type": "noul", "noul": 0.42}}, "n") == 0.42
    assert Jev.noul({"n": {"type": "boolean", "probability": 0.42}}, "n") == 0.42   # gateway-native shape too
