# `turns.py` — the seam a prompt turn runs behind

`session/prompt` is where an agent does its work, and decision D3 says *what* that work
is must be swappable. The shipped default is a deterministic MCP tool-router with no LLM
in the loop; an LLM-backed executor has to be droppable in later without reopening the
interface. This module is the seam that makes that a design choice rather than a dead end.

> **This is the minimum `pyacp-3rw.2` needed to wire `session/prompt`, not the finished
> interface.** `pyacp-hnk.1` owns that and depends on this bead. See
> [What hnk.1 still owns](#what-pyacp-hnk1-still-owns).

## The shape

```python
class TurnExecutor(Protocol):
    async def execute(self, context: TurnContext, prompt: list[Any]) -> StopReason: ...
```

One `async` method. That is deliberately the smallest shape that does not foreclose
hnk.1's requirements: because the call is `async` and single-method, a turn may take as
many steps and as many client round-trips as it likes, and nothing here assumes it is
synchronous, single-step, or non-interactive.

## Why the context is an object

An executor needs three things and they arrive from three places: the `Session`
([sessions.py](sessions.md)), the `Client` facade ([agent.py](agent.md), from
`on_connect`), and a way to push `session/update` — the two combined. Passing them
separately would make every executor signature grow when a later phase adds a fourth;
passing a context means `TurnContext` grows and executors do not.

`TurnContext` is not a bag of public attributes. **`emit` supplies the session id
itself**, so an executor cannot address someone else's session by accident. A context
that merely exposed `client` would invite exactly that.

## Emission is not optional

`session/update` is the only way a client sees anything before a turn ends, and
`ClientCapabilities` has **no gate** for it — every ACP client must accept it. So `emit`
is unconditional.

`pyacp-hnk.4`'s per-variant gating belongs on the *variant*, never on the call:
`clientCapabilities.plan` governs whether the client accepts the `agent_plan_*` update
variants, so a plan-less client means suppressing those updates, not skipping
`session_update`.

## `IdleTurnExecutor`

The default until `pyacp-hnk.2` ships the tool-router: complete the turn immediately,
having done nothing, and return `end_turn`.

Not a placeholder that raises, and not one that invents content. A conforming turn that
ends straight away is the honest answer while there is no executor — the client's
`session/prompt` gets a well-formed `PromptResponse`, the create-prompt-cancel cycle works
end to end, and nothing pretends to have run a tool. It logs a **warning** every turn on
purpose: a silent no-op turn is exactly the failure someone would otherwise spend an
afternoon on.

## Cancellation is not an error

An executor should let `asyncio.CancelledError` propagate rather than catching it to
return early. [agent.py](agent.md) runs the turn as its own task and converts a cancelled
one into `stopReason: "cancelled"`; an executor that swallowed the cancellation would
return `end_turn` for a turn the client explicitly stopped.

Raising anything else *is* an error and becomes a JSON-RPC error through
[errors.py](errors.md) — a `ValueError` from an executor reaches the client as `-32602`,
which is why an executor should raise `ValueError` for a prompt it cannot accept.

## What `pyacp-hnk.1` still owns

Named here so that bead does not have to rediscover the gap:

- **`stopReason` semantics beyond `end_turn` and `cancelled`.** `max_tokens`,
  `max_turn_requests`, and `refusal` have no producer yet (`pyacp-hnk.5`).
- **Client-method access and its capability gating.** `context.client` is the raw facade;
  Phase 4 (`pyacp-8bv.*`) decides how `fs/*`, `terminal/*`, and `elicitation/*` are
  reached and gated.
- **A cancellation token.** Today cancellation is task cancellation, which is enough for
  `session/cancel` but gives an executor no way to run cleanup that is itself async
  without racing the cancel.
- **Content-block typing.** `prompt` is `list[Any]`; `pyacp-hnk.3` decides how the
  `promptCapabilities` gates (`image`, `audio`, `embeddedContext`) are enforced, and those
  literals stay `false` in `capabilities.AGENT_CAPABILITY_MANIFEST` until it does.
- **A `TurnResult`** carrying usage alongside the stop reason — `PromptResponse.usage`
  exists and nothing fills it.

## Main symbols

| Symbol | Purpose |
|---|---|
| `TurnContext` | What a turn may reach: `session`, `client`, `session_id`, `emit(update)` |
| `TurnExecutor` | The Protocol one turn runs behind |
| `IdleTurnExecutor` | The default — a conforming turn that does nothing |

## Tests

`tests/test_agent.py`, under "Baseline session lifecycle". They drive executors through
the SDK router rather than calling `execute` directly, because the parts worth testing —
that a turn is a cancellable task, that `emit` reaches the client with the right session
id, that a raising executor becomes a mapped error — only exist once the agent is wiring
it up.

## Related

- [agent.py docs](agent.md) — runs the turn as a task and owns the cancelled path
- [sessions.py docs](sessions.md) — `attach_turn` / `cancel_turn`
- [errors.py docs](errors.md) — what a raising executor becomes
