"""Bind the ACP agent to a WebSocket.

`transport_*` faces the ACP client; `mcp_*` faces the backend. This module and
`transport_stdio.py` are two bindings of the *same* agent — under `--transport ws` a
client gets the same `initialize` negotiation, the same capability block, and the same
error codes a stdio client gets, because both go through `acp.run_agent` and the SDK's
router.

## Not `acp.ws.server` (decision B4)

The bead that produced this module was titled "rebind onto `acp.ws.server`". That
premise does not survive contact with the SDK: `acp.ws.server` exposes one function,
`handle_asgi_websocket(server, scope, receive, send)`, which is an **ASGI** handler
requiring an `acp.http.server.AcpServer`. Taking it means taking starlette and uvicorn as
runtime dependencies of a process that needs neither.

So we keep the `websockets` library and meet the SDK at its *message* seam instead.
`AgentSideConnection` branches publicly on `isinstance(input_stream, Transport)` and
`Transport` is a `@runtime_checkable` `Protocol` of three methods, so
`WebSocketMessageTransport` conforms **structurally** and nothing here imports the
private `acp._transport`.

The honest cost: we depend on a shape defined in a private module, and a future SDK could
change it with no deprecation warning. The mitigation is
`tests/test_transport_ws.py::test_the_sdk_accepts_our_transport_and_completes_initialize`,
which drives a real `run_agent` over this class — so the break surfaces in CI on the day
the pin moves, not in production.

## One agent per socket, one session registry per process

Each connection gets a fresh `PythonAcpAgent`. That is not a style choice: `on_connect`
stores *the* `Client` facade on the agent and `initialize` stores *the* client's
capabilities, so a shared instance would have the second connection overwrite the first's
handle and silently answer with the wrong client's gates.

The `SessionRegistry` goes the other way and is shared by every connection. A session
outlives the socket that created it — that is what `session/resume` means — so a
per-connection registry would make a reconnecting client's sessions vanish. `cli.py`
constructs the one registry and hands it here.

The MCP backend is shared too — one subprocess bound at startup — until the Phase 2
per-session registry.

## Framing is ours, dispatch is not

The SDK's `Transport` moves already-decoded `dict`s, so everything below JSON — malformed
text, a non-object payload — is this module's to answer. Everything above it is the
router's. The deprecated surface is intercepted in between, in `receive`, and never
reaches the SDK; see `legacy_ws.py` for what "deprecated" covers and why.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from acp import RequestError, run_agent
from websockets.asyncio.server import Server, ServerConnection, serve

from python_acp.agent import PythonAcpAgent
from python_acp.errors import to_error_object, to_request_error
from python_acp.legacy_ws import LegacyActionHandler, deprecation_notice, is_legacy
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPStdioClient
from python_acp.sessions import SessionRegistry
from python_acp.terminals import TerminalRegistry
from python_acp.turns import TurnExecutor

logger = logging.getLogger(__name__)

# Matches the stdio binding's buffer limit. `websockets` defaults to 1 MiB, which a
# single multimodal prompt can exceed; a client that hits the cap gets its connection
# closed rather than an error, so the two transports must not disagree about the size of
# a message they will both be asked to carry.
_MAX_MESSAGE_BYTES = 50 * 1024 * 1024


class WebSocketMessageTransport:
    """One WebSocket connection, shaped as the SDK's message-level `Transport`.

    Three methods, matching `acp._transport.Transport` structurally: `send(dict)`,
    `receive() -> dict | None`, and `close()`. `receive` returning `None` is EOF and is
    how the SDK learns the client hung up.
    """

    def __init__(self, websocket: ServerConnection, legacy: LegacyActionHandler) -> None:
        self._websocket = websocket
        self._legacy = legacy

    async def send(self, message: dict[str, Any]) -> None:
        logger.debug("WebSocket response sent to %s: %s", self._websocket.remote_address, message)
        await self._websocket.send(json.dumps(message))

    async def receive(self) -> dict[str, Any] | None:
        """The next ACP message, after answering anything that is not one.

        Loops rather than returning per frame because a legacy request, a parse error,
        and a non-object payload all produce a reply *here* and leave the SDK with
        nothing to dispatch. Only a well-formed message the deprecated surface does not
        claim is handed up.

        Legacy requests are served inline, so a slow backend call delays the next read on
        this socket. That is what the previous implementation did too, and each socket
        has its own task, so one client cannot stall another.
        """
        async for raw_message in self._websocket:
            logger.debug(
                "WebSocket request received from %s: %s", self._websocket.remote_address, raw_message
            )
            message = await self._decode(raw_message)
            if message is None:
                continue
            if not is_legacy(message):
                return message
            await self._serve_legacy(message)
        return None

    async def close(self) -> None:
        await self._websocket.close()

    # ------------------------------------------------------------------
    # Below the SDK: framing
    # ------------------------------------------------------------------

    async def _decode(self, raw_message: str | bytes) -> dict[str, Any] | None:
        """Decode one frame, answering the client directly when it is not usable.

        `Transport` promises the SDK a `dict`, so a parse failure has no way to travel
        upward — this is the only place that can report one.
        """
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.debug("JSON parse error for request %s", raw_message)
            await self._reject(None, RequestError.parse_error())
            return None
        if not isinstance(message, dict):
            await self._reject(None, RequestError.invalid_request())
            return None
        return message

    async def _serve_legacy(self, message: dict[str, Any]) -> None:
        """Answer a deprecated request without the SDK ever seeing it.

        The `{"action": ...}` surface signals failure with `{"ok": false, "error": str}`
        and has no code field, so a mapped error is flattened back to its message for
        that shape only. The JSON-RPC half gets a real error object.

        The failure envelope carries the same `deprecated` block the success envelope
        does, built here because this is where that envelope is built. A client whose
        call failed is no less on a surface that is going away — and is arguably more
        likely to be reading the reply closely.
        """
        try:
            reply = await self._legacy.respond(message)
        except Exception as exc:  # noqa: BLE001 — mapped, not swallowed
            error = to_request_error(exc)
            if "action" in message:
                logger.debug("Legacy action error: %s", exc)
                await self.send(
                    {
                        "ok": False,
                        "error": str(exc),
                        "deprecated": deprecation_notice(message.get("action")),
                    }
                )
                return
            await self._reject(message.get("id"), error)
            return
        if reply is not None:
            await self.send(reply)

    async def _reject(self, request_id: Any, error: RequestError) -> None:
        await self.send({"jsonrpc": "2.0", "id": request_id, "error": to_error_object(error)})


class WebSocketAgentServer:
    """Serves `PythonAcpAgent` over WebSocket, one connection at a time in parallel.

    Lifecycle only. It owns no dispatch, no error codes, and no capability block — those
    moved to the SDK router, `errors.py`, and `capabilities.py` respectively when
    `pyacp-tzd.3` gutted this module's predecessor.
    """

    def __init__(
        self,
        mcp_client: MCPStdioClient | None,
        host: str = "127.0.0.1",
        port: int = 8765,
        debug: bool = False,
        *,
        sessions: SessionRegistry | None = None,
        backends: McpBackendRegistry | None = None,
        terminals: TerminalRegistry | None = None,
        executor: TurnExecutor | None = None,
        use_unstable_protocol: bool = True,
    ) -> None:
        self._mcp_client = mcp_client
        self._host = host
        self._port = port
        # One registry for every connection this server accepts — a session outlives the
        # socket that created it, which is the whole meaning of `session/resume`.
        self._sessions = sessions if sessions is not None else SessionRegistry()
        self._backends = backends if backends is not None else McpBackendRegistry()
        self._terminals = terminals if terminals is not None else TerminalRegistry()
        self._executor = executor
        self._use_unstable_protocol = use_unstable_protocol
        logger.setLevel(logging.DEBUG if debug else logging.INFO)
        self._server: Server | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await serve(
            self._handle_client, self._host, self._port, max_size=_MAX_MESSAGE_BYTES
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        await self._server.wait_closed()

    async def _handle_client(self, websocket: ServerConnection) -> None:
        """Run one ACP connection for the life of one socket.

        `run_agent` returns when `receive()` reports EOF, so this coroutine ending is
        the same event as the client disconnecting.
        """
        logger.info("WebSocket client connected: %s", websocket.remote_address)
        try:
            await serve_websocket(
                websocket,
                self._mcp_client,
                sessions=self._sessions,
                backends=self._backends,
                terminals=self._terminals,
                executor=self._executor,
                use_unstable_protocol=self._use_unstable_protocol,
            )
        finally:
            logger.info("WebSocket client disconnected: %s", websocket.remote_address)


async def serve_websocket(
    websocket: ServerConnection,
    mcp_client: MCPStdioClient | None = None,
    *,
    sessions: SessionRegistry | None = None,
    backends: McpBackendRegistry | None = None,
    terminals: TerminalRegistry | None = None,
    executor: TurnExecutor | None = None,
    use_unstable_protocol: bool = True,
) -> None:
    """Bind one already-accepted socket to a fresh agent and run until EOF.

    Split out from `WebSocketAgentServer` so a test — or a caller embedding this in its
    own server — can exercise the binding without a listening port.

    `use_unstable_protocol` defaults to **True** for the same reason `transport_stdio`
    does: `session/close`, `session/fork`, and `session/resume` are registered
    `unstable=True` in the SDK's agent router, and with the flag off the router answers
    `method_not_found` without ever calling the agent. The two transports must agree, or
    the same client gets different answers depending on how it connected.
    """
    transport = WebSocketMessageTransport(websocket, LegacyActionHandler(mcp_client))
    live_terminals = terminals if terminals is not None else TerminalRegistry()
    agent = PythonAcpAgent(
        sessions if sessions is not None else SessionRegistry(),
        executor,
        backends if backends is not None else McpBackendRegistry(),
        live_terminals,
    )
    try:
        await run_agent(
            agent,
            input_stream=transport,
            use_unstable_protocol=use_unstable_protocol,
        )
    finally:
        # The sessions stay — another connection may resume them — but the terminals this
        # client created cannot outlive it in any useful sense: `terminal/release` is a
        # request, and the connection that would carry it has just ended. So the handles
        # are dropped, nothing is released, and `terminals.md` says plainly that the
        # terminals themselves are the departed client's to reap.
        dropped = live_terminals.forget_client(agent.connected_client)
        if dropped:
            logger.info(
                "Client disconnected holding %d terminal(s); dropped the handles", dropped
            )
