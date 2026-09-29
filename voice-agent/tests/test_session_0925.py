"""The four sentences from 2026-09-24/25 that went to the brain (which was
out of quota, so nothing happened at all). Each must now be handled -- or
deliberately dropped -- without it."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import Actions  # noqa: E402
from apps import Apps, epic_games  # noqa: E402
from intent import Controller, after_cancel  # noqa: E402
from jev import Jev  # noqa: E402
from test_intent import CATALOG  # noqa: E402

FORTNITE = {"name": "Fortnite", "id": "com.epicgames.launcher://apps/Fortnite?action=launch&silent=true"}


def world(tmp_path, catalog=CATALOG):
    apps = Apps(catalog=catalog, cache=tmp_path / "a.json")
    actions = Actions(apps, dry_run=True, foreground=lambda: "Discord", stacks={})
    handed = []
    ctl = Controller(actions, Jev(mode="replay", cache_dir=tmp_path / "j"),
                     to_brain=handed.append, app_names=apps.names())
    return actions, ctl, handed


def test_open_fortnite_launches_through_epic(tmp_path):
    actions, ctl, handed = world(tmp_path, CATALOG + [FORTNITE])
    ctl.on_final("Open Fortnite")
    assert [(r["action"], r.get("app")) for r in actions.log] == [("open_app", "Fortnite")]
    assert handed == []


def test_epic_install_list_becomes_catalog_entries(tmp_path):
    dat = tmp_path / "LauncherInstalled.dat"
    dat.write_text(json.dumps({"InstallationList": [
        {"AppName": "Fortnite", "InstallLocation": r"C:\Program Files\Epic Games\Fortnite"},
        {"AppName": "UE_5.4", "InstallLocation": r"C:\Program Files\Epic Games\UE_5.4"},
    ]}), encoding="utf-8")
    assert epic_games(dat) == [FORTNITE]
    assert epic_games(tmp_path / "missing.dat") == []


def test_open_reddit_on_chrome_opens_the_site(tmp_path):
    actions, ctl, handed = world(tmp_path)
    ctl.on_final("Open Reddit on Chrome.")
    assert [(r["action"], r.get("url")) for r in actions.log] == [("open_url", "https://reddit.com")]
    assert handed == []


def test_taking_it_back_does_nothing(tmp_path):
    actions, ctl, handed = world(tmp_path)
    ctl.on_final("Open YouTube, oh wait, wait, hold on.")
    assert actions.log == [] and handed == []


def test_only_what_follows_the_take_back_runs(tmp_path):
    actions, ctl, handed = world(tmp_path)
    ctl.on_final("open youtube, never mind, open reddit")
    assert [r.get("url") for r in actions.log] == ["https://reddit.com"]


def test_the_brain_never_hears_the_cancelled_part(tmp_path):
    actions, ctl, handed = world(tmp_path)
    ctl.on_final("open youtube, scratch that, what's on my calendar today")
    assert actions.log == [] and handed == ["what's on my calendar today"]


def test_a_verb_with_no_object_is_dropped(tmp_path):
    actions, ctl, handed = world(tmp_path)
    ctl.on_final("Open for me")
    assert actions.log == [] and handed == []


def test_wait_as_a_word_is_not_a_take_back():
    assert after_cancel("type wait for it") == "type wait for it"
    assert after_cancel("open chrome, wait") == ""
