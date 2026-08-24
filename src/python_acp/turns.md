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
    session_config_options: tuple[Any, ...]
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

`session_config_options` is declared the same way and for the same reason, and is subject
to the same rule: only expose an option that changes what a turn does.

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

### `allows` asks the client's question; `require` asserts ours

They read the same gate and they are **not** interchangeable.

`-32603` is the right answer to a programming error and the wrong answer to a client that
simply did not advertise a capability — declaring no `fs` is a perfectly conforming thing
for a client to be, and `require` would tell it that *we* were broken.

| | Used for | Answers |
|---|---|---|
| `context.allows(gate)` | deciding, **early**, what to do about a capability the client does not have | whatever the executor's own contract says: `turn_mcp_router.py` refuses the turn before anything runs |
| `context.require(gate)` | the call site itself | `UngatedClientCallError` → `-32603`, because by then a shut gate means the earlier check was missing |

[turn_mcp_router.py](turn_mcp_router.md) does both for `fs/*` and for `terminal/*`:
`allows` while parsing the prompt, `require` immediately before the client call. The first
is the client's answer; the second is an assertion about us.

`terminal/*` puts the split under load, because the calls outlive the parse: a terminal
created inside a turn has to be released from cleanup paths where a raised gate error
would be useless. So [terminals.py](terminals.md) checks the gate **before** any state
moves in `release`, where it cannot fire for a handle that exists, and swallows only the
client call itself.

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

### Emitting on the way out, and the one time it is refused

Cleanup that tells the client what got finished is legitimate, and `shield` is how it
survives its own cancellation long enough to reach the wire. It works on the
`session/cancel` path by construction: `prompt` is still inside `asyncio.wait` there, so
the notification is on the wire *before* the response — which is the "nothing after the
response" guarantee, not a breach of it.

There is one path where it is refused, and it is a different cancellation. When the
**`session/prompt` request itself** dies — the connection dropped, the client's call was
cancelled — there is no response for anything to be after, and `prompt` cancels the turn
task without awaiting it (awaiting a task inside a dead request is how a hang gets made if
an executor ignores cancellation). Between those two moments an executor's shielded
cleanup could put a notification on a socket nobody is reading.

So `prompt` calls `context.detach()` in its `finally`, and `emit` raises
`DetachedTurnError` afterwards. Three properties worth not re-deriving:

| | Why |
|---|---|
| It raises rather than dropping quietly | Dropping would leave an executor believing it told the client something. The failure belongs in the task that caused it |
| It refuses **before** `record`, unlike a send that fails on the wire | A wire failure still happened as far as the session is concerned and `session/load` should replay it. This one never happened and never will, so recording it would promise a replay of a notification no client ever saw |
| It closes the wire only — it does not cancel the turn | Whether the task stops is `prompt`'s business, and it is the one place that can tell the two cancellations apart |

`detach()` is in the `finally` rather than the `except` so there is one rule and no path
to forget it. On the ordinary path it is redundant — the task is already done — which is
the point: the invariant does not depend on which way the turn ended. `pyacp-48b`.

## `TurnResult`

A record rather than a bare `StopReason`, because `PromptResponse` already carries `usage`
and nothing was filling it — and because the next field the schema grows should widen this
type instead of every executor signature. `TurnResult.ended()` names the ordinary
completion so the common case does not read as a string literal.

`TurnResult.ended()`, `.refused()`, and `.cancelled()` name the three exit paths an
executor constructs, so a turn's ending reads as a name rather than a string literal. The
type can express all five `stopReason`s; the two it never returns are in the table below.

**`.cancelled()` is for the route with no task cancellation behind it.** An executor must
never raise `asyncio.CancelledError` to mean "the client stopped this": nothing was
cancelled, `agent.py` checks `Task.cancelled()` (which would be `False`), and a
`BaseException` the SDK does not catch leaves the request unanswered forever.

## Main symbols

| Symbol | Purpose |
|---|---|
| `TurnExecutor` | The Protocol one turn runs behind: `execute`, plus `supported_prompt_blocks`, `session_modes`, and `session_config_options` |
| `TurnContext` | `session`, `client`, `session_id`, `gates`, `cancelled`, `emit`, `require`, `allows`, `wait_for_cancellation`, `detach` |
| `DetachedTurnError` | Raised by `emit` after `detach()` — a turn that outlived its request. See "Emitting on the way out" |
| `TurnResult` | `stop_reason` plus optional `usage`; `ended()`, `refused()`, `cancelled()` name the three exit paths |
| `Gate`, `ClientGates`, `UngatedClientCallError` | Capability gating in method vocabulary |
| `SESSION_UPDATE_DISPOSITIONS`, `UpdateVariant`, `Disposition` | Every `session/update` variant and its fate — see above |
| `STOP_REASON_DISPOSITIONS`, `StopReasonUse` | Every `stopReason` and what produces it — see below |
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
| **emitted** | `UserMessageChunk`, `AgentMessageChunk`, `ToolCallStart`, `ToolCallProgress`, `AgentPlanUpdate`, `AvailableCommandsUpdate`, `CurrentModeUpdate`, `ConfigOptionUpdate` |
| **deferred** | *(none — Phase 5 finished the last two)* |
| **declined** | `AgentThoughtChunk`, `UsageUpdate` — no LLM, so no reasoning trace and no tokens; `AgentPlanContentUpdate`, `AgentPlanRemovedUpdate` — the plan is complete before the first tool runs and no step is ever withdrawn; `SessionInfoUpdate` — nothing mutates a session's title or cwd after creation, by design |

`tests/test_turns.py::test_every_variant_the_sdk_defines_has_a_disposition` walks the
SDK's union, so a release that grows a variant forces a decision rather than letting us
inherit silence. Deferred rows must name a bead; declined rows must say `never`.

## Every `stopReason`, and what produces it

`STOP_REASON_DISPOSITIONS` records all five values of the SDK's `StopReason` literal, on
the same terms as the `session/update` table: a reason we never return is `DECLINED` with
a structural cause rather than left unexplained.

| `stopReason` | Disposition | What reaches it |
|---|---|---|
| `end_turn` | **emitted** | Every invocation the prompt named has run — including one whose tool reported `isError`, which fails the *call*, not the turn |
| `refusal` | **emitted** | The prompt was valid ACP but named nothing this agent will run, so nothing ran; an `agent_message_chunk` says why. Two sources: a prompt that missed the invocation convention, and (`pyacp-8bv.2`) one that correctly asked for a client method the client never advertised |
| `cancelled` | **emitted** | Two routes — see below |
| `max_tokens` | **declined** | A token budget is a model's, and decision D1 puts no model here. Same root as the declined `UsageUpdate` variant |
| `max_turn_requests` | **declined** | A cap on requests made *of a model* inside one turn. This executor makes none: the step count is the number of invocations the client itself named, so there is no agent-initiated loop to bound |

A **backend failure is not on this list.** `MCPProtocolError` propagates out of the turn
and becomes a JSON-RPC error through [errors.py](errors.md), keeping the server's own
code. Collapsing it into a `stopReason` would tell the client the turn ended normally.

`tests/test_turns.py` walks the SDK's literal so a release that grows a value forces a
decision, and `STOP_REASON_EVIDENCE` there pairs every produced reason with the test that
watches a turn end that way — the same proof obligation `CAPABILITY_EVIDENCE` imposes on
an advertised capability.

### The two routes to `cancelled`

They share nothing, and both must keep working.

| | 1. `session/cancel` | 2. permission answered `cancelled` |
|---|---|---|
| What happens | `Session.cancel_turn()` sets the flag, then cancels the turn task | The client answers `session/request_permission` with `DeniedOutcome`, whose literal is **`cancelled`** |
| What the executor does | lets `CancelledError` propagate | returns `TurnResult.cancelled()` |
| Who answers | `agent.py`, from `Task.cancelled()` | `agent.py`, from the returned result |
| Task cancellation involved | yes | **none at all** |

Route 2 is why [turn_mcp_router.py](turn_mcp_router.md) raises a private `_TurnCancelled`
rather than `asyncio.CancelledError`, and why `DeniedOutcome` must not be read as a
rejection: a rejection is a *selected* reject option, and turning a "no" into a cancelled
turn is the inversion this bead exists to prevent.

### What cancellation costs elsewhere

An in-flight MCP call is not merely abandoned. `MCPStdioClient.request` catches the
`CancelledError` on its way out, sends `notifications/cancelled` for that request id, and
re-raises — the server stops computing a reply nobody will read. See
[mcp_stdio.md](mcp_stdio.md).

Nothing emits after the turn's response, and that is **structural** rather than a
convention: `agent.py` builds the `PromptResponse` only after the turn task is done, so
even an executor emitting from an `except CancelledError` handler under `asyncio.shield`
puts its notification on the wire first.

## What the later Phase 3 beads own

- `pyacp-hnk.2` ✔ — the deterministic MCP tool-router, reading backends through
  [mcp_registry.py](mcp_registry.md) rather than through the context. Shipped as
  [turn_mcp_router.py](turn_mcp_router.md), and the third implementer of this Protocol.
- `pyacp-hnk.3` — content-block typing. `prompt` is `list[Any]` here, and the
  `promptCapabilities` literals stay `false` in [capabilities.py](capabilities.md) until
  that bead enforces the gates.
- `pyacp-hnk.4` ✔ — the full `session/update` variant set, including `PLAN_UPDATES`
  suppression, and the disposition table above.
- `pyacp-hnk.5` ✔ — the rest of the `stopReason` contract: the table above, the two
  routes to `cancelled`, and telling the MCP backend to stop.

## Tests

`tests/test_turns.py` for the seam itself; `tests/test_agent.py` for the wiring, since a
turn only becomes a cancellable task once the agent is running it.

## Related

- [agent.py docs](agent.md) — runs the turn as a task and owns the cancelled path
- [sessions.py docs](sessions.md) — `attach_turn` / `cancel_turn` / `cancellation`
- [errors.py docs](errors.md) — what a raising executor becomes
