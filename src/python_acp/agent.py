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
  `optional=False`. Every routed method now has a body, so that no longer bites here —
  but it is why a member must never be deleted to "decline" it.

See `docs/acp-compliance-matrix.md` for the disposition of all 15 protocol members
and for the capability block `initialize` is allowed to advertise.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Any

from acp import RequestError
from acp.interfaces import Client
from acp.helpers import update_available_commands, update_current_mode
from acp.schema import (
    AuthenticateResponse,
    ConfigOptionUpdate,
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
    SessionModeState,
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
from python_acp.elicitation import ConnectedClient, Forwarder
from python_acp.elicitation import forwarder as elicitation_forwarder
from python_acp.errors import as_request_error
from python_acp.mcp_catalogue import CatalogueEntry, McpCatalogue
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.paths import normalize_roots
from python_acp.sessions import (
    Session,
    SessionRegistry,
    TurnAlreadyRunningError,
    UnknownSessionError,
)
from python_acp.terminals import TerminalRegistry
from python_acp.turn_mcp_router import McpToolRouterExecutor
from python_acp.turns import ClientGates, Gate, TurnContext, TurnExecutor

logger = logging.getLogger(__name__)

_AGENT_NAME = "python-acp"


class PythonAcpAgent:
    """`acp.interfaces.Agent` for this bridge.

    All 15 protocol members are present **and implemented**. Nothing is declined and
    nothing answers `method_not_found` from this class any more; the only `-32601` a
    client can now get is the router's, for a name the SDK does not route at all, or
    `ext_method`'s for an unknown extension.

    `_not_implemented` used to live here, returning exactly what the router produces for
    an absent attribute so that filling in a body was the only change each phase made. It
    was deleted with its last caller (`pyacp-fln.3`).
    """

    def __init__(
        self,
        sessions: SessionRegistry,
        executor: TurnExecutor | None = None,
        backends: McpBackendRegistry | None = None,
        terminals: TerminalRegistry | None = None,
        *,
        catalogue: McpCatalogue | None = None,
        unstable: bool = True,
    ) -> None:
        # `sessions` is required rather than defaulted, and that is the point. One
        # registry serves the whole process: the WebSocket transport builds an agent per
        # socket, and a per-agent registry would mean a client could not resume a session
        # it created on a connection that has since dropped. A default would hide that.
        self._sessions = sessions
        # The backend registry has to exist before the default executor can be built
        # over it, which is why this line reads out of order with the parameters.
        backend_registry = backends if backends is not None else McpBackendRegistry()
        # Same, and for the same reason `backends` is here: the executor needs it at
        # construction time, and `session/close` needs it to reach terminals a turn on
        # another connection left running.
        terminal_registry = terminals if terminals is not None else TerminalRegistry()
        # Decision D3's default (`pyacp-hnk.2`): a deterministic MCP tool-router, no LLM.
        # `turns.IdleTurnExecutor` remains for callers that want a turn to do nothing.
        self._executor = executor or McpToolRouterExecutor(backend_registry, terminal_registry)
        # Same sharing rule as the session registry, and for the same reason: a
        # session's MCP servers outlive the connection that created it. Teardown is the
        # session registry's `on_close` hook, not ours — `cli.py` wires the two together
        # (`SessionRegistry(on_close=backends.close)`), which is the only place that can,
        # because it is the only place that constructs both.
        self._backends = backend_registry
        # Process-wide for the same reason as the other two, and torn down by the same
        # `on_close` hook — `cli.py` wires `terminals.close` alongside `backends.close`.
        # What it does *not* share with them is the disconnect path: a departed client's
        # terminals cannot be released, only forgotten. See `terminals.md`.
        self._terminals = terminal_registry
        # Mirrors the connection's `use_unstable_protocol`. It changes what `initialize`
        # may advertise, because the SDK's router refuses `session/close`, `/fork`, and
        # `/resume` outright when the flag is off — see `capabilities.py`.
        self._unstable = unstable
        # The servers an *operator* configured, which a client selects from rather than
        # supplying. Empty is the ordinary state and costs nothing: no config options, no
        # specs, and every path below behaves exactly as it did before it existed. See
        # `mcp_catalogue.py`, including why this is not `--mcp-command` returning.
        self._catalogue = catalogue if catalogue is not None else McpCatalogue()
        self._client: Client | None = None
        self._client_capabilities: ClientCapabilities | None = None
        # Commands built *before* a minting response is written, for the announcer to
        # send *after* it. Keyed by session id, and normally empty: an entry lives only
        # for the moment between `session/new` returning and the observer firing. See
        # `_prepare_commands`, and `announcer.py` for why that moment is so narrow.
        self._prepared_commands: dict[str, list[Any]] = {}

    @property
    def sessions(self) -> SessionRegistry:
        """The process-wide session registry this agent serves."""
        return self._sessions

    @property
    def backends(self) -> McpBackendRegistry:
        """The per-session MCP servers. `pyacp-hnk.2`'s executor reads through this."""
        return self._backends

    @property
    def terminals(self) -> TerminalRegistry:
        """The terminals this agent's turns created on clients (`pyacp-8bv.3`)."""
        return self._terminals

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
    def connected_client(self) -> Client | None:
        """The client facade, or `None` before `on_connect` — the non-raising form.

        For a caller cleaning up *after* a connection rather than working inside one:
        `transport_ws.py` hands this to `TerminalRegistry.forget_client` when a socket
        closes, and "there was never a client" is an ordinary answer there rather than
        the bug `client` treats it as.
        """
        return self._client

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        """What the client declared at `initialize`, or `None` before it ran.

        Phase 4 gates every client-side call on this. `None` and "declared nothing"
        are different states and are deliberately not collapsed.
        """
        return self._client_capabilities

    def _elicit_for(self, session_id: str) -> Forwarder | None:
        """This session's route from an MCP server's question to the connected human.

        `None` when the current client cannot be asked, and that `None` is what stops
        `mcp_registry` declaring the MCP `elicitation` capability to the session's
        backends — the promise and the thing that keeps it are decided together, here,
        once per session.

        The gate is read **now**, when the backends are spawned, because that is when the
        promise is made. What the forwarder itself reads later is whoever is connected
        *then*; `elicitation.py` records why those can differ and what it answers when
        they do.
        """
        if not ClientGates.of(self._client_capabilities).allows(Gate.ELICITATION_FORM):
            return None
        return elicitation_forwarder(
            session_id, self._connected, lambda: self._running_tool_call(session_id)
        )

    def _running_tool_call(self, session_id: str) -> str | None:
        """The `toolCallId` this session's turn is inside, for a forwarded elicitation.

        By session id rather than by holding the `Session`, because the lookup outlives
        any one turn and a closed session must answer `None` rather than resurrect a
        record. An unknown session is exactly that case — `session/close` races an
        `elicitation/create` already in flight — so it is answered, not raised on.
        """
        try:
            return self._sessions.get(session_id).running_tool_call
        except UnknownSessionError:
            return None

    def _connected(self) -> ConnectedClient | None:
        """Who is on the far side right now, for a caller that may outlive a connection."""
        client = self._client
        if client is None:
            return None
        return ConnectedClient(client, ClientGates.of(self._client_capabilities))

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
            agentCapabilities=build_agent_capabilities(
                unstable=self._unstable,
                # What a content block *means* depends on the executor, which D3 makes
                # swappable, so the three promptCapabilities literals come from it rather
                # than from a table that cannot see it.
                prompt_blocks=getattr(self._executor, "supported_prompt_blocks", frozenset()),
            ),
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

        `cwd` and `additionalDirectories` are validated here and nowhere else — `-32602`
        for a relative one — and stored tidied. See `paths.py` for why they are
        normalised but not resolved, and for the containment rule Phase 4.2 builds on.

        `modes` come from the executor, which is the only thing that can act on them — the
        same arrangement as `promptCapabilities`. `configOptions` come from the executor
        *and* from the MCP catalogue, one boolean per configured server; see
        `_config_options`.

        **The servers this session opens are the client's plus the catalogue's**, not one
        or the other. `mcpServers` is untouched by the catalogue existing: an editor that
        knows its own servers keeps naming them. See `_with_catalogue`, and
        `mcp_catalogue.py` for why an agent-side list exists at all.
        """
        client_servers = self._reject_unsupported_mcp_servers(mcp_servers or [])
        config_options = self._config_options()
        # Before `create`, like the path validation below it and for the same reason: a
        # refused request must leave nothing behind.
        stdio_servers = self._with_catalogue(client_servers, config_options)
        root, extra = normalize_roots(cwd, additional_directories)
        session = self._sessions.create(
            root,
            additional_directories=extra,
            modes=self._modes(),
            config_options=config_options,
        )
        try:
            # Two things go with the servers, and both are promises made in the
            # handshake: the session's roots, which every backend declares and answers
            # `roots/list` from, and the elicitation forwarder, whose presence is what
            # decides whether a backend may ask the human anything at all. See
            # `mcp_registry.backend_responder`.
            await self._backends.open(
                session.session_id,
                stdio_servers,
                session.roots,
                self._elicit_for(session.session_id),
            )
        except Exception:
            # The session was registered a line ago and its backends did not come up.
            # Handing back an id whose tools silently do not exist is the failure this
            # whole path exists to avoid, so the session goes with them.
            await self._sessions.close(session.session_id)
            raise
        # **No command announcement here, and it is not an oversight.** The client learns
        # this session's id from the response below, so a `session/update` sent from in
        # here goes out *first*, names a session it has never heard of, and is dropped by
        # a correct client. The announcement happens on the far side of that write
        # instead, from the stream observer in `announcer.py`, which is a hook the SDK
        # fires only after the response bytes have gone.
        #
        # What *is* done here is the expensive half. The observer must not await MCP I/O
        # before it sends, or a client that pipelines `session/prompt` beats it to the
        # wire; see `_prepare_commands`.
        await self._prepare_commands(session.session_id)
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
        normalize_roots(cwd, additional_directories)
        session = self._sessions.get(session_id)
        logger.debug("Replaying %d update(s) for session %s", len(session.history), session_id)
        for update in session.history:
            await self.client.session_update(session_id=session_id, update=update)
        # After the replay, not inside it: the replayed history is what *happened*, and
        # splicing a fresh listing into it would rewrite the record. This one describes
        # the session as it is now, which is why it goes last.
        await self.announce_commands(session_id)
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
        which is why `mcp_registry` keeps the specs rather than only the clients. That
        recipe already contains whatever catalogue entries the parent had on, including
        ones toggled after it was created — so omitting `mcpServers` inherits the parent's
        *selection*, not the catalogue file's defaults.

        A client that names its own servers here gets the same additive treatment as
        `session/new`: its list, plus the catalogue entries this fork's inherited config
        options have switched on.
        """
        stdio_servers = (
            None
            if mcp_servers is None
            else self._with_catalogue(
                self._reject_unsupported_mcp_servers(mcp_servers),
                self._sessions.get(session_id).config_options,
            )
        )
        root, extra = normalize_roots(cwd, additional_directories)
        forked = self._sessions.fork(session_id, cwd=root, additional_directories=extra)
        try:
            # The fork's own roots, not the parent's: `session/fork` takes its own `cwd`.
            # Its own elicitation forwarder too, for the stronger reason that a forwarder
            # carries the session id it will put on the wire.
            await self._backends.fork(
                session_id,
                forked.session_id,
                stdio_servers,
                forked.roots,
                self._elicit_for(forked.session_id),
            )
        except Exception:
            await self._sessions.close(forked.session_id)
            raise
        # Same ordering as `new_session`, and the same answer: the fork's id is news to
        # the client, so its commands are announced by `announcer.py` after this reply —
        # and built here, so that announcement is a pure send. See `_prepare_commands`.
        await self._prepare_commands(forked.session_id)
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
        # Validated even though it is not applied: accepting a relative `cwd` here and
        # silently ignoring it would tell a client its path was fine when it was both
        # invalid and unused.
        normalize_roots(cwd, additional_directories)
        session = self._sessions.resume(session_id)
        await self.announce_commands(session_id)
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
        # Normally a no-op: the announcer pops it the instant the response goes out. It
        # is here for the session whose client hung up in between, so a long-lived stdio
        # process does not accumulate command lists nobody will ever be sent.
        self._prepared_commands.pop(session_id, None)
        return CloseSessionResponse()

    @as_request_error
    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        """Switch a session's mode and tell the client it changed.

        The notification goes out even though the client is the one who asked: a client
        is not the only possible source of a change, and every mode change looking the
        same on the wire is what lets `announce_mode` serve an internal one too. It is
        also what a second client attached to the same session needs in order to stay in
        step.

        `Session.set_mode` refuses an unknown mode *and* a session that advertises none,
        so the notification cannot describe a mode the client was never offered.
        """
        session = self._sessions.get(session_id)
        session.set_mode(mode_id)
        await self.announce_mode(session)
        return SetSessionModeResponse()

    async def announce_mode(self, session: Session) -> None:
        """Emit `current_mode_update` for a session's current mode.

        One place, so an internally-originated change — a future executor deciding it
        must drop out of `auto-approve`, say — is indistinguishable on the wire from a
        client-driven one. Nothing internal changes a mode today; this exists so that
        when something does, it does not invent a second way to say so.
        """
        if session.modes is None:
            return
        await self.client.session_update(
            session_id=session.session_id,
            update=update_current_mode(session.modes.current_mode_id),
        )

    async def announce_commands(self, session_id: str) -> None:
        """Emit `available_commands_update` for a session that has just become usable.

        Called two ways, and the difference is *when*, not what. `session/load` and
        `session/resume` call it inline: both take the session id as a *parameter*, so the
        client already knows which session an update is about. The paths that mint a new
        id — `session/new` and `session/fork` — cannot, because their id reaches the
        client in the response and a notification sent before it names a session the
        client has never heard of; for those, the stream observer in
        [announcer.py](announcer.md) calls this *after* the response is on the wire.
        Either way the client holds the id before the update arrives, which is the only
        property that matters.

        What it buys: a client gets its command palette as soon as it has a session,
        without having to take a turn first.

        One door, for the same reason as `announce_mode`: a second client attached to the
        same session, and any later internal change, say it the same way.

        Optional on the executor. `TurnExecutor` declares it, but an executor is
        swappable (D3) and a third-party one written before this existed is not broken by
        it — it simply announces nothing, which is what it could honestly say anyway.

        A failure here is logged and swallowed. The list is a convenience laid on top of a
        session that is already open and working; turning a `tools/list` that timed out
        into a failed `session/new` would cost the client its session over a palette.
        """
        if self._client is None:
            # No connection, so nobody to notify. `session/new` never needed a client
            # before this announcement existed — an embedder driving the agent directly
            # still gets its session, and making a convenience notification the thing
            # that requires a connection would be a regression dressed as a feature.
            return
        build = getattr(self._executor, "available_commands", None)
        if build is None:
            return
        try:
            commands = await build(session_id)
        except Exception as exc:  # noqa: BLE001 - see the docstring: never fatal
            logger.warning(
                "Could not list commands for session %s: %s", session_id, exc
            )
            return
        await self._send_commands(session_id, commands)

    async def announce_prepared_commands(self, session_id: str) -> None:
        """Send the list `session/new` or `session/fork` already built. The announcer's door.

        The one caller is the stream observer in [announcer.py](announcer.md), and the
        difference from `announce_commands` beside it is the whole point: this **awaits
        nothing before it sends**. See `_prepare_commands` for why that matters and what
        breaks without it.

        Falls back to building when nothing was prepared — an executor with no listing, or
        one whose `tools/list` failed a moment ago. That costs the ordering guarantee and
        keeps the palette, which is the better half to lose.
        """
        commands = self._prepared_commands.pop(session_id, None)
        if commands is None:
            await self.announce_commands(session_id)
            return
        if self._client is None:
            return
        await self._send_commands(session_id, commands)

    async def _send_commands(self, session_id: str, commands: Iterable[Any]) -> None:
        """The one `available_commands_update` write, shared by both doors above."""
        await self.client.session_update(
            session_id=session_id,
            update=update_available_commands(list(commands)),
        )

    async def _prepare_commands(self, session_id: str) -> None:
        """Build a minting session's commands *before* its response is written.

        **This is an ordering fix, not a cache.** The announcement for `session/new` and
        `session/fork` has to follow the response — see `announcer.py` — and the SDK
        gives it a stream observer to ride on. But an observer is a *task*, and building
        the list costs a `tools/list` round trip per backend. Awaiting that inside the
        observer parks it on real subprocess I/O, and a client that pipelines
        `session/prompt` straight after `session/new` — the SDK's own client does — has
        its turn's first `session/update` on the wire before the announcement gets back.
        The palette then arrives *after* the updates it was supposed to precede.

        Building here closes that gap. The observer is left with a pure send, and
        `acp.task.MessageSender` is an ordered queue, so the first thing the observer
        does is enqueue — before the loop can even read the pipelined request, let alone
        run the turn that answers it. The ordering stops being a race and becomes a
        consequence of when the two coroutines are scheduled.

        Failure is silent for the same reason `announce_commands` swallows its own: a
        `tools/list` that timed out must not cost the client the session it is attached
        to. Nothing is stashed, and the announcer falls back to building the list itself
        — losing the ordering guarantee, which is the right trade against losing the
        palette entirely.
        """
        if self._client is None:
            return
        build = getattr(self._executor, "available_commands", None)
        if build is None:
            return
        try:
            self._prepared_commands[session_id] = list(await build(session_id))
        except Exception as exc:  # noqa: BLE001 - never fatal to the session
            logger.warning(
                "Could not pre-list commands for session %s: %s", session_id, exc
            )

    def _modes(self) -> SessionModeState | None:
        """The modes a new session starts with — the executor's, deep-copied.

        Copied because `Session.set_mode` mutates `current_mode_id` in place, and the
        executor's declaration is shared by every session it serves.
        """
        declared = getattr(self._executor, "session_modes", None)
        return None if declared is None else declared.model_copy(deep=True)

    @as_request_error
    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        """Set one config option and hand back the whole set.

        **One implementation for both request shapes.** The SDK discriminates
        `SetSessionConfigOptionBooleanRequest` from `SetSessionConfigOptionSelectRequest`
        on `type` and splats either into these same parameters, so the only difference
        that reaches here is what `value` holds — and `Session.set_config_option` is what
        knows which of the two the named option can take. Writing two methods would mean
        writing that check twice.

        The response carries **every** option, not just the changed one, because that is
        what the schema asks for and because a client re-rendering a settings panel wants
        the current state rather than a diff to apply.

        **An `mcp/*` option is not a stored flag but an action.** Setting one spawns or
        tears down that server for this session, and the palette is re-announced after,
        so what the client can call follows what it selected. See `_select_mcp_server`.
        """
        session = self._sessions.get(session_id)
        entry = self._catalogue.entry_for_config_id(config_id)
        if entry is None:
            session.set_config_option(config_id, value)
            await self.announce_config_options(session)
            return SetSessionConfigOptionResponse(configOptions=list(session.config_options))

        previous = session.config_option(config_id).current_value
        # Set first, act second, revert on failure. The alternative — check the type here
        # and act before setting — would mean writing `Session.set_config_option`'s
        # boolean-versus-select validation a second time, in the one place it must not
        # drift from.
        session.set_config_option(config_id, value)
        try:
            await self._select_mcp_server(session, entry, bool(value))
        except Exception:
            session.set_config_option(config_id, previous)
            raise
        await self.announce_config_options(session)
        # The session's tools just changed, so the palette that names them is stale. This
        # is the case `announce_commands` was built for: the client already knows this
        # session's id — it named it in the request — so the notification can go inline.
        await self.announce_commands(session_id)
        return SetSessionConfigOptionResponse(configOptions=list(session.config_options))

    async def _select_mcp_server(
        self, session: Session, entry: CatalogueEntry, wanted: bool
    ) -> None:
        """Spawn or tear down one catalogue server for a session that is already open.

        **Refused while a turn is running.** Closing a backend out from under a
        `tools/call` turns a live call into a broken pipe, and the client would see a
        backend error for something it did on purpose. `session/cancel` or waiting are
        both available and neither silently loses work, so refusing is the honest answer
        rather than a race with a nicer name. It applies to *both* directions: a turn
        already holds its backend map, so a server added mid-turn would be invisible to
        it anyway, and half-applying a change is worse than declining it.

        A spawn that fails leaves the option off — the caller reverts it — and the session
        otherwise untouched. This is `open`'s all-or-nothing rule at one server's
        granularity, and it is an error to the client rather than a closed session: the
        session was working a moment ago and still is.
        """
        if session.turn_is_running:
            raise RequestError.invalid_params(
                {
                    "error": (
                        f"Session {session.session_id} has a turn running; its MCP servers "
                        f"cannot change until that turn ends. Cancel it with "
                        f"session/cancel, or wait for the prompt response."
                    ),
                    "configId": entry.config_id,
                }
            )
        if wanted:
            await self._backends.add(
                session.session_id, entry.spec(), self._elicit_for(session.session_id)
            )
        else:
            await self._backends.remove(session.session_id, entry.name)

    async def announce_config_options(self, session: Session) -> None:
        """Emit `config_option_update` for a session's options.

        One door, for the same reason as `announce_mode`: a second client on the same
        session needs to stay in step, and an internally-originated change should look
        identical on the wire to a client-driven one.
        """
        if not session.config_options:
            return
        await self.client.session_update(
            session_id=session.session_id,
            update=ConfigOptionUpdate(
                sessionUpdate="config_option_update",
                configOptions=list(session.config_options),
            ),
        )

    def _config_options(self) -> tuple[Any, ...]:
        """The config options a new session starts with, deep-copied.

        Two sources, in this order: the **executor's**, which are about how a turn behaves,
        and the **catalogue's**, one boolean per configured MCP server. The catalogue's go
        last because they are the operator's list and can be long, and a client renders
        these in the order they arrive.

        Copied for the same reason as the modes: `set_config_option` mutates
        `current_value` in place, and both declarations are shared by every session.

        The catalogue's ids are namespaced `mcp/<name>`, so an entry cannot shadow one of
        the executor's — see `mcp_catalogue.CONFIG_ID_PREFIX`.
        """
        declared = (
            *getattr(self._executor, "session_config_options", ()),
            *self._catalogue.config_options(),
        )
        return tuple(option.model_copy(deep=True) for option in declared)

    def _catalogue_specs(self, config_options: Iterable[Any]) -> tuple[McpServerStdio, ...]:
        """The catalogue servers a session with these options has switched **on**.

        Reads the session's own options rather than the catalogue's `enabled` defaults,
        because the two diverge the moment a client toggles anything — and a fork inherits
        its parent's selection, not the file's.
        """
        chosen = [
            entry.name
            for option in config_options
            if (entry := self._catalogue.entry_for_config_id(option.id)) is not None
            and getattr(option, "current_value", False) is True
        ]
        return self._catalogue.specs(chosen)

    def _with_catalogue(
        self, client_servers: Sequence[McpServerStdio], config_options: Iterable[Any]
    ) -> list[McpServerStdio]:
        """Everything a session should open: what the client named, plus what it selected.

        The two sources are **additive**, which is the whole point — an editor that knows
        its own servers keeps naming them, a thin client selects from the catalogue, and
        one session can have both.

        A name in both is `-32602` naming both sources. `McpBackendRegistry.open` already
        refuses duplicates, but its message says "in `session/new`", which would be a
        misleading thing to tell someone whose collision came from a config file they may
        not have written.
        """
        catalogue_servers = self._catalogue_specs(config_options)
        if not catalogue_servers:
            return list(client_servers)
        clashes = sorted(
            {server.name for server in client_servers}
            & {server.name for server in catalogue_servers}
        )
        if clashes:
            raise RequestError.invalid_params(
                {
                    "error": (
                        f"MCP server name(s) {clashes} are both named in this request and "
                        f"configured in this agent's catalogue. A session routes tool "
                        f"calls by server name, so the two cannot both answer to it — "
                        f"rename yours, or turn the catalogue entry off."
                    ),
                    "servers": clashes,
                }
            )
        return [*client_servers, *catalogue_servers]

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

        **The response is built only after the turn task is done**, which is what makes
        "no `session/update` after the response" structural rather than a convention: an
        executor emitting from an `except CancelledError` cleanup block is still inside
        the task, so its notification is on the wire before the answer is.

        That covers every path that *has* a response. The one that does not — this request
        itself being cancelled — is closed by `context.detach()` in the `finally`: the turn
        task is asked to stop but deliberately not awaited, so without it an executor
        cleaning up could still emit for a request nobody is reading (`pyacp-48b`).

        Every `stopReason` this agent can return, and why the two limit conditions are not
        among them, is `turns.STOP_REASON_DISPOSITIONS`.
        """
        session = self._sessions.get(session_id)
        context = TurnContext(session, self.client, self._client_capabilities)
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
            # nobody will read. Not awaited on purpose: awaiting a task inside a dead
            # request is how a hang gets made if an executor ignores cancellation.
            turn.cancel()
            raise
        finally:
            session.detach_turn()
            # Closes the wire for a turn that outlives its request. Redundant on the
            # normal path — the task is already done there — and load-bearing on the
            # cancelled one, which is why it is in the `finally` rather than in the
            # `except`: one rule, no path to forget.
            context.detach()

        if turn.cancelled():
            return PromptResponse(stopReason="cancelled")
        # Re-raises whatever the executor raised, for `as_request_error` to map.
        result = turn.result()
        if session.cancellation.is_set() and result.stop_reason != "cancelled":
            # `session/cancel` was delivered and the executor finished anyway — it caught
            # the `CancelledError` and returned instead of letting it propagate. The
            # client asked this turn to stop, so answering `end_turn` would be a lie about
            # a turn it explicitly ended. The flag is per turn (`attach_turn` installs a
            # fresh event), so it cannot be a previous turn's.
            logger.warning(
                "Turn for %s answered %r after session/cancel reached it; reporting "
                "cancelled. An executor should let CancelledError propagate.",
                session_id,
                result.stop_reason,
            )
            return PromptResponse(stopReason="cancelled", usage=result.usage)
        return PromptResponse(stopReason=result.stop_reason, usage=result.usage)

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
    # Extension methods — deliberately empty
    # ------------------------------------------------------------------

    @as_request_error
    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serve a `_`-prefixed extension request.

        There are no extensions, and an unknown one is a genuine method-not-found.

        This was where the legacy MCP passthrough (`tools/*`, `prompts/*`, `resources/*`)
        was to land under a namespaced prefix. `pyacp-sld.2` declined that move: the
        passthrough addresses the process-wide `--mcp-command` server — the arrangement
        ACP v1 inverted — so it is deleted with the rest of the deprecated surface rather
        than carried onto the ACP one. Keeping this empty is the decision, not an
        omission. `docs/acp-compliance-matrix.md` row 13 carries the reasoning.
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

