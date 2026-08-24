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
all appear twice with different meanings. `agent.py`'s `initialize` is ACP;
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

**Version negotiation is real, and `initialize` performs it.** We propose
`_MCP_PROTOCOL_VERSION` (`"2025-06-18"`) and accept either it or `"2024-11-05"` back;
the server replies with the revision it will actually use, which need not be the one we
asked for. `_agreed_protocol_version` checks
that reply against `_SUPPORTED_MCP_PROTOCOL_VERSIONS` and raises `MCPProtocolError` when
it is missing or unusable; `initialize` then stops the subprocess and never sends
`notifications/initialized`. Do not soften that into a warning — half a handshake is
worse than none, because the mismatch resurfaces later as unrelated-looking failures.
Accepting a newer revision means adding it to `_SUPPORTED_MCP_PROTOCOL_VERSIONS`, not
skipping the check.

**Shutdown has a prescribed order** — close stdin, wait, `SIGTERM`, wait, `SIGKILL`.
`_shutdown_process` implements exactly that, escalating only when the server fails to
exit within 2s at each step. Do not reorder it: a conforming server exits on EOF, and
starting at `terminate()` denies it the teardown it was written to run.

## Capabilities are a promise you must keep

Both sides declare what they support in `initialize`, and each side MUST NOT use a
capability the other did not declare. A missing declaration means the feature is never
exercised; a false declaration means the peer calls something you cannot answer.

`MCPClientCapabilities` (frozen, in `mcp_stdio.py`) is the block we send, and
`MCPStdioClient.client_capabilities` is where a caller sets it. It is empty by default,
because a client with no `on_server_request` handler can answer nothing.

**Rule: declaring a capability and answering it are one change, never two.** Both halves
fail quietly on their own — declare nothing and the handler is dead code no server will
ever reach; declare what nothing answers and the server strands itself on a `-32601` it
was told would not happen. `initialize` enforces the second half:
`_declared_capabilities()` raises `RuntimeError` — a conformance bug in *this* process,
not a bad input — when the block is non-empty and `on_server_request` is `None`, before
the promise reaches the wire.

| Client capability | Enables the server to call | python-acp's answer |
|---|---|---|
| `roots: {listChanged: bool}` | `roots/list` | **declared today.** `mcp_registry.roots_responder` answers it from the session's `cwd` + `additionalDirectories`; `listChanged` is `false` because a session's roots are fixed for its lifetime |
| `elicitation: {}` | `elicitation/create` | declarable, not yet declared — `pyacp-8bv.4` forwards it to ACP `session/request_permission`, and declaring it before that lands would strand a server |
| `sampling: {}` | `sampling/createMessage` | **never**, and `MCPClientCapabilities` has no field for it — there is no LLM in this runtime, so the block cannot be built wrong |

An undeclared capability contributes **no key at all**, not a `false`: absent means
unsupported.

The check `initialize` runs is presence, not coverage — one callable stands behind every
declared capability, so a handler wired up for `roots/list` is also what a server's
`sampling/createMessage` reaches. Raise `UnsupportedServerRequest` there: it becomes
`-32601` ("we never offered this") instead of `-32603` ("we broke"), and that distinction
is what makes the capability block mean anything on the wire.

Server capabilities move the other way. Treat the `initialize` result as the source of
truth for what to call; a server that omits `prompts` will answer `prompts/list` with
`-32601`, and that code now reaches the WebSocket client intact instead of collapsing
into a generic `-32603`.

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
| `tools/list` | `list_tools()` | `[{name, description, inputSchema}]` — every page |
| `tools/call` | `call_tool(name, arguments)` | `{content: [...], isError: bool}` |
| `prompts/list` | `list_prompts()` | `[{name, description, arguments}]` — every page |
| `prompts/get` | `get_prompt(name, arguments)` | `{description, messages: [{role, content}]}` |
| `resources/list` | `list_resources()` | `[{uri, name, mimeType}]` — every page |
| `resources/read` | `read_resource(resource_id, arguments)` | `{contents: [{uri, mimeType, text\|blob}]}` |

The three `*_list` wrappers all go through `_list_all`, which walks `nextCursor` to
exhaustion and hands back one flat list — not the raw page envelope. An absent
`nextCursor` is the only terminator; an empty page is not one. Because the walk is
driven entirely by the server it is bounded twice — a repeated cursor and
`_MAX_LIST_PAGES` (100) both raise `MCPProtocolError` rather than looping forever. A new
list method belongs on `_list_all` too, not on a bare `request()`.

Everything else goes through the generic `request()` / `notify()`. Not yet wrapped, in
rough order of usefulness here: `ping`, `resources/templates/list`,
`resources/subscribe` / `unsubscribe`, `logging/setLevel`, `completion/complete`,
`notifications/cancelled`, `notifications/progress`.

Inbound — `_handle_message` routes by shape, and `mcp_stdio.md` has the table. The part
worth memorizing: **every server request gets a reply.** `ping` is answered inline with
`{}`, anything else goes to `on_server_request` or gets `-32601`, a handler that raises
`UnsupportedServerRequest` also gets `-32601`, and a handler that raises anything else
produces `-32603` instead of killing the read loop. An unanswered request strands the
server forever.

Server notifications to expect: `notifications/message` (logging),
`notifications/tools/list_changed`, `notifications/resources/list_changed`,
`notifications/resources/updated`, `notifications/prompts/list_changed`,
`notifications/progress`, `notifications/cancelled`. All land in `on_notification` when
one is set and are dropped with a debug log when it is not.

## Three error channels, not one

This trips people up because two of them look like success:

1. **JSON-RPC error response** — `{"error": {"code", "message"}}`. `request()` raises
   `MCPProtocolError.from_error_response(error)`, which keeps the server's `code` and
   `data` on the exception. `errors.to_request_error` forwards that code to the ACP
   client and tags it `data.source = "mcp"` (`errors.MCP_SOURCE`) with the original in
   `data.mcpCode`, so a backend `-32601` stays distinguishable from a
   backend `-32602` — and from the bridge's own `-32601`. Only failures with no
   server-assigned code (timeout, transport death, a malformed `error` member) fall
   back to `-32603`.
2. **Tool execution failure** — a *successful* JSON-RPC result carrying
   `{"isError": true, "content": [...]}`. This is deliberate in MCP: the model is meant
   to see the failure. `call_tool` normalizes the optional field into a real boolean and
   rejects a non-boolean one, so both dispatchers can rely on it being present. The two
   surfaces then answer differently *on purpose*: the legacy action returns
   `{"ok": false, "error": <the tool's own text>, "result": ...}`, while `tools/call` on
   the JSON-RPC surface returns the result unchanged with `isError` still inside it.
   Do not "fix" the JSON-RPC side into a JSON-RPC error — the call succeeded and only
   the tool failed, and collapsing it would hide the content explaining why and make a
   failed tool indistinguishable from an unreachable backend. Either surface's behavior
   here is wire contract, so read the `acp-protocol` skill before changing it.
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

- `elicitation` is declarable but not declared: nothing forwards `elicitation/create` to
  the ACP client yet (`pyacp-8bv.4`).
- `roots.listChanged` is `false`, so a session whose roots could change could not say so.
  Nothing can change them today, which makes it honest rather than a gap.
- Nothing reads tool annotations, so `turn_mcp_router` asks permission for every tool
  call. `2025-06-18` carries them; reading them is `pyacp-eg1.3`, and a server's own hint
  about its tool is not a security boundary — a missing or false one must still land on
  "ask".
- `read_resource` forwards an `arguments` param that is not in the MCP spec —
  `resources/read` takes `uri` only. The mock server honors it; a real server will
  ignore it. Templated resources are meant to be expanded client-side into a concrete
  URI, and `resources/templates/list` is what advertises them.

## Protocol version

`"2025-06-18"` is the module-level `_MCP_PROTOCOL_VERSION` in `mcp_stdio.py`, and
`_SUPPORTED_MCP_PROTOCOL_VERSIONS` — `{"2025-06-18", "2024-11-05"}` — is the set
`initialize` will accept back.

**Proposed and accepted are deliberately different sets.** We ask for the newer revision
because `elicitation` does not exist before it, and still accept a `2024-11-05`
counter-offer because hanging up on one would drop every server that has not moved yet.
Never propose a revision that is not also in the accepted set — that is a handshake which
always fails.

The MCP version is unrelated to `SUPPORTED_PROTOCOL_VERSIONS` in `capabilities.py`, which
is the ACP version and an integer. Two protocols, two version fields — do not "unify"
them.

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
