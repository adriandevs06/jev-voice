"""apps.py - every launchable app on this Windows machine, by name.

`Get-StartApps` is the Start menu's own index: a Name -> AppID pair for every
installed app (Win32 shortcuts, Store apps, steam:// links, protocol handlers).
`Start-Process "shell:AppsFolder\\<AppID>"` launches any of them without a path
lookup, which is the one primitive a voice assistant needs for "open X".

The catalog is cached in .apps.json for a day (the PowerShell call takes ~1 s)
and a short alias table maps how people SAY an app to how Windows names it
("chrome" -> "Google Chrome", "notes" -> "Notepad"). Matching is aliases first,
then exact name, then a token overlap score -- deterministic, no model. When a
Jev key exists the intent layer additionally asks Jev to pick from the names,
which handles phrasings the aliases never saw.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
CACHE = HERE / ".apps.json"
CACHE_TTL_S = 24 * 3600
# Epic games have no Start-menu entry, so Get-StartApps never lists them
# ("open fortnite" went to the brain, 2026-09-24). The launcher's install list
# names them; its URI starts one the same way the Epic "Launch" button does.
EPIC_INSTALLED = Path(r"C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat")
URI_SCHEMES = ("http://", "https://", "steam://", "com.epicgames.launcher://")


def epic_games(path: Path = EPIC_INSTALLED) -> list:
    """[{name, id}] for installed Epic games; [] if Epic is not installed."""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8-sig")).get("InstallationList") or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for r in rows:
        app, where = r.get("AppName"), r.get("InstallLocation") or ""
        if not app or app.startswith("UE_"):               # engine installs are not games
            continue
        name = Path(where).name if where else app
        out.append({"name": name, "id": f"com.epicgames.launcher://apps/{app}?action=launch&silent=true"})
    return out

# Spoken -> Start menu name. Left side is lower-case, no punctuation.
ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "browser": "Google Chrome",
    "the browser": "Google Chrome", "google": "Google Chrome",
    "notes": "Notepad", "notepad": "Notepad", "a note": "Notepad", "text editor": "Notepad",
    "terminal": ["Windows Terminal", "Terminal"], "powershell": "Windows PowerShell", "cmd": "Command Prompt",
    "command prompt": "Command Prompt", "bash": "Git Bash", "git bash": "Git Bash",
    "explorer": "File Explorer", "file explorer": "File Explorer", "files": "File Explorer",
    "my files": "File Explorer", "folder": "File Explorer",
    "settings": "Settings", "calculator": "Calculator", "calc": "Calculator",
    "camera": "Camera", "photo booth": "Camera", "photobooth": "Camera", "webcam": "Camera",
    "claude": "Claude", "claude app": "Claude", "cursor": "Cursor", "devin": "Devin",
    "windsurf": "Devin", "vs code": "Visual Studio Code", "vscode": "Visual Studio Code",
    "code": "Visual Studio Code", "code editor": ["Cursor", "Visual Studio Code", "Devin"],
    "editor": ["Cursor", "Visual Studio Code"], "discord": "Discord",
    "telegram": ["Telegram", "Telegram Desktop"],
    # how faster-whisper actually hears these names (see WAKE_RE in listen.py)
    "clawed": "Claude", "cloud": "Claude", "claud": "Claude", "clod": "Claude", "klaus": "Claude",
    "devon": "Devin", "devan": "Devin", "kerser": "Cursor", "curser": "Cursor",
    "spotify": "Spotify", "excel": "Excel", "word": "Word", "powerpoint": "PowerPoint",
    "outlook": "Outlook", "capcut": "CapCut", "obs": "OBS Studio", "steam": "Steam",
    "task manager": "Task Manager", "control panel": "Control Panel",
    "snipping tool": "Snipping Tool", "screenshot tool": "Snipping Tool", "paint": "Paint",
    "edge": "Microsoft Edge", "firefox": "Firefox", "brave": "Brave",
}

# Apps nobody asks for by voice; kept out of the Jev choice list to save slots.
NOISE = re.compile(r"(help|documentation|release notes|faq|uninstall|readme|promotion|"
                   r"offers|energy star|diagnostics|privacy settings|system event|"
                   r"application verifier|debuggable|component services|defragment|"
                   r"disk cleanup|character map|developer (command prompt|powershell))", re.I)


# Words that may surround an app's name without changing which app is meant.
STOP = {"the", "my", "a", "an", "app", "application", "program", "browser", "window", "up",
        "please", "for", "me", "now", "again"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    return set(_norm(s).split())


class Apps:
    def __init__(self, catalog: Optional[list] = None, cache: Path = CACHE):
        self.cache = Path(cache)
        self.catalog = catalog if catalog is not None else self._load()
        if catalog is None:
            have = {a["name"].lower() for a in self.catalog}
            self.catalog += [g for g in epic_games() if g["name"].lower() not in have]
        self.by_name = {a["name"]: a["id"] for a in self.catalog}
        self.by_norm = {_norm(a["name"]): a["name"] for a in self.catalog}

    # -- catalog ---------------------------------------------------------------

    def _load(self) -> list:
        try:
            if self.cache.exists() and time.time() - self.cache.stat().st_mtime < CACHE_TTL_S:
                return json.loads(self.cache.read_text(encoding="utf-8"))
        except Exception:
            pass
        cat = self.scan()
        if cat:
            try:
                self.cache.write_text(json.dumps(cat, ensure_ascii=False, indent=0),
                                      encoding="utf-8")
            except OSError:
                pass
        return cat

    @staticmethod
    def scan() -> list:
        """Ask the Start menu. Returns [{name, id}] or [] on any failure."""
        if os.name != "nt":
            return []
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
            rows = json.loads(r.stdout or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            return [{"name": x["Name"], "id": x["AppID"]} for x in rows
                    if x.get("Name") and x.get("AppID")]
        except Exception:
            return []

    @staticmethod
    def _targets(v) -> list:
        return list(v) if isinstance(v, (list, tuple)) else [v]

    def _alias(self, alias: str) -> Optional[str]:
        for t in self._targets(ALIASES.get(alias, [])):
            if t in self.by_name:
                return t
        return None

    def names(self, limit: int = 200) -> list:
        """Voice-worthy app names, aliases' targets first, for a Jev choice list."""
        wanted = [n for n in dict.fromkeys(t for v in ALIASES.values() for t in self._targets(v))
                  if n in self.by_name]
        rest = sorted(n for n in self.by_name if n not in wanted and not NOISE.search(n))
        return (wanted + rest)[:limit]

    # -- matching --------------------------------------------------------------

    def resolve(self, spoken: str) -> Optional[str]:
        """Spoken phrase -> exact Start menu name, or None. Deterministic."""
        q = _norm(spoken)
        if not q:
            return None
        hit = self._alias(q)
        if hit:
            return hit
        for alias in sorted(ALIASES, key=len, reverse=True):   # "the chrome browser" -> chrome
            target = self._alias(alias)
            if target and re.search(rf"\b{re.escape(alias)}\b", q):
                rest = _tokens(re.sub(rf"\b{re.escape(alias)}\b", " ", q))
                if rest <= STOP:                        # nothing but "the ... app" around it
                    return target
        if q in self.by_norm:
            return self.by_norm[q]
        qt = _tokens(q)
        best, best_score = None, 0.0
        for norm, name in self.by_norm.items():
            nt = _tokens(norm)
            if not nt:
                continue
            overlap = len(qt & nt)
            if not overlap:
                continue
            if overlap < 2 and len(qt) > 1 and len(nt) > 1:
                continue        # "code editor" must not reach "Registry Editor" on one word
            # Both directions matter: "chrome" should reach "Google Chrome", but
            # "chrome and clear the history" must not -- most of what was said
            # has to be the app's name.
            score = overlap / max(len(nt), len(qt)) + (0.5 if norm.startswith(q) else 0)
            if score > best_score:
                best, best_score = name, score
        return best if best_score >= 0.5 else None

    # -- launching -------------------------------------------------------------

    def launch(self, name: str, dry_run: bool = False) -> dict:
        app_id = self.by_name.get(name)
        if not app_id:
            return {"ok": False, "error": f"unknown app {name!r}"}
        target = f"shell:AppsFolder\\{app_id}"
        if dry_run:
            return {"ok": True, "dry_run": True, "app": name, "target": target}
        try:
            if app_id.startswith(URI_SCHEMES):
                os.startfile(app_id)
            else:
                os.startfile(target)
            return {"ok": True, "app": name, "target": target}
        except OSError as e:
            return {"ok": False, "app": name, "error": str(e)}
