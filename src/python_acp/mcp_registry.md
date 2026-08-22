# `mcp_registry.py` — which MCP servers a session talks to, and for how long

Until this module, `python-acp` bound **one** MCP server at process start from
`--mcp-command` and shared it with every client. ACP v1 inverts that: the client names
its servers in `session/new`, so the agent is an MCP *client* to N servers whose lifetime
is a session's, not the process's.

This module owns spawning, addressing, and tearing those down. It does **not** own the
stdio wire — that is [mcp_stdio.py](mcp_stdio.md), and this module only ever holds
`MCPStdioClient` instances. It does not own session state either: [sessions.py](sessions.md)
deliberately never imports MCP and reaches here through the `on_close` hook it is given
(decision B6a in [docs/module-boundaries.md](../../docs/module-boundaries.md)).

## stdio only, and that is a promise not a limitation

[capabilities.py](capabilities.md) advertises `mcpCapabilities.http`, `.sse`, and `.acp`
as `false`, and `McpServerStdio` needs no capability at all. [agent.py](agent.md) refuses
the three unadvertised transports before they reach here, so this module handles exactly
one shape.

Adding another means flipping a capability literal, which the test suite will not let
happen without a feature test behind it.

## Every session gets its own subprocess

Two sessions naming the same server do **not** share one. It costs a process; the
alternative costs correctness, because a shared backend makes `session/close` on one
session tear down another's tools.

That is the same reasoning `sessions.md` records for `fork_session`, and the two must
agree — otherwise a forked session's `close` becomes a landmine for its parent. A
refcounted share is a valid later optimisation *provided* closing one session cannot
disturb another.

## Opening is all-or-nothing

`session/new` either gets a session with every server it asked for, or an error.

A partially-opened session would hand back an id whose tools silently do not exist — the
same failure the capability manifest exists to prevent, arriving by a different route —
and would leak the subprocesses that did start. So `open` tears down whatever it managed
to start before re-raising, and `agent.py` closes the session it had just created.

Duplicate names within one `mcpServers` list are refused: `pyacp-hnk.2` routes a tool
call by server name, and two servers answering to one name would make which of them ran a
matter of dict ordering.

## The handshake happens at `open`

`connect_stdio` starts the subprocess **and** completes `initialize` before returning. A
server that cannot negotiate is a `session/new` failure the client can act on; finding
out mid-turn would surface as a broken prompt with no explanation.

## Environment is overlaid, not replacing

`McpServerStdio.env` is added on top of this process's own environment rather than
replacing it. A server command almost always needs `PATH` and `HOME` to run at all, and
withholding them would make every client-supplied server fail for a reason that looks
nothing like the cause.

It is not a sandbox boundary either way: whoever supplies `env` already supplies
`command`.

## Teardown

`close(session_id)` **removes the entry before stopping anything**, so a stop that raises
cannot leave a session addressable with dead backends — the same ordering, and the same
reason, as `SessionRegistry.close`.

One server that will not stop does not strand the rest: `_stop_all` logs and continues,
because the leak, not the failure, is what actually costs something.

`close` on an unknown session is a no-op. The `on_close` hook fires for every session and
most of them named no servers.

## Errors

`UnknownBackendError` subclasses **`ValueError`**, so [errors.py](errors.md) maps it to
`-32602` with the name in `data` — the same treatment `UnknownSessionError` gets, and for
the same reason: the client named something that does not exist, which is a parameter
problem.

## Main symbols

| Symbol | Purpose |
|---|---|
| `McpBackendRegistry` | `open` / `backends` / `get` / `close` / `close_all`, keyed by session id then name |
| `connect_stdio(server)` | Spawn one server and complete its handshake — the default `Connector` |
| `Connector` | The injection point; `tests/test_mcp_registry.py` drives lifetime and failure handling through it without spawning anything |
| `UnknownBackendError` | A server name the session did not open |

## Wiring

`cli.py` is the only place that constructs both registries, so it is the only place that
can connect them:

```python
backends = McpBackendRegistry()
sessions = SessionRegistry(on_close=backends.close)
```

A deployment that forgot the hook would leak one subprocess per session.
`cli.py` also calls `sessions.close_all()` on the way out, because sessions the client
never closed still own subprocesses.

## Tests

`tests/test_mcp_registry.py`. Most drive a fake `Connector`: what is under test is
*lifetime* — all-or-nothing opening, teardown ordering, one-failure-does-not-strand-the-
rest — and a real subprocess adds a handshake and two timeouts without exercising one
extra line of it. Three tests do use the real
`tests/fixtures/mock_mcp_server.py`, because "the spec actually becomes a running server"
is not something a fake can prove.

## Related

- [mcp_stdio.py docs](mcp_stdio.md) — the wire this module holds instances of
- [sessions.py docs](sessions.md) — the `on_close` seam
- [agent.py docs](agent.md) — where `session/new` refuses unadvertised transports
- [capabilities.py docs](capabilities.md) — why stdio is the only shape here
