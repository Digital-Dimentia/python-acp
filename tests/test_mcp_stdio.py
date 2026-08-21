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
async def test_list_prompts_get_prompt_and_read_resource_over_stdio() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        prompts = await client.list_prompts()
        assert prompts[0]["name"] == "greeting"

        prompt_result = await client.get_prompt("greeting", {"name": "Ava"})
        assert prompt_result["messages"][0]["content"]["text"] == "Hello, Ava!"

        resources = await client.list_resources()
        assert resources[0]["name"] == "greeting-resource"

        resource = await client.read_resource("greeting://{name}", {"name": "Ava"})
        assert resource["contents"][0]["text"] == "Hello, Ava!"


@pytest.mark.asyncio
async def test_websocket_bridge_lists_and_calls_tools_prompts_and_resources() -> None:
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

                await ws.send(json.dumps({"action": "list_prompts"}))
                prompt_list_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert prompt_list_response["ok"] is True
                assert prompt_list_response["prompts"][0]["name"] == "greeting"

                await ws.send(
                    json.dumps(
                        {"action": "get_prompt", "name": "greeting", "arguments": {"name": "Milo"}}
                    )
                )
                prompt_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert prompt_response["ok"] is True
                assert prompt_response["result"]["messages"][0]["content"]["text"] == "Hello, Milo!"

                await ws.send(json.dumps({"action": "list_resources"}))
                resource_list_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert resource_list_response["ok"] is True
                assert resource_list_response["resources"][0]["name"] == "greeting-resource"

                await ws.send(
                    json.dumps(
                        {
                            "action": "read_resource",
                            "name": "greeting://{name}",
                            "arguments": {"name": "Nia"},
                        }
                    )
                )
                resource_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert resource_response["ok"] is True
                assert resource_response["result"]["contents"][0]["text"] == "Hello, Nia!"

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
