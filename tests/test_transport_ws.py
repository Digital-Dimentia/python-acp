"""Tests for the WebSocket binding and the deprecated surface it shelters.

Most tests here drive a **fake socket** rather than a listening port. That is not only to
dodge the sandbox's `bind()` denial: the thing under test is the message path from a frame
to the SDK router and back, and a real TCP listener adds a port, a handshake, and two
timeouts without exercising one extra line of it.
`test_the_sdk_accepts_our_transport_and_completes_initialize` is the exception that
matters — it runs a real `acp.run_agent` over `WebSocketMessageTransport`, which is the
decision-B4 canary: if a future SDK changes the private `Transport` shape we conform to,
that test fails on the day the pin moves.

The section "The real WebSocket" is the other exception. A fake socket cannot exercise
`websockets`' opening handshake, its frame codec, or `WebSocketAgentServer.start()` —
and those are the parts a client meets first. `pyacp-22w` covers them over a
`socket.socketpair()` instead of a listening port, so they run wherever `bind()` is
denied.

The MCP backend is the real `tests/fixtures/mock_mcp_server.py` subprocess, per the
repo's convention of not mocking it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import websockets
from acp import PROTOCOL_VERSION
from websockets.protocol import State

from python_acp import __version__
from python_acp.capabilities import build_agent_capabilities
from python_acp.legacy_ws import (
    ACTION_REPLACEMENTS,
    LEGACY_METHODS,
    REMOVED_IN,
    is_legacy,
)
from python_acp.mcp_stdio import MCPStdioClient
from python_acp.sessions import SessionRegistry
from python_acp.terminals import TerminalRegistry
from python_acp.transport_ws import (
    WebSocketAgentServer,
    WebSocketMessageTransport,
    serve_websocket,
)

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"
TIMEOUT = 10


class FakeWebSocket:
    """The slice of `websockets.asyncio.server.ServerConnection` the transport uses.

    Async-iterable for inbound frames, `send` for outbound, `close` at the end. Feeding
    `None` is how a test says the client hung up, which is what makes `receive()` report
    EOF and `run_agent` return.
    """

    remote_address = ("fake", 0)

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._replies: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        frame = await self._inbox.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def send(self, data: str) -> None:
        message = json.loads(data)
        self.sent.append(message)
        self._replies.put_nowait(message)

    async def close(self) -> None:
        self.closed = True

    # -- driver side ----------------------------------------------------

    def feed(self, message: dict[str, Any] | str) -> None:
        self._inbox.put_nowait(message if isinstance(message, str) else json.dumps(message))

    def hang_up(self) -> None:
        self._inbox.put_nowait(None)

    async def next_reply(self) -> dict[str, Any]:
        return await asyncio.wait_for(self._replies.get(), timeout=TIMEOUT)

    async def ask(self, message: dict[str, Any] | str) -> dict[str, Any]:
        """Send one message and return the **response** to it.

        Notifications the agent sends on the way — `session/update` from a running turn —
        arrive on the same socket and are skipped here rather than mistaken for the
        reply. They stay in `sent` for a test that wants them.
        """
        self.feed(message)
        wanted = message.get("id") if isinstance(message, dict) else None
        while True:
            reply = await self.next_reply()
            if wanted is None or reply.get("id") == wanted:
                return reply


@contextlib.asynccontextmanager
async def bound_socket() -> AsyncIterator[FakeWebSocket]:
    """A fake socket bound to a live agent over a real MCP backend."""
    async with MCPStdioClient([sys.executable, str(FIXTURE_SERVER)]) as mcp_client:
        await mcp_client.initialize()
        websocket = FakeWebSocket()
        connection = asyncio.create_task(serve_websocket(websocket, mcp_client))
        try:
            yield websocket
        finally:
            websocket.hang_up()
            await asyncio.wait_for(connection, timeout=TIMEOUT)


# ---------------------------------------------------------------------------
# The binding itself
# ---------------------------------------------------------------------------


async def test_the_sdk_accepts_our_transport_and_completes_initialize() -> None:
    """Decision B4's canary.

    `WebSocketMessageTransport` conforms to `acp._transport.Transport` structurally and
    we never import that private module, so nothing but this round trip would tell us if
    an SDK bump changed the shape. It fails on the day the pin moves, not in production.
    """
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION}}
        )

    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert reply["result"]["agentInfo"] == {"name": "python-acp", "version": __version__}


async def test_a_websocket_client_gets_the_same_capability_block_as_a_stdio_client() -> None:
    """The point of the rebind: one agent, one answer, whichever wire you arrived on."""
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION}}
        )

    expected = build_agent_capabilities().model_dump(by_alias=True, exclude_none=True)
    assert reply["result"]["agentCapabilities"] == expected
    assert reply["result"]["authMethods"] == []


async def test_an_unrouted_method_answers_method_not_found() -> None:
    """Proof the SDK router is doing the dispatching, not a hand-rolled branch.

    Every routed method is implemented, so the only `-32601` left comes from a name the
    SDK does not route at all.
    """
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/delete",
             "params": {"sessionId": "s1"}}
        )

    assert reply["error"]["code"] == -32601


async def test_a_websocket_client_can_run_the_session_lifecycle() -> None:
    """The same agent means the same session methods, not just the same handshake."""
    async with bound_socket() as websocket:
        await websocket.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION}}
        )
        created = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new",
             "params": {"cwd": "/tmp", "mcpServers": []}}
        )
        prompted = await websocket.ask(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": created["result"]["sessionId"], "prompt": []}}
        )

    assert created["result"]["sessionId"]
    # An empty prompt names no tool for the default MCP tool-router to run.
    assert prompted["result"]["stopReason"] == "refusal"


async def test_sessions_outlive_the_connection_that_created_them() -> None:
    """One registry per process: a reconnecting client must find its session again.

    A per-connection registry would make `session/resume` meaningless, and the failure
    would look like a stale id rather than a design mistake.
    """
    async with MCPStdioClient([sys.executable, str(FIXTURE_SERVER)]) as mcp_client:
        await mcp_client.initialize()
        sessions = SessionRegistry()

        first = FakeWebSocket()
        first_connection = asyncio.create_task(
            serve_websocket(first, mcp_client, sessions=sessions)
        )
        created = await first.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "session/new",
             "params": {"cwd": "/tmp", "mcpServers": []}}
        )
        first.hang_up()
        await asyncio.wait_for(first_connection, timeout=TIMEOUT)

        second = FakeWebSocket()
        second_connection = asyncio.create_task(
            serve_websocket(second, mcp_client, sessions=sessions)
        )
        try:
            prompted = await second.ask(
                {"jsonrpc": "2.0", "id": 1, "method": "session/prompt",
                 "params": {"sessionId": created["result"]["sessionId"], "prompt": []}}
            )
        finally:
            second.hang_up()
            await asyncio.wait_for(second_connection, timeout=TIMEOUT)

    assert prompted["result"]["stopReason"] == "refusal"


async def test_unstable_methods_are_reachable_over_websocket() -> None:
    """Both transports must pass use_unstable_protocol, or one client sees two agents.

    Without the flag the router answers -32601 *without calling the agent*, so a
    `session/close` that actually closes is the proof the flag reached it.
    """
    async with bound_socket() as websocket:
        created = await websocket.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "session/new",
             "params": {"cwd": "/tmp", "mcpServers": []}}
        )
        session_id = created["result"]["sessionId"]
        closed = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/close",
             "params": {"sessionId": session_id}}
        )
        gone = await websocket.ask(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": session_id, "prompt": []}}
        )

    assert "error" not in closed
    assert gone["error"]["code"] == -32602


async def test_each_connection_gets_its_own_agent() -> None:
    """`on_connect` stores *the* client handle, so a shared agent would cross the wires."""
    async with MCPStdioClient([sys.executable, str(FIXTURE_SERVER)]) as mcp_client:
        await mcp_client.initialize()
        first, second = FakeWebSocket(), FakeWebSocket()
        tasks = [
            asyncio.create_task(serve_websocket(ws, mcp_client)) for ws in (first, second)
        ]
        try:
            await first.ask(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": PROTOCOL_VERSION,
                            "clientCapabilities": {"terminal": True}}}
            )
            await second.ask(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": PROTOCOL_VERSION}}
            )
            # Each connection answered its own initialize; a shared agent would have let
            # the second overwrite the first's stored client handle.
            assert first.sent[0]["id"] == second.sent[0]["id"] == 1
            assert len(first.sent) == len(second.sent) == 1
        finally:
            for ws in (first, second):
                ws.hang_up()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=TIMEOUT)


# ---------------------------------------------------------------------------
# Framing — below the SDK, so ours to answer
# ---------------------------------------------------------------------------


async def test_malformed_json_is_a_parse_error() -> None:
    async with bound_socket() as websocket:
        reply = await websocket.ask("{bad-json")

    assert reply == {
        "jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"},
    }


async def test_a_non_object_payload_is_an_invalid_request() -> None:
    """`Transport` promises the SDK a dict; a bare list can never become one."""
    async with bound_socket() as websocket:
        reply = await websocket.ask("[1, 2, 3]")

    assert reply["error"]["code"] == -32600


async def test_bad_params_reach_the_client_as_invalid_params() -> None:
    """Validation is the SDK's now, and it answers -32602 through `errors.py`'s shape."""
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 9, "method": "session/new", "params": {"mcpServers": []}}
        )

    assert reply["id"] == 9
    assert reply["error"]["code"] == -32602


async def test_a_junk_protocol_version_is_salvaged_rather_than_rejected() -> None:
    """A behaviour change the rebind inherits, pinned here so it is not a surprise.

    The old hand-rolled dispatcher answered -32602 for a non-integer `protocolVersion`.
    The SDK's schema wraps that field in `salvage_on_error`, so junk becomes the default
    and the handshake completes. That is the SDK's deliberate robustness choice — a
    malformed optional field should not kill a connection — and adopting its validation
    means adopting it. It is not reachable from `capabilities.negotiate_protocol_version`,
    which only ever sees an int.
    """
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 9, "method": "initialize",
             "params": {"protocolVersion": "not-a-number"}}
        )

    assert "error" not in reply
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION


async def test_a_notification_draws_no_reply() -> None:
    """No `id` means no response — asserted by what arrives next, not by a timeout."""
    async with bound_socket() as websocket:
        websocket.feed({"jsonrpc": "2.0", "method": "nonexistent/notify", "params": {}})
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 5, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION}}
        )

    assert reply["id"] == 5
    assert len(websocket.sent) == 1


async def test_the_transport_closes_the_socket_when_the_connection_ends() -> None:
    async with bound_socket() as websocket:
        pass

    assert websocket.closed is True


def test_the_transport_has_the_three_methods_the_sdk_calls() -> None:
    """Structural conformance is the whole contract; a missing method is a runtime error."""
    transport = WebSocketMessageTransport(FakeWebSocket(), object())  # type: ignore[arg-type]

    assert all(callable(getattr(transport, name, None)) for name in ("send", "receive", "close"))


# ---------------------------------------------------------------------------
# The deprecated surface, intercepted before the SDK (legacy_ws.py)
# ---------------------------------------------------------------------------


def test_only_non_acp_methods_are_claimed_as_legacy() -> None:
    """Claiming an ACP method would shadow the agent; this is the guard on that set."""
    assert is_legacy({"action": "list_tools"})
    assert is_legacy({"method": "tools/call"})
    assert not is_legacy({"method": "initialize"})
    assert not is_legacy({"method": "session/new"})
    assert "initialize" not in LEGACY_METHODS


async def test_the_action_surface_still_serves_every_mcp_primitive() -> None:
    async with bound_socket() as websocket:
        tools = await websocket.ask({"action": "list_tools"})
        called = await websocket.ask(
            {"action": "call_tool", "name": "echo", "arguments": {"text": "from-ws"}}
        )
        prompts = await websocket.ask({"action": "list_prompts"})
        prompt = await websocket.ask(
            {"action": "get_prompt", "name": "greeting", "arguments": {"name": "Milo"}}
        )
        resources = await websocket.ask({"action": "list_resources"})
        resource = await websocket.ask(
            {"action": "read_resource", "name": "greeting://{name}", "arguments": {"name": "Nia"}}
        )
        missing = await websocket.ask({"action": "call_tool", "name": "missing"})

    assert tools["ok"] is True and tools["tools"][0]["name"] == "echo"
    assert called["ok"] is True and called["result"]["content"][0]["text"] == "from-ws"
    assert prompts["ok"] is True and prompts["prompts"][0]["name"] == "greeting"
    assert prompt["result"]["messages"][0]["content"]["text"] == "Hello, Milo!"
    assert resources["ok"] is True and resources["resources"][0]["name"] == "greeting-resource"
    assert resource["result"]["contents"][0]["text"] == "Hello, Nia!"
    assert missing["ok"] is False


async def test_the_mcp_passthrough_survives_the_rebind() -> None:
    """`tools/list` is not ACP and the agent has no member for it.

    Left to the SDK it would answer -32601, deleting a working surface in the release
    that rebound the socket. D4 keeps the legacy API alive *through* the migration, so
    `legacy_ws.py` carries it until `pyacp-sld.2` renames it onto `ext_method`.
    """
    async with bound_socket() as websocket:
        listed = await websocket.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "echo", "arguments": {"text": "hi"}}}
        )
        pong = await websocket.ask({"jsonrpc": "2.0", "id": 3, "method": "ping"})

    assert listed["result"]["tools"][0]["name"] == "echo"
    assert called["result"]["content"][0]["text"] == "hi"
    assert pong["result"] == {"pong": True}


async def test_a_legacy_action_failure_keeps_the_ok_false_envelope() -> None:
    """The action surface has no code field; a mapped error flattens back to its message."""
    async with bound_socket() as websocket:
        reply = await websocket.ask({"action": "no_such_action"})

    assert reply["ok"] is False
    assert "no_such_action" in reply["error"]


async def test_a_failed_tool_is_not_ok_but_still_carries_its_result() -> None:
    async with bound_socket() as websocket:
        failed = await websocket.ask(
            {"action": "call_tool", "name": "boom", "arguments": {}}
        )
        succeeded = await websocket.ask(
            {"action": "call_tool", "name": "echo", "arguments": {"text": "fine"}}
        )

    assert failed["ok"] is False
    assert failed["result"]["isError"] is True
    assert failed["error"] == failed["result"]["content"][0]["text"]
    assert succeeded["ok"] is True


# ---------------------------------------------------------------------------
# Failure fidelity across the rebind (pyacp-k5w, pyacp-tzd.6)
# ---------------------------------------------------------------------------


async def test_distinct_mcp_error_codes_stay_distinguishable() -> None:
    async with bound_socket() as websocket:
        not_found = await websocket.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "rpc-error",
                        "arguments": {"code": -32601, "message": "no such tool"}}}
        )
        bad_params = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "rpc-error", "arguments": {"code": -32602, "message": "bad args"}}}
        )

    assert not_found["error"]["code"] == -32601
    assert bad_params["error"]["code"] == -32602
    assert not_found["error"]["data"]["source"] == "mcp"
    assert "no such tool" in not_found["error"]["message"]


async def test_mcp_error_data_is_forwarded() -> None:
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "rpc-error",
                        "arguments": {"code": -32000, "data": {"retryAfter": 5}}}}
        )

    assert reply["error"]["code"] == -32000
    assert reply["error"]["data"]["mcpData"] == {"retryAfter": 5}


async def test_a_codeless_backend_failure_never_claims_the_backend_produced_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_MCP_LIST_STUCK", "1")
    async with bound_socket() as websocket:
        reply = await websocket.ask({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})

    assert reply["error"]["code"] == -32603
    assert "source" not in reply["error"].get("data", {})


async def test_a_tool_error_is_a_successful_result_not_a_transport_error() -> None:
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "boom", "arguments": {}}}
        )

    assert "error" not in reply
    assert reply["result"]["isError"] is True


async def test_a_legacy_backend_error_keeps_the_code_in_its_message() -> None:
    """The `{"ok": false}` envelope has nowhere else to put it."""
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"action": "call_tool", "name": "rpc-error",
             "arguments": {"code": -32601, "message": "no such tool"}}
        )

    assert reply["ok"] is False
    assert "-32601" in reply["error"]


async def test_requests_and_responses_are_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="python_acp.transport_ws"):
        async with bound_socket() as websocket:
            await websocket.ask({"action": "list_tools"})

    logged = "\n".join(record.message for record in caplog.records)
    assert "WebSocket request received" in logged
    assert "WebSocket response sent" in logged


async def test_the_server_binds_each_accepted_socket_to_an_agent() -> None:
    """Covers `WebSocketAgentServer` down to `_handle_client`, which is as far as a
    fake socket reaches. The real handshake, real frames, and `start()`/`stop()` are
    covered without a listening port under "The real WebSocket" below (`pyacp-22w`).
    """
    async with MCPStdioClient([sys.executable, str(FIXTURE_SERVER)]) as mcp_client:
        await mcp_client.initialize()
        server = WebSocketAgentServer(mcp_client)
        websocket = FakeWebSocket()
        connection = asyncio.create_task(server._handle_client(websocket))
        try:
            reply = await websocket.ask(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": PROTOCOL_VERSION}}
            )
        finally:
            websocket.hang_up()
            await asyncio.wait_for(connection, timeout=TIMEOUT)

    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert websocket.closed is True


async def test_a_disconnect_hands_the_departed_client_to_the_terminal_registry() -> None:
    """The wiring half of the disconnect rule (`pyacp-8bv.3`).

    A terminal lives in the client, so a connection ending means its terminals can never
    be released — only forgotten. This proves the transport asks, with the real client
    facade that just went away; `tests/test_terminals.py` proves what the asking does.
    """
    forgotten: list[Any] = []

    class Watching(TerminalRegistry):
        def forget_client(self, client: Any) -> int:
            forgotten.append(client)
            return super().forget_client(client)

    async with MCPStdioClient([sys.executable, str(FIXTURE_SERVER)]) as mcp_client:
        await mcp_client.initialize()
        terminals = Watching()
        websocket = FakeWebSocket()
        connection = asyncio.create_task(
            serve_websocket(websocket, mcp_client, terminals=terminals)
        )
        await websocket.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION,
                        "clientCapabilities": {"terminal": True}}}
        )
        websocket.hang_up()
        await asyncio.wait_for(connection, timeout=TIMEOUT)

    assert len(forgotten) == 1
    # The facade the SDK built for that socket, not None: `on_connect` ran.
    assert forgotten[0] is not None
    assert hasattr(forgotten[0], "create_terminal")


# ---------------------------------------------------------------------------
# Deprecation of the action surface (pyacp-sld.1). D4 keeps it working; every
# use now says so, on a channel the client can actually read.
# ---------------------------------------------------------------------------


async def test_every_action_reply_names_its_replacement_and_the_removal() -> None:
    """The notice is per *call*, because that is the signal a client can act on."""
    requests = {
        "list_tools": {"action": "list_tools"},
        "call_tool": {"action": "call_tool", "name": "echo", "arguments": {"text": "hi"}},
        "list_prompts": {"action": "list_prompts"},
        "get_prompt": {"action": "get_prompt", "name": "greeting", "arguments": {"name": "A"}},
        "list_resources": {"action": "list_resources"},
        "read_resource": {"action": "read_resource", "name": "greeting://x"},
        "ping": {"action": "ping"},
    }
    assert set(requests) == set(ACTION_REPLACEMENTS), "an action grew or vanished"

    async with bound_socket() as websocket:
        replies = {name: await websocket.ask(body) for name, body in requests.items()}

    for name, reply in replies.items():
        expected = {"action": name, "removedIn": REMOVED_IN}
        # `use` is present only where an ACP path exists. `pyacp-sld.2` decided the MCP
        # passthrough dies with this surface rather than moving to `ext_method`, so
        # prompts, resources, and ping have nothing honest to point at — and an absent
        # `use` says that, rather than naming a target that dies in the same release.
        if ACTION_REPLACEMENTS[name] is not None:
            expected["use"] = ACTION_REPLACEMENTS[name]
        assert reply["deprecated"] == expected, name

    assert [n for n, r in ACTION_REPLACEMENTS.items() if r is not None] == [
        "list_tools",
        "call_tool",
    ]


async def test_the_deprecation_notice_does_not_disturb_the_envelope() -> None:
    """D4 promises the surface keeps *working*; everything but the new key is untouched."""
    async with bound_socket() as websocket:
        tools = await websocket.ask({"action": "list_tools"})
        called = await websocket.ask(
            {"action": "call_tool", "name": "echo", "arguments": {"text": "from-ws"}}
        )

    assert tools["ok"] is True and tools["tools"][0]["name"] == "echo"
    assert called["ok"] is True and called["result"]["content"][0]["text"] == "from-ws"
    assert set(tools) == {"ok", "tools", "deprecated"}
    assert set(called) == {"ok", "result", "deprecated"}


async def test_a_failed_action_carries_the_notice_too() -> None:
    """A client whose call failed is no less on a surface that is going away."""
    async with bound_socket() as websocket:
        unsupported = await websocket.ask({"action": "no_such_action"})
        failed_tool = await websocket.ask({"action": "call_tool", "name": "boom"})

    # An unsupported action still earns the notice, but there is no honest migration
    # target to name for a method that never existed.
    assert unsupported["ok"] is False
    assert "no_such_action" in unsupported["error"]
    assert unsupported["deprecated"] == {
        "action": "no_such_action",
        "removedIn": REMOVED_IN,
    }

    # A failed *tool* is a normal reply, not an error envelope, and keeps both.
    assert failed_tool["ok"] is False
    assert failed_tool["result"]["isError"] is True
    assert failed_tool["deprecated"]["use"] == "session/new + session/prompt"


async def test_the_json_rpc_passthrough_carries_no_notice() -> None:
    """`pyacp-sld.1` is scoped to the action surface.

    The passthrough's fate is a *rename* onto `ext_method` (`pyacp-sld.2`), not a
    removal, so it needs a different message — and injecting one into a JSON-RPC
    `result` would change a payload clients parse today.
    """
    async with bound_socket() as websocket:
        listed = await websocket.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "echo", "arguments": {"text": "hi"}}}
        )

    assert "deprecated" not in listed and "deprecated" not in listed["result"]
    assert "deprecated" not in called and "deprecated" not in called["result"]


async def test_the_server_log_warns_once_per_action_not_once_per_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator's copy is deduped; a client looping on `call_tool` must not bury it."""
    with caplog.at_level(logging.WARNING, logger="python_acp.legacy_ws"):
        async with bound_socket() as websocket:
            for _ in range(3):
                await websocket.ask({"action": "list_tools"})
            await websocket.ask({"action": "ping"})

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 2, warnings
    assert "'list_tools' is deprecated" in warnings[0]
    assert "use session/new + session/prompt instead" in warnings[0]
    assert REMOVED_IN in warnings[0]
    # `ping` is a real action with no ACP counterpart, which the operator's copy says in
    # so many words: the capability goes away, not just the spelling.
    assert "'ping' is deprecated" in warnings[1]
    assert "no ACP replacement" in warnings[1]


async def test_an_unsupported_action_warns_without_naming_a_replacement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="python_acp.legacy_ws"):
        async with bound_socket() as websocket:
            await websocket.ask({"action": "frobnicate"})

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "action surface is deprecated" in message
    assert REMOVED_IN in message
    assert "'frobnicate' is not one of its actions" in message


def test_the_notice_is_a_fresh_dict_each_time() -> None:
    """It ends up inside a reply the caller owns and may mutate."""
    from python_acp.legacy_ws import deprecation_notice

    first = deprecation_notice("list_tools")
    first["use"] = "smuggled"

    assert deprecation_notice("list_tools")["use"] == "session/new + session/prompt"


# ---------------------------------------------------------------------------
# The real WebSocket: `serve()`, the opening handshake, and real frames, with
# no listening port (pyacp-22w)
# ---------------------------------------------------------------------------


class _StubAsyncioServer:
    """The slice of `asyncio.Server` that `websockets.asyncio.server.Server` touches.

    `serve()` calls `loop.create_server(...)` and keeps the result. Everything downstream
    of that — the protocol factory, the handshake, the frame codec, the handler task — is
    the real thing; only the *listener* is stubbed out, because a listener is the one part
    that needs `bind()` and the one part these tests do not care about.
    """

    sockets = ()

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.closed = False

    def get_loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def is_serving(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    async def start_serving(self) -> None:
        return None


@contextlib.asynccontextmanager
async def listening_server(**kwargs: Any) -> AsyncIterator[tuple[WebSocketAgentServer, Any]]:
    """A started `WebSocketAgentServer` whose listener never binds anything.

    `loop.create_server` is swapped for the duration of `start()` only — long enough to
    capture the protocol factory `serve()` builds and hand back a stub. `accept()` on the
    yielded handle then feeds that factory an already-connected socket, which is exactly
    what a real accept would have done.
    """
    loop = asyncio.get_running_loop()
    captured: dict[str, Any] = {}
    real_create_server = loop.create_server

    async def create_server(protocol_factory: Any, *args: Any, **kw: Any) -> _StubAsyncioServer:
        captured["factory"] = protocol_factory
        return _StubAsyncioServer(loop)

    server = WebSocketAgentServer(**kwargs)
    loop.create_server = create_server  # type: ignore[method-assign]
    try:
        await server.start()
    finally:
        loop.create_server = real_create_server  # type: ignore[method-assign]

    sockets: list[socket.socket] = []

    class Accepting:
        async def connect(self) -> Any:
            """One client, connected through the real opening handshake."""
            client_sock, server_sock = socket.socketpair()
            sockets.extend((client_sock, server_sock))
            await loop.connect_accepted_socket(captured["factory"], server_sock)
            # `ws://localhost/` is never resolved: `sock=` supplies the connection, and
            # the URI only supplies the Host header the handshake has to send.
            return await websockets.connect("ws://localhost/", sock=client_sock)

    try:
        yield server, Accepting()
    finally:
        await server.stop()
        for leftover in sockets:
            with contextlib.suppress(OSError):
                leftover.close()


async def test_the_real_opening_handshake_completes() -> None:
    """A fake socket starts *after* this. Everything a client meets first is here.

    `websockets.connect` raises unless the server answers `101 Switching Protocols` with
    a correct `Sec-WebSocket-Accept`, so reaching OPEN is the assertion.
    """
    async with listening_server(mcp_client=None) as (_server, accepting):
        connection = await asyncio.wait_for(accepting.connect(), timeout=TIMEOUT)
        try:
            assert connection.protocol.state is State.OPEN
            assert connection.response.status_code == 101
        finally:
            await connection.close()


async def test_a_real_frame_round_trips_through_the_agent() -> None:
    """Text frames, encoded and decoded by `websockets`, not handed over as dicts."""
    async with listening_server(mcp_client=None) as (_server, accepting):
        connection = await asyncio.wait_for(accepting.connect(), timeout=TIMEOUT)
        try:
            await connection.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": PROTOCOL_VERSION}}
                )
            )
            raw = await asyncio.wait_for(connection.recv(), timeout=TIMEOUT)
        finally:
            await connection.close()

    assert isinstance(raw, str), "ACP is a text protocol; a binary frame would be wrong"
    reply = json.loads(raw)
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert reply["result"]["agentInfo"]["name"] == "python-acp"


async def test_the_server_accepts_a_message_far_larger_than_the_websockets_default() -> None:
    """`_MAX_MESSAGE_BYTES` is only real on a real connection.

    `websockets` defaults to 1 MiB and *closes the connection* rather than answering when
    a frame exceeds it, so a client hitting the cap sees a disconnect with no error to
    read. Nothing but a real frame codec can prove the override reached `serve()`.
    """
    padding = "x" * (2 * 1024 * 1024)  # 2 MiB: over the default, under our 50 MiB cap.
    async with listening_server(mcp_client=None) as (_server, accepting):
        connection = await asyncio.wait_for(accepting.connect(), timeout=TIMEOUT)
        try:
            await connection.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": PROTOCOL_VERSION, "_padding": padding}}
                )
            )
            reply = json.loads(await asyncio.wait_for(connection.recv(), timeout=TIMEOUT))
        finally:
            await connection.close()

    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION


async def test_two_clients_are_served_at_once_each_with_its_own_agent() -> None:
    """One session registry, two connections: `on_connect` must not cross the wires."""
    async with listening_server(mcp_client=None) as (_server, accepting):
        first = await asyncio.wait_for(accepting.connect(), timeout=TIMEOUT)
        second = await asyncio.wait_for(accepting.connect(), timeout=TIMEOUT)
        try:
            for index, connection in enumerate((first, second), start=1):
                await connection.send(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": index, "method": "session/new",
                         "params": {"cwd": "/tmp", "mcpServers": []}}
                    )
                )
            replies = [
                json.loads(await asyncio.wait_for(connection.recv(), timeout=TIMEOUT))
                for connection in (first, second)
            ]
        finally:
            await first.close()
            await second.close()

    ids = [reply["result"]["sessionId"] for reply in replies]
    assert len(set(ids)) == 2, "two connections must not share a session"


async def test_start_is_idempotent_and_stop_leaves_the_server_restartable() -> None:
    """`start()` returns early when already started; `stop()` clears the handle.

    Both branches are pure lifecycle and were unreachable while every test used a fake
    socket — `test_the_server_binds_each_accepted_socket_to_an_agent` enters at
    `_handle_client`, below both of them.
    """
    async with listening_server(mcp_client=None) as (server, _accepting):
        assert server._server is not None
        already = server._server
        await server.start()
        assert server._server is already, "a second start() must not build a second server"

    assert server._server is None, "stop() clears the handle"
    # And stopping again is a no-op rather than an AttributeError on the cleared handle.
    await server.stop()


async def test_serve_forever_returns_when_the_server_is_closed() -> None:
    """It is `start()` plus `wait_closed()`, so the only thing to prove is that it ends."""
    loop = asyncio.get_running_loop()
    real_create_server = loop.create_server

    async def create_server(protocol_factory: Any, *args: Any, **kw: Any) -> _StubAsyncioServer:
        return _StubAsyncioServer(loop)

    server = WebSocketAgentServer(None)
    loop.create_server = create_server  # type: ignore[method-assign]
    try:
        serving = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0)
        await server.stop()
        await asyncio.wait_for(serving, timeout=TIMEOUT)
    finally:
        loop.create_server = real_create_server  # type: ignore[method-assign]
