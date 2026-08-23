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

## Permission

**Every tool call is consequential**, so every one is asked about. That is not caution
for its own sake: MCP `2024-11-05` has no tool annotations — `readOnlyHint`,
`destructiveHint`, and friends arrive in `2025-03-26` — so there is no way to tell a read
from a delete. Treating everything as consequential is the only setting that cannot
silently do damage, and `allow_always` is what keeps it to once per tool per session.
Refining this is tied to the MCP protocol-version bump, not to a heuristic.

*"But the client already chose the tool"* — the client that sent `session/prompt` and the
human at the ACP client are not necessarily the same party. Automation asks; the
permission prompt is how a person sees and approves it.

The request goes out **after** the `tool_call` notification and **before**
`in_progress`, which is what `pending` is for: the request carries the tool call, so the
client has something to attach its prompt to, and nothing has run yet.

`session/request_permission` has **no capability gate** — `ClientCapabilities` has no
field for it and every ACP client must accept it — so it is called with nothing to check
first.

### Denial is a selected option; the only other outcome is cancellation

This is the part worth reading twice. `RequestPermissionResponse.outcome` is:

| Model | Literal | Means |
|---|---|---|
| `AllowedOutcome` | `"selected"` + `optionId` | the user picked one of the options — which may be a **reject** one |
| `DeniedOutcome` | **`"cancelled"`** | the turn was cancelled while the prompt was open |

Despite the class name, `DeniedOutcome` does not mean denied. Reading it as a rejection
would turn a "no" into `stopReason: "cancelled"`, and reading a rejection as one would do
the reverse — which is the inversion this bead was told to get right.

| Answer | What happens |
|---|---|
| `allow_once` / `allow_always` | the call runs |
| `reject_once` / `reject_always` | the call does **not** run; its update is `failed` with a "Denied by the client" note, and the remaining calls still run |
| an option we never offered | treated as a refusal, and logged — the safe reading |
| outcome `cancelled` | the turn stops immediately with `stopReason: "cancelled"`, its plan entry left unfinished |

The `_always` variants are remembered on the `Session` for its lifetime and copied (not
shared) by a fork — a fork answering "always allow" must not decide for its parent. The
scope is the session because the SDK's own option is named *"Approve for session"*.

### A client that cannot ask a human is not a broken client

This was implemented the other way first, and [interop](../../docs/interop.md) corrected
it. `session/request_permission` is mandatory — `ClientCapabilities` has no field for it —
so a client answering `-32601` looked broken, and the turn refused. Then the SDK's own
`examples/client.py` turned out to answer exactly that, and so will any headless client
with no human to ask. An agent unusable against the reference client is the agent with the
problem.

**The turn proceeds, and says so once per session.** Not "assume consent from nowhere":
the client named this tool and these arguments in `session/prompt` itself, so the
authorization already exists. The prompt was a courtesy to a human who might be watching,
and a client that cannot reach one has already decided.

**That reasoning does not generalise.** An LLM-backed executor *chooses* the tool, so a
client's prompt authorizes nothing in particular and this fallback would be a hole. Any
executor added later must decide it again for itself.

### `acp.contrib.permissions` is used, with one addition

`PermissionBroker` builds the `RequestPermissionRequest` from the `ToolCallTracker` the
router already keeps, so the tool call in the prompt is the same object the client was
sent — used for the same reason as the tracker itself.

Its `default_permission_options()` offers `allow_once`, `allow_always`, and
`reject_once`. **A user can say "always yes" but not "always no"**, and is asked again
about a tool they have already turned down. That asymmetry looks like an oversight rather
than a design, and `reject_always` is one of the four kinds the protocol defines, so
`PERMISSION_OPTIONS` adds the fourth.

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
| `PERMISSION_OPTIONS` | The four options offered before every tool call |
| `McpToolRouterExecutor.supported_prompt_blocks` | `{"text"}` — what `promptCapabilities` is derived from |

## What later beads own

- `pyacp-eg1.1` ✔ — the MCP-result mapping now lives in [mcp_content.py](mcp_content.md)
  and covers all five MCP content types plus annotations. Unmappable content became a
  **visible placeholder** rather than a skip: once the mapping claims to be complete, a
  silent skip makes a client render "the tool did nothing" instead of "we could not show
  this".
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
