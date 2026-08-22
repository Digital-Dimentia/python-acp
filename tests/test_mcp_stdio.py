from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from pathlib import Path

import pytest
import websockets

from python_acp.cli import build_parser
from python_acp.mcp_stdio import MCPProtocolError, MCPStdioClient
from python_acp.ws_bridge import ACPWebSocketBridge


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def test_build_parser_accepts_debug_flag() -> None:
    args = build_parser().parse_args(["--mcp-command", "python", "script.py", "--debug"])
    assert args.debug is True


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
async def test_websocket_bridge_handles_jsonrpc_initialize() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    port = _free_port()

    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        bridge = ACPWebSocketBridge(client, port=port)
        await bridge.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": 1,
                                "clientCapabilities": {
                                    "fs": {"readTextFile": True, "writeTextFile": True}
                                },
                                "clientInfo": {
                                    "name": "acp-ui",
                                    "title": "ACP UI",
                                    "version": "0.1.16",
                                },
                            },
                        }
                    )
                )
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert response["jsonrpc"] == "2.0"
                assert response["id"] == 7
                assert response["result"]["protocolVersion"] == 1
                assert response["result"]["agentInfo"]["name"] == "python-acp"
        finally:
            await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_logs_requests_and_responses(caplog: pytest.LogCaptureFixture) -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        bridge = ACPWebSocketBridge(client, debug=True)
        with caplog.at_level(logging.DEBUG, logger="python_acp.ws_bridge"):
            result = await bridge._dispatch(json.dumps({"action": "list_tools"}))

        assert result["ok"] is True
        assert any("WebSocket request" in message for message in caplog.messages)
        assert any("WebSocket response" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_bridge_returns_jsonrpc_parse_error_for_invalid_json() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        bridge = ACPWebSocketBridge(client)
        result = await bridge._dispatch("{bad-json")

    assert result["jsonrpc"] == "2.0"
    assert result["id"] is None
    assert result["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_bridge_returns_invalid_params_for_bad_initialize_params() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        bridge = ACPWebSocketBridge(client)
        result = await bridge._dispatch(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "initialize",
                    "params": {"protocolVersion": "1"},
                }
            )
        )

    assert result["jsonrpc"] == "2.0"
    assert result["id"] == 9
    assert result["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_unknown_jsonrpc_notification_returns_no_response() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        bridge = ACPWebSocketBridge(client)
        result = await bridge._dispatch(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "unknown/notify",
                    "params": {},
                }
            )
        )

    assert result is None


@pytest.mark.asyncio
async def test_call_tool_raises_for_unknown_tool() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        with pytest.raises(MCPProtocolError):
            await client.call_tool("missing", {})
