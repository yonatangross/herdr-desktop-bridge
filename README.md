# herdr-desktop-bridge

A stdio MCP server that lets **Claude Desktop** see a [herdr](https://herdr.dev)
floor and leave it messages.

Claude Desktop is where you sit when you are not in a terminal. herdr is where
your coding agents actually live. This is the wire between the two, and it is
deliberately a thin one: Desktop gets a **mailbox and a doorbell**, never a
conductor seat.

Dependency free. Claude Desktop launches `bin/hq_floor_mcp.py` as a subprocess
and speaks MCP over newline delimited JSON-RPC on stdin/stdout. Nothing here
needs the `mcp` package, so it runs on the system `python3`.

---

## The refusal model

The design question was not "what can Desktop do to the floor", it was "what
happens when the Desktop side is wrong". A model on the other end of a chat
window has no working tree, no test run and no way to see what it broke. So the
surface is chosen so that the worst case is noise, not damage.

| Tool | Kind | What it can do |
|---|---|---|
| `floor_status` | read | condensed `workspace list` + `agent list` |
| `floor_read` | read | recent terminal output of one named agent |
| `floor_prompt` | write | `agent prompt <target> <text>`, target gated twice |
| `floor_inbox` | write | one markdown file under `~/.floor/inbox/` |
| `floor_notify` | write | one native herdr notification |

**Never exposed:** station open and close, workspace, tab or pane mutation,
label rewriting, send-keys, and any `pane run`. There is no escape hatch tool
and no generic CLI proxy. A full proxy was considered and rejected: it would
have made every future herdr subcommand reachable from Desktop by accident.

The two write paths that reach a running agent are shaped as **asynchronous
messages**, not commands:

- **The mailbox** (`floor_inbox`) writes a file into a corridor directory and
  returns the path. Nobody is obliged to read it. It touches no socket at all.
- **The doorbell** (`floor_notify`) puts a line on the operator's screen. A
  human decides what happens next.

`floor_prompt` is the one tool that speaks to a live agent, and it passes two
independent gates before a single byte reaches the `herdr` argv.

### Gate 1: the structural name gate (not configurable)

`^[a-z0-9][a-z0-9-]{0,63}$`. This is hardcoded and there is no setting that
relaxes it. It is what guarantees that no allow list entry, however loose, can
admit a flag shaped value (`-h`), a shell shaped value (`x;whoami`), or a path
shaped value (`../etc`) into the argv.

### Gate 2: `HQ_FLOOR_PROMPT_ALLOW` (operator configurable)

A comma separated list of `fnmatch` globs. A target must match at least one.

```
HQ_FLOOR_PROMPT_ALLOW="*-desk,conductor,conductor-*"   # the default
```

The default admits standing desk rows (`<slug>-desk`) and a conductor seat,
because those are **role** names: a desk row exists to receive briefs, and a
conductor exists to be told what the day looks like. A worker lane in the
middle of a task is not on that list, and that is the point.

A floor that names its lanes differently sets the variable instead of patching
the file. Setting it to `*` is legal and means "any structurally valid agent
name"; that is the operator's call, and gate 1 still holds. Setting it to an
empty string refuses every target.

Other knobs: `HQ_FLOOR_HERDR` (the binary, default `herdr`) and `HQ_FLOOR_DIR`
(the corridor root, default `~/.floor`).

### House rules on outbound text

Every write lints its text and refuses an em dash, an en dash or a double
hyphen. Some floors run a hook that rejects those; refusing here means the
failure is **named on the Desktop side** instead of being swallowed on the
floor. A value that starts with a dash is refused for the same reason it would
be dangerous: `herdr` would read it as a flag.

---

## Mounting it in Claude Desktop

Add this to `claude_desktop_config.json`, then restart Desktop:

```json
{
  "mcpServers": {
    "hq-floor": {
      "command": "python3",
      "args": ["/path/to/herdr-desktop-bridge/bin/hq_floor_mcp.py"]
    }
  }
}
```

To narrow or widen what Desktop may prompt, add the knob to the same entry:

```json
{
  "mcpServers": {
    "hq-floor": {
      "command": "python3",
      "args": ["/path/to/herdr-desktop-bridge/bin/hq_floor_mcp.py"],
      "env": { "HQ_FLOOR_PROMPT_ALLOW": "*-desk,conductor,conductor-*" }
    }
  }
}
```

`herdr` must be on the `PATH` that Desktop launches with. If it is not, set
`HQ_FLOOR_HERDR` to an absolute path in the same `env` block.

`HERDR_ENV=1` is a policy gate in herdr's own skill text, not a socket gate, so
a Desktop launched subprocess can front the socket without it.

---

## Installing it as a herdr plugin

```bash
herdr plugin link /path/to/herdr-desktop-bridge
herdr plugin action invoke floor.desktopbridge check
```

The plugin registers **one action and nothing else**: no startup hooks, no
event hooks. It owns no background work, because the MCP server is launched by
Desktop rather than by herdr. The `check` action is read only and answers the
only runtime question this plugin has: can the bridge see the socket, and what
is it currently willing to prompt.

---

## Acceptance

Each item names **how it fails**, because an acceptance list whose items cannot
fail is a list of wishes.

1. **The suite passes standalone.** `python3 -m pytest tests/ -q` from a clone,
   with no other repo on `sys.path`.
   *Fails as:* a collection error on an import that only resolves inside the
   repo this was extracted from.

2. **A refused target never reaches the binary.** Every refusal test asserts the
   fake `herdr`'s argv log **does not exist**, not merely that an error was
   returned.
   *Fails as:* a green refusal test sitting next to a log file proving the
   process ran anyway, which is the bug the test exists to catch.

3. **The allow list can narrow and widen, and gate 1 survives both.** Tests set
   `HQ_FLOOR_PROMPT_ALLOW` to a custom lane, to a value narrower than the
   default, to `*`, and to empty.
   *Fails as:* `*` admitting `-h` or `x;whoami`, which would mean the config had
   quietly replaced the structural gate instead of layering on top of it.

4. **The mailbox touches no socket.** The inbox test asserts the argv log does
   not exist after a write.
   *Fails as:* a corridor write that turns out to be a socket call, which would
   put the mailbox on the wrong side of the refusal model.

5. **The write surface is exactly five tools.** `tools/list` is asserted against
   an exact set, plus a negative assertion that no `floor_station*`,
   `floor_close*` or `floor_names*` tool exists.
   *Fails as:* a sixth tool added later and shipped without anyone noticing that
   the refusal model changed.

6. **Bad input is a JSON-RPC error, never a crash.** An unknown tool returns
   `-32602`, an unknown method returns `-32601`, and the process still exits 0.
   *Fails as:* a traceback on stdout, which corrupts the JSON-RPC stream and
   takes the whole Desktop session down rather than one call.

7. **`check` runs the server's own code.** It imports `hq_floor_mcp` and calls
   its real `_herdr` and `prompt_allow_patterns`.
   *Fails as:* a green check built from a similar looking shell command, which
   would report health for a code path the server does not use.

8. **The repo carries no private data.** `scripts/scrub.sh` greps for absolute
   home paths, account names, client slugs, secret manager references, internal
   lane names and backup files, and each pattern is proven live against a planted
   control before the repo is scanned.
   *Fails as:* a scrub that returns zero because its patterns are broken, which
   looks exactly like a scrub that returns zero because the repo is clean.

```bash
python3 -m pytest tests/ -q      # the suite
bash scripts/scrub.sh            # the privacy gate, controls first
python3 bin/check.py             # the live socket check
```

---

## Discovery

herdr's plugin marketplace auto discovers repositories by the **`herdr-plugin`
GitHub topic**. Discovery is by topic alone and listings are **unreviewed**, so
treat anything you find there the way you would treat any third party code that
is about to hold a socket to your agents: read it first.

This repository's topic is set at push time, not before. Until then it is
installable only by path, via `herdr plugin link`.

## License

MIT. See [LICENSE](LICENSE).
