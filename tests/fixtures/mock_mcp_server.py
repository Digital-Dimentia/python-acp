import json
import os
import signal
import sys
import time

# Shutdown-behaviour knobs. A well-behaved MCP server exits when its stdin
# reaches EOF; these two make it misbehave on purpose so the client's
# close-stdin -> SIGTERM -> SIGKILL escalation can be exercised end to end.
IGNORE_EOF = bool(os.environ.get("MOCK_MCP_IGNORE_EOF"))
if os.environ.get("MOCK_MCP_IGNORE_SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

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
# Adds `wipe`, `patch`, and `plain` beside `echo` on the first tools/list page, so the
# annotation -> ACP kind mapping has a destructive tool, an additive one, and one that
# says nothing. Opt-in: every other test expects this server to offer exactly one tool.
_annotated_tools = os.environ.get("MOCK_MCP_ANNOTATED_TOOLS") == "1"


# Protocol-version negotiation knobs. Default echoes back whatever the client
# proposed, which is what a server that supports the proposal must do.
#   MOCK_MCP_PROTOCOL_VERSION=<v> -> answer with <v> no matter what was
#                            proposed: the counter-offer a server makes when it
#                            cannot speak the client's revision.
#   MOCK_MCP_OMIT_PROTOCOL_VERSION=1 -> leave protocolVersion out of the result
#                            entirely, which the lifecycle forbids.
_protocol_version = os.environ.get("MOCK_MCP_PROTOCOL_VERSION")
_omit_protocol_version = os.environ.get("MOCK_MCP_OMIT_PROTOCOL_VERSION") == "1"


# Cancellation knobs. A stalled request is read and then never answered, so the
# only thing that ends the client's wait is its own request_timeout — after which
# notifications/cancelled should arrive. Both are recorded here and handed back by
# the `cancel-report` tool, so a test observes what this process really received
# instead of spying on the client.
#   MOCK_MCP_STALL_INITIALIZE=1 -> stall the handshake too, which is the one
#                            request a client MUST NOT cancel.
_stall_initialize = os.environ.get("MOCK_MCP_STALL_INITIALIZE") == "1"
_stalled_ids = []
_cancellations = []

# Replies to server-initiated requests, in arrival order. `provoke-detached` sends
# a request and does not wait for its answer, so this is where the answer is kept
# until a test asks for it with `provoke-report`.
_server_replies = []

# The params of the initialize request as they actually arrived, handed back by the
# `handshake-report` tool. A capability block is a promise made on the wire, so a test
# that wants to check it should read what this process received rather than the
# attribute the client was constructed with.
_initialize_params = None


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
    # An item factory may hand back several items for one page: `tool_for_page` does,
    # when MOCK_MCP_ANNOTATED_TOOLS asks for the annotated ones.
    items = [entry for item in items for entry in (item if isinstance(item, list) else [item])]
    result = {key: items}
    if _list_stuck:
        result["nextCursor"] = "stuck"
    elif index + 1 < _list_pages:
        result["nextCursor"] = "page-%d" % (index + 1)
    return result


def tool_for_page(index):
    if index == 0:
        echo = {
            "name": "echo",
            "description": "Echoes text",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            # A real annotation block, always present: echoing genuinely reads nothing
            # and reaches nothing, and a fixture whose only tool is unannotated could
            # not tell "we did not read the hints" from "there were none".
            "annotations": {
                "title": "Echo",
                "readOnlyHint": True,
                "openWorldHint": False,
            },
        }
        if not _annotated_tools:
            return echo
        # Opt-in so every unrelated test keeps seeing exactly one tool. These exist to
        # exercise the annotation -> ACP kind mapping end to end.
        return [
            echo,
            {
                "name": "wipe",
                "description": "Deletes things",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": False, "destructiveHint": True},
            },
            {
                "name": "patch",
                "description": "Appends things",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            },
            {
                "name": "plain",
                "description": "Says nothing about itself",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
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
        if IGNORE_EOF:
            # Stall instead of exiting, so the client has to escalate.
            while True:
                time.sleep(0.05)
        break

    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue

    method = req.get("method")
    req_id = req.get("id")

    # A reply to a request *we* sent: an id and no method. Recorded rather than
    # allowed to fall through to "method not found", which would put an error on
    # the wire addressed to an id the client is not waiting on.
    if method is None:
        _server_replies.append(req)
        continue

    if method == "notifications/initialized":
        continue

    # A notification, so it gets no reply — only a record that it arrived, with
    # whatever requestId and reason the client put on it.
    if method == "notifications/cancelled":
        _cancellations.append(req.get("params") or {})
        continue

    if method == "initialize" and _stall_initialize:
        # Read the handshake and never answer it.
        _initialize_params = req.get("params") or {}
        _stalled_ids.append(req_id)
    elif method == "initialize":
        _initialize_params = req.get("params") or {}
        result = {}
        if not _omit_protocol_version:
            proposed = (req.get("params") or {}).get("protocolVersion")
            result["protocolVersion"] = _protocol_version or proposed or "2024-11-05"
        result["serverInfo"] = {"name": "mock-mcp", "version": "1.0.0"}
        result["capabilities"] = {"tools": {}, "prompts": {}, "resources": {}}
        write({"jsonrpc": "2.0", "id": req_id, "result": result})
    elif method == "tools/list":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": list_result(req, "tools", tool_for_page),
            }
        )
    # Accept the call and never answer it. The client's request_timeout is the
    # only thing that ends the wait, which is what makes the cancellation path
    # reachable from a test at all.
    elif method == "tools/call" and req.get("params", {}).get("name") == "stall":
        _stalled_ids.append(req_id)
    # Hand back the initialize params exactly as they arrived, as JSON text, so a test
    # can assert on the protocolVersion and capability block that were really sent.
    elif method == "tools/call" and req.get("params", {}).get("name") == "handshake-report":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(_initialize_params)}],
                    "isError": False,
                },
            }
        )
    # Hand back everything stalled and every cancellation received, as JSON text.
    elif method == "tools/call" and req.get("params", {}).get("name") == "cancel-report":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"stalled": _stalled_ids, "cancelled": _cancellations}
                            ),
                        }
                    ],
                    "isError": False,
                },
            }
        )
    # Send a server-initiated request and answer the tools/call WITHOUT waiting for
    # the reply. A client whose read loop blocks inside its own request handler
    # cannot deliver this result, so a test that gets it has proved the loop is free.
    elif method == "tools/call" and req.get("params", {}).get("name") == "provoke-detached":
        args = req.get("params", {}).get("arguments", {}) or {}
        write(
            {
                "jsonrpc": "2.0",
                "id": "srv-detached",
                "method": args.get("server_method", "roots/list"),
                "params": args.get("server_params") or {},
            }
        )
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "sent"}], "isError": False},
            }
        )
    # Hand back every reply to a server-initiated request received so far.
    elif method == "tools/call" and req.get("params", {}).get("name") == "provoke-report":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(_server_replies)}],
                    "isError": False,
                },
            }
        )
    elif method == "tools/call" and req.get("params", {}).get("name") == "provoke":
        args = req.get("params", {}).get("arguments", {}) or {}
        server_method = args.get("server_method", "roots/list")
        # The params the provoked request carries. `roots/list` takes none, but
        # `elicitation/create` is only itself with a message and a schema.
        server_params = args.get("server_params") or {}
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
        write(
            {
                "jsonrpc": "2.0",
                "id": "srv-1",
                "method": server_method,
                "params": server_params,
            }
        )
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
    # A tool that FAILS: a successful JSON-RPC result carrying isError: true.
    # This is the MCP-sanctioned way to report tool-level failure, and it must
    # not reach the client as a transport error.
    elif method == "tools/call" and req.get("params", {}).get("name") == "boom":
        args = req.get("params", {}).get("arguments", {}) or {}
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": args.get("detail", "tool exploded")}
                    ],
                    "isError": True,
                },
            }
        )
    # Every MCP content type in one result, so `pyacp-eg1.1`'s mapping is exercised
    # against a real server rather than against a hand-built dict. The trailing two are
    # deliberately broken: one type nothing maps, and one of a known type missing what
    # that type needs.
    elif method == "tools/call" and req.get("params", {}).get("name") == "every-content":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "some words",
                            "annotations": {"audience": ["user"], "priority": 0.5},
                        },
                        {"type": "image", "data": "aGk=", "mimeType": "image/png"},
                        {"type": "audio", "data": "aGk=", "mimeType": "audio/wav"},
                        {
                            "type": "resource",
                            "resource": {"uri": "file:///notes.txt", "text": "embedded"},
                        },
                        {
                            "type": "resource",
                            "resource": {
                                "uri": "file:///doc.pdf",
                                "blob": "aGk=",
                                "mimeType": "application/pdf",
                            },
                        },
                        {"type": "resource_link", "name": "notes", "uri": "file:///notes.txt"},
                        {"type": "chart", "spec": {"kind": "bar"}},
                        {"type": "image", "data": "aGk="},
                    ],
                    "isError": False,
                },
            }
        )
    # A tool that succeeds with NO text content. `pyacp-8bv.2` needs it: an invocation
    # that asks for its output to be written has nothing to write here, and writing an
    # empty file would truncate one the client asked us to fill.
    elif method == "tools/call" and req.get("params", {}).get("name") == "picture":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "image", "data": "aGk=", "mimeType": "image/png"}],
                    "isError": False,
                },
            }
        )
    # A tool result that omits isError entirely. The spec defaults it to false;
    # the client is expected to fill it in rather than leave the field missing.
    elif method == "tools/call" and req.get("params", {}).get("name") == "no-flag":
        write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "no flag here"}]},
            }
        )
    # A JSON-RPC ERROR response with a caller-chosen code, so a test can prove
    # that two different server codes stay distinguishable to the client.
    elif method == "tools/call" and req.get("params", {}).get("name") == "rpc-error":
        args = req.get("params", {}).get("arguments", {}) or {}
        error = {
            "code": args.get("code", -32603),
            "message": args.get("message", "server said no"),
        }
        if "data" in args:
            error["data"] = args["data"]
        write({"jsonrpc": "2.0", "id": req_id, "error": error})
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
        # The annotated tools exist only to carry annotations; what they return is not
        # the point, so they all answer the same way.
        elif params.get("name") in ("wipe", "patch", "plain"):
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "%s ran" % params["name"]}],
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
