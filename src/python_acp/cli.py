from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

from python_acp.agent import PythonAcpAgent
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPStdioClient
from python_acp.sessions import SessionRegistry
from python_acp.transport_stdio import run_stdio
from python_acp.transport_ws import WebSocketAgentServer

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose MCP tools over ACP without an LLM.",
    )
    parser.add_argument(
        "--mcp-command",
        nargs="+",
        default=None,
        help=(
            "Command used to start a process-wide MCP server (stdio transport). "
            "Optional since ACP sessions carry their own servers in session/new; it is "
            "still required by the deprecated WebSocket action surface, which predates "
            "sessions and has nowhere else to look."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("ws", "stdio"),
        default="ws",
        help=(
            "Client-facing transport. 'stdio' speaks ACP on this process's stdin/stdout, "
            "which is how editors spawn an agent. 'ws' is the existing local-automation "
            "surface and remains the default while it still carries the legacy actions."
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


@contextlib.asynccontextmanager
async def _startup_backend(command: list[str] | None) -> AsyncIterator[MCPStdioClient | None]:
    """The process-wide MCP server, if one was asked for.

    Started and handshaked before any listener binds, so a bad `--mcp-command` fails at
    startup rather than mid-session. It is **not** what ACP sessions use — those carry
    their own servers (`pyacp-db3`) — and exists only for the deprecated action surface.
    """
    if command is None:
        yield None
        return
    async with MCPStdioClient(command) as mcp_client:
        await mcp_client.initialize()
        yield mcp_client


async def _run(args: argparse.Namespace) -> None:
    configure_logging(args.debug)

    async with _startup_backend(args.mcp_command) as mcp_client:
        # Both registries are process-wide, and both are created here because this is
        # the only place that constructs both: a session must outlive the connection
        # that created it (`session/resume`), and its MCP servers must be torn down when
        # it closes. `on_close` is the seam between them (decision B6a) and wiring it
        # anywhere else would leave subprocesses behind.
        backends = McpBackendRegistry()
        sessions = SessionRegistry(on_close=backends.close)

        try:
            if args.transport == "stdio":
                await run_stdio(PythonAcpAgent(sessions, backends=backends))
                return

            server = WebSocketAgentServer(
                mcp_client,
                args.host,
                args.port,
                debug=args.debug,
                sessions=sessions,
                backends=backends,
            )
            await server.start()
            # Never print(): under --transport stdio that corrupts the wire, and one
            # logging path in every mode is what keeps it from creeping back.
            logger.info("python-acp listening on ws://%s:%s", args.host, args.port)
            await server.serve_forever()
        finally:
            # Sessions the client never closed still own subprocesses.
            await sessions.close_all()


def run() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
