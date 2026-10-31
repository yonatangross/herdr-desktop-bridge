"""Tests for bin/herdr_floor_mcp.py, the MCP to herdr floor bridge.

Drives the real server as a subprocess over stdio with a FAKE backend binary,
so every assertion is against bytes the server actually emitted. The fake
records its argv, so a refusal is checked by what reached the binary, not by
what the server claims.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import initialize, install_fakes, rpc, run_server, text_of, tool  # noqa: E402

TOOLS = {
    "herdr": {
        "floor_status",
        "floor_read",
        "floor_prompt",
        "floor_inbox",
        "floor_notify",
    },
    "hc": {"floor_status", "floor_read", "floor_prompt", "floor_inbox"},
}


@pytest.fixture(params=["herdr", "hc"])
def backend(request):
    return request.param


@pytest.fixture
def bridge(tmp_path: Path, backend: str):
    fakes = install_fakes(tmp_path / "bin")
    log = tmp_path / "argv.log"
    inbox = tmp_path / "inbox"

    def call(messages, env=None, client="test-client"):
        base = {
            "HERDR_BRIDGE_BACKEND": backend,
            "HERDR_BRIDGE_BIN": str(fakes[backend]),
            "HERDR_BRIDGE_INBOX_DIR": str(inbox),
            "FAKE_LOG": str(log),
        }
        out, err, rc = run_server(
            [initialize(0, client), *messages], {**base, **(env or {})}
        )
        assert rc == 0, err
        assert 0 in out, "initialize must answer"
        return out

    call.log = log  # type: ignore[attr-defined]
    call.inbox = inbox  # type: ignore[attr-defined]
    call.backend = backend  # type: ignore[attr-defined]
    return call


def argv(bridge) -> list[str]:
    return bridge.log.read_text().splitlines()


def test_initialize_and_tools_list(bridge):
    out = bridge(
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/list"),
        ]
    )
    assert out[0]["result"]["serverInfo"]["name"] == "herdr-floor"
    assert out[0]["result"]["capabilities"] == {"tools": {}}
    names = {t["name"] for t in out[2]["result"]["tools"]}
    assert names == TOOLS[bridge.backend]
    # no mutation tool may exist, by name
    assert not any(
        n.startswith(("floor_run", "floor_exec", "floor_key", "floor_close"))
        for n in names
    )


def test_status_condenses_the_floor(bridge):
    out = bridge([tool(1, "floor_status")])
    assert out[1]["result"]["isError"] is False
    text = text_of(out[1])
    if bridge.backend == "herdr":
        assert text.startswith("backend herdr · workspaces 1 · agents 1")
        assert "FAKE-floor-label" in text
        assert "conductor-one · working · demo · main · pane w1:p1" in text
        assert argv(bridge) == ["workspace list", "agent list"]
    else:
        assert text.startswith("backend hc · terminals 1")
        assert "reviewer-one · t7 · claude-code · live · idle · FAKE-where-demo" in text
        assert argv(bridge) == ["ls --json"]


def test_read_reaches_the_backend_with_the_expected_argv(bridge):
    out = bridge([tool(1, "floor_read", agent="conductor-one", lines=40)])
    assert out[1]["result"]["isError"] is False
    if bridge.backend == "herdr":
        assert text_of(out[1]) == "FAKE-herdr-screen-4d1c"
        assert argv(bridge) == [
            "agent read conductor-one --source recent-unwrapped --lines 40"
        ]
    else:
        assert text_of(out[1]) == "FAKE-hc-screen-9b2e"
        assert argv(bridge) == ["read --json --lines 40 conductor-one"]


def test_prompt_is_refused_by_default_before_touching_the_backend(bridge):
    out = bridge([tool(1, "floor_prompt", target="conductor", text="read the inbox")])
    res = out[1]["result"]
    assert res["isError"] is True
    assert "gate 2" in text_of(out[1])
    assert "HERDR_BRIDGE_PROMPT_ALLOW is empty" in text_of(out[1])
    assert not bridge.log.exists(), (
        "a refused target must never reach the backend binary"
    )


def test_prompt_allowed_target_reaches_the_backend(bridge):
    out = bridge(
        [tool(1, "floor_prompt", target="conductor", text="read the inbox")],
        env={"HERDR_BRIDGE_PROMPT_ALLOW": "conductor,*-desk"},
    )
    assert out[1]["result"]["isError"] is False
    if bridge.backend == "herdr":
        assert argv(bridge) == ["agent prompt conductor read the inbox"]
    else:
        assert argv(bridge) == ["send --json conductor -- read the inbox"]


def test_allow_list_narrows(bridge):
    out = bridge(
        [tool(1, "floor_prompt", target="comms-desk", text="hello")],
        env={"HERDR_BRIDGE_PROMPT_ALLOW": "conductor"},
    )
    assert out[1]["result"]["isError"] is True
    assert "no HERDR_BRIDGE_PROMPT_ALLOW pattern matches" in text_of(out[1])
    assert not bridge.log.exists()


@pytest.mark.parametrize(
    "target", ["-h", "Conductor", "a b", "x;whoami", "../etc", "box/t7"]
)
def test_structural_gate_holds_even_when_allow_list_is_wide_open(bridge, target):
    out = bridge(
        [tool(1, "floor_prompt", target=target, text="hello")],
        env={"HERDR_BRIDGE_PROMPT_ALLOW": "*"},
    )
    assert out[1]["result"]["isError"] is True
    assert "gate 1" in text_of(out[1])
    assert not bridge.log.exists(), (
        "a malformed target must never reach the backend binary"
    )


@pytest.mark.parametrize(
    "text, needle",
    [
        ("draft it -- then wait", "double hyphen"),
        ("-h", "start with a dash"),
        ("  --version", "start with a dash"),
    ],
)
def test_outbound_text_lint(bridge, text, needle):
    out = bridge(
        [tool(1, "floor_prompt", target="conductor", text=text)],
        env={"HERDR_BRIDGE_PROMPT_ALLOW": "*"},
    )
    assert out[1]["result"]["isError"] is True
    assert needle in text_of(out[1])
    assert not bridge.log.exists()


def test_inbox_writes_one_file_signed_with_the_client_name(bridge):
    out = bridge(
        [
            tool(
                1,
                "floor_inbox",
                sender="sales",
                title="Acme: confirm Thursday",
                body="Draft is ready.",
            )
        ],
        client="Grok",
    )
    res = out[1]["result"]
    assert res["isError"] is False
    path = Path(text_of(out[1]))
    assert path.parent == bridge.inbox
    assert path.name.endswith("-sales-acme-confirm-thursday.md")
    assert path.read_text(encoding="utf-8").startswith(
        "# Acme: confirm Thursday\n\nfrom: sales (Grok)"
    )
    assert not bridge.log.exists(), (
        "the inbox is a file write, it must not call the backend"
    )


def test_client_name_env_overrides_and_is_sanitized(bridge):
    out = bridge(
        [tool(1, "floor_inbox", sender="ops", title="note", body="hi")],
        env={"HERDR_BRIDGE_CLIENT_NAME": "Grok <b>bot</b>\n"},
        client="ignored",
    )
    body = Path(text_of(out[1])).read_text(encoding="utf-8")
    assert "from: ops (Grok bbotb)" in body


def test_notify_only_on_herdr(bridge):
    out = bridge([tool(1, "floor_notify", title="NEEDS YOU", body="the mail arrived")])
    if bridge.backend == "herdr":
        assert text_of(out[1]) == "shown"
        assert argv(bridge) == ["notification show NEEDS YOU --body=the mail arrived"]
    else:
        assert out[1]["error"]["code"] == -32602
        assert not bridge.log.exists()


def test_unknown_tool_and_method_are_jsonrpc_errors_not_crashes(bridge):
    out = bridge([tool(1, "floor_run", cmd="x"), rpc(2, "resources/list")])
    assert out[1]["error"]["code"] == -32602
    assert out[2]["error"]["code"] == -32601


def test_hc_read_is_truncated_with_a_notice(tmp_path):
    fakes = install_fakes(tmp_path / "bin")
    out, err, rc = run_server(
        [initialize(), tool(1, "floor_read", agent="t7")],
        {
            "HERDR_BRIDGE_BACKEND": "hc",
            "HERDR_BRIDGE_BIN": str(fakes["hc"]),
            "FAKE_LOG": str(tmp_path / "log"),
            "FAKE_BIG": "1",
        },
    )
    assert rc == 0, err
    text = text_of(out[1])
    assert text.startswith("[truncated to the last 12000 characters")
    assert text.endswith("FAKE-tail")


def test_unknown_backend_exits_2_with_a_named_error():
    out, err, rc = run_server([initialize()], {"HERDR_BRIDGE_BACKEND": "tmux"})
    assert rc == 2
    assert "not one of herdr, hc" in err
    assert out == {}


def test_banner_goes_to_stderr_with_the_tool_count(tmp_path):
    fakes = install_fakes(tmp_path / "bin")
    out, err, rc = run_server(
        [initialize()],
        {"HERDR_BRIDGE_BACKEND": "hc", "HERDR_BRIDGE_BIN": str(fakes["hc"])},
    )
    assert rc == 0
    assert "herdr-floor 0.2.0 backend=hc" in err and "tools=4" in err
    assert list(out) == [0], "stdout carries only JSON-RPC"
