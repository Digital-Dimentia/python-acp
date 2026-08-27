# `announcer.py` — saying something *after* the response

[Source](announcer.py)

One job: emit `available_commands_update` for `session/new` and `session/fork`, on the
far side of the response that carries the new session's id.

## Why this is not two lines inside `new_session`

The client learns a minted session id from the **response**. Every way of sending a
notification from inside the handler puts it on the wire *before* that response:

| Attempt | What happens |
| --- | --- |
| `await client.session_update(...)` in the handler | Sent first. Names a session the client has never seen; a correct client drops it. |
| `asyncio.create_task(...)` in the handler | Races the reply. The task can reach the transport while the handler is still running, before the response is enqueued at all, so the order is undefined — worse than reliably wrong. |
| A field on `NewSessionResponse` | ACP has none. The response carries `sessionId`, `modes`, `configOptions` and `_meta`, and `AvailableCommand` is only ever delivered by the `available_commands_update` notification. |

What does exist is a hook on the far side of the write. `acp.Connection._run_request`
is three lines:

```python
payload = await self._execute_request(message)
await self._transport.send(payload)
self._notify_observers(StreamDirection.OUTGOING, payload)
```

An observer therefore runs **strictly after** the response bytes are gone, and a
notification it sends names an id the client already holds. Observers are public API —
`Connection.add_observer`, or `observers=[...]` through `run_agent`'s
`**connection_kwargs`. Verified on the wire, in that order:

```
RESPONSE id=2         {"sessionId": "11fe7e…", "modes": {…}}
NOTIFY session/update {"sessionId": "11fe7e…", "update": {"sessionUpdate": "available_commands_update", …}}
```

## The observer must not await anything before it sends

Firing after the response is necessary but not sufficient, and `pyacp-svt` is what the
difference costs: `tests/test_interop.py` failed on Python 3.11 four runs in five and
passed every time on 3.14, with the palette landing *behind* the first turn's
`user_message_chunk`.

An observer is a **task**. A client is free to pipeline — the SDK's own client sends
`session/prompt` the instant `session/new` returns — and if the observer awaits real I/O
before its notification reaches the transport, that request gets read and answered in the
meantime. Building the palette is a `tools/list` per backend, so it awaited exactly that.

Two facts make the fixed version deterministic rather than merely faster:

- **Wire order is enqueue order.** `acp.task.MessageSender` puts every outgoing payload
  on an `asyncio.Queue` that one loop drains, and `Queue.put` on an unbounded queue does
  not suspend. So whichever coroutine enqueues first is written first, full stop.
- **The observer's task is created ahead of the pipelined request's.** `_notify_observers`
  runs in `_run_request`'s continuation, which is already on the ready queue when the loop
  next polls for I/O; the request that poll reads cannot create its task until afterwards.

So an observer whose **first await is the send** wins by construction. That is why
`agent._prepare_commands` builds the list during `session/new`, and why the observer is
handed `announce_prepared_commands` rather than `announce_commands` — see
[agent.md](agent.md).

## Why it is not in `agent.py`

[agent.py](agent.md) opens by saying that nothing in it parses a request id, builds an
error envelope, or knows a transport exists. Matching raw JSON-RPC frames is the first
two of those. So the observer lives here and takes the `announce` **callable** —
`PythonAcpAgent.announce_prepared_commands` — rather than the agent, which leaves this
module depending on nothing in the project and testable with a list and a stub.

## Matching a response to its request

A JSON-RPC response carries no method, only the id it answers. The observer watches both
directions: it records the id of an incoming `session/new` or `session/fork`, and fires
when an outgoing message answers it.

**Outgoing is not the same as response.** The agent originates requests too —
`session/request_permission`, `fs/read_text_file` — and the SDK numbers those from its
*own* counter, independent of the client's. A client's `session/new` with id `2` and an
agent's `session/request_permission` with id `2` are both routine and both outgoing. The
discriminator is that a response has no `method`, and that is the check the code makes.
Getting it wrong would announce against whatever `result` happened to be in the request.

The pending-id table is per **connection**, closed over by the returned observer, because
ids are only unique within one connection: two sockets both numbering from `1` would
collide in a shared table.

## What it does not do

- **It does not decide the list.** The agent does, from the executor's
  `available_commands` — here by way of the list `session/new` stashed, elsewhere by
  building one. Both doors end in the same `_send_commands`, so there is one write and one
  place a listing failure is handled.
- **It never fails a request.** By construction it *cannot*: it runs after the response
  was written, so there is nothing left to fail. Exceptions are logged and swallowed
  rather than left to the SDK's observer error path, whose only move is a traceback. A
  socket closed between the response and the announcement is the ordinary case, not a bug.
- **It announces nothing for a refused session.** An error payload has no `result` and so
  no `sessionId`; the pending id is dropped either way, so a failed `session/new` leaves
  no entry behind.

## Both transports wire it

[transport_ws.py](transport_ws.md) passes it unconditionally;
[transport_stdio.py](transport_stdio.md) passes it behind a `getattr`, because `run_stdio`
is typed to the SDK's `Agent` interface and an embedder's agent need not have
`announce_prepared_commands`. They must not diverge — the same client asking the same
question over the two transports has to get the same answer, which is the rule
[transport_ws.md](transport_ws.md) already states about `use_unstable_protocol`.

## Related

- `pyacp-obt` — the `session/load` / `session/resume` half, shipped first.
- `pyacp-p8v` — this.
- `pyacp-svt` — the ordering race this fired into, and the `_prepare_commands` fix.
- `pyacp-mth` — the extension request, which this makes unnecessary for a palette that
  shows only name and description, and which is still the route if a client ever wants
  per-server grouping or MCP input schemas that `AvailableCommand` cannot carry.
