#!/usr/bin/env python3
"""The plugin's one action: can the bridge see the floor, and what will it prompt.

Read only. Runs the same code paths the MCP server runs, so a green check here
means the server would work, not that a similar looking command worked.

Exit codes: 0 all green, 1 the floor is not reachable, 2 the allow list is
empty (the bridge would refuse every prompt target), 3 unknown backend.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import herdr_floor_mcp as bridge  # noqa: E402

SERVER = Path(__file__).resolve().parent / "herdr_floor_mcp.py"


def main() -> int:
    if bridge.BACKEND not in bridge.BACKENDS:
        print(
            f"backend     UNKNOWN {bridge.BACKEND!r}, use one of {', '.join(bridge.BACKENDS)}"
        )
        return 3
    rc = 0
    print(f"server      {SERVER}")
    print(f"backend     {bridge.BACKEND} ({bridge.BIN})")
    print(f"tools       {', '.join(bridge.BACKEND_TOOLS[bridge.BACKEND])}")
    print(f"inbox       {bridge.INBOX_DIR}")

    patterns = bridge.prompt_allow_patterns()
    source = (
        "HERDR_BRIDGE_PROMPT_ALLOW"
        if "HERDR_BRIDGE_PROMPT_ALLOW" in os.environ
        else "unset"
    )
    print(f"allow list  {', '.join(patterns) or '(empty)'}   [{source}]")
    if not patterns:
        print("            WARNING: empty, floor_prompt refuses every target")
        rc = 2

    try:
        first = bridge.tool_floor_status({}).splitlines()[0]
        print(f"floor       reachable, {first}")
    except (bridge.ToolError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print(f"floor       UNREACHABLE: {exc}")
        rc = 1

    env = {"HERDR_BRIDGE_BACKEND": bridge.BACKEND}
    if patterns:
        env["HERDR_BRIDGE_PROMPT_ALLOW"] = ",".join(patterns)
    snippet = {
        "mcpServers": {
            "herdr-floor": {"command": "python3", "args": [str(SERVER)], "env": env}
        }
    }
    print(
        "\nMCP server entry (Claude Desktop shape; other clients take the same command and env):"
    )
    print(json.dumps(snippet, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
