"""stream_ears.py - the mic loop, now with partial transcripts.

The old Ears (server.py) waited for ~750 ms of silence, transcribed the whole
utterance once, and handed the text to Claude. For a chat that is right. For
"open chrome, then search for X" it means nothing happens until you stop
talking. This version keeps every proven piece (energy gate calibrated at the
20th percentile, pre-roll, hang time, barge-in over TTS, the silent-virtual-mic
check) and adds one thing: while an utterance is in progress, the buffer so far
is re-transcribed by the fast model every PARTIAL_EVERY seconds and pushed out
as a partial. The accurate model still produces the final text at the end.

faster-whisper is batch-only, so "streaming" here is a sliding re-decode of the
growing buffer with tiny.en (int8, CPU): ~150-400 ms for a 3 s buffer on this
laptop class, single-flight (a tick is skipped if the previous decode is still
running; the newest buffer always wins). That is the same trick the Jev voice
clones use when they are not on Apple's on-device recogniser, and it needs no
new model download -- tiny.en is already the wake-word gate in listen.py.

Vosk (50 MB, true streaming partials) is the upgrade path if the re-decode
proves too slow: same on_partial/on_final contract, swap the class.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

SAMPLE_RATE = 16000
FRAME = 480                 # 30 ms
PREROLL = 12                # ~360 ms kept before onset
HANG = 25                   # ~750 ms of quiet ends an utterance
GATE_MIN, GATE_MAX = 0.012, 0.050
MIN_UTTER, MAX_UTTER = 0.4, 30.0
BARGE_MULT = 3.0
PARTIAL_EVERY = 0.4         # seconds between partial decodes
# End of sentence when the WORDS stop, even if the room is not quiet. The
# energy hang needs 750 ms below the gate; with a YouTube video playing through
# the speakers that never came, so "open chrome" sat unprocessed until the mic
# was closed 14 s later (2026-09-24). Two more partial decodes with no new
# words, at least this long after the last new word, end it.
STALL_END_S = 1.1
STALL_DECODES = 2
AMBIENT_FRAMES = 100        # ~3 s of the room, sampled while the mic is closed
PARTIAL_MIN_AUDIO = 0.8     # do not bother decoding less than this

# Words Whisper should expect. 2026-09-24 it heard "Google Pro" for Google
# Chrome, "skip app" for skip ad, "20 years" for 20 percent, "here mode" for
# theater mode. faster-whisper's `hotwords` puts these in the decoder's
# prompt: names get spelled the way the rules and the app catalog spell them.
# vocab.txt (one word or phrase per line) adds his own: people, brands.
VOCAB = [
    "Google Chrome", "YouTube", "Gmail", "Discord", "Spotify", "Cursor", "Devin", "Roblox",
    "Telegram", "Instagram", "TikTok", "Twitter", "Reddit", "Netflix", "Twitch", "ChatGPT",
    "Whop", "Notepad", "Calculator", "File Explorer", "Steam", "Slack", "WhatsApp",
    "Outlook", "Excel", "PowerPoint", "VS Code", "Windsurf", "OBS", "CapCut",
    "theater mode", "full screen", "captions", "skip ad",
]
# NAMES ONLY -- no command verbs. Measured 2026-09-24: a list ending "...click,
# open" made Whisper drop the spoken "Open" ("Open YouTube, ..." came back as
# "YouTube, ..."): it read the prompt as words already said. The closing period
# keeps its punctuation, which the sentence split in intent.py relies on.
VOCAB_FILE = __import__("pathlib").Path(__file__).with_name("vocab.txt")
HOTWORDS_MAX_CHARS = 600     # Whisper's prompt is capped at ~224 tokens; leave room


def hotwords(extra_file=VOCAB_FILE) -> str:
    words = list(VOCAB)
    try:
        for line in extra_file.read_text(encoding="utf-8").splitlines():
            w = line.split("#", 1)[0].strip()
            if w and w not in words:
                words.insert(0, w)              # his own words first: never cut off
    except OSError:
        pass
    out = ""
    for w in words:
        nxt = f"{out}, {w}" if out else w
        if len(nxt) > HOTWORDS_MAX_CHARS:
            break
        out = nxt
    return out + "." if out else ""

# Capture devices that open and stream fine but hear nothing (or hear the
# speakers). Device INDEXES shift whenever Windows adds an endpoint -- on
# 2026-09-24 the documented VOICE_DEVICE=7 had become SplitCam -- so the mic is
# chosen by name, and an index that lands on one of these is refused.
VIRTUAL_MIC = ("splitcam", "obs", "vb-audio", "cable output", "voicemeeter", "stereo mix",
               "sound mapper", "primary sound capture", "virtual")
# The stream opens at 16 kHz; MME and DirectSound resample, WASAPI shared mode
# and WDM-KS refuse a rate the hardware does not run at.
RESAMPLING_APIS = ("Windows DirectSound", "MME")


def pick_mic(spec: Optional[str] = None, devices=None, hostapis=None) -> Optional[int]:
    """VOICE_DEVICE -> an input device index.

    spec is an index ("8"), a name fragment ("intel", "headset"), or None for
    the first real microphone. Returns None only when nothing usable exists,
    which leaves the choice to the system default."""
    if devices is None or hostapis is None:
        import sounddevice as sd
        devices, hostapis = sd.query_devices(), sd.query_hostapis()

    def api(d):
        return hostapis[d["hostapi"]]["name"]

    def usable(i, d):
        return d["max_input_channels"] > 0 and api(d) in RESAMPLING_APIS

    def virtual(d):
        return any(v in d["name"].lower() for v in VIRTUAL_MIC)

    spec = (spec or "").strip()
    if spec.isdigit():
        i = int(spec)
        if i < len(devices) and devices[i]["max_input_channels"] > 0 and not virtual(devices[i]):
            return i
        spec = ""                                  # stale or virtual index: fall back to by-name
    pool = [(i, d) for i, d in enumerate(devices) if usable(i, d) and not virtual(d)]
    if spec:
        named = [(i, d) for i, d in pool if spec.lower() in d["name"].lower()]
        pool = named or pool
    pool.sort(key=lambda p: RESAMPLING_APIS.index(api(p[1])))   # DirectSound first: full names
    return pool[0][0] if pool else None


class StreamEars:
    def __init__(self, on_partial: Callable[[str], None], on_final: Callable[[str], None],
                 on_state: Callable[[str], None], speaker=None, device: Optional[int] = None,
                 model: str = "small.en", fast_model: str = "tiny.en", load_models: bool = True,
                 on_level: Optional[Callable[[float, float], None]] = None,
                 hotwords: Optional[str] = None):
        self.on_partial, self.on_final, self.on_state = on_partial, on_final, on_state
        self.on_level = on_level          # (rms, gate) ~16x/s while the mic is open: the bar's meter
        self._frames_seen = 0
        self.on_log = None                # optional: one-line diagnostics (gate changes)
        self._ambient = deque(maxlen=AMBIENT_FRAMES)
        self._was_muted = True
        self._changed_at = 0.0            # when the partial text last gained words
        self._same = 0                    # partial decodes since then that added nothing
        self.speaker = speaker
        self.device = device
        self.frames: queue.Queue = queue.Queue()
        self.muted = True
        self.threshold = GATE_MIN
        self.running = False
        self._partial_busy = threading.Lock()
        self._last_partial_text = ""
        self.big = self.fast = None
        self.hotwords = hotwords or None
        if load_models:
            from faster_whisper import WhisperModel
            self.big = WhisperModel(model, device="cpu", compute_type="int8")
            self.fast = WhisperModel(fast_model, device="cpu", compute_type="int8") \
                if fast_model != model else self.big

    # -- audio in ------------------------------------------------------------

    def start(self):
        import sounddevice as sd
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     blocksize=FRAME, device=self.device,
                                     callback=lambda d, *_: self.frames.put(d.copy()))
        self.stream.start()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.stream.stop(); self.stream.close()
        except Exception:
            pass

    def calibrate(self):
        levels, peak, end = [], 0.0, time.time() + 1.5
        while time.time() < end:
            try:
                f = self.frames.get(timeout=0.3)
            except queue.Empty:
                break
            levels.append(float(np.sqrt(np.mean(f ** 2))))
            peak = max(peak, float(np.max(np.abs(f))))
        floor = float(np.percentile(levels, 20)) if levels else 0.0
        self.threshold = min(max(floor * 3.0, GATE_MIN), GATE_MAX)
        return floor, peak, self.threshold

    # -- transcription -------------------------------------------------------

    def _decode(self, audio: np.ndarray, model) -> str:
        if model is None:
            return ""
        segs, _ = model.transcribe(audio, language="en", beam_size=1 if model is self.fast else 5,
                                   hotwords=self.hotwords,
                                   vad_filter=True,
                                   vad_parameters={"min_silence_duration_ms": 400})
        return " ".join(s.text.strip() for s in segs).strip()

    def _partial(self, audio: np.ndarray):
        """Single-flight: if a decode is running, this tick is skipped."""
        if not self._partial_busy.acquire(blocking=False):
            return
        try:
            text = self._decode(audio, self.fast)
            if text and text != self._last_partial_text:
                self._last_partial_text = text
                self._changed_at, self._same = time.time(), 0
                self.on_partial(text)
            elif self._last_partial_text:
                self._same += 1
        except Exception:
            pass
        finally:
            self._partial_busy.release()

    def _final(self, audio: np.ndarray, provisional_text: str):
        """The accurate pass. It always ends in exactly one on_final(..., False)
        so the controller can reconcile and reset, even if the accurate model
        heard nothing (then the provisional text stands)."""
        self.on_state("thinking")
        try:
            text = self._decode(audio, self.big)
        except Exception:
            text = ""
        # wait for a straggling partial so the final is never overtaken
        self._partial_busy.acquire(); self._partial_busy.release()
        self._last_partial_text = ""
        if len(text) < 2:
            text = provisional_text
        if len(text) < 2:
            self.on_state("idle")
            return
        _call(self.on_final, text, False)

    def _retune(self):
        """The mic just opened: set the gate from the room as it is NOW (a video
        playing, a fan), not as it was at startup. Above most of the background,
        so a sentence can end on the energy too -- capped so speech still opens it."""
        if len(self._ambient) < 20:
            return
        lv = np.array(self._ambient)
        gate = max(float(np.percentile(lv, 20)) * 3.0, float(np.percentile(lv, 50)) * 1.5)
        self.threshold = min(max(gate, GATE_MIN), GATE_MAX)
        if self.on_log:
            self.on_log(f"mic open: room median {np.percentile(lv, 50):.4f} -> gate {self.threshold:.4f}")

    def _cut(self, audio: np.ndarray):
        """The mic was closed mid-sentence. There was no quiet tail for the
        partials to catch up in, so the last partial can be a word or two
        behind: decode the whole buffer with the fast model for the
        provisional, then the accurate pass as usual."""
        with self._partial_busy:                  # let a running partial land first
            try:
                text = self._decode(audio, self.fast)
            except Exception:
                text = ""
        provisional = text if len(text) >= 2 else self._last_partial_text
        if provisional:
            _call(self.on_final, provisional, True)
        self._final(audio, provisional)

    def _receipting(self) -> bool:
        sp = self.speaker
        return bool(sp and getattr(sp, "receipting", lambda: False)())

    # -- the loop --------------------------------------------------------------

    def _loop(self):
        pre, voiced, quiet, started = deque(maxlen=PREROLL), [], 0, False
        last_partial_at = 0.0
        while self.running:
            try:
                f = self.frames.get(timeout=0.3)
            except queue.Empty:
                continue
            if self.muted:
                self._ambient.append(float(np.sqrt(np.mean(f ** 2))))
                self._was_muted = True
            elif self._was_muted:
                self._was_muted = False
                self._retune()
            if self.muted or self._receipting():
                # Closing the mic mid-sentence (double-tap Alt) ENDS the
                # sentence instead of discarding it: "open chrome<Alt><Alt>"
                # must still open Chrome. A spoken receipt ("Chrome") is the
                # opposite case -- our own voice -- and is always dropped.
                if self.muted and started and len(voiced) * FRAME / SAMPLE_RATE > MIN_UTTER:
                    audio = np.concatenate(voiced, axis=0).flatten()
                    threading.Thread(target=self._cut, args=(audio,), daemon=True).start()
                started, voiced = False, []
                continue

            energy = float(np.sqrt(np.mean(f ** 2)))
            self._frames_seen += 1
            if self.on_level and self._frames_seen % 2 == 0:
                try:
                    self.on_level(energy, self.threshold)
                except Exception:
                    pass
            speaking = bool(self.speaker and getattr(self.speaker, "speaking", False))
            loud = energy > self.threshold * (BARGE_MULT if speaking else 1.0)

            if not started:
                pre.append(f)
                if loud:
                    started, voiced, quiet = True, list(pre), 0
                    last_partial_at = time.time()
                    self._changed_at, self._same = time.time(), 0
                    if speaking and hasattr(self.speaker, "shutup"):
                        self.speaker.shutup()          # barge-in
                    self.on_state("listening")
                continue

            voiced.append(f)
            quiet = 0 if loud else quiet + 1
            dur = len(voiced) * FRAME / SAMPLE_RATE

            now = time.time()
            if dur >= PARTIAL_MIN_AUDIO and now - last_partial_at >= PARTIAL_EVERY and quiet < HANG:
                last_partial_at = now
                audio = np.concatenate(voiced, axis=0).flatten()
                threading.Thread(target=self._partial, args=(audio,), daemon=True).start()

            stalled = (self._last_partial_text and self._same >= STALL_DECODES
                       and now - self._changed_at >= STALL_END_S)
            if (quiet >= HANG and dur > MIN_UTTER) or dur > MAX_UTTER or stalled:
                audio = np.concatenate(voiced, axis=0).flatten()
                started, voiced, pre = False, [], deque(maxlen=PREROLL)
                provisional = self._last_partial_text
                if provisional:
                    # route NOW on the fast model's text: closed slots fire ~1 s
                    # after the last word; the accurate pass follows in ~2 s
                    _call(self.on_final, provisional, True)
                threading.Thread(target=self._final, args=(audio, provisional), daemon=True).start()


def _call(fn, text, provisional):
    """on_final callbacks may take (text) or (text, provisional)."""
    try:
        return fn(text, provisional)
    except TypeError:
        return fn(text)


class FakeEars:
    """Feeds a typed sentence as word-by-word partials, then the provisional
    final (the last partial, as the fast model would give it) and the accurate
    final. For `voice_cli.py --text` and for tests: the whole controller with
    no microphone."""

    def __init__(self, on_partial, on_final, step_s: float = 0.0, accurate: Optional[str] = None):
        self.on_partial, self.on_final, self.step_s = on_partial, on_final, step_s
        self.accurate = accurate          # what the accurate model "heard", if different

    def say(self, text: str):
        words = text.split()
        for i in range(1, len(words)):
            self.on_partial(" ".join(words[:i]))
            if self.step_s:
                time.sleep(self.step_s)
        self.on_partial(text)
        _call(self.on_final, text, True)
        if self.step_s:
            time.sleep(self.step_s)
        _call(self.on_final, self.accurate or text, False)
