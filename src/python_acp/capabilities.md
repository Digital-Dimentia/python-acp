# `capabilities.py` — the compliance matrix, compiled

[docs/acp-compliance-matrix.md](../../docs/acp-compliance-matrix.md) decides what
`initialize` is allowed to advertise. This module is that decision in executable form,
and `agent.py` calls it rather than assembling a block of its own.

Nothing here knows about a transport, a request, or a connection. It is a table and two
functions.

## Why a manifest and not a literal block

The capability block is a **promise**. A `true` in it entitles a client to call
something; if the feature is not there, the client's turn breaks and the fault is ours.
`AGENT_CAPABILITY_MANIFEST` exists because three ways of getting that wrong are cheap
with a hand-written block and impossible with a table:

| Failure | What the manifest does about it |
|---|---|
| **Aspirational literal** — flipping `true` in the same keystroke as the wish | `tests/test_capabilities.py::test_every_advertised_capability_names_a_feature_test` refuses any advertised capability that does not name an existing test proving the feature runs |
| **Silent SDK drift** — `AgentCapabilities()` already defaults to Phase 1's values, so building from defaults looks right today and changes meaning the day the SDK changes a default | Every field is stated explicitly, and `test_the_manifest_covers_every_field_the_sdk_defines` walks the SDK model and fails on a field no row covers |
| **Losing the why** — a `False` with no owner is indistinguishable from an oversight | Every row carries `owner` (the bead that flips it) and `why`, both asserted non-empty |

## The manifest

`Capability` is one leaf of the block:

| Field | Meaning |
|---|---|
| `path` | Attribute path into `AgentCapabilities` in the SDK's **Python** spelling — `("prompt_capabilities", "image")`, not `promptCapabilities.image`. The SDK's aliases handle the wire. |
| `advertised` | The literal. `True`/`False` for a flag; `None` or a marker-model instance for a sub-capability, which the schema advertises by **presence**, not by a boolean. |
| `owner` | The bead that flips it, or `"never"`. |
| `why` | Why it reads the way it does today. |

`Capability.is_advertised` is the promise test: `False` and `None` both mean "not
offered", and anything else owes the suite a proof.

### Current state

| Path | Value | Owner |
|---|---|---|
| `loadSession` | **`true`** | `pyacp-3rw.3` |
| `sessionCapabilities.list` | **`{}`** | `pyacp-3rw.3` |
| `sessionCapabilities.fork` / `.resume` / `.close` | **`{}`**, *only on an unstable connection* | `pyacp-3rw.3` |
| `promptCapabilities.image` / `.audio` / `.embeddedContext` | `false` — **derived** | `pyacp-hnk.3` |
| `sessionCapabilities.additionalDirectories` | **`{}`** | `pyacp-3rw.4` |
| `mcpCapabilities.http` / `.sse` / `.acp` | `false` | never — transports we do not drive |
| `sessionCapabilities.delete` | `null` | never — no route, no `Agent` member in 0.12.1 |
| `auth.logout` | `null` | never — nothing to log out of, and `logout` is unrouted |
| `providers`, `nes`, `positionEncoding` | `null` | never — UNSTABLE and unrouted |

### The prompt capabilities are derived, not written

`build_agent_capabilities(prompt_blocks=...)` takes the turn executor's
`supported_prompt_blocks`, and those three rows come from it.

**What a content block means depends on the executor**, which decision D3 makes
swappable. A literal fixed in this table would be a promise about a component the table
cannot see — and the moment an LLM-backed executor is dropped in, a hand-written `false`
would be wrong with nothing to catch it. So the row records the value *when the block is
read*, and the build derives whether it is.

The binding runs both ways: an executor that starts reading images flips the literal by
declaring it, and one that declares it without reading them fails
`test_the_shipped_executor_advertises_exactly_what_it_reads`.

The shipped [`McpToolRouterExecutor`](turn_mcp_router.md) reads `text` only, so all three
read `false`. That is not a gap waiting to be filled — see that module for why an image
has no defensible mapping to an MCP tool call.

**`resource_link` is governed by nothing.** `PromptCapabilities` has three fields and the
prompt union has four non-text block types, so a client may send a resource link whatever
`initialize` says. Declining it needs a stated reason rather than an advertisement, which
is why it lives in `turn_mcp_router.DECLINED_BLOCKS` and not here.

### The unstable gate

`build_agent_capabilities(unstable=...)` takes the connection's `use_unstable_protocol`,
and the three rows marked `requires_unstable` are withheld when it is off.

That is not caution. `session/close`, `/fork`, and `/resume` are registered
`unstable=True` in the SDK's agent router, which answers `method_not_found` for them
**without calling the agent** on a connection without the flag — so advertising them
there would be a promise the SDK itself refuses to keep. It is per-connection because the
flag is. Both transports pass `True`.

`mcpCapabilities.*` are the three worth reading twice: they gate the **transport** of a
client-supplied MCP server, not the ability to accept one at all. `McpServerStdio` needs
no capability, and stdio is the only MCP transport this bridge drives (D6), so all three
stay `false` and `session/new` must *reject* `HttpMcpServer` and `SseMcpServer` entries
while they do.

`sessionCapabilities.fork`, `.resume`, and `.close` are **not in the matrix document's
original table** — they were read off `acp.schema.SessionCapabilities` in 0.12.1 while
building this module, which is exactly the drift the coverage test now catches. The
matrix has been amended to match.

## Flipping a capability on

Four things, in one commit. Any three without the fourth fails the suite:

1. Build the feature.
2. Write a test that exercises it.
3. Change the row's `advertised` value.
4. Add `path -> "module:test_name"` to `CAPABILITY_EVIDENCE` in
   `tests/test_capabilities.py`.

(`test_phase_1_advertises_nothing` existed to assert the block was entirely off and died
with the first flip, as intended.)

## `AUTH_METHODS` — authentication is declined, and why that is checkable

An empty tuple, **decided rather than deferred** (`pyacp-fln.1`). The bead's question was
whether this runtime has a credential in the picture at all. It does not:

1. Decision D1 puts **no LLM** here, so there is no model-provider key.
2. Every MCP backend is an `McpServerStdio` **spawned locally by this process**, inheriting
   the user's own environment. A subprocess of the user does not authenticate to the user.
3. The three *remote* MCP transports are advertised `false` and refused outright at
   `session/new` — so the bead's "if there IS something (a remote MCP backend over
   http/sse)" branch is closed by a decision already made and enforced.

Empty is therefore the accurate statement, and it is what makes `Agent.authenticate` a
typed `-32000 auth_required` refusal rather than a `-32601`. Advertising a method would
turn that refusal into a lie.

**The third premise is the one that can rot**, so it is bound rather than trusted:
`test_declining_authentication_holds_only_while_no_remote_mcp_transport_does` asserts the
two together. Flipping `mcpCapabilities.http`, `.sse`, or `.acp` fails until this decision
is revisited — because a remote backend is exactly the credential `AUTH_METHODS` would
have to carry.

`auth.logout` is `null` for a reason of its own on top: it would advertise an exit from a
state no client can enter. And `logout` is **unrouted** in the SDK at 0.12.1 — no route,
no `Agent` member — so even with an auth method the SDK could not dispatch it.
`tests/test_conformance.py` pins that, so an SDK bump that starts routing it is noticed.

## Version negotiation

`SUPPORTED_PROTOCOL_VERSIONS` is a **set**, not a scalar, because negotiation is a
membership test and the day there are two versions the shape of the answer should not
have to change. It holds exactly `acp.PROTOCOL_VERSION` today.

`negotiate_protocol_version(requested)` echoes a version we serve, and answers with our
newest when we do not serve the one asked for. **The handshake is not a rejection
point**: an agent that errors on an unknown version turns a recoverable mismatch into a
fatal one. The client reads our answer and decides whether to disconnect.

`PROTOCOL_VERSION` here is the **ACP** version, an integer. It is unrelated to the MCP
`protocolVersion` *string* in [mcp_stdio.py](mcp_stdio.md). Two protocols, two version
fields, and they are not interchangeable.

## Main symbols

| Symbol | Purpose |
|---|---|
| `Capability` | One leaf: path, value, owner, why, and the two conditions that can take the value away — `requires_unstable` and `prompt_block` |
| `Capability.value_for(unstable=, prompt_blocks=)` | What the row puts on the wire for one connection and executor |
| `AGENT_CAPABILITY_MANIFEST` | Every leaf, in one tuple — the source `initialize` is built from |
| `build_agent_capabilities()` | Assembles `AgentCapabilities` from the manifest; a fresh object per call, so no connection can reach another's |
| `AUTH_METHODS` | The auth methods offered at `initialize` — empty |
| `SUPPORTED_PROTOCOL_VERSIONS` | Every ACP version this agent serves |
| `negotiate_protocol_version()` | The handshake answer |

## Tests

`tests/test_capabilities.py`. Three of its tests are structural rather than behavioural
and are the reason the module exists — see the table at the top. The rest pin the
per-row values, the freshness of each built block, and both directions of version
negotiation.

`tests/test_agent.py` owns the *join*: `initialize`'s response must equal
`build_agent_capabilities()`, so a block assembled beside the manifest cannot reach the
wire.
