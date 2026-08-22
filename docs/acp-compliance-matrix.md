# ACP v1 Compliance Matrix

**Status:** ratified for Phase 0. No code changes in this document.
**Bead:** `pyacp-4ns.2` (Phase 0.2 of [docs/full-apc-plan.md](full-apc-plan.md)).
**Consumers:** `pyacp-tzd.1` (Agent skeleton), `pyacp-tzd.4` (initialize negotiation),
`pyacp-6ni.1` (conformance suite). Also constrains `pyacp-3rw.*`, `pyacp-fln.*`,
`pyacp-8bv.*`.

This is the contract. Every row states a **disposition** and *why*. The `initialize`
capability block in `pyacp-tzd.4` must be derivable from this document with nothing
aspirational in it, and the Phase 8 conformance suite tests against these dispositions —
including the declines.

## How this was derived

Read directly out of the pinned SDK, `agent-client-protocol==0.12.1`, as installed in
`.venv` — not from the published schema and not from the GitHub source. Sources:

| Fact | Read from |
|---|---|
| `Agent` protocol members (15) | `acp/interfaces.py:161` |
| `Client` protocol members (14) | `acp/interfaces.py:83` |
| Wire method names | `acp/meta.py` (`AGENT_METHODS`, `CLIENT_METHODS`); generated from `schema/meta.json` at `refs/tags/schema-v1.19.0` |
| Which members the SDK actually routes | `acp/agent/router.py:build_agent_router` — 11 requests + 1 notification, confirmed by inspecting the built router |
| Unstable gating | `acp/router.py:59` (`Route.handle`), `acp/agent/connection.py:86` |
| Capability shapes | `acp/schema.py`: `AgentCapabilities:5505`, `ClientCapabilities:4977` |
| The client facade we call | `acp/agent/connection.py:109-252` |

`PROTOCOL_VERSION = 1` (`acp/meta.py`).

### Three SDK mechanics that decide what a disposition can mean

These are not style choices — they are what the router does, and every disposition below
is expressed in their terms.

**1. Declining is done by omission.** `build_agent_router` registers every route with
`optional=False`, so `Route.handle` raises `RequestError.method_not_found` when the
attribute is absent (`acp/router.py:64`). `Agent` is a `Protocol`, not an ABC, so a method
we do not define is simply not there. **A decline is therefore "do not write the method"**
— never a hand-built `-32601`, and never a stub that raises.

**2. Three lifecycle methods are gated behind `use_unstable_protocol`.** `session/close`,
`session/fork`, and `session/resume` are registered with `unstable=True`. With the flag
off — the default on `run_agent` and `AgentSideConnection` — the route exists but
`Route.handle` emits a `UserWarning` and raises `method_not_found` **without ever calling
our method** (`acp/router.py:65-71`). Implementing them is not enough; the connection must
be constructed with `use_unstable_protocol=True` or they are dead code.

**3. Not every name in `AGENT_METHODS` is routed.** `AGENT_METHODS` carries 28 names;
`build_agent_router` registers **11 requests and 1 notification** — 16 names go nowhere.
Unrouted: `session/delete`,
`logout`, `providers/*`, `nes/*`, `document/*`, `mcp/message`. Nothing in `acp.interfaces.Agent`
corresponds to them. Reaching one means an `ext_method` handler or a hand-added route — see
[Consequences](#consequences-for-later-phases).

## Agent surface — what we implement

All 15 members of `acp.interfaces.Agent`. "Wire method" is the JSON-RPC method the SDK
routes to that member; `—` means the member is not a wire method.

| # | `Agent` member | Wire method | Disposition | Lands in | Why |
|---|---|---|---|---|---|
| 1 | `initialize` | `initialize` | **implement** | `pyacp-tzd.4` | Mandatory. Negotiates `protocolVersion` and returns `AgentCapabilities` + auth methods. Every other row's gate is set here. |
| 2 | `new_session` | `session/new` | **implement** | `pyacp-3rw.2`, `pyacp-db3` | Mandatory. The only way a client gets a session id; without it `session/prompt` is unreachable. Its `mcp_servers` argument is `pyacp-db3`'s — accept `McpServerStdio`, reject the transports we do not advertise. |
| 3 | `prompt` | `session/prompt` | **implement** | `pyacp-3rw.2`, `pyacp-hnk.2` | The point of the runtime. Served by the `TurnExecutor` (D3); default is the deterministic MCP tool-router. |
| 4 | `cancel` | `session/cancel` | **implement** | `pyacp-3rw.2`, `pyacp-hnk.5` | Notification, not a request. Mandatory companion to `prompt`: the in-flight turn must end with `stopReason: "cancelled"`. |
| 5 | `load_session` | `session/load` | **implement** | `pyacp-3rw.3` | Gated by `agentCapabilities.loadSession`, which we set `true` only once this lands. Replays history as `session/update` notifications before returning. |
| 6 | `list_sessions` | `session/list` | **implement** | `pyacp-3rw.3` | Gated by `agentCapabilities.sessionCapabilities.list`. Cheap given the Phase 2 registry (`pyacp-3rw.1`) — it is a read over state we already hold. Cursor pagination supported; a single page is a conforming answer. |
| 7 | `close_session` | `session/close` | **implement, unstable-gated** | `pyacp-3rw.3` | Registered `unstable=True`. Requires `use_unstable_protocol=True` on the connection or it is unreachable. Without it, sessions leak for the process lifetime. |
| 8 | `fork_session` | `session/fork` | **implement, unstable-gated** | `pyacp-3rw.3` | Same `unstable=True` gate. **Absent from the original plan** — it exists only because the matrix was read off the SDK. Semantics: copy session state under a new id; the fork must not alias the parent's mutable state. |
| 9 | `resume_session` | `session/resume` | **implement, unstable-gated** | `pyacp-3rw.3` | Same `unstable=True` gate. Distinct from `load_session`: resume continues a session we still hold, load reconstitutes one from persistence. |
| 10 | `set_session_mode` | `session/set_mode` | **implement** | `pyacp-fln.2` | Modes are advertised in `NewSessionResponse`; advertising them and then refusing to switch is incoherent. Must emit `current_mode_update`. |
| 11 | `set_config_option` | `session/set_config_option` | **implement** | `pyacp-fln.3` | Two request models (boolean, select) discriminated on `type` by `_validate_set_config_option_request`. Both variants or neither — a half-implementation fails validation on the other shape. Must emit `config_option_update`. |
| 12 | `authenticate` | `authenticate` | **implement as an explicit refusal** | `pyacp-fln.1` | The method exists and returns an error for any `methodId`, because `initialize` advertises **no** auth methods (see below). Defining it keeps the refusal a *typed auth error* rather than `-32601`, which is the honest answer to "authenticate with a method I never offered". |
| 13 | `ext_method` | `_<name>` | **implement** | `pyacp-sld.2` | Not an ACP method: the router strips a leading `_` and forwards (`acp/router.py:174-178`). This is where the legacy MCP passthrough (`tools/list`, `tools/call`, …) lives during the D4 deprecation window. |
| 14 | `ext_notification` | `_<name>` | **implement** | `pyacp-sld.2` | Same mechanism, notification side. Must stay silent on unknown names — the router returns `None` when the handler is absent, and we must not turn an unknown extension into an error. |
| 15 | `on_connect` | — | **implement** | `pyacp-tzd.1` | Not a wire method. The SDK hands us the `Client` facade here; it is the *only* way to obtain the handle every row in the next table is called through. Storing it is mandatory. |

**Nothing is declined.** All 15 are implemented — a consequence of the SDK giving us dispatch
for free: the cost of a method is its semantics, not its plumbing. Three carry the unstable
flag and one (`authenticate`) implements a refusal rather than a capability.

### Consequences for the `initialize` capability block

`pyacp-tzd.4` builds `AgentCapabilities` from this table and nothing else. Each literal
below is owned by the row that justifies it, and flips **in the same commit** as that row —
never ahead of it.

> **Flipped by `pyacp-3rw.3`.** `loadSession`, `sessionCapabilities.list`, `.fork`,
> `.resume`, and `.close` are now advertised. The "Value at Phase 1" column is kept as
> written — it is the record of what was ratified, not a description of the live block.
> `src/python_acp/capabilities.py` is the live block, and its current state is tabulated
> in [capabilities.md](../src/python_acp/capabilities.md).
>
> One correction to the `.fork` / `.resume` / `.close` rows: they are advertised **per
> connection**, withheld when `use_unstable_protocol` is off, because the SDK's router
> refuses those three without calling the agent. A single process-wide literal could not
> express that.

| Field | Value at Phase 1 | Flips to | When |
|---|---|---|---|
| `loadSession` | `false` | `true` | `pyacp-3rw.3` (row 5) ✔ |
| `promptCapabilities.image` | `false` | per `pyacp-hnk.3` | Only when `ImageContentBlock` is genuinely handled |
| `promptCapabilities.audio` | `false` | per `pyacp-hnk.3` | Only when `AudioContentBlock` is genuinely handled |
| `promptCapabilities.embeddedContext` | `false` | per `pyacp-hnk.3` | Only when `EmbeddedResourceContentBlock` is genuinely handled |
| `mcpCapabilities.http` | `false` | — | These three gate the *transport* of a client-supplied MCP server, not the ability to accept one. `McpServerStdio` needs no capability, and stdio is the only MCP transport we drive (`mcp_stdio.py`, D6). `pyacp-db3` accepts `mcpServers` on `session/new` and must **reject `HttpMcpServer` entries** while these read `false`. |
| `mcpCapabilities.sse` | `false` | — | Same, for `SseMcpServer`. |
| `mcpCapabilities.acp` | `false` | — | Same, for `AcpMcpServer`, and marked UNSTABLE in the schema. |
| `sessionCapabilities.list` | `null` | `{}` | `pyacp-3rw.3` (row 6) |
| `sessionCapabilities.delete` | `null` | — | `session/delete` has **no route and no `Agent` member** in 0.12.1. Advertising it would promise a method the SDK cannot dispatch. |
| `sessionCapabilities.additionalDirectories` | `null` | `{}` | `pyacp-3rw.4`, which is what enforces the absolute-path constraint on them |
| `sessionCapabilities.fork` | `null` | `{}` | `pyacp-3rw.3` (row 8), **and** only while the connection carries `use_unstable_protocol` — with the flag off the router answers `-32601` and the advertisement would be a lie |
| `sessionCapabilities.resume` | `null` | `{}` | `pyacp-3rw.3` (row 9), same unstable condition |
| `sessionCapabilities.close` | `null` | `{}` | `pyacp-3rw.3` (row 7), same unstable condition |
| `auth.logout` | `null` | — | No auth methods are offered, so there is nothing to log out of. Also unrouted — see below. |
| `providers`, `nes`, `positionEncoding` | `null` | — | UNSTABLE in the schema and unrouted by the SDK. Out of scope for v1. |
| `authMethods` (on `InitializeResponse`) | `[]` | — | The bridge authenticates nobody; it runs as a local subprocess under the user's own credentials. An empty list is the accurate statement, and it is what makes row 12 a refusal. |

> **Amended by `pyacp-tzd.4`.** The `sessionCapabilities.fork` / `.resume` / `.close`
> rows were absent from the table as ratified — `acp.schema.SessionCapabilities` in
> 0.12.1 carries six fields, not three. They were found by walking the SDK model while
> compiling this table into `src/python_acp/capabilities.py`, and
> `tests/test_capabilities.py::test_the_manifest_covers_every_field_the_sdk_defines`
> now walks it on every run, so the next field the SDK adds fails a test instead of
> being advertised at whatever the SDK defaults it to.

**Where this table lives in code.** `src/python_acp/capabilities.py` is this table as
`AGENT_CAPABILITY_MANIFEST` — one `Capability` row per leaf, each carrying its value,
its owner, and its reason — and `initialize` is built from it and nothing else. Turning
a capability on takes four things in one commit: the feature, a test that exercises it,
the changed row, and an entry in `CAPABILITY_EVIDENCE` naming that test. Leave any one
out and the suite fails. See [../src/python_acp/capabilities.md](../src/python_acp/capabilities.md).

## Client surface — what we consume

All 14 members of `acp.interfaces.Client`. We **never implement these**; we call them on the
facade handed to `on_connect`. "Gate" is the `clientCapabilities` field from `initialize`
that must be checked before the call.

| # | `Client` member | Wire method | Gate (`clientCapabilities.…`) | Disposition | Lands in |
|---|---|---|---|---|---|
| 1 | `session_update` | `session/update` | **none — always available** | **call** | `pyacp-3rw.2`, `pyacp-hnk.4` |
| 2 | `request_permission` | `session/request_permission` | **none — always available** | **call** | `pyacp-8bv.1` |
| 3 | `read_text_file` | `fs/read_text_file` | `fs.readTextFile` | **call, gated** | `pyacp-8bv.2` |
| 4 | `write_text_file` | `fs/write_text_file` | `fs.writeTextFile` | **call, gated** | `pyacp-8bv.2` |
| 5 | `create_terminal` | `terminal/create` | `terminal` | **call, gated** | `pyacp-8bv.3` |
| 6 | `terminal_output` | `terminal/output` | `terminal` | **call, gated** | `pyacp-8bv.3` |
| 7 | `release_terminal` | `terminal/release` | `terminal` | **call, gated** | `pyacp-8bv.3` |
| 8 | `wait_for_terminal_exit` | `terminal/wait_for_exit` | `terminal` | **call, gated** | `pyacp-8bv.3` |
| 9 | `kill_terminal` | `terminal/kill` | `terminal` | **call, gated** | `pyacp-8bv.3` |
| 10 | `create_elicitation` | `elicitation/create` | `elicitation` (**UNSTABLE**) | **call, gated** | `pyacp-8bv.4` |
| 11 | `complete_elicitation` | `elicitation/complete` | `elicitation` (**UNSTABLE**) | **call, gated** | `pyacp-8bv.4` |
| 12 | `ext_method` | `_<name>` | none | **do not call** | — |
| 13 | `ext_notification` | `_<name>` | none | **do not call** | — |
| 14 | `on_connect` | — | none | **not ours** | — |

Notes that the table cannot carry:

- **`session_update` and `request_permission` have no capability gate.** `ClientCapabilities`
  has no field for either (`acp/schema.py:4977`): `fs`, `terminal`, `session`, `plan`, `auth`,
  `elicitation`, `nes`, `positionEncodings`. Every ACP client must accept both. Do not invent
  a gate for them, and do not make streaming conditional.
- **`terminal` is one boolean for all five `terminal/*` methods.** There is no per-method
  granularity. Check it once and treat the family as all-or-nothing.
- **`fs` has two independent booleans.** Read may be permitted while write is not. Check the
  one belonging to the operation, never `fs` as a whole.
- **The `plan` gate belongs to `session_update`, not to a method.** `clientCapabilities.plan`
  governs whether the client accepts `agent_plan_*` *update variants*. `pyacp-hnk.4` must
  suppress those variants rather than skip the `session_update` call.
- **Elicitation is UNSTABLE in the schema.** `pyacp-8bv.4` is P3 for that reason; it may be
  removed by an SDK bump, and nothing else may depend on it.
- **Ungated calls are a conformance bug, not a runtime error.** A client that never advertised
  `terminal` is entitled to answer `-32601`; the failure surfaces as a broken turn, so
  `pyacp-6ni.3` tests the gate, not the recovery.

## Consequences for later phases

Three findings that change work already planned. Each is a fact about 0.12.1, re-checkable by
reading the file named.

1. **`logout` is unroutable.** `pyacp-fln.1` says "implement authenticate and logout flows".
   `AGENT_METHODS["logout"] = "logout"` exists and `AgentAuthCapabilities.logout` exists, but
   `build_agent_router` registers no `logout` route and `acp.interfaces.Agent` has no `logout`
   member. It is reachable only via `ext_method` or a hand-added route. Since we advertise no
   auth methods (row 12), the resolution is to **drop logout from scope** and leave
   `auth.logout` null — but `pyacp-fln.1` currently promises otherwise and needs amending.
2. **`use_unstable_protocol` is a Phase 1 decision, not a Phase 2 one.** Rows 7–9 are dead
   code unless `pyacp-tzd.1` constructs the connection with the flag set. That is a
   *protocol-visible* choice — the flag changes what `session/close` returns to a client — so
   it belongs with the connection, and `pyacp-3rw.3` must not discover it late.
3. **`$/cancel_request` still has no home.** `pyacp-tzd.5` targets it; confirmed again here
   that no `$/cancel_request` route exists anywhere in `meta.py`, `router.py`, or
   `agent/router.py` at 0.12.1. ACP cancellation is `session/cancel` (row 4). Either
   `pyacp-tzd.5` means the JSON-RPC-level cancel and must build it on `ext_method`, or the
   requirement is a misreading of the spec and the bead should be closed against row 4.
   Unresolved here; flagged for `pyacp-tzd.5`.

Two smaller corrections to [full-apc-plan.md](full-apc-plan.md), applied in the same commit
as this file:

- "**16 methods**, enumerated below" under *Substrate* — the enumeration lists 15, and
  `acp.interfaces.Agent` has 15 members. Corrected to 15.
- "The surface we consume (`Client`)" lists 11 members; the Protocol has 14. The three
  omitted (`ext_method`, `ext_notification`, `on_connect`) are exactly the three that are not
  wire methods, which is why they were missed — but `on_connect` is how the facade is obtained
  and belongs in the list.

## What Phase 8 tests

`pyacp-6ni.1` derives its cases from this document mechanically:

- Every **implement** row: the method is routed and answers a well-formed request.
- Every **unstable-gated** row: answers when the connection has the flag, and returns
  `-32601` when it does not. Both directions — the second is the regression that catches a
  connection built without the flag.
- Every **capability literal** in the `initialize` table: the advertised value matches what
  the runtime actually does. A `true` with no implementation behind it is the failure this
  matrix exists to prevent.
- Every **gated client call**: exercised with the capability absent, asserting the call is
  not made.
- `session/delete`, `logout`, `providers/*`, `nes/*`, `document/*`: return `-32601`.
  Unrouted is the correct behaviour, and the test pins it so a future SDK bump that starts
  routing them is noticed rather than silently accepted.
