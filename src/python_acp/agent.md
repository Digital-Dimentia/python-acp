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

## One agent per connection, one registry per process

`PythonAcpAgent` takes its `SessionRegistry` rather than making one, and the argument is
**required** on purpose. The WebSocket transport builds an agent per socket; a
per-agent registry would mean a client could not resume a session it created on a
connection that has since dropped, and `session/resume` would be meaningless. A default
would hide that. `cli.py` constructs the one registry and hands it to whichever transport
is bound.

The [`McpBackendRegistry`](mcp_registry.md) and [`TerminalRegistry`](terminals.md) are
process-wide for the same reason and are taken the same way: both are keyed by session id,
and both are torn down by the session registry's `on_close` hook rather than by anything
here. `cli.py` is the only place that constructs all three, which is why it is the only
place that can wire them together.

The `Client` facade goes the other way — `on_connect` stores *the* connection's, so it
must not be shared. `connected_client` exposes it without raising, for the one caller that
runs *after* a connection ends: `transport_ws.py` hands it to
`TerminalRegistry.forget_client` when a socket closes, where "there was never a client" is
an ordinary answer rather than the bug `client` treats it as.

## Running a turn

`session/prompt` runs the executor as its **own task**. That is what makes
`session/cancel` — a notification arriving on the same connection while the request is
still open — have something to reach; running the turn inline would leave the cancel with
nothing to cancel.

The wait is `asyncio.wait({turn})`, not `await turn`, because the two cancellations must
not be confused:

| What happened | `await turn` | `asyncio.wait({turn})` |
|---|---|---|
| `session/cancel` cancelled the turn | raises `CancelledError` here | returns; `turn.cancelled()` is `True` |
| *this request* was cancelled | raises `CancelledError` here | raises `CancelledError` |

`await turn` makes the two indistinguishable, and answering `stopReason: "cancelled"` for
a request that was itself cancelled would put a reply on a wire nobody is reading.
`wait` only raises when we are cancelled, so `turn.cancelled()` afterwards is an
unambiguous answer to "did `session/cancel` reach it".

`detach_turn` runs in a `finally`, so a cancelled session accepts the next prompt.
`TurnAlreadyRunningError` from `attach_turn` cancels the task it just created rather than
leaving one un-awaited.

The executor defaults to [`McpToolRouterExecutor`](turn_mcp_router.md) over this agent's
backend registry — decision D3's shipped default, deterministic and with no LLM in it.
`turns.IdleTurnExecutor` is still available for a caller that wants a turn to do nothing.

`detach_turn` runs in a `finally`, so a cancelled session accepts the next prompt, and
the executor's `TurnResult` supplies both `stopReason` and `usage` — `PromptResponse`
carries the second and nothing was filling it.

The client's declared capabilities are handed to the `TurnContext`, because gating is
per-connection and an executor must not have to reach back through the agent for it. The
seam itself is [turns.py](turns.md), and `turns.STOP_REASON_DISPOSITIONS` is the whole
`stopReason` contract — which three this agent returns, and why the two limit conditions
are not among them.

**The response is built only after the turn task is done.** That is what makes "no
`session/update` after the response" a property of the code rather than a rule executors
are asked to follow: an executor emitting from an `except CancelledError` cleanup block
is still inside the task, so its notification is on the wire before the answer is.

That covers every path that *has* a response. The one that does not is this request itself
being cancelled: `prompt` cancels the turn task and re-raises **without awaiting it**,
because awaiting a task inside a dead request is how a hang gets made if an executor
ignores cancellation. `context.detach()` in the `finally` closes that window — `emit`
raises `DetachedTurnError` from then on, so a shielded cleanup cannot write to a socket
nobody is reading. `pyacp-48b`; the reasoning is in [turns.py](turns.md) under "Emitting
on the way out".

`prompt` reports the executor's `stopReason` rather than deciding it, with **one
override**: if `session/cancel` was delivered to this turn and the executor still returned
something other than `cancelled`, the response says `cancelled` and a warning names the
executor. Answering `end_turn` for a turn the client explicitly stopped would be a lie,
and the flag is per turn — `attach_turn` installs a fresh event — so a previous turn's
cancellation cannot trigger it. Route two to `cancelled` needs no such rescue: a client
that answers a permission request with `DeniedOutcome` makes the executor *return*
`cancelled` with nothing cancelled anywhere.

## Load reconstitutes, resume reattaches

Both take a session id and both return the same settings, so the difference is easy to
collapse and expensive to get wrong. It is the **replay**:

| | `session/load` | `session/resume` |
|---|---|---|
| Sends the transcript again | **yes**, before the response | no |
| For a client that | lost its copy — restarted, reconnected | still has it and is picking the session back up |

Replaying on resume would duplicate every message the client already rendered.

The replay goes out **before** the response, which is the ordering the spec asks for: a
client that received the result first would have no way to tell replayed updates from
live ones on a session that is already running.

**`loadSession: true` claims the method works, not that a session outlives the agent.**
Nothing here persists across a restart, so `session/load` succeeds for a session this
process still holds and answers `-32602` for anything else.

`resume_session` receives `cwd` and `mcpServers` and applies neither. Changing either
mid-session would silently invalidate paths and tool names the transcript already refers
to; a client that wants different ones wants a fork.

## The unstable three are advertised per connection

`session/close`, `/fork`, and `/resume` are registered `unstable=True` in the SDK's
router, which answers `method_not_found` for them **without calling the agent** when the
connection lacks `use_unstable_protocol`. Advertising them there would be a promise the
SDK itself refuses to keep, so `PythonAcpAgent(unstable=...)` mirrors the connection's
flag and `capabilities.build_agent_capabilities(unstable=...)` withholds those three rows
when it is off. Both transports pass `True`.

## Every routed method has a body

`pyacp-fln.3` was the last. Nothing in this class answers `method_not_found` any more —
the only `-32601` a client can get is the router's, for a name the SDK does not route at
all, or `ext_method`'s for an unknown extension. `_not_implemented` was deleted with its
last caller.

## Mode and config changes go out through one door

`announce_mode` is the only thing that emits `current_mode_update`, and
`set_session_mode` calls it rather than emitting inline. The notification goes out even
though the client is the one who asked, for two reasons: a second client attached to the
same session needs it to stay in step, and an internally-originated change — a future
executor deciding it must drop out of `auto-approve`, say — should be indistinguishable
on the wire from a client-driven one. Nothing internal changes a mode today; the door
exists so that when something does, it does not invent a second way to say so.

`announce_config_options` is its twin, for the same reasons.

Modes and config options both come from the **executor** (`TurnExecutor.session_modes`,
`session_config_options`), the only thing that can act on either — the same arrangement as
`promptCapabilities`. Each session gets a deep copy, because both `set_mode` and
`set_config_option` mutate in place.

**`set_config_option` is one implementation for two request shapes.** The SDK
discriminates `SetSessionConfigOptionBooleanRequest` from its select twin on `type` and
splats either into the same parameters, so the only difference that arrives is what
`value` holds — and `Session.set_config_option` is what knows which of the two the named
option can take. Two methods would mean writing that check twice.

## Paths are validated here and nowhere else

`cwd` and `additionalDirectories` must be absolute — `-32602` otherwise — and are stored
lexically tidied. Every method that carries them validates them: `session/new`,
`session/fork`, and also `session/resume` and `session/load`, which receive a `cwd` they
deliberately do **not** apply. Accepting a relative path there and silently ignoring it
would tell a client its path was fine when it was both invalid and unused.

Validation runs *before* `SessionRegistry.create`, so a refused request leaves nothing
behind.

The containment rule those roots define — and why it resolves symlinks on both sides —
is [paths.py](paths.md). Phase 4.2's `fs/*` calls are its first consumer.

## `session/new` refuses what `initialize` did not advertise

`mcpCapabilities.http`, `.sse`, and `.acp` are all `false` in
[capabilities.py](capabilities.md), and stdio needs no capability at all. Accepting an
`HttpMcpServer` anyway would make the advertisement a lie and hand back a session whose
tools silently do not exist, so a well-formed entry of an unadvertised transport is a
`-32602`. Spawning the stdio ones is `pyacp-db3`'s; refusing the rest could not wait for
it, because the wrong answer is silent.

The stdio entries that survive are handed to [mcp_registry.py](mcp_registry.md), which
spawns and handshakes one subprocess per server. **Opening is all-or-nothing and takes
the session with it**: if any server fails to come up, the session created a line earlier
is closed before the error propagates, because handing back an id whose tools silently do
not exist is the failure this whole path avoids.

**One hazard is inherited and cannot be refused here.**
`NewSessionRequest.mcp_servers` carries a `skip_invalid_items` wrap validator, so an entry
that fails validation — a stdio server missing the required `env`, say — is silently
dropped from the list before the agent sees it. The client gets a session whose server is
simply absent, with no error. Pinned by
`test_a_malformed_mcp_server_is_dropped_before_we_see_it`.

## Method surface

All 15 `acp.interfaces.Agent` members are present. Nothing is declined — see
[docs/acp-compliance-matrix.md](../../docs/acp-compliance-matrix.md) for why, per
member.

| Member | Wire method | State today | Filled in by |
|---|---|---|---|
| `initialize` | `initialize` | **live** — negotiates the version, stores `clientCapabilities`, returns the capability block from [capabilities.py](capabilities.md) | — |
| `authenticate` | `authenticate` | **live** — refuses with `-32000 auth_required` | `pyacp-fln.1` |
| `cancel` | `session/cancel` | **live** — cancels the session's running turn; silent for an unknown session and for an idle one | `pyacp-hnk.5` |
| `ext_notification` | `_<name>` | **live** — silent by contract | `pyacp-sld.2` |
| `on_connect` | — | **live** — stores the `Client` facade | — |
| `ext_method` | `_<name>` | `-32601` | `pyacp-sld.2` |
| `new_session` | `session/new` | **live** — registers a session with the executor's modes, opens its MCP servers, rejects the transports `initialize` did not advertise | — |
| `prompt` | `session/prompt` | **live** — runs a turn as a task and returns its `stopReason` | `pyacp-hnk.2` |
| `load_session` | `session/load` | **live** — replays the session's transcript, then returns its settings | — |
| `list_sessions` | `session/list` | **live** — one keyset-paginated page, most recently active first | — |
| `close_session` | `session/close` | **live** *(unstable-gated)* — cancels the turn, drops the session, releases its backends and its client terminals | — |
| `fork_session` | `session/fork` | **live** *(unstable-gated)* — deep copy under a new id, with its own MCP subprocesses | — |
| `resume_session` | `session/resume` | **live** *(unstable-gated)* — reattaches; deliberately does **not** replay | — |
| `set_session_mode` | `session/set_mode` | **live** — switches the mode and emits `current_mode_update` | — |
| `set_config_option` | `session/set_config_option` | **live** — one implementation for both request shapes; emits `config_option_update` | — |

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
| `PythonAcpAgent.connected_client` | The same facade or `None` — the non-raising form, for cleanup after a connection ends |
| `PythonAcpAgent.terminals` | The process-wide [`TerminalRegistry`](terminals.md) this agent's turns create through |
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
