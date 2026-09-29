"""jev.py - one Jev client for the voice console.

Jev (TypeSafe AI, model `jev-1.13.0`, alias `jev-latest`) is a decision model,
not a writer: you hand it a `state` (string, object or array) and a dict of
typed questions, and it answers every question in one round trip --
`noul` -> probability of yes, `choice` -> one option + per-option probabilities
+ confidence, `score` -> a rung on a 2..10 ladder you define. It generates no
text. The reason it belongs in a voice loop is latency: 70-500 ms per call, so
it can be asked about a HALF-SPOKEN sentence on every partial transcript and
the answer lands before the sentence ends. (docs.typesafe.ai/api, 2026-09-21)

Two routes speak the same wire format, so the key decides the route:

  TYPESAFE_API_KEY    -> https://api.typesafe.ai/v1/systemone, model jev-latest
                         (open signup at console.typesafe.ai since 2026-09-20;
                         $0.042 per million input tokens, output free)
  AI_GATEWAY_API_KEY  -> https://ai-gateway.vercel.sh/typesafe/v1/systemone,
     or JEV_API_KEY      model typesafe-ai/jev (Vercel AI Gateway, free until
                         2026-09-25, then list price with no markup)

Keys are read from the process env and then the User/Machine registry, because
a key set with setx after this shell started is otherwise invisible.

Three modes, chosen once at construction:

  real    -> HTTPS. Every real answer is also written to the replay cache.
  replay  -> answers only from the cache (keyed by a hash of state+questions);
             a miss returns None. The tests run here: nothing is billed and
             nothing touches the network.
  mock    -> the caller wires a rule-based stand-in; the client reports misses.

Never raises. Any failure returns None and the caller falls back to rules; the
key is never logged. Design rules from the docs that shape the questions in
intent.py: one atomic judgement per question, facts in state and rules in
criteria, an explicit "none" option in every choice, arithmetic/dates in code,
and phrase every noul so that "yes" is the positive case.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
CACHE_DIR = HERE / ".jev_cache"
DIRECT_BASE = "https://api.typesafe.ai"
GATEWAY_BASE = "https://ai-gateway.vercel.sh/typesafe"


def _env(name: str) -> str:
    v = os.environ.get(name)
    if v:
        return v.strip()
    try:
        import winreg
    except ImportError:
        return ""
    for root, path in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                       (winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, path) as k:
                val, _ = winreg.QueryValueEx(k, name)
                if val:
                    return str(val).strip()
        except OSError:
            pass
    return ""


def credentials() -> tuple[str, str, str]:
    """(key, base_url, model) for whichever key exists; ('', '', '') if none."""
    k = _env("TYPESAFE_API_KEY")
    if k:
        return k, _env("TYPESAFE_BASE_URL") or DIRECT_BASE, _env("TYPESAFE_DEFAULT_MODEL") or "jev-latest"
    k = _env("AI_GATEWAY_API_KEY") or _env("JEV_API_KEY")
    if k:
        return k, _env("JEV_BASE_URL") or GATEWAY_BASE, "typesafe-ai/jev"
    return "", "", ""


def api_key() -> str:
    return credentials()[0]


def _key_for(state, questions) -> str:
    raw = json.dumps({"s": state, "q": questions}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class Jev:
    def __init__(self, mode: Optional[str] = None, timeout: float = 1.5,
                 cache_dir: Path = CACHE_DIR, on_log=None, retries: int = 0):
        """mode: 'real' | 'replay' | 'mock' | None (auto: real if a key exists, else replay)."""
        self.key, self.base, self.model = credentials()
        if mode is None:
            mode = os.environ.get("JEV_MODE") or ("real" if self.key else "replay")
        if mode == "real" and not self.key:
            mode = "replay"
        self.mode = mode
        self.timeout = timeout
        self.cache_dir = Path(cache_dir)
        self.on_log = on_log or (lambda *_: None)
        # 0 for the live voice loop: a throttled answer should fall back to the
        # rules at once, not make the speaker wait. Batch jobs (calibration)
        # pass a few, because the gateway throttles bursts without saying so.
        self.retries = retries
        self.stats = {"calls": 0, "hits": 0, "misses": 0, "errors": 0,
                      "input_tokens": 0, "latency_ms": [], "status": {}}
        self._lock = threading.Lock()
        self._session = None

    # -- public ----------------------------------------------------------------

    def evaluate(self, state, questions: dict) -> Optional[dict]:
        """Returns the `answers` dict ({id: {type, noul|choice|score, ...}}) or
        None. Replay hits are served from disk; real answers are cached."""
        if not questions:
            return None
        self.stats["calls"] += 1
        key = _key_for(state, questions)
        cached = self._read(key)
        if cached is not None:
            self.stats["hits"] += 1
            return cached
        if self.mode != "real":
            self.stats["misses"] += 1
            return None
        answers = self._call(state, questions)
        if answers is not None:
            self._write(key, state, questions, answers)
        return answers

    def warm(self) -> bool:
        """First call pays TLS setup (~700 ms measured by others); do it early."""
        if self.mode != "real":
            return False
        try:
            # Through the keep-alive session, or the warm-up pays for a TLS
            # handshake the first real decision then pays for again (measured:
            # 2.7 s first call after a warm-up on a separate connection).
            r = self.session().get(f"{self.base}/v1/models", timeout=self.timeout * 2)
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def noul(answers: Optional[dict], key: str, default: float = 0.0) -> float:
        a = (answers or {}).get(key) or {}
        p = a.get("noul", a.get("probability"))
        return float(p) if isinstance(p, (int, float)) else default

    @staticmethod
    def choice(answers: Optional[dict], key: str) -> tuple[Optional[str], float, float]:
        """(chosen option, its probability, confidence) or (None, 0, 0)."""
        a = (answers or {}).get(key) or {}
        c = a.get("choice")
        probs = a.get("probabilities") or {}
        p = 0.0
        if isinstance(probs, dict) and c in probs:
            try:
                p = float(probs[c])
            except (TypeError, ValueError):
                p = 0.0
        conf = a.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else p
        return (c if isinstance(c, str) else None), p, conf

    @property
    def cost_usd(self) -> float:
        return self.stats["input_tokens"] * 0.042 / 1_000_000

    # -- wire ------------------------------------------------------------------

    def session(self):
        """One keep-alive connection: re-negotiating TLS per partial would cost
        more than the decision itself."""
        if self._session is None:
            import requests
            s = requests.Session()
            s.headers.update({"Authorization": f"Bearer {self.key}",
                              "Content-Type": "application/json"})
            self._session = s
        return self._session

    def _call(self, state, questions) -> Optional[dict]:
        try:
            body = {"model": self.model, "state": state, "questions": questions}
            t0 = time.monotonic()
            for attempt in range(self.retries + 1):
                r = self.session().post(f"{self.base}/v1/systemone", json=body, timeout=self.timeout)
                if r.status_code not in (429, 529) or attempt == self.retries:
                    break
                try:
                    wait = float(r.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0.0
                time.sleep(wait or 0.5 * (2 ** attempt))
            ms = int((time.monotonic() - t0) * 1000)
            self.stats["latency_ms"].append(ms)
            st = self.stats["status"]
            st[r.status_code] = st.get(r.status_code, 0) + 1
            if r.status_code != 200:
                self.stats["errors"] += 1
                self.on_log(f"jev HTTP {r.status_code}: {(r.text or '')[:120]}")
                return None
            data = r.json()
            answers = data.get("answers")
            if not isinstance(answers, dict):
                self.stats["errors"] += 1
                return None
            usage = data.get("usage") or {}
            try:
                self.stats["input_tokens"] += int(usage.get("input_tokens") or 0)
            except (TypeError, ValueError):
                pass
            self.on_log(f"jev {ms} ms, {usage.get('input_tokens', '?')} tok")
            return answers
        except Exception as e:  # network, timeout, JSON -- all the same to the loop
            self.stats["errors"] += 1
            self.on_log(f"jev error: {type(e).__name__}")
            return None

    # -- cache -----------------------------------------------------------------

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def cached(self, state, questions) -> Optional[dict]:
        """Answers already on disk for this exact state+questions, or None.
        Never calls out; used by JevWorker to answer synchronously."""
        return self._read(_key_for(state, questions))

    def _read(self, key: str) -> Optional[dict]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))["answers"]
        except Exception:
            return None

    def _write(self, key: str, state, questions, answers) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._path(key).write_text(json.dumps(
                    {"state": state, "questions": questions, "answers": answers},
                    ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def seed(self, state, questions, answers) -> str:
        """Tests and fixtures: plant an answer in the replay cache."""
        key = _key_for(state, questions)
        self._write(key, state, questions, answers)
        return key


class JevWorker:
    """Ask Jev about partial transcripts without blocking the ears.

    A partial arrives every ~0.45 s and a decision costs 300-450 ms, so calling
    `evaluate` inline would stall the decode loop and answer questions about
    text the speaker has already moved past. This worker does what every real
    Jev voice build does (moritzkremb/jev-voice-browser constants.js):

      * DEBOUNCE 200 ms  - a burst of partials submits once, for the newest text
      * MAX_INFLIGHT 2   - never more than two open requests
      * late answers are DELIVERED, flagged by seq -- at ~3 words/s and
        ~400 ms per decision, nearly every answer describes a prefix of what is
        being said by the time it lands, and "the command was complete at
        'calculator'" is still true when the speaker has added "for me". The
        controller decides whether a late answer still applies.
      * cache first       - a replay hit answers synchronously, no thread at all

    `submit` returns immediately: either the answers (cache hit) or None, and
    the callback fires later with (seq, state, answers) when a live answer lands.
    """
    DEBOUNCE_S = 0.2
    MAX_INFLIGHT = 2

    def __init__(self, jev: "Jev", on_answers=None, debounce_s: float = DEBOUNCE_S,
                 max_inflight: int = MAX_INFLIGHT, clock=time.monotonic):
        self.jev = jev
        self.on_answers = on_answers or (lambda seq, state, answers: None)
        self.debounce_s = debounce_s
        self.max_inflight = max_inflight
        self.clock = clock
        self.seq = 0
        self.newest = 0                 # highest seq submitted
        self.inflight = 0
        self.dropped = {"stale": 0, "debounced": 0, "full": 0}   # "stale" = late, still delivered
        self._pending = None            # (seq, state, questions, at)
        self._lock = threading.Lock()
        self._timer = None

    def submit(self, state, questions: dict) -> Optional[dict]:
        """Cache hit -> answers now. Otherwise schedule a call and return None."""
        hit = self.jev.evaluate(state, questions) if self.jev.mode != "real" else None
        if hit is not None:
            return hit
        if self.jev.mode == "real":
            hit = self.jev.cached(state, questions)
            if hit is not None:
                return hit
        with self._lock:
            self.seq += 1
            seq = self.newest = self.seq
            if self._pending is not None:
                self.dropped["debounced"] += 1
            self._pending = (seq, state, questions, self.clock())
            if self._timer is None:
                self._timer = threading.Timer(self.debounce_s, self._flush)
                self._timer.daemon = True
                self._timer.start()
        return None

    def _flush(self):
        with self._lock:
            self._timer = None
            item = self._pending
            self._pending = None
            if item is None:
                return
            if self.inflight >= self.max_inflight:
                # Keep it: a finishing request flushes it (see _run). Dropping
                # it lost exactly the partial that mattered -- "bring up
                # spotify" was never asked while two earlier words were in flight.
                self.dropped["full"] += 1
                if self._pending is None:
                    self._pending = item
                return
            self.inflight += 1
        t = threading.Thread(target=self._run, args=(item,), daemon=True)
        t.start()

    def _run(self, item):
        seq, state, questions, _at = item
        try:
            answers = self.jev.evaluate(state, questions)
        except Exception:
            answers = None
        finally:
            with self._lock:
                self.inflight -= 1
                stale = seq < self.newest
                if stale:
                    self.dropped["stale"] += 1
                pending = self._pending is not None and self._timer is None
        if answers is not None:
            try:
                self.on_answers(seq, state, answers)
            except Exception:
                pass
        if pending:
            self._flush()               # a submit arrived while we were busy

    def drain(self, timeout: float = 3.0) -> bool:
        """Tests: run any pending call now and wait for the flight to land."""
        with self._lock:
            t, self._timer = self._timer, None
        if t:
            t.cancel()
            self._flush()
        end = self.clock() + timeout
        while self.clock() < end:
            with self._lock:
                if self.inflight == 0 and self._pending is None:
                    return True
            time.sleep(0.01)
        return False
