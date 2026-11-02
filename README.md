# herdr-desktop-bridge

A stdio MCP server that lets **any MCP client** (Claude Desktop, Grok, or
anything else that can launch a stdio MCP server) see a
[herdr](https://herdr.dev) floor and leave it messages.

The chat window is where you sit when you are not in a terminal. herdr is where
your coding agents actually live. This is the wire between the two, and it is
deliberately a thin one: the client gets a **mailbox and a doorbell**, never a
seat at the floor.

Dependency free. The client launches `bin/herdr_floor_mcp.py` as a subprocess
and speaks MCP over newline delimited JSON-RPC on stdin/stdout. Nothing here
needs the `mcp` package, so it runs on the system `python3` (3.9 or newer).

It drives the floor through one of two backends:

| Backend | Binary | Used for |
|---|---|---|
| `herdr` (default) | the herdr CLI | `workspace list`, `agent list`, `agent read`, `agent prompt`, `notification show` |
| `hc` | the herdr-core client | `hc ls`, `hc read`, `hc send` |

---

## The refusal model

The design question was not "what can the client do to the floor", it was
"what happens when the client side is wrong". A model on the other end of a
chat window has no working tree, no test run and no way to see what it broke.
So the surface is chosen so that the worst case is noise, not damage.

| Tool | Kind | What it can do | Backends |
|---|---|---|---|
| `floor_status` | read | one line per agent or terminal: name, status, where | both |
| `floor_read` | read | recent output of one agent or terminal named exactly in `HERDR_BRIDGE_READ_ALLOW` | both |
| `floor_prompt` | write | submit text to one agent, target gated twice | both |
| `floor_inbox` | write | one markdown file in the inbox directory | both |
| `floor_notify` | write | one native herdr notification | herdr only |

`hc` has no notification command, so on the `hc` backend `floor_notify` is not
listed at all. A listed tool that always fails teaches a client nothing.

**Never exposed:** workspace, tab or pane creation and mutation, label
rewriting, key presses, interrupts, `pane run`, `hc run`, `hc exec` and
`hc file`. There is no escape hatch tool and no generic CLI proxy. A full proxy
was considered and rejected: it would make every future subcommand reachable
from the chat window by accident.

The two write paths that do not reach a running agent are shaped as
**asynchronous messages**, not commands:

- **The mailbox** (`floor_inbox`) writes a file and returns its path. Nobody is
  obliged to read it. It touches no backend at all. The note is signed with the
  sender slug and the client's own name (from `clientInfo` on `initialize`), so
  a reader can tell a Grok note from a Claude note.
- **The doorbell** (`floor_notify`) puts a line on the operator's screen. A
  person decides what happens next.

`floor_prompt` is the one tool that speaks to a live agent, and it passes two
independent gates before a single byte reaches the backend argv.

### Gate 1: the structural name gate (not configurable)

`^[a-z0-9][a-z0-9-]{0,63}$`. This is hardcoded and no setting relaxes it. It
guarantees that no allow list entry, however loose, can admit a flag shaped
value (`-h`), a shell shaped value (`x;whoami`), or a path shaped value
(`../etc`, `box/t7`) into the argv.

### Gate 2: `HERDR_BRIDGE_PROMPT_ALLOW` (operator configurable)

A comma separated list of `fnmatch` globs. A target must match at least one.
**Unset or empty refuses every target**, so a fresh install can read and leave
notes but cannot prompt anyone until the operator says who.

```
HERDR_BRIDGE_PROMPT_ALLOW="conductor,*-desk"
```

Name role seats here (a seat whose job is to receive briefs), not worker lanes
in the middle of a task. `*` is legal and means "any structurally valid name";
that is the operator's call, and gate 1 still holds.

### Read gate: `HERDR_BRIDGE_READ_ALLOW` (operator configurable)

`floor_read` hands a terminal's output to the client's model, which for Grok
and other third-party clients is an outside model. So reading is default-deny:

- A comma separated list of **exact** seat names. No globs, no prefixes, no
  case folding: `conductor` does not admit `conductor-one`, and `*` admits
  nothing. A new or similarly named terminal stays denied until it is named.
- **Unset or empty refuses every read.**
- **Fails closed.** Any error inside the access check is a denial.
- **Every denied read is logged** as one JSON line (`ts`, `client`, `target`,
  `reason`) to `HERDR_BRIDGE_AUDIT_LOG` (default `~/.herdr-bridge/audit.log`).
  A log that cannot be written never turns a denial into an allow.

```
HERDR_BRIDGE_READ_ALLOW="conductor,release-seat"
```

Never list a client desk or a private lane here.

On the `hc` backend the prompt is sent without `--force`, so a terminal that is
parked on a dialog is refused by hc itself. A prompt from the chat window can
never answer a permission question by accident.

### Outbound text lint

Every write refuses text that starts with a dash (the backend would read it as
a flag) and text with an em dash, an en dash or a double hyphen (some floors
run hooks that reject those; refusing here names the failure on the client
side instead of losing it on the floor). Each refusal says which rule fired and
what to do instead.

---

## Install

```bash
git clone <this repository's URL> herdr-desktop-bridge
cd herdr-desktop-bridge
python3 tests/smoke_mock_client.py     # no herdr, no hc, no API key needed
```

### Claude Desktop

Add this to `claude_desktop_config.json`, then restart Desktop:

```json
{
  "mcpServers": {
    "herdr-floor": {
      "command": "python3",
      "args": ["/path/to/herdr-desktop-bridge/bin/herdr_floor_mcp.py"],
      "env": {
        "HERDR_BRIDGE_BACKEND": "hc",
        "HERDR_BRIDGE_PROMPT_ALLOW": "conductor,*-desk",
        "HERDR_BRIDGE_READ_ALLOW": "conductor"
      }
    }
  }
}
```

### Grok and other MCP clients

Any client that launches a **local stdio** MCP server takes the same three
things: the command `python3`, the argument `/path/to/bin/herdr_floor_mcp.py`,
and the environment variables below. The exact place you type them depends on
the client; the server does not care which client it is.

`python3 bin/check.py` prints a ready made server entry for the current
settings and tells you whether the floor is reachable.

### As a herdr plugin

```bash
herdr plugin link /path/to/herdr-desktop-bridge
herdr plugin action invoke floor.mcpbridge check
```

The plugin registers **one read only action and nothing else**: no startup
hooks, no event hooks. The MCP server is launched by the client, not by herdr.

---

## Configuration

All settings are environment variables, passed in the client's server entry.
`.env.example` lists them. No secret is needed: the bridge talks only to a
local binary.

| Variable | Default | Meaning |
|---|---|---|
| `HERDR_BRIDGE_BACKEND` | `herdr` | `herdr` or `hc` |
| `HERDR_BRIDGE_BIN` | `herdr` or `hc` | absolute path when the binary is not on the client's `PATH` |
| `HERDR_BRIDGE_PROMPT_ALLOW` | empty (refuse all) | comma separated globs of promptable names |
| `HERDR_BRIDGE_READ_ALLOW` | empty (refuse all) | comma separated EXACT names `floor_read` may return |
| `HERDR_BRIDGE_AUDIT_LOG` | `~/.herdr-bridge/audit.log` | one JSON line per denied read |
| `HERDR_BRIDGE_INBOX_DIR` | `~/.herdr-bridge/inbox` | where `floor_inbox` writes |
| `HERDR_BRIDGE_CLIENT_NAME` | the client's `clientInfo.name` | sender label in inbox notes |

GUI clients often launch servers with a short `PATH` (`/usr/bin:/bin`). If
`herdr` or `hc` lives elsewhere, set `HERDR_BRIDGE_BIN`.

---

## Limits

- **Local stdio only.** There is no HTTP transport. A client that cannot launch
  a local process (a model in a browser tab, a cloud API calling remote MCP
  servers) cannot use this directly. Exposing `floor_prompt` on a network port
  is a different threat model and is out of scope on purpose.
- **One machine.** Names with a machine prefix (`box/t7`) are refused by gate 1,
  so `hc` terminals on other machines are not reachable.
- **Names must be lowercase** letters, digits and hyphens. An agent or terminal
  named with capitals, dots or underscores can be read through `floor_status`
  but not targeted; rename it or use its `hc` address (`t7`).
- **No doorbell on `hc`.** `floor_notify` exists on the herdr backend only.
- **The inbox has no reader built in.** Something on the floor has to watch the
  directory; the bridge only writes the file.
- **`floor_read` returns at most 12000 characters**, the newest part, with a
  notice when it cut.

---

## Testing

```bash
python3 tests/smoke_mock_client.py   # mock MCP client, both backends, no keys
python3 -m pytest tests/ -q          # the full suite
bash scripts/scrub.sh                # the privacy gate, controls first
python3 bin/check.py                 # live: can the bridge see your floor
```

The smoke test plays an MCP client named `grok-mock`. It launches the server
the way a GUI client does (`PATH=/usr/bin:/bin`, nothing else), runs status,
read, a refused prompt, an allowed prompt and an inbox note against a fake
`herdr` and a fake `hc`, and checks the exact argv each fake received.

Each item below names **how it fails**, because a check that cannot fail is a
wish.

1. **A refused target never reaches the binary.** Refusal tests assert the fake
   backend's argv log does not exist.
   *Fails as:* a green refusal test next to a log proving the process ran.
2. **Gate 1 survives any allow list.** Tests set the allow list to `*` and try
   `-h`, `x;whoami`, `../etc`, `box/t7`.
   *Fails as:* `*` admitting a flag or shell shaped value.
3. **Prompts are refused until the operator allows them.** Unset allow list,
   refused, nothing reaches the binary.
   *Fails as:* a fresh install that can type into any agent.
4. **Reads are refused until the operator names the seat, exactly.** Unset
   list, an unlisted seat, a prefix, a glob and a different case are all
   refused before the binary runs; an error in the check denies; each denial
   writes one audit line.
   *Fails as:* a third-party model reading a client desk or a private lane.
5. **The mailbox touches no backend.** The inbox test asserts no argv log.
6. **The tool set is exact per backend.** Five on herdr, four on hc, asserted as
   sets, plus a negative check that no run, exec, key or close tool exists.
7. **Bad input is a JSON-RPC error, never a crash.** Unknown tool `-32602`,
   unknown method `-32601`, the process still exits 0. Only JSON-RPC goes to
   stdout; the startup banner goes to stderr.
8. **The smoke test can fail.** Breaking the hc prompt argv, or opening gate 2,
   turns it red (checked by hand with mutated copies before release).
9. **The repo carries no private data.** `scripts/scrub.sh` runs every pattern
   against a planted control first, then scans. Patterns that would themselves
   leak (your names, handles, customer list) live in files outside the repo,
   named by `SCRUB_PRIVATE_PATTERNS` and `SCRUB_CLIENTS_DIR`. A skipped check
   is reported, never counted as a pass.

---

## Discovery

herdr's plugin marketplace auto discovers repositories by the **`herdr-plugin`
GitHub topic**. Listings are **unreviewed**, so treat anything you find there
the way you would treat any third party code that is about to hold a socket to
your agents: read it first.

## License

MIT. See [LICENSE](LICENSE).
