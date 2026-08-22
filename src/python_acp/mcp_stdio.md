# mcp_stdio.py

## Purpose

`mcp_stdio.py` manages communication with an MCP server process over stdio using JSON-RPC payloads.

## Key Responsibilities

- Spawn and terminate the MCP subprocess.
- Serialize request and notification messages to JSON lines.
- Read and correlate JSON-RPC responses by request id.
- Expose convenience methods for tools, prompts, and resources.

## Main Symbols

- `MCPProtocolError`: protocol/runtime error type for MCP communication failures.
- `MCPStdioClient.start()` / `stop()`: subprocess lifecycle management.
- `MCPStdioClient.initialize()`: sends MCP `initialize` and `notifications/initialized`.
- `MCPStdioClient.request()`: sends request and waits for matching response.
- `MCPStdioClient.notify()`: sends notification without expecting response.
- `MCPStdioClient._read_response()`: waits for the matching id with timeout behavior.

## Request/Response Correlation

```mermaid
sequenceDiagram
    participant Bridge as Caller
    participant Client as MCPStdioClient
    participant Proc as MCP Server Process

    Bridge->>Client: request(method, params)
    Client->>Client: increment request id
    Client->>Proc: write JSON-RPC line with id
    loop until matching id
        Proc-->>Client: stdout line
        Client->>Client: parse JSON and inspect id
    end
    Client-->>Bridge: result dict or MCPProtocolError
```

## Concurrency Model

- A single async lock serializes writes and response waits, preventing interleaved request/response confusion.
- Requests are effectively processed one-at-a-time through this lock.

## Failure Modes

- Timeout waiting for response raises `MCPProtocolError`.
- Closed stdout/stopped process raises `MCPProtocolError`.
- Non-dict or malformed responses are skipped until a valid matching message is found.
- JSON-RPC `error` responses are surfaced as `MCPProtocolError`.

## Related

- [Repository architecture](../../ARCHITECTURE.md)
- [cli.py docs](cli.md)
- [ws_bridge.py docs](ws_bridge.md)
