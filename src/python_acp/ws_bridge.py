from __future__ import annotations

import json
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from python_acp.mcp_stdio import MCPProtocolError, MCPStdioClient


class ACPWebSocketBridge:
    def __init__(self, mcp_client: MCPStdioClient, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._mcp_client = mcp_client
        self._host = host
        self._port = port
        self._server: Any = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await websockets.serve(self._handle_client, self._host, self._port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def serve_forever(self) -> None:
        await self.start()
        await self._server.wait_closed()

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        async for raw_message in websocket:
            response = await self._dispatch(raw_message)
            await websocket.send(json.dumps(response))

    async def _dispatch(self, raw_message: str) -> dict[str, Any]:
        try:
            request = json.loads(raw_message)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")

            action = request.get("action")
            if action == "list_tools":
                tools = await self._mcp_client.list_tools()
                return {"ok": True, "tools": tools}

            if action == "call_tool":
                name = request.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("call_tool requires a non-empty string field 'name'")
                arguments = request.get("arguments")
                if arguments is None:
                    arguments = {}
                if not isinstance(arguments, dict):
                    raise ValueError("'arguments' must be an object")
                result = await self._mcp_client.call_tool(name, arguments)
                return {"ok": True, "result": result}

            if action == "ping":
                return {"ok": True, "pong": True}

            raise ValueError(f"Unsupported action: {action}")
        except (ValueError, json.JSONDecodeError, MCPProtocolError) as exc:
            return {"ok": False, "error": str(exc)}
