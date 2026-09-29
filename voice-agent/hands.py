"""hands.py - give the headless Claude brain something to click with.

Why this file exists (verified 2026-09-21 against code.claude.com/docs):
Claude Code's built-in computer use "requires an interactive session, so it is
not available in non-interactive mode with the -p flag", and in the CLI it is
macOS only. server.py's brain is `claude -p` on Windows, launched with
`--strict-mcp-config` and no `--mcp-config` and no permission mode, which the
headless docs define as Manual mode where every prompt-requiring tool call is
denied. So "leftovers go to a brain that can click" was false: the brain could
talk, not act.

The fix is not computer use. It is an MCP server that drives Windows through
UI Automation -- CursorTouch/Windows-MCP (MIT, 7k stars, 15 tools after the
dangerous ones are excluded: Snapshot / Click / Type / Shortcut / App /
Scroll / WaitFor ...) -- passed to the brain inline with `--mcp-config`,
allow-listed with `--allowedTools mcp__windows-mcp`, and an explicit
`--permission-mode acceptEdits` so allow-listed tools run without a prompt
while everything else is still denied. Windows-MCP runs as the user with no
sandbox, so PowerShell / Registry / FileSystem / Process / Scrape are excluded
by env, Bash is never allow-listed for the voice brain, and the brain's cwd is
a scratch folder so acceptEdits cannot write into a real project.

Install once (no admin), from the voice-agent folder:
    python -m venv .venv-wm
    .venv-wm\\Scripts\\python -m pip install windows-mcp
(Not under %LOCALAPPDATA%: see the note at VENV below.)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
# Inside the repo on purpose: this laptop runs Claude as a Store (MSIX) app, and
# anything a child of it writes under %LOCALAPPDATA% is silently redirected to
# Packages\Claude_...\LocalCache -- invisible from a normal terminal.
VENV = Path(os.environ.get("WINDOWS_MCP_VENV") or (HERE / ".venv-wm"))
EXE = VENV / "Scripts" / "windows-mcp.exe"
BRAIN_CWD = HERE / ".brain_cwd"          # gitignored scratch: acceptEdits writes land here, nowhere else

SERVER_ENV = {
    "ANONYMIZED_TELEMETRY": "false",
    "POSTHOG_API_KEY": "",
    "WINDOWS_MCP_SCREENSHOT_SCALE": "0.5",
    "WINDOWS_MCP_DISABLE_FLASH": "1",
    "WINDOWS_MCP_EXCLUDE_TOOLS": "PowerShell,Registry,FileSystem,Process,Scrape",
}

# The brain may run these without a prompt. Nothing that writes outside its
# scratch cwd, nothing that runs a shell.
# WebSearch/WebFetch: "look for new stuff for me" is research, and doing it by
# driving Chrome through screenshots is slow and lossy. Both are read-only.
ALLOWED_TOOLS = ["mcp__windows-mcp", "Read", "Grep", "Glob", "WebSearch", "WebFetch"]
# Where research lands, one markdown file per ask. Outside the gitignored
# scratch dir so it is somewhere he will actually look; added with --add-dir
# so acceptEdits may write there (and still nowhere else).
FINDS_DIR = HERE.parent / "voice-finds"

# 2026-09-24, first real session: 2 minutes for "theater mode and volume to
# 20%" (a screenshot after every click), and it typed "google.com" into an
# agent app's chat box, then clicked around it to "clean up". Hence: speed rules,
# and a hard line around chat/agent windows.
SYSTEM_HANDS = (
    " You operate this Windows desktop through the windows-mcp tools (Snapshot, App, Click, "
    "Type, Shortcut, Scroll, Move, WaitFor). Be fast: the fewest tool calls that do the job. "
    "Prefer a keyboard Shortcut over clicking (YouTube: k pause, t theater, f fullscreen, "
    "m mute, shift+n next, / search). Read the screen with Snapshot use_vision=false first -- "
    "the UI tree has names and coordinates; use vision only when the tree cannot show what "
    "you need. Take a Snapshot before acting, but do not take another just to confirm an "
    "action worked unless the next step depends on it. "
    "Act only in the window the request is about: the one in front, or one the user names. "
    "Never type into, click in, or clear anything in a chat or agent window -- Claude, Devin, "
    "Cursor, Windsurf, VS Code, Discord, Telegram, Slack, WhatsApp, Messages, any email "
    "client -- unless the user names that app in this request. If a Type would land "
    "anywhere other than the field you meant, stop. "
    "Before anything that would send, post, publish, pay, buy, delete, or close unsaved "
    "work, ask and wait for a yes. Never act inside a trading platform, a password "
    "manager, or a sign-in or two-factor dialog."
)


# Claude Code marks an MCP server "failed" if it has not answered `initialize`
# within its startup timeout (~30 s by default). windows-mcp imports comtypes,
# pywin32 and dxcam and was measured at 81 s to first response on this laptop
# with 24 MB of RAM free -- so under load the brain came up with no hands at
# all. Give it room; an idle machine answers in a few seconds either way.
MCP_STARTUP_MS = 150_000
MCP_TOOL_MS = 180_000          # a Snapshot has been measured at 44 s under load


def available() -> bool:
    return EXE.exists()


# Variables a Claude desktop session injects into its children. A brain that
# inherits them believes it is hosted and tries to refresh auth over a socket
# that is not its own; stripped, it uses its own `claude auth login` like it
# would from a normal terminal.
HOST_VARS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
             "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ID",
             "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_OAUTH_SCOPES",
             "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_PID",
             "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_DESKTOP_APP_VERSION")


def _user_env(name: str) -> Optional[str]:
    """HKCU\\Environment, never printed."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0] or None
    except (ImportError, OSError):
        return None


def brain_env(base: Optional[dict] = None) -> dict:
    """Environment for the brain process: the caller's minus any host-session
    variables, plus MCP timeouts long enough that a slow windows-mcp start is
    not recorded as a failure."""
    env = dict(os.environ if base is None else base)
    for k in HOST_VARS:
        env.pop(k, None)
    # The login is a user-scope env var (`claude setup-token`). A process
    # started from a shell that predates it, or from inside the desktop app,
    # does not inherit it, and the brain then answers every request with
    # "OAuth session expired". Read it from the registry, like the other keys.
    if base is None and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        tok = _user_env("CLAUDE_CODE_OAUTH_TOKEN")
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    env.setdefault("MCP_TIMEOUT", str(MCP_STARTUP_MS))
    env.setdefault("MCP_TOOL_TIMEOUT", str(MCP_TOOL_MS))
    # Load the windows-mcp tools up front. With tool search on, every request
    # began with a ToolSearch round trip just to find Snapshot and Click.
    env.setdefault("ENABLE_TOOL_SEARCH", "false")
    return env


def auth_status(launcher: list) -> dict:
    """`claude auth status` as a dict. Costs nothing: no model call.

    Why it matters: the Claude desktop app authenticates the sessions it hosts
    itself, so a CLI binary launched on its own -- which is what the voice
    brain is -- has NO login unless `claude auth login` was run in a normal
    terminal. Without this check the brain starts, waits ~40 s, and answers
    every request with "OAuth session expired".

    A token in the environment (CLAUDE_CODE_OAUTH_TOKEN, from `claude
    setup-token`) signs the brain in too, and `auth status` does not report
    it -- 2026-09-24 the brain worked all session while this said logged out."""
    env = brain_env()
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if env.get(var):
            return {"loggedIn": True, "authMethod": f"env:{var}"}
    try:
        out = subprocess.run([*launcher, "auth", "status"], capture_output=True, text=True,
                             timeout=60, env=brain_env()).stdout
        return json.loads(out)
    except Exception as e:
        return {"loggedIn": None, "error": f"{type(e).__name__}: {e}"}


def mcp_config() -> dict:
    return {"mcpServers": {"windows-mcp": {"command": str(EXE), "args": ["serve"],
                                           "env": dict(SERVER_ENV)}}}


def parse_version(s: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", s or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def cli_version(exe: list) -> tuple:
    try:
        out = subprocess.run([*exe, "--version"], capture_output=True, text=True, timeout=20).stdout
        return parse_version(out)
    except Exception:
        return (0, 0, 0)


def brain_args(version: tuple, enabled: bool = True) -> list:
    """Extra argv for the brain. With hands: inline MCP config + allow-list +
    an explicit permission mode. Without: still an explicit mode so a denial
    is a visible event rather than a silent wait."""
    args = ["--permission-mode", "acceptEdits", "--add-dir", str(FINDS_DIR)]
    if version >= (2, 1, 259):
        args += ["--permission-prompts", "none"]      # deny fast instead of hanging (v2.1.259+)
    if enabled and available():
        args += ["--mcp-config", json.dumps(mcp_config()),
                 "--allowedTools", ",".join(ALLOWED_TOOLS)]
    return args


def hands_from_init(event: dict) -> Optional[dict]:
    """Read the stream-json `system/init` event: are the hands connected?"""
    if event.get("type") != "system" or event.get("subtype") != "init":
        return None
    servers = event.get("mcp_servers") or []
    ok = any(s.get("name") == "windows-mcp" and s.get("status") == "connected" for s in servers)
    errors = event.get("mcp_server_errors") or {}
    tools = [t for t in (event.get("tools") or []) if str(t).startswith("mcp__windows-mcp")]
    return {"hands": ok, "servers": [(s.get("name"), s.get("status")) for s in servers],
            "errors": errors, "tool_count": len(tools),
            "permission_mode": event.get("permissionMode") or event.get("permission_mode"),
            "cli": event.get("claude_code_version") or event.get("version")}


def probe(timeout: float = 25.0) -> dict:
    """Talk MCP to the server directly (no Claude, no tokens): initialize,
    tools/list. Proves the venv works on this machine."""
    if not available():
        return {"ok": False, "error": f"not installed: {EXE}"}
    env = {**os.environ, **SERVER_ENV}
    try:
        p = subprocess.Popen([str(EXE), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8")

        def send(o):
            p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "voice-agent", "version": "0"}}})
        init = json.loads(p.stdout.readline())
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = [t["name"] for t in json.loads(p.stdout.readline())["result"]["tools"]]
        p.terminate()
        return {"ok": True, "server": init.get("result", {}).get("serverInfo"), "tools": tools}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    print(json.dumps({"exe": str(EXE), "available": available(), "probe": probe()}, indent=1))
