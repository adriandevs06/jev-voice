"""intent.py - turn a half-spoken sentence into an action, safely.

The shape (same as Andy Gao's Jev demo on macOS, 2026-09-18): speech is
transcribed continuously; on every partial transcript a DECISION model is asked
"is there a complete command in here yet, and which one?"; code extracts the
argument and fires the matching primitive; the sentence is still going. Jev
answers in a few hundred ms, which is what makes acting mid-sentence possible.
Without a Jev key the same loop runs on the regex rules alone and only fires at
the end of the utterance -- everything still works, just not early.

Three rules keep this from being a liability:

1. Segments, not sentences. "open chrome, then search for cats, then go to
   x dot com" is split on connectors; every segment except the last is
   complete by construction and fires immediately; only the last one needs the
   model's "ready" judgement.
2. Closed slots fire early, open slots fire late. An app name or a domain is
   bounded -- it either resolves against the catalog or it does not -- so it
   fires the moment `ready` crosses the threshold. A search query or typed
   text is unbounded, so it waits until the segment stops changing between
   partials (a natural pause) or the utterance ends. This is the difference
   between searching "norbert wiener" and searching "norbert".
3. Only the six primitives in actions.py fire here, and only once per
   segment. Anything the model calls complex, anything it calls irreversible,
   and anything the rules cannot parse is handed to the Claude Code brain
   with the full sentence -- it has computer-use and it asks first.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from actions import Actions, KINDS, looks_like_keys, normalise_url, site_url
from apps import Apps
from jev import Jev, JevWorker

QUESTION_SET = "voice-v2"   # TypeSafe-native shape (noul / choice criteria map / score)
# Thresholds start where moritzkremb/jev-voice-browser measured them (is_command
# >= 0.5, intent confidence >= 0.55, complete >= 0.6, free text waits ~600 ms,
# destructive >= 0.5) and get re-tuned from the replay cache on this laptop.
COMMAND_AT = 0.50          # noul: the speaker is instructing the computer at all
READY_AT = 0.85            # noul: the last command has both its action and its target.
                           # Calibrated 2026-09-23 on 40 labeled sentences / 230 real Jev
                           # answers: 40/40 correct everywhere in 0.80-0.95, firing ~1.9 words
                           # before the sentence ends; 0.99 is also 40/40 but only 0.6 early.
                           # 0.85 (not 0.90): live paced speech asks about half-finished
                           # phrases ('...spotify real') that score ~0.85; still 40/40.
INTENT_AT = 0.55           # choice confidence to trust Jev's intent/app over the rules
IRREVERSIBLE_AT = 0.50     # noul: hold and hand to the brain instead of firing
STABLE_S = 0.7             # an open-text segment unchanged this long counts as finished
FINAL_WAIT_S = 1.0         # at the end of an utterance, how long to wait for Jev before the brain
NONE_APP = "none of these"

INTENTS = {
    "open an app or program": "open_app",
    "search the web for something": "web_search",
    "open a website or url": "open_url",
    "type or write some text": "type_text",
    "press a key or keyboard shortcut": "press_keys",
    "switch to or focus an open window": "focus_window",
    "click a button or control on the screen": "click_element",
    "type text into a field on the screen": "type_into",
    "a task with several steps or that changes files, messages, or settings": "complex",
    "not a command: thinking aloud, a question, or filler": "none",
}
INTENT_BY_KEY = {v: k for k, v in INTENTS.items()}

CONNECTOR = re.compile(
    r"\s*(?:,\s*)?\b(?:and then|then|after that|after which|next|afterwards|and also|"
    r"and(?=\s+(?:open|launch|start|search|google|look up|go to|type|write|press|hit|switch|focus|"
    r"turn|put|set|skip|pause|play|mute|unmute|scroll|click|refresh|reload)))\b\s*",
    re.I)
FILLER = re.compile(r"^(?:ok(?:ay)?|so|alright|all right|now|great|nice|cool|um+|uh+|hey|please|"
                    r"can you|could you|would you|will you|let's|lets|i want you to|i need you to|"
                    r"go ahead and|and|also|and once you're there|once you're there|when you're there|"
                    r"from there|after that|let's move on and)[\s,]+", re.I)
TAIL = re.compile(r"[\s,]*(?:for me|please|now|real quick|right now|thanks|thank you)[\s.!?]*$", re.I)
# A new command starting mid-segment with no "and/then" ("open chrome open
# cursor"). Only honoured when the words before it already form a complete,
# bounded command -- "search for open source tools" must stay one search.
VERB_START = re.compile(r"\s+(?=(?:open|launch|start|run|bring up|pull up|fire up|go to|navigate to|"
                        r"visit|search|google|look up|type|write|press|hit|click|tap|switch to|focus|"
                        r"turn|put|set|skip|pause|mute|unmute|scroll|refresh|reload)\b)", re.I)
# Where a command can start inside a sentence that opened with something else
# ("well, I said open google", "alright let's do open youtube"). The rules
# retry from each of these before giving up on the sentence.
COMMAND_WORD = re.compile(r"\b(?:open|launch|start|run|bring up|pull up|fire up|go to|navigate to|visit|"
                          r"search|google|look up|look for|type|write|press|hit|click|tap|switch to|"
                          r"focus|turn|put|set|skip|pause|play|resume|mute|unmute|scroll|go back|"
                          r"refresh|reload)\b", re.I)

# Media and page controls: one keystroke, no model, no screen read.
# Distinctive phrases are found anywhere in the sentence ("put this video into
# theater mode") and fire mid-sentence (`bounded`): growing the sentence
# cannot change what they mean. Short ones must be the whole sentence
# ("pause", "next video") and wait for the pause, because a partial "play"
# may still become "play some music".
YOUTUBE_ONLY = {"t", "c"}
MEDIA_ANYWHERE = [
    (re.compile(r"\bthea(?:ter|tre) mode\b", re.I), "t"),
    (re.compile(r"\bfull ?screen\b", re.I), "f"),
    (re.compile(r"\b(?:closed captions|captions|subtitles)\b", re.I), "c"),
]
SKIP_AD = re.compile(r"\bskip (?:the |this |that )?(?:ad|ads|app|add)\b", re.I)
MEDIA_WHOLE = [
    (re.compile(r"(?:pause|play|resume|unpause)(?: (?:it|this|that|the (?:video|music|song|track|audio)))?"
                r"|stop the (?:video|music|song)", re.I), "playpause"),
    (re.compile(r"next(?: (?:video|song|track))?|skip (?:the |this )?(?:video|song|track)", re.I), "nexttrack"),
    (re.compile(r"(?:previous|last) (?:video|song|track)|go back a (?:song|track)", re.I), "prevtrack"),
    (re.compile(r"(?:un)?mute(?: (?:it|this|that|the (?:video|audio|sound|music|tab)))?", re.I), "volumemute"),
    (re.compile(r"scroll(?: the page)? down(?: a (?:bit|little))?", re.I), "pagedown"),
    (re.compile(r"scroll(?: the page)? up(?: a (?:bit|little))?", re.I), "pageup"),
    (re.compile(r"go back|back a page|previous page", re.I), "alt+left"),
    (re.compile(r"go forward|forward a page", re.I), "alt+right"),
    (re.compile(r"(?:refresh|reload)(?: (?:the|this) page| it)?", re.I), "f5"),
    (re.compile(r"next tab", re.I), "ctrl+tab"),
    (re.compile(r"(?:previous|last) tab", re.I), "ctrl+shift+tab"),
]
# "turn the audio down to like 20%" (Whisper heard "20 years"), "volume 30"
VOLUME_TO = re.compile(r"\b(?:volume|audio|sound|video|music|it)\b.*?\b(?:to|at)\s+"
                       r"(?:like\s+|about\s+|around\s+|maybe\s+)?(\d{1,3})\b"
                       r"|\bvolume\s+(\d{1,3})\b", re.I)
VOLUME_DIR = re.compile(r"\b(?:turn|put)\s+(?:it|this|that|the (?:volume|audio|sound|video|music))?\s*(up|down)\b"
                        r"|\b(?:volume|audio|sound)\s+(up|down)\b|\b(louder|quieter|softer)\b", re.I)
# A question addressed to the assistant goes to the brain even when Jev calls
# it "not a command". Calibration (2026-09-23) caught "what time is it" being
# dropped because questions and filler shared one intent label.
TRAILING_CONNECTOR = re.compile(r"(?:[\s,]+(?:and|then|also|so|um+|uh+|and then|after that))+[\s,.]*$", re.I)
QUESTION = re.compile(r"^(?:what|what's|whats|when|where|who|whose|why|how|which)\b", re.I)
# The speaker taking it back. Everything said BEFORE the last one of these is
# dropped -- not fired, not handed to the brain. 2026-09-25: "Open YouTube, oh
# wait, wait, hold on." went to the brain as a request. A bare "wait" only
# counts at a pause (comma, stop, end), so "wait for it" is still words.
CANCEL = re.compile(r"\b(?:never\s?mind|cancel that|scratch that|forget (?:it|that)|hold on|hang on|"
                    r"(?:no|oh),?\s+wait|wait,?\s+wait|actually,?\s+no|stop,?\s+stop)\b"
                    r"|\bwait\b(?=\s*(?:[,.!?]|$))", re.I)
# "open reddit on chrome", "open youtube in a new tab": where it opens, not what.
WHERE_SUFFIX = re.compile(r"\s+(?:on|in|with|using)\s+(?:the\s+|my\s+|a\s+)?(?:new\s+)?"
                          r"(?:google\s+)?(?:chrome|browser|tab|window)$", re.I)
# A command verb with nothing after it ("open for me" once FILLER/TAIL go):
# the object has not been said, so there is nothing to do -- never the brain's.
BARE_VERB = re.compile(r"(?:open|launch|start|run|bring up|pull up|go to|search|google|look up|"
                       r"type|write|press|click|tap|play|show|find|switch to)", re.I)


def after_cancel(text: str) -> str:
    """Only what was said after the last take-back."""
    last = None
    for last in CANCEL.finditer(text or ""):
        pass
    return (text or "")[last.end():].lstrip(" ,.!?") if last else (text or "")


@dataclass
class Decision:
    intent: str                  # key of INTENTS' values, or "unparsed"
    ready: float = 0.0
    slot: dict = field(default_factory=dict)
    source: str = "rules"        # "jev" | "rules"
    irreversible: float = 0.0
    app_prob: float = 0.0


# ---------------------------------------------------------------------------
# rules: the deterministic parse. Cheap, runs on every partial, never wrong
# about what it matches -- only incomplete about what it fails to match.
# ---------------------------------------------------------------------------

class Rules:
    OPEN = re.compile(r"^(?:open(?: up)?|launch|start|run|bring up|pull up|fire up)\s+(?:the\s+|my\s+|a\s+)?(.+?)$", re.I)
    # "look for" / "find" are NOT here: "look for new AI tools for me" is
    # research for the brain, not a Google tab (2026-09-24).
    SEARCH = re.compile(r"^(?:google(?: search)?|search(?: the web| google| online)?(?: for)?|look up)"
                        r"\s+(.+?)$", re.I)
    GOTO = re.compile(r"^(?:go to|navigate to|visit|open(?: up)?|pull up|take me to)\s+(?:the\s+)?(.+?)$", re.I)
    TYPE = re.compile(r"^(?:type(?: out| in)?|write(?: out| in)?|enter|make (?:the )?(?:title|it) say|"
                      r"say)\s+(.+?)$", re.I)
    PRESS = re.compile(r"^(?:press|hit|push)\s+(.+?)$", re.I)
    FOCUS = re.compile(r"^(?:switch to|focus(?: on)?|bring back|go back to)\s+(?:the\s+)?(.+?)(?:\s+window)?$", re.I)
    STACK = re.compile(r"^(?:run|start|open|launch|fire up|do)\s+(?:my\s+|the\s+)?(.+?)\s+(?:stack|setup|routine)$", re.I)
    CLICK = re.compile(r"^(?:click|tap|select|choose|press|hit|push)\s+(?:on\s+)?(?:the\s+)?(.+?)$", re.I)
    TYPE_INTO = re.compile(r"^(?:type|write|enter|put)\s+(.+?)\s+(?:in|into|in the|into the)\s+(?:the\s+)?(.+?)$", re.I)
    CONTROLWORD = re.compile(r"\s+(?:button|link|tab|field|box|icon|menu|option|checkbox)$", re.I)
    APPWORD = re.compile(r"\s+(?:app|application|program|window|browser)$", re.I)

    def __init__(self, apps: Apps, stacks: Optional[dict] = None, foreground=None):
        self.apps = apps
        self.stacks = stacks or {}
        # the foreground title: "theater mode" means YouTube's `t` only on YouTube
        self.foreground = foreground or (lambda: "")

    def apps_stacks(self) -> dict:
        return self.stacks

    @staticmethod
    def clean(seg: str) -> str:
        s = seg.strip()
        for _ in range(8):
            s2 = FILLER.sub("", s)
            s2 = TAIL.sub("", s2)
            if s2 == s:
                break
            s = s2
        return s.strip(" ,.!?")

    FILLER_WORDS = {"ok", "okay", "so", "alright", "all", "right", "now", "great", "nice", "cool",
                    "um", "uh", "hey", "please", "yeah", "yes", "no", "awesome", "thanks", "thank",
                    "you", "good", "perfect", "sure", "that", "s", "it", "is", "was", "fine", "oh",
                    "ooh", "wow", "damn", "bro", "lol", "mm", "hmm", "yep", "nope", "dope"}

    def parse(self, seg: str) -> Decision:
        """The deterministic parse, then -- if it failed -- again from each
        place a command could start, so a preamble ("well, I said open
        google") does not send a one-keystroke job to the brain."""
        d = self._parse(seg)
        if d.intent != "unparsed":
            return d
        s = self.clean(seg)
        if QUESTION.match(s):
            return d
        for m in COMMAND_WORD.finditer(s):
            if m.start() == 0:
                continue
            d2 = self._parse(s[m.start():])
            if d2.intent not in ("unparsed", "none"):
                return d2
        return d

    def media(self, s: str) -> Optional[Decision]:
        low = s.lower()
        try:
            on_youtube = "youtube" in (self.foreground() or "").lower()
        except Exception:
            on_youtube = False
        m = VOLUME_TO.search(low)
        if m:
            return Decision("set_volume", 1.0, {"level": int(m.group(1) or m.group(2)), "media": True})
        m = VOLUME_DIR.search(low)
        if m:
            word = (m.group(1) or m.group(2) or m.group(3)).lower()
            up = word in ("up", "louder")
            return Decision("set_volume", 1.0, {"step": 10 if up else -10, "media": True, "bounded": True})
        if SKIP_AD.search(low):
            return Decision("click_element", 1.0, {"target": "skip", "media": True, "bounded": True})
        for rx, combo in MEDIA_ANYWHERE:
            if rx.search(low):
                if combo in YOUTUBE_ONLY and not on_youtube:
                    return None                   # theater mode outside YouTube: the brain's call
                if combo == "f" and not on_youtube:
                    combo = "f11"                 # browser/app full screen
                return Decision("press_keys", 1.0, {"combo": combo, "media": True, "bounded": True})
        for rx, combo in MEDIA_WHOLE:
            if rx.fullmatch(low):
                return Decision("press_keys", 1.0, {"combo": combo, "media": True})
        return None

    def _parse(self, seg: str) -> Decision:
        s = self.clean(seg)
        if not s or set(re.findall(r"[a-z]+", s.lower())) <= self.FILLER_WORDS:
            return Decision("none")
        if BARE_VERB.fullmatch(s):
            return Decision("none")
        s = WHERE_SUFFIX.sub("", s)
        low = s.lower()
        if re.fullmatch(r"(?:open\s+)?(?:a\s+)?new tab", low):
            return Decision("press_keys", 1.0, {"combo": "ctrl+t"})
        if re.fullmatch(r"(?:open\s+)?(?:a\s+)?new window", low):
            return Decision("press_keys", 1.0, {"combo": "ctrl+n"})
        if re.fullmatch(r"close (?:this|the|that) (?:tab|window)", low):
            return Decision("press_keys", 1.0, {"combo": "ctrl+w"})   # on the danger list: held, brain asks
        if not QUESTION.match(low):
            md = self.media(s)
            if md:
                return md
        m = self.STACK.match(s)
        if m and m.group(1).strip().lower() in self.apps_stacks():
            return Decision("stack", 1.0, {"name": m.group(1).strip().lower()})
        m = self.GOTO.match(s)
        if m and normalise_url(m.group(1)):
            return Decision("open_url", 1.0, {"url": normalise_url(m.group(1))})
        if m and not self.apps.resolve(self.APPWORD.sub("", m.group(1)).strip()) and site_url(m.group(1)):
            return Decision("open_url", 1.0, {"url": site_url(m.group(1))})
        m = self.OPEN.match(s)
        if m:
            name = self.APPWORD.sub("", m.group(1)).strip()
            app = self.apps.resolve(name)
            if app:
                return Decision("open_app", 1.0, {"app": app, "asked": name})
            # "open telegram and reply to the last message": the app is the first
            # half; the rest is the brain's. Deterministic, no model.
            head, sep, rest = name.partition(" and ")
            if sep:
                app = self.apps.resolve(self.APPWORD.sub("", head).strip())
                if app:
                    return Decision("open_app", 1.0, {"app": app, "asked": head, "rest": rest.strip()})
            return Decision("unparsed", 0.0, {"asked": name})
        m = self.SEARCH.match(s)
        if m:
            return Decision("web_search", 1.0, {"query": m.group(1).strip()})
        m = self.TYPE_INTO.match(s)
        if m:
            return Decision("type_into", 1.0,
                            {"text": m.group(1).strip().strip("\"'"),
                             "target": self.CONTROLWORD.sub("", m.group(2)).strip()})
        m = self.TYPE.match(s)
        if m:
            t = m.group(1).strip().strip("\"'")
            if t.lower().strip(" .") in {"out", "in", "it", "that", "this", "something", "out it"}:
                return Decision("unparsed")      # "just type out" -- the words have not come yet
            return Decision("type_text", 1.0, {"text": t})
        m = self.PRESS.match(s)
        if m and looks_like_keys(m.group(1)):
            combo = re.sub(r"\s*(?:plus|and|\+)\s*", "+", m.group(1).strip().lower())
            combo = re.sub(r"\s+", "+", combo)
            return Decision("press_keys", 1.0, {"combo": combo})
        m = self.CLICK.match(s)
        if m:
            # "press the send button" is a control, not a keystroke
            return Decision("click_element", 1.0,
                            {"target": self.CONTROLWORD.sub("", m.group(1)).strip()})
        m = self.FOCUS.match(s)
        if m:
            return Decision("focus_window", 1.0, {"title": m.group(1).strip()})
        return Decision("unparsed")


# ---------------------------------------------------------------------------
# Jev: the questions asked of every partial. One call, all answered at once.
# ---------------------------------------------------------------------------

def questions(app_names: list) -> dict:
    """One request, five atomic judgements, all phrased so that yes is the
    positive case (docs: a noul whose true maps to "no" performs worse).
    Facts live in `state`, rules in `criteria`, and every choice carries an
    explicit none option. Free text is never asked for: Jev selects, code
    extracts (docs: "select instead of generate")."""
    apps = list(dict.fromkeys(list(app_names)[:254])) + [NONE_APP]
    return {
        "command": {
            "type": "noul",
            "instructions": ("The text is a live speech transcript. Is the speaker giving the "
                             "computer an instruction to carry out?"),
            "criteria": {
                "true": "an imperative aimed at the machine: 'open chrome', 'search for cats', 'go to x dot com'",
                "false": "thinking aloud, a question, a reaction ('nice, cool'), or talk aimed at another person",
            },
        },
        "ready": {
            "type": "noul",
            "instructions": ("The transcript may stop mid-sentence. Does the LAST command in it "
                             "already contain both the action and its target, so a computer "
                             "could carry it out right now even if the speaker keeps talking?"),
            "criteria": {
                "true": ("the action word and its object are both present: 'open chrome', "
                         "'search for cats', 'go to x dot com', 'type hello world'"),
                "false": ("the words stop before the object ('open the', 'can you search'), or "
                          "there is no command yet"),
            },
        },
        "intent": {
            "type": "choice",
            "instructions": "What is the speaker asking the computer to do in the LAST command?",
            "criteria": {k: None for k in INTENTS},
        },
        "app": {
            "type": "choice",
            "instructions": ("If the last command opens or switches to an application, which "
                             "installed app is meant? Pick the closest name, or none of these."),
            "criteria": {a: None for a in apps},
        },
        "irreversible": {
            "type": "noul",
            "instructions": ("Would carrying out the last command delete, send, publish, pay, "
                             "close unsaved work, change settings, or do anything else that "
                             "cannot simply be undone?"),
            "criteria": {
                "true": "deleting, sending a message or email, posting, paying, closing without saving, changing settings, running a shell command",
                "false": "opening an app or page, searching, typing into a fresh document, pressing a navigation key",
            },
        },
    }


# ---------------------------------------------------------------------------
# controller: partials in, receipts out, leftovers to the brain
# ---------------------------------------------------------------------------

def split_segments(text: str) -> list:
    parts = [p.strip(" ,.") for sentence in re.split(r"(?<=[.!?])\s+", text or "")
             for p in CONNECTOR.split(sentence)]
    return [p for p in parts if p]


class Controller:
    def __init__(self, actions: Actions, jev: Optional[Jev] = None,
                 to_brain: Optional[Callable[[str], None]] = None, on_log=None,
                 app_names: Optional[list] = None, on_decision=None, on_fire=None,
                 worker: Optional[JevWorker] = None):
        self.actions = actions
        self.apps = actions.apps
        self.jev = jev or Jev(mode="replay")
        self.rules = Rules(self.apps, getattr(actions, "stacks", None),
                           foreground=getattr(actions, "foreground", None))
        self.to_brain = to_brain or (lambda _t: None)
        self.on_log = on_log or (lambda *_: None)
        self.on_decision = on_decision or (lambda _row: None)
        # Fired late, from the worker thread, when an answer lands after the
        # partial that asked for it. The console broadcasts + speaks these.
        self.on_fire = on_fire or (lambda _receipts: None)
        self.q = questions(app_names if app_names is not None else self.apps.names())
        self.utterances = 0
        # Asking Jev inline would block the decode thread for 300-450 ms per
        # partial and answer about words the speaker has already passed. The
        # worker debounces, caps in-flight and drops stale answers; decisions
        # here read whatever has already arrived and never wait.
        self.worker = worker or JevWorker(self.jev, on_answers=self._on_answers)
        self.answers: dict = {}       # segment text -> answers
        self.last_text = ""
        self._busy = threading.RLock()
        self.reset()

    def reset(self):
        self.done: dict = {}          # segment index -> receipt
        self.held: dict = {}          # segment index -> why it was not fired
        self.seen: dict = {}          # segment index -> (text, first seen at)
        self.fired_sigs: set = set()  # (intent, slot) already fired this utterance
        self.provisional: Optional[str] = None   # text routed at the hang, awaiting the accurate pass
        self.answers = {}             # segment text -> Jev answers, this utterance only
        self.last_text = ""
        self.started = time.time()
        self.utterances += 1
        self.seq = 0

    # -- deciding ----------------------------------------------------------

    def decide(self, seg: str, ask_jev: bool = True) -> Decision:
        d = self.rules.parse(seg)
        if not ask_jev or self.jev.mode == "mock":
            return d
        answers = self._answers_for(seg)
        if answers is None:
            # returns answers only if they are already on disk; otherwise it
            # schedules the call and we proceed on the rules for now
            answers = self.worker.submit({"transcript": seg}, self.q)
            if answers is not None:
                self._store(seg, answers)
        if answers is None:
            return d
        command = self.jev.noul(answers, "command")
        ready = self.jev.noul(answers, "ready")
        label, _p_intent, c_intent = self.jev.choice(answers, "intent")
        jev_intent = INTENTS.get(label or "", None)
        app, _p_app, c_app = self.jev.choice(answers, "app")
        if app == NONE_APP:
            app = None
        irreversible = self.jev.noul(answers, "irreversible")

        if command < COMMAND_AT:
            # Jev says nobody is instructing the machine ("I should open chrome
            # later", "did you google it?"): ignore, whatever the regex saw and
            # whatever intent Jev would otherwise have picked.
            d = Decision("none", ready, {}, "jev")
        elif jev_intent == "complex" and c_intent >= 0.8 and not d.slot.get("rest") \
                and not d.slot.get("media"):
            # (a media/volume command the rules matched is one keystroke, not a
            # task -- Jev called "turn the audio down to 20%" complex, 0.98)
            # ...unless the rules already cut it into "open X" + a remainder:
            # firing the app and handing only the remainder to the brain beats
            # handing the whole sentence over (calibration: "open telegram and
            # reply to the last message" never opened Telegram).
            # Jev is sure this is more than one primitive ("open chrome and clear
            # the history", "type up a summary of the deck"): the brain's job,
            # whatever the regex thought it saw.
            d = Decision("complex", ready, {}, "jev")
        elif d.intent == "unparsed" and jev_intent and c_intent >= INTENT_AT:
            # the rules did not recognise the phrasing; Jev did. Slots still come
            # from code -- only the closed app slot can be filled without a parse.
            if jev_intent == "open_app" and app in self.apps.by_name and c_app >= INTENT_AT:
                d = Decision("open_app", ready, {"app": app, "asked": seg}, "jev")
            elif jev_intent == "none":
                d = Decision("none", ready, {}, "jev")
            else:
                d = Decision("unparsed", ready, d.slot, "jev")
        elif d.intent == "unparsed" and d.slot.get("asked") and app in self.apps.by_name and c_app >= INTENT_AT:
            # "open the arc thing" -> the rules saw an open but could not resolve
            # the name; Jev picked it from the catalog.
            d = Decision("open_app", ready, {"app": app, "asked": d.slot["asked"]}, "jev")
        elif d.intent != "unparsed":
            d.source = "jev"            # rules parsed it; Jev supplies readiness
        d.ready = ready
        d.irreversible = irreversible
        d.app_prob = c_app
        return d

    @staticmethod
    def _norm(seg: str) -> str:
        """Partial 'open google pro' and final 'Open Google Pro.' are the same
        words; an answer for one is an answer for the other."""
        return " ".join(re.findall(r"[a-z0-9']+", (seg or "").lower()))

    def _answers_for(self, seg: str) -> Optional[dict]:
        a = self.answers.get(seg)
        return a if a is not None else self.answers.get(self._norm(seg))

    def _store(self, seg: str, answers: dict):
        self.answers[seg] = answers
        self.answers[self._norm(seg)] = answers

    def _await_answers(self, seg: str):
        """End of the utterance and the rules cannot place the words: there is
        no later partial to wait for, so wait for Jev here, briefly. Before
        this, an answer arriving ~300 ms after the last word was ignored and
        EVERY such sentence went to the brain (2026-09-24: "open google pro",
        "alright let's do open youtube" -- Jev had both right)."""
        if self.jev.mode != "real" or self._answers_for(seg) is not None:
            return
        box = {}

        def ask():
            try:
                box["a"] = self.jev.evaluate({"transcript": seg}, self.q)
            except Exception:
                pass
        t = threading.Thread(target=ask, daemon=True)
        t.start()
        t.join(FINAL_WAIT_S)
        if box.get("a"):
            self._store(seg, box["a"])

    def segments(self, text: str) -> list:
        """Connector split, then a second split at a new command verb when the
        words before it already parse as a complete bounded command."""
        out = []
        for seg in split_segments(after_cancel(text)):
            parts = VERB_START.split(seg)
            cur = parts[0]
            for nxt in parts[1:]:
                d = self.rules.parse(cur)
                if d.intent in ("open_app", "open_url", "stack") and not d.slot.get("rest"):
                    out.append(cur.strip(" ,."))
                    cur = nxt
                else:
                    cur = f"{cur} {nxt}"
            out.append(cur.strip(" ,."))
        return [s for s in out if s]

    def _on_answers(self, seq: int, state, answers: dict):
        """A live answer landed after the partial that asked for it. Store it
        and re-run the decision for the text we are on now -- `done` and
        `fired_sigs` make that idempotent, so at worst nothing new fires."""
        seg = (state or {}).get("transcript") if isinstance(state, dict) else None
        if not seg or not answers:
            return
        with self._busy:
            self._store(seg, answers)
            text = self.last_text
            if not text or self.provisional is not None:
                return
            fired = self.on_partial(text, _reentrant=True)
            if not fired:
                fired = self._prefix_fire(seg)
        if fired:
            self.on_fire(fired)

    def _prefix_fire(self, judged: str) -> list:
        """The answer is about `judged`, and the speaker has since said more.
        If `judged` was already a complete bounded command (Jev ready >=
        READY_AT on an app or stack) and the segment being spoken now still
        STARTS with it and still resolves to the SAME target, fire it now.

        The same-target check is what keeps "open visual studio" from firing
        Visual Studio when the words turn out to be "open visual studio code":
        the grown segment resolves to a different app, so nothing fires and the
        answer for the longer text decides instead."""
        for i, cur in enumerate(self.segments(self.last_text)):
            if i in self.done or i in self.held or not cur.startswith(judged + " "):
                continue
            d = self.decide(judged)
            if d.source != "jev" or d.intent not in ("open_app", "stack") or d.ready < READY_AT:
                return []
            # A trailing connector ("...real quick and") is the speaker
            # moving on -- exactly when to fire -- but on its own it does not
            # parse. Strip only connector-ish words; never trim content words,
            # or "open paint 3" would be read as "open paint".
            now = self.rules.parse(TRAILING_CONNECTOR.sub("", cur))
            if now.intent != d.intent or self._sig(now) != self._sig(d):
                return []
            r = self._fire(i, d, f"ready {d.ready:.2f} on '{judged[-24:]}'")
            return [r] if r else []
        return []

    # -- acting --------------------------------------------------------------

    def _rest_after_early_fire(self, i: int, seg: str) -> Optional[str]:
        """The app fired on an early partial ("open telegram") and the speaker
        kept going ("...and reply to the last message"). The receipt was made
        before the remainder existed, so recover it from the full segment --
        but only if the full segment still leads with the same app, so a
        remainder is never attached to something that did not fire."""
        fired = self.done.get(i) or {}
        d = self.rules.parse(seg)
        rest = d.slot.get("rest")
        if rest and d.intent == fired.get("action") and d.slot.get("app") == fired.get("app"):
            return rest
        return None

    @staticmethod
    def _sig(d: Decision) -> tuple:
        for k in ("app", "url", "query", "target", "combo", "title", "name", "text", "level", "step"):
            if k in d.slot:
                return (d.intent, str(d.slot[k]).lower())
        return (d.intent, "")

    def _fire(self, i: int, d: Decision, why: str) -> Optional[dict]:
        if i in self.done:
            return None
        if d.intent not in KINDS:
            return None
        sig = self._sig(d)
        if sig in self.fired_sigs:
            # the accurate transcript re-segmented the sentence; this primitive
            # already fired on the provisional one
            self.done[i] = {"action": d.intent, "ok": True, "dup_of": sig, "why": "already fired"}
            return None
        # Jev scores the WHOLE segment. When the rules split it into "open X"
        # + a remainder, the risky part ("clear the history", "reply to the
        # last message") is the remainder -- and that goes to the brain, which
        # asks first. Holding the harmless app launch in front of it too meant
        # Telegram never opened (calibration, 2026-09-23).
        if d.irreversible >= IRREVERSIBLE_AT and not d.slot.get("rest"):
            # not marked done: on_final hands the words to the brain, which asks
            self.held[i] = {"action": d.intent, "ok": False, "held": "irreversible", "slot": d.slot}
            self.on_log(f"held (irreversible {d.irreversible:.2f}): {d.intent} {d.slot}")
            return None
        kw = {k: v for k, v in d.slot.items()
              if k in ("app", "url", "query", "text", "combo", "title", "name", "target", "level", "step")}
        if d.intent == "open_app":
            kw = {"name": kw.get("app")}
        r = self.actions.run(d.intent, **kw)
        if not r.get("ok"):
            # refused (danger list, no-arm zone, ambiguous target) or simply
            # failed. Either way nothing happened, so the words are still the
            # brain's to deal with -- never mark the segment done.
            self.held[i] = r
            self._record(i, d, r, fired=False)
            return None
        r["why"] = why
        r["source"] = d.source
        if d.slot.get("rest"):
            r["rest"] = d.slot["rest"]
        self.done[i] = r
        self.fired_sigs.add(sig)
        self._record(i, d, r, fired=True)
        return r

    def _record(self, i: int, d: Decision, r: Optional[dict], fired: bool, text: str = ""):
        """One row per decision that mattered -> decisions.jsonl (server) / stdout (cli)."""
        self.seq += 1
        row = {"t": round(time.time(), 3), "utt": self.utterances, "seq": self.seq, "segment": i,
               "text": text or self.seen.get(i, ("",))[0], "intent": d.intent, "source": d.source,
               "ready": round(d.ready, 2), "irreversible": round(d.irreversible, 2),
               "slot": d.slot, "fired": fired, "why": (r or {}).get("why") or (r or {}).get("held"),
               "thresholds": {"ready": READY_AT, "command": COMMAND_AT, "irreversible": IRREVERSIBLE_AT}}
        try:
            self.on_decision(row)
        except Exception:
            pass

    def on_partial(self, text: str, _reentrant: bool = False) -> list:
        """Called on every partial transcript. Fires what is safely ready.
        Never blocks: Jev answers arrive later through _on_answers."""
        if not _reentrant:
            self._busy.acquire()
        try:
            return self._on_partial(text)
        finally:
            if not _reentrant:
                self._busy.release()

    def _on_partial(self, text: str) -> list:
        fired = []
        self.last_text = text
        segs = self.segments(text)
        now = time.time()
        for i, seg in enumerate(segs):
            prev = self.seen.get(i)
            if prev is None or prev[0] != seg:
                self.seen[i] = (seg, now)
            if i in self.done:
                continue
            last = i == len(segs) - 1
            d = self.decide(seg, ask_jev=last)
            if d.intent in ("none", "unparsed", "complex"):
                continue
            stable = now - self.seen[i][1] >= STABLE_S
            # A spoken URL is NOT bounded: "github dot com" can still become
            # "github dot com slash anthropics" (calibration opened github.com
            # two words early). Only app names and stack names fire on `ready`;
            # a URL waits for the pause like any open slot. In a chain it still
            # fires at the connector, and the provisional final still takes it.
            closed = d.intent in ("open_app", "stack")
            if not last:
                r = self._fire(i, d, "connector")            # complete by construction
            elif d.slot.get("bounded"):
                r = self._fire(i, d, "bounded")              # "theater mode": fire on the word
            elif d.source == "jev":
                if d.ready < READY_AT:
                    r = None                                  # Jev: the object is not there yet
                elif closed:
                    r = self._fire(i, d, f"ready {d.ready:.2f}")   # bounded slot: fire now
                elif stable:
                    r = self._fire(i, d, "stable")            # free text: wait for the pause
                else:
                    r = None
            else:
                # rules only (no Jev answer): nothing says the sentence is over,
                # so the pause is the only signal. Slower, never wrong earlier.
                r = self._fire(i, d, "stable") if stable else None
            if r:
                fired.append(r)
        return fired

    def on_final(self, text: str, provisional: bool = False) -> dict:
        with self._busy:
            return self._on_final(text, provisional)

    def _on_final(self, text: str, provisional: bool = False) -> dict:
        """Utterance ended.

        provisional=True: the fast model's last partial at the moment of
        silence. Closed slots (app, URL, stack) fire now -- ~1 s after the last
        word instead of ~3 s -- and everything else waits for the accurate
        text. provisional=False: the accurate transcript (or the same text again
        when no accurate pass is coming): fire the rest, hand leftovers to the
        brain, reset.
        """
        if provisional:
            fired = []
            for i, seg in enumerate(self.segments(text)):
                if i in self.done or i in self.held:
                    continue
                if self.seen.get(i, ("",))[0] != seg:
                    self.seen[i] = (seg, time.time())
                d = self.decide(seg, ask_jev=True)
                if d.slot.get("media") or (d.intent in ("open_app", "open_url", "stack")
                                           and (d.source == "rules" or d.ready >= READY_AT)):
                    r = self._fire(i, d, "provisional")
                    if r:
                        fired.append(r)
            self.provisional = text
            return {"fired": fired, "done": dict(self.done), "held": dict(self.held),
                    "to_brain": None, "provisional": True}

        fired, leftovers = [], []
        segs = self.segments(text)
        for i, seg in enumerate(segs):
            prev = self.seen.get(i, ("",))[0]
            if i in self.done and (prev == seg or self.done[i].get("dup_of")):
                rest = self.done[i].get("rest") or self._rest_after_early_fire(i, seg)
                if rest:
                    leftovers.append(rest)
                continue
            # first sight of this segment, or the accurate text changed it:
            # decide again; the signature set stops a primitive firing twice
            self.done.pop(i, None)
            if prev != seg:
                self.seen[i] = (seg, time.time())
            if self.rules.parse(seg).intent == "unparsed":
                self._await_answers(seg)
            d = self.decide(seg, ask_jev=True)
            if d.intent in KINDS and i not in self.held:
                r = self._fire(i, d, "final")
                if r:
                    fired.append(r)
                    if r.get("rest"):
                        leftovers.append(r["rest"])
                    continue
                if i in self.done:                 # duplicate of a provisional fire
                    if d.slot.get("rest"):
                        leftovers.append(d.slot["rest"])
                    continue
            if (d.intent != "none" and self.rules.clean(seg)) or QUESTION.match(self.rules.clean(seg)):
                leftovers.append(seg)
        handed = None
        if leftovers:
            nothing_fired = not fired and not self.done
            # the whole utterance reads better than its fragments -- but never the
            # part the speaker took back ("open youtube, never mind, what's the time")
            handed = (after_cancel(text) if nothing_fired and len(leftovers) == len(segs)
                      else ", ".join(leftovers))
            self.to_brain(handed)
        out = {"fired": fired, "done": dict(self.done), "held": dict(self.held), "to_brain": handed,
               "provisional": False}
        if handed:
            self._record(-1, Decision("brain", 0.0, {"text": handed}, "rules"), None, fired=False, text=handed)
        self.reset()
        return out
