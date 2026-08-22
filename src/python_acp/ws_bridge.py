from __future__ import annotations

import json
import logging
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from python_acp.mcp_stdio import MCPProtocolError, MCPStdioClient

logger = logging.getLogger("python_acp.ws_bridge")


class ACPWebSocketBridge:
    _SUPPORTED_PROTOCOL_VERSION = 1

    def __init__(
        self,
        mcp_client: MCPStdioClient,
        host: str = "127.0.0.1",
        port: int = 8765,
        debug: bool = False,
    ) -> None:
        self._mcp_client = mcp_client
        self._host = host
        self._port = port
        self._debug = debug
        logger.setLevel(logging.DEBUG if debug else logging.INFO)
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
        logger.info("WebSocket client connected: %s", websocket.remote_address)
        async for raw_message in websocket:
            logger.debug("WebSocket request received from %s: %s", websocket.remote_address, raw_message)
            response = await self._dispatch(raw_message)
            if response is None:
                continue
            logger.debug("WebSocket response sent to %s: %s", websocket.remote_address, response)
            await websocket.send(json.dumps(response))
        logger.info("WebSocket client disconnected: %s", websocket.remote_address)

    async def _dispatch(self, raw_message: str) -> dict[str, Any] | None:
        request: dict[str, Any] | None = None
        try:
            request = json.loads(raw_message)
            if not isinstance(request, dict):
                return self._jsonrpc_error(None, -32600, "Invalid request")

            logger.debug("WebSocket request: %s", request)
            if "action" in request:
                try:
                    return await self._dispatch_legacy_action(request)
                except (ValueError, MCPProtocolError) as exc:
                    logger.debug("Legacy WebSocket error for request %s: %s", raw_message, exc)
                    response = {"ok": False, "error": str(exc)}
                    logger.debug("WebSocket response: %s", response)
                    return response
            if "method" in request:
                method = request.get("method")
                if not isinstance(method, str) or not method:
                    return self._jsonrpc_error(request.get("id"), -32600, "Invalid request")
                try:
                    return await self._dispatch_jsonrpc(request)
                except ValueError as exc:
                    logger.debug("JSON-RPC params error for request %s: %s", raw_message, exc)
                    return self._jsonrpc_error(request.get("id"), -32602, str(exc))
                except MCPProtocolError as exc:
                    logger.debug("JSON-RPC internal error for request %s: %s", raw_message, exc)
                    return self._jsonrpc_error(request.get("id"), -32603, str(exc))
            return self._jsonrpc_error(request.get("id"), -32600, "Invalid request")
        except json.JSONDecodeError:
            logger.debug("JSON parse error for request %s", raw_message)
            return self._jsonrpc_error(None, -32700, "Parse error")

    @staticmethod
    def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    async def _dispatch_legacy_action(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "list_tools":
            tools = await self._mcp_client.list_tools()
            logger.debug("MCP tools response: %s", tools)
            response = {"ok": True, "tools": tools}
            logger.debug("WebSocket response: %s", response)
            return response

        if action == "call_tool":
            name = request.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("call_tool requires a non-empty string field 'name'")
            arguments = request.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ValueError("'arguments' must be an object")
            logger.debug("Calling MCP tool '%s' with arguments %s", name, arguments)
            result = await self._mcp_client.call_tool(name, arguments)
            logger.debug("MCP tool '%s' result: %s", name, result)
            response = {"ok": True, "result": result}
            logger.debug("WebSocket response: %s", response)
            return response

        if action == "list_prompts":
            prompts = await self._mcp_client.list_prompts()
            logger.debug("MCP prompts response: %s", prompts)
            response = {"ok": True, "prompts": prompts}
            logger.debug("WebSocket response: %s", response)
            return response

        if action == "get_prompt":
            name = request.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("get_prompt requires a non-empty string field 'name'")
            arguments = request.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ValueError("'arguments' must be an object")
            logger.debug("Getting MCP prompt '%s' with arguments %s", name, arguments)
            result = await self._mcp_client.get_prompt(name, arguments)
            logger.debug("MCP prompt '%s' result: %s", name, result)
            response = {"ok": True, "result": result}
            logger.debug("WebSocket response: %s", response)
            return response

        if action == "list_resources":
            resources = await self._mcp_client.list_resources()
            logger.debug("MCP resources response: %s", resources)
            response = {"ok": True, "resources": resources}
            logger.debug("WebSocket response: %s", response)
            return response

        if action == "read_resource":
            resource = request.get("name")
            if resource is None:
                resource = request.get("uri")
            if not isinstance(resource, str) or not resource:
                raise ValueError("read_resource requires a non-empty string field 'name' or 'uri'")
            arguments = request.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ValueError("'arguments' must be an object")
            logger.debug("Reading MCP resource '%s' with arguments %s", resource, arguments)
            result = await self._mcp_client.read_resource(resource, arguments)
            logger.debug("MCP resource '%s' result: %s", resource, result)
            response = {"ok": True, "result": result}
            logger.debug("WebSocket response: %s", response)
            return response

        if action == "ping":
            logger.debug("WebSocket ping request")
            response = {"ok": True, "pong": True}
            logger.debug("WebSocket response: %s", response)
            return response

        raise ValueError(f"Unsupported action: {action}")

    async def _dispatch_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if not isinstance(params, dict):
            raise ValueError("'params' must be an object")

        if method == "initialize":
            protocol_version = params.get("protocolVersion")
            if not isinstance(protocol_version, int):
                raise ValueError("initialize requires an integer parameter 'protocolVersion'")
            result = {
                "protocolVersion": self._SUPPORTED_PROTOCOL_VERSION,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {
                        "image": False,
                        "audio": False,
                        "embeddedContext": False,
                    },
                    "mcpCapabilities": {"http": False, "sse": False},
                    "auth": {"logout": None},
                    "sessionCapabilities": {
                        "delete": None,
                        "additionalDirectories": None,
                    },
                },
                "agentInfo": {
                    "name": "python-acp",
                    "title": "python-acp",
                    "version": "0.1.0",
                },
                "authMethods": [],
            }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        if method == "notifications/initialized":
            return None

        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"pong": True}}

        if method == "tools/list":
            tools = await self._mcp_client.list_tools()
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not name:
                raise ValueError("tools/call requires a non-empty string parameter 'name'")
            if not isinstance(arguments, dict):
                raise ValueError("'arguments' must be an object")
            result = await self._mcp_client.call_tool(name, arguments)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        if method == "prompts/list":
            prompts = await self._mcp_client.list_prompts()
            return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": prompts}}

        if method == "prompts/get":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not name:
                raise ValueError("prompts/get requires a non-empty string parameter 'name'")
            if not isinstance(arguments, dict):
                raise ValueError("'arguments' must be an object")
            result = await self._mcp_client.get_prompt(name, arguments)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        if method == "resources/list":
            resources = await self._mcp_client.list_resources()
            return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": resources}}

        if method == "resources/read":
            resource = params.get("uri")
            if resource is None:
                resource = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(resource, str) or not resource:
                raise ValueError("resources/read requires a non-empty string parameter 'uri' or 'name'")
            if not isinstance(arguments, dict):
                raise ValueError("'arguments' must be an object")
            result = await self._mcp_client.read_resource(resource, arguments)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        if request_id is None:
            return None

        error = {"code": -32601, "message": f"Unsupported method: {method}"}
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
