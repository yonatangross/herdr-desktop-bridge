#!/usr/bin/env python3
"""Smoke test: a mock MCP client drives the bridge end to end. No pytest, no
real herdr, no real hc, no model and no API key.

It plays the part of a client such as Grok or Claude Desktop: it launches the
server the way a GUI client does (PATH=/usr/bin:/bin and nothing else), sends
initialize with a clientInfo name, lists tools, then runs one realistic task
against each backend:

  status  ->  read the first agent  ->  prompt it (refused, no allow list)
          ->  prompt it again (allowed)  ->  leave an inbox note

Every check names the literal output that would be missing if the bridge were
broken. Responses are matched by JSON-RPC id, never by line position.

Run:  python3 tests/smoke_mock_client.py      exit 0 only when every check holds
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import initialize, install_fakes, rpc, run_server, text_of, tool  # noqa: E402

CLIENT = "grok-mock"
failures: list[str] = []
checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def scenario(backend: str, tmp: Path) -> None:
    print(f"== backend {backend} ==")
    fakes = install_fakes(tmp / "bin")
    log = tmp / f"{backend}.argv"
    inbox = tmp / f"{backend}-inbox"
    env = {
        "HERDR_BRIDGE_BACKEND": backend,
        "HERDR_BRIDGE_BIN": str(fakes[backend]),
        "HERDR_BRIDGE_INBOX_DIR": str(inbox),
        "FAKE_LOG": str(log),
    }
    first = "conductor-one" if backend == "herdr" else "reviewer-one"
    screen = "FAKE-herdr-screen-4d1c" if backend == "herdr" else "FAKE-hc-screen-9b2e"
    expected_tools = 5 if backend == "herdr" else 4

    # Session 1: no allow list. The prompt must be refused before the backend runs.
    out, err, rc = run_server(
        [
            initialize(0, CLIENT),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(1, "tools/list"),
            tool(2, "floor_status"),
            tool(3, "floor_read", agent=first),
            tool(4, "floor_prompt", target=first, text="please read the inbox"),
            tool(
                5,
                "floor_inbox",
                sender="grok",
                title="Smoke note",
                body="left by the mock client",
            ),
        ],
        env,
        minimal_env=True,
    )
    check(rc == 0, f"server exits 0 (stderr: {err.strip()[:120]})")
    check(
        f"tools={expected_tools}" in err,
        f"stderr banner reports tools={expected_tools}",
    )
    check(
        out.get(0, {}).get("result", {}).get("serverInfo", {}).get("name")
        == "herdr-floor",
        "initialize answers as herdr-floor",
    )
    names = [t["name"] for t in out.get(1, {}).get("result", {}).get("tools", [])]
    check(
        len(names) == expected_tools and "floor_prompt" in names,
        f"tools/list returns {expected_tools} tools incl. floor_prompt",
    )
    check(
        first in text_of(out[2]) if 2 in out else False, f"floor_status lists {first}"
    )
    check(
        text_of(out[3]) == screen if 3 in out else False,
        f"floor_read returns the fake screen {screen}",
    )
    refused = text_of(out[4]) if 4 in out else ""
    check(
        out.get(4, {}).get("result", {}).get("isError") is True and "gate 2" in refused,
        "floor_prompt refused by gate 2 with no allow list",
    )
    calls = log.read_text().splitlines() if log.exists() else []
    check(
        bool(calls) and not any("prompt" in c or c.startswith("send") for c in calls),
        "the refused prompt never reached the backend (status and read did)",
    )
    note = Path(text_of(out[5])) if 5 in out else Path("/nonexistent")
    check(
        note.is_file() and note.parent == inbox,
        "floor_inbox wrote one file in the inbox dir",
    )
    check(
        note.is_file() and f"from: grok ({CLIENT})" in note.read_text(encoding="utf-8"),
        "the inbox note is signed with the client's own name",
    )

    # Session 2: the operator allows the target. The prompt must reach the backend.
    log.unlink(missing_ok=True)
    out, err, rc = run_server(
        [
            initialize(0, CLIENT),
            tool(1, "floor_prompt", target=first, text="please read the inbox"),
        ],
        {**env, "HERDR_BRIDGE_PROMPT_ALLOW": f"{first}"},
        minimal_env=True,
    )
    check(
        rc == 0 and out.get(1, {}).get("result", {}).get("isError") is False,
        "floor_prompt allowed once the target is on the allow list",
    )
    want = (
        f"agent prompt {first} please read the inbox"
        if backend == "herdr"
        else f"send --json {first} -- please read the inbox"
    )
    got = log.read_text().splitlines() if log.exists() else []
    check(got == [want], f"backend argv is exactly: {want}")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        for backend in ("herdr", "hc"):
            scenario(backend, Path(d) / backend)
    print()
    if failures:
        print(f"SMOKE FAIL: {len(failures)} of {checks} checks failed")
        return 1
    print(f"SMOKE PASS: {checks} checks, 2 backends, mock client {CLIENT!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
