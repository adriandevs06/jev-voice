"""
server.py - localhost voice console for Claude Code.

    python server.py            then open http://127.0.0.1:8420

The brain is a single persistent `claude` process in stream-json mode. That matters:
spawning one per turn re-creates ~20k tokens of prompt cache every time you speak
(measured), while a persistent session creates 18. Same session id the whole way, so
context, tools, skills and computer-use all survive across turns -- no functionality
is traded away for the UI.

Audio stays in Python rather than the browser: the mic path here is the one already
proven out in listen.py, and it avoids WebRTC entirely. The browser is a control
panel -- mute, stop, transcript, status -- talking over a WebSocket.

2026-09-21: a fast path in front of the brain. The mic now emits PARTIAL
transcripts every ~400 ms (stream_ears.py); each one goes through intent.py,
which asks Jev (TypeSafe's decision model, ~300 ms) whether the sentence already
holds a complete command and fires the matching primitive from actions.py --
open an app, open a URL, search, type, press keys, focus a window -- while you
are still talking. Whatever is left when the sentence ends (multi-step work,
anything irreversible, anything the rules and Jev cannot place) goes to the
Claude brain exactly as before. Without a Jev key the fast path still runs on
the regex rules alone; it just waits for the pause instead of firing early.

2026-09-23: the fast path can also act INSIDE an app. elements.py reads the
foreground window's UI Automation tree through the same windows-mcp server the
brain uses, so "click the settings button" / "type hello into the text editor"
resolve against real controls by name -- no key, no model. Anything ambiguous is
read back numbered instead of guessed, and a control whose name looks like
send/post/pay/delete is never pressed by the fast path.

    VOICE_DRY_RUN=1   log what would fire, touch nothing (safe first run)
    VOICE_FAST=0      disable the fast path entirely (old behaviour)
    VOICE_CLICK=0     keep the fast path but drop in-app control
    VOICE_HANDS=0     start the brain without windows-mcp
    VOICE_HOTKEY=0    no double-tap-Alt mic toggle
    VOICE_OVERLAY=0   no listening bar at the top of the screen
    VOICE_HOTWORDS=0  do not bias Whisper toward app names / commands (vocab.txt adds words)

2026-09-24: double-tap Alt toggles the mic from any window (hotkey.py, its own
process). A rising beep means listening, a falling one means off; turning it
off mid-sentence still acts on what was said. While the mic is on, a
click-through bar at the top of the screen (overlay.py) shows live levels, the
words as they are heard, and what was done.
"""

import asyncio
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

import hands
import router
from actions import Actions, receipt_word
from apps import Apps
from intent import Controller
from jev import Jev
from stream_ears import StreamEars, hotwords, pick_mic


HERE = Path(__file__).parent
PORT = 8420
DECISIONS_LOG = HERE / "decisions.jsonl"      # gitignored: every routed partial, for replay + calibration
ACCURATE_TIMEOUT_S = 2.5                      # provisional final stands if the accurate pass is slower
# Only the console page itself may open the socket. A WebSocket is not covered
# by CORS: without this, any website open in any tab could connect to
# ws://127.0.0.1:8420/ws and send {"cmd": "say"} -- i.e. drive the machine.
CONSOLE_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
HOTKEY_TOKEN = secrets.token_urlsafe(24)      # per run; only hotkey.py is handed it
# Silent by default: the bar at the top of the screen is the feedback. Spoken
# replies and receipts are opt-in (first real use, 2026-09-24: talking back was unwanted).
SPEAK = os.environ.get("VOICE_SPEAK", "0") == "1"
# The brain does multi-step screen work; per step, speed matters more than
# depth. Sonnet 5 measured 2-10 s per step in the first session.
BRAIN_MODEL = os.environ.get("VOICE_BRAIN_MODEL", "haiku")
# ...and per request, the router (router.py) moves research and multi-step
# work up to this one on the same running brain.
BRAIN_SMART = os.environ.get("VOICE_BRAIN_SMART", "sonnet")
ROUTER = os.environ.get("VOICE_ROUTER", "1") != "0"

ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")


def env(name):
    """Read an env var including User/Machine scope.

    os.environ is only the snapshot this process inherited at launch, so a var set
    with setx / [Environment]::SetEnvironmentVariable(...,'User') after the parent
    shell started is invisible to it. That silently downgraded speech to the SAPI
    fallback -- it "worked", just in the robot voice. Go to the registry instead.
    """
    v = os.environ.get(name)
    if v:
        return v
    try:
        import winreg
    except ImportError:
        return None
    for root, path in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, path) as k:
                val, _ = winreg.QueryValueEx(k, name)
                if val:
                    return val
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# the brain: one long-lived Claude Code process
# ---------------------------------------------------------------------------

class Brain:
    SYSTEM = (
        "The user talks to this computer by voice while they work; you are the part that "
        "carries out requests the instant commands could not. Your reply is shown as ONE "
        "short line on a small bar at the top of the screen and is never spoken. Reply in "
        "at most eight words, no markdown: what you did ('Done -- third video playing') or "
        "the one thing you need. Never narrate plans or steps; just do it. If the words are "
        "not a request for the computer -- a reaction, thinking aloud, talk to someone else "
        "-- do nothing and reply with a single dash. "
        "Research is different: when asked to find, look up, research, compare or keep an "
        "eye out for something, use WebSearch and WebFetch (not the browser on screen), "
        "then write the full findings -- links, what each thing is, why it matters to him -- "
        "to a new markdown file in " + str(hands.FINDS_DIR) + " named "
        "YYYY-MM-DD-topic.md. Your reply is then one line: how many finds and the file name."
    )

    @staticmethod
    def launcher():
        """Returns the argv prefix that starts Claude Code.

        `claude` on PATH is a .ps1/.cmd shim on Windows, and CreateProcess can only
        execute a real binary -- Popen(["claude", ...]) dies with WinError 2. The shim
        forwards to bin/claude.exe, so resolve that directly; fall back to running the
        shim through cmd.exe, which can.
        """
        from shutil import which
        override = os.environ.get("VOICE_CLAUDE_EXE")
        if override and Path(override).exists():
            return [override]
        candidates = []
        npm = (Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules"
               / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
        if npm.exists():
            candidates.append(npm)
        # The desktop app embeds full CLIs under %APPDATA%\Claude\claude-code\<ver>\;
        # they are usually newer than the npm one (2.1.275 vs 2.1.220 today) and
        # `--permission-prompts none` needs >= 2.1.259. Newest wins.
        embedded = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"
        if embedded.exists():
            candidates += [p / "claude.exe" for p in embedded.iterdir() if (p / "claude.exe").exists()]
        if candidates:
            def ver(p):
                # embedded dirs are named by version; the npm one has to be asked
                return hands.parse_version(p.parent.name) if p.parent.parent == embedded \
                    else hands.cli_version([str(p)])
            return [str(max(candidates, key=ver))]
        found = which("claude")
        if not found:
            raise RuntimeError("claude CLI not found on PATH")
        if found.lower().endswith(".exe"):
            return [found]
        return ["cmd", "/c", found]

    @classmethod
    def argv(cls, launcher: list, version: tuple, want_hands: bool) -> list:
        """The full command line. Pure, so tests can check it without a process."""
        return [
            *launcher, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--strict-mcp-config",
            # The Stop hook in ~/.claude/settings.json speaks each turn aloud, and this
            # child inherits it -- so the reply got spoken twice, once by the hook and
            # once by Speaker below. The server owns speech here; the child stays mute.
            "--settings", json.dumps({"disableAllHooks": True}),
            "--verbose",
            "--model", BRAIN_MODEL,
            "--append-system-prompt", cls.SYSTEM + (hands.SYSTEM_HANDS if want_hands and hands.available() else ""),
            *hands.brain_args(version, enabled=want_hands),
        ]

    def set_model(self, model: str):
        """Switch the running brain's model for the next request: a stream-json
        control request, answered by a control_response (logged). No restart,
        no second process."""
        if not model or model == getattr(self, "model", None):
            return
        req = {"type": "control_request", "request_id": f"model-{int(time.time() * 1000)}",
               "request": {"subtype": "set_model", "model": model}}
        with self.lock:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        self.model = model

    def __init__(self, on_event, want_hands: bool = True):
        self.on_event = on_event
        self.session_id = None
        self.hands = None                     # None until system/init says
        self.lock = threading.Lock()
        self.model = BRAIN_MODEL
        launcher = self.launcher()
        self.version = hands.cli_version(launcher)
        self.want_hands = want_hands
        cmd = self.argv(launcher, self.version, want_hands)
        hands.BRAIN_CWD.mkdir(exist_ok=True)   # acceptEdits may write here and nowhere else
        hands.FINDS_DIR.mkdir(exist_ok=True)   # ...and here: research results
        self.auth = hands.auth_status(launcher)
        if self.auth.get("loggedIn") is False:
            print("\n  !! the Claude CLI is not logged in, so the brain cannot answer anything.\n"
                  "     Run once in a normal terminal:  claude auth login\n", flush=True)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, encoding="utf-8", cwd=str(hands.BRAIN_CWD),
            env=hands.brain_env(),
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def handle_line(self, line: str):
        """One stream-json line from the child. Split out so tests can replay lines."""
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return
        t = e.get("type")
        if t == "system" and e.get("subtype") == "init":
            self.session_id = e.get("session_id")
            self.hands = hands.hands_from_init(e)
            self.on_event("hands", self.hands)
        elif t == "assistant":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    self.on_event("text", c["text"].strip())
                elif c.get("type") == "tool_use":
                    self.on_event("tool", c.get("name", "?"))
        elif t == "control_response":
            self.on_event("control", e.get("response") or {})
        elif t == "result":
            u = e.get("usage", {})
            self.on_event("done", {
                "cost": e.get("total_cost_usd"),
                "cache_created": u.get("cache_creation_input_tokens"),
                "cache_read": u.get("cache_read_input_tokens"),
                "permission_denials": len(e.get("permission_denials") or []),
            })

    def _pump(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.handle_line(line)

    def send(self, text):
        msg = {"type": "user",
               "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        with self.lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# speech out, interruptible
# ---------------------------------------------------------------------------

class LocalVoice:
    """One persistent PowerShell/SAPI process: a line in, a word out, ~0 spawn
    cost after the first. Receipts ("Chrome", "searching", "held") go here so a
    wrong fire is heard within half a second; ElevenLabs stays for the brain."""
    SCRIPT = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate = 3; "
              "while (($l = [Console]::In.ReadLine()) -ne $null) { if ($l) { $s.Speak($l) } }")

    def __init__(self):
        self.proc = None
        self.until = 0.0
        self.lock = threading.Lock()

    def _ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", self.SCRIPT],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8")

    def say(self, word: str):
        word = (word or "").strip()
        if not word:
            return
        try:
            with self.lock:
                self._ensure()
                self.proc.stdin.write(word + "\n")
                self.proc.stdin.flush()
            self.until = max(self.until, time.time()) + 0.25 + 0.08 * len(word)
        except Exception:
            pass

    def busy(self) -> bool:
        return time.time() < self.until


class Speaker:
    def __init__(self, on_state):
        self.on_state = on_state
        self.q = queue.Queue()
        self.proc = None
        self.speaking = False
        self.ffplay = self._find_ffplay()
        self.local = LocalVoice()
        threading.Thread(target=self._loop, daemon=True).start()

    def receipt(self, word: str):
        self.local.say(word)

    def receipting(self) -> bool:
        """True while a receipt is being spoken: the mic ignores those frames."""
        return self.local.busy()

    @staticmethod
    def _find_ffplay():
        from shutil import which
        f = which("ffplay")
        if f:
            return f
        ff = which("ffmpeg")
        return str(Path(ff).parent / "ffplay.exe") if ff else None

    def say(self, text):
        for part in re.split(r"(?<=[.!?])\s+", text.strip()):
            if part.strip():
                self.q.put(part.strip())

    def shutup(self):
        """Barge-in: drop the queue and kill playback mid-word."""
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def _loop(self):
        while True:
            text = self.q.get()
            self.speaking = True
            self.on_state("speaking")
            try:
                self._play(text)
            except Exception as e:
                print(f"  tts: {e}", file=sys.stderr)
            finally:
                self.speaking = False
                if self.q.empty():
                    self.on_state("idle")

    def _play(self, text):
        key = env("ELEVENLABS_API_KEY")
        voice = env("ELEVENLABS_VOICE_ID")
        if not (key and voice and self.ffplay):
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(HERE / "tts.ps1"), text, "-Force", "sapi"])
            return

        import urllib.request
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"
            f"?output_format=mp3_22050_32&optimize_streaming_latency=3",
            data=json.dumps({"text": text, "model_id": ELEVEN_MODEL,
                             "voice_settings": {"stability": 0.45, "similarity_boost": 0.75}}).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json"},
        )
        self.proc = subprocess.Popen([self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-"],
                                     stdin=subprocess.PIPE)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                while chunk := r.read(4096):
                    if self.proc.poll() is not None:
                        break
                    self.proc.stdin.write(chunk)
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self.proc.wait()


# ---------------------------------------------------------------------------

app = FastAPI()
STATE = {"status": "idle", "muted": True, "session": None, "clients": set()}
loop_ref = {"loop": None}
brain = speaker = ears = ctl = None


VOICE_LOG = HERE / ".voice.log"               # gitignored: timeline of what was heard and done


def vlog(kind, payload):
    try:
        if VOICE_LOG.exists() and VOICE_LOG.stat().st_size > 2_000_000:
            VOICE_LOG.replace(VOICE_LOG.with_suffix(".log.1"))
        with open(VOICE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {kind:8s} "
                    f"{json.dumps(payload, ensure_ascii=False)[:300]}\n")
    except Exception:
        pass


def broadcast(kind, payload):
    if kind != "level":
        vlog(kind, payload)
    if kind in OVERLAY_KINDS:
        overlay_send(kind, payload)
    loop = loop_ref["loop"]
    if not loop:
        return
    msg = json.dumps({"kind": kind, "payload": payload})
    for ws in list(STATE["clients"]):
        asyncio.run_coroutine_threadsafe(_safe_send(ws, msg), loop)


async def _safe_send(ws, msg):
    try:
        await ws.send_text(msg)
    except Exception:
        STATE["clients"].discard(ws)


def set_state(s):
    STATE["status"] = s
    broadcast("state", {"status": s, "muted": STATE["muted"]})


def set_muted(muted: bool, beep: bool = False):
    """One place that opens and closes the mic (console button, hotkey).

    Opening: the rising beep plays BEFORE the mic opens, so the mic never hears
    it as speech. Closing: the mic closes first, then the falling beep; a
    sentence in progress is finished, not thrown away (stream_ears)."""
    STATE["muted"] = muted
    if muted:
        ears.muted = True
        set_state("muted")
        if beep:
            threading.Thread(target=_beep, args=(620, 90), daemon=True).start()
        return

    def open_mic():
        if beep:
            _beep(880, 90)
            time.sleep(0.12)                     # let the speaker ring out
        if not STATE["muted"]:                   # not closed again meanwhile
            ears.muted = False
            set_state("idle")
    threading.Thread(target=open_mic, daemon=True).start()


def _beep(freq, ms):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        pass


@app.get("/")
async def index():
    return HTMLResponse((HERE / "ui.html").read_text(encoding="utf-8"))


@app.post("/toggle")
async def toggle(request: Request):
    """The double-tap-Alt hotkey. Needs the per-run token in a custom header:
    a web page cannot send one without a CORS preflight, which this server
    never answers, so no site can flip the mic."""
    if not ears or not secrets.compare_digest(request.headers.get("x-voice-token", ""), HOTKEY_TOKEN):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    set_muted(not STATE["muted"], beep=True)
    return {"muted": STATE["muted"]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if ws.headers.get("origin") not in CONSOLE_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    STATE["clients"].add(ws)
    await ws.send_text(json.dumps({"kind": "state",
                                   "payload": {"status": STATE["status"], "muted": STATE["muted"]}}))
    try:
        while True:
            data = json.loads(await ws.receive_text())
            cmd = data.get("cmd")
            if cmd == "mute":
                speaker.shutup()
                set_muted(True)
            elif cmd == "unmute":
                set_muted(False)
            elif cmd == "stop":
                speaker.shutup()
                set_state("idle")
            elif cmd == "say":
                on_final(data.get("text", ""))
    except WebSocketDisconnect:
        STATE["clients"].discard(ws)


def handle_user_text(text):
    if not text.strip():
        return
    broadcast("you", text)
    if brain is None:
        broadcast("action", {"action": "multi-step", "ok": False,
                             "held": "needs Claude Code (claude.com/claude-code)"})
        set_state("idle")
        return
    if ROUTER:
        tier, why = router.route(text, ctl.jev if ctl is not None else None)
        brain.set_model(router.model_for(tier, BRAIN_MODEL, BRAIN_SMART))
        broadcast("route", {"tier": tier, "model": brain.model, "why": why})
    set_state("thinking")
    brain.send(text)


# ---- the fast path: partials -> intent -> primitives; leftovers -> brain ----

_accurate_timer = {"t": None}


def _no_arm():
    """The foreground window is a no-arm zone: say so once, do nothing."""
    zone = ctl.actions.blocked() if ctl is not None else None
    if zone:
        broadcast("action", {"action": "fast path", "ok": False, "held": f"no-arm: {zone[:60]}"})
    return zone


def _fired(rs):
    for r in rs:
        broadcast("action", r)
        if SPEAK and speaker is not None and r.get("ok") and not r.get("dry_run"):
            speaker.receipt(receipt_word(r))


def log_decision(row: dict):
    try:
        with open(DECISIONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def on_partial(text):
    broadcast("partial", text)
    if ctl is None or _no_arm():
        return
    try:
        _fired(ctl.on_partial(text))
    except Exception as e:                       # the fast path must never take the mic down
        print(f"  fast path: {e}", file=sys.stderr, flush=True)


def on_final(text, provisional=False):
    if ctl is None:
        if not provisional:
            handle_user_text(text)
        return
    if _no_arm():
        return
    if provisional:
        # the fast model's text at the moment of silence: closed slots fire now
        try:
            _fired(ctl.on_final(text, provisional=True)["fired"])
        except Exception as e:
            print(f"  fast path: {e}", file=sys.stderr, flush=True)
        # if the accurate pass never lands, the provisional text stands
        old = _accurate_timer["t"]
        if old:
            old.cancel()
        t = threading.Timer(ACCURATE_TIMEOUT_S, lambda: ctl.provisional == text and on_final(text, False))
        t.daemon = True
        t.start()
        _accurate_timer["t"] = t
        return
    old = _accurate_timer["t"]
    if old:
        old.cancel()
        _accurate_timer["t"] = None
    try:
        out = ctl.on_final(text)
    except Exception as e:
        print(f"  fast path: {e}", file=sys.stderr, flush=True)
        handle_user_text(text)
        return
    _fired(out["fired"])
    for r in out["held"].values():
        broadcast("action", r)
        if SPEAK and speaker is not None:
            speaker.receipt("which one?" if r.get("options") else "held")
    if out["to_brain"]:
        # the controller already called brain-bound text through to_brain;
        # handle_user_text is that callback, so nothing more to do here
        pass
    elif out["fired"] or out["done"]:
        broadcast("you", text)                   # heard, handled without Claude
        set_state("idle")
    else:
        set_state("idle")


def on_brain_event(kind, payload):
    if kind == "hands":
        broadcast("hands", payload)
        print(f"  brain: cli {payload.get('cli')} mode {payload.get('permission_mode')} "
              f"hands={'on' if payload.get('hands') else 'OFF'} servers={payload.get('servers')}"
              f"{' errors=' + str(payload['errors']) if payload.get('errors') else ''}", flush=True)
        return
    if kind == "text":
        broadcast("claude", payload)
        if SPEAK:
            speaker.say(payload)
    elif kind == "tool":
        broadcast("tool", payload)
    elif kind == "control":
        broadcast("control", payload)
        if payload.get("subtype") == "error":
            print(f"  brain control error: {payload.get('error')}", flush=True)
    elif kind == "done":
        broadcast("usage", payload)


@app.on_event("startup")
async def startup():
    global brain, speaker, ears, ctl
    loop_ref["loop"] = asyncio.get_running_loop()
    speaker = Speaker(set_state)
    want_hands = os.environ.get("VOICE_HANDS", "1") != "0"
    if want_hands and not hands.available():
        print(f"\nhands: windows-mcp not installed at {hands.EXE} -- the brain can talk but not click.\n"
              f"       python -m venv {hands.VENV} && {hands.VENV}\\Scripts\\python -m pip install windows-mcp")
    try:
        brain = Brain(on_brain_event, want_hands=want_hands)
    except RuntimeError as e:                    # Claude Code not installed
        brain = None
        print(f"\nbrain: off -- {e}. Instant commands still work.\n", flush=True)

    if os.environ.get("VOICE_FAST", "1") != "0":
        dry = os.environ.get("VOICE_DRY_RUN") == "1"
        jev = Jev(on_log=lambda m: print(f"  {m}", flush=True))
        # In-app control ("click the settings button"). Shares the windows-mcp
        # install with the brain; without it the fast path simply has no
        # click/type-into primitives and says so.
        desktop = None
        if os.environ.get("VOICE_CLICK", "1") != "0" and hands.available():
            from elements import Desktop
            desktop = Desktop(on_log=lambda m: print(f"  {m}", flush=True))
        actions = Actions(Apps(), dry_run=dry, on_log=lambda r: print(f"  action: {r}", flush=True),
                          desktop=desktop)
        ctl = Controller(actions, jev, to_brain=handle_user_text, on_decision=log_decision,
                         on_fire=_fired)
        if jev.mode == "real":
            jev.warm()
        print(f"\nfast path: on ({'DRY RUN, ' if dry else ''}jev={jev.mode}"
              f"{', ' + jev.base if jev.mode == 'real' else ''}; {len(actions.apps.catalog)} apps"
              f"; click={'on' if desktop else 'off'})")
    else:
        print("\nfast path: off (VOICE_FAST=0)")

    ears = StreamEars(on_partial, on_final, set_state, speaker,
                      device=pick_mic(os.environ.get("VOICE_DEVICE")),
                      on_level=lambda rms, gate: overlay_send("level", {"rms": rms, "gate": gate}),
                      model=os.environ.get("VOICE_MODEL", "small.en"),
                      hotwords=hotwords() if os.environ.get("VOICE_HOTWORDS", "1") != "0" else None,
                      fast_model=os.environ.get("VOICE_FAST_MODEL", "tiny.en"))
    ears.on_log = lambda m: (vlog("ears", m), print(f"  {m}", flush=True))
    ears.start()
    floor, peak, gate = ears.calibrate()
    name = sd.query_devices(ears.device if ears.device is not None else sd.default.device[0])["name"]
    print(f"\nmic: {name}\n  floor={floor:.6f} peak={peak:.6f} gate={gate:.4f}")
    if peak < 1e-4:
        print("  !! silent device -- set VOICE_DEVICE to part of your mic's name (e.g. intel)")
    start_overlay()
    start_hotkey()
    print(f"\n  console: http://127.0.0.1:{PORT}   (starts muted)\n", flush=True)


hotkey_proc = None
overlay_proc = None
OVERLAY_KINDS = {"state", "partial", "you", "action", "level", "claude"}
_overlay_q: "queue.Queue" = queue.Queue(maxsize=256)


def overlay_send(kind, payload):
    """Never blocks: the mic loop calls this ~16x/s. If the bar falls behind,
    level frames are dropped rather than stalling the ears."""
    if overlay_proc is None:
        return
    try:
        _overlay_q.put_nowait(json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False))
    except queue.Full:
        pass


def _overlay_pump():
    while overlay_proc is not None:
        line = _overlay_q.get()
        try:
            overlay_proc.stdin.write(line + "\n")
            overlay_proc.stdin.flush()
        except (OSError, ValueError):
            return                                   # bar closed; the voice agent carries on


def start_overlay():
    """overlay.py in its own process: Tk wants its own main thread, and a
    stall in the bar must never stall the mic."""
    global overlay_proc
    if os.environ.get("VOICE_OVERLAY", "1") == "0":
        return
    overlay_proc = subprocess.Popen(
        [sys.executable, str(HERE / "overlay.py")], stdin=subprocess.PIPE, text=True,
        encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    threading.Thread(target=_overlay_pump, daemon=True).start()
    overlay_send("state", {"status": STATE["status"], "muted": STATE["muted"]})


def start_hotkey():
    """hotkey.py in its own process -- see its docstring for why."""
    global hotkey_proc
    if os.environ.get("VOICE_HOTKEY", "1") == "0":
        return
    env = {**os.environ, "VOICE_TOKEN": HOTKEY_TOKEN}
    hotkey_proc = subprocess.Popen(
        [sys.executable, str(HERE / "hotkey.py"), "--port", str(PORT), "--parent", str(os.getpid())],
        env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    print("  hotkey: double-tap Alt = mic on (rising beep) / off (falling beep)", flush=True)


@app.on_event("shutdown")
async def shutdown():
    if hotkey_proc:
        hotkey_proc.terminate()
    if overlay_proc:
        overlay_proc.terminate()
    if brain:
        brain.stop()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
