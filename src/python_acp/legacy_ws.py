"""The deprecated WebSocket surface, quarantined behind one class.

Everything here is on its way out. Decision D4 in `docs/full-apc-plan.md` keeps the
legacy API working *through* the migration and removes it in Phase 7 (`pyacp-sld.3`), so
this module exists to hold it apart from the ACP runtime rather than to be improved.
**Add nothing to it.** New capability goes on the ACP surface, which `agent.py` serves.

## Two shapes, one deprecation

| Request | Reply |
|---|---|
| `{"action": "list_tools", ...}` | `{"ok": true, ...}` / `{"ok": false, "error": "..."}` |
| `{"method": "tools/call", ...}` | JSON-RPC result / error |

The second is the one worth explaining. `tools/*`, `prompts/*`, and `resources/*` are
**MCP methods on an ACP wire** — they are not ACP and never were, and `PythonAcpAgent`
has no members for them. Once the socket is bound to the SDK they would answer `-32601`.
That would delete a working surface with no replacement in the same release, which D4's
promise does not allow, so they are carried here under their current names until
`pyacp-sld.2` moves them onto `ext_method` as `_tools/call` and friends — a rename a
client can be told about, rather than a disappearance.

`ping` and `notifications/initialized` are here for the same reason: neither is an ACP
method, and `notifications/initialized` is in fact an MCP-ism that arrived by copy.

**`initialize` is deliberately absent.** It *is* ACP, the agent serves it, and a
WebSocket client now gets the same negotiated answer a stdio client gets — which is the
point of `pyacp-tzd.3`.
"""

from __future__ import annotations

import logging
from typing import Any

from python_acp.mcp_stdio import MCPStdioClient, tool_result_text

logger = logging.getLogger(__name__)

#: JSON-RPC methods this handler answers instead of the ACP agent. Every one is an MCP
#: method or transport plumbing; none is in `acp.meta.AGENT_METHODS`. The set is closed —
#: it shrinks as `pyacp-sld.2` moves entries to `ext_method`, and never grows.
LEGACY_METHODS = frozenset(
    {
        "ping",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
    }
)


def is_legacy(message: dict[str, Any]) -> bool:
    """Whether this message belongs to the deprecated surface rather than to ACP.

    Called on every inbound message before the SDK sees it, so it is a membership test
    and nothing more. An `action` key is unambiguous — ACP has no such field — and a
    `method` is legacy only if it is one of the names we already served.
    """
    if "action" in message:
        return True
    return message.get("method") in LEGACY_METHODS


class LegacyActionHandler:
    """Serves the deprecated surface for one WebSocket connection.

    Holds no per-connection state; it is constructed per connection anyway so that the
    transport has one object to talk to and `pyacp-sld.1` has one place to put the
    deprecation warning.
    """

    def __init__(self, mcp_client: MCPStdioClient | None) -> None:
        self._mcp_client = mcp_client

    @property
    def mcp_client(self) -> MCPStdioClient:
        """The process-wide backend this surface was built around.

        `None` once `--mcp-command` became optional (`pyacp-db3`): ACP sessions carry
        their own servers now, so a client can run without one. The deprecated surface
        cannot — it predates sessions entirely and has nowhere else to look — so it says
        so rather than failing later with something that looks like a backend fault.
        """
        if self._mcp_client is None:
            raise ValueError(
                "The deprecated WebSocket surface needs a process-wide MCP server; "
                "start python-acp with --mcp-command, or use session/new instead"
            )
        return self._mcp_client

    async def respond(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer a legacy message, or return `None` when it deserves no reply.

        Raises `ValueError` for a bad request and `MCPProtocolError` for a backend
        failure; the transport maps both through `errors.py`. Nothing here builds an
        error envelope.
        """
        if "action" in message:
            return await self.dispatch_action(message)
        return await self.dispatch_method(message)

    # ------------------------------------------------------------------
    # The `{"action": ...}` surface — `{"ok": bool}` envelopes
    # ------------------------------------------------------------------

    async def dispatch_action(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "list_tools":
            tools = await self.mcp_client.list_tools()
            logger.debug("MCP tools response: %s", tools)
            return {"ok": True, "tools": tools}

        if action == "call_tool":
            name = _required_name(request.get("name"), "call_tool", "field 'name'")
            arguments = _arguments(request.get("arguments"))
            logger.debug("Calling MCP tool '%s' with arguments %s", name, arguments)
            result = await self.mcp_client.call_tool(name, arguments)
            logger.debug("MCP tool '%s' result: %s", name, result)
            # A tool that failed is not a transport failure, so this does not raise —
            # but `ok` must not claim success either. The full result rides along in
            # both cases; the failure text is what the tool said.
            response: dict[str, Any] = {"ok": not result["isError"], "result": result}
            if result["isError"]:
                response["error"] = tool_result_text(result) or f"Tool '{name}' failed"
            return response

        if action == "list_prompts":
            prompts = await self.mcp_client.list_prompts()
            logger.debug("MCP prompts response: %s", prompts)
            return {"ok": True, "prompts": prompts}

        if action == "get_prompt":
            name = _required_name(request.get("name"), "get_prompt", "field 'name'")
            arguments = _arguments(request.get("arguments"))
            logger.debug("Getting MCP prompt '%s' with arguments %s", name, arguments)
            result = await self.mcp_client.get_prompt(name, arguments)
            logger.debug("MCP prompt '%s' result: %s", name, result)
            return {"ok": True, "result": result}

        if action == "list_resources":
            resources = await self.mcp_client.list_resources()
            logger.debug("MCP resources response: %s", resources)
            return {"ok": True, "resources": resources}

        if action == "read_resource":
            resource = request.get("name")
            if resource is None:
                resource = request.get("uri")
            resource = _required_name(resource, "read_resource", "field 'name' or 'uri'")
            arguments = _arguments(request.get("arguments"))
            logger.debug("Reading MCP resource '%s' with arguments %s", resource, arguments)
            result = await self.mcp_client.read_resource(resource, arguments)
            logger.debug("MCP resource '%s' result: %s", resource, result)
            return {"ok": True, "result": result}

        if action == "ping":
            logger.debug("WebSocket ping request")
            return {"ok": True, "pong": True}

        raise ValueError(f"Unsupported action: {action}")

    # ------------------------------------------------------------------
    # The MCP passthrough on JSON-RPC — real JSON-RPC envelopes
    # ------------------------------------------------------------------

    async def dispatch_method(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if not isinstance(params, dict):
            raise ValueError("'params' must be an object")

        if method == "notifications/initialized":
            return None

        if method == "ping":
            return _result(request_id, {"pong": True})

        if method == "tools/list":
            return _result(request_id, {"tools": await self.mcp_client.list_tools()})

        if method == "tools/call":
            name = _required_name(params.get("name"), "tools/call", "parameter 'name'")
            arguments = _arguments(params.get("arguments"))
            result = await self.mcp_client.call_tool(name, arguments)
            # `isError: true` stays inside `result` and is deliberately NOT turned into
            # a JSON-RPC error. The call succeeded; the tool did not. Collapsing it here
            # would hide the content explaining why, and would make a tool failure
            # indistinguishable from the backend being unreachable.
            return _result(request_id, result)

        if method == "prompts/list":
            return _result(request_id, {"prompts": await self.mcp_client.list_prompts()})

        if method == "prompts/get":
            name = _required_name(params.get("name"), "prompts/get", "parameter 'name'")
            arguments = _arguments(params.get("arguments"))
            return _result(request_id, await self.mcp_client.get_prompt(name, arguments))

        if method == "resources/list":
            return _result(request_id, {"resources": await self.mcp_client.list_resources()})

        if method == "resources/read":
            resource = params.get("uri")
            if resource is None:
                resource = params.get("name")
            resource = _required_name(
                resource, "resources/read", "parameter 'uri' or 'name'"
            )
            arguments = _arguments(params.get("arguments"))
            return _result(request_id, await self.mcp_client.read_resource(resource, arguments))

        # `is_legacy` gates entry, so this is unreachable unless LEGACY_METHODS and this
        # body disagree. Loud rather than a silent `None`, which a notification would
        # otherwise look like.
        raise ValueError(f"{method} is listed as legacy but has no handler")


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _required_name(value: Any, method: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{method} requires a non-empty string {label}")
    return value


def _arguments(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("'arguments' must be an object")
    return value
