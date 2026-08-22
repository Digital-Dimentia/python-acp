# `sessions.py` — what the agent remembers between `session/new` and `session/close`

A session is the unit every ACP method after `initialize` is addressed to. This module
owns the record and the registry that holds them, and nothing else — no JSON-RPC shapes,
no MCP subprocesses, no prompt execution. [agent.py](agent.md) translates requests into
calls on `SessionRegistry`; `mcp_registry.py` (Phase 2.3) owns the backends a session's
turns use.

> **Not yet wired.** `session/*` still answers `-32601`. `pyacp-3rw.2` connects
> `new_session` / `prompt` / `cancel` to this registry and `pyacp-3rw.3` the rest. The
> module exists first so those beads add call sites rather than invent state.

## Why not `acp.contrib.session_state`

`pyacp-3rw.1` was told to evaluate the SDK's session helper before writing one. It does
not fit, and the reason is **direction**, not completeness:

| | `SessionAccumulator` | what `session/new` needs |
|---|---|---|
| Side of the wire | consumes `SessionNotification`s — the things an agent *sends* | we are the sender |
| Cardinality | one session; a notification for another id resets it or raises | a registry addressable by id |
| Contents | messages, tool calls, plan, current mode | cwd, `additionalDirectories`, modes, config options, timestamps, lifetime |
| Stability | "**experimental**: APIs may change while we gather feedback" | under `session/new` |

So: **replaced, deliberately.**

It is still the right tool for a *different* job, and `pyacp-3rw.3` should reach for it.
`load_session` must replay history as `session/update` notifications; a session that fed
an accumulator with every notification it sent would have exactly that history, in a form
the SDK maintains. Recorded rather than built, because nothing emits notifications until
Phase 3 — and the experimental marker means that dependency is a decision, not a default.

## Lifetime

**Created** by `session/new` (or `fork`), **destroyed** by `session/close` or process
exit. There is deliberately **no idle expiry.**

This process is a subprocess of the client and its lifetime is the client's. A TTL would
reap a session the user simply left open, and the failure would present as data loss
rather than as a timeout — the client would get `-32602` for a `sessionId` it is holding
correctly.

The cost, stated honestly: a long-lived process accumulates sessions a client never
closes. `session/close` is registered `unstable=True` in the SDK's agent router, so a
client without `use_unstable_protocol` **cannot call it at all** and leaks until exit.
That is the leak [docs/acp-compliance-matrix.md](../../docs/acp-compliance-matrix.md)
warns about; it is bounded in practice by the process being short-lived, and
`close_all()` exists for shutdown.

`close()` removes the entry **before** running the close hook, so a hook that raises
cannot leave a half-closed session addressable — the worst outcome being a session a
client can still name after its backends were torn down.

## Fork copies, resume shares

The distinction is the whole reason both methods exist, and getting it wrong is silent.

| | `fork_session` | `resume_session` |
|---|---|---|
| session id | **new** | same |
| cwd, `additionalDirectories` | copied; the caller may override either | unchanged |
| mode and config option state | **deep-copied** | the same objects — there is only one session |
| history | copied at the fork point (`pyacp-3rw.3`) | shared; it *is* the session |
| MCP backends | **its own instances**, from the same `mcpServers` spec | the same instances |
| in-flight turn | not inherited; a fork starts idle | whatever the session had |

The deep copy is load-bearing. `modes` and `config_options` are pydantic models that
`set_mode` and `set_config_option` mutate **in place**, so a shallow copy would let a
fork's settings move its parent's with nothing on the wire to say so.
`test_a_fork_does_not_alias_its_parents_mode_state` and its config-option twin are the
guards.

Backends are the one row where cost argues the other way — forking re-spawns subprocesses
byte-identical to the parent's. **Correctness wins:** a shared backend would make
`session/close` on the fork tear down the parent's tools. A refcounted share is a valid
later optimisation *provided* closing one session cannot disturb another. `pyacp-3rw.3`
and `pyacp-db3` implement this; the semantics are fixed here so they are not rediscovered.

## Errors

`UnknownSessionError` subclasses **`ValueError`**, which is not incidental:
[errors.py](errors.md) maps a `ValueError` to `-32602 Invalid params` with the reason in
`data`, so a stale `sessionId` reaches the client correctly with no special case anywhere.

`-32602` is the honest code. `-32603` would blame us for the client's stale id, and
`-32601` would claim the method does not exist.

`TurnAlreadyRunningError` is a `RuntimeError` instead, so it maps to `-32603`: a second
`session/prompt` arriving while the first runs is a state we must never silently allow,
because two turns on one session would interleave their `session/update` notifications
with nothing on the wire to tell them apart. It refuses rather than queues — the client
decides whether to cancel and retry.

## What is *not* here

- **Path validation.** `pyacp-3rw.4` enforces the absolute-path constraint on `cwd` and
  `additionalDirectories`, at the edge where a bad value must become `-32602`. A registry
  that also validated would put the rule in two places and let them disagree.
- **MCP backends.** A session's backends are keyed by session id in `mcp_registry.py`.
  The registry is the only thing that knows when a session ends, so it takes an
  `on_close` hook — that is the seam, and the only coupling.
- **`stopReason`.** `cancel_turn()` delivers the cancellation; `pyacp-hnk.5` decides what
  the turn reports.
- **Pagination.** `list()` fixes the ordering — most recently active first — so a
  `pyacp-3rw.3` cursor means the same thing to every caller. The cursor itself is that
  bead's.

## Main symbols

| Symbol | Purpose |
|---|---|
| `Session` | One session: id, cwd, `additionalDirectories`, modes, config options, title, timestamps, in-flight turn |
| `Session.set_mode` / `set_config_option` | Validated mutation; both move `updated_at` |
| `Session.attach_turn` / `detach_turn` / `cancel_turn` | Where `session/cancel` reaches |
| `Session.fork` / `to_info` | The deep copy, and the `session/list` view |
| `SessionRegistry` | `create` / `get` / `fork` / `resume` / `list` / `close` / `close_all` |
| `UnknownSessionError`, `TurnAlreadyRunningError` | See **Errors** above |

`SessionRegistry` takes `clock` and `id_factory` so tests are deterministic rather than
time- and uuid-dependent, and `on_close` for the backend seam.

It is **not thread-safe and does not need to be**: one asyncio loop drives every
connection. It *is* shared across the WebSocket transport's connections, which is why
`close` is the only thing that removes an entry — a disconnecting client must not silently
delete sessions another connection may resume.

## Tests

`tests/test_sessions.py`. A `FakeClock` that only moves when a test says so, so ordering
assertions are exact rather than racing the wall clock.

## Related

- [agent.py docs](agent.md) — what will call this
- [errors.py docs](errors.md) — why `UnknownSessionError` is a `ValueError`
- [Module boundaries](../../docs/module-boundaries.md) — what this module must not own
