# Plan: Full ACP v1 Runtime

Turn `python-acp` from an MCP-passthrough WebSocket bridge into a strict ACP v1 agent
runtime, built on the `agent-client-protocol` Python SDK, reachable over both stdio (the
transport real ACP clients use) and WebSocket (the existing local-automation surface).

> **Revision note.** This plan was rewritten on 2026-08-22 after diffing the original
> against the repository and the `agent-client-protocol` SDK. Four things changed: the
> documentation phase is already complete, the substrate decision is settled and it
> collapses most of the original Phase 1, the original Phase 4 was backwards, and the
> prompt-turn semantics gap is now addressed explicitly. See **What changed** at the end.

## Decisions

| # | Decision | Consequence |
|---|---|---|
| D1 | Build on the **`agent-client-protocol` Python SDK** (PyPI `agent-client-protocol`, GitHub `agentclientprotocol/python-sdk`) | Schema models, JSON-RPC plumbing, and both transports come from the SDK. Adds a `pydantic>=2.7` runtime dependency. |
| D2 | **Both transports, stdio-first** | stdio is primary so Zed/Neovim/any ACP client can connect; WebSocket is retained as a second binding. Core runtime is transport-agnostic. |
| D3 | **Pluggable `TurnExecutor`, deterministic default** | `session/prompt` is served by a swappable executor. The shipped default is a deterministic MCP tool-router — no LLM, consistent with the project's stated architecture. An LLM-backed executor can be added later without reopening the design. |
| D4 | **Deprecate the legacy `action` surface, then remove** | The `{"action": ...}` API keeps working with a deprecation warning through the migration, and is removed in a final cleanup once JSON-RPC has full parity. |
| D5 | Runtime surface is **JSON-RPC only** at the end state | Terminal state of D4. |
| D6 | MCP remains a **backend adapter**, not a protocol boundary | `mcp_stdio.py` serves the turn executor; it is not on the client-facing wire. |

## Substrate: what the SDK gives us

Adopting the SDK (D1) is the single largest change to this plan, because it eliminates
most of what the original Phase 1 described as work to be done.

`src/acp/` provides:

- **`acp.schema`** — generated Pydantic models tracking every ACP release (~200 KB of
  generated types). This *is* the compliance matrix; there is nothing to freeze by hand.
- **`acp.interfaces.Agent`** — the Protocol we implement. 15 members, enumerated below.
- **`acp.interfaces.Client`** — the Protocol the *client* implements. We **call** these;
  we do not implement them.
- **`acp.stdio`**, **`acp.ws.server`** — both transports we need, already written.
- **`acp.agent.connection` / `acp.agent.router`** — JSON-RPC dispatch, request/response
  correlation, error mapping.
- **`acp.helpers`** — builders for content blocks, tool calls, and session updates.
- **`acp.contrib`** — `session_state` (session accumulator), `tool_calls` (tool-call
  tracker), `permissions` (permission broker).

The work is therefore **implementing `acp.interfaces.Agent` against our MCP backend**,
not building a JSON-RPC server.

### The surface we implement (`Agent`)

`initialize`, `new_session`, `load_session`, `list_sessions`, `fork_session`,
`resume_session`, `close_session`, `set_session_mode`, `set_config_option`,
`authenticate`, `prompt`, `cancel`, `ext_method`, `ext_notification`, `on_connect`.

Note the SDK's `Agent` includes `fork_session` — absent from the original plan.

### The surface we consume (`Client`)

`request_permission`, `session_update`, `read_text_file`, `write_text_file`,
`create_terminal`, `terminal_output`, `release_terminal`, `wait_for_terminal_exit`,
`kill_terminal`, `create_elicitation`, `complete_elicitation`, plus `ext_method`,
`ext_notification`, and `on_connect` — 14 members in total. The last three are not wire
methods, which is why they are easy to miss, but `on_connect` is how the agent obtains the
`Client` facade every other call goes through.

**Per-method dispositions for both protocols live in
[docs/acp-compliance-matrix.md](acp-compliance-matrix.md)** (`pyacp-4ns.2`), which is derived
from the pinned SDK and is the source of truth for the `initialize` capability block (1.4)
and the Phase 8 conformance suite.

**The original Phase 4 had this backwards.** It read "Implement `fs/read_text_file`",
"Implement `terminal/create`", "Implement `session/request_permission`". An ACP *agent*
does not implement those — the *client* does. Our job is to call them from the turn
executor, gated on the `clientCapabilities` received at `initialize`, and to degrade
correctly when a capability is absent. That inversion is corrected in Phase 4 below.

## Phases

### Phase 0 — Contract and substrate adoption

0.1. Add `agent-client-protocol` to `pyproject.toml`; validate across the full 3.11–3.14
     CI matrix; assess the `pydantic` dependency's effect on wheel and container size.
0.2. Derive the compliance matrix from `acp.interfaces` — record, per method, whether we
     implement it, stub it, or decline it, and which client capabilities we consume.
     **Done:** [docs/acp-compliance-matrix.md](acp-compliance-matrix.md).
0.3. Define module boundaries: agent runtime, session registry, turn executor, MCP backend
     adapter, transport bindings.
0.4. Update the `acp-protocol` skill to describe the SDK-based contract rather than the
     hand-rolled dispatcher it documents today.

### Phase 1 — Agent runtime foundation *(blocking)*

1.1. Implement the `acp.Agent` skeleton wired to `acp.agent.connection`.
1.2. Bind the stdio transport and add a CLI entry point for it.
1.3. Rebind WebSocket onto `acp.ws.server`, replacing the hand-rolled `websockets.serve`.
1.4. Implement `initialize` negotiation with accurate capabilities and auth methods.
1.5. ~~Request registry and cooperative cancellation for `$/cancel_request`.~~
     **Dropped — `$/cancel_request` is not an ACP method.** The upstream schema
     (`agentclientprotocol/agent-client-protocol`, `agent-client-protocol-schema/src/v1/`)
     defines exactly one cancellation: the `session/cancel` notification. The Python
     SDK's `CancelRequestNotification` (`acp/schema.py`) is an orphan model — no entry
     in `AGENT_METHODS`/`CLIENT_METHODS`, absent from the `ClientNotification` union,
     referenced nowhere, and carrying no wire name. Cancellation is covered by 3.5;
     telling the MCP backend to stop is `MCPStdioClient.cancel_request()`, wired from
     that path.
1.6. Standardize ACP/JSON-RPC error mapping and payloads.

### Phase 2 — Session lifecycle and state

2.1. Durable session registry: cwd, `additionalDirectories`, mode/config state, timestamps.
2.2. Baseline lifecycle: `new_session`, `prompt`, `cancel`, `session_update` emission.
2.3. Per-session MCP server registry driven by `new_session`'s `mcpServers`, replacing the
     single process-wide `MCPStdioClient`.
2.4. Extended lifecycle: `load_session`, `resume_session`, `fork_session`, `list_sessions`,
     `close_session`.
2.5. Enforce absolute-path and argument constraints.

### Phase 3 — Prompt turn and streaming semantics

3.1. Define the `TurnExecutor` interface (D3).
3.2. Ship the deterministic MCP tool-router executor as the default.
3.3. Handle all ACP content-block types with capability gating.
3.4. Emit the full `session/update` variant set: message chunks, thought chunks, tool call
     start/progress, plan updates, mode/config/session-info/usage updates.
3.5. Correct `stopReason` semantics, especially `cancelled`.

### Phase 4 — Client method consumption *(inverted from the original)*

4.1. Call `session/request_permission` from the turn executor, including cancellation
     outcomes, gated on client capability.
4.2. Call `fs/read_text_file` and `fs/write_text_file`; fall back correctly when unsupported.
4.3. Call the `terminal/*` family.
4.4. Drive `elicitation/create` and `elicitation/complete`. **Resolved:** `elicitation/create`
     is called by `elicitation.py`, forwarding an MCP server's question to the client as a
     form-mode, session-scoped elicitation. `elicitation/complete` is **declined
     structurally** — it is addressed by `elicitationId`, which exists only on the two URL
     variants of `ElicitationMode`, and nothing here creates one.

### Phase 5 — Authentication, modes, and config options

5.1. `authenticate` and logout flows.
5.2. `set_session_mode` and `current_mode_update` propagation.
5.3. `set_config_option` (select and boolean variants) and `config_option_update`.

### Phase 6 — MCP backend adapter

6.1. Backend hardening — protocol-version negotiation, client capability declaration,
     pagination, cancellation, error-code fidelity, shutdown ordering. **Also delivered
     here:** reading the server's tool annotations as an ACP `ToolCall.kind`
     (`pyacp-eg1.3`), which relabels a permission prompt and never skips one.
6.2. Map MCP results onto ACP tool-call and content updates.
6.3. Backend abstraction permitting non-MCP executors. **Resolved by Phase 3.1:** the
     `TurnExecutor` seam is the abstraction, and `pyacp-eg1.2` closed by *proving* it —
     `tests/test_executor_neutrality.py` runs a non-MCP executor through a whole session
     and guards the seam's imports. No second abstraction was added.

### Phase 7 — Legacy surface deprecation and removal *(replaces the old docs phase)*

7.1. Emit deprecation warnings from the `action` surface.
7.2. Carry the legacy MCP passthrough methods on `ext_method` during the transition.
7.3. Remove the `action` surface; rewrite the README's request documentation.

### Phase 8 — Validation and conformance

8.1. Method-by-method ACP conformance tests.
8.2. Golden JSON-RPC transcript tests (initialize, lifecycle, streaming, cancellation).
8.3. Negative tests: unsupported methods, invalid params, path constraints.
8.4. Interop smoke test against a real ACP client over stdio.
8.5. Final architecture-doc refresh against the delivered module boundaries.

## Delivery sequencing

- Blocking chain: **Phase 0 → Phase 1 → Phase 2 → Phase 3**.
- Parallel after Phase 1: Phase 5, Phase 6.
- Phase 4 depends on Phase 3 (the turn executor is what calls client methods).
- Phase 7 removal step lands last, after JSON-RPC parity is proven in Phase 8.
- Phase 6.1 backend hardening is independent of the SDK migration and can start now.

## Documentation

The original Phase 7 documentation workstream is **complete**: `ARCHITECTURE.md` exists
with both Mermaid diagrams, `cli.md` / `mcp_stdio.md` / `ws_bridge.md` are present, and
README cross-links resolve. The `repo-docs-sync` skill now enforces the co-located-doc
rule automatically.

Documentation is therefore no longer a phase. Instead it is part of every issue's
definition of done: **any new module under `src/python_acp/` ships its sibling `.md` in
the same change, and any change to the request path updates the `ARCHITECTURE.md`
sequence diagram.** Phase 8.5 is the final consistency pass.

## Verification

1. ACP conformance suite green for every method we claim to support.
2. Cancellation verified for `session/cancel` — including that an in-flight MCP
   request is told to stop rather than left computing a reply nobody reads.
3. Capability advertisement matches implemented behavior exactly — no aspirational literals.
4. A real ACP client completes a session over stdio end to end.
5. Full 3.11–3.14 CI matrix green.
6. Mermaid diagrams render; module docs match delivered symbol names.

## What changed from the original plan

| Original | Now | Why |
|---|---|---|
| Phase 0.1 "freeze compliance matrix from schema" | Read it off `acp.interfaces.Agent` | The SDK ships generated models; hand-freezing is redundant work. |
| Phase 0.2 "select substrate (recommended: SDK)" | Settled: adopt the SDK | Verified available, actively maintained, covers 3.11–3.14. |
| Phase 1 "refactor WebSocket endpoint into strict JSON-RPC dispatch" | Adopt SDK dispatch; bind both transports | The SDK already provides dispatch and both transports. |
| Phase 3 prompt-turn executor, semantics unspecified | `TurnExecutor` interface + deterministic MCP router | The original never said what a prompt turn does with no LLM — this blocked the whole phase. |
| Phase 4 "implement `fs/*`, `terminal/*`, `request_permission`" | Consume them as client capabilities | Those are `Client` methods. An agent calls them; it does not serve them. |
| Phase 7 documentation workstream | Already done; folded into every issue's DoD | `ARCHITECTURE.md` and all three module docs exist and are skill-enforced. |
| `fork_session` absent | Included in Phase 2.4 | Present in the SDK's `Agent` protocol. |
| Legacy `action` removal in Phase 1 | Deprecate through migration, remove in Phase 7 | Avoids breaking every documented request mid-migration. |
| stdio transport unaddressed | Primary transport (D2) | ACP clients connect over stdio; WebSocket-only conformance is untestable against a real client. |

## Relevant files

- `src/python_acp/ws_bridge.py` — current hand-rolled dispatch; rebound onto the SDK in Phase 1.
- `src/python_acp/mcp_stdio.py` — MCP backend adapter; hardened in Phase 6.1.
- `src/python_acp/cli.py` — runtime startup; gains a stdio entry point in Phase 1.2.
- `tests/test_mcp_stdio.py` — conformance and backend tests.
- `tests/fixtures/mock_mcp_server.py` — fixture for streaming and cancellation scenarios.
- `pyproject.toml` — SDK and pydantic dependencies.
- `ARCHITECTURE.md` and the co-located module docs — refreshed as boundaries move.
