"""Mic choice: by name, never a virtual device, never a stale index."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stream_ears import pick_mic  # noqa: E402

APIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]


def dev(name, api, inputs=1):
    return {"name": name, "hostapi": api, "max_input_channels": inputs}


# The real layout on 2026-09-24, trimmed: index 7 had become SplitCam.
DEVICES = [
    dev("Microsoft Sound Mapper - Input", 0),
    dev("Microphone (SplitCam Audio Mixe", 0),
    dev("Microphone Array (Intel Smart ", 0),
    dev("Speakers", 0, inputs=0),
    dev("Primary Sound Capture Driver", 1),
    dev("Microphone (SplitCam Audio Mixer)", 1),
    dev("Microphone Array (Intel Smart Sound Technology for Digital Microphones)", 1),
    dev("Microphone (SplitCam Audio Mixer)", 2),
    dev("Microphone Array (Intel Smart Sound Technology for Digital Microphones)", 2),
]


def test_default_is_the_real_mic_on_a_resampling_api():
    i = pick_mic(None, DEVICES, APIS)
    assert "Intel" in DEVICES[i]["name"] and APIS[DEVICES[i]["hostapi"]]["name"] == "Windows DirectSound"


def test_an_index_that_became_splitcam_is_refused():
    assert "SplitCam" in DEVICES[5]["name"]
    assert "Intel" in DEVICES[pick_mic("5", DEVICES, APIS)]["name"]


def test_an_out_of_range_index_falls_back():
    assert "Intel" in DEVICES[pick_mic("99", DEVICES, APIS)]["name"]


def test_a_good_index_is_kept():
    assert pick_mic("2", DEVICES, APIS) == 2


def test_by_name_fragment():
    devices = DEVICES + [dev("Headset Microphone (Jabra)", 1)]
    assert "Jabra" in devices[pick_mic("jabra", devices, APIS)]["name"]


def test_no_real_mic_leaves_it_to_the_system():
    assert pick_mic(None, [DEVICES[1], DEVICES[5]], APIS) is None


# -- hotwords: bias Whisper toward the names the rules expect ------------------

def test_hotwords_include_his_words_first_and_stay_short(tmp_path):
    from stream_ears import HOTWORDS_MAX_CHARS, hotwords
    f = tmp_path / "vocab.txt"
    f.write_text("# comment\nAcmeCo\nGlobex  # brand\n\n", encoding="utf-8")
    h = hotwords(f)
    assert h.startswith("Globex, AcmeCo") and "Google Chrome" in h and "theater mode" in h
    assert "comment" not in h and len(h) <= HOTWORDS_MAX_CHARS


def test_hotwords_survive_a_missing_file(tmp_path):
    from stream_ears import hotwords
    assert "YouTube" in hotwords(tmp_path / "nope.txt")


def test_both_models_get_the_hotwords():
    import numpy as np
    from stream_ears import StreamEars
    seen = []

    class Model:
        def transcribe(self, audio, **kw):
            seen.append(kw.get("hotwords"))
            return [], None
    ears = StreamEars(lambda t: None, lambda *a: None, lambda s: None, load_models=False,
                      hotwords="YouTube, theater mode")
    ears.fast, ears.big = Model(), Model()
    ears._decode(np.zeros(16000, dtype="float32"), ears.fast)
    ears._decode(np.zeros(16000, dtype="float32"), ears.big)
    assert seen == ["YouTube, theater mode"] * 2


def test_hotwords_hold_no_command_verbs():
    """A prompt ending in 'open' made Whisper drop the spoken 'Open'."""
    import re
    from stream_ears import hotwords
    h = hotwords()
    assert h.endswith(".")
    for verb in ("open", "search", "type", "click", "look up", "pause", "mute"):
        assert not re.search(rf"(?:^|, ){verb}(?:,|\.|$)", h, re.I), verb
