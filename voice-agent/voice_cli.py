"""voice_cli.py - drive the voice router from a typed sentence, no mic needed.

    python voice_cli.py --text "open chrome, then search for norbert wiener, then go to x dot com"
    python voice_cli.py --text "..." --live          # actually open / search / type
    python voice_cli.py --text "..." --jev real      # ask Jev for real (needs a key)
    python voice_cli.py --apps                       # what "open X" can reach
    python voice_cli.py --stats                      # replay cache + spend so far

Default is a dry run against the replay cache: it prints, word by word, what
the controller would have fired at each partial and what would go to the
Claude brain at the end. With a TYPESAFE_API_KEY (or AI_GATEWAY_API_KEY) and
--jev real, every partial is a real Jev call (~$0.00001 each at list price,
free on the Vercel gateway until 2026-09-25) and the answers are written to
.jev_cache so the same sentence replays for free afterwards -- that is how the
thresholds in intent.py get tuned to this laptop's phrasing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from actions import Actions
from apps import Apps
from intent import Controller
from jev import Jev
from stream_ears import FakeEars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="the sentence to feed as partials")
    ap.add_argument("--live", action="store_true", help="really execute (default: dry run)")
    ap.add_argument("--jev", choices=["auto", "real", "replay", "mock"], default="auto")
    ap.add_argument("--step", type=float, default=0.0, help="seconds between partials (0 = instant)")
    ap.add_argument("--apps", action="store_true")
    ap.add_argument("--click", action="store_true",
                    help="enable in-app control (reads the screen via windows-mcp)")
    ap.add_argument("--elements", action="store_true", help="print what is clickable right now")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    apps = Apps()
    if a.apps:
        for n in apps.names():
            print(f"  {n}")
        print(f"\n{len(apps.catalog)} apps in the catalog, {len(apps.names())} offered to Jev")
        return

    jev = Jev(mode=None if a.jev == "auto" else a.jev, on_log=lambda m: print(f"    [{m}]"))
    if a.stats:
        print(json.dumps({"mode": jev.mode, "base": jev.base, "model": jev.model,
                          "cached": len(list(jev.cache_dir.glob('*.json'))) if jev.cache_dir.exists() else 0},
                         indent=1))
        return
    if a.elements:
        from elements import Desktop
        d = Desktop(on_log=lambda m: print(f"  [{m}]"))
        for e in d.elements():
            print(f"  [{e['i']:>3}] {e['type']:<12} {e['name'][:70]!r} @ ({e['x']},{e['y']})")
        d.stop()
        return
    if not a.text:
        ap.error("--text is required (or --apps / --elements / --stats)")

    print(f"jev: {jev.mode}" + (f" via {jev.base} ({jev.model})" if jev.mode == "real" else ""))
    if jev.mode == "real":
        t0 = time.monotonic(); jev.warm(); print(f"  warm-up {int((time.monotonic()-t0)*1000)} ms")
    desktop = None
    if a.click:
        from elements import Desktop
        desktop = Desktop(on_log=lambda m: print(f"    [{m}]"))
        if not desktop.available():
            print(f"  ! windows-mcp not installed at {desktop.exe}")
            desktop = None
    actions = Actions(apps, dry_run=not a.live, desktop=desktop)
    handed = []
    t_start = time.monotonic()

    def stamp():
        return f"{(time.monotonic() - t_start) * 1000:6.0f} ms"

    ctl = Controller(actions, jev, to_brain=handed.append,
                     on_fire=lambda rs: [print(f"{stamp()}    -> FIRED {r['action']} {_slot(r)}  "
                                               f"[{r.get('why')}, {r.get('source')}]  (late Jev answer)")
                                         for r in rs])

    def on_partial(text):
        t0 = time.monotonic()
        fired = ctl.on_partial(text)
        dt = int((time.monotonic() - t0) * 1000)
        line = f"{stamp()}  partial: {text!r}  ({dt} ms)"
        print(line)
        for r in fired:
            print(f"{stamp()}    -> FIRED {r['action']} {_slot(r)}  [{r.get('why')}, {r.get('source')}]")

    def on_final(text, provisional=False):
        out = ctl.on_final(text, provisional=provisional)
        print(f"{stamp()}  {'hang:   ' if provisional else 'final:  '} {text!r}" + ("  (fast model; closed slots fire now)" if provisional else ""))
        for r in out["fired"]:
            print(f"{stamp()}    -> FIRED {r['action']} {_slot(r)}  [{r.get('why')}, {r.get('source')}]")
        for i, r in out["held"].items():
            why = r.get("held") or r.get("error") or "failed"
            opts = f"  options: {r['options']}" if r.get("options") else ""
            print(f"{stamp()}    -> HELD segment {i}: {r.get('action')} {_slot(r)} ({why}){opts}")
        if out["to_brain"]:
            print(f"{stamp()}    -> to Claude brain: {out['to_brain']!r}")

    FakeEars(on_partial, on_final, step_s=a.step).say(a.text)
    s = jev.stats
    if s["calls"]:
        lat = s["latency_ms"]
        print(f"\njev: {s['calls']} calls, {s['hits']} cache hits, {s['errors']} errors"
              + (f", {int(sum(lat)/len(lat))} ms avg, {s['input_tokens']} tokens, ${jev.cost_usd:.5f}" if lat else ""))
    if not a.live:
        print("(dry run: nothing was opened or typed; add --live to execute)")


def _slot(r: dict) -> str:
    for k in ("app", "url", "query", "name", "target", "asked", "text", "combo", "window"):
        if k in r:
            extra = f" text={r['text']!r}" if k in ("name", "target", "asked") and "text" in r else ""
            return f"{k}={r[k]!r}{extra}"
    return ""


if __name__ == "__main__":
    sys.exit(main())
