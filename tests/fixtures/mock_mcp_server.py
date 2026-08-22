import json
import os
import sys

# Emit a burst of stderr before serving anything. With no drain on the client
# side this fills the OS pipe buffer and blocks the server mid-write, before it
# can answer a single request.
_stderr_bytes = int(os.environ.get("MOCK_MCP_STDERR_BYTES", "0"))
if _stderr_bytes:
    _written = 0
    while _written < _stderr_bytes:
        _written += sys.stderr.write("mock-mcp noise " + "x" * 240 + "\n")
    sys.stderr.flush()


def write(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


while True:
    line = sys.stdin.readline()
    if not line:
        break

    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue

    method = req.get("method")
    req_id = req.get("id")

    if method == "notifications/initialized":
        continue

    if method == "initialize":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "serverInfo": {"name": "mock-mcp", "version": "1.0.0"},
                    "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
                },
            }
        )
    elif method == "tools/list":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echoes text",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        }
                    ]
                },
            }
        )
    elif method == "tools/call" and req.get("params", {}).get("name") == "provoke":
        args = req.get("params", {}).get("arguments", {}) or {}
        server_method = args.get("server_method", "roots/list")
        # A notification the client should route to its notification handler.
        write(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "provoked"},
            }
        )
        # A server-initiated request. The client must answer it; we echo whatever
        # it sends back so the test can assert on the reply it actually produced.
        write({"jsonrpc": "2.0", "id": "srv-1", "method": server_method, "params": {}})
        reply = sys.stdin.readline()
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": reply.strip()}],
                    "isError": False,
                },
            }
        )
    elif method == "tools/call":
        params = req.get("params", {})
        if params.get("name") == "echo":
            text = params.get("arguments", {}).get("text", "")
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                }
            )
        else:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Unknown tool"},
                }
            )
    elif method == "prompts/list":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "prompts": [
                        {
                            "name": "greeting",
                            "description": "Build a greeting message",
                            "arguments": [{"name": "name", "required": True}],
                        }
                    ]
                },
            }
        )
    elif method == "prompts/get":
        params = req.get("params", {})
        if params.get("name") == "greeting":
            name = params.get("arguments", {}).get("name", "friend")
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "description": "Greeting prompt",
                        "messages": [
                            {
                                "role": "user",
                                "content": {"type": "text", "text": f"Hello, {name}!"},
                            }
                        ],
                    },
                }
            )
        else:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Unknown prompt"},
                }
            )
    elif method == "resources/list":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "resources": [
                        {
                            "uri": "greeting://{name}",
                            "name": "greeting-resource",
                            "description": "A greeting resource",
                            "mimeType": "text/plain",
                        }
                    ]
                },
            }
        )
    elif method == "resources/read":
        params = req.get("params", {})
        resource_uri = params.get("uri") or params.get("name")
        arguments = params.get("arguments", {}) or {}
        if resource_uri == "greeting://{name}" or resource_uri == "greeting://" or resource_uri.startswith("greeting://"):
            name = arguments.get("name", resource_uri.split("//", 1)[1] or "friend")
            content = f"Hello, {name}!"
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [{"uri": resource_uri, "mimeType": "text/plain", "text": content}]
                    },
                }
            )
        else:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Unknown resource"},
                }
            )
    else:
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
