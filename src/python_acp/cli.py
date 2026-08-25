from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from python_acp.agent import PythonAcpAgent
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.sessions import SessionRegistry
from python_acp.terminals import TerminalRegistry
from python_acp.transport_stdio import run_stdio
from python_acp.transport_ws import (
    UnauthenticatedBindError,
    WebSocketAgentServer,
    access_key_from_env,
    unauthenticated_bind_allowed,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose MCP tools over ACP without an LLM.",
    )
    parser.add_argument(
        "--transport",
        choices=("ws", "stdio"),
        default="ws",
        help=(
            "Client-facing transport. 'stdio' speaks ACP on this process's stdin/stdout, "
            "which is how editors spawn an agent. 'ws' is the local-automation surface, "
            "and stays the default because that is what existing deployments bind."
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="WebSocket host to bind (--transport ws only)."
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="WebSocket port to bind (--transport ws only)."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging for websocket and MCP message flow.",
    )
    return parser


def configure_logging(debug: bool) -> None:
    """Send every diagnostic to stderr, in every transport mode.

    `basicConfig` already defaults to stderr, but the default is not the point: under
    `--transport stdio` stdout is the protocol wire, so the stream is named explicitly
    here rather than relied upon.
    """
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )


async def _run(args: argparse.Namespace) -> None:
    """Build the process-wide registries and bind them to one transport.

    **No process-wide MCP server is started, and there is no flag to ask for one.**
    `--mcp-command` and the `MCPStdioClient` it built existed for the deprecated action
    surface, which predated sessions and had nowhere else to look; `pyacp-sld.3` removed
    that surface and `pyacp-sld.4` removed this with it. Every MCP server this process
    talks to is now named by a client in `session/new` and lives and dies with its
    session — which is the arrangement ACP v1 asks for, and now the only one.
    """
    configure_logging(args.debug)

    # Both registries are process-wide, and both are created here because this is the
    # only place that constructs both: a session must outlive the connection that
    # created it (`session/resume`), and its MCP servers must be torn down when it
    # closes. `on_close` is the seam between them (decision B6a) and wiring it anywhere
    # else would leave subprocesses behind.
    backends = McpBackendRegistry()
    terminals = TerminalRegistry()

    async def release_session(session_id: str) -> None:
        """Everything bound to one session's lifetime, in the one hook there is.

        `SessionRegistry` takes a single `on_close`, and both registries need it, so the
        composition lives here for the same reason the wiring does: this is the only
        place that constructs all three.

        Terminals go first. They are requests over a live connection and the client is
        waiting on `session/close`; MCP teardown is local subprocess work that nobody is
        watching.
        """
        await terminals.close(session_id)
        await backends.close(session_id)

    sessions = SessionRegistry(on_close=release_session)

    try:
        if args.transport == "stdio":
            # No disconnect hook here on purpose: over stdio the client going away *is*
            # this process ending, so the shutdown path below is the same event.
            await run_stdio(PythonAcpAgent(sessions, backends=backends, terminals=terminals))
            return

        # The key is read from the environment, never from argv — see `ACCESS_KEY_ENV`.
        # A non-loopback bind with neither a key nor the opt-out raises here, before the
        # port is bound.
        server = WebSocketAgentServer(
            args.host,
            args.port,
            debug=args.debug,
            access_key=access_key_from_env(),
            allow_unauthenticated=unauthenticated_bind_allowed(),
            sessions=sessions,
            backends=backends,
            terminals=terminals,
        )
        await server.start()
        # Never print(): under --transport stdio that corrupts the wire, and one logging
        # path in every mode is what keeps it from creeping back.
        logger.info("python-acp listening on ws://%s:%s", args.host, args.port)
        await server.serve_forever()
    finally:
        # Sessions the client never closed still own subprocesses and, if a turn was cut
        # short, terminals. `close_all` fires the hook above for each of them.
        await sessions.close_all()


def run() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except UnauthenticatedBindError as refusal:
        # Exit 2, matching argparse's own code for "you asked for something I will not
        # do", and log rather than print so the message goes to stderr like every other
        # diagnostic. A traceback would bury the one sentence that says how to fix it.
        logger.error("%s", refusal)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
