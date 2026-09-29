"""The sentences from the first real session (2026-09-24), each of which went
to the slow brain. Every one here must resolve in the rules -- no model."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import Actions, site_url  # noqa: E402
from apps import Apps  # noqa: E402
from intent import Controller, Rules, split_segments  # noqa: E402
from jev import Jev  # noqa: E402
from test_intent import CATALOG  # noqa: E402

YT = "POV 900HP 2025 BMW M4 - YouTube - Google Chrome"


def rules(tmp_path, fg=YT):
    return Rules(Apps(catalog=CATALOG, cache=tmp_path / "a.json"), foreground=lambda: fg)


def world(tmp_path, fg=YT):
    apps = Apps(catalog=CATALOG, cache=tmp_path / "a.json")
    actions = Actions(apps, dry_run=True, foreground=lambda: fg, stacks={})
    handed = []
    ctl = Controller(actions, Jev(mode="replay", cache_dir=tmp_path / "j"),
                     to_brain=handed.append, app_names=apps.names())
    return actions, ctl, handed


@pytest.mark.parametrize("said, intent, slot", [
    ("Alright, let's do open YouTube for me", "open_url", {"url": "https://youtube.com"}),
    ("go to gmail", "open_url", {"url": "https://mail.google.com"}),
    ("Well, I said open Google", "open_app", {"app": "Google Chrome"}),   # the installed app wins
    ("I just look up BMW M4", "web_search", {"query": "BMW M4"}),
    ("put this video into theater mode", "press_keys", {"combo": "t"}),
    ("make it full screen", "press_keys", {"combo": "f"}),
    ("pause", "press_keys", {"combo": "playpause"}),
    ("play the video", "press_keys", {"combo": "playpause"}),
    ("next video", "press_keys", {"combo": "nexttrack"}),
    ("mute it", "press_keys", {"combo": "volumemute"}),
    ("scroll down", "press_keys", {"combo": "pagedown"}),
    ("go back", "press_keys", {"combo": "alt+left"}),
    ("refresh the page", "press_keys", {"combo": "f5"}),
    ("turn the audio down of the easy video down to like 20 years", "set_volume", {"level": 20}),
    ("set the volume to 35%", "set_volume", {"level": 35}),
    ("volume 50", "set_volume", {"level": 50}),
    ("turn the audio down", "set_volume", {"step": -10}),
    ("make it louder", "set_volume", {"step": 10}),
    ("And then I'll just monitor and just click skip app for me", "click_element", {"target": "skip"}),
    ("skip the ad", "click_element", {"target": "skip"}),
])
def test_first_session_sentences_parse(tmp_path, said, intent, slot):
    d = rules(tmp_path).parse(said)
    assert d.intent == intent, (said, d)
    for k, v in slot.items():
        assert d.slot.get(k) == v, (said, d.slot)


def test_installed_app_beats_the_website(tmp_path):
    d = rules(tmp_path).parse("open spotify")
    assert d.intent == "open_app" and d.slot["app"] == "Spotify"


def test_theater_mode_needs_youtube(tmp_path):
    assert rules(tmp_path, fg="Untitled - Notepad").parse("theater mode").intent == "unparsed"
    assert rules(tmp_path, fg="Untitled - Notepad").parse("full screen").slot["combo"] == "f11"


def test_chatter_is_not_a_command(tmp_path):
    r = rules(tmp_path)
    for said in ("Yeah, that's good", "Oh wow", "that's fine"):
        assert r.parse(said).intent == "none", said
    assert r.parse("Black BMW. Yeah.").intent == "unparsed"      # Jev decides this one


def test_just_type_out_waits_for_the_words(tmp_path):
    assert rules(tmp_path).parse("just type out").intent == "unparsed"


def test_questions_are_not_media_keys(tmp_path):
    assert rules(tmp_path).parse("what is theater mode").intent != "press_keys"


def test_sentences_split_so_chatter_does_not_join_the_query():
    assert split_segments("I just look up BMW M4. Yeah, that's good") == ["I just look up BMW M4", "Yeah, that's good"]
    assert split_segments("put this video into theater mode and turn the audio down to 20%") == \
        ["put this video into theater mode", "turn the audio down to 20%"]


def test_whole_session_command_fires_without_the_brain(tmp_path):
    actions, ctl, handed = world(tmp_path)
    ctl.on_final("Just put this video into theater mode and turn the audio down to like 20%.")
    done = [(r["action"], r.get("combo") or r.get("level")) for r in actions.log]
    assert done == [("press_keys", "t"), ("set_volume", 20)] and handed == []


def test_look_up_then_reaction_searches_youtube_in_place(tmp_path):
    actions, ctl, handed = world(tmp_path)
    ctl.on_final("I just look up BMW M4. Yeah, that's good")
    assert [(r["action"], r.get("query"), r.get("site")) for r in actions.log] == \
        [("web_search", "BMW M4", "youtube")]
    assert handed == []                                            # the reaction is not sent anywhere


def test_bounded_media_fires_mid_sentence(tmp_path):
    actions, ctl, handed = world(tmp_path)
    fired = ctl.on_partial("put this video into theater mode")
    assert [r.get("combo") for r in fired] == ["t"]
    ctl.on_final("put this video into theater mode")
    assert len(actions.log) == 1                                   # not twice


def test_bare_play_waits_for_the_pause(tmp_path):
    """'play' may still become 'play some lofi' -- never toggle on the partial."""
    actions, ctl, handed = world(tmp_path)
    assert ctl.on_partial("play") == []


def test_site_url():
    assert site_url("YouTube") == "https://youtube.com"
    assert site_url("the youtube website") == "https://youtube.com"
    assert site_url("google pro") is None


def test_set_volume_dry_run_receipt(tmp_path):
    actions, _, _ = world(tmp_path)
    r = actions.run("set_volume", level=150)
    assert r["ok"] and r["level"] == 100
