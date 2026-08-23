"""Tests for the turn seam.

Two of these are *design* assertions rather than behaviour ones, and they are the reason
the file exists: `test_a_turn_may_be_multi_step_and_interactive` holds the interface open
against a future simplification that would assume a single-shot turn, and
`test_a_read_grant_does_not_satisfy_a_write` pins the one gate shape that is easy to
collapse.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import typing

import pytest
from acp.schema import (
    SessionNotification,
    StopReason,
    AgentMessageChunk,
    ClientCapabilities,
    ElicitationCapabilities,
    FileSystemCapabilities,
    PlanCapabilities,
    TextContentBlock,
    Usage,
)

from python_acp.errors import to_request_error
from python_acp.sessions import SessionRegistry
from python_acp.turns import (
    SESSION_UPDATE_DISPOSITIONS,
    STOP_REASON_DISPOSITIONS,
    ClientGates,
    Disposition,
    Gate,
    IdleTurnExecutor,
    TurnContext,
    TurnResult,
    UngatedClientCallError,
)


class RecordingClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs) -> None:
        self.updates.append((session_id, update))


def make_context(capabilities: ClientCapabilities | None = None) -> tuple[TurnContext, RecordingClient]:
    client = RecordingClient()
    session = SessionRegistry().create("/work")
    return TurnContext(session, client, capabilities), client  # type: ignore[arg-type]


def chunk(text: str) -> AgentMessageChunk:
    return AgentMessageChunk(
        sessionUpdate="agent_message_chunk", content=TextContentBlock(type="text", text=text)
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_a_client_that_declared_nothing_may_be_called_for_nothing() -> None:
    """The only safe reading of an absent declaration."""
    gates = ClientGates.of(None)

    assert [gate for gate in Gate if gates.allows(gate)] == []


def test_a_read_grant_does_not_satisfy_a_write() -> None:
    """`fs` is two independent booleans, and collapsing them is the easy mistake."""
    gates = ClientGates.of(
        ClientCapabilities(fs=FileSystemCapabilities(readTextFile=True, writeTextFile=False))
    )

    assert gates.allows(Gate.READ_TEXT_FILE)
    assert not gates.allows(Gate.WRITE_TEXT_FILE)


def test_terminal_is_one_gate_for_the_whole_family() -> None:
    """There is no per-method granularity in the schema; all-or-nothing is the contract."""
    assert ClientGates.of(ClientCapabilities(terminal=True)).allows(Gate.TERMINAL)
    assert not ClientGates.of(ClientCapabilities(terminal=False)).allows(Gate.TERMINAL)


@pytest.mark.parametrize(
    ("capabilities", "gate"),
    [
        (ClientCapabilities(plan=PlanCapabilities()), Gate.PLAN_UPDATES),
        (ClientCapabilities(elicitation=ElicitationCapabilities()), Gate.ELICITATION),
    ],
)
def test_marker_capabilities_are_advertised_by_presence(
    capabilities: ClientCapabilities, gate: Gate
) -> None:
    """`plan` and `elicitation` are empty models; `bool(model)` would be the wrong check."""
    assert ClientGates.of(capabilities).allows(gate)


def test_an_ungated_call_is_our_bug_not_a_bad_parameter() -> None:
    """The client is entitled to answer -32601; failing here names the omission instead."""
    gates = ClientGates.of(None)

    with pytest.raises(UngatedClientCallError) as excinfo:
        gates.require(Gate.TERMINAL)

    assert excinfo.value.gate is Gate.TERMINAL
    assert to_request_error(excinfo.value).code == -32603


def test_require_is_silent_when_the_gate_is_open() -> None:
    ClientGates.of(ClientCapabilities(terminal=True)).require(Gate.TERMINAL)


def test_every_gate_maps_to_a_field() -> None:
    """A `Gate` with no backing field would raise `AttributeError`, not answer False."""
    gates = ClientGates()

    assert all(isinstance(gates.allows(gate), bool) for gate in Gate)


def test_the_context_exposes_the_gates_at_the_reach_an_executor_has() -> None:
    context, _client = make_context(ClientCapabilities(terminal=True))

    assert context.allows(Gate.TERMINAL)
    with pytest.raises(UngatedClientCallError):
        context.require(Gate.WRITE_TEXT_FILE)


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


async def test_emit_addresses_this_turns_session_and_records_it() -> None:
    """The session id comes from the context, so an executor cannot get it wrong."""
    context, client = make_context()

    await context.emit(chunk("working"))

    assert client.updates == [(context.session_id, chunk("working"))]
    assert context.session.history == [chunk("working")]


async def test_emit_is_not_gated() -> None:
    """`ClientCapabilities` has no field for `session/update`; every client must accept it.

    Per-variant suppression is the caller's choice of *what* to emit, never a reason to
    skip the call.
    """
    context, client = make_context(None)

    await context.emit(chunk("still fine"))

    assert len(client.updates) == 1


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_the_cancel_flag_is_set_before_the_task_is_cancelled() -> None:
    """The ordering is the entire value of the flag.

    Inside an `except CancelledError` handler it is what distinguishes "the client
    cancelled this turn" from "the whole request died".
    """
    context, _client = make_context()
    observed: list[bool] = []

    async def turn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed.append(context.cancelled)
            raise

    task = asyncio.get_running_loop().create_task(turn())
    context.session.attach_turn(task)
    await asyncio.sleep(0)

    assert context.cancelled is False
    context.session.cancel_turn()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert observed == [True]


async def test_a_turn_can_race_the_cancellation_instead_of_being_torn_out_of_it() -> None:
    context, _client = make_context()
    task = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    context.session.attach_turn(task)
    waiter = asyncio.get_running_loop().create_task(context.wait_for_cancellation())
    await asyncio.sleep(0)

    context.session.cancel_turn()
    await asyncio.wait_for(waiter, timeout=5)

    assert context.cancelled is True
    task.cancel()


async def test_a_new_turn_starts_uncancelled() -> None:
    """A reused event would leave the next turn already flagged by the previous cancel."""
    context, _client = make_context()
    first = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    context.session.attach_turn(first)
    context.session.cancel_turn()
    with pytest.raises(asyncio.CancelledError):
        await first
    context.session.detach_turn()

    context.session.attach_turn(asyncio.get_running_loop().create_task(asyncio.Event().wait()))

    assert context.cancelled is False
    context.session.cancel_turn()


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


def test_a_result_carries_usage_when_there_is_some() -> None:
    """`PromptResponse.usage` exists and nothing was filling it."""
    usage = Usage(totalTokens=3, inputTokens=1, outputTokens=2)

    assert TurnResult("end_turn", usage).usage is usage
    assert TurnResult.ended() == TurnResult("end_turn", None)


def test_a_result_is_immutable() -> None:
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError is a dataclass detail
        TurnResult.ended().stop_reason = "refusal"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The interface itself
# ---------------------------------------------------------------------------


async def test_a_turn_may_be_multi_step_and_interactive() -> None:
    """A design assertion, not a behaviour one.

    `pyacp-hnk.1` requires the interface not to assume a single-step or non-interactive
    turn. This drives an executor that emits, awaits a client round trip, emits again,
    and only then returns — if a later simplification made `execute` single-shot or
    forbade client calls mid-turn, this is what would stop it.
    """

    class Interactive:
        async def execute(self, context: TurnContext, prompt: list) -> TurnResult:
            await context.emit(chunk("step 1"))
            await asyncio.sleep(0)  # stands in for a client round trip
            await context.emit(chunk("step 2"))
            await asyncio.sleep(0)
            await context.emit(chunk("step 3"))
            return TurnResult("end_turn", Usage(totalTokens=1, inputTokens=1, outputTokens=0))

    context, client = make_context()

    result = await Interactive().execute(context, [])

    assert [update for _id, update in client.updates] == [
        chunk("step 1"),
        chunk("step 2"),
        chunk("step 3"),
    ]
    assert result.usage is not None


async def test_the_default_executor_implements_the_interface() -> None:
    """Two implementers, per the bead: the shipped default and the test double above."""
    context, client = make_context()

    result = await IdleTurnExecutor().execute(context, [])

    assert result == TurnResult.ended()
    assert client.updates == []


def test_both_implementers_expose_the_same_single_async_method() -> None:
    """Structural conformance is the whole contract — `TurnExecutor` is a `Protocol`."""

    class Double:
        async def execute(self, context: TurnContext, prompt: list) -> TurnResult:
            return TurnResult.ended()

    for implementer in (IdleTurnExecutor(), Double()):
        assert inspect.iscoroutinefunction(implementer.execute)


# ---------------------------------------------------------------------------
# The session/update variant dispositions (pyacp-hnk.4)
# ---------------------------------------------------------------------------


def test_every_variant_the_sdk_defines_has_a_disposition() -> None:
    """The point of the table: nothing is silently missing.

    Walks the SDK's own union, so a release that grows a variant forces a decision
    instead of letting us inherit silence.
    """
    defined = {
        member.__name__
        for member in typing.get_args(SessionNotification.model_fields["update"].annotation)
    }
    recorded = {variant.name for variant in SESSION_UPDATE_DISPOSITIONS}

    assert defined - recorded == set(), "SDK variant with no recorded disposition"
    assert recorded - defined == set(), "disposition for a variant the SDK dropped"


def test_no_variant_is_recorded_twice() -> None:
    names = [variant.name for variant in SESSION_UPDATE_DISPOSITIONS]

    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "variant", SESSION_UPDATE_DISPOSITIONS, ids=lambda v: getattr(v, "name", str(v))
)
def test_every_disposition_says_who_and_why(variant) -> None:
    """A variant we do not send is either waiting on a named bead or structurally
    impossible. "Not done yet" with no owner is the state this table exists to prevent."""
    assert variant.owner
    assert len(variant.why) > 30
    if variant.disposition is Disposition.DEFERRED:
        assert variant.owner.startswith("pyacp-")  # pragma: no cover - none left
    if variant.disposition is Disposition.DECLINED:
        assert variant.owner == "never"


def test_nothing_is_deferred_any_more() -> None:
    """Phase 5 finished the last two. Every remaining absence is a `DECLINED` with a
    structural reason, not a "not yet"."""
    deferred = {
        v.name for v in SESSION_UPDATE_DISPOSITIONS if v.disposition is Disposition.DEFERRED
    }

    assert deferred == set()


def test_the_declined_variants_share_a_structural_reason() -> None:
    """Declined is not "unfinished" — each of these has no source and never will."""
    declined = {
        v.name for v in SESSION_UPDATE_DISPOSITIONS if v.disposition is Disposition.DECLINED
    }

    assert declined == {
        "AgentThoughtChunk",
        "AgentPlanContentUpdate",
        "AgentPlanRemovedUpdate",
        "SessionInfoUpdate",
        "UsageUpdate",
    }


# ---------------------------------------------------------------------------
# The stopReason dispositions (pyacp-hnk.5)
# ---------------------------------------------------------------------------

# The proof obligation, in the shape `tests/test_capabilities.py` uses for capability
# literals: a `stopReason` claimed as produced must name a test that watches a turn end
# that way, written `"module:test_name"` and resolved by import. `cancelled` names both
# of its routes, because they share nothing — one is a cancelled task, the other is a
# value returned from a turn nothing cancelled.
STOP_REASON_EVIDENCE: dict[str, tuple[str, ...]] = {
    "end_turn": ("test_turn_mcp_router:test_a_prompt_naming_a_tool_runs_it_and_ends_the_turn",),
    "refusal": ("test_turn_mcp_router:test_an_empty_prompt_is_refused",),
    "cancelled": (
        "test_agent:test_cancel_stops_an_in_flight_turn_with_stop_reason_cancelled",
        "test_turn_mcp_router:test_cancelling_the_prompt_ends_the_turn_as_cancelled_not_denied",
    ),
}


def test_every_stop_reason_the_sdk_defines_has_a_disposition() -> None:
    """Walks the SDK's own literal, so a release that grows one forces a decision."""
    defined = set(typing.get_args(StopReason))
    recorded = {use.name for use in STOP_REASON_DISPOSITIONS}

    assert defined - recorded == set(), "SDK stopReason with no recorded disposition"
    assert recorded - defined == set(), "disposition for a stopReason the SDK dropped"


def test_no_stop_reason_is_recorded_twice() -> None:
    names = [use.name for use in STOP_REASON_DISPOSITIONS]

    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "use", STOP_REASON_DISPOSITIONS, ids=lambda u: getattr(u, "name", str(u))
)
def test_every_stop_reason_says_who_and_why(use) -> None:
    assert use.owner
    assert len(use.why) > 30
    if use.disposition is Disposition.DEFERRED:
        assert use.owner.startswith("pyacp-")  # pragma: no cover - none exist
    if use.disposition is Disposition.DECLINED:
        assert use.owner == "never"


def test_the_declined_stop_reasons_are_the_two_limit_conditions() -> None:
    """Both are limits on a *model*, and decision D1 puts none in this runtime.

    `max_turn_requests` is the one worth stating out loud: this executor runs exactly the
    invocations the client named, so there is no agent-initiated loop whose length could
    exceed a cap. A prompt it will not run is a `refusal` before anything runs.
    """
    declined = {u.name for u in STOP_REASON_DISPOSITIONS if u.disposition is Disposition.DECLINED}

    assert declined == {"max_tokens", "max_turn_requests"}


def test_every_produced_stop_reason_names_the_test_that_watches_it() -> None:
    """A `stopReason` claimed as reachable, with nothing that ever reaches it, is the
    same failure as an aspirational capability literal."""
    produced = {u.name for u in STOP_REASON_DISPOSITIONS if u.disposition is Disposition.EMITTED}

    assert produced == set(STOP_REASON_EVIDENCE)
    for reason, references in STOP_REASON_EVIDENCE.items():
        for reference in references:
            module_name, _, test_name = reference.partition(":")
            module = importlib.import_module(module_name)
            assert callable(getattr(module, test_name, None)), (
                f"{reason} names {reference}, which does not exist"
            )


def test_the_three_named_results_are_the_three_produced_reasons() -> None:
    """An exit path reads as a name, not as a string literal, and the names agree with
    the table."""
    named = {TurnResult.ended(), TurnResult.refused(), TurnResult.cancelled()}

    assert {result.stop_reason for result in named} == set(STOP_REASON_EVIDENCE)
    assert all(result.usage is None for result in named)
