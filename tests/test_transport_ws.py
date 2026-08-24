"""Tests for the WebSocket binding.

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
from python_acp.mcp_registry import McpBackendRegistry
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
    """A fake socket bound to a live agent.

    No MCP backend is passed in, because there is nowhere to pass one: `pyacp-sld.3`
    removed the surface that used a process-wide server, and a session's servers arrive
    in `session/new`. A test that needs a backend opens a session naming `FIXTURE_SERVER`
    — see `open_session`.
    """
    backends = McpBackendRegistry()
    sessions = SessionRegistry(on_close=backends.close)
    websocket = FakeWebSocket()
    connection = asyncio.create_task(
        serve_websocket(websocket, sessions=sessions, backends=backends)
    )
    try:
        yield websocket
    finally:
        websocket.hang_up()
        await asyncio.wait_for(connection, timeout=TIMEOUT)
        # A disconnect deliberately does **not** close sessions, so hanging up leaves
        # every backend a test opened still running. This is what `cli.py` does on the
        # way out, and the reason it has to (`pyacp-6k5`).
        await sessions.close_all()


#: One MCP server spec, as `session/new` carries it. `env` is not optional on the wire:
#: the SDK drops an entry that omits it (`pyacp-mej`).
FIXTURE_SPEC = {
    "name": "tools",
    "command": sys.executable,
    "args": [str(FIXTURE_SERVER)],
    "env": [],
}


async def open_session(websocket: FakeWebSocket, request_id: int = 900) -> str:
    """`initialize` + `session/new` + `auto-approve`, returning the session id.

    The only way to reach a backend now, and therefore the preamble to every test about
    what a backend failure looks like on the wire.

    The mode matters: in the default `execute` mode the turn sends
    `session/request_permission` and waits, and `FakeWebSocket` is a script rather than a
    client — nobody would ever answer, so the turn would hang instead of failing.
    `auto-approve` is the session saying the consent was given up front, which is exactly
    what a test that is about *error codes* wants.
    """
    await websocket.ask(
        {"jsonrpc": "2.0", "id": request_id, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION}}
    )
    created = await websocket.ask(
        {"jsonrpc": "2.0", "id": request_id + 1, "method": "session/new",
         "params": {"cwd": "/work", "mcpServers": [FIXTURE_SPEC]}}
    )
    session_id = created["result"]["sessionId"]
    await websocket.ask(
        {"jsonrpc": "2.0", "id": request_id + 2, "method": "session/set_mode",
         "params": {"sessionId": session_id, "modeId": "auto-approve"}}
    )
    return session_id


def invocation(**payload: Any) -> dict[str, Any]:
    """One prompt block in the tool-router's invocation convention."""
    return {"type": "text", "text": json.dumps(payload)}


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
    sessions = SessionRegistry()

    first = FakeWebSocket()
    first_connection = asyncio.create_task(serve_websocket(first, sessions=sessions))
    created = await first.ask(
        {"jsonrpc": "2.0", "id": 1, "method": "session/new",
         "params": {"cwd": "/tmp", "mcpServers": []}}
    )
    first.hang_up()
    await asyncio.wait_for(first_connection, timeout=TIMEOUT)

    second = FakeWebSocket()
    second_connection = asyncio.create_task(serve_websocket(second, sessions=sessions))
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
    first, second = FakeWebSocket(), FakeWebSocket()
    tasks = [asyncio.create_task(serve_websocket(ws)) for ws in (first, second)]
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
    transport = WebSocketMessageTransport(FakeWebSocket())  # type: ignore[arg-type]

    assert all(callable(getattr(transport, name, None)) for name in ("send", "receive", "close"))


# ---------------------------------------------------------------------------
# Failure fidelity, now through the only path there is (pyacp-k5w, pyacp-tzd.6)
# ---------------------------------------------------------------------------
#
# These used to drive the MCP passthrough — `{"method": "tools/call"}` straight at a
# process-wide backend — because that reached a real MCP server in one hop.
# `pyacp-sld.3` removed it, so they go through `session/new` + `session/prompt` instead.
# The claim under test is unchanged and is the one that matters: a backend's own
# JSON-RPC error code survives all the way to the client's wire, tagged `source: "mcp"`
# so it stays distinguishable from a code this bridge produced. `tests/test_errors.py`
# proves the mapping in isolation; only these prove it end to end over a socket.


async def test_distinct_mcp_error_codes_stay_distinguishable() -> None:
    async with bound_socket() as websocket:
        session_id = await open_session(websocket)
        not_found = await websocket.ask(
            {"jsonrpc": "2.0", "id": 1, "method": "session/prompt",
             "params": {"sessionId": session_id, "prompt": [invocation(
                 tool="rpc-error", arguments={"code": -32601, "message": "no such tool"})]}}
        )
        bad_params = await websocket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
             "params": {"sessionId": session_id, "prompt": [invocation(
                 tool="rpc-error", arguments={"code": -32602, "message": "bad args"})]}}
        )

    assert not_found["error"]["code"] == -32601
    assert bad_params["error"]["code"] == -32602
    assert not_found["error"]["data"]["source"] == "mcp"
    assert "no such tool" in not_found["error"]["message"]


async def test_mcp_error_data_is_forwarded() -> None:
    async with bound_socket() as websocket:
        session_id = await open_session(websocket)
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": session_id, "prompt": [invocation(
                 tool="rpc-error", arguments={"code": -32000, "data": {"retryAfter": 5}})]}}
        )

    assert reply["error"]["code"] == -32000
    assert reply["error"]["data"]["mcpData"] == {"retryAfter": 5}


async def test_a_codeless_backend_failure_never_claims_the_backend_produced_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure *we* raised must never carry `source: "mcp"`. Not usually: never."""
    monkeypatch.setenv("MOCK_MCP_LIST_STUCK", "1")
    async with bound_socket() as websocket:
        session_id = await open_session(websocket)
        # The turn's `available_commands` walks `tools/list`, which this fixture keeps
        # handing the same cursor. The bridge gives up with a code of its own.
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 4, "method": "session/prompt",
             "params": {"sessionId": session_id, "prompt": [invocation(tool="echo")]}}
        )

    assert reply["error"]["code"] == -32603
    assert "source" not in reply["error"].get("data", {})


async def test_a_tool_error_is_a_successful_result_not_a_transport_error() -> None:
    """`isError` is the tool failing, not the call. The turn still ends normally."""
    async with bound_socket() as websocket:
        session_id = await open_session(websocket)
        reply = await websocket.ask(
            {"jsonrpc": "2.0", "id": 5, "method": "session/prompt",
             "params": {"sessionId": session_id, "prompt": [invocation(tool="boom")]}}
        )

    assert "error" not in reply
    assert reply["result"]["stopReason"] == "end_turn"
    # The failure is in the tool call's own update, where a client can read what failed.
    failed = [
        m for m in websocket.sent
        if m.get("method") == "session/update"
        and m["params"]["update"].get("status") == "failed"
    ]
    assert failed, "the failed tool call should have been streamed"


async def test_requests_and_responses_are_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="python_acp.transport_ws"):
        async with bound_socket() as websocket:
            await websocket.ask(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": PROTOCOL_VERSION}}
            )

    logged = "\n".join(record.message for record in caplog.records)
    assert "WebSocket request received" in logged
    assert "WebSocket response sent" in logged


async def test_the_server_binds_each_accepted_socket_to_an_agent() -> None:
    """Covers `WebSocketAgentServer` down to `_handle_client`, which is as far as a
    fake socket reaches. The real handshake, real frames, and `start()`/`stop()` are
    covered without a listening port under "The real WebSocket" below (`pyacp-22w`).
    """
    server = WebSocketAgentServer()
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

    terminals = Watching()
    websocket = FakeWebSocket()
    connection = asyncio.create_task(serve_websocket(websocket, terminals=terminals))
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
    async with listening_server() as (_server, accepting):
        connection = await asyncio.wait_for(accepting.connect(), timeout=TIMEOUT)
        try:
            assert connection.protocol.state is State.OPEN
            assert connection.response.status_code == 101
        finally:
            await connection.close()


async def test_a_real_frame_round_trips_through_the_agent() -> None:
    """Text frames, encoded and decoded by `websockets`, not handed over as dicts."""
    async with listening_server() as (_server, accepting):
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
    async with listening_server() as (_server, accepting):
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
    async with listening_server() as (_server, accepting):
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
    async with listening_server() as (server, _accepting):
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
