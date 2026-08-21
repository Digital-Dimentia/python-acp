from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
import websockets

from python_acp.mcp_stdio import MCPProtocolError, MCPStdioClient
from python_acp.ws_bridge import ACPWebSocketBridge


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.asyncio
async def test_list_tools_and_call_tool_over_stdio() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert tools[0]["name"] == "echo"

        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_websocket_bridge_lists_and_calls_tools() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    port = _free_port()

    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        bridge = ACPWebSocketBridge(client, port=port)
        await bridge.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"action": "list_tools"}))
                list_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert list_response["ok"] is True
                assert list_response["tools"][0]["name"] == "echo"

                await ws.send(
                    json.dumps(
                        {"action": "call_tool", "name": "echo", "arguments": {"text": "from-ws"}}
                    )
                )
                call_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert call_response["ok"] is True
                assert call_response["result"]["content"][0]["text"] == "from-ws"

                await ws.send(json.dumps({"action": "call_tool", "name": "missing"}))
                error_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert error_response["ok"] is False
        finally:
            await bridge.stop()


@pytest.mark.asyncio
async def test_call_tool_raises_for_unknown_tool() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        with pytest.raises(MCPProtocolError):
            await client.call_tool("missing", {})
