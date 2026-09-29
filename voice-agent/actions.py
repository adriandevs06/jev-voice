"""actions.py - the few things the voice loop is allowed to do by itself.

These are the reversible, one-shot primitives a spoken command can trigger in
well under a second without any model in the path: open an app, open a URL,
run a web search, type text into the focused window, press a shortcut, focus a
window. Anything beyond that ("rename the last three files", "reply to Tom")
is not an action here -- it goes to the Claude Code brain, which has
computer-use, file tools and the judgement to ask before doing something
irreversible.

Every action returns a receipt dict and honours `dry_run`, so the decision
layer can be tested end to end without touching the desktop. Nothing in this
file deletes, sends, pays, posts or closes without being asked twice: those
are not primitives on purpose.
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import re
import subprocess
import time
import urllib.parse
from ctypes import wintypes
from typing import Optional

from apps import Apps

SEARCH_URL = os.environ.get("VOICE_SEARCH_URL", "https://www.google.com/search?q={q}")

# Irreversibility is decided in code first, Jev second. Text or key combos that
# match here are never typed or pressed by the fast path; they go to the brain,
# which asks. (Jev's `irreversible` noul is the second net, not the first.)
DANGEROUS_TEXT = re.compile(
    r"(rm\s+(-|dash\s+)?r?f?\s*[/\\]|rm\s+-rf|del\s+/|rmdir\s+/s|format\s+[a-z]:|shutdown|"
    r"reboot|restart|diskpart|reg\s+delete|drop\s+(table|database)|git\s+push\s+--force|"
    r"sudo\s|password|passcode)", re.I)
DANGEROUS_KEYS = {"alt+f4", "ctrl+w", "ctrl+q", "ctrl+shift+q", "ctrl+alt+delete", "ctrl+alt+del",
                  "win+l", "ctrl+s", "ctrl+shift+delete", "delete", "backspace", "ctrl+z", "ctrl+y",
                  "enter", "return", "ctrl+enter", "shift+enter", "alt+enter"}

# What each primitive may do to the world. `irreversible` primitives do not
# exist here; `visible` ones change what is on screen and are fine to fire on
# a partial sentence; `typed` ones put characters somewhere and wait for the
# sentence to finish.
KINDS = {
    "open_app": "visible", "open_url": "visible", "web_search": "visible",
    "focus_window": "visible", "stack": "visible", "type_text": "typed", "press_keys": "typed",
    "set_volume": "visible",
    # in-app control, via the UIA table in elements.py. "typed" because the
    # spoken target name is open text: wait for the words to stop changing.
    "click_element": "typed", "type_into": "typed",
}

# Key names the press/hit/push verb may be followed by. Anything else after
# those verbs is a control on screen ("press the send button"), not a keystroke.
KEYNAMES = {"ctrl", "control", "alt", "shift", "win", "windows", "cmd", "enter", "return", "tab",
            "esc", "escape", "space", "backspace", "delete", "del", "up", "down", "left", "right",
            "home", "end", "pageup", "pagedown", "plus",
            "playpause", "nexttrack", "prevtrack", "volumemute", "volumeup", "volumedown"}

# Spoken site names -> URLs, for "open youtube" / "go to gmail" when no
# installed app has that name. Installed apps win ("open spotify" is the app).
SITES = {
    "youtube": "youtube.com", "you tube": "youtube.com", "google": "google.com",
    "gmail": "mail.google.com", "google mail": "mail.google.com", "twitter": "x.com", "x": "x.com",
    "instagram": "instagram.com", "insta": "instagram.com", "facebook": "facebook.com",
    "reddit": "reddit.com", "netflix": "netflix.com", "amazon": "amazon.com", "github": "github.com",
    "chatgpt": "chatgpt.com", "chat gpt": "chatgpt.com", "tiktok": "tiktok.com", "tik tok": "tiktok.com",
    "linkedin": "linkedin.com", "twitch": "twitch.tv", "whop": "whop.com", "hulu": "hulu.com",
    "google drive": "drive.google.com", "google docs": "docs.google.com",
    "google sheets": "sheets.google.com", "google calendar": "calendar.google.com",
    "google maps": "maps.google.com", "vercel": "vercel.com", "railway": "railway.app",
    "wikipedia": "wikipedia.org", "canva": "canva.com", "espn": "espn.com",
}


def site_url(spoken: str) -> Optional[str]:
    k = re.sub(r"[^a-z ]", "", (spoken or "").lower()).strip()
    k = re.sub(r"^(?:the|my)\s+", "", k)
    k = re.sub(r"\s+(?:website|site|page|dot com|com|homepage)$", "", k).strip()
    return "https://" + SITES[k] if k in SITES else None


def looks_like_keys(s: str) -> bool:
    words = [w for w in re.split(r"[\s+]+", (s or "").lower().strip()) if w]
    if not words:
        return False
    return all(w in KEYNAMES or len(w) == 1 or re.fullmatch(r"f\d{1,2}", w) for w in words)

# No-arm zones: while a window whose title matches this is in the foreground,
# the fast path does nothing at all -- no primitive fires, nothing is typed,
# and the console says why. A futures platform shares this desktop; an
# always-on mic next to it is only acceptable with this in code, not in a
# model. Password and 2FA dialogs are in the same list for the same reason.
NO_ARM_TITLES = re.compile(
    r"(tradovate|ninjatrader|tradingview|topstep|apex ?trader|projectx|rithmic|metatrader|"
    r"\bmt[45]\b|robinhood|thinkorswim|tastytrade|webull|interactive brokers|1password|bitwarden|"
    r"keepass|sign in|log ?in|two-factor|2fa|verification code|password|passcode|authenticator)", re.I)

# One spoken word per fired primitive, said by the local voice so a wrong fire
# is heard before the next one lands.
RECEIPT = {"open_url": "opening", "web_search": "searching", "type_text": "typing",
           "press_keys": "keys", "focus_window": "switching", "stack": "stack",
           "click_element": "clicked", "type_into": "typing"}
STACKS_FILE = pathlib.Path(__file__).with_name("stacks.json")

_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None


def _find_windows(substr: str) -> list:
    hits = []
    if not _user32:
        return hits

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            if substr.lower() in buf.value.lower():
                hits.append((hwnd, buf.value))
        return True

    _user32.EnumWindows(cb, 0)
    hits.sort(key=lambda h: (h[1].strip().lower() != substr.strip().lower(), len(h[1])))
    return hits


def _kb():
    from pynput import keyboard
    return keyboard, keyboard.Controller()


def foreground_title() -> str:
    """Title of the window that has focus right now ('' off Windows)."""
    if not _user32:
        return ""
    hwnd = _user32.GetForegroundWindow()
    n = _user32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def receipt_word(r: dict) -> str:
    """What the local voice says for a fired receipt."""
    if r.get("held"):
        return "held"
    if r.get("action") == "open_app":
        return (r.get("app") or "app").split(" ")[0]
    if r.get("action") == "stack":
        return r.get("name", "stack")
    return RECEIPT.get(r.get("action", ""), "done")


def load_stacks(path: pathlib.Path = STACKS_FILE) -> dict:
    """stacks.json: {"morning": [{"open_app": "Google Chrome"}, {"open_url": "x.com"}, ...]}.
    Each step is one primitive with one argument. Missing file -> no stacks."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for name, steps in (data or {}).items():
        if isinstance(steps, list) and all(isinstance(s, dict) and len(s) == 1 for s in steps):
            out[str(name).lower()] = steps
    return out


def normalise_url(spoken: str) -> Optional[str]:
    """'x dot com' / 'x.com' / 'https://x.com/foo' -> 'https://x.com/foo'."""
    s = (spoken or "").strip().lower()
    s = re.sub(r"\s+dot\s+", ".", s)
    s = re.sub(r"\s+slash\s+", "/", s)
    s = s.replace(" ", "")
    if not s:
        return None
    if not re.match(r"^https?://", s):
        if not re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", s):
            return None
        s = "https://" + s
    return s


class Actions:
    def __init__(self, apps: Optional[Apps] = None, dry_run: bool = False, on_log=None,
                 foreground=foreground_title, stacks: Optional[dict] = None, desktop=None):
        self.apps = apps or Apps()
        self.desktop = desktop            # elements.Desktop, or None -> no in-app control
        self.dry_run = dry_run
        self.on_log = on_log or (lambda *_: None)
        self.foreground = foreground          # injectable for tests
        self.stacks = stacks if stacks is not None else load_stacks()
        self.log: list = []

    def blocked(self) -> Optional[str]:
        """The foreground title if it is a no-arm zone, else None."""
        try:
            t = self.foreground() or ""
        except Exception:
            return None
        return t if NO_ARM_TITLES.search(t) else None

    def _receipt(self, r: dict) -> dict:
        r.setdefault("t", time.time())
        self.log.append(r)
        self.on_log(r)
        return r

    # -- primitives ------------------------------------------------------------

    def open_app(self, name: str) -> dict:
        target = self.apps.resolve(name) or name
        r = self.apps.launch(target, dry_run=self.dry_run)
        r.update({"action": "open_app", "asked": name})
        return self._receipt(r)

    def open_url(self, url: str) -> dict:
        u = normalise_url(url)
        if not u:
            return self._receipt({"action": "open_url", "ok": False, "asked": url,
                                  "error": "not a url"})
        if not self.dry_run:
            try:
                os.startfile(u)
            except OSError as e:
                return self._receipt({"action": "open_url", "ok": False, "url": u, "error": str(e)})
        return self._receipt({"action": "open_url", "ok": True, "url": u, "dry_run": self.dry_run})

    def web_search(self, query: str) -> dict:
        q = (query or "").strip(" .,!?")
        if not q:
            return self._receipt({"action": "web_search", "ok": False, "error": "empty query"})
        if "youtube" in (self.foreground() or "").lower():
            # On YouTube, "look up X" means YouTube's own search, in place:
            # "/" focuses its search box from anywhere on the page.
            if not self.dry_run:
                keyboard, kb = _kb()
                kb.press("/"); kb.release("/")
                time.sleep(0.15)
                with kb.pressed(keyboard.Key.ctrl):
                    kb.press("a"); kb.release("a")
                kb.type(q)
                kb.press(keyboard.Key.enter); kb.release(keyboard.Key.enter)
            return self._receipt({"action": "web_search", "ok": True, "query": q, "site": "youtube",
                                  "dry_run": self.dry_run})
        u = SEARCH_URL.format(q=urllib.parse.quote_plus(q))
        if not self.dry_run:
            try:
                os.startfile(u)
            except OSError as e:
                return self._receipt({"action": "web_search", "ok": False, "query": q, "error": str(e)})
        return self._receipt({"action": "web_search", "ok": True, "query": q, "url": u,
                              "dry_run": self.dry_run})

    def focus_window(self, title: str) -> dict:
        hits = _find_windows(title)
        if not hits:
            if self.dry_run:
                return self._receipt({"action": "focus_window", "ok": True, "asked": title,
                                      "window": None, "dry_run": True})
            return self._receipt({"action": "focus_window", "ok": False, "asked": title,
                                  "error": "no window"})
        hwnd, found = hits[0]
        if not self.dry_run:
            keyboard, kb = _kb()
            _user32.ShowWindow(hwnd, 9)
            kb.press(keyboard.Key.alt); kb.release(keyboard.Key.alt)
            _user32.SetForegroundWindow(hwnd)
            time.sleep(0.18)
        return self._receipt({"action": "focus_window", "ok": True, "asked": title, "window": found,
                              "dry_run": self.dry_run})

    def type_text(self, text: str, enter: bool = False) -> dict:
        t = (text or "").strip()
        if not t:
            return self._receipt({"action": "type_text", "ok": False, "error": "empty"})
        if DANGEROUS_TEXT.search(t):
            return self._receipt({"action": "type_text", "ok": False, "text": t, "held": "dangerous text"})
        if not self.dry_run:
            keyboard, kb = _kb()
            kb.type(t)
            if enter:
                kb.press(keyboard.Key.enter); kb.release(keyboard.Key.enter)
        return self._receipt({"action": "type_text", "ok": True, "text": t, "enter": enter,
                              "dry_run": self.dry_run})

    def press_keys(self, combo: str) -> dict:
        """'ctrl+t', 'win+d', 'enter', 'alt+tab'. Unknown key names fail closed."""
        names = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
        if not names:
            return self._receipt({"action": "press_keys", "ok": False, "error": "empty"})
        if "+".join(names) in DANGEROUS_KEYS:
            return self._receipt({"action": "press_keys", "ok": False, "combo": "+".join(names),
                                  "held": "dangerous keys"})
        keyboard, kb = _kb()
        special = {"ctrl": keyboard.Key.ctrl, "control": keyboard.Key.ctrl,
                   "alt": keyboard.Key.alt, "shift": keyboard.Key.shift,
                   "win": keyboard.Key.cmd, "windows": keyboard.Key.cmd, "cmd": keyboard.Key.cmd,
                   "enter": keyboard.Key.enter, "return": keyboard.Key.enter,
                   "tab": keyboard.Key.tab, "esc": keyboard.Key.esc, "escape": keyboard.Key.esc,
                   "space": keyboard.Key.space, "backspace": keyboard.Key.backspace,
                   "delete": keyboard.Key.delete, "up": keyboard.Key.up, "down": keyboard.Key.down,
                   "left": keyboard.Key.left, "right": keyboard.Key.right,
                   "home": keyboard.Key.home, "end": keyboard.Key.end,
                   "pageup": keyboard.Key.page_up, "pagedown": keyboard.Key.page_down,
                   "playpause": keyboard.Key.media_play_pause, "nexttrack": keyboard.Key.media_next,
                   "prevtrack": keyboard.Key.media_previous, "volumemute": keyboard.Key.media_volume_mute,
                   "volumeup": keyboard.Key.media_volume_up, "volumedown": keyboard.Key.media_volume_down}
        keys = []
        for n in names:
            if n in special:
                keys.append(special[n])
            elif len(n) == 1:
                keys.append(n)
            elif re.fullmatch(r"f\d{1,2}", n):
                keys.append(getattr(keyboard.Key, n))
            else:
                return self._receipt({"action": "press_keys", "ok": False, "combo": combo,
                                      "error": f"unknown key {n!r}"})
        if not self.dry_run:
            for k in keys:
                kb.press(k)
            for k in reversed(keys):
                kb.release(k)
        return self._receipt({"action": "press_keys", "ok": True, "combo": "+".join(names),
                              "dry_run": self.dry_run})

    def set_volume(self, level: Optional[int] = None, step: Optional[int] = None) -> dict:
        """System volume through the media keys -- no audio API, same on every
        device. Windows moves 2 points per press, so an exact level is: all
        the way down (50 presses), then up level/2."""
        if level is None and step is None:
            return self._receipt({"action": "set_volume", "ok": False, "error": "no level"})
        if level is not None:
            level = max(0, min(100, int(level)))
        if not self.dry_run:
            keyboard, kb = _kb()

            def tap(key, n):
                for _ in range(n):
                    kb.press(key); kb.release(key)
            if level is not None:
                tap(keyboard.Key.media_volume_down, 50)
                tap(keyboard.Key.media_volume_up, round(level / 2))
            else:
                tap(keyboard.Key.media_volume_up if step > 0 else keyboard.Key.media_volume_down,
                    max(1, round(abs(int(step)) / 2)))
        return self._receipt({"action": "set_volume", "ok": True, "level": level, "step": step,
                              "dry_run": self.dry_run})

    # -- in-app control (needs a Desktop) ---------------------------------------

    def _pick(self, target: str) -> tuple:
        """(element, receipt_if_we_should_not_act). Name match only -- Jev's
        target Choice plugs in here when a key exists."""
        from elements import candidates, dangerous, match
        if self.desktop is None or not self.desktop.available():
            return None, {"ok": False, "error": "no desktop control (windows-mcp not installed)"}
        els = self.desktop.elements()
        if not els:
            return None, {"ok": False, "error": "nothing readable on screen"}
        i, score, runner = match(els, target)
        if i is None:
            opts = [e["name"] for e in candidates(els, target)]
            # "ambiguous" means several plausible controls -> read them out
            # numbered; "no match" means none at all -> say so and stop.
            return None, {"ok": False, "held": "ambiguous target" if opts else "no matching control",
                          "options": opts, "best": round(score, 2), "runner_up": round(runner, 2)}
        # The gate lives here, at the one chokepoint every driver goes through,
        # not only inside Desktop -- swapping the driver must not lose it.
        if dangerous(els[i]):
            return None, {"ok": False, "held": "dangerous control", "name": els[i]["name"]}
        return els[i], None

    def click_element(self, target: str) -> dict:
        e, stop = self._pick(target)
        if stop:
            return self._receipt({"action": "click_element", "asked": target, **stop})
        r = self.desktop.click(e, dry_run=self.dry_run)
        return self._receipt({**r, "asked": target, "dry_run": self.dry_run})

    def type_into(self, target: str, text: str, enter: bool = False) -> dict:
        if DANGEROUS_TEXT.search(text or ""):
            return self._receipt({"action": "type_into", "asked": target, "text": text,
                                  "ok": False, "held": "dangerous text"})
        e, stop = self._pick(target)
        if stop:
            return self._receipt({"action": "type_into", "asked": target, "text": text, **stop})
        r = self.desktop.type_into(e, text, enter=enter, dry_run=self.dry_run)
        return self._receipt({**r, "asked": target, "dry_run": self.dry_run})

    def stack(self, name: str) -> dict:
        """Run a named chain from stacks.json, one primitive after another."""
        steps = self.stacks.get((name or "").lower())
        if not steps:
            return self._receipt({"action": "stack", "ok": False, "name": name, "error": "no such stack"})
        results = []
        for step in steps:
            (act, arg), = step.items()
            key = {"open_app": "name", "open_url": "url", "web_search": "query", "type_text": "text",
                   "press_keys": "combo", "focus_window": "title"}.get(act)
            if not key or act == "stack":
                results.append({"action": act, "ok": False, "error": "not allowed in a stack"})
                continue
            results.append(self.run(act, **{key: arg}))
            if not self.dry_run:
                time.sleep(0.35)                 # let the previous window come up
        ok = all(r.get("ok") for r in results)
        return self._receipt({"action": "stack", "ok": ok, "name": name, "steps": results,
                              "dry_run": self.dry_run})

    # -- dispatch --------------------------------------------------------------

    def run(self, action: str, **kw) -> dict:
        fn = getattr(self, action, None)
        if action not in KINDS or fn is None:
            return self._receipt({"action": action, "ok": False, "error": "not a primitive"})
        zone = self.blocked()
        if zone:
            return self._receipt({"action": action, "ok": False, "held": f"no-arm: {zone[:60]}", **kw})
        return fn(**kw)
