# python-acp Architecture

This document describes how `python-acp` is organized today, how requests flow through the
runtime, and the subsystem shape it is being migrated to.

## Subsystems Today

- CLI runtime: parses startup arguments and bootstraps async services.
- ACP WebSocket bridge: accepts WebSocket client traffic and dispatches requests.
- Error mapping: one translation from our exception types to `acp.RequestError`, shared with the agent runtime so both client-facing surfaces answer the same codes.
- MCP stdio client: communicates with an MCP server subprocess using JSON-RPC over newline-delimited stdio.
- MCP server process: external tool/prompt/resource provider.

```mermaid
flowchart LR
    UserClient["WebSocket client"]
    CLI["cli.py<br/>runtime bootstrap"]
    Bridge["ws_bridge.py<br/>ACPWebSocketBridge"]
    Errors["errors.py<br/>to_request_error"]
    MCPClient["mcp_stdio.py<br/>MCPStdioClient"]
    MCPProc[("MCP server subprocess")]

    CLI --> Bridge
    Bridge <--> UserClient
    Bridge --> Errors
    Bridge --> MCPClient
    MCPClient <--> MCPProc
```

## Target Subsystems (ACP v1)

The runtime is being rebuilt on the `agent-client-protocol` SDK. See
[docs/module-boundaries.md](docs/module-boundaries.md) for what each module owns, why
`ws_bridge.py` splits, and which parts still await verification against the pinned SDK,
and [docs/acp-compliance-matrix.md](docs/acp-compliance-matrix.md) for the per-method
dispositions.

**Four pieces are built.** `agent.py` (`PythonAcpAgent`, all 15 `acp.interfaces.Agent`
members), `capabilities.py` (the block `initialize` advertises, and version
negotiation), `errors.py` (one exception-to-`RequestError` mapping, already shared with
the legacy WebSocket path), and `transport_stdio.py`, which binds the agent to
stdin/stdout under `--transport stdio`. An ACP client can spawn the process and complete `initialize`
today; session and prompt methods answer `-32601` until Phases 2 and 3 fill them in, and
the agent cannot reach the MCP backend yet — that is the Phase 2 registry.

The capability block that handshake returns is **entirely off**, by construction rather
than by omission: every field is a row of `AGENT_CAPABILITY_MANIFEST` carrying the bead
that will flip it, and a row cannot be turned on without a test proving the feature
behind it runs. See [capabilities.py](src/python_acp/capabilities.md).

The WebSocket path is **not** rebound yet (`pyacp-tzd.3`), so under the default
`--transport ws` the live request path is still the one under
[Request Lifecycle](#request-lifecycle) below. Everything else in this section is decided
and not yet built.

```mermaid
sequenceDiagram
    participant E as ACP client (editor)
    participant T as transport_stdio.py
    participant SDK as acp.run_agent + router
    participant A as agent.py
    participant C as capabilities.py

    E->>T: spawns the process; JSON-RPC over stdin
    T->>SDK: run_agent(agent, use_unstable_protocol=True)
    SDK->>A: initialize(protocol_version, client_capabilities)
    A->>C: negotiate version, build the capability block
    C-->>A: negotiated version + AgentCapabilities
    A-->>SDK: InitializeResponse
    SDK-->>E: result on stdout
    SDK->>A: session/new
    A-->>SDK: RequestError(-32601) until Phase 2
    SDK-->>E: error on stdout
```

- Transport bindings (`transport_stdio.py`, `transport_ws.py`): attach the agent to a wire. Nothing else.
- Agent runtime (`agent.py`): the `acp.interfaces.Agent` implementation; translates and delegates.
- Capability manifest (`capabilities.py`): what `initialize` may advertise, and the version handshake. One table, derived from the compliance matrix; nothing else builds a capability block.
- Session registry (`sessions.py`): cwd, additional directories, modes, config options, lifetimes.
- Turn executor (`turns.py` + `turn_mcp_router.py`): serves one prompt turn and streams `session/update`.
- MCP backend (`mcp_registry.py` + `mcp_stdio.py`): per-session MCP servers behind a registry.
- Error mapping (`errors.py`): our exceptions to `acp.RequestError`, in one place.

```mermaid
flowchart LR
    Editor["ACP client<br/>(stdio)"]
    WsClient["Local automation<br/>(WebSocket)"]
    CLI["cli.py"]
    TStdio["transport_stdio.py"]
    TWs["transport_ws.py"]
    Legacy["legacy_ws.py<br/>(deprecated)"]
    SDK["acp.run_agent<br/>+ agent router"]
    Agent["agent.py<br/>PythonAcpAgent"]
    Caps["capabilities.py<br/>capability manifest"]
    Errors["errors.py"]
    Sessions["sessions.py"]
    Turns["turns.py<br/>TurnExecutor"]
    Router["turn_mcp_router.py"]
    Registry["mcp_registry.py"]
    MCPClient["mcp_stdio.py"]
    MCPProc[("MCP server subprocess")]

    Editor <--> TStdio
    WsClient <--> TWs
    WsClient <--> Legacy
    CLI --> TStdio
    CLI --> TWs
    TStdio --> SDK
    TWs --> SDK
    SDK <--> Agent
    Agent --> Caps
    Agent --> Errors
    Agent --> Sessions
    Agent --> Turns
    Turns -.implemented by.-> Router
    Legacy --> Registry
    Router --> Registry
    Sessions --> Registry
    Registry --> MCPClient
    MCPClient <--> MCPProc
    Router -. "session/update via Client handle" .-> SDK
```

The dotted edge is the point of the design: the turn executor pushes `session/update`
back through the `Client` handle the agent received from `on_connect`, without knowing
which transport is underneath.

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
    alt server returns an error response
        S-->>M: JSON-RPC error
        M-->>B: MCPProtocolError (code preserved)
        B-->>C: error payload, MCP code forwarded
    else server returns a result
        S-->>M: JSON-RPC result
        M-->>B: decoded result
        B-->>C: success payload
    end
```

The upper branch is why the response edge is drawn twice: a backend error and a
backend result take different paths out of the bridge. A **third** outcome hides
inside the lower branch — a `tools/call` result carrying `isError: true`. That is
a tool failure, not a request failure; it travels the success path with its
content intact and never becomes an error payload.

The sequence above describes the runtime as it exists today. It changes shape during
the ACP v1 migration; [docs/module-boundaries.md](docs/module-boundaries.md) records
which beads redraw it.

## Module Documentation

- [ACP agent module](src/python_acp/agent.md)
- [Capability manifest module](src/python_acp/capabilities.md)
- [Error mapping module](src/python_acp/errors.md)
- [CLI module](src/python_acp/cli.md)
- [MCP stdio module](src/python_acp/mcp_stdio.md)
- [ACP stdio transport module](src/python_acp/transport_stdio.md)
- [WebSocket bridge module](src/python_acp/ws_bridge.md)

## Design Documents

- [ACP v1 plan](docs/full-apc-plan.md) — phases, decisions D1-D6, and delivery sequencing.
- [ACP v1 compliance matrix](docs/acp-compliance-matrix.md) — per-method disposition for every
  `acp.interfaces.Agent` and `Client` member, and the `initialize` capability block it dictates.
- [Module boundaries](docs/module-boundaries.md) — the target module layout and the fate of `ws_bridge.py`.

## Notes

- The bridge currently supports two request styles:
  - Legacy action messages (`action` field)
  - JSON-RPC-like messages (`method` field)
- Unsupported JSON-RPC methods return a `-32601` method-not-found error payload.
- Error codes from the MCP backend are forwarded rather than collapsed into
  `-32603`, tagged with `data.source = "mcp"` to mark whose namespace they are
  from. See [the bridge module docs](src/python_acp/ws_bridge.md#error-mapping).
