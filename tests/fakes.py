"""Fake backend binaries and a minimal MCP client, shared by the pytest suite
and the standalone smoke test.

Each fake records its argv, one call per line, to the file named by FAKE_LOG,
so a test can assert on what actually reached the binary instead of what the
server claims. The canned outputs carry marker strings (FAKE-...) that appear
nowhere else in the repo, so a match can only come from the fake.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "bin" / "herdr_floor_mcp.py"

FAKE_HERDR = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "workspace list") echo '{"id":"x","result":{"workspaces":[{"workspace_id":"w1","label":"FAKE-floor-label","agent_status":"working"}]}}' ;;
  "agent list") echo '{"id":"x","result":{"agents":[{"name":"conductor-one","agent_status":"working","pane_id":"w1:p1","tokens":{"repo":"demo","wt":"main"}}]}}' ;;
  "agent read") echo '{"id":"x","result":{"text":"FAKE-herdr-screen-4d1c"}}' ;;
  "agent prompt") echo '{"id":"x","result":{"submitted":true,"target":"'"$3"'"}}' ;;
  "notification show") echo '{"id":"x","result":{"shown":true}}' ;;
  *) echo 'fake herdr: unknown subcommand' >&2; exit 1 ;;
esac
"""

FAKE_HC = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  ls) echo '{"hidden_finished":0,"terminals":[{"address":"t7","name":"reviewer-one","agent":"claude-code","state":"live","status":"idle","where":"FAKE-where-demo","cwd":"/tmp","turn":3}]}' ;;
  read) if [ -n "$FAKE_BIG" ]; then python3 -c 'import json; print(json.dumps({"complete":True,"how":"screen","text":"x"*20000+"FAKE-tail"}))'; else echo '{"complete":true,"how":"screen","text":"FAKE-hc-screen-9b2e"}'; fi ;;
  send) echo '{"started":true,"status":"working","turn":4}' ;;
  *) echo 'fake hc: unknown subcommand' >&2; exit 1 ;;
esac
"""


def install_fakes(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, body in (("herdr", FAKE_HERDR), ("hc", FAKE_HC)):
        p = root / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        paths[name] = p
    return paths


def rpc(mid, method, params=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def tool(mid, name, **arguments) -> dict:
    return rpc(mid, "tools/call", {"name": name, "arguments": arguments})


def initialize(mid=0, client="test-client") -> dict:
    return rpc(
        mid,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": client, "version": "0"},
        },
    )


def run_server(messages: list[dict], env: dict[str, str], minimal_env: bool = False):
    """Run the server once over stdio. Returns (responses by id, stderr, rc).

    Responses are keyed by JSON-RPC id, never by line position, so a stray
    notification or log line cannot shift what a test is looking at.
    With minimal_env the server gets only PATH=/usr/bin:/bin plus `env`, the
    way a GUI client launches it, not the developer's shell.
    """
    base = (
        {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
        if minimal_env
        else dict(os.environ)
    )
    for k in list(base):
        if k.startswith("HERDR_BRIDGE_"):
            del base[k]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**base, **env},
        timeout=60,
    )
    by_id = {}
    for line in proc.stdout.splitlines():
        if line.strip():
            msg = json.loads(line)
            by_id[msg.get("id")] = msg
    return by_id, proc.stderr, proc.returncode


def text_of(resp: dict) -> str:
    return resp["result"]["content"][0]["text"]
