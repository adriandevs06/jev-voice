"""smoke_hands.py - prove the brain can actually see the desktop.

Launches the same CLI binary with the same hands flags server.py uses, as a
one-shot `-p` run, and asks for one read-only action: take a Snapshot and name
the focused window. Nothing is clicked or typed. Draws from the Claude
subscription the CLI is logged into, not an API key.

    python smoke_hands.py

Prints: whether windows-mcp connected (from the init event), which tools the
model called, any permission denials, the answer, and the wall time.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import hands
from server import Brain

PROMPT = ("Use the windows-mcp Snapshot tool exactly once with use_vision set to false, then reply "
          "with only the name of the focused window. Do not click, type, or open anything.")


def main() -> int:
    if not hands.available():
        print(f"windows-mcp not installed at {hands.EXE}")
        return 2
    launcher = Brain.launcher()
    version = hands.cli_version(launcher)
    argv = [*launcher, "-p", PROMPT, "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--settings", json.dumps({"disableAllHooks": True}),
            *hands.brain_args(version, enabled=True)]
    hands.BRAIN_CWD.mkdir(exist_ok=True)
    print(f"cli {'.'.join(map(str, version))}: {launcher[0]}")
    auth = hands.auth_status(launcher)
    print(f"logged in       : {auth.get('loggedIn')}  ({auth.get('authMethod', auth.get('error', '?'))})")
    if auth.get("loggedIn") is False:
        print("The CLI has no login of its own (the desktop app only authenticates the sessions it hosts).")
        print("Run once in a normal terminal:  claude auth login")
        print("SMOKE BLOCKED")
        return 3
    t0 = time.monotonic()
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       cwd=str(hands.BRAIN_CWD), timeout=420, env=hands.brain_env())
    dt = time.monotonic() - t0

    init, tools, result = None, [], None
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "system" and e.get("subtype") == "init":
            init = hands.hands_from_init(e)
        elif e.get("type") == "assistant":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    tools.append(c.get("name"))
        elif e.get("type") == "result":
            result = e

    print(f"hands connected : {init and init['hands']}  servers={init and init['servers']}")
    print(f"permission mode : {init and init['permission_mode']}")
    print(f"windows tools   : {init and init['tool_count']}")
    print(f"tools called    : {tools}")
    if result:
        print(f"denials         : {len(result.get('permission_denials') or [])}")
        print(f"turns           : {result.get('num_turns')}")
        print(f"answer          : {(result.get('result') or '').strip()[:300]}")
    else:
        print(f"no result event; exit {p.returncode}; stderr tail: {p.stderr[-400:]}")
    print(f"wall time       : {dt:.1f}s")
    ok = bool(init and init["hands"] and any(t.startswith("mcp__windows-mcp") for t in tools)
              and result and not result.get("is_error"))
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
