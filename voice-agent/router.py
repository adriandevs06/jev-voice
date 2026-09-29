"""router.py - which model the brain uses for THIS request.

The idea from 0xNatoshi/jev-codex-router (Jev picks model + depth per turn),
cut down to what a voice agent needs: two tiers.

    fast   (haiku)  - "click the third video", "reply to that", screen work
    smart  (sonnet) - "look for new AI tools", "compare these", plans, anything
                      that needs judgment or web research

Rules first: free and instant, and they settle most requests by their first
words. Jev only when the rules cannot tell -- one Choice, bounded wait -- and
fast is the fallback, because a slow answer to a quick request is the failure
users actually complain about.

The switch itself is a stream-json `set_model` control request on the one
running brain (no second process, which this laptop has no RAM for).
"""
from __future__ import annotations

import re
import threading
from typing import Optional

# Research / judgment words anywhere in the request -> smart.
SMART = re.compile(
    r"\b(?:research|look into|look for|find (?:me|out|new|the best|some|good|all|a|an)|"
    r"compare|what'?s new|keep an eye|summari[sz]e|write (?:up|me|a)|analy[sz]e|"
    r"make (?:a|me a) (?:plan|list)|plan (?:out|my)|figure out|recommend|should i|"
    r"pros and cons|deep dive|dig into|go through (?:all|every))\b", re.I)
# A request that opens with a screen verb -> fast.
FAST = re.compile(
    r"^(?:\W*(?:ok(?:ay)?|so|now|and|then|please|yo|alright|can you|could you|just)\W+)*"
    r"(?:click|tap|open|close|play|pause|scroll|type|press|switch|go to|go back|select|pick|"
    r"choose|skip|mute|turn|put|move|drag|minimi[sz]e|maximi[sz]e|show|hide|stop|start)\b", re.I)

KINDS = {
    "a quick action on the screen: open, click, type, play, scroll or switch something": "fast",
    "research: finding, comparing or summarising information from the web": "smart",
    "a task with several steps across apps or files, or one that needs careful judgment": "smart",
    "not a request: a reaction, thinking aloud, or talk aimed at someone else": "fast",
}
QUESTIONS = {
    "kind": {
        "type": "choice",
        "instructions": ("The text is a spoken request to a computer assistant. What kind of "
                         "work does it need?"),
        "criteria": {k: None for k in KINDS},
    }
}
JEV_WAIT_S = 0.8
JEV_TRUST = 0.6


def route(text: str, jev=None, wait_s: float = JEV_WAIT_S) -> tuple:
    """-> (tier, why). tier is 'fast' or 'smart'."""
    t = (text or "").strip()
    if SMART.search(t):
        return "smart", "rule: research words"
    if FAST.match(t):
        return "fast", "rule: screen verb"
    if jev is None or getattr(jev, "mode", "") not in ("real", "replay"):
        return "fast", "default"
    box = {}

    def ask():
        try:
            box["a"] = jev.evaluate({"transcript": t}, QUESTIONS)
        except Exception:
            pass
    th = threading.Thread(target=ask, daemon=True)
    th.start()
    th.join(wait_s)
    a = box.get("a")
    if not a:
        return "fast", "default (jev slow or absent)"
    label, _p, conf = jev.choice(a, "kind")
    tier = KINDS.get(label or "")
    if tier and conf >= JEV_TRUST:
        return tier, f"jev {conf:.2f}: {label.split(':')[0]}"
    return "fast", f"default (jev unsure {conf:.2f})"


def model_for(tier: str, fast: str, smart: str) -> str:
    return smart if tier == "smart" else fast


def label(tier: str) -> Optional[str]:
    return {"fast": "quick", "smart": "thinking"}.get(tier)
