"""Tests for the session registry.

The two that matter most are `test_a_fork_does_not_alias_its_parents_mode_state` and its
config-option twin: `modes` and `config_options` are pydantic models mutated **in place**
by `set_mode` / `set_config_option`, so a shallow copy would let a fork's settings move
its parent's, and nothing on the wire would say so.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from acp.schema import (
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectGroup,
    SessionConfigSelectOption,
    SessionMode,
    SessionModeState,
)

from python_acp.errors import to_request_error
from python_acp.sessions import (
    Session,
    SessionRegistry,
    TurnAlreadyRunningError,
    UnknownSessionError,
)

START = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """A clock that only moves when a test says so, so ordering assertions are exact."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


def counting_ids() -> object:
    counter = iter(f"s{n}" for n in range(1, 1000))
    return lambda: next(counter)


def make_registry(**kwargs) -> tuple[SessionRegistry, FakeClock]:
    clock = FakeClock()
    return SessionRegistry(clock=clock, id_factory=counting_ids(), **kwargs), clock


def modes() -> SessionModeState:
    return SessionModeState(
        currentModeId="ask",
        availableModes=[SessionMode(id="ask", name="Ask"), SessionMode(id="code", name="Code")],
    )


def config_options() -> list:
    return [
        SessionConfigOptionBoolean(type="boolean", id="verbose", name="Verbose", currentValue=False),
        SessionConfigOptionSelect(
            type="select",
            id="model",
            name="Model",
            currentValue="fast",
            options=[
                SessionConfigSelectOption(value="fast", name="Fast"),
                SessionConfigSelectOption(value="slow", name="Slow"),
            ],
        ),
    ]


def grouped_select() -> SessionConfigOptionSelect:
    return SessionConfigOptionSelect(
        type="select",
        id="model",
        name="Model",
        currentValue="a1",
        options=[
            SessionConfigSelectGroup(
                group="alpha",
                name="Alpha",
                options=[SessionConfigSelectOption(value="a1", name="A1")],
            ),
            SessionConfigSelectGroup(
                group="beta",
                name="Beta",
                options=[SessionConfigSelectOption(value="b1", name="B1")],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Creation and lookup
# ---------------------------------------------------------------------------


def test_a_new_session_carries_the_whole_metadata_set() -> None:
    registry, clock = make_registry()

    session = registry.create(
        "/work",
        additional_directories=["/extra"],
        modes=modes(),
        config_options=config_options(),
        title="a title",
    )

    assert session.session_id == "s1"
    assert session.cwd == "/work"
    assert session.additional_directories == ("/extra",)
    assert session.modes.current_mode_id == "ask"
    assert [option.id for option in session.config_options] == ["verbose", "model"]
    assert session.created_at == session.updated_at == clock.now


def test_a_session_is_addressable_by_id() -> None:
    registry, _ = make_registry()
    session = registry.create("/work")

    assert registry.get(session.session_id) is session
    assert session.session_id in registry
    assert len(registry) == 1


def test_an_unknown_id_raises_rather_than_returning_none() -> None:
    """`None` would push the failure frames away from the request that caused it."""
    registry, _ = make_registry()

    with pytest.raises(UnknownSessionError) as excinfo:
        registry.get("nope")

    assert excinfo.value.session_id == "nope"


def test_an_unknown_session_reaches_the_client_as_invalid_params() -> None:
    """The point of subclassing ValueError: `errors.py` maps it with no special case."""
    error = to_request_error(UnknownSessionError("nope"))

    assert error.code == -32602
    assert "nope" in error.data["reason"]


def test_the_registry_does_not_validate_paths() -> None:
    """`pyacp-3rw.4` owns that, at the edge where a bad value must become -32602."""
    registry, _ = make_registry()

    assert registry.create("relative/path").cwd == "relative/path"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def test_setting_a_mode_moves_the_current_id_and_the_timestamp() -> None:
    registry, clock = make_registry()
    session = registry.create("/work", modes=modes())
    clock.advance()

    assert session.set_mode("code").name == "Code"
    assert session.modes.current_mode_id == "code"
    assert session.updated_at == clock.now
    assert session.created_at < session.updated_at


def test_an_unknown_mode_is_refused() -> None:
    registry, _ = make_registry()
    session = registry.create("/work", modes=modes())

    with pytest.raises(ValueError, match="Unknown mode"):
        session.set_mode("plan")


def test_a_session_with_no_modes_refuses_to_switch() -> None:
    """`pyacp-fln.2` emits `current_mode_update` after this; an unoffered mode must not."""
    registry, _ = make_registry()
    session = registry.create("/work")

    with pytest.raises(ValueError, match="no modes"):
        session.set_mode("ask")


# ---------------------------------------------------------------------------
# Config options
# ---------------------------------------------------------------------------


def test_a_boolean_option_takes_a_bool() -> None:
    registry, _ = make_registry()
    session = registry.create("/work", config_options=config_options())

    assert session.set_config_option("verbose", True).current_value is True


def test_a_select_option_takes_one_of_its_values() -> None:
    registry, _ = make_registry()
    session = registry.create("/work", config_options=config_options())

    assert session.set_config_option("model", "slow").current_value == "slow"


def test_a_grouped_select_accepts_values_nested_in_its_groups() -> None:
    """`options` is a list of options *or* a list of groups; only the second nests.

    Validating against the top level alone would reject every value a grouped option has.
    """
    registry, _ = make_registry()
    session = registry.create("/work", config_options=[grouped_select()])

    assert session.set_config_option("model", "b1").current_value == "b1"


@pytest.mark.parametrize(
    ("config_id", "value", "message"),
    [
        ("verbose", "yes", "is boolean"),
        ("model", True, "is a select"),
        ("model", "enormous", "Unknown value"),
        ("nope", True, "Unknown config option"),
    ],
)
def test_a_config_option_refuses_what_it_cannot_hold(
    config_id: str, value: object, message: str
) -> None:
    registry, _ = make_registry()
    session = registry.create("/work", config_options=config_options())

    with pytest.raises(ValueError, match=message):
        session.set_config_option(config_id, value)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fork copies, resume shares
# ---------------------------------------------------------------------------


def _never_finishing_task() -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(asyncio.Event().wait())


def test_a_fork_gets_a_new_id_and_the_parents_metadata() -> None:
    registry, _ = make_registry()
    parent = registry.create("/work", additional_directories=["/extra"], modes=modes())

    forked = registry.fork(parent.session_id)

    assert forked.session_id != parent.session_id
    assert forked.cwd == "/work"
    assert forked.additional_directories == ("/extra",)
    assert len(registry) == 2


def test_a_fork_does_not_alias_its_parents_mode_state() -> None:
    """`set_mode` mutates the model in place; a shallow copy would move both."""
    registry, _ = make_registry()
    parent = registry.create("/work", modes=modes())

    forked = registry.fork(parent.session_id)
    forked.set_mode("code")

    assert forked.modes.current_mode_id == "code"
    assert parent.modes.current_mode_id == "ask"


def test_a_fork_does_not_alias_its_parents_config_options() -> None:
    registry, _ = make_registry()
    parent = registry.create("/work", config_options=config_options())

    forked = registry.fork(parent.session_id)
    forked.set_config_option("verbose", True)

    assert forked.config_option("verbose").current_value is True
    assert parent.config_option("verbose").current_value is False


def test_a_fork_may_override_cwd_and_directories() -> None:
    registry, _ = make_registry()
    parent = registry.create("/work", additional_directories=["/extra"])

    forked = registry.fork(parent.session_id, cwd="/elsewhere", additional_directories=[])

    assert (forked.cwd, forked.additional_directories) == ("/elsewhere", ())
    assert (parent.cwd, parent.additional_directories) == ("/work", ("/extra",))


async def test_a_fork_starts_idle_even_when_its_parent_is_mid_turn() -> None:
    """A running task belongs to the request that started it, not to the state."""
    registry, _ = make_registry()
    parent = registry.create("/work")
    parent.attach_turn(_never_finishing_task())
    try:
        forked = registry.fork(parent.session_id)

        assert parent.turn_is_running is True
        assert forked.turn_is_running is False
    finally:
        parent.cancel_turn()


def test_resume_returns_the_same_session_marked_active() -> None:
    registry, clock = make_registry()
    session = registry.create("/work")
    clock.advance()

    resumed = registry.resume(session.session_id)

    assert resumed is session
    assert resumed.updated_at == clock.now
    assert len(registry) == 1


def test_resuming_an_unknown_session_is_the_same_error_as_getting_one() -> None:
    registry, _ = make_registry()

    with pytest.raises(UnknownSessionError):
        registry.resume("nope")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_listing_is_most_recently_active_first() -> None:
    """Ordering lives here so a Phase 2.4 cursor means the same thing to every caller."""
    registry, clock = make_registry()
    first = registry.create("/work")
    clock.advance()
    second = registry.create("/work")
    clock.advance()
    first.touch()

    assert [s.session_id for s in registry.list()] == [first.session_id, second.session_id]


def test_listing_can_be_filtered_by_cwd() -> None:
    registry, _ = make_registry()
    here = registry.create("/here")
    registry.create("/there")

    assert [s.session_id for s in registry.list(cwd="/here")] == [here.session_id]


def test_session_info_is_the_schema_shape_with_an_iso_timestamp() -> None:
    registry, _ = make_registry()
    session = registry.create("/work", additional_directories=["/extra"], title="t")

    info = session.to_info()

    assert info.session_id == session.session_id
    assert info.cwd == "/work"
    assert info.additional_directories == ["/extra"]
    assert info.title == "t"
    assert info.updated_at == START.isoformat()


def test_no_additional_directories_is_absent_rather_than_empty() -> None:
    assert Session(session_id="s", cwd="/work").to_info().additional_directories is None


# ---------------------------------------------------------------------------
# The in-flight turn
# ---------------------------------------------------------------------------


async def test_a_second_turn_is_refused_while_the_first_runs() -> None:
    """Two turns on one session would interleave session/update with nothing to sort them."""
    session = Session(session_id="s", cwd="/work")
    session.attach_turn(_never_finishing_task())
    try:
        with pytest.raises(TurnAlreadyRunningError):
            session.attach_turn(_never_finishing_task())
    finally:
        session.cancel_turn()


async def test_cancelling_reaches_the_running_turn() -> None:
    session = Session(session_id="s", cwd="/work")
    turn = _never_finishing_task()
    session.attach_turn(turn)

    assert session.cancel_turn() is True
    with pytest.raises(asyncio.CancelledError):
        await turn


async def test_cancelling_an_idle_session_is_not_an_error() -> None:
    """`session/cancel` is a notification: a client cancelling a finished turn is correct."""
    session = Session(session_id="s", cwd="/work")

    assert session.cancel_turn() is False


async def test_a_detached_turn_leaves_the_session_idle() -> None:
    session = Session(session_id="s", cwd="/work")
    turn = _never_finishing_task()
    session.attach_turn(turn)
    turn.cancel()
    session.detach_turn()

    assert session.turn_is_running is False
    session.attach_turn(_never_finishing_task())
    session.cancel_turn()


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


async def test_closing_removes_the_session_and_runs_the_hook() -> None:
    closed: list[str] = []
    registry, _ = make_registry(on_close=lambda session_id: _record(closed, session_id))
    session = registry.create("/work")

    await registry.close(session.session_id)

    assert closed == [session.session_id]
    assert session.session_id not in registry
    with pytest.raises(UnknownSessionError):
        registry.get(session.session_id)


async def test_closing_cancels_a_running_turn() -> None:
    registry, _ = make_registry()
    session = registry.create("/work")
    turn = _never_finishing_task()
    session.attach_turn(turn)

    await registry.close(session.session_id)

    with pytest.raises(asyncio.CancelledError):
        await turn


async def test_a_failing_close_hook_still_leaves_the_session_gone() -> None:
    """Half-closed is the worst outcome: addressable, but with its backends torn down."""

    async def explode(session_id: str) -> None:
        raise RuntimeError("teardown failed")

    registry, _ = make_registry(on_close=explode)
    session = registry.create("/work")

    with pytest.raises(RuntimeError, match="teardown failed"):
        await registry.close(session.session_id)

    assert session.session_id not in registry


async def test_closing_an_unknown_session_is_an_error() -> None:
    registry, _ = make_registry()

    with pytest.raises(UnknownSessionError):
        await registry.close("nope")


async def test_close_all_empties_the_registry() -> None:
    closed: list[str] = []
    registry, _ = make_registry(on_close=lambda session_id: _record(closed, session_id))
    registry.create("/a")
    registry.create("/b")

    await registry.close_all()

    assert len(registry) == 0
    assert sorted(closed) == ["s1", "s2"]


async def _record(sink: list[str], session_id: str) -> None:
    sink.append(session_id)
