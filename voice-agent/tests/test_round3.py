"""Round 3: provisional finals, signature dedupe, no-arm zones, receipts,
stacks, the decision log, and the brain's hands -- all without audio, Claude,
a Jev key or Windows-MCP installed."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hands  # noqa: E402
import server  # noqa: E402
from actions import Actions, receipt_word  # noqa: E402
from apps import Apps  # noqa: E402
from intent import Controller  # noqa: E402
from jev import Jev  # noqa: E402
from stream_ears import FakeEars  # noqa: E402

CATALOG = [{"name": "Google Chrome", "id": "Chrome"}, {"name": "Notepad", "id": "np"},
           {"name": "Claude", "id": "cl"}, {"name": "Telegram", "id": "tg"}, {"name": "Cursor", "id": "cu"}]
STACKS = {"morning": [{"open_app": "Google Chrome"}, {"open_app": "Telegram"}, {"open_url": "x.com"}]}


def world(tmp_path, foreground=lambda: "Untitled - Notepad"):
    apps = Apps(catalog=CATALOG, cache=tmp_path / "apps.json")
    actions = Actions(apps, dry_run=True, foreground=foreground, stacks=STACKS)
    handed, rows = [], []
    ctl = Controller(actions, Jev(mode="replay", cache_dir=tmp_path / "jev"),
                     to_brain=handed.append, app_names=apps.names(), on_decision=rows.append)
    return apps, actions, ctl, handed, rows


# -- provisional finals ---------------------------------------------------------

def test_provisional_fires_closed_slots_then_accurate_fires_the_rest(tmp_path):
    _, actions, ctl, handed, rows = world(tmp_path)
    out = ctl.on_final("open chrome then search for norbert wiener", provisional=True)
    assert [f["action"] for f in out["fired"]] == ["open_app"] and out["provisional"]
    assert out["fired"][0]["why"] == "provisional"
    out2 = ctl.on_final("open chrome then search for norbert wiener")
    assert [f["action"] for f in out2["fired"]] == ["web_search"]
    assert out2["fired"][0]["query"] == "norbert wiener" and handed == []
    assert [r["action"] for r in actions.log] == ["open_app", "web_search"]   # each exactly once


def test_accurate_transcript_cannot_double_fire_the_same_app(tmp_path):
    _, actions, ctl, handed, rows = world(tmp_path)
    ctl.on_final("open clawed", provisional=True)                       # tiny.en mis-hearing
    assert actions.log[-1]["app"] == "Claude"
    out = ctl.on_final("open claude")                                   # small.en got it right
    assert out["fired"] == [] and handed == []
    assert [r["action"] for r in actions.log] == ["open_app"]


def test_accurate_pass_can_add_what_provisional_missed(tmp_path):
    _, actions, ctl, handed, rows = world(tmp_path)
    ctl.on_final("open chrome then open", provisional=True)             # cut off mid-word
    out = ctl.on_final("open chrome then open telegram")
    assert [f["app"] for f in out["fired"]] == ["Telegram"]
    assert [r["app"] for r in actions.log] == ["Google Chrome", "Telegram"]


def test_fake_ears_two_step_end_to_end(tmp_path):
    _, actions, ctl, handed, rows = world(tmp_path)
    FakeEars(ctl.on_partial, ctl.on_final, accurate="open chrome and reply to the last message").say(
        "open chrome and reply to the last massage")
    assert [r["action"] for r in actions.log] == ["open_app"]
    assert handed == ["reply to the last message"]                      # the accurate words go to the brain


# -- no-arm zones ------------------------------------------------------------------

def test_no_arm_zone_blocks_every_primitive(tmp_path):
    _, actions, ctl, handed, rows = world(tmp_path, foreground=lambda: "Tradovate - Trading")
    assert actions.blocked().startswith("Tradovate")
    out = ctl.on_final("open chrome then type hello")
    assert out["fired"] == [] and all(r["held"].startswith("no-arm") for r in out["held"].values())
    assert handed == ["open chrome", "type hello"] or handed == ["open chrome then type hello"]


def test_no_arm_titles_cover_the_dangerous_windows(tmp_path):
    for title in ("NinjaTrader 8", "1Password - vault", "Sign in - Google Accounts", "Two-factor authentication",
                  "MetaTrader 5", "TradingView - NQ1!"):
        _, actions, *_ = world(tmp_path, foreground=lambda t=title: t)
        assert actions.blocked(), title
    _, actions, *_ = world(tmp_path, foreground=lambda: "x.com - Google Chrome")
    assert actions.blocked() is None


# -- receipts ----------------------------------------------------------------------

def test_receipt_words():
    assert receipt_word({"action": "open_app", "app": "Google Chrome", "ok": True}) == "Google"
    assert receipt_word({"action": "web_search", "ok": True}) == "searching"
    assert receipt_word({"action": "type_text", "held": "dangerous text"}) == "held"
    assert receipt_word({"action": "stack", "name": "morning", "ok": True}) == "morning"


# -- stacks ------------------------------------------------------------------------

def test_stack_runs_its_chain(tmp_path):
    _, actions, ctl, handed, rows = world(tmp_path)
    out = ctl.on_final("run my morning stack")
    r = out["fired"][0]
    assert r["action"] == "stack" and r["ok"] and [s["action"] for s in r["steps"]] == ["open_app", "open_app", "open_url"]
    assert r["steps"][2]["url"] == "https://x.com" and handed == []
    out = ctl.on_final("run my evening stack")                          # unknown -> brain, not a guess
    assert out["fired"] == [] and handed == ["run my evening stack"]


def test_stack_cannot_nest_or_run_shell(tmp_path):
    apps = Apps(catalog=CATALOG, cache=tmp_path / "a.json")
    actions = Actions(apps, dry_run=True, foreground=lambda: "", stacks={"bad": [{"stack": "morning"}, {"open_app": "Notepad"}]})
    r = actions.stack("bad")
    assert r["ok"] is False and r["steps"][0]["error"] == "not allowed in a stack" and r["steps"][1]["ok"]


# -- decision log ---------------------------------------------------------------

def test_decision_rows_are_written_for_fires_holds_and_brain(tmp_path):
    _, actions, ctl, handed, rows = world(tmp_path)
    ctl.on_final("open chrome then press alt f4 then rename the files")
    kinds = [(r["intent"], r["fired"]) for r in rows]
    assert ("open_app", True) in kinds and ("press_keys", False) in kinds and ("brain", False) in kinds
    assert all({"t", "utt", "seq", "text", "thresholds"} <= set(r) for r in rows)
    json.dumps(rows)                                                    # serialisable


# -- hands ------------------------------------------------------------------------

needs_hands = pytest.mark.skipif(not __import__('hands').available(),
                                 reason='windows-mcp not installed (setup.cmd installs it)')


@needs_hands
def test_brain_argv_with_and_without_hands(monkeypatch):
    monkeypatch.setattr(hands, "available", lambda: True)
    argv = server.Brain.argv(["claude.exe"], (2, 1, 275), want_hands=True)
    assert "--strict-mcp-config" in argv and "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(cfg["mcpServers"]) == {"windows-mcp"}
    assert cfg["mcpServers"]["windows-mcp"]["env"]["WINDOWS_MCP_EXCLUDE_TOOLS"].startswith("PowerShell")
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "mcp__windows-mcp" in allowed and not any(a.startswith("Bash") for a in allowed)
    assert "Snapshot before acting" in argv[argv.index("--append-system-prompt") + 1]

    old = server.Brain.argv(["claude.exe"], (2, 1, 220), want_hands=True)
    assert "--permission-prompts" not in old and "--mcp-config" in old      # old CLI: no fast-deny flag
    off = server.Brain.argv(["claude.exe"], (2, 1, 275), want_hands=False)
    assert "--mcp-config" not in off and "--permission-mode" in off         # explicit mode regardless


def test_brain_argv_without_windows_mcp_installed(monkeypatch):
    monkeypatch.setattr(hands, "available", lambda: False)
    argv = server.Brain.argv(["claude.exe"], (2, 1, 275), want_hands=True)
    assert "--mcp-config" not in argv and "Snapshot" not in argv[argv.index("--append-system-prompt") + 1]


def test_hands_from_init_event():
    ev = {"type": "system", "subtype": "init", "session_id": "s1", "permissionMode": "acceptEdits",
          "mcp_servers": [{"name": "windows-mcp", "status": "connected"}],
          "tools": ["Read", "mcp__windows-mcp__Snapshot", "mcp__windows-mcp__Click"]}
    h = hands.hands_from_init(ev)
    assert h["hands"] and h["tool_count"] == 2 and h["permission_mode"] == "acceptEdits"
    bad = hands.hands_from_init({"type": "system", "subtype": "init",
                                 "mcp_servers": [{"name": "windows-mcp", "status": "failed"}],
                                 "mcp_server_errors": {"windows-mcp": "spawn ENOENT"}})
    assert not bad["hands"] and bad["errors"]
    assert hands.hands_from_init({"type": "assistant"}) is None


def test_brain_handle_line_reports_hands_and_denials():
    events = []
    b = server.Brain.__new__(server.Brain)
    b.on_event = lambda k, p: events.append((k, p))
    b.handle_line(json.dumps({"type": "system", "subtype": "init", "session_id": "x",
                              "mcp_servers": [{"name": "windows-mcp", "status": "connected"}]}))
    b.handle_line(json.dumps({"type": "result", "usage": {}, "total_cost_usd": 0,
                              "permission_denials": [{"tool_name": "Bash"}]}))
    b.handle_line("not json")
    assert events[0][0] == "hands" and events[0][1]["hands"] and b.session_id == "x"
    assert events[1][0] == "done" and events[1][1]["permission_denials"] == 1


def test_version_parsing():
    assert hands.parse_version("2.1.275 (Claude Code)") == (2, 1, 275)
    assert hands.parse_version("") == (0, 0, 0)


def test_brain_env_strips_host_session_and_sets_mcp_timeouts():
    env = hands.brain_env({"PATH": "x", "CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
                           "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH": "1", "MCP_TIMEOUT": "5000"})
    assert env["PATH"] == "x"
    assert not any(k in env for k in ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN",
                                      "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH"))
    assert env["MCP_TIMEOUT"] == "5000"                      # an explicit choice is kept
    assert int(hands.brain_env({})["MCP_TIMEOUT"]) >= 120_000  # windows-mcp took 81 s under load


def test_auth_status_never_raises(monkeypatch):
    import subprocess as sp
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(hands, "_user_env", lambda name: None)     # no login in the registry either

    class R:
        stdout = '{"loggedIn": false, "authMethod": "none"}'
    monkeypatch.setattr(sp, "run", lambda *a, **k: R())
    assert hands.auth_status(["claude.exe"])["loggedIn"] is False

    def boom(*a, **k):
        raise FileNotFoundError("no binary")
    monkeypatch.setattr(sp, "run", boom)
    r = hands.auth_status(["claude.exe"])
    assert r["loggedIn"] is None and "FileNotFoundError" in r["error"]


def test_env_token_counts_as_signed_in(monkeypatch):
    """`claude auth status` says logged out when the login is an env token --
    the brain worked for a whole session while this reported False."""
    import subprocess as sp
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setattr(sp, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not needed")))
    assert hands.auth_status(["claude.exe"])["loggedIn"] is True


def test_brain_is_fast_and_silent(monkeypatch):
    import server
    argv = server.Brain.argv(["claude.exe"], (2, 1, 275), want_hands=True)
    assert argv[argv.index("--model") + 1] == server.BRAIN_MODEL
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "never spoken" in prompt and "chat or agent window" in prompt
    assert hands.brain_env({})["ENABLE_TOOL_SEARCH"] == "false"
    assert server.SPEAK is False or __import__("os").environ.get("VOICE_SPEAK") == "1"


@needs_hands
def test_brain_can_research_and_save_finds(monkeypatch):
    """'look for new stuff for me' -- web tools, and a folder he will look in."""
    import server
    argv = server.Brain.argv(["claude.exe"], (2, 1, 275), want_hands=True)
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "WebSearch" in allowed and "WebFetch" in allowed
    assert argv[argv.index("--add-dir") + 1] == str(hands.FINDS_DIR)
    assert str(hands.FINDS_DIR) in argv[argv.index("--append-system-prompt") + 1]
    # still nothing that writes, sends or runs beyond the finds folder
    assert not {"Bash", "Write", "Edit", "PowerShell"} & set(allowed)


def test_brain_env_fills_the_login_from_the_registry(monkeypatch):
    """Started from a shell that predates `claude setup-token` (or from inside
    the desktop app), the brain had no login and answered 'OAuth session
    expired'. brain_env reads the user-scope var itself."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(hands, "_user_env", lambda name: "tok" if name == "CLAUDE_CODE_OAUTH_TOKEN" else None)
    assert hands.brain_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
    assert hands.brain_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "from-env"     # the inherited one wins
