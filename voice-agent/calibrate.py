"""calibrate.py - measure the router against labeled transcripts.

    python calibrate.py                 # replay cache + rules only: what fires today, $0
    python calibrate.py --jev real      # one real Jev pass per partial, answers cached
    python calibrate.py --thresholds    # sweep READY_AT / COMMAND_AT on the cached answers

The labels live in tests/fixtures/transcripts.jsonl: for each sentence, which
primitives must fire (in order) and whether anything should reach the Claude
brain. The harness feeds every sentence word by word through the real
Controller (dry-run actions, FakeEars partials) and scores three things:

  fired   the primitives that fired == the expected list (order matters)
  brain   whether something was handed to the brain == the label
  early   for closed-slot commands, how many words BEFORE the end of the
          segment the primitive fired (0 = only at the pause; >0 = mid-sentence)

Why this exists: independent benchmarks (2026-09-19/20) found Jev's
probabilities are overconfident out of distribution and only a 0.99 gate is
safe for anything automated; thresholds copied from a Mac browser demo will
not be right for Windows app names and real spoken phrasing. One real pass over
these ~40 sentences (a few hundred partials, ~$0.005 at list price, free on the
gateway until 2026-09-25) fills the replay cache; every sweep after that is free.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import intent
from actions import Actions
from apps import Apps
from intent import Controller
from jev import Jev
from stream_ears import FakeEars

HERE = Path(__file__).parent
FIXTURES = HERE / "tests" / "fixtures" / "transcripts.jsonl"


def load_cases(path: Path = FIXTURES) -> list:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def sig(r: dict) -> str:
    if r["action"] == "focus_window":
        return f"focus_window:{r.get('asked', '')}"
    for k in ("app", "query", "url", "text", "combo"):
        if k in r:
            return f"{r['action']}:{r[k]}"
    return r["action"]


def run_case(case: dict, apps: Apps, jev: Jev) -> dict:
    actions = Actions(apps, dry_run=True)
    handed = []
    ctl = Controller(actions, jev, to_brain=handed.append)
    fired, early = [], {}
    words = case["text"].split()
    n = len(words)

    def on_partial(text):
        k = len(text.split())
        for r in ctl.on_partial(text):
            fired.append(r)
            early[sig(r)] = n - k          # words still to come when it fired

    def on_final(text, provisional=False):
        for r in ctl.on_final(text, provisional=provisional)["fired"]:
            fired.append(r)
            early.setdefault(sig(r), 0)

    FakeEars(on_partial, on_final).say(case["text"])
    got = [sig(r) for r in fired if r.get("ok")]
    return {"text": case["text"], "expect": case["expect"], "got": got,
            "fired_ok": got == case["expect"],
            "brain_ok": bool(handed) == bool(case.get("brain")),
            "handed": handed, "early": early, "note": case.get("note", "")}


def prefetch(cases: list, apps: Apps, jev: Jev, workers: int = 2) -> None:
    """Ask Jev, synchronously, every question the controller could ask while
    these sentences are spoken word by word: the last segment of every partial,
    and every segment of the final text. The live console asks the same things
    through the debounced worker; here each answer is fetched once and cached,
    so calibration reflects Jev and not the pacing of a replay."""
    from concurrent.futures import ThreadPoolExecutor
    from intent import questions, split_segments
    q = questions(apps.names())
    wanted = []
    for c in cases:
        words = c["text"].split()
        for k in range(1, len(words) + 1):
            segs = split_segments(" ".join(words[:k]))
            if segs:
                wanted.append(segs[-1])
        wanted.extend(split_segments(c["text"]))
    wanted = list(dict.fromkeys(wanted))
    todo = [s for s in wanted if jev.cached({"transcript": s}, q) is None]
    print(f"prefetch: {len(wanted)} distinct segments, {len(todo)} not cached yet")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda seg: jev.evaluate({"transcript": seg}, q), todo))
    lat = sorted(jev.stats["latency_ms"]) or [0]
    print(f"prefetch done: {jev.stats['calls']} calls, {jev.stats['errors']} errors, "
          f"median {lat[len(lat)//2]} ms, {jev.stats['input_tokens']} tokens, ${jev.cost_usd:.4f}, "
          f"http {jev.stats['status']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jev", choices=["auto", "real", "replay"], default="auto")
    ap.add_argument("--thresholds", action="store_true", help="sweep READY_AT/COMMAND_AT on cached answers")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    apps = Apps()
    jev = Jev(mode=None if a.jev == "auto" else a.jev, timeout=15, retries=4)
    cases = load_cases()
    if jev.mode == "real":
        jev.warm()
        prefetch(cases, apps, jev)
        # every question the harness can ask is now on disk; score from the
        # cache so the result is deterministic and threshold sweeps are free
        jev = Jev(mode="replay")

    grids = [(intent.READY_AT, intent.COMMAND_AT)]
    if a.thresholds:
        grids = [(r, c) for r in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99) for c in (0.3, 0.5, 0.7)]

    best = None
    for ready_at, command_at in grids:
        intent.READY_AT, intent.COMMAND_AT = ready_at, command_at
        results = [run_case(c, apps, jev) for c in cases]
        fired_ok = sum(r["fired_ok"] for r in results)
        brain_ok = sum(r["brain_ok"] for r in results)
        both = sum(r["fired_ok"] and r["brain_ok"] for r in results)
        early_words = [w for r in results for w in r["early"].values()]
        avg_early = sum(early_words) / len(early_words) if early_words else 0.0
        line = (f"READY_AT={ready_at:.2f} COMMAND_AT={command_at:.2f}  fired {fired_ok}/{len(cases)}  "
                f"brain {brain_ok}/{len(cases)}  both {both}/{len(cases)}  "
                f"avg words-early {avg_early:.2f}")
        print(line)
        if best is None or both > best[0]:
            best = (both, line, results)

    both, line, results = best
    print("\nbest:", line)
    print(f"jev mode={jev.mode} calls={jev.stats['calls']} hits={jev.stats['hits']} "
          f"misses={jev.stats['misses']} errors={jev.stats['errors']} tokens={jev.stats['input_tokens']} "
          f"cost=${jev.cost_usd:.5f}")
    if jev.mode != "real" and jev.stats["misses"]:
        print("  (no cached Jev answers for most partials: this is the rules-only baseline; "
              "run with --jev real once a key is set)")
    failures = [r for r in results if not (r["fired_ok"] and r["brain_ok"])]
    if failures or a.verbose:
        print("\nmisses:" if failures else "\nall cases:")
        for r in (failures if failures else results):
            print(f"  {r['text']!r}\n     expect {r['expect']} brain={bool(r['handed'])==False and 'no' or r['handed']}"
                  f"\n     got    {r['got']} handed={r['handed']}  early={r['early']}  # {r['note']}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
