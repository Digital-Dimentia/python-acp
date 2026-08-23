"""Tests for the ACP agent skeleton.

These drive `PythonAcpAgent` through the SDK's own router rather than calling its
methods directly. That is the point: the contract under test is "the SDK can dispatch
to this object", and a signature the router cannot splat into is the failure mode a
direct call would hide.
"""

from __future__ import annotations

import asyncio
import json
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
from python_acp.turn_mcp_router import McpToolRouterExecutor
from python_acp.mcp_stdio import MCPProtocolError
from python_acp.sessions import SessionRegistry
from python_acp.turns import IdleTurnExecutor, TurnContext, TurnResult

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

    # Built: the session lifecycle (pyacp-3rw.2, pyacp-3rw.3, pyacp-3rw.4).
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
    assert capabilities.session_capabilities.additional_directories is not None


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


async def test_nothing_routed_answers_method_not_found_any_more() -> None:
    """Every routed method now has a body — `pyacp-fln.3` was the last.

    This replaces the per-method "still unbuilt" test, which had nothing left to
    parametrize over. A `-32601` from the agent would now mean a member was deleted.
    """
    router = make_router()
    unbuilt = []

    for method, params in PARAMS.items():
        try:
            await router(method, params, method in router._notifications)
        except RequestError as exc:
            if exc.code == -32601:
                unbuilt.append(method)
        except Exception:  # noqa: BLE001 — any other failure is not this test's subject
            pass

    assert unbuilt == []


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
        self.stop_reason: StopReason = stop_reason
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
        return TurnResult(self.stop_reason)


class RecordingClient:
    """Captures `session/update` calls the way the SDK's Client facade would receive them.

    Approves every permission request: these tests are about the agent's plumbing, and a
    client that refused would turn each of them into a test about permissions instead.
    """

    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        from acp.schema import AllowedOutcome, RequestPermissionResponse

        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", optionId="approve")
        )


async def test_new_session_registers_a_session_and_returns_its_id() -> None:
    registry = SessionRegistry()
    router = make_router(agent=make_agent(sessions=registry))

    result = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    assert result.session_id in registry
    assert registry.get(result.session_id).cwd == "/work"
    # Both come from the executor (pyacp-fln.2, pyacp-fln.3).
    assert result.modes.current_mode_id == "execute"
    assert [option.id for option in result.config_options] == [
        "announce-tools", "on-tool-failure",
    ]


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


# ---------------------------------------------------------------------------
# stopReason semantics (pyacp-hnk.5)
# ---------------------------------------------------------------------------


def _chunk(text: str) -> AgentMessageChunk:
    return AgentMessageChunk(
        sessionUpdate="agent_message_chunk", content=TextContentBlock(type="text", text=text)
    )


async def _updates_until(client: RecordingClient, predicate, timeout: float = 10):
    """Wait for a `session/update` matching `predicate`, so a test cancels a turn at a
    known point rather than after a guessed sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for _session_id, update in list(client.updates):
            if predicate(update):
                return update
        await asyncio.sleep(0.01)
    raise AssertionError("the update never arrived")


@pytest.mark.parametrize("stop_reason", ["end_turn", "refusal", "max_tokens"])
async def test_the_response_carries_the_stop_reason_the_executor_returned(
    stop_reason: StopReason,
) -> None:
    """`prompt` reports, it does not decide. The one case where it overrides the executor
    is a cancelled turn, which the tests below cover."""
    executor = RecordingExecutor(stop_reason=stop_reason)
    executor.release.set()
    agent, router, _client, session_id = await _lifecycle_agent(executor=executor)

    result = await router("session/prompt", {"sessionId": session_id, "prompt": []}, False)

    assert result.stop_reason == stop_reason


async def test_a_cancel_that_lands_before_the_turn_starts_answers_cancelled() -> None:
    """Cancel-before-start. The task is attached before its first step runs, so the
    cancellation reaches a turn that has emitted nothing — and the answer is still a
    well-formed response rather than a raise or a hang."""
    executor = RecordingExecutor()
    agent, router, client, session_id = await _lifecycle_agent(executor=executor)

    turn = asyncio.create_task(
        router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    )
    # One scheduling step: enough for `prompt` to reach `attach_turn`, not enough for the
    # executor task queued behind this test to have run at all.
    await asyncio.sleep(0)
    await router("session/cancel", {"sessionId": session_id}, True)

    result = await asyncio.wait_for(turn, timeout=5)
    assert result.stop_reason == "cancelled"
    assert executor.started.is_set() is False
    assert client.updates == []


async def test_a_cancel_with_no_turn_running_does_not_poison_the_next_turn() -> None:
    """`attach_turn` installs a fresh event, so a stray cancel cannot pre-cancel a turn
    that has not been asked for yet."""
    executor = RecordingExecutor()
    executor.release.set()
    agent, router, _client, session_id = await _lifecycle_agent(executor=executor)

    await router("session/cancel", {"sessionId": session_id}, True)
    result = await router("session/prompt", {"sessionId": session_id, "prompt": []}, False)

    assert result.stop_reason == "end_turn"


async def test_a_cancel_after_the_turn_completed_changes_nothing() -> None:
    """Cancel-after-completion. The client is behaving correctly — it could not have
    known the turn had finished — so the answer already sent stands and the next turn is
    unaffected."""
    executor = RecordingExecutor()
    executor.release.set()
    agent, router, client, session_id = await _lifecycle_agent(executor=executor)
    first = await router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    updates_at_response = len(client.updates)

    assert await router("session/cancel", {"sessionId": session_id}, True) is None
    await asyncio.sleep(0.05)
    second = await router("session/prompt", {"sessionId": session_id, "prompt": []}, False)

    assert first.stop_reason == "end_turn"
    assert second.stop_reason == "end_turn"
    assert len(client.updates) == updates_at_response


async def test_a_cancelled_turns_cleanup_update_lands_before_the_response() -> None:
    """The ordering guarantee is structural: `prompt` builds the response only after the
    turn task is done, so even an executor that emits from its `except CancelledError`
    handler cannot put a notification on the wire after the answer."""

    class CleaningUp:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def execute(self, context, prompt):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Shielded, so the emit survives the cancellation already in flight.
                await asyncio.shield(context.emit(_chunk("stopping")))
                raise

    executor = CleaningUp()
    agent, router, client, session_id = await _lifecycle_agent(executor=executor)
    turn = asyncio.create_task(
        router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    )
    await asyncio.wait_for(executor.started.wait(), timeout=5)
    await router("session/cancel", {"sessionId": session_id}, True)

    result = await asyncio.wait_for(turn, timeout=5)
    assert result.stop_reason == "cancelled"
    # Already delivered when the response was built, and nothing follows it.
    assert [update for _session_id, update in client.updates] == [_chunk("stopping")]
    await asyncio.sleep(0.05)
    assert len(client.updates) == 1


async def test_an_executor_that_swallows_the_cancellation_still_reports_cancelled() -> None:
    """A turn the client explicitly stopped may not answer `end_turn`.

    Letting `CancelledError` propagate is the contract; this is what happens when an
    executor breaks it. The flag is per turn, so the override cannot fire on a turn
    nobody cancelled."""

    class Swallowing:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def execute(self, context, prompt):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return TurnResult.ended()

    executor = Swallowing()
    agent, router, _client, session_id = await _lifecycle_agent(executor=executor)
    turn = asyncio.create_task(
        router("session/prompt", {"sessionId": session_id, "prompt": []}, False)
    )
    await asyncio.wait_for(executor.started.wait(), timeout=5)
    await router("session/cancel", {"sessionId": session_id}, True)

    result = await asyncio.wait_for(turn, timeout=5)
    assert result.stop_reason == "cancelled"


async def test_cancelling_mid_tool_call_answers_cancelled_and_unasks_the_backend() -> None:
    """Cancel-mid-tool-call, against the real fixture server.

    `stall` is read and never answered, so the only things that can end the turn are the
    cancellation and the client's 30s MCP timeout — a `wait_for(5)` tells those apart.
    The backend must then be *told* to stop rather than left computing a reply nobody
    will read, which is `notifications/cancelled` carrying that request's own id.
    """
    backends = McpBackendRegistry()
    agent = make_agent(backends=backends)
    client = RecordingClient()
    agent.on_connect(client)  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router(
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
        turn = asyncio.create_task(
            router(
                "session/prompt",
                {
                    "sessionId": created.session_id,
                    "prompt": [{"type": "text", "text": json.dumps({"tool": "stall"})}],
                },
                False,
            )
        )
        await _updates_until(
            client, lambda u: getattr(u, "status", None) == "in_progress"
        )
        await router("session/cancel", {"sessionId": created.session_id}, True)

        result = await asyncio.wait_for(turn, timeout=5)
        assert result.stop_reason == "cancelled"
        updates_at_response = len(client.updates)

        backend = backends.backends(created.session_id)["tools"]
        report = json.loads(
            (await backend.call_tool("cancel-report", {}))["content"][0]["text"]
        )
        assert report["stalled"], "the fixture never saw the stalled tools/call"
        assert [c["requestId"] for c in report["cancelled"]] == [report["stalled"][-1]]
        # Nothing from the abandoned turn arrives after its response.
        assert len(client.updates) == updates_at_response
    finally:
        await backends.close_all()


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


async def test_the_default_executor_is_the_mcp_tool_router() -> None:
    """Decision D3's shipped default. An empty prompt names no tool, so it is refused.

    `turns.IdleTurnExecutor` is still there for a caller that wants a turn to do nothing;
    it is no longer what an unconfigured agent does.
    """
    agent = make_agent()
    client = RecordingClient()
    agent.on_connect(client)  # type: ignore[arg-type]
    router = build_agent_router(agent, use_unstable_protocol=True)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    result = await router(
        "session/prompt", {"sessionId": created.session_id, "prompt": []}, False
    )

    assert result.stop_reason == "refusal"
    # An empty prompt echoes nothing, so the turn's updates are the command list (empty:
    # this session opened no MCP servers) and the refusal that names the convention.
    kinds = [update.session_update for _session_id, update in client.updates]
    assert kinds == ["available_commands_update", "agent_message_chunk"]
    assert '"tool"' in client.updates[-1][1].content.text


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


# ---------------------------------------------------------------------------
# Path constraints at the edge (pyacp-3rw.4)
# ---------------------------------------------------------------------------


async def test_new_session_refuses_a_relative_cwd() -> None:
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router("session/new", {"cwd": "relative/path", "mcpServers": []}, False)

    assert excinfo.value.code == -32602
    assert "absolute" in excinfo.value.data["reason"]


async def test_new_session_stores_additional_directories_validated_and_tidied() -> None:
    """Evidence for `sessionCapabilities.additionalDirectories`."""
    sessions = SessionRegistry()
    router = make_router(agent=make_agent(sessions=sessions))

    result = await router(
        "session/new",
        {"cwd": "/work/./sub", "additionalDirectories": ["/a/b/..", "/a"], "mcpServers": []},
        False,
    )

    session = sessions.get(result.session_id)
    assert session.cwd == "/work/sub"
    # `/a/b/..` is `/a`, and the duplicate collapses.
    assert session.additional_directories == ("/a",)
    assert session.roots == ("/work/sub", "/a")


async def test_new_session_refuses_a_relative_additional_directory() -> None:
    """A relative extra root is as broken as a relative cwd, and names its index."""
    router = make_router()

    with pytest.raises(RequestError) as excinfo:
        await router(
            "session/new",
            {"cwd": "/work", "additionalDirectories": ["/fine", "nope"], "mcpServers": []},
            False,
        )

    assert excinfo.value.code == -32602
    assert "additionalDirectories[1]" in excinfo.value.data["reason"]


async def test_a_relative_cwd_leaves_no_session_behind() -> None:
    """Validation runs before `create`, so a refused request creates nothing."""
    sessions = SessionRegistry()
    router = make_router(agent=make_agent(sessions=sessions))

    with pytest.raises(RequestError):
        await router("session/new", {"cwd": "nope", "mcpServers": []}, False)

    assert len(sessions) == 0


async def test_fork_validates_its_own_cwd() -> None:
    _agent, router, _client, session_id = await _lifecycle_agent()

    with pytest.raises(RequestError) as excinfo:
        await router("session/fork", {"sessionId": session_id, "cwd": "relative"}, False)

    assert excinfo.value.code == -32602


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("session/resume", {"cwd": "relative"}),
        ("session/load", {"cwd": "relative", "mcpServers": []}),
    ],
)
async def test_resume_and_load_validate_a_cwd_they_do_not_apply(
    method: str, params: dict
) -> None:
    """Accepting a relative path and silently ignoring it would tell a client its path
    was fine when it was both invalid and unused."""
    _agent, router, _client, session_id = await _lifecycle_agent()

    with pytest.raises(RequestError) as excinfo:
        await router(method, {"sessionId": session_id, **params}, False)

    assert excinfo.value.code == -32602


# ---------------------------------------------------------------------------
# Mode switching (pyacp-fln.2)
# ---------------------------------------------------------------------------


async def test_new_session_advertises_the_executors_modes() -> None:
    """Modes come from the executor, the only thing that can act on one."""
    agent, router, _client, session_id = await _lifecycle_agent(
        executor=McpToolRouterExecutor(McpBackendRegistry())
    )

    listed = agent.sessions.get(session_id).modes
    assert listed.current_mode_id == "execute"
    assert {m.id for m in listed.available_modes} == {"execute", "dry-run", "auto-approve"}


async def test_setting_a_mode_updates_the_session_and_tells_the_client() -> None:
    """The notification goes out even though the client asked, so an internal change and
    a client-driven one look the same — and a second client stays in step."""
    agent, router, client, session_id = await _lifecycle_agent(
        executor=McpToolRouterExecutor(McpBackendRegistry())
    )

    await router("session/set_mode", {"sessionId": session_id, "modeId": "dry-run"}, False)

    assert agent.sessions.get(session_id).modes.current_mode_id == "dry-run"
    announced = [u for _s, u in client.updates if u.session_update == "current_mode_update"]
    assert [u.current_mode_id for u in announced] == ["dry-run"]


async def test_an_unknown_mode_is_refused_and_changes_nothing() -> None:
    agent, router, client, session_id = await _lifecycle_agent(
        executor=McpToolRouterExecutor(McpBackendRegistry())
    )

    with pytest.raises(RequestError) as excinfo:
        await router("session/set_mode", {"sessionId": session_id, "modeId": "nope"}, False)

    assert excinfo.value.code == -32602
    assert agent.sessions.get(session_id).modes.current_mode_id == "execute"
    assert client.updates == []


async def test_setting_a_mode_on_a_session_that_has_none_is_refused() -> None:
    """`IdleTurnExecutor` advertises no modes, and a notification for a mode the client
    was never offered would be worse than an error."""
    _agent, router, _client, session_id = await _lifecycle_agent(executor=IdleTurnExecutor())

    with pytest.raises(RequestError) as excinfo:
        await router("session/set_mode", {"sessionId": session_id, "modeId": "dry-run"}, False)

    assert excinfo.value.code == -32602


async def test_each_session_gets_its_own_copy_of_the_declared_modes() -> None:
    """`set_mode` mutates `current_mode_id` in place, and the executor's declaration is
    shared by every session it serves."""
    executor = McpToolRouterExecutor(McpBackendRegistry())
    agent, router, _client, first = await _lifecycle_agent(executor=executor)
    second = (await router("session/new", {"cwd": "/work", "mcpServers": []}, False)).session_id

    await router("session/set_mode", {"sessionId": first, "modeId": "dry-run"}, False)

    assert agent.sessions.get(second).modes.current_mode_id == "execute"
    assert executor.session_modes.current_mode_id == "execute"


async def test_a_fork_does_not_inherit_a_later_mode_change() -> None:
    agent, router, _client, session_id = await _lifecycle_agent(
        executor=McpToolRouterExecutor(McpBackendRegistry())
    )
    forked = await router("session/fork", {"sessionId": session_id, "cwd": "/work"}, False)

    await router("session/set_mode", {"sessionId": session_id, "modeId": "auto-approve"}, False)

    assert agent.sessions.get(forked.session_id).modes.current_mode_id == "execute"


# ---------------------------------------------------------------------------
# Config options (pyacp-fln.3)
# ---------------------------------------------------------------------------


async def _configurable():
    return await _lifecycle_agent(executor=McpToolRouterExecutor(McpBackendRegistry()))


async def test_a_boolean_option_is_set_and_announced() -> None:
    agent, router, client, session_id = await _configurable()

    result = await router(
        "session/set_config_option",
        {"type": "boolean", "sessionId": session_id, "configId": "announce-tools", "value": False},
        False,
    )

    assert agent.sessions.get(session_id).config_option("announce-tools").current_value is False
    # The response carries EVERY option, which is what the schema asks for and what a
    # client re-rendering a settings panel wants. It arrives as a dict because this route
    # is one of the three the SDK registers with `adapt_result=normalize_result`.
    assert [o["id"] for o in result["configOptions"]] == ["announce-tools", "on-tool-failure"]
    announced = [u for _s, u in client.updates if u.session_update == "config_option_update"]
    assert [o.current_value for o in announced[0].config_options] == [False, "continue"]


async def test_a_select_option_is_set_by_the_same_implementation() -> None:
    """One method for both request shapes: the SDK discriminates on `type` and splats
    either into the same parameters, so only `value` differs by the time it arrives."""
    agent, router, _client, session_id = await _configurable()

    await router(
        "session/set_config_option",
        {"type": "select", "sessionId": session_id, "configId": "on-tool-failure", "value": "stop"},
        False,
    )

    assert agent.sessions.get(session_id).config_option("on-tool-failure").current_value == "stop"


@pytest.mark.parametrize(
    ("params", "because"),
    [
        ({"type": "boolean", "configId": "nope", "value": True}, "Unknown config option"),
        ({"type": "boolean", "configId": "on-tool-failure", "value": True}, "is a select"),
        ({"type": "select", "configId": "announce-tools", "value": "x"}, "is boolean"),
        ({"type": "select", "configId": "on-tool-failure", "value": "sideways"}, "Unknown value"),
    ],
)
async def test_an_invalid_option_or_value_is_refused(params: dict, because: str) -> None:
    agent, router, client, session_id = await _configurable()

    with pytest.raises(RequestError) as excinfo:
        await router(
            "session/set_config_option", {"sessionId": session_id, **params}, False
        )

    assert excinfo.value.code == -32602
    assert because in excinfo.value.data["reason"]
    assert client.updates == []


async def test_each_session_gets_its_own_copy_of_the_declared_options() -> None:
    executor = McpToolRouterExecutor(McpBackendRegistry())
    agent, router, _client, first = await _lifecycle_agent(executor=executor)
    second = (await router("session/new", {"cwd": "/work", "mcpServers": []}, False)).session_id

    await router(
        "session/set_config_option",
        {"type": "boolean", "sessionId": first, "configId": "announce-tools", "value": False},
        False,
    )

    assert agent.sessions.get(second).config_option("announce-tools").current_value is True
    assert executor.session_config_options[0].current_value is True


async def test_a_session_with_no_options_announces_nothing() -> None:
    """`IdleTurnExecutor` exposes none, so there is nothing to set and nothing to say."""
    agent, _router, client, session_id = await _lifecycle_agent(executor=IdleTurnExecutor())

    await agent.announce_config_options(agent.sessions.get(session_id))

    assert client.updates == []
