"""The ACP conformance suite: every protocol member, tested against its disposition.

`docs/acp-compliance-matrix.md` is the checklist, and this file is that document made
executable. The cases derive from one table, `CONFORMANCE`, and three structural tests
walk the SDK — the `Agent` Protocol, the router's registered routes, and
`acp.meta.AGENT_METHODS` — so that **a method with no coverage is a failure rather than
a silence**. That is what the bead asks for, and it is what made the Phase 7 removal of
the legacy surface safe to carry out (`pyacp-sld.3`): the ACP surface was proven complete
before the surface it replaced was deleted.

Adding an `Agent` member without adding a row here fails
`test_the_table_covers_every_agent_protocol_member`. Adding a row for a member that does
not exist fails the same test from the other side.

The suite drives the router in-process rather than over a transport. The wire itself is
covered by `tests/test_transport_stdio.py` (a real subprocess and the SDK's own client)
and by `pyacp-6ni.2`'s golden transcripts; what is under test *here* is dispatch and
disposition, and a subprocess per case would add minutes without adding a single
assertion.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.agent.router import build_agent_router
from acp.interfaces import Agent
from acp.meta import AGENT_METHODS
from acp.schema import AllowedOutcome, RequestPermissionResponse

from python_acp.agent import PythonAcpAgent
from python_acp.capabilities import AGENT_CAPABILITY_MANIFEST, build_agent_capabilities
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.sessions import SessionRegistry

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"

Disposition = Literal["implemented", "refuses", "unimplemented", "not-a-wire-method"]


@dataclass(frozen=True)
class Case:
    """One `acp.interfaces.Agent` member and what the wire must do about it."""

    member: str
    #: The JSON-RPC method the SDK routes to it, or `None` for a member that is not one.
    wire: str | None
    disposition: Disposition
    #: Minimal valid params, so schema validation is never what a test is measuring.
    params: dict[str, Any] = field(default_factory=dict)
    #: Registered `unstable=True`: the router refuses it outright without the flag.
    unstable: bool = False
    #: Set for a member whose `session/*` params need a live session id.
    needs_session: bool = False
    why: str = ""


#: Every member of `acp.interfaces.Agent`, in the matrix's order.
CONFORMANCE: tuple[Case, ...] = (
    Case("initialize", "initialize", "implemented", {"protocolVersion": PROTOCOL_VERSION}),
    Case(
        "authenticate",
        "authenticate",
        "refuses",
        {"methodId": "oauth"},
        why="initialize advertises no auth methods, so every methodId is one we never "
        "offered. -32000 says the method exists and the credentials do not; -32601 "
        "would say the opposite.",
    ),
    Case("new_session", "session/new", "implemented", {"cwd": "/tmp", "mcpServers": []}),
    Case(
        "load_session",
        "session/load",
        "implemented",
        {"cwd": "/tmp", "mcpServers": []},
        needs_session=True,
    ),
    Case("list_sessions", "session/list", "implemented", {}),
    Case("prompt", "session/prompt", "implemented", {"prompt": []}, needs_session=True),
    Case(
        "set_session_mode",
        "session/set_mode",
        "implemented",
        {"modeId": "dry-run"},
        needs_session=True,
    ),
    Case(
        "set_config_option",
        "session/set_config_option",
        "implemented",
        {"type": "boolean", "configId": "announce-tools", "value": False},
        needs_session=True,
    ),
    Case(
        "close_session", "session/close", "implemented", {}, unstable=True, needs_session=True
    ),
    Case(
        "fork_session",
        "session/fork",
        "implemented",
        {"cwd": "/tmp"},
        unstable=True,
        needs_session=True,
    ),
    Case(
        "resume_session",
        "session/resume",
        "implemented",
        {"cwd": "/tmp"},
        unstable=True,
        needs_session=True,
    ),
    Case("cancel", "session/cancel", "implemented", {"sessionId": "nope"}),
    Case(
        "ext_method",
        None,
        "not-a-wire-method",
        why="Reached as `_<name>`; the router strips the underscore. Deliberately empty: "
        "pyacp-sld.2 declined to put the legacy MCP passthrough here, and pyacp-sld.3 "
        "deleted it instead.",
    ),
    Case("ext_notification", None, "not-a-wire-method", why="Same mechanism, notification side."),
    Case(
        "on_connect",
        None,
        "not-a-wire-method",
        why="How the SDK hands us the Client facade. The only way to obtain it.",
    ),
)

#: Names in `AGENT_METHODS` that `build_agent_router` does **not** register, and which
#: `acp.interfaces.Agent` has no member for. Pinned so an SDK bump that starts routing one
#: is noticed rather than silently accepted — the matrix asks for exactly this.
UNROUTED = (
    "session/delete",
    "logout",
    "providers/list",
    "providers/set",
    "providers/disable",
    "nes/start",
    "nes/suggest",
    "nes/accept",
    "nes/reject",
    "nes/close",
    "document/didOpen",
    "document/didChange",
    "document/didClose",
    "document/didSave",
    "document/didFocus",
    "mcp/message",
)

#: Each advertised capability and the wire method it promises. A `true` with a broken
#: method behind it is the failure the whole capability manifest exists to prevent, and
#: this is where the promise meets the behaviour.
CAPABILITY_PROMISES: dict[tuple[str, ...], str] = {
    ("load_session",): "session/load",
    ("session_capabilities", "list"): "session/list",
    ("session_capabilities", "fork"): "session/fork",
    ("session_capabilities", "resume"): "session/resume",
    ("session_capabilities", "close"): "session/close",
}


class ConformanceClient:
    """The minimum a client must be for a turn to complete: updates and permission."""

    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", optionId="approve")
        )


def make_router(*, unstable: bool = True):
    # `on_close` wired as `cli.py` wires it. It is the entire coupling between the two
    # registries (decision B6a), so a harness that builds both and forgets it closes
    # sessions while their MCP subprocesses keep running -- invisible until 3.11 reports
    # the transport being finalized after its loop (`pyacp-6k5`).
    backends = McpBackendRegistry()
    agent = PythonAcpAgent(
        SessionRegistry(on_close=backends.close), backends=backends, unstable=unstable
    )
    agent.on_connect(ConformanceClient())  # type: ignore[arg-type]
    return build_agent_router(agent, use_unstable_protocol=unstable)


async def params_for(router, case: Case) -> dict[str, Any]:
    """A case's params, with a real session id when the method needs one."""
    if not case.needs_session:
        return dict(case.params)
    created = await router("session/new", {"cwd": "/tmp", "mcpServers": []}, False)
    return {"sessionId": created.session_id, **case.params}


def cases(*dispositions: Disposition) -> list[Case]:
    return [c for c in CONFORMANCE if c.disposition in dispositions]


def ident(case: Case) -> str:
    return case.wire or case.member


# ---------------------------------------------------------------------------
# The table is complete — this is what makes a gap detectable
# ---------------------------------------------------------------------------


def protocol_members() -> set[str]:
    """Every member `acp.interfaces.Agent` declares.

    Read off `__dict__` rather than `typing`'s `__protocol_attrs__`, which is a **3.12+
    internal**: on 3.11 it is an empty list, so a test built on it passes vacuously on the
    project's own floor. That is not hypothetical — this suite shipped that way for one
    commit and the 3.11 matrix leg caught it.
    """
    return {
        name
        for name, value in vars(Agent).items()
        if not name.startswith("_") and callable(value)
    }


def test_the_table_covers_every_agent_protocol_member() -> None:
    """Adding an `Agent` member without a row here is a failure, not a silence."""
    protocol = protocol_members()
    covered = {case.member for case in CONFORMANCE}

    assert len(protocol) == 15, "the matrix counts 15 Agent members"

    assert protocol - covered == set(), "Agent member with no conformance coverage"
    assert covered - protocol == set(), "conformance row for a member the SDK dropped"


def test_the_member_walk_does_not_depend_on_the_interpreter_version() -> None:
    """`typing.__protocol_attrs__` is 3.12+ and empty on 3.11 — the project's own floor.

    A structural test that passes vacuously on the oldest supported interpreter is worse
    than no structural test, because CI reports it green.
    """
    assert len(protocol_members()) == 15
    assert "initialize" in protocol_members()


def test_the_table_covers_every_routed_method() -> None:
    """From the router's side, so a route we stopped serving is caught too."""
    router = make_router()
    routed = set(router._requests) | set(router._notifications)
    covered = {case.wire for case in CONFORMANCE if case.wire}

    assert routed == covered


def test_every_member_is_actually_present_on_the_agent() -> None:
    """A missing member is a `-32601` the router invents, not one we chose."""
    agent = PythonAcpAgent(SessionRegistry())

    assert [c.member for c in CONFORMANCE if not callable(getattr(agent, c.member, None))] == []


def test_a_disposition_that_is_not_implemented_says_why() -> None:
    assert [c.member for c in CONFORMANCE if c.disposition != "implemented" and not c.why] == []


# ---------------------------------------------------------------------------
# Implemented methods answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", cases("implemented"), ids=ident)
async def test_implemented_methods_do_not_answer_method_not_found(case: Case) -> None:
    router = make_router()
    params = await params_for(router, case)

    is_notification = case.wire in router._notifications
    try:
        await router(case.wire, params, is_notification)
    except RequestError as exc:  # pragma: no cover - the failure path
        assert exc.code != -32601, f"{case.wire} is listed implemented but answers -32601"


# ---------------------------------------------------------------------------
# Unimplemented and refusing methods answer *correctly*, not merely fail
# ---------------------------------------------------------------------------


def test_nothing_is_left_unimplemented() -> None:
    """`pyacp-fln.3` was the last one.

    Kept as an assertion rather than deleted with its parametrize list: this is where the
    matrix's "nothing is declined" claim finally cashes, and a member regressing to
    `-32601` should read as a failure rather than as a test that quietly generates no
    cases.
    """
    assert cases("unimplemented") == []


async def test_authenticate_refuses_with_auth_required() -> None:
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router("authenticate", {"methodId": "oauth"}, False)

    assert excinfo.value.code == -32000
    assert excinfo.value.data["methodId"] == "oauth"


# ---------------------------------------------------------------------------
# The unstable three, in both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", [c for c in CONFORMANCE if c.unstable], ids=ident)
def test_unstable_methods_are_gated_by_the_flag(case: Case) -> None:
    assert make_router(unstable=True)._requests[case.wire].warn_unstable is False
    assert make_router(unstable=False)._requests[case.wire].warn_unstable is True


@pytest.mark.parametrize("case", [c for c in CONFORMANCE if c.unstable], ids=ident)
async def test_unstable_methods_are_unreachable_without_the_flag(case: Case) -> None:
    """The direction that catches a connection built wrong: the router answers without
    ever calling the agent, so a correct implementation is invisible."""
    router = make_router(unstable=False)

    with pytest.warns(UserWarning, match="unstable protocol"):
        with pytest.raises(RequestError) as excinfo:
            await router(case.wire, {"sessionId": "s1", **case.params}, False)

    assert excinfo.value.code == -32601


# ---------------------------------------------------------------------------
# Unrouted names stay unrouted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", UNROUTED)
def test_unrouted_agent_methods_are_not_registered(method: str) -> None:
    """`AGENT_METHODS` carries 28 names; the router registers 12.

    Pinned so an SDK bump that starts routing one of the other 16 is noticed rather than
    silently accepted — at which point it becomes a decision about whether to serve it.
    """
    router = make_router()

    assert method in AGENT_METHODS.values()
    assert method not in router._requests
    assert method not in router._notifications


@pytest.mark.parametrize("method", UNROUTED)
async def test_unrouted_methods_answer_method_not_found_on_the_wire(method: str) -> None:
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router(method, {}, False)

    assert excinfo.value.code == -32601


# ---------------------------------------------------------------------------
# Advertisement matches behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "wire"), sorted(CAPABILITY_PROMISES.items()), ids=lambda v: v if isinstance(v, str) else ".".join(v)
)
async def test_every_advertised_capability_has_a_working_method(
    path: tuple[str, ...], wire: str
) -> None:
    """A `true` with a broken method behind it is the failure the manifest exists to
    prevent. This is where the promise meets the behaviour."""
    capabilities: Any = build_agent_capabilities()
    for part in path:
        capabilities = getattr(capabilities, part)
    assert capabilities not in (None, False), f"{'.'.join(path)} is not advertised"

    router = make_router()
    case = next(c for c in CONFORMANCE if c.wire == wire)
    await router(wire, await params_for(router, case), False)


def test_every_capability_promise_names_a_method_in_the_table() -> None:
    covered = {case.wire for case in CONFORMANCE}

    assert set(CAPABILITY_PROMISES.values()) <= covered


def test_every_advertised_session_capability_is_promised() -> None:
    """The other direction: a capability advertised with nothing asserting it works."""
    advertised = {
        capability.path
        for capability in AGENT_CAPABILITY_MANIFEST
        if capability.is_advertised and capability.path[0] in ("load_session", "session_capabilities")
    }

    assert advertised - set(CAPABILITY_PROMISES) == {
        ("session_capabilities", "additional_directories"),
    }, "an advertised session capability with no conformance promise"


# ---------------------------------------------------------------------------
# A full lifecycle, in the order a real client walks it
# ---------------------------------------------------------------------------


async def test_a_client_can_walk_the_whole_surface_in_order() -> None:
    """Each method in isolation can pass while the sequence a client actually uses does
    not. This is that sequence."""
    router = make_router()

    await router("initialize", {"protocolVersion": PROTOCOL_VERSION}, False)
    created = await router(
        "session/new",
        {
            "cwd": "/tmp",
            "mcpServers": [
                {"name": "tools", "command": sys.executable, "args": [str(FIXTURE_SERVER)], "env": []}
            ],
        },
        False,
    )
    session_id = created.session_id

    prompted = await router(
        "session/prompt",
        {"sessionId": session_id, "prompt": [{"type": "text", "text": '{"tool": "echo"}'}]},
        False,
    )
    assert prompted.stop_reason == "end_turn"

    forked = await router("session/fork", {"sessionId": session_id, "cwd": "/tmp"}, False)
    listed = await router("session/list", {}, False)
    assert {s.session_id for s in listed.sessions} == {session_id, forked.session_id}

    await router("session/load", {"cwd": "/tmp", "sessionId": session_id, "mcpServers": []}, False)
    await router("session/resume", {"sessionId": session_id, "cwd": "/tmp"}, False)
    await router("session/cancel", {"sessionId": session_id}, True)

    for closing in (session_id, forked.session_id):
        await router("session/close", {"sessionId": closing}, False)
    assert (await router("session/list", {}, False)).sessions == []
