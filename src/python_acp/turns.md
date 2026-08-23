# `turns.py` — the seam a prompt turn runs behind

`session/prompt` is where an agent does its work, and decision D3 says *what* that work is
must be swappable. The shipped default is a deterministic MCP tool-router with no LLM in
the loop; an LLM-backed executor has to be droppable in later without reopening the
interface. This module is what makes that a design choice rather than a dead end — and it
is the same seam the original plan's "backend abstraction for non-MCP executors" asked
for, satisfied once.

## One method, and nothing it forecloses

```python
class TurnExecutor(Protocol):
    supported_prompt_blocks: frozenset[str]
    session_modes: SessionModeState | None
    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult: ...
```

The interface must not assume a turn is single-step or non-interactive, so the parts that
would have assumed it are **deliberately absent**: no step count, no "return the answer"
shape, no rule against awaiting the client mid-turn. One `async` call may emit, wait on a
client round trip, emit again, and repeat as many times as it likes.

`tests/test_turns.py::test_a_turn_may_be_multi_step_and_interactive` is a *design*
assertion rather than a behaviour one — it exists so that a later simplification into a
single-shot call has something to break.

`supported_prompt_blocks` is the one thing besides `execute`, and it is declarative
because `initialize` has to promise it **before any prompt arrives**:
`promptCapabilities.image`, `.audio`, and `.embeddedContext` are derived from this set by
[capabilities.py](capabilities.md). What a content block *means* depends on the executor,
which D3 makes swappable, so the promise has to come from the executor rather than from a
table that cannot see it — and the capability block is per-agent, which is what makes a
per-executor promise expressible at all.

`session_modes` is declarative for the same reason, and for a second one: a mode only
means something to the executor that acts on it, so nothing else can say what modes
exist. `None` is not "no opinion" — `Session.set_mode` refuses a session that advertises
no modes, so an executor without them cannot have one imposed.

## What a turn is handed

Three things arrive from three places: the `Session` ([sessions.py](sessions.md)), the
`Client` facade ([agent.py](agent.md), from `on_connect`), and what the client said it can
do (`initialize`). Passing them separately would make every executor signature grow when a
later phase adds a fourth; passing a context means `TurnContext` grows and executors do
not.

`TurnContext` is **not** a bag of public attributes. `emit` supplies the session id
itself, so an executor cannot address someone else's session by accident.

## Capability gating belongs to the seam

`ClientCapabilities` decides which client methods an agent may call at all. A call made
without checking is a **conformance bug, not a runtime error** — the client is entitled to
answer `-32601`, and the failure then surfaces as a broken turn far from the omission.
`context.gates` answers the question once, per connection, in the vocabulary of the
methods rather than of the schema: an executor asks "may I write a file", not "is
`clientCapabilities.fs.writeTextFile` true".

Three gate shapes, and they are **not** interchangeable:

| Gate | Shape | The mistake it prevents |
|---|---|---|
| `READ_TEXT_FILE`, `WRITE_TEXT_FILE` | two independent booleans under `fs` | a read grant quietly satisfying a write |
| `TERMINAL` | one boolean for all five `terminal/*` methods | inventing per-method granularity the schema does not have |
| `ELICITATION`, `PLAN_UPDATES` | advertised by **presence** — empty marker models | checking `bool(model)` instead of `is not None` |

`PLAN_UPDATES` gates *update variants*, not a method. `session/update` itself is ungated,
so a plan-less client means `pyacp-hnk.4` suppresses the `agent_plan_*` variants — never
that it skips `emit`.

**`session/update` and `session/request_permission` have no gate at all.**
`ClientCapabilities` has no field for either and every ACP client must accept both. Do not
invent one.

A client that declared nothing — or a turn running before `initialize` — may call
**nothing**. That is the only safe reading of an absent declaration.

`UngatedClientCallError` is a `RuntimeError`, so [errors.py](errors.md) maps it to
`-32603`. That code is the honest one: this is our bug, not a bad parameter.

## Cancellation is not an error

`session/cancel` cancels the turn's task, and an executor should let
`asyncio.CancelledError` **propagate** rather than catching it to return early.
[agent.py](agent.md) converts a cancelled turn into `stopReason: "cancelled"`, so an
executor that swallowed the cancellation would report `end_turn` for a turn the client
explicitly stopped.

`context.cancelled` is how an executor *knows* without being told by the exception, and
**it is set before the task is cancelled**. That ordering is its entire value:

- inside an `except asyncio.CancelledError` handler it distinguishes "the client cancelled
  this turn" from "the whole request died";
- it lets async cleanup run under `asyncio.shield` instead of racing a cancellation
  already in flight.

`context.wait_for_cancellation()` is for an executor that would rather *race* a long
operation than be torn out of it —
`asyncio.wait([work, cancel], return_when=FIRST_COMPLETED)`.

## `TurnResult`

A record rather than a bare `StopReason`, because `PromptResponse` already carries `usage`
and nothing was filling it — and because the next field the schema grows should widen this
type instead of every executor signature. `TurnResult.ended()` names the ordinary
completion so the common case does not read as a string literal.

The `stopReason` contract beyond `end_turn` and `cancelled` — `max_tokens`,
`max_turn_requests`, `refusal`, and how each interleaves with in-flight updates and a
running MCP call — is `pyacp-hnk.5`'s. The type can already express all five.

## Main symbols

| Symbol | Purpose |
|---|---|
| `TurnExecutor` | The Protocol one turn runs behind: `execute`, plus `supported_prompt_blocks` and `session_modes` |
| `TurnContext` | `session`, `client`, `session_id`, `gates`, `cancelled`, `emit`, `require`, `allows`, `wait_for_cancellation` |
| `TurnResult` | `stop_reason` plus optional `usage`; `TurnResult.ended()` for the common case |
| `Gate`, `ClientGates`, `UngatedClientCallError` | Capability gating in method vocabulary |
| `SESSION_UPDATE_DISPOSITIONS`, `UpdateVariant`, `Disposition` | Every `session/update` variant and its fate — see above |
| `IdleTurnExecutor` | The default — a conforming turn that does nothing |

`IdleTurnExecutor` is **no longer the default** — `pyacp-hnk.2` shipped
[`McpToolRouterExecutor`](turn_mcp_router.md), which `agent.py` now builds when no
executor is passed. It remains for a caller that genuinely wants a turn to do nothing, and
it still warns every turn, because a silent no-op turn is exactly the failure someone
would otherwise spend an afternoon on.

## The `session/update` variant set

`SESSION_UPDATE_DISPOSITIONS` records all thirteen members of the SDK's
`SessionNotification.update` union and what this project does about each. The point is
that **nothing is silently missing**: a variant we do not send is either waiting on a
feature (`DEFERRED`, naming the bead) or will never have a source (`DECLINED`, with the
structural reason).

| Disposition | Variants |
|---|---|
| **emitted** | `UserMessageChunk`, `AgentMessageChunk`, `ToolCallStart`, `ToolCallProgress`, `AgentPlanUpdate`, `AvailableCommandsUpdate`, `CurrentModeUpdate` |
| **deferred** | `ConfigOptionUpdate` (`pyacp-fln.3`) — nothing offers config options yet |
| **declined** | `AgentThoughtChunk`, `UsageUpdate` — no LLM, so no reasoning trace and no tokens; `AgentPlanContentUpdate`, `AgentPlanRemovedUpdate` — the plan is complete before the first tool runs and no step is ever withdrawn; `SessionInfoUpdate` — nothing mutates a session's title or cwd after creation, by design |

`tests/test_turns.py::test_every_variant_the_sdk_defines_has_a_disposition` walks the
SDK's union, so a release that grows a variant forces a decision rather than letting us
inherit silence. Deferred rows must name a bead; declined rows must say `never`.

## What the later Phase 3 beads own

- `pyacp-hnk.2` ✔ — the deterministic MCP tool-router, reading backends through
  [mcp_registry.py](mcp_registry.md) rather than through the context. Shipped as
  [turn_mcp_router.py](turn_mcp_router.md), and the third implementer of this Protocol.
- `pyacp-hnk.3` — content-block typing. `prompt` is `list[Any]` here, and the
  `promptCapabilities` literals stay `false` in [capabilities.py](capabilities.md) until
  that bead enforces the gates.
- `pyacp-hnk.4` ✔ — the full `session/update` variant set, including `PLAN_UPDATES`
  suppression, and the disposition table above.
- `pyacp-hnk.5` — the rest of the `stopReason` contract.

## Tests

`tests/test_turns.py` for the seam itself; `tests/test_agent.py` for the wiring, since a
turn only becomes a cancellable task once the agent is running it.

## Related

- [agent.py docs](agent.md) — runs the turn as a task and owns the cancelled path
- [sessions.py docs](sessions.md) — `attach_turn` / `cancel_turn` / `cancellation`
- [errors.py docs](errors.md) — what a raising executor becomes
