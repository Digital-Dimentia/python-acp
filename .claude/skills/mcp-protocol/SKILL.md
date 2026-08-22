---
name: mcp-protocol
description: Use when changing how python-acp talks to the MCP server subprocess it embeds — adding an MCP call, handling a server-initiated request or notification, touching the initialize handshake or capability block, changing subprocess lifecycle or stdio framing, or debugging a hang, a dropped message, or a swallowed error. Covers the stdio transport MUSTs, the lifecycle, the primitive surface in both directions, and where mcp_stdio.py currently diverges from the spec. Trigger on work involving mcp_stdio.py, MCPStdioClient, MCPProtocolError, tests/fixtures/mock_mcp_server.py, or the MCP protocol version.
---

# The MCP Side of the Bridge

`python-acp` is an **MCP client**. It launches an MCP **server** as a subprocess and
speaks JSON-RPC 2.0 to it over that process's stdin/stdout. All of it lives in
`src/python_acp/mcp_stdio.py`; `MCPStdioClient` is the only thing in the repo that
touches the MCP wire.

Get the direction right before anything else — the two protocols in this repo point
opposite ways and reuse the same vocabulary:

```
WebSocket client ──ACP──▶ python-acp ──MCP──▶ server subprocess
                   (we are the server)  (we are the client)
```

So "capabilities", "initialize", "notifications/initialized" and JSON-RPC error codes
all appear twice with different meanings. `ws_bridge.py`'s `initialize` is ACP;
`MCPStdioClient.initialize` is MCP. The `acp-protocol` skill owns the other side.

## Transport rules (non-negotiable)

The stdio transport is four MUSTs. Violating any of them corrupts the stream or
deadlocks the process, and none of it is caught by a linter.

| Rule | Where it is honored |
|---|---|
| Messages are UTF-8 JSON-RPC, newline-delimited, **no embedded newlines** | `_write` — `json.dumps` escapes `\n`, so never hand-build a line or add `indent=` |
| The client MUST NOT write anything to the server's stdin that is not an MCP message | `_write` is the only writer; keep it that way |
| The server MUST NOT write non-MCP output to its stdout — but they do anyway | `_read_loop` skips unparseable lines with a debug log rather than dying |
| stderr is the server's log channel; the client MAY capture it | `_drain_stderr` **must keep running** — see below |

Two hard-won constraints in `MCPStdioClient` exist because of this transport, and both
are load-bearing:

- **`_drain_stderr` is not optional.** stderr is piped. A piped stream nothing reads
  fills its OS buffer, the server blocks mid-write, stops reading stdin, and every
  pending request hangs. It reads with `read(_STDERR_CHUNK)` rather than `readline()`
  on purpose: `readline()` raises on an over-long line, which would abort the drain and
  restore the deadlock.
- **`_STREAM_LIMIT = 8 MiB`** overrides asyncio's 64 KiB reader default. A real
  `resources/read` response exceeds 64 KiB routinely, and `readline()` in the stdout
  loop raises on it.

Do not "simplify" either one away.

## Lifecycle

Exactly one handshake, and it must be first:

```
client → initialize   {protocolVersion, capabilities, clientInfo}
client ← result       {protocolVersion, capabilities, serverInfo, instructions?}
client → notifications/initialized
```

`MCPStdioClient.initialize` does all three, and `cli.py` calls it once at startup,
before the WebSocket listener binds. Before the `initialize` result arrives the client
SHOULD send nothing but `ping`; before `notifications/initialized` arrives a
well-behaved server sends nothing but `ping` and log messages.

**Version negotiation is real, and we don't do it.** We send `"2024-11-05"`. If the
server does not support it, it MUST answer with a version it does support — and we
throw that away (`initialize` returns the result dict; nothing reads
`result["protocolVersion"]`). Against a server that has dropped `2024-11-05`, the
symptom is a successful handshake followed by confusing failures later. If you touch
`initialize`, compare the returned version against what we sent and fail loudly on a
mismatch.

**Shutdown has a prescribed order** — close stdin, wait, `SIGTERM`, wait, `SIGKILL`.
`stop()` currently starts at `terminate()` and skips the stdin close, so servers that
exit cleanly on EOF get signalled instead.

## Capabilities are a promise you must keep

Both sides declare what they support in `initialize`, and each side MUST NOT use a
capability the other did not declare. A missing declaration means the feature is never
exercised; a false declaration means the peer calls something you cannot answer.

We send `"capabilities": {}` — literally nothing. That is currently a lie by omission:
`_handle_server_request` will happily route `roots/list`, `sampling/createMessage`, or
`elicitation/create` to `on_server_request`, but no conforming server will ever send
them, because we never said we could take them.

**Rule: when you make `on_server_request` handle a client primitive, declare it in the
same change.** The mapping:

| Client capability | Enables the server to call | python-acp's answer |
|---|---|---|
| `roots: {listChanged: bool}` | `roots/list` | plausible — ACP sessions carry `cwd`/`additionalDirectories` |
| `elicitation: {}` | `elicitation/create` | the intended path; maps onto ACP `session/request_permission` |
| `sampling: {}` | `sampling/createMessage` | **no** — there is no LLM in this runtime, and declaring it strands the server |

Server capabilities move the other way. Treat the `initialize` result as the source of
truth for what to call; a server that omits `prompts` will answer `prompts/list` with
`-32601`, and today that surfaces to the WebSocket client as a generic `-32603`.

| Server capability | Unlocks |
|---|---|
| `tools: {listChanged}` | `tools/list`, `tools/call` |
| `resources: {subscribe, listChanged}` | `resources/list`, `resources/read`, `resources/templates/list`, `resources/subscribe` |
| `prompts: {listChanged}` | `prompts/list`, `prompts/get` |
| `logging: {}` | `logging/setLevel`, and `notifications/message` coming back |
| `completions: {}` | `completion/complete` |

## The method surface

Outbound — what `MCPStdioClient` wraps today, and the shape each returns:

| Method | Wrapper | Result shape |
|---|---|---|
| `initialize` | `initialize()` | `{protocolVersion, capabilities, serverInfo}` |
| `tools/list` | `list_tools()` | `{tools: [{name, description, inputSchema}], nextCursor?}` |
| `tools/call` | `call_tool(name, arguments)` | `{content: [...], isError: bool}` |
| `prompts/list` | `list_prompts()` | `{prompts: [{name, description, arguments}], nextCursor?}` |
| `prompts/get` | `get_prompt(name, arguments)` | `{description, messages: [{role, content}]}` |
| `resources/list` | `list_resources()` | `{resources: [{uri, name, mimeType}], nextCursor?}` |
| `resources/read` | `read_resource(uri, arguments)` | `{contents: [{uri, mimeType, text\|blob}]}` |

Everything else goes through the generic `request()` / `notify()`. Not yet wrapped, in
rough order of usefulness here: `ping`, `resources/templates/list`,
`resources/subscribe` / `unsubscribe`, `logging/setLevel`, `completion/complete`,
`notifications/cancelled`, `notifications/progress`.

Inbound — `_handle_message` routes by shape, and `mcp_stdio.md` has the table. The part
worth memorizing: **every server request gets a reply.** `ping` is answered inline with
`{}`, anything else goes to `on_server_request` or gets `-32601`, and a handler that
raises produces `-32603` instead of killing the read loop. An unanswered request
strands the server forever.

Server notifications to expect: `notifications/message` (logging),
`notifications/tools/list_changed`, `notifications/resources/list_changed`,
`notifications/resources/updated`, `notifications/prompts/list_changed`,
`notifications/progress`, `notifications/cancelled`. All land in `on_notification` when
one is set and are dropped with a debug log when it is not.

## Three error channels, not one

This trips people up because two of them look like success:

1. **JSON-RPC error response** — `{"error": {"code", "message"}}`. `request()` raises
   `MCPProtocolError(str(error))`. Note the code is stringified into the message and
   lost; `ws_bridge` then maps every one of these to ACP `-32603`. A server's `-32601`
   and its `-32602` are indistinguishable to a WebSocket client today.
2. **Tool execution failure** — a *successful* JSON-RPC result carrying
   `{"isError": true, "content": [...]}`. This is deliberate in MCP: the model is meant
   to see the failure. Nothing in this repo inspects `isError` (`ws_bridge.py:119`,
   `ws_bridge.py:235`), so a failed tool call reaches the client as `{"ok": true}`. If
   a task says "surface tool errors," this is the line to change — and changing it is a
   wire-contract change, so read the `acp-protocol` skill first.
3. **Transport death** — closed stdout, a dead read loop, or `stop()`. `_fail_pending`
   fails every outstanding future with `MCPProtocolError`, so callers get an error
   rather than hanging.

A timeout is its own case: `request()` raises after `request_timeout` (default 30s) but
sends nothing to the server, which keeps working on an answer nobody will read. The
spec's remedy is `notifications/cancelled` carrying the `requestId`.

## Adding an MCP call: the checklist

1. **Check the capability.** Is this method gated behind something the server declared
   in `initialize`? If yes, that gate belongs in the code, not in a comment.
2. `src/python_acp/mcp_stdio.py` — add the wrapper. Validate the response shape and
   raise `MCPProtocolError` on anything unexpected, matching `list_tools` /
   `list_prompts`. Do not return raw `Any`.
3. `tests/fixtures/mock_mcp_server.py` — teach the fixture to answer the method. The
   fixture is a plain `while True` loop over stdin; add an `elif method == "..."`
   branch. Tests run against this process, not a mock object, so an untaught fixture
   means an untested path.
4. `tests/test_mcp_stdio.py` — cover the success path **and** the failure path. The
   fixture's fallthrough already returns `-32601` for unknown methods, and
   `MOCK_MCP_STDERR_BYTES` exists to reproduce the stderr deadlock.
5. `src/python_acp/mcp_stdio.md` — update Main Symbols and, if routing changed, the
   Message Routing table and its Mermaid diagram.
6. If it becomes reachable from the WebSocket, the `acp-protocol` skill's checklist
   applies on top of this one.

Anything that adds or renames a module also triggers the `repo-docs-sync` skill.

## Known divergences

Real gaps, in rough priority order. Check beads before filing a duplicate.

- `initialize` ignores the server's returned `protocolVersion` — no negotiation.
- `"capabilities": {}` is sent, so no server will ever exercise `on_server_request`.
- `stop()` skips the stdin close before `SIGTERM`.
- No pagination: `nextCursor` is ignored by all three `*_list` wrappers, so a paginated
  server silently returns only its first page.
- Timeouts do not send `notifications/cancelled`.
- Server error codes collapse into an `MCPProtocolError` message string.
- `isError` on tool results is never inspected.
- `read_resource` forwards an `arguments` param that is not in the MCP spec —
  `resources/read` takes `uri` only. The mock server honors it; a real server will
  ignore it. Templated resources are meant to be expanded client-side into a concrete
  URI, and `resources/templates/list` is what advertises them.

## Protocol version

`"2024-11-05"` is hardcoded in `MCPStdioClient.initialize`. It is unrelated to
`_SUPPORTED_PROTOCOL_VERSION = 1` in `ws_bridge.py`, which is the ACP version. Two
protocols, two version fields — do not "unify" them.

Before changing the pinned version, read [spec-versions.md](spec-versions.md) beside
this file. The jump to `2026-07-28` is not a string swap: that revision makes MCP
stateless, replaces the handshake with `server/discover`, moves the protocol version
into per-request `_meta`, puts notifications behind `subscriptions/listen`, and
deprecates sampling.

## Verify

```bash
make lint && make test
```

`asyncio_mode = "auto"`, so `async def test_*` needs no decorator. If a new test hangs
instead of failing, suspect the stderr drain or a server request nothing replied to.
