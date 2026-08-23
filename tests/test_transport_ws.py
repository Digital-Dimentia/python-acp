"""Tests for the WebSocket binding and the deprecated surface it shelters.

Every test here drives a **fake socket** rather than a listening port. That is not only
to dodge the sandbox's `bind()` denial (`pyacp-22w`): the thing under test is the
message path from a frame to the SDK router and back, and a real TCP listener adds a
port, a handshake, and two timeouts without exercising one extra line of it.
`test_the_sdk_accepts_our_transport_and_completes_initialize` is the exception that
matters — it runs a real `acp.run_agent` over `WebSocketMessageTransport`, which is the
decision-B4 canary: if a future SDK changes the private `Transport` shape we conform to,
that test fails on the day the pin moves.

The MCP backend is the real `tests/fixtures/mock_mcp_server.py` subprocess, per the
repo's convention of not mocking it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION

from python_acp import __version__
from python_acp.capabilities import build_agent_capabilities
from python_acp.legacy_ws import LEGACY_METHODS, is_legacy
from python_acp.mcp_stdio import MCPStdioClient
from python_acp.sessions import SessionRegistry
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


async def test_unimplemented_acp_methods_answer_method_not_found() -> None:
    """Proof the SDK router is doing the dispatching, not a hand-rolled branch."""
    async with bound_socket() as websocket:
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/set_config_option",
             "params": {"type": "boolean", "sessionId": "s1", "configId": "c1", "value": True}}
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
    fake socket reaches. `start()`/`stop()` need a real listener and are exercised by
    running the process, not by this suite — see `pyacp-22w`.
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
