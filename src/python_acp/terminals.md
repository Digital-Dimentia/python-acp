# `terminals.py` — terminals this agent asked a client to run

`terminal/create`, `/output`, `/wait_for_exit`, `/kill`, and `/release` are
`acp.interfaces.Client` methods. An ACP agent **calls** them; it never serves them. So a
"terminal" in this codebase is not a process this runtime owns — it is a handle to a
process running on the *client's* machine, and the only things this module can do with one
are remember it and ask for it back.

That asymmetry is the whole reason the module exists. `terminal/create` answers with an id
and nothing else; the client holds the process, its output buffer, and its file
descriptors until `terminal/release` arrives. **An agent that forgets an id has leaked a
process it can no longer name, and nothing anywhere raises.** No timeout fires, no error
surfaces, and the only symptom is a machine that gets slower.

## The shape is `mcp_registry.py`'s, deliberately

| Piece | Here | There |
|---|---|---|
| Tracking | per session, then per terminal id | per session, then per server name |
| `close(session_id)` | releases every terminal the session holds | stops every subprocess |
| Wired to | `SessionRegistry.on_close`, in `cli.py` | `SessionRegistry.on_close`, in `cli.py` |
| `close_all()` | process shutdown, **not** a disconnect | process shutdown, **not** a disconnect |
| Ordering | the entry is removed **before** the release is sent | the entry is removed **before** the stop |

That last row is the one worth stating: a teardown that raises must not leave a session
addressable with a resource that is already gone. `Terminal.release` drops itself from the
registry and only then asks the client, so a client that answers `-32603` to
`terminal/release` costs us a terminal — not a dangling handle as well.

`SessionRegistry` takes **one** `on_close` hook and two registries need it, so `cli.py`
composes them into a single function. Terminals go first: they are requests over a live
connection with a client waiting on `session/close`, while MCP teardown is local
subprocess work nobody is watching.

One thing is genuinely different from the MCP registry, and it is why `Terminal` carries
its own `Client`. An MCP backend is a subprocess *we* spawned; a terminal belongs to
whichever connection created it. `session/close` may arrive on a **different** connection
than the turn that opened the terminal — sessions are process-wide and survive a
disconnect — so the release has to go back to the client that actually has it.

## Disconnect releases nothing, and this is the honest description

The acceptance criterion this module was written against asked for "every created terminal
released on ... disconnect". **That cannot be done, and implementing something weaker
while leaving the claim standing would be the bug.**

The terminal lives in the client. Once its connection is gone there is nobody to send
`terminal/release` to: the request has no transport, and the process it names is on a
machine we are no longer talking to. Sessions deliberately *survive* a disconnect — a
disconnecting client must not silently delete sessions another connection may resume, see
[sessions.md](sessions.md) — so the session's entry stays and only the terminals belonging
to the departed client go.

So `forget_client` **drops tracking and releases nothing**:

- It frees the memory a dead handle would otherwise hold for the life of the process,
  which matters for a long-lived server accumulating one handle set per connection.
- The terminals themselves are **the departed client's to reap**. A client that killed our
  connection still owns the processes it started for us, and reaping them is its job
  because it is the only party that can.
- A dropped handle is marked spent, so a stray `release()` on it later sends nothing down
  a connection that no longer exists.

`transport_ws.py` calls it when a socket closes. Under `--transport stdio` there is no
separate disconnect event: the client going away *is* the process ending, and the shutdown
path (`sessions.close_all()`, which fires the hook) covers it.

### The window this *did* not close, and now mostly does

A turn cancelled *during* `terminal/create` — after the client started the process but
before its answer reached us — used to leave a terminal whose id we never learned: a
process running on someone's machine that this side could neither release nor even name.
This document said no amount of care could change that, on the reasoning that the id
existed only in a reply that was never delivered.

**That was too strong.** The reply was not undelivered; it was delivered to a future
nobody was waiting on any more, because our own cancellation dropped it. `pyacp-9hd`
closed the gap with `asyncio.shield`: the request survives the turn's cancellation, so
the id still arrives, and `_abandon_unclaimed` kills and releases the terminal exactly as
`_capture` does for a command cancelled a moment later. It is tracked before it is
abandoned, so a concurrent `session/close` can still see it if anything below fails.

What genuinely cannot be closed is narrower: a reply that never arrives *at all*, which is
the connection dying underneath us. There the id really did exist only in a message that
does not exist, and the wait is bounded (`_UNCLAIMED_CREATE_TIMEOUT`, 5s) so a cancellation
cannot hang on it. That case logs a warning naming the command, which is the whole of what
can honestly be done.

`tests/test_terminals.py` covers all three: the reply that lands late and is released, the
reply that never lands and does not hang, and the ordinary create the shield must not
disturb. The first of those fails without the shield — checked, rather than assumed.

## `output_byte_limit` is always set

`CreateTerminalRequest.outputByteLimit` is optional in the schema and **not optional
here**. `TerminalRegistry.create` defaults it and refuses `None` outright, because
unbounded output is a failure mode with no error message attached: a runaway command
produces bytes the client buffers until something dies.

`DEFAULT_OUTPUT_BYTE_LIMIT = 1 MiB`, and the number comes from where the bytes end up
rather than from taste:

- Captured output becomes an **MCP tool argument**. It is JSON-escaped into a
  `tools/call` request that has to fit `MCPStdioClient`'s 8 MiB stream limit
  ([mcp_stdio.md](mcp_stdio.md)), and escaping can multiply a byte several times over —
  a control character becomes six. 1 MiB leaves that headroom with the rest of the
  request to spare.
- It is far more than any sane command's output, so the limit is not a constraint a
  reasonable prompt will meet by accident.
- The client truncates from the **beginning** (the schema says so), which keeps the tail:
  where a failing command puts its error and a finishing one puts its result.

A caller may name another limit — `turn_mcp_router.py` exposes it as `outputByteLimit` on
a `run` spec, spelled exactly as the schema field it sets — but never *no* limit. When
truncation happened, the tool call's content says so, because an argument silently missing
its first 90 bytes is worse than one that admits it.

## Gating

`clientCapabilities.terminal` is **one boolean for all five methods**. No per-method
granularity exists in the schema, so there is nothing finer to check and inventing one
would be inventing protocol.

Every call here starts with `require(Gate.TERMINAL)`, and that is an assertion of *our*
invariant rather than the ordinary capability question. The ordinary question — "does this
client do terminals at all?" — is asked at parse time with `allows`, and a prompt asking
for a command from a client that never advertised one is **refused** before anything runs.
Routing that case through `require` would answer `-32603`, telling a client we were broken
when the truth is that it cannot do what it asked for. See [turns.md](turns.md) and
[turn_mcp_router.md](turn_mcp_router.md).

`require` cannot actually fail for a terminal that exists — `create` required the same gate
and `ClientGates` is frozen for the life of a connection — which is exactly what makes it
an assertion worth keeping.

## What raises and what does not

| Call | On failure |
|---|---|
| `create` | **raises.** The command never started, the caller has an argument it cannot fill, and it needs to know |
| `wait_for_exit`, `output`, `kill` | **raise.** The caller decides what a half-run command means |
| `release` | **never raises.** Logged and swallowed |
| `abandon` | **never raises.** The kill is best-effort; the release is the part that matters |

`release` swallowing is a decision, not laziness. A release failure is not information a
caller can act on — the terminal is the client's and it has just refused to take it back —
and propagating would replace a tool call's real result with a cleanup error. It is also
what makes `release` safe in a `finally` inside a task that is being cancelled: an already
released handle returns without awaiting anything, so there is no suspension point for the
cancellation to land on.

## Where the leak paths are tested

`tests/test_terminals.py` covers the lifetime rather than the plumbing, against
`TerminalClient` — a client running **real subprocesses**, because "did the kill land" is
only worth asking about something that was really running.

| Path | Test |
|---|---|
| Turn cancelled mid-command | `test_turn_mcp_router.py::test_cancelling_a_turn_mid_command_kills_and_releases_the_terminal` |
| Turn cancelled mid-**create**, reply lands late | `test_a_terminal_created_after_the_cancel_is_still_given_back` |
| Turn cancelled mid-create, reply never lands | `test_a_create_that_never_answers_does_not_hang_the_cancellation` |
| The shield leaves an ordinary create alone | `test_an_ordinary_create_is_unaffected_by_the_shield` |
| Session closed holding a live terminal | `test_closing_a_session_releases_the_terminal_a_turn_left_running` |
| Client disconnects | `test_a_disconnect_drops_tracking_and_releases_nothing` |
| A release that raises | `test_a_release_that_raises_still_drops_tracking` |
| Process shutdown | `test_close_all_releases_every_session` |
| Every method gated | `test_every_terminal_method_is_gated` |

`tests/test_interop.py` proves the wire from the other side: the interop client serves all
five methods with real subprocesses, and is the only vantage point from which
`outputByteLimit` can be seen to have actually been encoded rather than merely intended.
