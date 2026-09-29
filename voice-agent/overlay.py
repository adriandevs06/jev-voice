"""overlay.py - the listening bar at the top of the screen.

Shown while the mic is on (double-tap Alt), hidden when it is off. A pulsing
dot, live level bars, the words as they are heard, and what was done
("✓ Opened Chrome"). An action that lands after the mic was closed still
flashes for a moment so the result of a cut-off sentence is visible.

It must never get in the way of the thing it controls:
  * click-through (WS_EX_TRANSPARENT) -- the mouse goes to whatever is under it
  * never activated (WS_EX_NOACTIVATE, SW_SHOWNOACTIVATE) -- the fast path types
    into the foreground window, so a bar that took focus would eat keystrokes
  * no taskbar button, not in Alt+Tab (WS_EX_TOOLWINDOW)
  * truly hidden (SW_HIDE) when off, not just transparent, so it cannot
    interfere with a fullscreen game

server.py starts it and writes JSON lines to its stdin:
    {"kind": "state"|"partial"|"you"|"action"|"level", "payload": ...}
EOF on stdin means the server is gone; the bar exits.

    python overlay.py --demo      show it for a few seconds with fake input
"""
from __future__ import annotations

import ctypes
import json
import queue
import sys
import threading
import time
from collections import deque

W, H, TOP = 460, 44, 14          # logical px, scaled by DPI
N_BARS = 16
KEY = "#010203"                  # colour-keyed to transparent: the rounded corners
BG = "#15181d"
FG, DIM = "#e8eaed", "#8b93a1"
DOT = {"listening": "#ff4d4f", "idle": "#ff4d4f", "thinking": "#f5b301", "speaking": "#4ea1ff"}
LEVEL_ON, LEVEL_OFF = "#5ee38a", "#3a414c"
HOLD_S = 2.5                     # how long a heard sentence / result stays up

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80, 0x08000000, 0x8
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2

VERB = {"open_app": "Opened", "open_url": "Opened", "web_search": "Searched",
        "focus_window": "Switched to", "type_text": "Typed", "type_into": "Typed",
        "press_keys": "Pressed", "click_element": "Clicked", "stack": "Ran", "set_volume": "Volume"}
KEY_WORDS = {"t": "Theater mode", "f": "Full screen", "f11": "Full screen", "c": "Captions",
             "playpause": "Play/pause", "nexttrack": "Next", "prevtrack": "Previous",
             "volumemute": "Mute", "pagedown": "Scrolled down", "pageup": "Scrolled up",
             "alt+left": "Back", "alt+right": "Forward", "f5": "Refreshed",
             "ctrl+tab": "Next tab", "ctrl+shift+tab": "Previous tab", "ctrl+t": "New tab"}


def describe(r: dict) -> str:
    """One line for an action receipt, the same facts the console shows."""
    if r.get("held"):
        return f"Held: {r['held']}"
    ok = "✓" if r.get("ok", True) else "✗"
    if r.get("action") == "set_volume":
        lvl = r.get("level")
        line = f"{ok} Volume " + (f"{lvl}%" if lvl is not None else ("up" if (r.get("step") or 0) > 0 else "down"))
    elif r.get("action") == "press_keys" and r.get("combo") in KEY_WORDS and r.get("ok", True):
        line = f"{ok} {KEY_WORDS[r['combo']]}"
    else:
        what = (r.get("app") or r.get("url") or (f"“{r['query']}”" if r.get("query") else None)
                or (f"“{r['text']}”" if r.get("text") else None) or r.get("combo")
                or r.get("window") or r.get("name") or r.get("target") or "")
        verb = VERB.get(r.get("action", ""), r.get("action", "Done"))
        line = f"{ok} {verb} {what}".rstrip()
    if r.get("dry_run"):
        line += "  (dry run)"
    if r.get("error"):
        line += f" · {r['error']}"
    return line


def tail(text: str, n: int) -> str:
    """Keep the END of a growing sentence visible -- that is the new part."""
    text = " ".join(text.split())
    return text if len(text) <= n else "…" + text[-(n - 1):]


def head(text: str, n: int) -> str:
    """A reply reads from the start."""
    return text if len(text) <= n else text[: n - 1] + "…"


def level_to_bar(rms: float, gate: float) -> float:
    """0..1: silence at the gate sits low, normal speech fills most of it."""
    if gate <= 0:
        gate = 0.012
    return max(0.0, min(1.0, (rms / gate) / 4.0))


class Bar:
    def __init__(self, inbox: "queue.Queue"):
        import tkinter as tk
        from tkinter import font as tkfont
        self.inbox = inbox
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)     # crisp text, real pixels
        except Exception:
            pass
        self.root = r = tk.Tk()
        s = self.s = r.winfo_fpixels("1i") / 96.0
        self.w, self.h = int(W * s), int(H * s)
        r.overrideredirect(True)
        r.attributes("-alpha", 0.0)          # the first map (once, at startup) is invisible
        r.configure(bg=KEY)
        r.geometry(f"{self.w}x{self.h}+{(r.winfo_screenwidth() - self.w) // 2}+{int(TOP * s)}")
        self.c = c = tk.Canvas(r, width=self.w, height=self.h, bg=KEY, highlightthickness=0)
        c.pack()
        self._pill(2, 2, self.w - 2, self.h - 2, (self.h - 4) / 2, fill=BG, outline="#2a3039")
        cy = self.h / 2
        self.dot_xy = (int(22 * s), cy)
        self.dot = c.create_oval(0, 0, 0, 0, fill=DOT["idle"], outline="")
        self.font = tkfont.Font(family="Segoe UI", size=10)
        self.text = c.create_text(int(38 * s), cy, anchor="w", fill=FG, font=self.font, text="Listening…")
        bw, gap = 3 * s, 3 * s
        x0 = self.w - int(20 * s) - N_BARS * (bw + gap)
        self.bar_x = [x0 + i * (bw + gap) for i in range(N_BARS)]
        self.bar_w = bw
        self.bars = [c.create_rectangle(0, 0, 0, 0, fill=LEVEL_OFF, outline="") for _ in range(N_BARS)]
        self.max_chars = max(20, int((x0 - 44 * s) / (self.font.measure("n") or 7)))
        self.levels = deque([0.0] * N_BARS, maxlen=N_BARS)
        self.status, self.muted = "idle", True
        self.shown = False
        self.hold_until = 0.0                # a sentence/result stays up this long
        self.flash_until = 0.0               # shown even though the mic is off
        self.last_level_at = 0.0

        r.update()
        u = ctypes.windll.user32
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = [ctypes.c_void_p]
        self.hwnd = u.GetParent(r.winfo_id()) or r.winfo_id()
        u.ShowWindow(self.hwnd, SW_HIDE)
        ex = u.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
                         | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        # Colour key for the corners + overall alpha, set directly: Tk's own
        # attribute calls rewrite the extended style and would drop the bits above.
        u.SetLayeredWindowAttributes(self.hwnd, 0x030201, int(0.94 * 255), LWA_COLORKEY | LWA_ALPHA)
        self.t0 = time.time()

    def _pill(self, x1, y1, x2, y2, rad, **kw):
        p = [x1 + rad, y1, x2 - rad, y1, x2, y1, x2, y1 + rad, x2, y2 - rad, x2, y2,
             x2 - rad, y2, x1 + rad, y2, x1, y2, x1, y2 - rad, x1, y1 + rad, x1, y1]
        return self.c.create_polygon(p, smooth=True, **kw)

    # -- visibility (never through Tk: that would activate the window) --------

    def show(self):
        if not self.shown:
            u = ctypes.windll.user32
            u.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            u.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.shown = True

    def hide(self):
        if self.shown:
            ctypes.windll.user32.ShowWindow(self.hwnd, SW_HIDE)
            self.shown = False

    # -- messages ---------------------------------------------------------------

    def set_text(self, text: str, hold: bool = True):
        self.c.itemconfigure(self.text, text=tail(text, self.max_chars))
        if hold:
            self.hold_until = time.time() + HOLD_S

    def handle(self, m: dict):
        kind, p = m.get("kind"), m.get("payload")
        if kind == "state":
            was_muted = self.muted
            self.status, self.muted = p.get("status", "idle"), bool(p.get("muted"))
            if was_muted and not self.muted:
                self.levels.extend([0.0] * N_BARS)
                self.set_text("Listening…", hold=False)
                self.hold_until = 0.0
        elif kind == "partial":
            self.set_text(p)
        elif kind == "you":
            self.set_text(p)
        elif kind == "action":
            self.set_text(describe(p))
            if self.muted:
                self.flash_until = time.time() + HOLD_S
        elif kind == "claude":
            text = " ".join(str(p).split())
            if text and text != "-":
                self.set_text(head(text, self.max_chars))
                if self.muted:
                    self.flash_until = time.time() + HOLD_S
        elif kind == "level":
            self.levels.append(level_to_bar(p.get("rms", 0.0), p.get("gate", 0.012)))
            self.last_level_at = time.time()

    # -- frame ------------------------------------------------------------------

    def tick(self):
        while True:
            try:
                m = self.inbox.get_nowait()
            except queue.Empty:
                break
            if m is None:                     # server gone
                self.root.destroy()
                return
            self.handle(m)
        now = time.time()
        if not self.muted or now < self.flash_until:
            self.show()
        else:
            self.hide()
        if self.shown:
            self._draw(now)
        self.root.after(33, self.tick)

    def _draw(self, now):
        c, s = self.c, self.s
        if now > self.hold_until and not self.muted:
            label = {"thinking": "Working…", "speaking": "Speaking…"}.get(self.status, "Listening…")
            c.itemconfigure(self.text, text=label)
        colour = DOT.get(self.status, DOT["idle"]) if not self.muted else DIM
        pulse = 0.75 + 0.25 * abs(((now - self.t0) * 1.6) % 2 - 1)   # 0.75..1 breathing
        rad = 5.5 * s * pulse
        x, y = self.dot_xy
        c.coords(self.dot, x - rad, y - rad, x + rad, y + rad)
        c.itemconfigure(self.dot, fill=colour)
        live = not self.muted
        full = self.h - 18 * s
        for i, (rect, lvl) in enumerate(zip(self.bars, self.levels)):
            hgt = max(3 * s, full * (lvl if live else 0.0))
            bx = self.bar_x[i]
            c.coords(rect, bx, self.h / 2 - hgt / 2, bx + self.bar_w, self.h / 2 + hgt / 2)
            c.itemconfigure(rect, fill=LEVEL_ON if live and lvl > 0.25 else LEVEL_OFF)
        if live and now - self.last_level_at > 0.12:    # no audio news: settle toward silence
            self.levels.append(self.levels[-1] * 0.5)
            self.last_level_at = now

    def run(self):
        self.root.after(33, self.tick)
        self.root.mainloop()


def read_stdin(inbox):
    for line in sys.stdin:
        try:
            inbox.put(json.loads(line))
        except ValueError:
            pass
    inbox.put(None)


def demo(inbox):
    """Fake a short session: mic on, a sentence, an action, mic off."""
    import math
    def feed():
        inbox.put({"kind": "state", "payload": {"status": "idle", "muted": False}})
        words = "open chrome and search cheap flights to miami".split()
        for i in range(60):
            inbox.put({"kind": "level", "payload": {"rms": 0.03 * (1 + math.sin(i / 2)), "gate": 0.012}})
            if i % 6 == 5 and i // 6 < len(words):
                inbox.put({"kind": "partial", "payload": " ".join(words[: i // 6 + 1])})
            time.sleep(0.06)
        inbox.put({"kind": "action", "payload": {"action": "open_app", "ok": True, "app": "Google Chrome"}})
        time.sleep(1.5)
        inbox.put({"kind": "state", "payload": {"status": "muted", "muted": True}})
        time.sleep(1.0)
        inbox.put(None)
    threading.Thread(target=feed, daemon=True).start()


def main():
    inbox: "queue.Queue" = queue.Queue()
    if "--demo" in sys.argv:
        demo(inbox)
    else:
        threading.Thread(target=read_stdin, args=(inbox,), daemon=True).start()
    Bar(inbox).run()


if __name__ == "__main__":
    main()
