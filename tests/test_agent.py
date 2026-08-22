"""Tests for the ACP agent skeleton.

These drive `PythonAcpAgent` through the SDK's own router rather than calling its
methods directly. That is the point: the contract under test is "the SDK can dispatch
to this object", and a signature the router cannot splat into is the failure mode a
direct call would hide.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.agent.router import build_agent_router
from acp.schema import (
    AgentMessageChunk,
    ClientCapabilities,
    FileSystemCapabilities,
    StopReason,
    TextContentBlock,
)

from python_acp import __version__
from python_acp.agent import PythonAcpAgent
from python_acp.capabilities import SUPPORTED_PROTOCOL_VERSIONS, build_agent_capabilities
from python_acp.errors import as_request_error
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPProtocolError
from python_acp.sessions import SessionRegistry
from python_acp.turns import TurnContext

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"

# Every wire method the SDK routes to an agent, and whether it is gated behind
# use_unstable_protocol. Derived from docs/acp-compliance-matrix.md.
ROUTED_REQUESTS = {
    "initialize": False,
    "authenticate": False,
    "session/new": False,
    "session/load": False,
    "session/list": False,
    "session/prompt": False,
    "session/set_mode": False,
    "session/set_config_option": False,
    "session/close": True,
    "session/fork": True,
    "session/resume": True,
}

# Minimal valid params per method, so validation is never what fails a test.
PARAMS = {
    "initialize": {"protocolVersion": PROTOCOL_VERSION},
    "authenticate": {"methodId": "oauth"},
    # mcpServers is required on the wire for these two, despite being optional in
    # the Protocol signature — the request model has no default for it.
    "session/new": {"cwd": "/tmp", "mcpServers": []},
    "session/load": {"cwd": "/tmp", "sessionId": "s1", "mcpServers": []},
    "session/list": {},
    "session/prompt": {"sessionId": "s1", "prompt": []},
    "session/set_mode": {"sessionId": "s1", "modeId": "ask"},
    "session/set_config_option": {
        "type": "boolean",
        "configId": "c1",
        "sessionId": "s1",
        "value": True,
    },
    "session/close": {"sessionId": "s1"},
    "session/fork": {"sessionId": "s1", "cwd": "/tmp"},
    "session/resume": {"sessionId": "s1", "cwd": "/tmp"},
}


def make_agent(**kwargs) -> PythonAcpAgent:
    """A fresh agent over a fresh registry. Sharing one would let tests leak sessions.

    `is None` rather than `or`: an empty `SessionRegistry` is falsy — it defines
    `__len__` — so `registry or SessionRegistry()` would silently discard the one a test
    passed in and hand back a different, empty one.
    """
    sessions = kwargs.pop("sessions", None)
    return PythonAcpAgent(SessionRegistry() if sessions is None else sessions, **kwargs)


def make_router(*, unstable: bool = True, agent: PythonAcpAgent | None = None):
    return build_agent_router(agent or make_agent(), use_unstable_protocol=unstable)


def test_every_protocol_member_is_present() -> None:
    """A missing member is a silent -32601, so assert on the class, not the wire."""
    agent = make_agent()
    members = [
        "initialize",
        "new_session",
        "load_session",
        "list_sessions",
        "set_session_mode",
        "set_config_option",
        "authenticate",
        "prompt",
        "fork_session",
        "resume_session",
        "close_session",
        "cancel",
        "ext_method",
        "ext_notification",
        "on_connect",
    ]
    assert [m for m in members if not callable(getattr(agent, m, None))] == []


def test_the_sdk_routes_every_agent_method_to_us() -> None:
    """No route may fall back to the router's absent-attribute -32601."""
    router = make_router()
    assert set(router._requests) == set(ROUTED_REQUESTS)
    assert set(router._notifications) == {"session/cancel"}
    assert [m for m, route in router._requests.items() if route.func is None] == []


async def test_initialize_identifies_this_agent() -> None:
    router = make_router()

    result = await router("initialize", PARAMS["initialize"], False)

    assert result.protocol_version == PROTOCOL_VERSION
    assert result.agent_info.name == "python-acp"
    assert result.agent_info.version == __version__


async def test_initialize_advertises_the_manifest_and_nothing_else() -> None:
    """What reaches the wire is the manifest, not a block assembled beside it.

    `tests/test_capabilities.py` owns *which* literals are correct and why. This owns
    the join: `initialize` must not be able to answer with a different block.
    """
    router = make_router()

    result = await router("initialize", PARAMS["initialize"], False)

    assert result.agent_capabilities == build_agent_capabilities()
    assert result.auth_methods == []


async def test_initialize_promises_exactly_what_is_built() -> None:
    """Spelled out per field, so the promise is legible without running the walker."""
    router = make_router()

    capabilities = (await router("initialize", PARAMS["initialize"], False)).agent_capabilities

    # Built: the session lifecycle (pyacp-3rw.2, pyacp-3rw.3).
    assert capabilities.load_session is True
    assert capabilities.session_capabilities.list is not None
    assert capabilities.session_capabilities.fork is not None
    assert capabilities.session_capabilities.resume is not None
    assert capabilities.session_capabilities.close is not None
    # Not built: content-block handling is pyacp-hnk.3's.
    assert capabilities.prompt_capabilities.image is False
    assert capabilities.prompt_capabilities.audio is False
    assert capabilities.prompt_capabilities.embedded_context is False
    # Never: transports we do not drive, and members the SDK does not route.
    assert capabilities.mcp_capabilities.http is False
    assert capabilities.mcp_capabilities.sse is False
    assert capabilities.mcp_capabilities.acp is False
    assert capabilities.session_capabilities.delete is None
    assert capabilities.auth.logout is None
    assert capabilities.providers is None
    assert capabilities.nes is None
    assert capabilities.position_encoding is None
    # Not built yet: pyacp-3rw.4 is what enforces the absolute-path constraint.
    assert capabilities.session_capabilities.additional_directories is None


async def test_the_capability_block_is_not_shared_between_connections() -> None:
    """A client that mutated its response must not be able to reach the next one's."""
    first = (await make_router()("initialize", PARAMS["initialize"], False)).agent_capabilities
    first.prompt_capabilities.image = True
    first.session_capabilities.fork.field_meta = {"mutated": True}

    second = (await make_router()("initialize", PARAMS["initialize"], False)).agent_capabilities

    assert second.prompt_capabilities.image is False
    assert second.session_capabilities.fork.field_meta is None


@pytest.mark.parametrize("requested", sorted(SUPPORTED_PROTOCOL_VERSIONS))
async def test_a_supported_protocol_version_is_echoed_back(requested: int) -> None:
    router = make_router()

    result = await router("initialize", {"protocolVersion": requested}, False)

    assert result.protocol_version == requested


@pytest.mark.parametrize("requested", [0, PROTOCOL_VERSION + 1, 9999])
async def test_an_unsupported_protocol_version_is_answered_not_rejected(requested: int) -> None:
    """The spec has the client decide whether our version is usable, not us."""
    router = make_router()

    result = await router("initialize", {"protocolVersion": requested}, False)

    assert result.protocol_version == max(SUPPORTED_PROTOCOL_VERSIONS)


async def test_initialize_stores_client_capabilities_for_phase_4() -> None:
    agent = make_agent()
    router = build_agent_router(agent, use_unstable_protocol=True)
    assert agent.client_capabilities is None

    await router(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True}, "terminal": True},
        },
        False,
    )

    caps = agent.client_capabilities
    assert isinstance(caps, ClientCapabilities)
    assert isinstance(caps.fs, FileSystemCapabilities)
    assert caps.fs.read_text_file is True
    assert caps.fs.write_text_file is False
    assert caps.terminal is True


async def test_client_capabilities_are_per_connection() -> None:
    """One agent instance serves one connection, so this is where "per-connection" lives.

    Phase 4 gates `fs/*` and `terminal/*` on these. Two clients declaring different
    capabilities must not be able to unlock each other's calls.
    """
    permissive, restricted = make_agent(), make_agent()

    await build_agent_router(permissive, use_unstable_protocol=True)(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {"terminal": True}},
        False,
    )
    await build_agent_router(restricted, use_unstable_protocol=True)(
        "initialize", {"protocolVersion": PROTOCOL_VERSION}, False
    )

    assert permissive.client_capabilities.terminal is True
    assert restricted.client_capabilities.terminal is False


async def test_a_client_that_declares_nothing_is_not_the_same_as_no_initialize() -> None:
    """`None` means the handshake has not happened; a defaults-only object means it has."""
    agent = make_agent()
    assert agent.client_capabilities is None

    await build_agent_router(agent, use_unstable_protocol=True)(
        "initialize", {"protocolVersion": PROTOCOL_VERSION}, False
    )

    assert isinstance(agent.client_capabilities, ClientCapabilities)


async def test_meta_keys_do_not_break_dispatch() -> None:
    """The router splats _meta in as kwargs; a closed signature would TypeError."""
    router = make_router()

    result = await router(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "_meta": {"traceId": "abc"}},
        False,
    )

    assert result.protocol_version == PROTOCOL_VERSION


# Methods with a body. Everything else still answers -32601, and moving a name here is
# the same commit as implementing it.
# Only the two Phase 5 methods are left unbuilt.
IMPLEMENTED = set(ROUTED_REQUESTS) - {"session/set_mode", "session/set_config_option"}


@pytest.mark.parametrize("method", [m for m in ROUTED_REQUESTS if m not in IMPLEMENTED])
async def test_unbuilt_methods_answer_method_not_found(method: str) -> None:
    """Until its phase lands, a member's wire behaviour matches an absent one."""
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router(method, PARAMS[method], False)

    assert excinfo.value.code == -32601
    assert excinfo.value.data == {"method": method}


def test_unstable_methods_are_only_routed_with_the_flag() -> None:
    """Without use_unstable_protocol the router never calls us at all."""
    gated = [m for m, unstable in ROUTED_REQUESTS.items() if unstable]

    on = make_router(unstable=True)
    off = make_router(unstable=False)

    assert [m for m in gated if on._requests[m].warn_unstable] == []
    assert [m for m in gated if not off._requests[m].warn_unstable] == []


@pytest.mark.parametrize("method", [m for m, unstable in ROUTED_REQUESTS.items() if unstable])
async def test_unstable_methods_are_unreachable_without_the_flag(method: str) -> None:
    router = make_router(unstable=False)

    with pytest.warns(UserWarning, match="unstable protocol"):
        with pytest.raises(RequestError) as excinfo:
            await router(method, PARAMS[method], False)

    assert excinfo.value.code == -32601


async def test_authenticate_refuses_with_auth_required_not_method_not_found() -> None:
    """The method exists; the credentials do not. -32601 would say the opposite."""
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router("authenticate", PARAMS["authenticate"], False)

    assert excinfo.value.code == -32000
    assert excinfo.value.data["methodId"] == "oauth"


async def test_cancel_is_silent_for_an_unknown_session() -> None:
    """A notification has no reply channel, so it must not raise."""
    router = make_router()

    assert await router("session/cancel", {"sessionId": "nope"}, True) is None


async def test_unknown_extension_request_is_method_not_found() -> None:
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router("_experimental/thing", {}, False)

    assert excinfo.value.code == -32601


async def test_unknown_extension_notification_is_silent() -> None:
    router = make_router()

    assert await router("_experimental/thing", {}, True) is None


def test_client_handle_is_stored_on_connect() -> None:
    agent = make_agent()
    sentinel = object()

    with pytest.raises(RuntimeError, match="on_connect"):
        _ = agent.client

    agent.on_connect(sentinel)  # type: ignore[arg-type]
    assert agent.client is sentinel


# ---------------------------------------------------------------------------
# Error mapping (pyacp-tzd.6)
# ---------------------------------------------------------------------------

# Every routed request maps to one of these members. `session/cancel` is absent
# deliberately: it is a notification, and a notification that raises is already the bug.
REQUEST_MEMBERS = [
    "initialize",
    "authenticate",
    "new_session",
    "load_session",
    "list_sessions",
    "fork_session",
    "resume_session",
    "close_session",
    "set_session_mode",
    "set_config_option",
    "prompt",
    "ext_method",
]


@pytest.mark.parametrize("member", REQUEST_MEMBERS)
def test_every_request_method_maps_its_errors(member: str) -> None:
    """An undecorated method leaks to `acp.Connection`, which flattens it to -32603."""
    assert getattr(getattr(PythonAcpAgent, member), "maps_errors", False) is True


@pytest.mark.parametrize("member", ["cancel", "ext_notification"])
def test_notification_handlers_are_not_decorated(member: str) -> None:
    """There is no reply channel to map an error onto; silence is the contract."""
    assert getattr(getattr(PythonAcpAgent, member), "maps_errors", False) is False


async def test_a_backend_failure_reaches_the_client_with_its_own_code() -> None:
    """The whole point of the mapping: -32601 from the MCP server is not -32603 from us."""

    # The override carries the decorator explicitly: it lives on the function, so a
    # subclass that replaces a method replaces the guard with it. Production code fills
    # these bodies in place rather than by overriding, which is what
    # `test_every_request_method_maps_its_errors` checks.
    class BackendFails(PythonAcpAgent):
        @as_request_error
        async def list_sessions(self, cursor=None, **kwargs):  # type: ignore[override]
            raise MCPProtocolError.from_error_response(
                {"code": -32601, "message": "Unknown tool"}
            )

    router = build_agent_router(BackendFails(SessionRegistry()), use_unstable_protocol=True)

    with pytest.raises(RequestError) as excinfo:
        await router("session/list", {}, False)

    assert excinfo.value.code == -32601
    assert excinfo.value.data["source"] == "mcp"


async def test_a_bad_parameter_from_below_becomes_invalid_params() -> None:
    """A ValueError would otherwise reach the client as -32603, not -32602."""

    class Picky(PythonAcpAgent):
        @as_request_error
        async def list_sessions(self, cursor=None, **kwargs):  # type: ignore[override]
            raise ValueError("'cursor' must be a string")

    router = build_agent_router(Picky(SessionRegistry()), use_unstable_protocol=True)

    with pytest.raises(RequestError) as excinfo:
        await router("session/list", {}, False)

    assert excinfo.value.code == -32602
    assert excinfo.value.data == {"reason": "'cursor' must be a string"}


# ---------------------------------------------------------------------------
# Baseline session lifecycle (pyacp-3rw.2)
# ---------------------------------------------------------------------------


class RecordingExecutor:
    """A turn that emits what it is told to, then stops on command.

    `started` lets a test wait until the turn is genuinely running before cancelling it —
    without that, a cancel can land before `attach_turn` and pass for the wrong reason.
    """

    def __init__(self, stop_reason: StopReason = "end_turn", updates: list | None = None) -> None:
        self.stop_reason = stop_reason
        self.updates = updates or []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.seen_prompts: list[list] = []
        self.cancelled = False

    async def execute(self, context, prompt):
        self.seen_prompts.append(prompt)
        for update in self.updates:
            await context.emit(update)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.stop_reason


class RecordingClient:
    """Captures `session/update` calls the way the SDK's Client facade would receive them."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs) -> None:
        self.updates.append((session_id, update))


async def test_new_session_registers_a_session_and_returns_its_id() -> None:
    registry = SessionRegistry()
    router = make_router(agent=make_agent(sessions=registry))

    result = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    assert result.session_id in registry
    assert registry.get(result.session_id).cwd == "/work"
    # Nothing offers modes or config options yet (pyacp-fln.2, pyacp-fln.3).
    assert result.modes is None
    assert result.config_options is None


async def test_new_session_keeps_additional_directories() -> None:
    registry = SessionRegistry()
    router = make_router(agent=make_agent(sessions=registry))

    result = await router(
        "session/new",
        {"cwd": "/work", "additionalDirectories": ["/extra"], "mcpServers": []},
        False,
    )

    assert registry.get(result.session_id).additional_directories == ("/extra",)


async def test_new_session_opens_the_stdio_servers_it_was_given() -> None:
    """stdio needs no capability, so it is the one transport that may be handed to us."""
    backends = McpBackendRegistry()
    router = make_router(agent=make_agent(backends=backends))

    result = await router(
        "session/new",
        {
            "cwd": "/work",
            "mcpServers": [
                {
                    "name": "tools",
                    "command": sys.executable,
                    "args": [str(FIXTURE_SERVER)],
                    "env": [],
                }
            ],
        },
        False,
    )
    try:
        opened = backends.backends(result.session_id)
        assert list(opened) == ["tools"]
        assert [tool["name"] for tool in await opened["tools"].list_tools()] == ["echo"]
    finally:
        await backends.close_all()


async def test_a_backend_that_cannot_start_takes_the_session_with_it() -> None:
    """A session id whose tools silently do not exist is the failure this path prevents."""
    sessions = SessionRegistry()
    backends = McpBackendRegistry()
    router = make_router(agent=make_agent(sessions=sessions, backends=backends))

    with pytest.raises(RequestError):
        await router(
            "session/new",
            {
                "cwd": "/work",
                "mcpServers": [
                    {"name": "broken", "command": sys.executable, "args": ["-c", "pass"], "env": []}
                ],
            },
            False,
        )

    assert len(sessions) == 0
    assert len(backends) == 0


@pytest.mark.parametrize(
    "server",
    [
        {"type": "http", "name": "remote", "url": "https://example.invalid", "headers": []},
        {"type": "sse", "name": "streamy", "url": "https://example.invalid", "headers": []},
    ],
)
async def test_new_session_rejects_transports_initialize_never_advertised(server: dict) -> None:
    """mcpCapabilities.http/.sse are false; accepting one would make that a lie."""
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router("session/new", {"cwd": "/work", "mcpServers": [server]}, False)

    assert excinfo.value.code == -32602
    assert server["name"] in excinfo.value.data["reason"]


async def test_a_malformed_mcp_server_is_dropped_before_we_see_it() -> None:
    """A hazard inherited from the SDK, pinned so `pyacp-db3` does not rediscover it.

    `NewSessionRequest.mcp_servers` carries a `skip_invalid_items` wrap validator, so an
    entry that fails validation — a stdio server missing the required `env`, say — is
    silently removed from the list. The agent cannot refuse what never arrives, and the
    client gets a session whose server is simply absent. Refusing the *well-formed*
    transports we do not advertise is the part we can control.
    """
    registry = SessionRegistry()
    router = make_router(agent=make_agent(sessions=registry))

    result = await router(
        "session/new",
        {"cwd": "/work", "mcpServers": [{"name": "tools", "command": "/bin/true"}]},
        False,
    )

    assert result.session_id in registry


async def test_prompt_runs_the_executor_and_returns_its_stop_reason() -> None:
    executor = RecordingExecutor(stop_reason="end_turn")
    executor.release.set()
    agent = make_agent(executor=executor)
    agent.on_connect(RecordingClient())  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    result = await router(
        "session/prompt",
        {"sessionId": created.session_id, "prompt": [{"type": "text", "text": "hi"}]},
        False,
    )

    assert result.stop_reason == "end_turn"
    assert len(executor.seen_prompts) == 1


async def test_a_turn_streams_session_updates_through_the_client_handle() -> None:
    """`session/update` has no capability gate; every ACP client must accept it."""
    chunk = AgentMessageChunk(sessionUpdate="agent_message_chunk", content=TextContentBlock(type="text", text="working"))
    executor = RecordingExecutor(updates=[chunk])
    executor.release.set()
    client = RecordingClient()
    agent = make_agent(executor=executor)
    agent.on_connect(client)  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    await router(
        "session/prompt", {"sessionId": created.session_id, "prompt": []}, False
    )

    assert client.updates == [(created.session_id, chunk)]


async def test_the_turn_cannot_address_another_session() -> None:
    """The context supplies the session id, so an executor cannot get it wrong."""
    client = RecordingClient()
    session = SessionRegistry().create("/work")
    context = TurnContext(session, client)  # type: ignore[arg-type]

    await context.emit("anything")

    assert client.updates == [(session.session_id, "anything")]


async def test_prompting_an_unknown_session_is_invalid_params() -> None:
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router("session/prompt", {"sessionId": "nope", "prompt": []}, False)

    assert excinfo.value.code == -32602
    assert "nope" in excinfo.value.data["reason"]


async def test_cancel_stops_an_in_flight_turn_with_stop_reason_cancelled() -> None:
    """The whole point of running the turn as a task: a notification can reach it."""
    executor = RecordingExecutor()
    agent = make_agent(executor=executor)
    agent.on_connect(RecordingClient())  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    turn = asyncio.create_task(
        router("session/prompt", {"sessionId": created.session_id, "prompt": []}, False)
    )
    await asyncio.wait_for(executor.started.wait(), timeout=5)
    await router("session/cancel", {"sessionId": created.session_id}, True)

    result = await asyncio.wait_for(turn, timeout=5)
    assert result.stop_reason == "cancelled"
    assert executor.cancelled is True


async def test_a_cancelled_session_is_idle_again_afterwards() -> None:
    """`detach_turn` runs in a finally, so a cancelled session accepts the next prompt."""
    executor = RecordingExecutor()
    registry = SessionRegistry()
    agent = make_agent(sessions=registry, executor=executor)
    agent.on_connect(RecordingClient())  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    turn = asyncio.create_task(
        router("session/prompt", {"sessionId": created.session_id, "prompt": []}, False)
    )
    await asyncio.wait_for(executor.started.wait(), timeout=5)
    await router("session/cancel", {"sessionId": created.session_id}, True)
    await asyncio.wait_for(turn, timeout=5)

    assert registry.get(created.session_id).turn_is_running is False


async def test_a_second_concurrent_prompt_is_refused() -> None:
    """Two turns on one session would interleave session/update with nothing to sort them."""
    executor = RecordingExecutor()
    agent = make_agent(executor=executor)
    agent.on_connect(RecordingClient())  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    first = asyncio.create_task(
        router("session/prompt", {"sessionId": created.session_id, "prompt": []}, False)
    )
    await asyncio.wait_for(executor.started.wait(), timeout=5)
    try:
        with pytest.raises(RequestError) as excinfo:
            await router(
                "session/prompt", {"sessionId": created.session_id, "prompt": []}, False
            )
        assert excinfo.value.code == -32603
    finally:
        await router("session/cancel", {"sessionId": created.session_id}, True)
        await asyncio.wait_for(first, timeout=5)


async def test_cancelling_a_session_with_no_turn_running_is_silent() -> None:
    """A client cancelling a turn that already finished is behaving correctly."""
    registry = SessionRegistry()
    router = make_router(agent=make_agent(sessions=registry))
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    assert await router("session/cancel", {"sessionId": created.session_id}, True) is None


async def test_cancelling_an_unknown_session_is_silent() -> None:
    """A notification has no reply channel, so there is nowhere to report the id."""
    router = make_router()

    assert await router("session/cancel", {"sessionId": "nope"}, True) is None


async def test_an_executor_that_raises_becomes_a_mapped_error() -> None:
    class Exploding:
        async def execute(self, context, prompt):
            raise ValueError("the turn disagreed")

    agent = make_agent(executor=Exploding())
    agent.on_connect(RecordingClient())  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    with pytest.raises(RequestError) as excinfo:
        await router("session/prompt", {"sessionId": created.session_id, "prompt": []}, False)

    assert excinfo.value.code == -32602
    assert excinfo.value.data == {"reason": "the turn disagreed"}


async def test_the_default_executor_completes_a_turn_without_doing_anything() -> None:
    """A conforming no-op is the honest answer while pyacp-hnk.2 has not shipped."""
    agent = make_agent()
    agent.on_connect(RecordingClient())  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    result = await router(
        "session/prompt", {"sessionId": created.session_id, "prompt": []}, False
    )

    assert result.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Extended session lifecycle (pyacp-3rw.3)
# ---------------------------------------------------------------------------


async def _lifecycle_agent(**kwargs):
    """An agent with a connected recording client and a session already created."""
    agent = make_agent(**kwargs)
    client = RecordingClient()
    agent.on_connect(client)  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)
    return agent, router, client, created.session_id


async def test_load_replays_the_sessions_transcript() -> None:
    """Evidence for `agentCapabilities.loadSession`.

    The replay must go out *before* the response: a client that got the result first
    would have no way to tell replayed updates from live ones on a running session.
    """
    chunk = AgentMessageChunk(
        sessionUpdate="agent_message_chunk",
        content=TextContentBlock(type="text", text="earlier"),
    )
    executor = RecordingExecutor(updates=[chunk])
    executor.release.set()
    agent, router, client, session_id = await _lifecycle_agent(executor=executor)
    await router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    client.updates.clear()

    result = await router(
        "session/load", {"cwd": "/work", "sessionId": session_id, "mcpServers": []}, False
    )

    assert client.updates == [(session_id, chunk)]
    # `session/load` and `session/close` are the two routes the SDK registers with
    # `adapt_result=normalize_result`, so an optional response arrives as a plain dict.
    assert result == {}


async def test_load_on_a_session_we_no_longer_hold_is_invalid_params() -> None:
    """`loadSession: true` claims the method works, not that a session outlives us."""
    _agent, router, _client, _session_id = await _lifecycle_agent()

    with pytest.raises(RequestError) as excinfo:
        await router("session/load", {"cwd": "/work", "sessionId": "gone", "mcpServers": []}, False)

    assert excinfo.value.code == -32602


async def test_list_sessions_pages_most_recent_first() -> None:
    """Evidence for `sessionCapabilities.list`."""
    sessions = SessionRegistry()
    router = make_router(agent=make_agent(sessions=sessions))
    for index in range(3):
        await router("session/new", {"cwd": f"/w{index}", "mcpServers": []}, False)

    result = await router("session/list", {}, False)

    assert [info.cwd for info in result.sessions] == ["/w2", "/w1", "/w0"]
    assert result.next_cursor is None


async def test_list_sessions_can_be_filtered_by_cwd() -> None:
    router = make_router()
    await router("session/new", {"cwd": "/here", "mcpServers": []}, False)
    await router("session/new", {"cwd": "/there", "mcpServers": []}, False)

    result = await router("session/list", {"cwd": "/here"}, False)

    assert [info.cwd for info in result.sessions] == ["/here"]


async def test_a_cursor_this_agent_did_not_issue_is_refused() -> None:
    """Silently restarting from page one would loop a client forever."""
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router("session/list", {"cursor": "not-a-cursor"}, False)

    assert excinfo.value.code == -32602


async def test_fork_copies_the_session_under_a_new_id() -> None:
    """Evidence for `sessionCapabilities.fork`."""
    sessions = SessionRegistry()
    _agent, router, _client, session_id = await _lifecycle_agent(sessions=sessions)

    result = await router("session/fork", {"sessionId": session_id, "cwd": "/elsewhere"}, False)

    assert result.session_id != session_id
    assert sessions.get(result.session_id).cwd == "/elsewhere"
    assert len(sessions) == 2


async def test_a_fork_gets_its_own_mcp_subprocesses() -> None:
    """Sharing the parent's would make close on the fork tear down the parent's tools."""
    backends = McpBackendRegistry()
    _agent, router, _client, session_id = await _lifecycle_agent(backends=backends)
    await backends.close(session_id)
    await backends.open(
        session_id, [_stdio_spec("tools")]
    )
    try:
        result = await router("session/fork", {"sessionId": session_id, "cwd": "/work"}, False)

        parent = backends.backends(session_id)["tools"]
        child = backends.backends(result.session_id)["tools"]
        assert parent is not child
        assert [tool["name"] for tool in await child.list_tools()] == ["echo"]
    finally:
        await backends.close_all()


async def test_resume_returns_the_same_session_without_replaying() -> None:
    """Evidence for `sessionCapabilities.resume`.

    Load reconstitutes; resume reattaches. A client resuming a session it was already
    attached to already has the transcript, so re-sending it would duplicate everything.
    """
    chunk = AgentMessageChunk(
        sessionUpdate="agent_message_chunk",
        content=TextContentBlock(type="text", text="earlier"),
    )
    executor = RecordingExecutor(updates=[chunk])
    executor.release.set()
    sessions = SessionRegistry()
    _agent, router, client, session_id = await _lifecycle_agent(
        sessions=sessions, executor=executor
    )
    await router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    client.updates.clear()

    await router("session/resume", {"sessionId": session_id, "cwd": "/work"}, False)

    assert client.updates == []
    assert len(sessions) == 1


async def test_close_ends_the_session_and_releases_its_backends() -> None:
    """Evidence for `sessionCapabilities.close`."""
    backends = McpBackendRegistry()
    sessions = SessionRegistry(on_close=backends.close)
    _agent, router, _client, session_id = await _lifecycle_agent(
        sessions=sessions, backends=backends
    )

    await router("session/close", {"sessionId": session_id}, False)

    assert len(sessions) == 0
    assert session_id not in backends
    with pytest.raises(RequestError) as excinfo:
        await router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    assert excinfo.value.code == -32602


async def test_closing_a_session_twice_is_an_error() -> None:
    """A request has no notification-shaped silence available, and a double close is a bug."""
    _agent, router, _client, session_id = await _lifecycle_agent()
    await router("session/close", {"sessionId": session_id}, False)

    with pytest.raises(RequestError) as excinfo:
        await router("session/close", {"sessionId": session_id}, False)

    assert excinfo.value.code == -32602


async def test_closing_a_session_cancels_its_running_turn() -> None:
    executor = RecordingExecutor()
    _agent, router, _client, session_id = await _lifecycle_agent(executor=executor)
    turn = asyncio.create_task(
        router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    )
    await asyncio.wait_for(executor.started.wait(), timeout=5)

    await router("session/close", {"sessionId": session_id}, False)

    assert (await asyncio.wait_for(turn, timeout=5)).stop_reason == "cancelled"


async def test_the_unstable_lifecycle_is_not_advertised_without_the_flag() -> None:
    """The agent's own view of the connection, not just the router's."""
    agent = make_agent(unstable=False)
    router = build_agent_router(agent, use_unstable_protocol=False)

    result = await router("initialize", PARAMS["initialize"], False)

    assert result.agent_capabilities.session_capabilities.fork is None
    assert result.agent_capabilities.session_capabilities.list is not None


def _stdio_spec(name: str):
    from acp.schema import McpServerStdio

    return McpServerStdio(
        name=name, command=sys.executable, args=[str(FIXTURE_SERVER)], env=[]
    )
