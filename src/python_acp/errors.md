# `errors.py` — one mapping, in one place

Translating our exception types into `acp.RequestError`. Three callers need the same
translation — `agent.py`, the WebSocket transport, and the turn executor when Phase 3
lands — and decision B7 in [docs/module-boundaries.md](../../docs/module-boundaries.md)
gives it a module rather than a home-of-convenience, because the alternative is how a
backend `-32601` and a bridge `-32601` became indistinguishable in the first place.

## The rule

**A message is a concise sentence; structured detail goes in `data`.**

That is not a house style — `acp.schema.Error` says it of `message` ("should be limited
to a concise single sentence"), and the SDK's `RequestError` constructors already work
that way: `invalid_params({...})` produces the message `"Invalid params"` and puts the
specifics in `data`. Following them means an error from the SDK-dispatched path and an
error from a hand-rolled transport read identically on the wire.

**One exception, and it is the reason the module exists: an error forwarded from the MCP
backend keeps the backend's code *and* its message.** The server already wrote a concise
sentence, and replacing it with ours would destroy the only account of what failed.

## `data.source` is a discriminator, not decoration

Forwarding a backend code makes the code space ambiguous — the same integer can now come
from us or from the MCP server. So:

- A **forwarded** error always carries `data["source"] == "mcp"`, plus `mcpCode`, plus
  `mcpData` when the server supplied a `data` member of its own.
- An error **we originate** never sets that key. Not "usually" — never. Claiming the
  backend produced a code it never sent is the exact fidelity loss this mapping exists
  to prevent, so a codeless backend failure (timeout, dead transport, malformed result)
  takes the generic `-32603` with no `source`.

That is what lets a client tell "this agent has no such method" from "the MCP server
behind it has no such method".

## The mapping

| Raised | Becomes | Message | `data` |
|---|---|---|---|
| `RequestError` | itself, unchanged | — | — |
| `MCPProtocolError` with a server code | that code | the server's | `{source, mcpCode, mcpData?}` |
| `MCPProtocolError` with no code | `-32603` | `"Internal error"` | `{reason}` |
| `ValueError` | `-32602` | `"Invalid params"` | `{reason}` |
| anything else | `-32603` | `"Internal error"` | `{reason}` |
| `asyncio.CancelledError` | **re-raised** | — | — |

An already-mapped `RequestError` passes through untouched. Re-wrapping would bury a
deliberate `-32000 auth_required` under a generic `-32603`.

### Not every `-32602` on the wire comes from here

This module never sees a request whose *params do not parse*. The SDK validates against
the schema before the agent method is called, and answers `-32602` itself with pydantic's
own report — `data.errors`, a list carrying `loc` and `type` — rather than the `{reason}`
this table describes. So a client sees two different `data` shapes under one code, and
which one it gets says which layer refused:

| `data` key | Refused by | Looks like |
|---|---|---|
| `errors` | the SDK's schema | `{"errors": [{"type": "missing", "loc": ["cwd"], ...}]}` |
| `reason` | a `ValueError` raised in this process | `{"reason": "cwd must be an absolute path, got 'rel'"}` |
| `source` | the MCP backend, forwarded | `{"source": "mcp", "mcpCode": -32601}` |

Never more than one of them. `tests/test_negative.py::test_data_names_the_layer_that_refused`
is the guard, and the rest of that file is where the whole mapping is asserted as one
coherent table rather than case by case.

## `@as_request_error` is load-bearing, not defensive

`acp.Connection._run_request` catches a non-`RequestError` from an agent method and
answers `RequestError.internal_error({"details": str(exc)})`. So an `MCPProtocolError`
that escapes an agent method reaches the client as a bare `-32603` with the backend's
code destroyed, and a `ValueError` arrives as `-32603` instead of `-32602`. **The
mapping has to happen on our side of that boundary.**

Every request-serving member of `PythonAcpAgent` therefore carries the decorator,
including the ones whose bodies are still `-32601`. The bodies arrive over three more
phases; a decorator already in place is a requirement a later phase cannot forget, and
it costs nothing on a method that only raises `RequestError`.

Two things it deliberately does not do:

- **Notification handlers are not decorated.** `cancel` and `ext_notification` have no
  reply channel, so raising at all is already the bug — there is nowhere to put a mapped
  error.
- **It lives on the function, so an override replaces it.** Later phases fill these
  bodies in *in place* rather than by subclassing;
  `tests/test_agent.py::test_every_request_method_maps_its_errors` is what holds that.

## Cancellation

`asyncio.CancelledError` is never mapped — `to_request_error` re-raises it. It is a
`BaseException` for a reason: returning a value from a cancelled coroutine tells asyncio
the cancellation did not take, and the task keeps running. A caller that needs to
*report* a cancellation calls `request_cancelled()` on the side that observed it, having
already let the exception propagate.

`REQUEST_CANCELLED = -32800` is an **unverified reading**, recorded here so the next
person does not have to redo it. `acp.schema.Error` accepts `-32800` alongside the six
JSON-RPC standard codes and ACP's two (`-32000` auth required, `-32002` resource not
found), but `RequestError` supplies no constructor for it and the schema carries no
description. `-32800` is LSP's `RequestCancelled`, and ACP's error shape mirrors LSP
elsewhere. `pyacp-tzd.5` owns request cancellation and should confirm against the ACP
docs before putting it on the wire.

## What this module does not cover

`mcp_stdio.py` also builds JSON-RPC error objects — for requests the MCP *server* sends
*us*. Those go out on the MCP wire, in the opposite direction, to a peer for whom
`source: "mcp"` would be nonsense. They belong to the `mcp-protocol` skill and are
deliberately not routed through here.

## Main symbols

| Symbol | Purpose |
|---|---|
| `to_request_error(exc)` | The mapping above |
| `as_request_error` | Decorator applying it to an agent method's exceptions |
| `to_error_object(error)` | Render the JSON-RPC `error` member for a transport that frames its own messages |
| `request_cancelled(reason=None)` | `-32800`, for whoever ends up reporting a cancellation |
| `REQUEST_CANCELLED`, `MCP_SOURCE` | The two constants worth naming |

`to_error_object` exists because `RequestError.to_error_obj()` always emits a `data` key,
`null` included. `data` is optional in JSON-RPC and its *presence* is meaningful here —
it is where `source` lives — so an error with nothing structured to say omits the key
rather than asserting `null`. The SDK-dispatched path does not use it; `acp.Connection`
renders its own envelopes.

## Tests

`tests/test_errors.py`. Two check the mapping against the SDK rather than against
itself: `test_every_code_we_can_produce_is_one_acp_recognises` reads the literals out of
`acp.schema.Error`, and `test_request_cancelled_uses_a_code_the_schema_accepts` pins
`-32800` to that same union. `tests/test_agent.py` owns the join — that a backend
failure raised inside an agent method reaches the client with its own code.
