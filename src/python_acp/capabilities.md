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
| `promptCapabilities.image` / `.audio` / `.embeddedContext` | `false` | `pyacp-hnk.3` |
| `sessionCapabilities.additionalDirectories` | **`{}`** | `pyacp-3rw.4` |
| `mcpCapabilities.http` / `.sse` / `.acp` | `false` | never — transports we do not drive |
| `sessionCapabilities.delete` | `null` | never — no route, no `Agent` member in 0.12.1 |
| `auth.logout` | `null` | never — nothing to log out of, and `logout` is unrouted |
| `providers`, `nes`, `positionEncoding` | `null` | never — UNSTABLE and unrouted |

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

## `AUTH_METHODS`

An empty tuple, and not provisionally. This process runs locally as a subprocess of the
client, under the user's own credentials, and authenticates nobody. Empty is the accurate
statement — and it is what makes `Agent.authenticate` a typed `-32000 auth_required`
refusal rather than a `-32601`. Advertising a method would turn that refusal into a lie.

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
| `Capability` | One leaf of the advertised block: path, value, owner, why |
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
