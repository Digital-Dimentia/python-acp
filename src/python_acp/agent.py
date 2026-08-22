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

import logging
from typing import Any

from acp import PROTOCOL_VERSION, RequestError
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    CloseSessionResponse,
    ClientCapabilities,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
)

from python_acp import __version__

logger = logging.getLogger(__name__)

_AGENT_NAME = "python-acp"


class PythonAcpAgent:
    """`acp.interfaces.Agent` for this bridge.

    All 15 protocol members are present. `initialize`, `cancel`, `ext_notification`,
    and `on_connect` are live; the rest raise `method_not_found` until the phase that
    owns each one fills the body in. Nothing is *declined* — see the compliance
    matrix — so no member is left off the class.
    """

    def __init__(self) -> None:
        self._client: Client | None = None
        self._client_capabilities: ClientCapabilities | None = None

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

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        """Negotiate the protocol version and declare what this agent can do.

        Version negotiation per the spec: echo the client's version when we support
        it, otherwise answer with ours and let the client decide whether to
        disconnect. We support exactly `acp.PROTOCOL_VERSION`.

        The capability block is a **promise**, and every literal in it is owned by a
        row of `docs/acp-compliance-matrix.md`. It is all-false/null here because
        nothing behind it is built yet; a literal flips in the same commit as the
        feature it advertises, never ahead of it. `pyacp-tzd.4` owns the accurate
        block once there are features to describe.
        """
        self._client_capabilities = client_capabilities
        logger.debug(
            "ACP initialize from %s (protocol %s)",
            client_info.name if client_info is not None else "<unnamed client>",
            protocol_version,
        )
        if protocol_version != PROTOCOL_VERSION:
            # Not an error: the spec has us answer with what we do support and lets
            # the client disconnect if that is unusable to it.
            logger.info(
                "Client requested ACP protocol %s; answering with %s",
                protocol_version,
                PROTOCOL_VERSION,
            )

        return InitializeResponse(
            protocolVersion=PROTOCOL_VERSION,
            agentCapabilities=AgentCapabilities(
                loadSession=False,
                promptCapabilities=PromptCapabilities(
                    image=False, audio=False, embeddedContext=False
                ),
                # MCP is a backend adapter (D6), and stdio is the only MCP transport
                # this project drives. These three gate *client-supplied* servers of
                # each transport, so they stay false until one is actually driven.
                mcpCapabilities=McpCapabilities(http=False, sse=False, acp=False),
                sessionCapabilities=SessionCapabilities(),
            ),
            # Empty on purpose: this process runs locally under the user's own
            # credentials and authenticates nobody. It is what makes `authenticate`
            # a refusal rather than a capability.
            authMethods=[],
            agentInfo=Implementation(name=_AGENT_NAME, version=__version__),
        )

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

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        raise self._not_implemented("session/new")

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        raise self._not_implemented("session/load")

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        raise self._not_implemented("session/list")

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        raise self._not_implemented("session/fork")

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        raise self._not_implemented("session/resume")

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse | None:
        raise self._not_implemented("session/close")

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        raise self._not_implemented("session/set_mode")

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        raise self._not_implemented("session/set_config_option")

    # ------------------------------------------------------------------
    # Prompt turn — body arrives in Phase 3
    # ------------------------------------------------------------------

    async def prompt(
        self, session_id: str, prompt: list[Any], **kwargs: Any
    ) -> PromptResponse:
        raise self._not_implemented("session/prompt")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Cancel the running turn for a session.

        A notification, so it must never raise: the router has no reply channel for
        one, and an exception here would surface as an unhandled error on a message
        the client is not waiting for. Cancelling a session that does not exist — the
        only case reachable until Phase 2 — is a no-op by design, because a client
        that cancels a turn already finished is behaving correctly.
        """
        logger.debug("session/cancel for %s (no turn is running yet)", session_id)

    # ------------------------------------------------------------------
    # Extension methods — the legacy MCP passthrough lands here in Phase 7
    # ------------------------------------------------------------------

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
    def _not_implemented(method: str) -> RequestError:
        """The error for a member that exists but has no body yet.

        Deliberately identical to what the router produces for an absent attribute,
        so filling in a body is the only change a later phase makes — the wire
        behaviour before it does is already correct.
        """
        return RequestError.method_not_found(method)
