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

## Framing is ours, dispatch is not

The SDK's `Transport` moves already-decoded `dict`s, so everything below JSON — malformed
text, a non-object payload — is this module's to answer. Everything above it is the
router's. There is nothing in between any more: `pyacp-sld.3` removed the deprecated
surface this module used to intercept before the SDK saw it, so `receive` now hands up
every well-formed message it reads.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlsplit

from acp import RequestError, run_agent
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from python_acp.agent import PythonAcpAgent
from python_acp.errors import to_error_object
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.sessions import SessionRegistry
from python_acp.terminals import TerminalRegistry
from python_acp.turns import TurnExecutor

logger = logging.getLogger(__name__)

# Matches the stdio binding's buffer limit. `websockets` defaults to 1 MiB, which a
# single multimodal prompt can exceed; a client that hits the cap gets its connection
# closed rather than an error, so the two transports must not disagree about the size of
# a message they will both be asked to carry.
_MAX_MESSAGE_BYTES = 50 * 1024 * 1024

#: Environment variable holding the shared access key a client must present. Unset — or
#: set to the empty string — means no key is required.
#:
#: **An environment variable and not a CLI flag, deliberately.** `argv` is world-readable
#: through `ps` on every platform this runs on, so a `--ws-key` flag would publish the
#: secret to every other user of the machine at the moment it is used to protect it.
ACCESS_KEY_ENV = "PYTHON_ACP_WS_KEY"

#: Escape hatch for `_refuse_unauthenticated_bind`. Set to `1`, `true`, or `yes` to bind a
#: non-loopback interface with no key anyway.
ALLOW_UNAUTHENTICATED_ENV = "PYTHON_ACP_WS_ALLOW_UNAUTHENTICATED"

#: Query parameter carrying the key: `ws://host:8765/?key=<secret>`.
#:
#: A query parameter is the wrong place for a secret on principle — it lands in proxy and
#: server access logs, which is where URLs get written down — and `Authorization` or a
#: `Sec-WebSocket-Protocol` token would keep it out of the URL. It is here because it is
#: the one carrier every WebSocket client library can send without custom header support,
#: and a key nobody can present protects nothing. `pyacp-smj` owns the better answer.
ACCESS_KEY_QUERY_PARAM = "key"


class UnauthenticatedBindError(RuntimeError):
    """Raised when a non-loopback bind is asked for with no key and no opt-out.

    A `RuntimeError` rather than a `ValueError`: nothing about the arguments is malformed,
    and `errors.py` maps `ValueError` to `-32602`, which would be a bizarre answer to a
    startup misconfiguration that never reaches a client.
    """


def access_key_from_env(environ: dict[str, str] | None = None) -> str | None:
    """The configured key, or `None` when there is none.

    An empty value reads as unset. `PYTHON_ACP_WS_KEY=` in a shell profile or a compose
    file is how someone spells "I turned this off", and treating it as a key that matches
    only the empty string would be a trap: every client that sent no key at all would
    still be refused, while one that sent `?key=` would be let in.
    """
    source = os.environ if environ is None else environ
    return source.get(ACCESS_KEY_ENV) or None


def unauthenticated_bind_allowed(environ: dict[str, str] | None = None) -> bool:
    """Whether the opt-out is set, read strictly.

    Only `1`, `true`, and `yes` enable it, case-insensitively. A permissive reading would
    turn `PYTHON_ACP_WS_ALLOW_UNAUTHENTICATED=0` — which says the opposite — into consent.
    """
    source = os.environ if environ is None else environ
    return source.get(ALLOW_UNAUTHENTICATED_ENV, "").strip().lower() in {"1", "true", "yes"}


def is_loopback(host: str | None) -> bool:
    """Whether binding `host` reaches only this machine. **Fails closed.**

    `None` and `""` mean every interface to `websockets.serve`, and a name we cannot parse
    as an address is not resolved here — a DNS lookup at startup is a side effect this
    function has no business having, and one that could answer differently later. Anything
    not provably loopback is treated as exposed, which is the safe direction to be wrong in.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse_unauthenticated_bind(host: str | None, key: str | None, allowed: bool) -> None:
    """Refuse to serve an unauthenticated agent to anything but this machine.

    The threat is specific and not hypothetical: `session/new` takes a `command` and
    `args` and spawns them, so a socket anyone can open is arbitrary code execution as
    whoever runs the bridge. On loopback that is the design — the client is the user. On
    any other interface it is a remote shell with no password.

    This lives in the transport rather than in `cli.py` so that a caller embedding
    `WebSocketAgentServer` in its own program inherits the guard instead of having to
    remember it.
    """
    if key is not None or allowed or is_loopback(host):
        return
    raise UnauthenticatedBindError(
        f"refusing to bind {host or 'all interfaces'} without an access key: "
        f"session/new runs commands named by the client, so an unauthenticated socket "
        f"off loopback is remote code execution. Set {ACCESS_KEY_ENV}=<secret> and "
        f"connect to ws://…/?{ACCESS_KEY_QUERY_PARAM}=<secret>, or set "
        f"{ALLOW_UNAUTHENTICATED_ENV}=1 to accept the risk."
    )


def _access_key_check(expected: str) -> Any:
    """A `process_request` hook that answers 401 unless the URL carries the key.

    Checked during the opening handshake, so a client without the key never reaches
    `initialize` and never becomes an ACP connection at all. That is why this changes
    nothing about `AUTH_METHODS`: ACP's `authenticate` is the agent presenting a
    credential, and this is admission control one layer below the protocol.
    """

    expected_bytes = expected.encode("utf-8")

    def process_request(connection: ServerConnection, request: Request) -> Response | None:
        offered = parse_qs(urlsplit(request.path).query).get(ACCESS_KEY_QUERY_PARAM, [])
        # Exactly one, so `?key=wrong&key=right` cannot be smuggled past a check that
        # scanned for any match. `compare_digest` keeps the comparison constant-time.
        if len(offered) == 1 and secrets.compare_digest(offered[0].encode("utf-8"), expected_bytes):
            return None
        logger.warning(
            "Rejected WebSocket connection from %s: %s access key",
            connection.remote_address,
            "missing" if not offered else "wrong",
        )
        # No detail in the body. A rejected client is unauthenticated by definition, so
        # it has no claim on knowing whether the key was absent, wrong, or duplicated.
        return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

    return process_request


class WebSocketMessageTransport:
    """One WebSocket connection, shaped as the SDK's message-level `Transport`.

    Three methods, matching `acp._transport.Transport` structurally: `send(dict)`,
    `receive() -> dict | None`, and `close()`. `receive` returning `None` is EOF and is
    how the SDK learns the client hung up.
    """

    def __init__(self, websocket: ServerConnection) -> None:
        self._websocket = websocket

    async def send(self, message: dict[str, Any]) -> None:
        logger.debug("WebSocket response sent to %s: %s", self._websocket.remote_address, message)
        await self._websocket.send(json.dumps(message))

    async def receive(self) -> dict[str, Any] | None:
        """The next ACP message, after answering anything that is not one.

        Loops rather than returning per frame because an unusable frame is answered
        *here* and leaves the SDK with nothing to dispatch: a parse error and a
        non-object payload are both replied to and skipped. Everything else goes up.
        """
        async for raw_message in self._websocket:
            logger.debug(
                "WebSocket request received from %s: %s", self._websocket.remote_address, raw_message
            )
            message = await self._decode(raw_message)
            if message is not None:
                return message
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
        host: str = "127.0.0.1",
        port: int = 8765,
        debug: bool = False,
        *,
        access_key: str | None = None,
        allow_unauthenticated: bool = False,
        sessions: SessionRegistry | None = None,
        backends: McpBackendRegistry | None = None,
        terminals: TerminalRegistry | None = None,
        executor: TurnExecutor | None = None,
        use_unstable_protocol: bool = True,
    ) -> None:
        # Before anything else, and in the constructor rather than in `start()`: the point
        # of the guard is that the misconfiguration never gets as far as a listening port.
        _refuse_unauthenticated_bind(host, access_key, allow_unauthenticated)
        self._host = host
        self._port = port
        self._access_key = access_key
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
            self._handle_client,
            self._host,
            self._port,
            max_size=_MAX_MESSAGE_BYTES,
            # `None` when no key is configured, which is `serve()`'s own default and
            # leaves the handshake exactly as it was.
            process_request=_access_key_check(self._access_key) if self._access_key else None,
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
    transport = WebSocketMessageTransport(websocket)
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
