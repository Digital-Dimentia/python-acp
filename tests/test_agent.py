"""Tests for the ACP agent skeleton.

These drive `PythonAcpAgent` through the SDK's own router rather than calling its
methods directly. That is the point: the contract under test is "the SDK can dispatch
to this object", and a signature the router cannot splat into is the failure mode a
direct call would hide.
"""

from __future__ import annotations

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.agent.router import build_agent_router
from acp.schema import ClientCapabilities, FileSystemCapabilities

from python_acp import __version__
from python_acp.agent import PythonAcpAgent
from python_acp.capabilities import SUPPORTED_PROTOCOL_VERSIONS, build_agent_capabilities
from python_acp.errors import as_request_error
from python_acp.mcp_stdio import MCPProtocolError

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


def make_router(*, unstable: bool = True):
    return build_agent_router(PythonAcpAgent(), use_unstable_protocol=unstable)


def test_every_protocol_member_is_present() -> None:
    """A missing member is a silent -32601, so assert on the class, not the wire."""
    agent = PythonAcpAgent()
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


async def test_initialize_promises_nothing_it_cannot_do() -> None:
    """Spelled out per field, so the promise is legible without running the walker."""
    router = make_router()

    capabilities = (await router("initialize", PARAMS["initialize"], False)).agent_capabilities

    assert capabilities.load_session is False
    assert capabilities.prompt_capabilities.image is False
    assert capabilities.prompt_capabilities.audio is False
    assert capabilities.prompt_capabilities.embedded_context is False
    assert capabilities.mcp_capabilities.http is False
    assert capabilities.mcp_capabilities.sse is False
    assert capabilities.mcp_capabilities.acp is False
    assert capabilities.session_capabilities.list is None
    assert capabilities.session_capabilities.delete is None
    assert capabilities.session_capabilities.additional_directories is None
    # Advertising these three would promise session/fork, /resume, and /close, which
    # are unstable-gated in the router and unimplemented until pyacp-3rw.3.
    assert capabilities.session_capabilities.fork is None
    assert capabilities.session_capabilities.resume is None
    assert capabilities.session_capabilities.close is None
    assert capabilities.auth.logout is None
    assert capabilities.providers is None
    assert capabilities.nes is None
    assert capabilities.position_encoding is None


async def test_the_capability_block_is_not_shared_between_connections() -> None:
    """A client that mutated its response must not be able to reach the next one's."""
    first = (await make_router()("initialize", PARAMS["initialize"], False)).agent_capabilities
    first.load_session = True

    second = (await make_router()("initialize", PARAMS["initialize"], False)).agent_capabilities

    assert second.load_session is False


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
    agent = PythonAcpAgent()
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
    permissive, restricted = PythonAcpAgent(), PythonAcpAgent()

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
    agent = PythonAcpAgent()
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


@pytest.mark.parametrize(
    "method",
    [m for m in ROUTED_REQUESTS if m not in ("initialize", "authenticate")],
)
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
    agent = PythonAcpAgent()
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

    router = build_agent_router(BackendFails(), use_unstable_protocol=True)

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

    router = build_agent_router(Picky(), use_unstable_protocol=True)

    with pytest.raises(RequestError) as excinfo:
        await router("session/list", {}, False)

    assert excinfo.value.code == -32602
    assert excinfo.value.data == {"reason": "'cursor' must be a string"}
