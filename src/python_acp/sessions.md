# `sessions.py` — what the agent remembers between `session/new` and `session/close`

A session is the unit every ACP method after `initialize` is addressed to. This module
owns the record and the registry that holds them, and nothing else — no JSON-RPC shapes,
no MCP subprocesses, no prompt execution. [agent.py](agent.md) translates requests into
calls on `SessionRegistry`; `mcp_registry.py` (Phase 2.3) owns the backends a session's
turns use.

> **Wired.** Every `session/*` method except `set_mode` and `set_config_option`
> (`pyacp-fln.2`, `pyacp-fln.3`) runs against this registry.

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

## History

`Session.history` is an append-only list of every `session/update` the session emitted, in
order, written by `turns.TurnContext.emit` on the way out. `session/load` replays it.

**Not `acp.contrib.SessionAccumulator`**, which `pyacp-3rw.1` flagged as the candidate.
That helper *merges* updates into a snapshot — tool calls keyed by id, one plan, message
chunks bucketed by kind — which is what a UI wants and the opposite of what a replay
needs. Order across categories, and duplicate chunks, are exactly the information it
discards. A list is also simpler and not marked experimental.

The list is **unbounded**, deliberately. A cap would silently truncate the middle of a
transcript a client asked to reload, which is worse than the memory; a session's history
dies with the session.

A fork copies the transcript up to the fork point. A shallow copy is enough — updates are
never mutated after `record`, only appended — but it must be a *copy*, or the child's
next turn would append to the parent's transcript.

## Remembered permissions

`Session.remembered_permissions` maps a qualified tool name to the answer the user asked
to be remembered. `allow_always` and `reject_always` are the two ACP options that write
here, and **session** is their scope — the SDK's own default option is literally named
*"Approve for session"*.

It lives on the session rather than in the executor so it dies with the session instead
of outliving it in a process-wide map, and a fork **copies** it: a fork answering "always
allow" must not decide for its parent, exactly as with mode and config state.

This module does not interpret the values. [turn_mcp_router.py](turn_mcp_router.md) does.

## Pagination

`page(cwd, cursor, limit)` returns one page plus the cursor for the next, `None` when
done. The cursor is a **keyset**, not an offset: it names the last session on the page as
`(updated_at, session_id)`, and the next page is everything strictly after it in the same
ordering.

An offset would skip or repeat entries whenever a session was created or touched between
two calls — which, for a live registry, is most of the time. The keyset is not immune
either: a session that becomes active mid-walk sorts earlier and can be seen twice.
`session_id` is in the key so a client can dedupe, and a repeat is a far better failure
than a silent omission.

A cursor the registry did not issue raises `ValueError` → `-32602`. Silently restarting
from page one would loop a client forever without ever telling it why.

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

- **Path validation.** [paths.py](paths.md) owns the rules and [agent.py](agent.md)
  applies them, at the edge where a bad value must become `-32602`. A registry that also
  validated would put the rule in two places and let them disagree — so
  `SessionRegistry.create("relative/path")` is deliberately fine, and there is a test
  saying so. `Session.roots` is the declaration `paths.require_contained` checks against.
- **MCP backends.** A session's backends are keyed by session id in
  [mcp_registry.py](mcp_registry.md). The registry is the only thing that knows when a
  session ends, so it takes an `on_close` hook — that is the seam, and the only coupling.
  `cli.py` wires it (`SessionRegistry(on_close=backends.close)`); a deployment that
  forgot to would leak one subprocess per session.
- **`stopReason`.** `cancel_turn()` delivers the cancellation; `pyacp-hnk.5` decides what
  the turn reports. `cancel_turn` sets `Session.cancellation` **before** cancelling the
  task, which is what lets an executor tell `session/cancel` from the whole request dying
  — see [turns.py](turns.md). `attach_turn` replaces the event, so a new turn never starts
  already flagged by the previous turn's cancel.
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
