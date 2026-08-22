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

## Main Symbols

- `MCPProtocolError`: protocol/runtime error type for MCP communication failures.
- `MCPStdioClient.start()` / `stop()`: subprocess and background task lifecycle.
- `MCPStdioClient.initialize()`: sends MCP `initialize` and `notifications/initialized`.
- `MCPStdioClient.request()`: sends a request and awaits its correlated reply.
- `MCPStdioClient.notify()`: sends a notification without expecting a response.
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
- A reply that cannot be written (process already gone) is logged, not raised.

## Related

- [Repository architecture](../../ARCHITECTURE.md)
- [cli.py docs](cli.md)
- [ws_bridge.py docs](ws_bridge.md)
