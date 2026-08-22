---
name: acp-protocol
description: Use when changing python-acp's WebSocket request surface — adding or modifying an action, a JSON-RPC method, an error response, or an MCP passthrough. Covers the dual legacy-action / JSON-RPC dispatch in ws_bridge.py, the JSON-RPC error-code mapping, and the full checklist of files one new method must touch. Trigger on work involving ws_bridge.py, mcp_stdio.py, ACP protocol compliance, or the ACP v1 migration in docs/full-apc-plan.md.
---

# python-acp Wire Contract

`ws_bridge.py` accepts **two different request shapes on the same socket**, and they
return **different response envelopes**. This is not visible from any single function,
and it is the most common thing to get wrong.

## The two surfaces

Dispatch is chosen in `ACPWebSocketBridge._dispatch` by which key is present:

| Request key | Handler | Success envelope | Failure envelope |
|---|---|---|---|
| `"action"` | `_dispatch_legacy_action` | `{"ok": true, ...}` | `{"ok": false, "error": "<str>"}` |
| `"method"` | `_dispatch_jsonrpc` | `{"jsonrpc": "2.0", "id": ..., "result": {...}}` | `{"jsonrpc": "2.0", "id": ..., "error": {"code": ..., "message": ...}}` |

Neither key present, or a non-dict payload → JSON-RPC `-32600`.

**`action` is the legacy surface.** Under decision D4 in `docs/full-apc-plan.md` it
keeps working — with a deprecation warning — through the whole migration, and is
removed only in Phase 7 (`pyacp-sld.3`), after Phase 8 proves JSON-RPC parity. D5 is
the terminal JSON-RPC-only state. Do not add new capabilities to
`_dispatch_legacy_action` unless the task explicitly says to keep parity. Add to
`_dispatch_jsonrpc` first.

## Error-code mapping

Legacy actions signal failure by raising; JSON-RPC maps exception type to code.
Preserve this mapping — tests assert on the codes:

| Condition | Code | Raised as |
|---|---|---|
| Malformed JSON on the wire | `-32700` | `json.JSONDecodeError` caught in `_dispatch` |
| Non-dict payload, missing/empty `method`, no `action` or `method` | `-32600` | returned directly |
| Known shape, unhandled method | `-32601` | fallthrough at end of `_dispatch_jsonrpc` |
| Bad or missing params | `-32602` | **raise `ValueError`** |
| MCP backend failed, server sent a code | *the server's own code* | **raise `MCPProtocolError`** with `code=` |
| MCP backend failed, no code available | `-32603` | **raise `MCPProtocolError`** with `code=None` |

So inside `_dispatch_jsonrpc` you never build an error envelope for validation
problems — you `raise ValueError("...")` and let `_dispatch` map it. Legacy handlers
raise the same two exception types; `_dispatch` flattens both to `{"ok": false}`.

### Backend codes are forwarded, not collapsed

`_mcp_error` does not flatten every backend failure to `-32603`. When the MCP server
returned a JSON-RPC error, `MCPProtocolError.from_error_response` captures its `code`
and `data`, and the bridge re-emits **that code** so a backend `-32601` (no such tool)
stays distinguishable from a backend `-32602` (bad arguments).

That makes the code space ambiguous — the same integer can now come from the bridge or
from the server — so a forwarded error always carries a `data` marker:

```json
{"jsonrpc": "2.0", "id": 1,
 "error": {"code": -32601, "message": "MCP error -32601: Unknown tool",
           "data": {"source": "mcp", "mcpCode": -32601, "mcpData": {...}}}}
```

- `data.source == "mcp"` is the discriminator. **The bridge's own errors carry no
  `data.source`.** Never emit `source: "mcp"` for an error the bridge originated.
- `mcpData` is present only when the server supplied `data`.
- Failures the client raises itself — timeout, transport death, a malformed result —
  have no server code, so they take the `-32603` path with no `data` at all.
- A malformed `error` member (not a dict, or a non-int `code`) degrades to the codeless
  path rather than forwarding junk; `from_error_response` enforces that.

A failed **tool call** is not a protocol error at all: `tools/call` reports tool-level
failure via `isError` on an otherwise successful result. Do not translate it into a
JSON-RPC error.

## Notifications

A JSON-RPC message with no `id` is a notification: return `None`, and `_handle_client`
sends nothing. `notifications/initialized` already does this. The `-32601` fallthrough
is guarded by `if request_id is None: return None` so unknown notifications stay silent
rather than erroring.

## Capability advertisement

`initialize` returns a hand-built capability block. It is a **promise**, not a
reflection of runtime state — `mcpCapabilities: {http: false, sse: false}` and the
`false`/`None` entries under `agentCapabilities` are literals. If you implement one of
those features, flip the literal in the same change; if you flip a literal, make sure
the feature actually exists.

`_SUPPORTED_PROTOCOL_VERSION = 1` is the ACP version echoed to WebSocket clients. It is
unrelated to the MCP protocol version `"2024-11-05"` hardcoded in
`MCPStdioClient.initialize` — two different protocols, two different version fields.

## Adding a method: the checklist

Every one of these is required. Docs are not optional here; see the
`repo-docs-sync` skill for why.

1. `src/python_acp/mcp_stdio.py` — add the `MCPStdioClient` method if it needs a new
   MCP call. Validate the response shape and raise `MCPProtocolError` on anything
   unexpected, matching `list_tools` / `list_prompts`.
2. `src/python_acp/ws_bridge.py` — add the branch to `_dispatch_jsonrpc`. Add to
   `_dispatch_legacy_action` only for parity with an existing action.
3. `tests/fixtures/mock_mcp_server.py` — teach the mock server to answer the new MCP
   method, or the test cannot exercise the path.
4. `tests/test_mcp_stdio.py` — cover success **and** the error code. `asyncio_mode`
   is `auto` in `pyproject.toml`, so `async def test_*` needs no decorator.
5. `src/python_acp/ws_bridge.md` (and `mcp_stdio.md` if step 1 applied) — document it.
6. `ARCHITECTURE.md` — update the sequence diagram if the request path changed shape.
7. `README.md` — add the example payload under "WebSocket actions".

## Conventions inside the dispatchers

- Argument coercion is uniform: `arguments = request.get("arguments") or {}`, then
  `if not isinstance(arguments, dict): raise ValueError("'arguments' must be an object")`.
- `read_resource` / `resources/read` accept **either** `name` or `uri`, preferring
  `uri` on the JSON-RPC side and `name` on the legacy side. Keep both aliases.
- Log at `debug` on both the request and response edges; the `logger` level is bound to
  the `--debug` flag in `ACPWebSocketBridge.__init__`.

## Known constraints in the MCP backend

Before extending the backend, know what is and is not there. The **`mcp-protocol`
skill** is the authority on `mcp_stdio.py`; this list exists only so a wire-contract
change does not assume a capability the backend lacks.

Still missing (tracked in beads):

- `MCPStdioClient` binds **one** server, fixed at process start from `--mcp-command`,
  shared by every WebSocket client. There is no per-session MCP registry.
- No `notifications/cancelled` is sent when a request times out (`pyacp-ua1`), and the
  `initialize` request declares no client capabilities (`pyacp-pb7`).

Already there — do not "fix" these, and do not design around their absence:

- A background `_read_loop` demultiplexes stdout into a `_pending` map of futures, so
  responses are matched by `id` and nothing is dropped for arriving out of order.
  Server-initiated requests and notifications reach `_handle_server_request` /
  `_handle_notification`.
- `request()` holds `_write_lock` across id allocation and the write **only**. It
  awaits the reply outside the lock, so concurrent requests pipeline rather than
  serialize.
- `_drain_stderr` runs for the process lifetime and is load-bearing: stderr is piped,
  so an undrained pipe would fill and deadlock every request on the client.

## Verify

```bash
make lint && make test
```
