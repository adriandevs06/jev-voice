"""hotkey.py - double-tap Alt to turn the mic on, double-tap again to turn it off.

Runs as its OWN process (server.py starts it). A low-level keyboard hook sits
in front of every keystroke on the machine: while its callback runs, all
typing waits. In the server process the callback would need the GIL that
whisper decoding and the brain's pipes are busy with, so a stall there would
lag every key the user presses (and Windows silently unhooks a callback that
keeps timing out). Here the process does nothing else, so the callback is
always a few microseconds.

What counts as a double tap (DoubleTap below):
  * Alt pressed and released on its own -- nothing else held, nothing else
    pressed in between -- in under TAP_MAX. Alt+Tab, Alt+F4, Ctrl+Alt+... never
    count, and neither does holding Alt.
  * A second such tap whose release is within GAP_MAX of the first's.
  * Only PHYSICAL keys. actions.py taps Alt itself (the focus-steal trick)
    and those events arrive flagged as injected; they are ignored, so the
    agent can never toggle its own mic.

Nothing is logged, stored or sent except "toggle" to the local server. No
keystroke leaves the hook callback.

    python hotkey.py --port 8420 --parent <pid>    (token in VOICE_TOKEN)
    python hotkey.py --probe                       prove the hook is live
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import urllib.request
from ctypes import wintypes

TAP_MAX_MS = 350       # longer than this is a hold, not a tap
GAP_MAX_MS = 500       # first release -> second release (Windows' double-click default)

VK_ALTS = {0x12, 0xA4, 0xA5}                      # VK_MENU, VK_LMENU, VK_RMENU
VK_PROBE = 0x87                                   # F24: no keyboard has it; --probe only
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
LLKHF_INJECTED = 0x10
WH_KEYBOARD_LL = 13


class DoubleTap:
    """Pure logic: feed (vk, down, injected, t_ms) events, get True on a double tap."""

    def __init__(self, tap_max_ms: int = TAP_MAX_MS, gap_max_ms: int = GAP_MAX_MS):
        self.tap_max, self.gap_max = tap_max_ms, gap_max_ms
        self.held: set[int] = set()        # other physical keys currently down
        self.alt_down_at = None            # when the current Alt press began
        self.clean = False                 # nothing else touched during this press
        self.last_tap = None               # release time of a pending first tap

    def feed(self, vk: int, down: bool, injected: bool, t: int) -> bool:
        if injected:
            return False
        if vk in VK_ALTS:
            if down:
                if self.alt_down_at is None:               # ignore auto-repeat
                    self.alt_down_at, self.clean = t, not self.held
                return False
            if self.alt_down_at is None:
                return False
            tap = self.clean and not self.held and (t - self.alt_down_at) <= self.tap_max
            self.alt_down_at = None
            if not tap:
                self.last_tap = None
                return False
            if self.last_tap is not None and (t - self.last_tap) <= self.gap_max:
                self.last_tap = None
                return True
            self.last_tap = t
            return False
        if down:
            self.held.add(vk)
            self.clean = False                             # Alt+Tab etc. is not a tap
            self.last_tap = None                           # alt, x, alt is not a double tap
        else:
            self.held.discard(vk)
        return False


# -- the hook -----------------------------------------------------------------

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def run_hook(on_event):
    """Install the hook on this thread and pump messages forever. on_event
    gets (vk, down, injected, t_ms) and must return immediately. The key is
    always passed on -- nothing is swallowed."""
    def proc(code, wparam, lparam):
        if code == 0 and wparam in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
            k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            try:
                on_event(k.vkCode, wparam in (WM_KEYDOWN, WM_SYSKEYDOWN),
                         bool(k.flags & LLKHF_INJECTED), k.time)
            except Exception:
                pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    cb = HOOKPROC(proc)                                    # keep a reference alive
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        raise OSError(f"SetWindowsHookExW failed: {ctypes.get_last_error()}")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def exit_with_parent(pid: int):
    """Die when the server does, however it dies (closed window, crash, Ctrl+C)."""
    SYNCHRONIZE = 0x00100000
    h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        os._exit(0)

    def wait():
        kernel32.WaitForSingleObject(h, 0xFFFFFFFF)
        os._exit(0)
    threading.Thread(target=wait, daemon=True).start()


def toggle(port: int, token: str):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/toggle", data=b"", method="POST",
                                 headers={"X-Voice-Token": token})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        import winsound
        winsound.Beep(300, 200)                            # server not up yet / gone


def probe(timeout_s: float = 5.0) -> bool:
    """Install the real hook, inject one F24 tap, and report whether the hook
    saw it. Proves the hook + message pump without touching Alt or printing
    any real keystroke."""
    seen = threading.Event()

    def on_event(vk, down, injected, t):
        if vk == VK_PROBE and injected:
            seen.set()
    threading.Thread(target=run_hook, args=(on_event,), daemon=True).start()

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    import time
    for _ in range(int(timeout_s / 0.25)):
        time.sleep(0.25)
        for flags in (0, 2):                               # down, KEYEVENTF_KEYUP
            i = INPUT(type=1)
            i.ki = KEYBDINPUT(wVk=VK_PROBE, dwFlags=flags)
            user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        if seen.wait(0.25):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--parent", type=int)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    if a.probe:
        ok = probe()
        print("hook ok" if ok else "hook NOT receiving events")
        sys.exit(0 if ok else 1)

    token = os.environ.get("VOICE_TOKEN", "")
    if a.parent:
        exit_with_parent(a.parent)
    tap = DoubleTap()

    def on_event(vk, down, injected, t):
        if tap.feed(vk, down, injected, t):
            threading.Thread(target=toggle, args=(a.port, token), daemon=True).start()
    run_hook(on_event)


if __name__ == "__main__":
    main()
