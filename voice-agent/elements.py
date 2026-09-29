"""elements.py - what is clickable on screen right now.

The voice fast path can open apps, pages and searches, but the reel's second
half ("create a new note and make the title say hello", "take a picture") is
*inside* an app. Doing that without waking the LLM needs one thing: a list of
the controls on screen, with a stable way to press one.

Windows exposes exactly that through UI Automation, and the Windows-MCP server
already installed for the brain (`hands.py`) will dump it: one `Snapshot` call
returns the desktop's UIA tree with a coordinate, a control type, a name and
the actions each element supports. This module keeps ONE of those servers alive,
parses the tree into an indexed table, and presses an element by index.

Two ways to choose the index, in this order:

  1. NAME MATCH (no model, no key, ~0 ms). "click the send button" against the
     table's names. Exact, then prefix, then token overlap. One clear winner
     required -- ties never guess.
  2. JEV (when a key exists). The table goes into `state` as `elements`, Jev
     answers a `target` Choice over the indexes plus "none", gated at
     confidence >= 0.45 and top probability >= 0.35. This is the same design
     as browser-use/jev-ultrafast and Yappy (which reports 275-690 ms/decision).

Below either gate the answer is NOT a click: `candidates()` returns the top few
and the console reads them out numbered, the way Windows Voice Access does with
"show numbers". A wrong click is worse than a question.

Safety, in code and before any model:
  * a control whose name looks like send/post/pay/delete/confirm is never
    pressed by the fast path -- it goes to the brain, which asks first;
  * nothing runs at all while a no-arm window is in front (actions.blocked());
  * element names are attacker-controllable text (a web page can name a button
    "confirm order" or write instructions into a label), so names are matched
    by code and only ever *selected* by Jev, never executed as text.

A snapshot costs 2-6 s on this laptop depending on load, so it is cached per
foreground window for CACHE_S and refreshed in the background when the
foreground changes. That makes the first in-app command of a session slow and
the rest fast.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Optional

import hands
from actions import foreground_title

CACHE_S = 1.5          # a snapshot is good for this long on the same window
MAX_ELEMENTS = 100     # cap the table (moritzkremb/jev-voice-browser uses the same)
TASKBAR_RESERVE = 20   # slots the taskbar keeps even when the focused window is huge
NAME_MATCH_MIN = 0.55  # token-overlap score a name match must beat to win
NAME_MATCH_GAP = 0.15  # ...and it must beat the runner-up by this much

# One line of the UIA tree:  (1723,1416) button "File Explorer pinned"  [action: click]
ROW = re.compile(r'\((\d+),\s*(\d+)\)\s+([a-z]+)\s+"([^"]*)"(.*)$', re.I)

# Names the fast path never presses. Checked before Jev, on the element's own
# name, because this is the one place a wrong click is expensive.
DANGEROUS_CONTROL = re.compile(
    r"\b(send|post|publish|tweet|reply|share|submit|pay|buy|purchase|order|checkout|"
    r"subscribe|confirm|delete|remove|discard|uninstall|format|wipe|reset|sign ?out|"
    r"log ?out|transfer|withdraw|deposit|accept|agree|allow)\b", re.I)

STOP = {"the", "a", "an", "my", "this", "that", "button", "field", "box", "icon",
        "on", "in", "at", "to", "please", "it"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    return {w for w in _norm(s).split() if w not in STOP}


def decode(text: str) -> str:
    """The MCP text block is a JSON-encoded array, so the tree arrives with
    escaped newlines AND escaped quotes. Decoding it properly matters: a naive
    newline-only replace leaves a backslash in front of every control name and
    nothing parses at all."""
    t = (text or "").strip()
    if t[:1] in ("[", '"'):
        try:
            v = json.loads(t)
            if isinstance(v, list):
                return "\n".join(str(x) for x in v)
            if isinstance(v, str):
                return v
        except ValueError:
            pass
    return t.replace("\\n", "\n").replace('\\"', '"')


def focused_window(text: str) -> str:
    """The name Windows reports for the foreground window, from the dump's own
    header table (more reliable here than asking user32 separately)."""
    lines = decode(text).splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("Focused Window:"):
            for row in lines[i + 1:i + 6]:
                r = row.rstrip()
                if r.strip().startswith("-") or not r.strip() or r.lstrip().startswith("Name"):
                    continue
                # "<name>  <depth>  <status>  <w>  <h>  <handle>" - the name is
                # everything before the run of spaces preceding the numbers
                return re.sub(r"\s{2,}\d+\s+\w+.*$", "", r).strip()
    return ""


def parse_tree(text: str, max_elements: Optional[int] = None) -> list:
    """UIA dump -> [{i, x, y, type, name, actions}], actionable elements only.

    The dump covers every window on the desktop, so rows are grouped by their
    owning window and the FOCUSED one is emitted first, then the taskbar (so
    "click start" keeps working), then the rest.

    `max_elements` is None here on purpose. One window can expose 500+ controls,
    and matching a spoken name against all of them is local and free, so the
    table is never truncated for that. The cap exists only where a list has to
    be *sent* somewhere -- `for_choice()` applies it before the table becomes a
    Jev Choice, because that costs tokens and Jev allows at most 255 options.
    """
    tree = decode(text)
    focused = focused_window(text)
    rows, owner, owner_depth = [], "", -1
    for line in tree.splitlines():
        mark = re.search(r"[├└]── ", line)
        depth = mark.start() if mark else 0
        win = re.search(r'\bwindow "([^"]*)"', line)
        if win:
            owner, owner_depth = win.group(1), depth
            continue
        if mark and depth <= owner_depth:
            owner, owner_depth = "", -1     # left that window's subtree
        m = ROW.search(line)
        if not m:
            continue
        x, y, ctype, name, rest = m.groups()
        if "[action:" not in rest:
            continue                       # not actionable: static text, panes
        actions = re.findall(r"action:\s*([a-z ,]+)", rest, re.I)
        acts = [a.strip() for a in ",".join(actions).split(",") if a.strip()]
        name = name.replace("\r", " ").replace("\n", " ").strip()
        if not name:
            continue                       # unnamed controls cannot be asked for
        rows.append({"x": int(x), "y": int(y), "type": ctype.lower(), "name": name,
                     "actions": acts, "window": owner})

    groups = {0: [], 1: [], 2: []}
    for r in rows:
        w = r["window"]
        groups[0 if (focused and w == focused) else 1 if w == "Taskbar" else 2].append(r)

    # The focused window gets the lion's share, but the taskbar keeps a
    # reserved slice: a window with 100+ controls would otherwise push "click
    # start" off the table entirely.
    if max_elements is None:
        out = groups[0] + groups[1] + groups[2]
    else:
        reserve = min(TASKBAR_RESERVE, len(groups[1]))
        out = groups[0][:max(0, max_elements - reserve)]
        out += groups[1][:max_elements - len(out)]
        out += groups[2][:max_elements - len(out)]
    for i, r in enumerate(out):
        r["i"] = i
    return out


def for_choice(elements: list, max_elements: int = MAX_ELEMENTS) -> list:
    """The slice that may be offered to Jev as a Choice: focused window first,
    taskbar reserved, capped. Indexes are re-numbered so the option labels and
    the list stay in step."""
    if len(elements) <= max_elements:
        return elements
    focused = elements[0]["window"] if elements else ""
    mine = [e for e in elements if e["window"] == focused]
    others = [e for e in elements if e["window"] != focused]
    keep = mine[:max_elements - min(TASKBAR_RESERVE, len(others))]
    keep += others[:max_elements - len(keep)]
    keep += mine[len(keep):max_elements]        # nothing else to reserve for
    return [{**e, "i": i} for i, e in enumerate(keep[:max_elements])]


def match(elements: list, spoken: str) -> tuple:
    """(index, score, runner_up_score) for the best name match, or (None, 0, 0).
    Deterministic; no model."""
    q = _norm(spoken)
    qt = _tokens(spoken)
    if not q or not elements:
        return None, 0.0, 0.0
    scored = []
    for e in elements:
        n = _norm(e["name"])
        nt = _tokens(e["name"])
        if not nt:
            continue
        if n == q:
            s = 1.0
        elif n.startswith(q) or q in n:
            s = 0.8
        else:
            overlap = len(qt & nt)
            if not overlap:
                continue
            s = overlap / max(len(qt), len(nt))
        scored.append((s, e["i"]))
    if not scored:
        return None, 0.0, 0.0
    scored.sort(reverse=True)
    # The same logical control often appears more than once in the tree (a
    # button and its label, a list item and its text). Two rows with the same
    # name are not a choice between two things, so collapse them to the first
    # before deciding whether the match was ambiguous.
    seen, deduped = set(), []
    for sc, idx in scored:
        n = _norm(elements[idx]["name"])
        if n in seen:
            continue
        seen.add(n)
        deduped.append((sc, idx))
    scored = deduped
    best, runner = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
    if best[0] < NAME_MATCH_MIN or best[0] - runner[0] < NAME_MATCH_GAP:
        return None, best[0], runner[0]        # ambiguous: ask, do not guess
    return best[1], best[0], runner[0]


def candidates(elements: list, spoken: str, n: int = 5) -> list:
    """The numbered options to read out when nothing wins outright."""
    qt = _tokens(spoken)
    scored = []
    for e in elements:
        nt = _tokens(e["name"])
        overlap = len(qt & nt) if qt else 0
        if overlap or not qt:
            scored.append((overlap / max(len(qt or {1}), len(nt or {1})), e))
    scored.sort(key=lambda p: -p[0])
    return [e for _s, e in scored[:n]]


def dangerous(e: dict) -> bool:
    return bool(DANGEROUS_CONTROL.search(e.get("name", "")))


class Desktop:
    """One long-lived Windows-MCP process; snapshots cached per window."""

    def __init__(self, exe: Optional[str] = None, timeout: float = 30.0, on_log=None):
        self.exe = str(exe or hands.EXE)
        self.timeout = timeout
        self.on_log = on_log or (lambda *_: None)
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()
        self._cache = None          # (window_title, at, elements)

    # -- process ---------------------------------------------------------------

    def available(self) -> bool:
        return os.path.exists(self.exe)

    def _start(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        env = {**os.environ, **hands.SERVER_ENV}
        self.proc = subprocess.Popen([self.exe, "serve"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env, text=True, encoding="utf-8")
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "voice-agent", "version": "0"}})
        self._notify("notifications/initialized")

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _notify(self, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method, params):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        # A snapshot takes 2-6 s on an idle machine but has been measured at
        # 25 s under load, and readline() would block the caller forever. Read
        # on a thread, give up at `timeout`, and kill the server so the next
        # call starts clean.
        box = {}

        def read():
            box["line"] = self.proc.stdout.readline()

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            self.stop()
            raise TimeoutError(f"windows-mcp {method} exceeded {self.timeout:.0f}s")
        line = box.get("line")
        if not line:
            raise RuntimeError("windows-mcp closed the pipe")
        return json.loads(line)

    def call(self, tool: str, args: dict) -> Optional[str]:
        """Returns the tool's text output, or None on any failure."""
        if not self.available():
            return None
        with self._lock:
            try:
                self._start()
                r = self._rpc("tools/call", {"name": tool, "arguments": args})
            except Exception as e:
                self.on_log(f"desktop {tool}: {type(e).__name__}")
                self.stop()
                return None
        res = (r or {}).get("result") or {}
        if res.get("isError"):
            return None
        return "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")

    # -- the table -------------------------------------------------------------

    def elements(self, force: bool = False) -> list:
        """Actionable controls on screen. Cached per foreground window."""
        title = foreground_title()
        now = time.monotonic()
        if not force and self._cache and self._cache[0] == title and now - self._cache[1] < CACHE_S:
            return self._cache[2]
        t0 = time.monotonic()
        # use_annotation / use_ui_tree stay at their defaults on purpose:
        # forcing them made the call ~5x slower for a byte-identical payload.
        txt = self.call("Snapshot", {"use_vision": False})
        if txt is None:
            return []
        els = parse_tree(txt)
        self.on_log(f"snapshot {int((time.monotonic()-t0)*1000)} ms, {len(els)} elements")
        self._cache = (title, time.monotonic(), els)
        return els

    def prefetch(self):
        """Warm the cache off the hot path (call it when the foreground changes)."""
        threading.Thread(target=lambda: self.elements(force=True), daemon=True).start()

    # -- acting ----------------------------------------------------------------

    def click(self, e: dict, dry_run: bool = False) -> dict:
        r = {"action": "click_element", "name": e.get("name"), "type": e.get("type"),
             "loc": [e.get("x"), e.get("y")]}
        if dangerous(e):
            return {**r, "ok": False, "held": "dangerous control"}
        if dry_run:
            return {**r, "ok": True, "dry_run": True}
        out = self.call("Click", {"loc": [e["x"], e["y"]]})
        return {**r, "ok": out is not None, "detail": (out or "")[:120]}

    def type_into(self, e: dict, text: str, enter: bool = False, dry_run: bool = False) -> dict:
        r = {"action": "type_into", "name": e.get("name"), "text": text,
             "loc": [e.get("x"), e.get("y")]}
        if dangerous(e):
            return {**r, "ok": False, "held": "dangerous control"}
        if dry_run:
            return {**r, "ok": True, "dry_run": True}
        out = self.call("Type", {"loc": [e["x"], e["y"]], "text": text,
                                 "clear": False, "press_enter": bool(enter)})
        return {**r, "ok": out is not None, "detail": (out or "")[:120]}
