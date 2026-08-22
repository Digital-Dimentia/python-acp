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


async def test_initialize_negotiates_and_advertises_nothing_it_cannot_do() -> None:
    router = make_router()

    result = await router("initialize", PARAMS["initialize"], False)

    assert result.protocol_version == PROTOCOL_VERSION
    assert result.agent_info.name == "python-acp"
    assert result.agent_info.version == __version__
    # The block is a promise. Phase 1 implements no features, so it promises none.
    assert result.agent_capabilities.load_session is False
    assert result.agent_capabilities.prompt_capabilities.image is False
    assert result.agent_capabilities.prompt_capabilities.audio is False
    assert result.agent_capabilities.prompt_capabilities.embedded_context is False
    assert result.agent_capabilities.mcp_capabilities.http is False
    assert result.agent_capabilities.mcp_capabilities.sse is False
    assert result.agent_capabilities.mcp_capabilities.acp is False
    assert result.agent_capabilities.session_capabilities.list is None
    assert result.agent_capabilities.session_capabilities.delete is None
    assert result.agent_capabilities.session_capabilities.additional_directories is None
    assert result.auth_methods == []


async def test_an_unsupported_protocol_version_is_answered_not_rejected() -> None:
    """The spec has the client decide whether our version is usable, not us."""
    router = make_router()

    result = await router("initialize", {"protocolVersion": PROTOCOL_VERSION + 1}, False)

    assert result.protocol_version == PROTOCOL_VERSION


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
