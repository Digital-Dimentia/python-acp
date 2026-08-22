"""The ACP agent runtime: `acp.interfaces.Agent` implemented against this project.

This is the protocol edge and nothing else. Every method here validates, delegates,
and serializes; session state, turn execution, and MCP calls live below it and arrive
in later phases (see `docs/module-boundaries.md`).

**Dispatch is not ours.** `acp.agent.router.build_agent_router` maps JSON-RPC method
names onto the attributes of this class, and `acp.connection` turns returned models
into results and `acp.RequestError` into error objects. Nothing in this module parses
a request id, builds an error envelope, or knows a transport exists.

Two consequences of that arrangement are easy to get wrong:

* **Every method takes `**kwargs`.** The router splats the request's `_meta` keys in
  alongside the real parameters (`acp/router.py:104-107`), so a method with a closed
  signature raises `TypeError` the first time a client attaches metadata.
* **A method this class does not define is not an error we choose — it is
  `-32601` from the router**, because every agent route is registered
  `optional=False`. That is why the not-yet-implemented methods below raise
  `method_not_found` explicitly rather than being omitted: the wire behaviour is
  identical today, and a later phase fills in a body instead of adding a member.

See `docs/acp-compliance-matrix.md` for the disposition of all 15 protocol members
and for the capability block `initialize` is allowed to advertise.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from acp import RequestError
from acp.interfaces import Client
from acp.schema import (
    AuthenticateResponse,
    CloseSessionResponse,
    ClientCapabilities,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpServerStdio,
    NewSessionResponse,
    PromptResponse,
    ResumeSessionResponse,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
)

from python_acp import __version__
from python_acp.capabilities import (
    AUTH_METHODS,
    SUPPORTED_PROTOCOL_VERSIONS,
    build_agent_capabilities,
    negotiate_protocol_version,
)
from python_acp.errors import as_request_error
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.sessions import SessionRegistry, TurnAlreadyRunningError, UnknownSessionError
from python_acp.turns import IdleTurnExecutor, TurnContext, TurnExecutor

logger = logging.getLogger(__name__)

_AGENT_NAME = "python-acp"


class PythonAcpAgent:
    """`acp.interfaces.Agent` for this bridge.

    All 15 protocol members are present. `initialize`, `cancel`, `ext_notification`,
    and `on_connect` are live; the rest raise `method_not_found` until the phase that
    owns each one fills the body in. Nothing is *declined* — see the compliance
    matrix — so no member is left off the class.
    """

    def __init__(
        self,
        sessions: SessionRegistry,
        executor: TurnExecutor | None = None,
        backends: McpBackendRegistry | None = None,
        *,
        unstable: bool = True,
    ) -> None:
        # `sessions` is required rather than defaulted, and that is the point. One
        # registry serves the whole process: the WebSocket transport builds an agent per
        # socket, and a per-agent registry would mean a client could not resume a session
        # it created on a connection that has since dropped. A default would hide that.
        self._sessions = sessions
        self._executor = executor or IdleTurnExecutor()
        # Same sharing rule as the session registry, and for the same reason: a
        # session's MCP servers outlive the connection that created it. Teardown is the
        # session registry's `on_close` hook, not ours — `cli.py` wires the two together
        # (`SessionRegistry(on_close=backends.close)`), which is the only place that can,
        # because it is the only place that constructs both.
        self._backends = backends if backends is not None else McpBackendRegistry()
        # Mirrors the connection's `use_unstable_protocol`. It changes what `initialize`
        # may advertise, because the SDK's router refuses `session/close`, `/fork`, and
        # `/resume` outright when the flag is off — see `capabilities.py`.
        self._unstable = unstable
        self._client: Client | None = None
        self._client_capabilities: ClientCapabilities | None = None

    @property
    def sessions(self) -> SessionRegistry:
        """The process-wide session registry this agent serves."""
        return self._sessions

    @property
    def backends(self) -> McpBackendRegistry:
        """The per-session MCP servers. `pyacp-hnk.2`'s executor reads through this."""
        return self._backends

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def on_connect(self, conn: Client) -> None:
        """Receive the `Client` facade the SDK built for this connection.

        This is the only way to obtain the handle used to call `session/update`,
        `fs/*`, and `terminal/*`. `AgentSideConnection.__init__` calls it, so it runs
        before any request is dispatched.
        """
        self._client = conn
        logger.debug("ACP client connected")

    @property
    def client(self) -> Client:
        """The connected client.

        Raises rather than returning `None`: a caller reaching for the client outside
        a connection is a bug in *our* code, not a protocol error, and returning
        `None` would push the failure to a random attribute access later.
        """
        if self._client is None:
            raise RuntimeError("No ACP client is connected; on_connect has not run")
        return self._client

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        """What the client declared at `initialize`, or `None` before it ran.

        Phase 4 gates every client-side call on this. `None` and "declared nothing"
        are different states and are deliberately not collapsed.
        """
        return self._client_capabilities

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    @as_request_error
    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        """Negotiate the protocol version and declare what this agent can do.

        Three things happen here, and the second is the one later phases depend on.

        **Version.** `negotiate_protocol_version` echoes the client's version when it
        is one we serve and answers with our newest when it is not. Either way this is
        not a rejection point — the client decides whether our answer is usable.

        **Client capabilities.** Whatever the client declared is stored for the life of
        the connection. Phase 4 gates every `fs/*`, `terminal/*`, and `elicitation/*`
        call on it, and a call made without checking is a conformance bug the client is
        entitled to answer `-32601` to. `None` (no `initialize` yet) is kept distinct
        from a `ClientCapabilities` that declares nothing.

        **Capabilities.** The block is a **promise**, built from
        `capabilities.AGENT_CAPABILITY_MANIFEST` and nothing else. It is not assembled
        here, so that a literal cannot be flipped without a manifest row and a test
        proving the feature it advertises actually runs.
        """
        self._client_capabilities = client_capabilities
        negotiated = negotiate_protocol_version(protocol_version)
        logger.debug(
            "ACP initialize from %s (protocol %s)",
            client_info.name if client_info is not None else "<unnamed client>",
            protocol_version,
        )
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            # Not an error: the spec has us answer with what we do support and lets
            # the client disconnect if that is unusable to it.
            logger.info(
                "Client requested ACP protocol %s; answering with %s",
                protocol_version,
                negotiated,
            )

        return InitializeResponse(
            protocolVersion=negotiated,
            agentCapabilities=build_agent_capabilities(unstable=self._unstable),
            authMethods=list(AUTH_METHODS),
            agentInfo=Implementation(name=_AGENT_NAME, version=__version__),
        )

    @as_request_error
    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        """Refuse authentication, in the protocol's own vocabulary.

        `initialize` advertises no auth methods, so every `methodId` is one we never
        offered. Answering `-32000 auth_required` rather than `-32601` is the honest
        distinction: the method exists, the credentials do not.
        """
        raise RequestError.auth_required(
            {"reason": "This agent advertises no authentication methods", "methodId": method_id}
        )

    # ------------------------------------------------------------------
    # Session lifecycle — bodies arrive in Phase 2
    # ------------------------------------------------------------------

    @as_request_error
    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        """Create a session and hand back its id.

        `cwd` and `additionalDirectories` are stored as given; `pyacp-3rw.4` enforces the
        absolute-path constraint, at this edge, in one place.

        `modes` and `configOptions` are absent because nothing offers any yet
        (`pyacp-fln.2`, `pyacp-fln.3`). The registry carries both, so those beads change
        what is created rather than what is returned.
        """
        stdio_servers = self._reject_unsupported_mcp_servers(mcp_servers or [])
        session = self._sessions.create(
            cwd, additional_directories=additional_directories or ()
        )
        try:
            await self._backends.open(session.session_id, stdio_servers)
        except Exception:
            # The session was registered a line ago and its backends did not come up.
            # Handing back an id whose tools silently do not exist is the failure this
            # whole path exists to avoid, so the session goes with them.
            await self._sessions.close(session.session_id)
            raise
        return NewSessionResponse(
            sessionId=session.session_id,
            modes=session.modes,
            configOptions=list(session.config_options) or None,
        )

    @as_request_error
    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        """Replay a session's transcript, then hand back its current settings.

        **In-process only.** Nothing here persists across a restart, so `session/load`
        succeeds for a session this process still holds and answers `-32602` for anything
        else. That is what `agentCapabilities.loadSession: true` claims — that the method
        works — not that a session outlives the agent.

        The replay goes out **before** the response, which is the ordering the spec asks
        for: a client that received the result first would have no way to tell the
        replayed updates from live ones on a session that is already running.
        """
        session = self._sessions.get(session_id)
        logger.debug("Replaying %d update(s) for session %s", len(session.history), session_id)
        for update in session.history:
            await self.client.session_update(session_id=session_id, update=update)
        return LoadSessionResponse(
            modes=session.modes,
            configOptions=list(session.config_options) or None,
        )

    @as_request_error
    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        """One page of sessions, most recently active first.

        A single page is a conforming answer, so the pagination is a courtesy — but a
        long-lived process accumulates sessions, and a client that asked for a list should
        not receive all of them at once. A cursor this agent did not issue is `-32602`
        rather than a silent restart from page one, which would loop a client forever.
        """
        page, next_cursor = self._sessions.page(cwd=cwd, cursor=cursor)
        return ListSessionsResponse(
            sessions=[session.to_info() for session in page], nextCursor=next_cursor
        )

    @as_request_error
    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        """Copy a session under a new id, sharing nothing mutable with its parent.

        Deep-copies mode and config state and opens the fork **its own** MCP
        subprocesses — sharing the parent's would make `session/close` on the fork tear
        down the parent's tools. `sessions.py` and `mcp_registry.py` both record that
        decision; this method is where the two meet.

        `mcpServers` is optional here and means "use the parent's recipe" when omitted,
        which is why `mcp_registry` keeps the specs rather than only the clients.
        """
        stdio_servers = (
            None if mcp_servers is None else self._reject_unsupported_mcp_servers(mcp_servers)
        )
        forked = self._sessions.fork(
            session_id, cwd=cwd, additional_directories=additional_directories
        )
        try:
            await self._backends.fork(session_id, forked.session_id, stdio_servers)
        except Exception:
            await self._sessions.close(forked.session_id)
            raise
        return ForkSessionResponse(
            sessionId=forked.session_id,
            modes=forked.modes,
            configOptions=list(forked.config_options) or None,
        )

    @as_request_error
    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        """Continue a session this process still holds. Shares everything.

        Distinct from `load_session`, and the difference is the replay: resume does
        **not** re-send history, because the client resuming a session it was already
        attached to has it. Load reconstitutes; resume reattaches.

        `cwd` and `mcpServers` arrive on the request but are not applied. Changing either
        mid-session would silently invalidate paths and tool names the transcript already
        refers to; a client that wants different ones wants a fork.
        """
        session = self._sessions.resume(session_id)
        return ResumeSessionResponse(
            modes=session.modes,
            configOptions=list(session.config_options) or None,
        )

    @as_request_error
    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse | None:
        """End a session and release everything bound to it.

        The registry cancels any running turn and then fires its `on_close` hook, which
        `cli.py` wires to the MCP backend registry. Closing an id we do not hold is
        `-32602`: a client that closes twice has a bug worth hearing about, and a
        notification-shaped silence is not available on a request.
        """
        await self._sessions.close(session_id)
        return CloseSessionResponse()

    @as_request_error
    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        raise self._not_implemented("session/set_mode")

    @as_request_error
    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        raise self._not_implemented("session/set_config_option")

    # ------------------------------------------------------------------
    # Prompt turn — body arrives in Phase 3
    # ------------------------------------------------------------------

    @as_request_error
    async def prompt(
        self, session_id: str, prompt: list[Any], **kwargs: Any
    ) -> PromptResponse:
        """Run one turn and answer with its `stopReason`.

        The turn runs as its own task so `session/cancel` — a *notification*, arriving on
        the same connection while this request is still open — has something to cancel.
        Running it inline would leave the cancel with nothing to reach.

        `asyncio.wait` rather than `await turn`, because the two cancellations must not be
        confused. Awaiting a cancelled task raises `CancelledError` **here**, which is
        indistinguishable from *this request* being cancelled; `wait` only raises when we
        ourselves are cancelled, so `turn.cancelled()` afterwards is an unambiguous answer
        to "did `session/cancel` reach it".

        The `stopReason` contract beyond `cancelled` and `end_turn` — limits, refusals,
        interleaving with in-flight updates and MCP calls — is `pyacp-hnk.5`'s.
        """
        session = self._sessions.get(session_id)
        context = TurnContext(session, self.client)
        turn = asyncio.create_task(
            self._executor.execute(context, prompt), name=f"acp-turn-{session_id}"
        )
        try:
            session.attach_turn(turn)
        except TurnAlreadyRunningError:
            # Never leave an un-awaited task behind on the refusal path.
            turn.cancel()
            raise

        try:
            await asyncio.wait({turn})
        except asyncio.CancelledError:
            # This request died, not the turn. Do not leave it running for a response
            # nobody will read.
            turn.cancel()
            raise
        finally:
            session.detach_turn()

        if turn.cancelled():
            return PromptResponse(stopReason="cancelled")
        # Re-raises whatever the executor raised, for `as_request_error` to map.
        return PromptResponse(stopReason=turn.result())

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Cancel the running turn for a session.

        A notification, so it must never raise: the router has no reply channel for
        one, and an exception here would surface as an unhandled error on a message
        the client is not waiting for. **Both** no-op cases are deliberate — an unknown
        session and a session with nothing running — because a client that cancels a turn
        which has already finished is behaving correctly, and there is nowhere to tell it
        otherwise.
        """
        try:
            session = self._sessions.get(session_id)
        except UnknownSessionError:
            logger.debug("session/cancel for unknown session %s; ignoring", session_id)
            return
        if not session.cancel_turn():
            logger.debug("session/cancel for %s: no turn is running", session_id)

    # ------------------------------------------------------------------
    # Extension methods — the legacy MCP passthrough lands here in Phase 7
    # ------------------------------------------------------------------

    @as_request_error
    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serve a `_`-prefixed extension request.

        `pyacp-sld.2` carries the legacy MCP passthrough (`tools/*`, `prompts/*`,
        `resources/*`) here during the D4 deprecation window. Until then there are no
        extensions, and an unknown one is a genuine method-not-found.
        """
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """Absorb a `_`-prefixed extension notification.

        Silent by contract. An unknown extension notification is not an error, and
        there is nowhere to report one to.
        """
        logger.debug("Ignoring unknown extension notification _%s", method)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _reject_unsupported_mcp_servers(mcp_servers: list[Any]) -> list[McpServerStdio]:
        """Keep the stdio servers, refuse the transports `initialize` did not advertise.

        `mcpCapabilities.http`, `.sse`, and `.acp` are all `false` in
        `capabilities.AGENT_CAPABILITY_MANIFEST`, and stdio needs no capability at all.
        Accepting an `HttpMcpServer` anyway would make the advertisement a lie and hand
        back a session whose tools silently do not exist.

        Partitioning rather than validating-then-reusing, so `mcp_registry` is handed a
        list whose element type it can rely on instead of re-checking.
        """
        unsupported = [
            server for server in mcp_servers if not isinstance(server, McpServerStdio)
        ]
        if unsupported:
            raise ValueError(
                "This agent advertises no HTTP, SSE, or ACP MCP transports; "
                f"rejected: {[getattr(server, 'name', '?') for server in unsupported]}"
            )
        return list(mcp_servers)

    @staticmethod
    def _not_implemented(method: str) -> RequestError:
        """The error for a member that exists but has no body yet.

        Deliberately identical to what the router produces for an absent attribute,
        so filling in a body is the only change a later phase makes — the wire
        behaviour before it does is already correct.
        """
        return RequestError.method_not_found(method)
