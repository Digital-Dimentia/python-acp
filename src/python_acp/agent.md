# `agent.py` — the ACP agent runtime

`PythonAcpAgent` is this project's implementation of `acp.interfaces.Agent`. It is the
protocol edge: it validates, delegates, and serializes, and owns nothing else. Session
state (`sessions.py`), turn execution (`turns.py`), and MCP calls (`mcp_stdio.py`) all
sit below it and arrive in later phases.

**Both transports bind this class.** [transport_stdio.py](transport_stdio.md)
(`pyacp-tzd.2`) and [transport_ws.py](transport_ws.md) (`pyacp-tzd.3`) each run it
through `acp.run_agent`, one agent instance per connection, so a client gets the same
answers whichever wire it arrived on. Session and prompt methods answer `-32601` until
Phases 2 and 3 fill the bodies in.

`transport_*` faces the ACP client; `mcp_*` faces the backend. Two stdio modules sit near
each other in this directory meaning opposite directions.

## Dispatch is not ours

`acp.agent.router.build_agent_router` maps JSON-RPC method names onto this class's
attributes, and `acp.connection` turns a returned model into a result and an
`acp.RequestError` into an error object. Nothing here parses a request id, builds an
error envelope, or knows a transport exists.

Three mechanics of that arrangement are load-bearing:

| Mechanic | Consequence for this module |
|---|---|
| The router splats the request's `_meta` keys in as kwargs (`acp/router.py:104-107`) | **Every method takes `**kwargs`.** A closed signature raises `TypeError` the first time a client attaches metadata. |
| Every agent route is registered `optional=False` | A member this class does not define is already `-32601`. Declining a method means *omitting* it — never a hand-built error. |
| `session/close`, `session/fork`, `session/resume` are registered `unstable=True` | With `use_unstable_protocol` off, the router raises `method_not_found` **without calling us**. The connection must be built with the flag or those three are dead code. |

That last one is why `pyacp-tzd.2` and `pyacp-tzd.3` must pass
`use_unstable_protocol=True` to `acp.run_agent`. It is a protocol-visible choice, not a
detail of Phase 2.

## Method surface

All 15 `acp.interfaces.Agent` members are present. Nothing is declined — see
[docs/acp-compliance-matrix.md](../../docs/acp-compliance-matrix.md) for why, per
member.

| Member | Wire method | State today | Filled in by |
|---|---|---|---|
| `initialize` | `initialize` | **live** — negotiates the version, stores `clientCapabilities`, returns the capability block from [capabilities.py](capabilities.md) | — |
| `authenticate` | `authenticate` | **live** — refuses with `-32000 auth_required` | `pyacp-fln.1` |
| `cancel` | `session/cancel` | **live** — logs and returns; a notification must never raise | `pyacp-3rw.2`, `pyacp-hnk.5` |
| `ext_notification` | `_<name>` | **live** — silent by contract | `pyacp-sld.2` |
| `on_connect` | — | **live** — stores the `Client` facade | — |
| `ext_method` | `_<name>` | `-32601` | `pyacp-sld.2` |
| `new_session` | `session/new` | `-32601` | `pyacp-3rw.2`, `pyacp-db3` |
| `prompt` | `session/prompt` | `-32601` | `pyacp-3rw.2`, `pyacp-hnk.2` |
| `load_session` | `session/load` | `-32601` | `pyacp-3rw.3` |
| `list_sessions` | `session/list` | `-32601` | `pyacp-3rw.3` |
| `close_session` | `session/close` | `-32601` *(unstable-gated)* | `pyacp-3rw.3` |
| `fork_session` | `session/fork` | `-32601` *(unstable-gated)* | `pyacp-3rw.3` |
| `resume_session` | `session/resume` | `-32601` *(unstable-gated)* | `pyacp-3rw.3` |
| `set_session_mode` | `session/set_mode` | `-32601` | `pyacp-fln.2` |
| `set_config_option` | `session/set_config_option` | `-32601` | `pyacp-fln.3` |

`_not_implemented` returns exactly what the router produces for an absent attribute, so
a later phase changes a body and nothing else; the wire behaviour before it does is
already correct.

**Every request member carries `@as_request_error`**, including the ones whose bodies
are still `-32601`. That is not defensive: `acp.Connection._run_request` catches a
non-`RequestError` and answers a bare `-32603`, so an `MCPProtocolError` escaping one of
these methods would arrive at the client with the backend's code destroyed, and a
`ValueError` would arrive as `-32603` instead of `-32602`. The mapping has to happen on
our side of that boundary, and putting it there now is what keeps a later phase from
having to remember. The decorator lives on the function, so it is replaced by an
override — these bodies get filled in *in place*. See [errors.py](errors.md).

`cancel` and `ext_notification` are **not** decorated. A notification has no reply
channel, so there is nowhere to put a mapped error and raising at all is already the
bug.

**`authenticate` is a refusal, not a stub.** `initialize` advertises no auth methods, so
every `methodId` is one we never offered. `-32000 auth_required` says "the method exists,
the credentials do not"; `-32601` would say the opposite.

## `initialize` does three things

**Negotiates the version.** `capabilities.negotiate_protocol_version` echoes a version
we serve and answers with our newest when the client asked for one we do not. This is
not a rejection point — the client reads the answer and decides whether to disconnect —
so an unsupported version is logged, not raised.

**Stores `clientCapabilities`.** Whatever the client declared is kept for the life of
the connection and read back through `PythonAcpAgent.client_capabilities`. Phase 4 gates
every `fs/*`, `terminal/*`, and `elicitation/*` call on it; a call made without checking
is a conformance bug the client is entitled to answer `-32601` to. `None` means the
handshake has not happened and is deliberately not collapsed with a `ClientCapabilities`
that declares nothing.

*Per-connection* means per instance: **one `PythonAcpAgent` serves one connection.**
`on_connect` stores that connection's `Client` facade on the same object, so the two
have the same lifetime by construction. A transport that binds one agent to several
connections would break both, which is why `cli.py` constructs the agent at the point it
binds it.

**Returns the capability block.** Not assembled here —
[capabilities.py](capabilities.md) owns it, so a literal cannot be flipped on without a
manifest row and a test proving the feature it advertises actually runs. Everything is
`false`/`null` today because Phase 1 implements no features; the owner of each flip is
in that module's table.

`PROTOCOL_VERSION` is the **ACP** version, an integer. It is unrelated to
`MCPStdioClient`'s MCP `protocolVersion` string. Two protocols, two version fields.

## Main symbols

| Symbol | Purpose |
|---|---|
| `PythonAcpAgent` | The `acp.interfaces.Agent` implementation |
| `PythonAcpAgent.client` | The connected `Client` facade; raises `RuntimeError` before `on_connect` |
| `PythonAcpAgent.client_capabilities` | What the client declared at `initialize`, or `None` before it ran — Phase 4 gates every client call on this |

`client_capabilities` distinguishes `None` (no `initialize` yet) from "declared nothing";
the two are deliberately not collapsed.

## Wire-shape gotcha

`mcpServers` is **required** on `session/new` and `session/load` — `NewSessionRequest`
and `LoadSessionRequest` give it no default, even though the `Agent` Protocol's signature
does. The router always passes it, so the Python-side default never applies.

## Tests

`tests/test_agent.py` drives the agent through `build_agent_router` rather than calling
its methods directly. The contract under test is "the SDK can dispatch to this object",
and a signature the router cannot splat into is exactly the failure a direct call would
hide. The unstable-gated methods are tested in **both** directions — reachable with the
flag, `-32601` without it — because the second is what catches a connection built wrong.
