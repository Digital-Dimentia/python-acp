"""The seam a prompt turn runs behind.

`session/prompt` is where an agent does its work, and decision D3 says *what* that work
is must be swappable. The shipped default is a deterministic MCP tool-router with no LLM
in the loop; an LLM-backed executor has to be droppable in later without reopening the
interface. This module is what makes that a design choice rather than a dead end — and
it is the same seam the original plan's "backend abstraction for non-MCP executors"
asked for, satisfied once.

## One method, and nothing it forecloses

```python
class TurnExecutor(Protocol):
    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult: ...
```

The interface must not assume a turn is single-step or non-interactive, so the parts that
would have assumed it are deliberately absent: there is no step count, no "return the
answer" shape, and no rule against awaiting the client mid-turn. One `async` call may
emit, wait on a client round trip, emit again, and repeat as many times as it likes.
`tests/test_turns.py::test_a_turn_may_be_multi_step_and_interactive` is what holds that
open — it is a *design* assertion, not a behaviour one.

## What a turn is handed

Three things arrive from three places: the `Session` (`sessions.py`), the `Client` facade
(`agent.py`, from `on_connect`), and what the client said it can do (`initialize`).
Passing them separately would make every executor signature grow when a later phase adds
a fourth; passing a context means `TurnContext` grows and executors do not.

`TurnContext` is not a bag of public attributes. **`emit` supplies the session id
itself**, so an executor cannot address someone else's session by accident.

## Capability gating belongs to the seam, not to each call site

`ClientCapabilities` decides which client methods an agent may call at all, and a call
made without checking is a conformance bug — the client is entitled to answer `-32601`,
and the failure surfaces as a broken turn far from the omission. `TurnContext.gates`
answers the question once, per connection, in the vocabulary of the methods rather than
of the schema.

Three shapes of gate, and they are not interchangeable:

* **`fs` has two independent booleans.** Read may be permitted while write is not, so a
  `require(Gate.WRITE_TEXT_FILE)` must never be satisfied by a read grant.
* **`terminal` is one boolean for all five `terminal/*` methods.** No per-method
  granularity exists; check it once and treat the family as all-or-nothing.
* **`plan` gates *update variants*, not a method.** `session/update` itself is ungated —
  `ClientCapabilities` has no field for it and every ACP client must accept it — so a
  plan-less client means suppressing the `agent_plan_*` variants, never skipping `emit`.
  `pyacp-hnk.4` owns that suppression; the gate is here so it has one place to read.

`session/update` and `session/request_permission` have **no gate at all**. Do not invent
one for them.

## Cancellation is not an error

`session/cancel` cancels the turn's task, and an executor should let
`asyncio.CancelledError` propagate rather than catching it to return early — `agent.py`
converts a cancelled turn into `stopReason: "cancelled"`, so an executor that swallowed
the cancellation would report `end_turn` for a turn the client explicitly stopped.

`context.cancelled` is how an executor *knows* without being told by the exception. It is
set **before** the task is cancelled, which is the entire value of it: inside an
`except CancelledError` handler it distinguishes "the client cancelled this turn" from
"the whole request died", and it lets async cleanup run under `asyncio.shield` instead of
racing the cancellation that is already in flight.

Raising anything else *is* an error and becomes a JSON-RPC error through `errors.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from acp.interfaces import Client
from acp.schema import ClientCapabilities, StopReason, Usage

from python_acp.sessions import Session

logger = logging.getLogger(__name__)


class Disposition(str, Enum):
    """What this project does about one `session/update` variant."""

    #: Something produces it today.
    EMITTED = "emitted"
    #: Nothing produces it yet, and a named bead will.
    DEFERRED = "deferred"
    #: Nothing will produce it, and the reason is structural rather than unfinished.
    DECLINED = "declined"


@dataclass(frozen=True)
class UpdateVariant:
    """One `SessionNotification.update` member and what we do about it."""

    name: str
    disposition: Disposition
    owner: str
    why: str


#: Every variant the SDK's `SessionNotification.update` union defines, and its fate.
#:
#: **The point is that nothing is silently missing.** A variant we do not send is either
#: waiting on a feature (`DEFERRED`, with the bead that brings it) or will never have a
#: source (`DECLINED`, with the structural reason). `tests/test_turns.py` walks the SDK's
#: union and fails on any member this table does not name, so an SDK that grows a variant
#: forces a decision instead of inheriting silence.
SESSION_UPDATE_DISPOSITIONS: tuple[UpdateVariant, ...] = (
    UpdateVariant(
        "UserMessageChunk",
        Disposition.EMITTED,
        "pyacp-hnk.4",
        "The prompt is echoed back at the start of a turn, so `session/load` replays a "
        "transcript with both halves of the conversation rather than only the agent's.",
    ),
    UpdateVariant(
        "AgentMessageChunk",
        Disposition.EMITTED,
        "pyacp-hnk.2",
        "Carries a refusal's explanation. It is the only prose this agent produces.",
    ),
    UpdateVariant(
        "AgentThoughtChunk",
        Disposition.DECLINED,
        "never",
        "A thought is a model's reasoning trace. Decision D1 puts no LLM in this runtime, "
        "so there is nothing to narrate and inventing one would be a fiction.",
    ),
    UpdateVariant(
        "ToolCallStart",
        Disposition.EMITTED,
        "pyacp-hnk.2",
        "One per invocation, at `pending`, before the call is made.",
    ),
    UpdateVariant(
        "ToolCallProgress",
        Disposition.EMITTED,
        "pyacp-hnk.2",
        "`in_progress` when the call starts, then `completed` or `failed` with the tool's "
        "own output.",
    ),
    UpdateVariant(
        "AgentPlanUpdate",
        Disposition.EMITTED,
        "pyacp-hnk.4",
        "The router validates every invocation before running any, so the whole plan is "
        "known up front — an honest plan rather than a guess. Re-emitted after each call "
        "with statuses advanced, which is the protocol's own mechanism: the variant "
        "carries the full entry list. Gated on `clientCapabilities.plan`.",
    ),
    UpdateVariant(
        "AgentPlanContentUpdate",
        Disposition.DECLINED,
        "never",
        "Streams content into a plan entry as it is produced. This plan is complete "
        "before the first tool runs, so there is never partial entry content to stream.",
    ),
    UpdateVariant(
        "AgentPlanRemovedUpdate",
        Disposition.DECLINED,
        "never",
        "Withdraws a plan entry. Entries here are the invocations the client named; a "
        "turn that cannot run one refuses the whole prompt rather than dropping a step.",
    ),
    UpdateVariant(
        "AvailableCommandsUpdate",
        Disposition.EMITTED,
        "pyacp-hnk.4",
        "The session's MCP tools, listed at the start of every turn. It is what makes a "
        "refusal actionable — the client is told what it could have called.",
    ),
    UpdateVariant(
        "CurrentModeUpdate",
        Disposition.DEFERRED,
        "pyacp-fln.2",
        "`session/set_mode` must emit it, and nothing offers modes yet: `NewSessionResponse."
        "modes` is None until Phase 5.",
    ),
    UpdateVariant(
        "ConfigOptionUpdate",
        Disposition.DEFERRED,
        "pyacp-fln.3",
        "`session/set_config_option` must emit it, and nothing offers config options yet.",
    ),
    UpdateVariant(
        "SessionInfoUpdate",
        Disposition.DECLINED,
        "never",
        "Announces a change to a session's title, cwd, or timestamps. Nothing mutates "
        "those after creation *by design* — `session/resume` deliberately ignores the cwd "
        "it is handed — so there is no change to announce.",
    ),
    UpdateVariant(
        "UsageUpdate",
        Disposition.DECLINED,
        "never",
        "Token usage. Same root as AgentThoughtChunk: no LLM, no tokens. `TurnResult.usage` "
        "stays None for the same reason, and flipping either would mean inventing numbers.",
    ),
)


class Gate(str, Enum):
    """A client capability, named after what it unlocks rather than where it lives.

    An executor asks "may I write a file", not "is `clientCapabilities.fs.writeTextFile`
    true". The second spelling is what leaks the schema into every call site and lets a
    read grant quietly satisfy a write.
    """

    READ_TEXT_FILE = "fs/read_text_file"
    WRITE_TEXT_FILE = "fs/write_text_file"
    TERMINAL = "terminal/*"
    ELICITATION = "elicitation/*"
    PLAN_UPDATES = "session/update:agent_plan_*"


class UngatedClientCallError(RuntimeError):
    """An executor reached for a client method the client never advertised.

    A `RuntimeError`, so `errors.py` maps it to `-32603`: this is **our** conformance
    bug, not a bad parameter. The client is entitled to answer `-32601` to such a call,
    and failing here names the omission instead of letting it surface as a broken turn.
    """

    def __init__(self, gate: Gate) -> None:
        super().__init__(f"The client did not advertise support for {gate.value}")
        self.gate = gate


@dataclass(frozen=True)
class ClientGates:
    """What the connected client said it can do, in method terms.

    Built once per connection from `initialize`. Absent capabilities mean **no** — a turn
    that runs before `initialize` (or against a client that declared nothing) may call
    nothing, which is the only safe reading.
    """

    read_text_file: bool = False
    write_text_file: bool = False
    terminal: bool = False
    elicitation: bool = False
    plan_updates: bool = False

    @classmethod
    def of(cls, capabilities: ClientCapabilities | None) -> ClientGates:
        if capabilities is None:
            return cls()
        fs = capabilities.fs
        return cls(
            read_text_file=bool(fs and fs.read_text_file),
            write_text_file=bool(fs and fs.write_text_file),
            terminal=bool(capabilities.terminal),
            # `plan` and `elicitation` are advertised by **presence**, not by a boolean —
            # they are marker models, so `is not None` is the check and `bool(...)` on the
            # model itself would be wrong for an empty one.
            elicitation=capabilities.elicitation is not None,
            plan_updates=capabilities.plan is not None,
        )

    def allows(self, gate: Gate) -> bool:
        return bool(getattr(self, _GATE_FIELDS[gate]))

    def require(self, gate: Gate) -> None:
        """Raise unless the gate is open. The one line every client call starts with."""
        if not self.allows(gate):
            raise UngatedClientCallError(gate)


_GATE_FIELDS: dict[Gate, str] = {
    Gate.READ_TEXT_FILE: "read_text_file",
    Gate.WRITE_TEXT_FILE: "write_text_file",
    Gate.TERMINAL: "terminal",
    Gate.ELICITATION: "elicitation",
    Gate.PLAN_UPDATES: "plan_updates",
}


@dataclass(frozen=True)
class TurnResult:
    """What a turn reports when it finishes.

    A record rather than a bare `StopReason` because `PromptResponse` already carries
    `usage` and nothing was filling it — and because the next field the schema grows
    should widen this type instead of every executor signature.
    """

    stop_reason: StopReason
    usage: Usage | None = None

    @classmethod
    def ended(cls) -> TurnResult:
        """The ordinary completion. Named so the common case does not read as a literal."""
        return cls("end_turn")


class TurnContext:
    """Everything a turn is allowed to reach, and nothing else.

    Deliberately not a dataclass of public attributes: `emit` is the point, and a context
    that merely exposed `client` would invite an executor to call `session_update` with
    the wrong session id.
    """

    def __init__(
        self,
        session: Session,
        client: Client,
        client_capabilities: ClientCapabilities | None = None,
    ) -> None:
        self.session = session
        self.client = client
        self.gates = ClientGates.of(client_capabilities)

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def cancelled(self) -> bool:
        """Whether `session/cancel` has asked this turn to stop.

        True *before* the `CancelledError` arrives — see the module docstring for why the
        ordering is the whole point.
        """
        return self.session.cancellation.is_set()

    async def wait_for_cancellation(self) -> None:
        """Block until this turn is cancelled.

        For an executor that wants to race a long operation against the cancel rather
        than be torn out of it — `asyncio.wait([work, cancel], return_when=FIRST_COMPLETED)`.
        """
        await self.session.cancellation.wait()

    def require(self, gate: Gate) -> None:
        """`self.gates.require`, at the reach an executor already has."""
        self.gates.require(gate)

    def allows(self, gate: Gate) -> bool:
        return self.gates.allows(gate)

    async def emit(self, update: Any) -> None:
        """Push one `session/update` notification for this turn's session.

        Ungated on purpose: `ClientCapabilities` has no field for `session/update` and
        every ACP client must accept it. Per-variant suppression (`plan_updates`) belongs
        to the caller choosing what to emit, never to this call.

        The session id comes from the context, never from the caller, so an executor
        cannot address someone else's session by accident.
        """
        logger.debug("session/update for %s: %s", self.session_id, type(update).__name__)
        # Recorded before the send, not after: a notification that failed on the wire
        # still happened as far as this session is concerned, and `session/load` replaying
        # it is the client's chance to see what it missed.
        self.session.record(update)
        await self.client.session_update(session_id=self.session_id, update=update)


class TurnExecutor(Protocol):
    """Serves one `session/prompt`.

    Returns the `TurnResult` the response is built from. Raising is allowed and becomes a
    JSON-RPC error through `errors.py`; being cancelled is **not** an error — let
    `asyncio.CancelledError` propagate.
    """

    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult: ...


class IdleTurnExecutor:
    """The default: complete the turn immediately, having done nothing.

    Not a placeholder that raises, and not one that invents content. A conforming turn
    that ends straight away is the honest answer while there is no executor: the client's
    `session/prompt` gets a well-formed `PromptResponse`, the create-prompt-cancel cycle
    works end to end, and nothing pretends to have run a tool.

    **No longer the default** — `agent.py` builds `turn_mcp_router.McpToolRouterExecutor`
    when none is passed. This remains for a caller that genuinely wants a turn to do
    nothing. The warning fires once per turn on purpose: a silent no-op turn is exactly
    the failure someone would otherwise spend an afternoon on.
    """

    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult:
        logger.warning(
            "session/prompt for %s completed without running anything: no turn executor "
            "is configured (pyacp-hnk.2 ships the default)",
            context.session_id,
        )
        return TurnResult.ended()
