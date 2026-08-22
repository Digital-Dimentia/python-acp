from __future__ import annotations

import argparse
import asyncio

from python_acp.mcp_stdio import MCPStdioClient
from python_acp.ws_bridge import ACPWebSocketBridge


import logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose MCP tools over WebSockets without an LLM.",
    )
    parser.add_argument(
        "--mcp-command",
        nargs="+",
        required=True,
        help="Command used to start the MCP server (stdio transport).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="WebSocket host to bind.")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port to bind.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging for websocket and MCP message flow.",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")
    async with MCPStdioClient(args.mcp_command) as mcp_client:
        await mcp_client.initialize()
        bridge = ACPWebSocketBridge(mcp_client, args.host, args.port, debug=args.debug)
        await bridge.start()
        print(f"python-acp listening on ws://{args.host}:{args.port}")
        await bridge.serve_forever()


def run() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
