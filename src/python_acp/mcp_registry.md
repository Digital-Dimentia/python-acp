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

## The session's roots are what we can answer

MCP is bidirectional, and `roots/list` is the **client** primitive whose answer this
process already has: a session's `cwd` plus its `additionalDirectories` is exactly what
MCP calls a root. (The other primitive a backend may reach here, `elicitation/create`,
is not answered so much as forwarded — see [elicitation.md](elicitation.md).)

So `open` hands those roots to every backend it starts, each one declares `roots` in its
`initialize` capability block, and `roots_responder` answers `roots/list` from them as
`file://` URIs.

Declaring and answering are one decision, not two — see the capability section of
[mcp_stdio.md](mcp_stdio.md). A backend given no roots and no forwarder therefore gets no
handler *and* no declaration; `connect_stdio(spec)` with both defaults is that case, and
it is what every direct caller in the tests takes.

| Capability | Declared here? | Why |
|---|---|---|
| `roots` | yes, whenever the session has roots | `cwd` + `additionalDirectories` is the answer |
| `roots.listChanged` | no (`false`) | a session's roots are fixed when it is created — `session/prompt` and `session/resume` both validate a `cwd` they then do not apply — so there is nothing to notify |
| `elicitation` | yes, whenever `open` is handed a forwarder | [elicitation.py](elicitation.md) answers it by forwarding to the ACP client, and [agent.py](agent.md) supplies a forwarder only when the connected client advertised form-mode elicitation |
| `sampling` | never | there is no LLM in this runtime |

`MCPStdioClient` takes exactly one `on_server_request`, so `backend_responder` composes
the two primitives into it: `roots/list` to `roots_responder`, `elicitation/create` to the
forwarder, and `UnsupportedServerRequest` — `-32601` — for anything else. That fallthrough
is what keeps a `sampling/createMessage` we never declared from coming back as `-32603`,
saying "we broke" instead of "we never offered that". A backend with neither roots nor a
forwarder gets **no handler at all**, which is what lets `initialize` refuse to send a
capability block nothing stands behind.

A forwarder is bound to one session id, so a fork never inherits its parent's: the caller
builds the child's. Roots do inherit, because a fork that names none means "the same
recipe as the parent"; a session id has no such reading.

Roots are stored per session alongside the specs, and for the same reason: a fork that
names no roots of its own reuses the parent's recipe. A fork that does name its own —
`session/fork` carries a `cwd` — declares *those* to its own subprocesses.

## Opening is all-or-nothing

`session/new` either gets a session with every server it asked for, or an error.

A partially-opened session would hand back an id whose tools silently do not exist — the
same failure the capability manifest exists to prevent, arriving by a different route —
and would leak the subprocesses that did start. So `open` tears down whatever it managed
to start before re-raising, and `agent.py` closes the session it had just created.

Duplicate names within one `mcpServers` list are refused: `pyacp-hnk.2` routes a tool
call by server name, and two servers answering to one name would make which of them ran a
matter of dict ordering.

## `add` and `remove` are the same rules at one server's granularity

`open` is all-or-nothing for a **whole session** and refuses a session that already has
backends. That is right at `session/new` and useless afterwards, and a client selecting a
catalogue entry mid-session needs the single-server form:

| | `open` | `add` / `remove` |
|---|---|---|
| Scope | every server a session will have | one |
| When | `session/new`, `session/fork` | any time the session is alive |
| Roots | taken from the caller | **reused** from the session |
| Already open | refuses the session | `add` returns the running client |

Roots are reused rather than taken again because they are `cwd` +
`additionalDirectories` and cannot change for the life of a session — a caller asked for
them a second time could only get them wrong.

Both keep `_specs` in step, which is the part that is easy to forget: the specs exist so a
`fork` can respawn them, so a fork after a toggle must inherit the session's **actual**
selection rather than what it was created with. `remove` drops the entry from the map
*before* stopping it, the same ordering `close` uses and for the same reason — a stop that
raises must not leave the session addressing a dead backend.

Adding a name that is already open is a **no-op**. A client re-sending a value it already
set is ordinary, and respawning would strand the first subprocess while looking like it
worked.

Who calls these, when a turn may not be running, and what happens to the config option
when a spawn fails are all [agent.py](agent.md)'s — this module has no opinion about
sessions beyond their ids.

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
| `McpBackendRegistry` | `open` / `add` / `remove` / `fork` / `backends` / `get` / `close` / `close_all`, keyed by session id then name |
| `connect_stdio(server, roots)` | Spawn one server and complete its handshake — the default `Connector` |
| `roots_responder(roots)` | Answers `roots/list`, and raises `UnsupportedServerRequest` for anything else |
| `backend_responder(roots, elicit)` | The single `on_server_request` handler a backend gets, composing `roots_responder` with the elicitation forwarder. `None` when there is nothing to answer |
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
extra line of it. Several do use the real `tests/fixtures/mock_mcp_server.py`, because
"the spec actually becomes a running server" is not something a fake can prove — and
neither is "the capability block we declared is the one that arrived", which the
fixture's `handshake-report` tool hands back verbatim.

## Related

- [mcp_stdio.py docs](mcp_stdio.md) — the wire this module holds instances of
- [sessions.py docs](sessions.md) — the `on_close` seam
- [agent.py docs](agent.md) — where `session/new` refuses unadvertised transports
- [capabilities.py docs](capabilities.md) — why stdio is the only shape here
