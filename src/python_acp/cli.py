from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from python_acp.agent import PythonAcpAgent
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
        required=True,
        help="Command used to start the MCP server (stdio transport).",
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


async def _run(args: argparse.Namespace) -> None:
    configure_logging(args.debug)

    async with MCPStdioClient(args.mcp_command) as mcp_client:
        await mcp_client.initialize()

        # One registry for the process, whichever transport is bound. A session must
        # outlive the connection that created it or `session/resume` means nothing, so
        # this is created here rather than inside a transport or an agent.
        sessions = SessionRegistry()

        if args.transport == "stdio":
            # The backend is started and handshaked in both modes so --mcp-command
            # means the same thing either way and a bad one fails at startup rather
            # than mid-session. The agent cannot reach it yet — that wiring is the
            # per-session backend registry in Phase 2 (pyacp-3rw.3, pyacp-db3). The
            # WebSocket transport does hand it to the deprecated surface, which is the
            # only thing still calling MCP directly.
            await run_stdio(PythonAcpAgent(sessions))
            return

        server = WebSocketAgentServer(
            mcp_client, args.host, args.port, debug=args.debug, sessions=sessions
        )
        await server.start()
        # Never print(): under --transport stdio that corrupts the wire, and one
        # logging path in every mode is what keeps it from creeping back.
        logger.info("python-acp listening on ws://%s:%s", args.host, args.port)
        await server.serve_forever()


def run() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
