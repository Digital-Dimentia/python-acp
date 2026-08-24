# `elicitation.py` — passing a question to the only human in the system

An MCP server we launched wants to ask a person something. There is no person on this
side of the process; the only one anywhere is on the far side of the ACP connection. So
this module does not answer — it forwards, and hands the answer back.

```
MCP server ──elicitation/create──▶ python-acp ──elicitation/create──▶ ACP client
           ◀──── {action, content} ──────────  ◀──── accept | decline | cancel | …
```

Both hops are called `elicitation/create`. That is two specs agreeing, not one method:
the left arrow is [mcp_stdio.py](mcp_stdio.md)'s wire and the right is the SDK's `Client`
facade. Nothing about the two shapes is guaranteed to stay aligned.

## Why this module exists at all

`MCPClientCapabilities.elicitation` and the `2025-06-18` protocol pin were both put in
place by `pyacp-pb7`, which deliberately stopped short of declaring the capability:
**declaring one and answering it are a single change**, and a server told it may elicit,
with nothing to answer, strands itself on a `-32601` it was promised would not happen.

This module is the second half. [mcp_registry.py](mcp_registry.md) now declares
`elicitation` to a backend exactly when it is handed a forwarder from here, and
[agent.py](agent.md) builds one exactly when the connected client can be asked. One
decision, in one place, with the promise and the thing that keeps it inseparable.

## MCP asks a form, so ACP is asked a form

MCP's `elicitation/create` has one shape — `{message, requestedSchema}` — and it is always
a form. ACP's is a union of four:

| ACP mode | Carries an MCP question? |
|---|---|
| `ElicitationFormSessionMode` | **yes** — form, because that is what MCP sends; session-scoped, because a backend belongs to a session and its id is the thing we have |
| `ElicitationFormRequestMode` | no — request-scoped, and the MCP request id is not an ACP one |
| `ElicitationUrlSessionMode` | no — nothing here has an out-of-band flow to send a user to |
| `ElicitationUrlRequestMode` | no — same |

`tool_call_id` is left unset. It is optional, and attaching the question to the tool call
that provoked it would need the id to reach the read loop the handler runs on — worth
doing, tracked separately, and not something to fake in the meantime.

## `complete_elicitation` is declined, not deferred

`Client.complete_elicitation` announces that an elicitation has finished, and it is
addressed by `elicitationId`. That field exists **only** on the two URL variants. A form
elicitation has no id, so there is nothing to name; and nothing in this runtime creates a
URL elicitation, so there is nothing to complete.

That is structural, not unfinished: it would take a source of URL-mode elicitations to
change it, and an MCP `requestedSchema` cannot become one.
[docs/acp-compliance-matrix.md](../../docs/acp-compliance-matrix.md) carries the row.

## Three answers that are not the client's

Something must always be said: an MCP server that sent a request is blocked until it is
answered.

| Situation | Reply | Why not an error |
|---|---|---|
| Nobody is connected | `cancel` | A session outlives the connection that made it, so a backend may ask after its client has gone. "The prompt went away without a choice" is literally what happened |
| The client has no `elicitation.form` | `cancel` | Only reachable across a reconnect — see below. A client lacking a capability is conforming, not broken |
| The client answered with an action ACP added later | `cancel` | MCP has exactly three actions. An extension has no MCP spelling, and "no explicit choice" is the truthful reduction |

`cancel` rather than `decline` in all three: `decline` says a human refused, and there was
no human.

**A client that raises is left alone.** The exception reaches
`mcp_stdio._handle_server_request`, which answers the server `-32603`. Something really
did break, and flattening that into `cancel` would report a dismissal nobody performed.

**A server that sends bad params gets `-32602`.** `MalformedServerRequest` is the third
answer a handler needs, added in `mcp_stdio.py` for this: not a result, not "we never
offered this", and not our fault either.

## The window this does not close

The MCP capability block is a promise made once, when the backend is spawned. The ACP
client's capabilities are a fact about **one connection**. A session created by a
form-capable client, disconnected, and resumed by a client without elicitation leaves a
backend holding a promise the current connection cannot keep.

It is answered with `cancel` and a warning rather than pretended away, and it cannot be
closed from this side: MCP offers no way to withdraw a declared capability short of
restarting the subprocess. The same shape as the terminal that cannot be released after a
disconnect ([terminals.md](terminals.md)), recorded for the same reason.

## What is not translated

`requestedSchema` goes through the SDK's `ElicitationSchema` because
`Client.create_elicitation` takes a model and offers no raw path. That parse **is** the
validation, and it is faithful: titles, `minLength`/`maximum`, `enum`, `oneOf`, defaults,
and unknown property types (which land in the catch-all with their extra keys intact) all
survive. The SDK serialises with `exclude_defaults=True`, so `"type": "object"` — the one
value the ACP schema fixes — does not reach the wire.

`content` on an accepted answer is **not** checked against that schema. The client
rendered the form and the server declared it; a bridge with a third opinion would only be
a new way to be wrong. It is passed through as it arrived, and omitted when the client
sent none.

## Main symbols

| Symbol | What it is |
|---|---|
| `forwarder(session_id, connected)` | Builds the handler for one session's `elicitation/create` requests |
| `ConnectedClient` | The live `Client` facade and its `ClientGates`, looked up per elicitation rather than captured once |
| `Connected` | `Callable[[], ConnectedClient \| None]` — how the forwarder finds someone to ask |
| `Forwarder` | `Callable[[dict], Awaitable[dict]]` — MCP params in, MCP result out |
| `MCP_ELICITATION_CREATE` | The MCP method name this module answers |

## Related

- [mcp_stdio.py](mcp_stdio.md) — the wire the question arrives on, and why the handler
  runs in a task of its own
- [mcp_registry.py](mcp_registry.md) — what a backend is promised, and by whom
- [turns.py](turns.md) — `Gate.ELICITATION_FORM`, and why the outer `elicitation` object
  is not a gate
- [agent.md](agent.md) — where the per-session forwarder is built
