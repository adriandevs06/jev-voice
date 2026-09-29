"""In-app control: the UIA tree parser, the keyless name match, the ambiguity
fallback and the code-side danger list. No desktop is touched -- the tree is a
fixture captured from this laptop and the Desktop is stubbed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import Actions, looks_like_keys  # noqa: E402
from apps import Apps  # noqa: E402
from elements import (candidates, dangerous, focused_window, for_choice,  # noqa: E402
                      match, parse_tree)
from intent import Controller, Rules  # noqa: E402
from jev import Jev  # noqa: E402
from test_intent import CATALOG  # noqa: E402

# Captured from windows-mcp Snapshot on this machine (shape verified 2026-09-23)
TREE = r'''
    Focused Window:
    Name                                            Depth  Status   Width  Height    Handle
    Untitled - Notepad                                 11  Normal    1384    1041  23333552

    └── window "Untitled - Notepad"
        ├── pane "Notepad"
        │   ├── (120,80) button "New tab"  [action: click]
        │   ├── (300,80) tab "Untitled"  [action: click]  [toggle:off]
        │   ├── (640,300) edit "Text editor"  [action: click, settext]
        │   ├── text "Ln 1, Col 1"
        │   └── (1200,80) button "Settings"  [action: click]
        ├── pane "Chrome"
        │   ├── (500,140) edit "Search Google or type a URL"  [action: click, settext]
        │   ├── (900,600) button "Send"  [action: click]
        │   ├── (960,600) button "Save draft"  [action: click]
        │   └── (1020,600) button "Delete conversation"  [action: click]
        └── pane "Taskbar"
            ├── (1410,1416) button "Start"  [action: click]  [toggle:off]
            └── (1767,1416) button "Google Chrome pinned"  [action: click]
'''


class StubDesktop:
    def __init__(self, els, fail=False):
        self.els, self.fail = els, fail
        self.clicked, self.typed = [], []

    def available(self):
        return not self.fail

    def elements(self, force=False):
        return [] if self.fail else self.els

    def click(self, e, dry_run=False):
        self.clicked.append(e["name"])
        return {"action": "click_element", "name": e["name"], "loc": [e["x"], e["y"]],
                "ok": True, "dry_run": dry_run}

    def type_into(self, e, text, enter=False, dry_run=False):
        self.typed.append((e["name"], text))
        return {"action": "type_into", "name": e["name"], "text": text, "ok": True,
                "dry_run": dry_run}


# -- parsing -------------------------------------------------------------------

def test_parse_keeps_only_named_actionable_controls():
    els = parse_tree(TREE)
    names = [e["name"] for e in els]
    assert "Text editor" in names and "Send" in names and "Start" in names
    assert "Ln 1, Col 1" not in names              # static text has no [action:]
    assert all(e["name"] and e["actions"] for e in els)
    assert [e["i"] for e in els] == list(range(len(els)))
    editor = next(e for e in els if e["name"] == "Text editor")
    assert (editor["x"], editor["y"], editor["type"]) == (640, 300, "edit")
    assert "settext" in editor["actions"]


def test_parse_caps_and_survives_junk():
    full = parse_tree(TREE)
    assert len(full) > 3
    capped = parse_tree(TREE, max_elements=3)
    assert len(capped) == 3 and [e["name"] for e in capped] == [e["name"] for e in full[:3]]
    assert for_choice(full, 3) == capped                 # same slice, renumbered
    assert for_choice(full, 99) is full                  # nothing to trim
    assert parse_tree("") == [] and parse_tree("no tree here at all") == []
    assert parse_tree('(1,2) button ""  [action: click]') == []       # unnamed


# -- matching ------------------------------------------------------------------

def test_exact_and_partial_name_match():
    els = parse_tree(TREE)
    i, s, _ = match(els, "send")
    assert els[i]["name"] == "Send" and s == 1.0
    i, _, _ = match(els, "text editor")
    assert els[i]["name"] == "Text editor"
    i, _, _ = match(els, "settings button")
    assert els[i]["name"] == "Settings"


def test_ambiguity_refuses_to_guess():
    els = parse_tree(TREE)
    i, best, runner = match(els, "the button")           # matches nothing usefully
    assert i is None
    i, _, _ = match(els, "nothing like this exists here")
    assert i is None
    opts = [e["name"] for e in candidates(els, "google")]
    assert "Google Chrome pinned" in opts or "Search Google or type a URL" in opts


def test_danger_list_covers_the_expensive_buttons():
    els = parse_tree(TREE)
    by = {e["name"]: e for e in els}
    assert dangerous(by["Send"]) and dangerous(by["Delete conversation"])
    assert not dangerous(by["Text editor"]) and not dangerous(by["Start"])
    assert not dangerous(by["Save draft"])               # saving is not irreversible


# -- through Actions -----------------------------------------------------------

def _actions(tmp_path, fail=False):
    d = StubDesktop(parse_tree(TREE), fail=fail)
    a = Actions(Apps(catalog=CATALOG, cache=tmp_path / "a.json"), dry_run=True,
                foreground=lambda: "Untitled - Notepad", stacks={}, desktop=d)
    return a, d


def test_click_by_name(tmp_path):
    a, d = _actions(tmp_path)
    r = a.click_element("the settings button")
    assert r["ok"] and d.clicked == ["Settings"] and r["loc"] == [1200, 80]


def test_click_refuses_dangerous_and_ambiguous(tmp_path):
    a, d = _actions(tmp_path)
    assert a.click_element("send")["ok"] is False            # code danger list
    r = a.click_element("google")                            # two plausible controls
    assert r["ok"] is False and r["held"] == "ambiguous target" and len(r["options"]) >= 2
    r = a.click_element("the flux capacitor")                # nothing resembles it
    assert r["ok"] is False and r["held"] == "no matching control"
    assert d.clicked == []


def test_type_into_a_field(tmp_path):
    a, d = _actions(tmp_path)
    r = a.type_into("text editor", "hello")
    assert r["ok"] and d.typed == [("Text editor", "hello")]
    assert a.type_into("text editor", "sudo rm -rf /")["held"] == "dangerous text"


def test_no_desktop_is_an_honest_failure(tmp_path):
    a, _ = _actions(tmp_path, fail=True)
    r = a.click_element("settings")
    assert r["ok"] is False and "windows-mcp" in r["error"]


# -- through the rules ---------------------------------------------------------

def test_press_disambiguates_keys_from_controls(tmp_path):
    rules = Rules(Apps(catalog=CATALOG, cache=tmp_path / "a.json"))
    assert looks_like_keys("control plus t") and looks_like_keys("alt f4")
    assert not looks_like_keys("the send button")
    assert rules.parse("press control plus t").intent == "press_keys"
    d = rules.parse("press the send button")
    assert d.intent == "click_element" and d.slot["target"] == "send"
    d = rules.parse("click on the settings icon")
    assert d.intent == "click_element" and d.slot["target"] == "settings"


def test_type_into_rule(tmp_path):
    rules = Rules(Apps(catalog=CATALOG, cache=tmp_path / "a.json"))
    d = rules.parse("type hello into the text editor")
    assert d.intent == "type_into" and d.slot == {"text": "hello", "target": "text editor"}
    assert rules.parse("type hello world").intent == "type_text"   # no target: still plain typing


def test_chain_reaches_a_control(tmp_path):
    a, d = _actions(tmp_path)
    handed = []
    ctl = Controller(a, Jev(mode="replay", cache_dir=tmp_path / "j"), to_brain=handed.append,
                     app_names=a.apps.names())
    out = ctl.on_final("open notepad and then click the settings button")
    assert [r["action"] for r in out["fired"]] == ["open_app", "click_element"]
    assert d.clicked == ["Settings"] and handed == []


def test_real_snapshot_payload_is_json_escaped():
    """The server hands back a JSON-encoded array, so the tree arrives with
    escaped quotes as well as escaped newlines. Unescaping only the newlines
    leaves a backslash before every control name and parses to nothing -- this
    fixture is the real payload shape, captured from windows-mcp."""
    raw = (Path(__file__).parent / "fixtures" / "snapshot_raw.json").read_text(encoding="utf-8")
    assert raw.startswith('["') and '\\"' in raw          # genuinely escaped
    els = parse_tree(raw)
    assert els, "the escaped payload must parse"
    assert all(not e["name"].startswith("\\") for e in els)
    assert focused_window(raw)                            # header table is readable
    i, _, _ = match(els, els[0]["name"])
    assert i is not None
