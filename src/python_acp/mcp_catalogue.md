# `mcp_catalogue.py` — the servers an operator configured

[Source](mcp_catalogue.py)

A file in; `McpServerStdio` recipes and `SessionConfigOptionBoolean` toggles out. No
registry, no agent, no subprocess.

## Why an agent-side catalogue exists at all

ACP puts `mcpServers` on `session/new`, and for its canonical topology that is right: the
client is an editor that **spawns** the agent over stdio, so it is a parent handing its own
configuration to a process it just created.

The WebSocket bridge inverts that. python-acp is a long-lived server an operator brings
up; clients connect afterwards. Two things follow:

- Requiring every client to know a server's command line is backwards — the operator
  configured this deployment, not the client.
- A client naming `command` and `args` is asking this process to **execute an arbitrary
  binary**. That is unremarkable when the client is your own editor and alarming on a
  shared socket, where the only thing between a caller and a spawn is the access key.

So the operator writes a catalogue and the client selects from it. Selection is strictly
safer than supply: the client picks from a list the operator approved.

**Both remain true at once.** `session/new`'s `mcpServers` is untouched — an editor that
knows its own servers keeps naming them, a thin client selects from the catalogue, and one
session can have both.

**Until an operator says otherwise.** This module made selection *available*; it did not
make supply *refusable*, and by default a client past the access key can still name a
command line. `--no-client-mcp-servers` is the other half (`pyacp-80k`): with it, a
non-empty `mcpServers` is `-32602` naming the flag and listing this catalogue, so the
selection above becomes the only way in. Off by default, because refusing is wrong for the
transport ACP was designed around. See [cli.md](cli.md) and [agent.md](agent.md).

## This is not `--mcp-command` returning

`pyacp-sld.4` removed a flag that bound **one** MCP server, process-wide, shared by every
connected client, reachable through a passthrough surface that let a client drive it
directly. The arrangement ACP v1 inverts.

A catalogue holds **recipes**. What it does not change:

| | Then (`--mcp-command`) | Now (catalogue) |
| --- | --- | --- |
| Servers per process | one, shared | one subprocess set **per session** |
| Who a server belongs to | the process | the session that opened it |
| Torn down when | the process exits | the session closes |
| Client reaches it by | a passthrough method | `session/prompt`, like any other tool |
| Client chooses | nothing | which entries its session uses |

The only thing that moved is where a recipe comes from.

## Selection is native ACP

One `SessionConfigOptionBoolean` per entry, advertised in `NewSessionResponse.configOptions`
and `LoadSessionResponse`, changed with `session/set_config_option`, announced with
`config_option_update`. No extension method, no `_meta`, nothing a client has to be taught.

ACP's `select` variant is single-choice, so a **set of booleans is what a multi-select
looks like** here. That is not a workaround: a server is independently on or off, which is
exactly what a boolean says.

Ids are namespaced `mcp/<name>` — `Session.set_config_option` looks options up by id
alone, so a catalogue entry called `announce-tools` would otherwise shadow the executor's
own toggle. `entry_for_config_id` is the one place that namespace is taken apart.

## The file

TOML is primary: `tomllib` is stdlib at this project's floor, and a file an operator
maintains wants comments.

```toml
[servers.tools]
command = "python"
args = ["server.py"]
env = { LOG = "debug" }
description = "Local demo tools"   # shown beside the toggle
enabled = true                     # whether a new session starts with it on
```

JSON is accepted too, chosen by suffix, because `{"mcpServers": {...}}` is the shape every
editor and desktop app already writes — an operator should be able to paste the config
they have rather than translate it. A bare top-level map works as well, so a fragment cut
out of a larger file needs no wrapper.

`env` is a table in the file and becomes the list of `EnvVariable` the SDK wants. It is
**not optional on the wire**: an entry that omits it is dropped (`pyacp-mej`), so an entry
with no environment still carries an empty list.

**Order is the file's order**, not sorted. It is what a settings panel renders, and that is
the operator's call.

## The file is re-read on `SIGHUP`

It used to be read **once, at startup**, and the reason given here was that a catalogue
changing under a running process would leave open sessions advertising toggles for servers
that no longer exist. That was the right *first* behaviour and the wrong permanent one: it
made adding a server cost a restart, which on the WebSocket transport drops every connected
client and every live session — for a deployment whose whole point is being long-lived and
shared. `pyacp-izr` answered the objection rather than the convenience: the toggles do get
brought back into line, at a moment where there is a client to tell.

**`SIGHUP`, on the WebSocket transport, only with `--mcp-config`.** Reloading is an
*operator* action. An ACP extension request would need a client to send it, putting a
deployment decision in the hands of whoever connected; watching the file is the most
convenient and the most surprising, because an editor saving a half-written file is a
reload nobody asked for. Under `--transport stdio` no handler is installed at all — there
the process is the client's child, restarting it is trivial and is what the editor already
does. [cli.md](cli.md) holds that decision.

**A bad file changes nothing.** `load` builds a *new* catalogue and raises without touching
the running one, so `replace` is never reached and every session stays exactly as it was.
The log line names the file, the entry and the key, as it does at startup. There is no
half-applied state to be in, because there is no state to half-apply until a complete
catalogue exists.

**`replace` mutates in place, and that is load-bearing.** The WebSocket transport builds
one `PythonAcpAgent` per connection and hands each the same catalogue object; rebinding a
name would reload the connection that happened to do it and leave every other one reading
the old list.

### What a reload means for a session already open

Not decided here. This module holds a list of recipes and knows nothing about sessions —
`agent._reconcile_catalogue` owns the four cases (an entry added, removed, changed, or the
file being invalid) and, more importantly, owns *when*: at the session's next request, not
at the signal. There is no client to notify at signal time, and a sweep would have to send
`config_option_update` down a connection that never heard of the session. See
[agent.md](agent.md).

## Why the validation is loud

A catalogue that half-parses is worse than one that refuses. A typo'd `commmand` would
otherwise produce an entry that is advertised, toggled on, and only *then* fails to spawn —
at which point the error names a subprocess rather than the line that was wrong. So:

- an unknown key is an error, not a shrug;
- every field is type-checked;
- every message names the file, the entry, and what was wrong.

`CatalogueError` is one type for all of it, because there is one caller — the CLI, at
startup — and its answer to every case is the same: refuse to start, and print why.

**A name may not contain `/`.** Server identity travels as `<server>/<tool>`: it is how
[turn_mcp_router.py](turn_mcp_router.md) routes a call, and the only way the palette
carries which server a command came from. A slash in the name would split in the wrong
place.

## Related

- [mcp_registry.py](mcp_registry.md) — what actually spawns these recipes, per session.
- [announcer.py](announcer.md) — why the palette follows a selection change.
- `pyacp-lx7` — the epic; `pyacp-lx7.1` is this module.
