# mcp_stdio.py

## Purpose

`mcp_stdio.py` manages communication with an MCP server process over stdio using JSON-RPC payloads.

MCP is bidirectional. The server may send requests and notifications of its own at
any time, not only responses to ours, so this module reads stdout continuously
rather than only while one of our requests is in flight.

## Key Responsibilities

- Spawn and terminate the MCP subprocess.
- Continuously drain the subprocess's stderr.
- Serialize request and notification messages to JSON lines.
- Read every stdout message and route it by shape: response, server request, or notification.
- Answer server-initiated requests, including with an error.
- Expose convenience methods for tools, prompts, and resources.
- Walk cursor-paginated list results to exhaustion instead of returning page one.

## Main Symbols

- `MCPProtocolError`: protocol/runtime error type for MCP communication failures.
- `MCPStdioClient.start()` / `stop()`: subprocess and background task lifecycle.
- `MCPStdioClient.initialize()`: sends MCP `initialize` and `notifications/initialized`.
- `MCPStdioClient.request()`: sends a request and awaits its correlated reply.
- `MCPStdioClient.notify()`: sends a notification without expecting a response.
- `MCPStdioClient.list_tools()` / `list_prompts()` / `list_resources()`: fully
  paginated list wrappers, each returning the accumulated items across all pages.
- `MCPStdioClient._list_all()`: the shared `nextCursor` walk behind those three.
- `MCPStdioClient._MAX_LIST_PAGES`: hard ceiling on pages walked in one list call.
- `MCPStdioClient._read_loop()`: background task consuming all stdout messages.
- `MCPStdioClient._handle_message()`: routes one inbound message by shape.
- `MCPStdioClient._drain_stderr()`: background task consuming the subprocess's stderr.
- `on_server_request` / `on_notification`: optional async hooks for inbound server traffic.

## Message Routing

Every inbound message is classified by whether it carries `method` and `id`:

| `method` | `id` | Treated as | Handling |
|---|---|---|---|
| absent | present | response to our request | resolves the pending future for that id |
| present | absent | server notification | passed to `on_notification`, if set |
| present | present | server-initiated request | answered — see below |

```mermaid
sequenceDiagram
    participant Caller
    participant Client as MCPStdioClient
    participant Loop as _read_loop
    participant Proc as MCP Server Process

    Note over Loop,Proc: read loop runs for the life of the subprocess

    Caller->>Client: request(method, params)
    Client->>Client: allocate id, register pending future
    Client->>Proc: write JSON-RPC line with id
    Proc-->>Loop: stdout line
    alt response to our id
        Loop->>Client: resolve pending future
        Client-->>Caller: result dict or MCPProtocolError
    else server-initiated request
        Loop->>Loop: ping, on_server_request, or -32601
        Loop->>Proc: write reply
    else notification
        Loop->>Loop: on_notification, if set
    end
```

## Answering Server Requests

Every server request gets a reply. Leaving one unanswered strands the server
waiting on us — the failure this design exists to prevent.

- `ping` is answered inline with `{}`. It is protocol plumbing, not application logic.
- Anything else goes to `on_server_request` when one is set; its return value
  becomes the `result`.
- With no handler set, the reply is `-32601` naming the method.
- A handler that raises produces `-32603` carrying the exception text; the read
  loop keeps running.

`on_server_request` is where ACP integration will hook in: MCP's
`elicitation/create` maps onto ACP's `session/request_permission`, and
`sampling/createMessage` has no answer here because this runtime has no LLM.

## List Pagination

MCP list results are cursor-paginated. A result carries `nextCursor` when more
pages exist; the client re-issues the same method with `cursor` set to that
value and keeps going. `_list_all()` implements the walk once, and all three
list wrappers delegate to it.

Two rules that are easy to get wrong:

- **An absent `nextCursor` is the only terminator.** A page carrying zero items
  is legal mid-walk and does not mean the list ended. `MOCK_MCP_LIST_EMPTY_MIDDLE=1`
  in the mock server reproduces exactly that shape.
- **The first request omits `cursor` entirely.** It does not send `null`.

The walk is driven entirely by the server, so a broken or hostile one could keep
issuing cursors forever. It is bounded twice, and both bounds raise
`MCPProtocolError` rather than hanging the bridge:

| Condition | Result |
|---|---|
| `nextCursor` absent | walk ends, accumulated items returned |
| `nextCursor` repeats one already seen | `MCPProtocolError: <method> repeated cursor '<c>'` |
| more than `_MAX_LIST_PAGES` (100) pages | `MCPProtocolError: <method> exceeded 100 pages` |
| `nextCursor` present but not a non-empty string | `MCPProtocolError: Invalid nextCursor in <method> response` |
| the page key holds a non-list | `MCPProtocolError: Invalid <method> response` |

The mock server at `tests/fixtures/mock_mcp_server.py` serves paginated lists on
demand, so these paths are tested against a real subprocess:

| Env var | Effect |
|---|---|
| `MOCK_MCP_LIST_PAGES=N` | serve N pages; `nextCursor` is absent on the last (default 1) |
| `MOCK_MCP_LIST_STUCK=1` | hand back the same `nextCursor` forever |
| `MOCK_MCP_LIST_EMPTY_MIDDLE=1` | page 0 has no items but does carry a `nextCursor` |

## Concurrency Model

- A write lock covers request-id allocation and the write only. Waiting for a
  reply happens outside it, so concurrent requests pipeline instead of queueing
  behind one another.
- Replies are correlated by id through a pending-future map, not by position.
- `start()` spawns two background tasks — the stdout read loop and the stderr
  drain — both cancelled and awaited by `stop()`.

## stderr Draining

stderr is piped, and a piped stream nothing reads will fill its OS buffer — at
which point the server blocks mid-write and stops reading stdin, deadlocking
every pending request. `_drain_stderr()` runs for the life of the subprocess to
prevent that, logging what it reads at debug level so the CLI's `--debug` flag
surfaces server-side output.

It reads with `StreamReader.read()` rather than `readline()` on purpose:
`readline()` raises on any line longer than the reader's limit, and one
over-long line would abort the drain and reintroduce the deadlock. Lines are
reassembled from fixed-size chunks instead, and an over-long line is flushed
rather than buffered without bound.

Draining is best-effort: unexpected errors stop the task quietly instead of
propagating into the client.

## Stream Limit

The subprocess is created with `limit=8 MiB` rather than asyncio's 64 KiB
default. A large `resources/read` response can exceed 64 KiB, and `readline()`
in the stdout loop would raise on it.

## Failure Modes

- Timeout waiting for a response raises `MCPProtocolError`.
- Closed stdout, a failed read loop, or a stopped process fails every pending
  request with `MCPProtocolError`.
- Non-dict or malformed stdout lines are skipped with a debug log.
- A response whose id matches nothing pending is discarded with a debug log.
- JSON-RPC `error` responses are surfaced as `MCPProtocolError`.
- A list walk that repeats a cursor or exceeds `_MAX_LIST_PAGES` raises
  `MCPProtocolError` instead of looping forever.
- A reply that cannot be written (process already gone) is logged, not raised.

## Related

- [MCP protocol skill](../../.claude/skills/mcp-protocol/SKILL.md) — transport MUSTs,
  lifecycle, capability rules, and the checklist for adding an MCP call
- [Repository architecture](../../ARCHITECTURE.md)
- [cli.py docs](cli.md)
- [ws_bridge.py docs](ws_bridge.md)
