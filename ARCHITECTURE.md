# python-acp Architecture

This document describes how `python-acp` is organized today and how requests flow through the runtime.

## Subsystems

- CLI runtime: parses startup arguments and bootstraps async services.
- ACP WebSocket bridge: accepts WebSocket client traffic and dispatches requests.
- MCP stdio client: communicates with an MCP server subprocess using JSON-RPC over newline-delimited stdio.
- MCP server process: external tool/prompt/resource provider.

```mermaid
flowchart LR
    UserClient[WebSocket Client]
    CLI[cli.py\nRuntime Bootstrap]
    Bridge[ws_bridge.py\nACPWebSocketBridge]
    MCPClient[mcp_stdio.py\nMCPStdioClient]
    MCPProc[(MCP Server Subprocess)]

    CLI --> Bridge
    Bridge <--> UserClient
    Bridge --> MCPClient
    MCPClient <--> MCPProc
```

## Request Lifecycle

The most common request path is a tool call from a WebSocket client.

```mermaid
sequenceDiagram
    participant C as WebSocket Client
    participant B as ACPWebSocketBridge
    participant M as MCPStdioClient
    participant S as MCP Server

    C->>B: JSON message (action or JSON-RPC method)
    B->>B: Parse and validate request
    alt action-based request
        B->>M: tools/list or tools/call
    else JSON-RPC request
        B->>M: tools/*, prompts/*, resources/*
    end
    M->>S: JSON-RPC request over stdio
    S-->>M: JSON-RPC response
    M-->>B: decoded result or MCPProtocolError
    B-->>C: JSON response
```

## Module Documentation

- [CLI module](src/python_acp/cli.md)
- [MCP stdio module](src/python_acp/mcp_stdio.md)
- [WebSocket bridge module](src/python_acp/ws_bridge.md)

## Notes

- The bridge currently supports two request styles:
  - Legacy action messages (`action` field)
  - JSON-RPC-like messages (`method` field)
- Unsupported JSON-RPC methods return a `-32601` method-not-found error payload.
