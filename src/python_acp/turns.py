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

Four shapes of gate, and they are not interchangeable:

* **`fs` has two independent booleans.** Read may be permitted while write is not, so a
  `require(Gate.WRITE_TEXT_FILE)` must never be satisfied by a read grant.
* **`terminal` is one boolean for all five `terminal/*` methods.** No per-method
  granularity exists; check it once and treat the family as all-or-nothing.
* **`elicitation` is a container of two independent markers, not a marker itself.**
  `ElicitationCapabilities` carries `form` and `url`, and a client may send
  `elicitation: {}` — advertising the object while supporting neither mode. So the outer
  object is never a gate; `ELICITATION_FORM` and `ELICITATION_URL` are, and each is a
  presence check on its own sub-model.
* **`plan` gates *update variants*, not a method.** `session/update` itself is ungated —
  `ClientCapabilities` has no field for it and every ACP client must accept it — so a
  plan-less client means suppressing the `agent_plan_*` variants, never skipping `emit`.
  `pyacp-hnk.4` owns that suppression; the gate is here so it has one place to read.

`session/update` and `session/request_permission` have **no gate at all**. Do not invent
one for them.

## `allows` asks the client's question; `require` asserts ours

`require` raises `UngatedClientCallError`, which is a `RuntimeError` and therefore
`-32603` — *we* reached for a method we never checked for. That is the right answer to a
programming error and the **wrong** answer to a client that simply did not advertise a
capability, which is a perfectly conforming thing for a client to be.

So an executor decides what to do about an absent capability with `allows`, early, in
whatever vocabulary its own contract has — `turn_mcp_router.py` refuses the turn with
`TurnResult.refused()` before anything runs — and keeps `require` for the call site, where
by then a shut gate really would mean the earlier check was missing. Both readings of the
same gate, and they are not interchangeable.

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

## Every `stopReason`, and what produces it

`STOP_REASON_DISPOSITIONS` names all five values of the SDK's `StopReason` literal the
same way `SESSION_UPDATE_DISPOSITIONS` names the `session/update` variants: three are
produced here, and the two limit conditions are `DECLINED` with a structural reason
rather than left unexplained. There is no LLM, so there is no token budget to exhaust and
no agent-initiated request loop to bound; the number of steps in a turn is the number of
invocations the client itself named.

`TurnResult.ended()`, `.refused()`, and `.cancelled()` are the three an executor
constructs, so an exit path reads as a name rather than as a string literal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from acp.interfaces import Client
from acp.schema import ClientCapabilities, SessionModeState, StopReason, Usage

from python_acp.sessions import Session

logger = logging.getLogger(__name__)


class Disposition(str, Enum):
    """What this project does about one member of a protocol enumeration.

    Shared by the two tables below — the `session/update` variants and the `stopReason`
    values — because the question is the same in both: does something here produce it,
    is something going to, or will nothing ever?
    """

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
        Disposition.EMITTED,
        "pyacp-fln.2",
        "`session/set_mode` emits it, and so would an internal change — `agent.announce_mode` "
        "is the one place either goes through, so the two are indistinguishable on the wire.",
    ),
    UpdateVariant(
        "ConfigOptionUpdate",
        Disposition.EMITTED,
        "pyacp-fln.3",
        "`session/set_config_option` emits it through `agent.announce_config_options`, "
        "carrying every option rather than the changed one — which is what the schema "
        "asks for and what a client re-rendering a settings panel wants.",
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


@dataclass(frozen=True)
class StopReasonUse:
    """One `StopReason` the schema defines, and what makes a turn end that way."""

    name: str
    disposition: Disposition
    owner: str
    why: str


#: Every value of the SDK's `StopReason` literal, and what produces it here.
#:
#: Same contract as `SESSION_UPDATE_DISPOSITIONS`, for the same reason: a `stopReason`
#: this agent never returns is either waiting on a feature (`DEFERRED`, naming the bead)
#: or has no source and never will (`DECLINED`, with the structural reason).
#: `tests/test_turns.py` walks the SDK's literal so a release that grows a value forces a
#: decision, and pairs every `EMITTED` row with the test that proves a turn really ends
#: that way.
#:
#: **A backend failure is not on this list.** An `MCPProtocolError` propagates out of the
#: turn and becomes a JSON-RPC error through `errors.py`, keeping the server's own code —
#: collapsing it into a `stopReason` would tell the client the turn ended normally.
STOP_REASON_DISPOSITIONS: tuple[StopReasonUse, ...] = (
    StopReasonUse(
        "end_turn",
        Disposition.EMITTED,
        "pyacp-hnk.2",
        "The ordinary completion: every invocation the prompt named has been run. A tool "
        "that reported `isError` ends the turn this way too — the turn finished, one tool "
        "did not, and the `tool_call_update` carries which and why.",
    ),
    StopReasonUse(
        "refusal",
        Disposition.EMITTED,
        "pyacp-hnk.2",
        "The prompt was well-formed ACP but named nothing this agent can run, so nothing "
        "ran at all. It comes with an `agent_message_chunk` explaining why; a JSON-RPC "
        "error would be wrong, because the request itself was valid. `pyacp-8bv.2` added "
        "a second source with the same shape: a prompt that correctly asks for a client "
        "method the client never advertised — an `fs/*` call without "
        "`clientCapabilities.fs`, or a `terminal/*` one without "
        "`clientCapabilities.terminal` (`pyacp-8bv.3`) — is refused rather than raising "
        "`UngatedClientCallError`, which would report our conformance bug for the "
        "client's ordinary absence of a capability.",
    ),
    StopReasonUse(
        "cancelled",
        Disposition.EMITTED,
        "pyacp-hnk.5",
        "Two routes reach it and both must keep working: `session/cancel` cancels the "
        "turn task, and a client answering `session/request_permission` with "
        "`DeniedOutcome` — whose literal is `cancelled` — makes the executor return it "
        "directly, with no task cancellation anywhere.",
    ),
    StopReasonUse(
        "max_tokens",
        Disposition.DECLINED,
        "never",
        "A token budget belongs to a model. Decision D1 puts no LLM in this runtime, so "
        "there is nothing to exhaust — the same root as the declined `UsageUpdate` "
        "variant, and returning it would mean inventing a limit nothing measures.",
    ),
    StopReasonUse(
        "max_turn_requests",
        Disposition.DECLINED,
        "never",
        "A cap on how many requests an agent makes *of a model* within one turn. This "
        "executor makes none: it runs exactly the invocations the client itself named, so "
        "the step count is the client's own and there is no agent-initiated loop to "
        "bound. A prompt this agent will not run is refused before anything runs.",
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
    ELICITATION_FORM = "elicitation/create:form"
    ELICITATION_URL = "elicitation/create:url"
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
    elicitation_form: bool = False
    elicitation_url: bool = False
    plan_updates: bool = False

    @classmethod
    def of(cls, capabilities: ClientCapabilities | None) -> ClientGates:
        if capabilities is None:
            return cls()
        fs = capabilities.fs
        elicitation = capabilities.elicitation
        return cls(
            read_text_file=bool(fs and fs.read_text_file),
            write_text_file=bool(fs and fs.write_text_file),
            terminal=bool(capabilities.terminal),
            # `elicitation` is a **container**, not a marker: its own presence promises
            # nothing, and the two modes inside it are the markers. A client may advertise
            # `elicitation: {}` and support neither, so each mode is read separately and
            # the outer object is never a gate of its own.
            elicitation_form=elicitation is not None and elicitation.form is not None,
            elicitation_url=elicitation is not None and elicitation.url is not None,
            # `plan` really is an empty marker model, so `is not None` is the check and
            # `bool(...)` on the model itself would be wrong for an empty one.
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
    Gate.ELICITATION_FORM: "elicitation_form",
    Gate.ELICITATION_URL: "elicitation_url",
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

    @classmethod
    def refused(cls) -> TurnResult:
        """The prompt named nothing this agent can run, so nothing ran."""
        return cls("refusal")

    @classmethod
    def cancelled(cls) -> TurnResult:
        """The client stopped the turn.

        For the route that has **no** task cancellation behind it: a client answering
        `session/request_permission` with `DeniedOutcome` (literal `"cancelled"`) is
        telling us to stop, and the executor says so by returning. An executor must never
        raise `asyncio.CancelledError` to mean this — nothing was cancelled, the response
        would never be sent, and `agent.py` checks `Task.cancelled()`, which would be
        `False`.
        """
        return cls("cancelled")


class DetachedTurnError(RuntimeError):
    """An executor tried to emit after its turn stopped belonging to a request.

    A `RuntimeError`, so `errors.to_request_error` would map it to `-32603` — our bug,
    not the client's — though in the case it exists for there is no request left to answer
    it on. Raising is still the point: it turns a notification nobody can receive into a
    loud failure inside the detached task instead of a silent write to a dead connection.
    """


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
        #: False once `detach()` runs. `emit` checks it, which is what makes "no
        #: session/update after the request is over" hold on the path where there is no
        #: response to be after. See `detach`.
        self._attached = True

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

    def detach(self) -> None:
        """Cut this context off from the wire. Idempotent, and never raises.

        `agent.prompt` calls it once the turn no longer belongs to a live request. On the
        ordinary path that is redundant — the response is built only after the turn task is
        *done*, so nothing can emit later anyway — and on the path this exists for it is
        the only thing standing there.

        That path is the `session/prompt` **request itself** being cancelled, which is not
        `session/cancel`. `prompt` cancels the turn task and re-raises without awaiting it,
        because awaiting a task inside a dead request is how a hang gets made if an
        executor ignores cancellation. So between the request dying and the task reaching
        its next suspension point, an executor cleaning up under `except CancelledError`
        can still call `emit` — for a request nobody is reading, on a connection that may
        already be gone. `pyacp-48b`.

        Deliberately not a cancel of the turn: this only closes the wire. Whether the task
        stops is `prompt`'s business, and a context that killed the task would take the
        decision away from the one place that can tell the two cancellations apart.
        """
        self._attached = False

    async def emit(self, update: Any) -> None:
        """Push one `session/update` notification for this turn's session.

        Ungated on purpose: `ClientCapabilities` has no field for `session/update` and
        every ACP client must accept it. Per-variant suppression (`plan_updates`) belongs
        to the caller choosing what to emit, never to this call.

        The session id comes from the context, never from the caller, so an executor
        cannot address someone else's session by accident.

        Raises `DetachedTurnError` once `detach()` has run. Enforcement rather than
        convention: "an executor should not emit after it is cancelled" is a rule nobody
        can check, and this makes breaking it fail loudly in the task that broke it.
        """
        if not self._attached:
            # Refused *before* `record`, unlike a send that fails on the wire. That one
            # still happened as far as the session is concerned; this one never will, so
            # putting it in the history would promise a `session/load` replay of a
            # notification no client ever saw.
            raise DetachedTurnError(
                f"Turn for session {self.session_id} emitted a "
                f"{type(update).__name__} after its request was over"
            )
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

    #: The `ContentBlock` `type` discriminators this executor actually reads — `"text"`,
    #: `"image"`, `"audio"`, `"resource"`, `"resource_link"`.
    #:
    #: Declarative rather than discovered, because `initialize` has to promise it before
    #: any prompt arrives: `promptCapabilities.image`, `.audio`, and `.embeddedContext`
    #: are **derived from this set**, so an executor that starts reading images flips the
    #: literal by saying so, and one that says so without reading them fails a test. The
    #: capability block is per-agent and an agent knows its executor, which is what makes
    #: a per-executor promise expressible at all.
    supported_prompt_blocks: frozenset[str]

    #: The modes this executor offers, or `None` when it has none. Declarative for the
    #: same reason as `supported_prompt_blocks`: `session/new` advertises them before any
    #: turn runs, and a mode only means something to the executor that acts on it.
    #:
    #: `None` is not "no opinion" — `Session.set_mode` refuses a session that advertises
    #: no modes, so an executor without them cannot have one imposed.
    session_modes: SessionModeState | None

    #: The config options this executor exposes, in their initial state. Declared for the
    #: same reason as `session_modes`, and subject to the same rule: only expose an option
    #: that changes what a turn does.
    session_config_options: tuple[Any, ...]

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

    #: It reads nothing at all, so it promises nothing. An agent wired with only this
    #: advertises no prompt capabilities, which is the accurate statement.
    supported_prompt_blocks: frozenset[str] = frozenset()
    #: It does nothing, so there is no mode in which it does something different, and
    #: nothing to configure about the nothing.
    session_modes: SessionModeState | None = None
    session_config_options: tuple[Any, ...] = ()

    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult:
        logger.warning(
            "session/prompt for %s completed without running anything: no turn executor "
            "is configured (pyacp-hnk.2 ships the default)",
            context.session_id,
        )
        return TurnResult.ended()
