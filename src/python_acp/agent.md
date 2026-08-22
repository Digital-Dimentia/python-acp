# `agent.py` — the ACP agent runtime

`PythonAcpAgent` is this project's implementation of `acp.interfaces.Agent`. It is the
protocol edge: it validates, delegates, and serializes, and owns nothing else. Session
state (`sessions.py`), turn execution (`turns.py`), and MCP calls (`mcp_stdio.py`) all
sit below it and arrive in later phases.

> **Not yet reachable.** No transport binds this class yet — `transport_stdio.py`
> (`pyacp-tzd.2`) and `transport_ws.py` (`pyacp-tzd.3`) do that. The live WebSocket path
> is still `ws_bridge.py`. This module exists so later phases fill in method bodies
> rather than restructure.

`transport_*` faces the ACP client; `mcp_*` faces the backend. Two stdio modules will
eventually sit near each other in this directory meaning opposite directions.

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
| `initialize` | `initialize` | **live** — negotiates the version, stores `clientCapabilities`, returns the capability block | refined by `pyacp-tzd.4` |
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

**`authenticate` is a refusal, not a stub.** `initialize` advertises no auth methods, so
every `methodId` is one we never offered. `-32000 auth_required` says "the method exists,
the credentials do not"; `-32601` would say the opposite.

## The capability block is a promise

`initialize` returns `AgentCapabilities` with every feature flag false or null, because
Phase 1 implements no features. **A literal flips in the same commit as the feature it
advertises, never ahead of it.** Each one is owned by a row of the compliance matrix:

- `loadSession` → `pyacp-3rw.3`
- `promptCapabilities.image` / `.audio` / `.embeddedContext` → `pyacp-hnk.3`
- `mcpCapabilities.http` / `.sse` / `.acp` → stay false; these gate the *transport* of a
  client-supplied MCP server, and stdio (which needs no flag) is the only one we drive
- `sessionCapabilities.list` → `pyacp-3rw.3`; `.additionalDirectories` → `pyacp-3rw.4`;
  `.delete` stays null (the SDK routes no `session/delete`)
- `authMethods` stays `[]` — this process runs locally under the user's own credentials

`PROTOCOL_VERSION` comes from the SDK. It is the **ACP** version and is unrelated to
`MCPStdioClient`'s MCP `protocolVersion` string. Two protocols, two version fields.

Version negotiation answers with `PROTOCOL_VERSION` whatever the client asked for, and
logs when they differ. That is the spec's shape: the agent states what it supports and
the *client* decides whether to disconnect.

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
