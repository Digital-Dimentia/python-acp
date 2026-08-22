# cli.py

## Purpose

`cli.py` is the process entrypoint. It parses startup arguments, initializes logging, starts the MCP client connection, and launches the WebSocket bridge server.

## Key Responsibilities

- Define command-line interface options.
- Configure runtime logging (`INFO` or `DEBUG`).
- Start MCP initialization handshake.
- Start and hold the WebSocket service loop.

## Main Symbols

- `build_parser()`: defines `--mcp-command`, `--host`, `--port`, and `--debug`.
- `_run(args)`: async runtime bootstrap and service startup sequence.
- `run()`: sync wrapper that parses args and runs `_run()` with `asyncio.run`.

## Startup Flow

```mermaid
flowchart TD
    Start[Process Start] --> Parse[build_parser + parse_args]
    Parse --> AsyncRun[asyncio.run(_run)]
    AsyncRun --> Log[configure logging]
    Log --> MCPStart[start MCPStdioClient context]
    MCPStart --> Init[initialize MCP handshake]
    Init --> BridgeStart[start ACPWebSocketBridge]
    BridgeStart --> Serve[serve_forever]
    Serve --> Stop[KeyboardInterrupt or shutdown]
```

## Error and Shutdown Behavior

- `KeyboardInterrupt` is caught in `run()` to allow clean interactive shutdown.
- MCP process lifecycle is managed by `MCPStdioClient` context manager (`__aenter__` / `__aexit__`).

## Related

- [Repository architecture](../../ARCHITECTURE.md)
- [mcp_stdio.py docs](mcp_stdio.md)
- [ws_bridge.py docs](ws_bridge.md)
