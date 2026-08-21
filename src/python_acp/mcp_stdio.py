from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Sequence


class MCPProtocolError(RuntimeError):
    """Raised when the MCP service responds with an error."""


@dataclass
class MCPStdioClient:
    command: Sequence[str]
    request_timeout: float = 30.0

    def __post_init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def __aenter__(self) -> "MCPStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "python-acp", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        response = await self.request("tools/list", {})
        tools = response.get("tools", [])
        if not isinstance(tools, list):
            raise MCPProtocolError("Invalid tools/list response")
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})

    async def list_prompts(self) -> list[dict[str, Any]]:
        response = await self.request("prompts/list", {})
        prompts = response.get("prompts", [])
        if not isinstance(prompts, list):
            raise MCPProtocolError("Invalid prompts/list response")
        return prompts

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request("prompts/get", {"name": name, "arguments": arguments or {}})

    async def list_resources(self) -> list[dict[str, Any]]:
        response = await self.request("resources/list", {})
        resources = response.get("resources", [])
        if not isinstance(resources, list):
            raise MCPProtocolError("Invalid resources/list response")
        return resources

    async def read_resource(
        self, resource_id: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"uri": resource_id}
        if arguments:
            params["arguments"] = arguments
        return await self.request("resources/read", params)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        async with self._lock:
            await self._write(payload)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            self._id += 1
            request_id = self._id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            await self._write(payload)
            response = await self._read_response(request_id)
        error = response.get("error")
        if error:
            raise MCPProtocolError(str(error))
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(f"Invalid response for method {method}")
        return result

    async def _write(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPProtocolError("MCP process not running")
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_response(self, request_id: int) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise MCPProtocolError("MCP process not running")
        while True:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=self.request_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise MCPProtocolError("Timed out waiting for MCP response") from exc
            if not line:
                raise MCPProtocolError("MCP process closed stdout")
            try:
                message = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "id" not in message:
                continue
            if message["id"] != request_id:
                continue
            return message
