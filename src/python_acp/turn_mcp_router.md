# `turn_mcp_router.py` — the shipped default turn executor

Decision D3 says `session/prompt` runs behind a swappable executor, and D1 says there is
no LLM in this runtime. So the default cannot *interpret* a prompt — it can only **route**
one. A client says which tool to run and with what; this executes it against that
session's MCP backends, streams the call's real status transitions back as
`session/update`, and returns.

Nothing here reasons, plans, or retries. That is the point, not a limitation: an
LLM-backed executor drops into the same [seam](turns.md) without reopening it.

## The invocation convention

**Invented here.** The ACP spec says what a prompt *is* — a list of content blocks — and
nothing about how a block names a tool, because every other agent has a model to work that
out. With no model the contract has to be explicit, so it is the one thing in this module
a client codes against.

A **text** content block whose entire text is a JSON object:

```json
{"type": "text", "text": "{\"tool\": \"echo\", \"arguments\": {\"text\": \"hi\"}}"}
```

| Field | Required | Meaning |
|---|---|---|
| `tool` | yes | The MCP tool name |
| `arguments` | no, defaults to `{}` | Passed to `tools/call` unchanged |
| `server` | only when the session opened **more than one** MCP server | Which server from `session/new`'s `mcpServers` |

Explicit `server`/`tool` fields rather than a single `"server/tool"` string: both names
are arbitrary and may contain a slash, so a separator would be ambiguous exactly where
being wrong is silent.

Every text block in the prompt is one invocation, run **in order**.

`server` may be omitted for a single-server session because there is nothing to guess.
With two or more it is required — picking one is the kind of help nobody wants. The
refusal names the servers that *are* open, so a client does not have to go looking.

The tool-call title is **always** qualified (`tools/echo`), even when the client omitted
`server`. The title outlives the turn — it is in the transcript `session/load` replays —
and "which server ran this" is not recoverable later from a bare name.

## Only text blocks, and the other four are declined by name

A prompt may carry five block types. This executor reads **`text`** and declines the rest:

| Block | Governed by | Declined because |
|---|---|---|
| `text` | — always allowed | *read* — it carries the invocation |
| `image` | `promptCapabilities.image` | it needs a model to look at it |
| `audio` | `promptCapabilities.audio` | it needs a model to listen to it |
| `resource` | `promptCapabilities.embeddedContext` | it is context for a model to read |
| `resource_link` | **nothing** | this agent would have to fetch and reason about it |

All four share one reason, and it is worth saying out loud: **an image, a sound, or an
embedded document is context for a model to reason over, and decision D1 puts no model in
this runtime.** There is no defensible mapping from a picture to an MCP tool call, and
inventing one would be worse than refusing. This is a decision, not a gap — the bead that
made it says as much: "declining a block type is a legitimate outcome as long as
advertisement matches."

They are declined **by name**: a client debugging a rejected prompt is told which block
and why, rather than getting a crash, a silent drop, or a message about JSON. A declined
block takes the whole prompt with it, for the same validate-then-run reason below.

`resource_link` is the odd one out. `PromptCapabilities` has fields for image, audio, and
embeddedContext only, so **no capability governs a resource link** — a client may send one
however this agent answers `initialize`, which is why its refusal carries its own reason
rather than pointing at an advertisement.

`supported_prompt_blocks` is `{"text"}`, and
[capabilities.py](capabilities.md) derives the three `promptCapabilities` literals from
it. The advertisement therefore cannot drift from what this class reads, in either
direction.

## Validate everything, then run anything

A prompt is parsed completely before the first tool runs.

Tools have side effects. A turn that wrote two files and *then* refused because the third
block was malformed leaves no way to undo the first two, and no way to tell from the
outside that it stopped early. So a prompt that does not fully parse runs **nothing at
all** — `test_nothing_runs_when_a_later_block_fails_to_parse` is the guard.

## A prompt that is not an invocation is a refusal, not an error

`stopReason: "refusal"` exists for exactly this, and it comes with an
`agent_message_chunk` carrying the reason *and* the convention.

A JSON-RPC error would be wrong twice over: the request was well-formed ACP, and by the
time a later block fails to parse the turn may already have emitted notifications a client
cannot un-see. A silent refusal would be worse than either.

An **empty prompt** refuses too. It names no tool, so it does not parse as an invocation,
and silently completing is exactly the failure `IdleTurnExecutor` warns about.

## Two kinds of failure, and only one of them fails the turn

| | What MCP sends | What the client sees | `stopReason` |
|---|---|---|---|
| **The tool failed** | a *successful* result with `isError: true` | `tool_call_update` with `status: "failed"` and the tool's own content | `end_turn` |
| **The backend failed** | a JSON-RPC error response | the error, backend code intact via [errors.py](errors.md) | — the request errors |

The first row is MCP's design, not an accident: tool-level failure is meant to be visible
to whatever is driving. Collapsing it into a `stopReason` would lose *which* tool failed
and why, so the remaining calls still run and the turn still ends normally — the turn
completed, one tool did not.

## What a turn emits, in order

| Order | Variant | Always? |
|---|---|---|
| 1 | `user_message_chunk`, one per text block | yes — the prompt, echoed |
| 2 | `available_commands_update` | yes, **including a turn about to be refused** |
| 3 | `agent_message_chunk` | only on a refusal, and then the turn ends |
| 4 | `plan`, all entries `pending` | only when `clientCapabilities.plan` is set |
| 5 | per call: `plan` (this entry `in_progress`) → `tool_call` → `tool_call_update` ×2 → `plan` (entry `completed`/`failed`) | plan lines gated as above |

**The echo is not redundant.** The transcript `session/load` replays is built from what a
turn *emitted*, so without it a reloaded session shows the agent talking to itself.

**The command list on a refusal is the point.** A refusal that also says what *could*
have been called is actionable; one that only says "that was not an invocation" is not.
It costs one `tools/list` per server per turn — sub-millisecond against a local
subprocess, and caching it would need `notifications/tools/list_changed` handling to stay
honest, which is `pyacp-eg1.1`'s neighbourhood.

**The plan is honest rather than aspirational.** Every invocation is validated before the
first tool runs, so the whole plan is known up front — this agent is in the unusual
position of never having to guess at one. It is re-emitted with statuses advanced after
each call, which is the protocol's own mechanism: `AgentPlanUpdate` carries the full
entry list and there is no per-entry patch.

`clientCapabilities.plan` gates the **variant**, never the `session/update` call. A
plan-less client still gets everything else — see [turns.md](turns.md).

The full disposition of all thirteen `session/update` variants — emitted, deferred, and
declined, each with a reason — is `turns.SESSION_UPDATE_DISPOSITIONS`.

## Status transitions

`pending` → `in_progress` → `completed` / `failed`, as three notifications.

The first two are separate on purpose: a client renders the call the moment it is known,
and the move to `in_progress` is what tells it the wait has begun rather than the request
sitting behind something else.

`acp.contrib.tool_calls.ToolCallTracker` generates the ids and merges each partial update
into tracked state. **Used rather than hand-rolled**: the tracker makes "a
`tool_call_update` for a call that never started" impossible instead of merely unlikely,
which is the fiddly part of this variant, and unlike `acp.contrib.session_state` it
carries no experimental marker. The `external_id` indirection is the price; the router
keys it by position in the prompt.

## Where it gets its backends

Constructed with the `McpBackendRegistry`, not by reading one off `TurnContext`.
`docs/module-boundaries.md` has this module reach [mcp_registry.py](mcp_registry.md)
directly, so the context does not widen for one executor's dependency. Servers were opened
**and handshaked** during `session/new`, so every client here is live.

## Main symbols

| Symbol | Purpose |
|---|---|
| `McpToolRouterExecutor(backends)` | The executor. `agent.py`'s default |
| `Invocation` | One parsed call: `tool`, `arguments`, `server`, `title` |
| `PromptConventionError` | A block that is not an invocation. Caught by `execute` and turned into a refusal; a `ValueError` so a future caller that let it escape gets `-32602` |
| `CONVENTION` | The explanation appended to every refusal |
| `DECLINED_BLOCKS` | Each non-text block type and why it is refused |
| `McpToolRouterExecutor.supported_prompt_blocks` | `{"text"}` — what `promptCapabilities` is derived from |

## What later beads own

- `pyacp-eg1.1` — the richer MCP-result mapping. `_as_tool_content` carries **text** and
  skips what it does not understand rather than guessing, because a wrong `type` on the
  wire is harder to notice than a missing block.
- `pyacp-hnk.3` ✔ — content-block typing and the `promptCapabilities` gates. Settled
  above: text only, the rest declined by name, and the literals derived from
  `supported_prompt_blocks`.
- `pyacp-hnk.4` — the rest of the `session/update` variant set.
- `pyacp-hnk.5` — `stopReason` breadth beyond `end_turn`, `refusal`, and `cancelled`.

## Tests

`tests/test_turn_mcp_router.py`, against the real `tests/fixtures/mock_mcp_server.py`
subprocess: what is under test is a tool call actually running and its result actually
reaching a `session/update`, and a mock backend would prove neither. The parsing tests are
exhaustive because the convention is invented here — every refusal it can produce is part
of the contract.

## Related

- [turns.py docs](turns.md) — the seam this implements
- [mcp_registry.py docs](mcp_registry.md) — where the backends come from
- [agent.py docs](agent.md) — what runs the turn as a cancellable task
