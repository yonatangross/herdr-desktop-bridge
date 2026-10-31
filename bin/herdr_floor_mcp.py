#!/usr/bin/env python3
"""herdr-floor: a stdio MCP server that lets any MCP client reach a herdr floor.

Claude Desktop, Grok, or any other client that can launch a stdio MCP server
starts this file as a subprocess and speaks MCP over newline delimited JSON-RPC
on stdin/stdout. It is dependency free on purpose: nothing here needs the `mcp`
package, so it runs on the system python3 and it is testable with a fake
backend binary on PATH.

Two backends, picked by HERDR_BRIDGE_BACKEND:
  herdr  the herdr CLI (`herdr workspace list`, `herdr agent prompt`, ...)
  hc     the herdr-core client (`hc ls`, `hc read`, `hc send`)

Surface, deliberately small (a full CLI proxy was considered and rejected):
  floor_status   read   every agent or terminal on the floor, condensed
  floor_read     read   recent output of one named agent or terminal
  floor_prompt   write  submit text to one agent; the target must pass BOTH
                        the structural name gate and HERDR_BRIDGE_PROMPT_ALLOW
  floor_inbox    write  one markdown file in the inbox directory, the mailbox
  floor_notify   write  one native herdr notification, the doorbell
                        (herdr backend only; hc has no notification command)

Never exposed: workspace, tab or pane creation and mutation, label rewriting,
key presses, interrupts, `pane run`, `hc run`, `hc exec`, `hc file`. A wrong or
compromised client gets a mailbox and a doorbell, not a seat at the floor.

Every outbound text is linted: a value that starts with a dash is refused (the
backend would parse it as a flag), and so are em dashes, en dashes and double
hyphens (some floors run hooks that reject them; refusing here names the
failure on the client side instead of losing it on the floor).

Environment (see .env.example):
  HERDR_BRIDGE_BACKEND        herdr (default) or hc
  HERDR_BRIDGE_BIN            backend binary, default `herdr` or `hc`
  HERDR_BRIDGE_PROMPT_ALLOW   comma separated fnmatch globs of promptable
                              names; empty or unset refuses every target
  HERDR_BRIDGE_INBOX_DIR      mailbox directory, default ~/.herdr-bridge/inbox
  HERDR_BRIDGE_CLIENT_NAME    sender label in inbox files; default is the
                              clientInfo.name the client sent on initialize
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
VERSION = "0.2.0"
SERVER_INFO = {"name": "herdr-floor", "version": VERSION}

BACKENDS = ("herdr", "hc")
BACKEND = os.environ.get("HERDR_BRIDGE_BACKEND", "herdr").strip() or "herdr"
BIN = os.environ.get("HERDR_BRIDGE_BIN", "").strip() or BACKEND
INBOX_DIR = Path(
    os.environ.get("HERDR_BRIDGE_INBOX_DIR", "").strip()
    or str(Path.home() / ".herdr-bridge" / "inbox")
)

# The STRUCTURAL gate. Not configurable, on purpose: it is what guarantees that
# no allow list entry, however loose, can ever admit a flag shaped, shell
# shaped or path shaped value into the backend argv. The allow list can only
# NARROW what this already permits, it can never widen it.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SENDER_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
CLIENT_NAME_RE = re.compile(r"[^A-Za-z0-9 ._-]+")
# em dash, en dash, double hyphen; built from code points so this file carries none
BAD_DASH_RE = re.compile("|".join((chr(0x2014), chr(0x2013), "-" * 2)))

READ_CAP = 12000  # characters returned by floor_read before a truncation notice

_state = {"client": os.environ.get("HERDR_BRIDGE_CLIENT_NAME", "").strip()}


class ToolError(Exception):
    """Named refusal that surfaces to the client as isError=true, never a crash."""


def prompt_allow_patterns() -> list[str]:
    raw = os.environ.get("HERDR_BRIDGE_PROMPT_ALLOW", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def client_name() -> str:
    name = CLIENT_NAME_RE.sub("", _state["client"]).strip()[:40]
    return name or "MCP client"


# ---------------------------------------------------------------- backend calls


def _run(*args: str) -> str:
    proc = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise ToolError(
            f"{BACKEND} {' '.join(args[:2])} failed rc={proc.returncode}: "
            f"{proc.stderr.strip()[:300]}"
        )
    return proc.stdout


def _json(*args: str) -> dict:
    out = _run(*args)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ToolError(f"{BACKEND} returned non-JSON: {out[:200]!r}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"{BACKEND} returned JSON that is not an object")
    return data


def _herdr_status() -> list[str]:
    ws = _json("workspace", "list").get("result", {}).get("workspaces", [])
    ag = _json("agent", "list").get("result", {}).get("agents", [])
    lines = [f"backend herdr · workspaces {len(ws)} · agents {len(ag)}"]
    for w in ws:
        lines.append(
            f"[{w.get('workspace_id')}] {w.get('label')} · {w.get('agent_status')}"
        )
    for a in ag:
        tok = a.get("tokens", {}) or {}
        lines.append(
            f"  {a.get('name') or '(unnamed)'} · {a.get('agent_status')} · "
            f"{tok.get('repo', '?')} · {tok.get('wt', '')} · pane {a.get('pane_id')}"
        )
    return lines


def _hc_status() -> list[str]:
    terms = _json("ls", "--json").get("terminals", [])
    lines = [f"backend hc · terminals {len(terms)}"]
    for t in terms:
        lines.append(
            f"  {t.get('name') or '(unnamed)'} · {t.get('address')} · "
            f"{t.get('agent') or '?'} · {t.get('state')} · {t.get('status')} · "
            f"{t.get('where') or ''}"
        )
    return lines


def _herdr_read(name: str, lines: int) -> str:
    res = _json(
        "agent", "read", name, "--source", "recent-unwrapped", "--lines", str(lines)
    ).get("result", {})
    return res.get("text") or json.dumps(res)


def _hc_read(name: str, lines: int) -> str:
    res = _json("read", "--json", "--lines", str(lines), name)
    return res.get("text") or json.dumps(res)


def _herdr_prompt(target: str, text: str) -> str:
    out = _json("agent", "prompt", target, text)
    return json.dumps(out.get("result", out))[:1000]


def _hc_prompt(target: str, text: str) -> str:
    # No --force: a terminal parked on a dialog is refused by hc itself, so a
    # prompt from here can never answer a permission question by accident.
    out = _json("send", "--json", target, "--", text)
    return json.dumps(out)[:1000]


def _herdr_notify(title: str, body: str) -> None:
    cmd = ["notification", "show", title]
    if body:
        cmd.append(f"--body={body}")
    _json(*cmd)


# ---------------------------------------------------------------- tool handlers


def _lint_text(text: str, field: str) -> None:
    if text.lstrip().startswith("-"):
        raise ToolError(
            f"{field} must not start with a dash, the backend would parse it as a flag. "
            "Reword it so it starts with a letter or digit."
        )
    if BAD_DASH_RE.search(text):
        raise ToolError(
            f"{field} contains an em dash, en dash or double hyphen, which the floor "
            "refuses. Use a comma, a period or parentheses instead."
        )


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "brief"


def tool_floor_status(_: dict) -> str:
    lines = _hc_status() if BACKEND == "hc" else _herdr_status()
    return "\n".join(lines)


def tool_floor_read(args: dict) -> str:
    name = str(args["agent"])
    if not NAME_RE.match(name):
        raise ToolError(
            "agent must be a name or address exactly as floor_status prints it "
            "([a-z0-9][a-z0-9-]{0,63})"
        )
    lines = max(10, min(400, int(args.get("lines", 80))))
    text = _hc_read(name, lines) if BACKEND == "hc" else _herdr_read(name, lines)
    if len(text) > READ_CAP:
        text = text[-READ_CAP:]
        text = (
            f"[truncated to the last {READ_CAP} characters; "
            "call again with fewer lines for less]\n" + text
        )
    return text


def tool_floor_prompt(args: dict) -> str:
    target = str(args["target"])
    text = str(args["text"])
    patterns = prompt_allow_patterns()
    if not NAME_RE.match(target):
        raise ToolError(
            f"target {target!r} refused by gate 1: not a structurally valid name "
            "([a-z0-9][a-z0-9-]{0,63})"
        )
    if not patterns:
        raise ToolError(
            f"target {target!r} refused by gate 2: HERDR_BRIDGE_PROMPT_ALLOW is empty, "
            "so every prompt is refused. The operator sets it in the client's MCP "
            "server env. Use floor_inbox to leave a message instead."
        )
    if not any(fnmatch.fnmatchcase(target, pat) for pat in patterns):
        raise ToolError(
            f"target {target!r} refused by gate 2: no HERDR_BRIDGE_PROMPT_ALLOW pattern "
            f"matches it (allow list: {', '.join(patterns)}). Use floor_inbox instead."
        )
    _lint_text(text, "text")
    return _hc_prompt(target, text) if BACKEND == "hc" else _herdr_prompt(target, text)


def tool_floor_inbox(args: dict) -> str:
    sender = str(args["sender"])
    if not SENDER_RE.match(sender):
        raise ToolError("sender must be a short slug ([a-z][a-z0-9-]{0,31})")
    title, body = str(args["title"]), str(args["body"])
    _lint_text(title, "title")
    _lint_text(body, "body")
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = INBOX_DIR / f"{stamp}-{sender}-{_slug(title)}.md"
    path.write_text(
        f"# {title}\n\nfrom: {sender} ({client_name()}) · {stamp}\n\n{body.rstrip()}\n",
        encoding="utf-8",
    )
    return str(path)


def tool_floor_notify(args: dict) -> str:
    title = str(args["title"])
    body = str(args.get("body", ""))
    _lint_text(title, "title")
    if body:
        _lint_text(body, "body")
    _herdr_notify(title, body)
    return "shown"


_NAME_HINT = "name or address exactly as floor_status prints it"

TOOL_SPECS = {
    "floor_status": {
        "description": (
            "List every agent (herdr) or terminal (hc) on the floor, one line each: "
            "name, status and where it works. Read only. Call this first; other "
            "tools take a name from this list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_floor_status,
    },
    "floor_read": {
        "description": (
            "Recent terminal output of one agent or terminal. Read only. Output over "
            f"{READ_CAP} characters is cut to the newest part with a notice."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": _NAME_HINT},
                "lines": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 400,
                    "default": 80,
                },
            },
            "required": ["agent"],
            "additionalProperties": False,
        },
        "handler": tool_floor_read,
    },
    "floor_prompt": {
        "description": (
            "Submit text to one agent as if typed and sent. The target must be a "
            "plain lowercase name AND match the operator's HERDR_BRIDGE_PROMPT_ALLOW "
            "globs; otherwise it is refused before anything runs, and the refusal "
            "says which gate. Prefer floor_inbox when the message can wait."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _NAME_HINT},
                "text": {"type": "string", "description": "must not start with a dash"},
            },
            "required": ["target", "text"],
            "additionalProperties": False,
        },
        "handler": tool_floor_prompt,
    },
    "floor_inbox": {
        "description": (
            "Leave a markdown note in the floor's inbox directory for whoever reads "
            "it next. Touches no agent. Returns the path of the file written."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sender": {
                    "type": "string",
                    "description": "short lowercase slug naming who is writing",
                },
                "title": {"type": "string"},
                "body": {"type": "string", "description": "markdown"},
            },
            "required": ["sender", "title", "body"],
            "additionalProperties": False,
        },
        "handler": tool_floor_inbox,
    },
    "floor_notify": {
        "description": (
            "Ring the doorbell: one native herdr notification on the operator's "
            "screen. A person decides what happens next."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "default": ""},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        "handler": tool_floor_notify,
    },
}

# hc has no notification command, so the hc backend does not list floor_notify
# at all: a listed tool that always fails teaches a client nothing.
BACKEND_TOOLS = {
    "herdr": (
        "floor_status",
        "floor_read",
        "floor_prompt",
        "floor_inbox",
        "floor_notify",
    ),
    "hc": ("floor_status", "floor_read", "floor_prompt", "floor_inbox"),
}


def tools() -> list[dict]:
    return [
        {
            "name": n,
            "description": TOOL_SPECS[n]["description"],
            "inputSchema": TOOL_SPECS[n]["inputSchema"],
        }
        for n in BACKEND_TOOLS[BACKEND]
    ]


# ---------------------------------------------------------------- JSON-RPC


def handle(msg: dict) -> dict | None:
    """Return a JSON-RPC response for a request, or None for a notification."""
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:
        return None  # notifications/initialized and friends
    if method == "initialize":
        params = msg.get("params") or {}
        info = params.get("clientInfo") or {}
        if not _state["client"] and isinstance(info, dict):
            _state["client"] = str(info.get("name") or "")
        return _ok(
            mid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": tools()})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = str(params.get("name") or "")
        if name not in BACKEND_TOOLS[BACKEND]:
            return _err(mid, -32602, f"unknown tool {name!r} on backend {BACKEND}")
        try:
            text = TOOL_SPECS[name]["handler"](params.get("arguments") or {})
            return _ok(
                mid, {"content": [{"type": "text", "text": text}], "isError": False}
            )
        except ToolError as exc:
            return _ok(
                mid,
                {
                    "content": [{"type": "text", "text": f"REFUSED: {exc}"}],
                    "isError": True,
                },
            )
        except (
            subprocess.TimeoutExpired,
            OSError,
            KeyError,
            ValueError,
            TypeError,
        ) as exc:
            return _ok(
                mid,
                {
                    "content": [{"type": "text", "text": f"FAILED: {exc!r}"}],
                    "isError": True,
                },
            )
    return _err(mid, -32601, f"method not found: {method}")


def _ok(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> int:
    if BACKEND not in BACKENDS:
        sys.stderr.write(
            f"herdr-floor: HERDR_BRIDGE_BACKEND={BACKEND!r} is not one of {', '.join(BACKENDS)}\n"
        )
        return 2
    # The banner goes to stderr: stdout carries only JSON-RPC.
    sys.stderr.write(
        f"herdr-floor {VERSION} backend={BACKEND} bin={BIN} "
        f"tools={len(BACKEND_TOOLS[BACKEND])} prompt_allow={len(prompt_allow_patterns())}\n"
    )
    sys.stderr.flush()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
