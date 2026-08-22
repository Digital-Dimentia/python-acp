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


# Cursor pagination knobs. Default is a single page with no nextCursor, which
# is what every pre-existing test expects.
#   MOCK_MCP_LIST_PAGES=N  -> serve N pages; nextCursor is absent on the last.
#   MOCK_MCP_LIST_STUCK=1  -> always hand back the same nextCursor, forever.
#   MOCK_MCP_LIST_EMPTY_MIDDLE=1 -> page 0 carries no items but does carry a
#                            nextCursor, proving an empty page is not a terminator.
_list_pages = int(os.environ.get("MOCK_MCP_LIST_PAGES", "1"))
_list_stuck = os.environ.get("MOCK_MCP_LIST_STUCK") == "1"
_list_empty_middle = os.environ.get("MOCK_MCP_LIST_EMPTY_MIDDLE") == "1"


def write(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def page_index(req):
    """Recover the requested page number from the opaque cursor we minted."""
    cursor = (req.get("params") or {}).get("cursor")
    if not isinstance(cursor, str) or not cursor.startswith("page-"):
        return 0
    try:
        return int(cursor[len("page-") :])
    except ValueError:
        return 0


def list_result(req, key, item_for_page):
    """Build one page of a cursor-paginated list result."""
    index = page_index(req)
    if _list_empty_middle and index == 0:
        items = []
    else:
        items = [item_for_page(index)]
    result = {key: items}
    if _list_stuck:
        result["nextCursor"] = "stuck"
    elif index + 1 < _list_pages:
        result["nextCursor"] = "page-%d" % (index + 1)
    return result


def tool_for_page(index):
    if index == 0:
        return {
            "name": "echo",
            "description": "Echoes text",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    return {
        "name": "echo-%d" % index,
        "description": "Echoes text (page %d)" % index,
        "inputSchema": {"type": "object", "properties": {}},
    }


def prompt_for_page(index):
    if index == 0:
        return {
            "name": "greeting",
            "description": "Build a greeting message",
            "arguments": [{"name": "name", "required": True}],
        }
    return {
        "name": "greeting-%d" % index,
        "description": "Build a greeting message (page %d)" % index,
        "arguments": [],
    }


def resource_for_page(index):
    if index == 0:
        return {
            "uri": "greeting://{name}",
            "name": "greeting-resource",
            "description": "A greeting resource",
            "mimeType": "text/plain",
        }
    return {
        "uri": "greeting://page-%d" % index,
        "name": "greeting-resource-%d" % index,
        "description": "A greeting resource (page %d)" % index,
        "mimeType": "text/plain",
    }


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
                "result": list_result(req, "tools", tool_for_page),
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
                "result": list_result(req, "prompts", prompt_for_page),
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
                "result": list_result(req, "resources", resource_for_page),
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
