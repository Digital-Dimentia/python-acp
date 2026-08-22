# cli.py

## Purpose

`cli.py` is the process entrypoint. It parses startup arguments, initializes logging,
starts the MCP backend connection, and hands the process to the selected client-facing
transport. It owns no protocol logic of its own.

## Key Responsibilities

- Define command-line interface options, including `--transport`.
- Configure runtime logging (`INFO` or `DEBUG`) **onto stderr**.
- Start the MCP initialization handshake.
- Start and hold whichever transport was selected.

## Main Symbols

- `build_parser()`: defines `--mcp-command`, `--transport`, `--host`, `--port`, `--debug`.
- `configure_logging(debug)`: installs the root handler on `sys.stderr`.
- `_run(args)`: async runtime bootstrap and service startup sequence.
- `run()`: sync wrapper that parses args and runs `_run()` with `asyncio.run`.

## Transports

`--transport` selects the client-facing wire. The MCP backend is unaffected by it —
`--mcp-command` means the same thing either way.

| Value | What it does | Notes |
|---|---|---|
| `ws` *(default)* | Binds `ACPWebSocketBridge` on `--host`/`--port` | The existing local-automation surface. Still carries the legacy `{"action": ...}` API (D4), which is why it stays the default for now. |
| `stdio` | Serves ACP on this process's own stdin/stdout via [transport_stdio.py](transport_stdio.md) | How an editor spawns an agent (D2). `--host` and `--port` are ignored. |

**The default flips to `stdio` when the action surface is removed** (`pyacp-sld.3`).
Changing it earlier would break every existing WebSocket invocation for no gain.

## stdout is reserved under `--transport stdio`

`cli.py` must never `print()`, in any mode. Under `--transport stdio` stdout is the
JSON-RPC wire and one stray byte corrupts it; keeping a single logging path in *every*
mode is what stops a banner from creeping back in later. `configure_logging` names
`sys.stderr` explicitly rather than relying on `basicConfig`'s default for the same
reason. `transport_stdio.py` adds a structural backstop on top of this discipline — see
its docs.

## The MCP backend starts in both modes

`_run` starts and handshakes `MCPStdioClient` before selecting a transport, so a bad
`--mcp-command` fails at startup rather than mid-session, and the flag means the same
thing in both modes.

Under `--transport stdio` **the agent cannot reach that client yet.** `PythonAcpAgent`
holds no backend; per-session MCP backends are the Phase 2 registry (`pyacp-3rw.3`,
`pyacp-db3`). Until then the handshake is a startup validation, not a wiring.

## Startup Flow

```mermaid
flowchart TD
    Start["process start"] --> Parse["build_parser + parse_args"]
    Parse --> AsyncRun["asyncio.run(_run)"]
    AsyncRun --> Log["configure_logging → stderr"]
    Log --> MCPStart["start MCPStdioClient context"]
    MCPStart --> Init["initialize MCP handshake"]
    Init --> Version{"protocol version agreed?"}
    Version -- no --> Abort["MCPProtocolError; MCP process stopped"]
    Version -- yes --> Pick{"--transport"}
    Pick -- stdio --> Stdio["run_stdio(PythonAcpAgent())"]
    Pick -- ws --> BridgeStart["start ACPWebSocketBridge"]
    BridgeStart --> Serve["serve_forever"]
    Stdio --> Stop["client disconnects, or KeyboardInterrupt"]
    Serve --> Stop
```

## Error and Shutdown Behavior

- `KeyboardInterrupt` is caught in `run()` to allow clean interactive shutdown.
- MCP process lifecycle is managed by `MCPStdioClient` context manager (`__aenter__` /
  `__aexit__`), so the backend is torn down on either transport's exit.
- A protocol-version mismatch during the handshake aborts startup: `initialize()` stops
  the MCP subprocess and raises `MCPProtocolError`, so no transport is ever bound. See
  [mcp_stdio.py docs](mcp_stdio.md).
- Under `--transport stdio`, the client closing the pipe ends `run_stdio` and the
  process exits normally.

## Related

- [Repository architecture](../../ARCHITECTURE.md)
- [agent.py docs](agent.md)
- [transport_stdio.py docs](transport_stdio.md)
- [mcp_stdio.py docs](mcp_stdio.md)
- [ws_bridge.py docs](ws_bridge.md)
