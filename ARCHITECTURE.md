# python-acp Architecture

This document describes how `python-acp` is organized today, how requests flow through the
runtime, and the subsystem shape it is being migrated to.

## Subsystems Today

Both transports now bind the same agent through the SDK. There is one dispatch path, one
capability block, and one error mapping, whichever wire a client arrives on.

- CLI runtime: parses startup arguments and bootstraps async services. Owns the one MCP subprocess.
- Transport bindings (`transport_stdio.py`, `transport_ws.py`): attach the agent to a wire, and nothing else.
- Agent runtime (`agent.py`): the `acp.interfaces.Agent` implementation. Every routed method is live except `session/set_mode` and `session/set_config_option` (`pyacp-fln.2`, `pyacp-fln.3`).
- Capability manifest (`capabilities.py`): what `initialize` may advertise, and the version handshake.
- Error mapping (`errors.py`): one translation from our exception types to `acp.RequestError`.
- Deprecated surface (`legacy_ws.py`): the `{"action": ...}` API and the MCP passthrough, intercepted before the SDK and removed in Phase 7.
- Path constraints (`paths.py`): the absolute-path rule, and the containment boundary a session's `cwd` plus `additionalDirectories` define. Phase 4.2's `fs/*` calls are its first consumer.
- Session registry (`sessions.py`): the `Session` record — metadata, config state, transcript, in-flight turn — and the registry that creates, forks, resumes, pages, and closes them. One per process, shared by every connection.
- Turn seam (`turns.py`): the `TurnExecutor` a `session/prompt` runs behind — the `session/update` emission channel, client-capability gating, cancellation, and the `stopReason`/`usage` a turn reports. The default is the deterministic MCP tool-router below.
- MCP tool-router (`turn_mcp_router.py`): the shipped executor. Reads each text prompt block as a JSON tool invocation, runs it against the session's MCP backends, and streams real `tool_call` status transitions. No LLM, no reasoning.
- MCP content mapping (`mcp_content.py`): the seam between MCP's content model and ACP's. Unmappable content becomes a visible placeholder rather than a gap.
- MCP elicitation forwarding (`elicitation.py`): an MCP server's `elicitation/create` becomes an ACP one, so a question from a backend reaches the only human in the system.
- MCP backend registry (`mcp_registry.py`): the MCP servers each session opened, spawned from `session/new`'s `mcpServers` and torn down with the session.
- MCP stdio client (`mcp_stdio.py`): drives one MCP server subprocess over newline-delimited JSON-RPC.

```mermaid
flowchart LR
    Editor["ACP client<br/>(stdio)"]
    WsClient["WebSocket client"]
    CLI["cli.py<br/>runtime bootstrap"]
    TStdio["transport_stdio.py"]
    TWs["transport_ws.py<br/>WebSocketMessageTransport"]
    Legacy["legacy_ws.py<br/>(deprecated)"]
    SDK["acp.run_agent<br/>+ agent router"]
    Agent["agent.py<br/>PythonAcpAgent"]
    Caps["capabilities.py"]
    Errors["errors.py"]
    Sessions["sessions.py<br/>SessionRegistry"]
    Paths["paths.py<br/>containment rule"]
    Turns["turns.py<br/>TurnExecutor"]
    Router["turn_mcp_router.py<br/>McpToolRouterExecutor"]
    Backends["mcp_registry.py<br/>McpBackendRegistry"]
    Elicit["elicitation.py<br/>MCP question &rarr; ACP question"]
    MCPClient["mcp_stdio.py<br/>MCPStdioClient"]
    MCPProc[("MCP server subprocess<br/>one per session server")]
    StartupProc[("--mcp-command subprocess<br/>optional, deprecated surface only")]

    Editor <--> TStdio
    WsClient <--> TWs
    CLI --> TStdio
    CLI --> TWs
    CLI --> Sessions
    CLI --> Backends
    TStdio --> SDK
    TWs --> SDK
    TWs --> Legacy
    TWs --> Errors
    SDK <--> Agent
    Agent --> Caps
    Agent --> Errors
    Agent --> Sessions
    Agent --> Paths
    Agent --> Turns
    Turns -.implemented by.-> Router
    Router --> Backends
    Agent --> Backends
    Sessions -. "on_close" .-> Backends
    Turns -. "session/update via the Client handle" .-> SDK
    Turns -. "gated client calls (Phase 4)" .-> SDK
    Backends --> MCPClient
    Backends -. "elicitation forwarder" .-> Elicit
    Agent --> Elicit
    MCPClient -. "elicitation/create" .-> Elicit
    Elicit -. "asks the connected client" .-> SDK
    MCPClient <--> MCPProc
    Legacy --> StartupProc
    CLI -.-> StartupProc
```

One arrow runs the other way from all the others: `mcp_stdio.py` reaches
`elicitation.py`, which reaches back out through the SDK. That is a *server-initiated*
request — the backend asking us a question — and it is the only path in the process where
traffic starts at the MCP end.

Two more things are worth reading off that diagram. `legacy_ws.py` is the only thing still
bound to the process-wide `--mcp-command` subprocess — that is what Phase 7 deletes, and
why `--mcp-command` is now optional for everyone else. And `sessions.py` reaches
`mcp_registry.py` only through the dotted `on_close` edge: it never imports MCP, so
`cli.py` wiring that hook is the entire coupling (decision B6a).

## Target Subsystems (ACP v1)

The runtime is being rebuilt on the `agent-client-protocol` SDK. See
[docs/module-boundaries.md](docs/module-boundaries.md) for what each module owns, how
`ws_bridge.py` was split, and which parts still await verification against the pinned SDK,
and [docs/acp-compliance-matrix.md](docs/acp-compliance-matrix.md) for the per-method
dispositions.

**Phase 1's runtime is built.** `agent.py` (`PythonAcpAgent`, all 15
`acp.interfaces.Agent` members), `capabilities.py` (the block `initialize` advertises,
and version negotiation), `errors.py` (one exception-to-`RequestError` mapping), and
both transports — `transport_stdio.py` and `transport_ws.py`. An ACP client can spawn the process and complete `initialize`
today; session and prompt methods answer `-32601` until Phases 2 and 3 fill them in, and
the agent cannot reach the MCP backend yet — that is the Phase 2 registry.

The capability block that handshake returns is **entirely off**, by construction rather
than by omission: every field is a row of `AGENT_CAPABILITY_MANIFEST` carrying the bead
that will flip it, and a row cannot be turned on without a test proving the feature
behind it runs. See [capabilities.py](src/python_acp/capabilities.md).

The WebSocket path is rebound (`pyacp-tzd.3`): under `--transport ws` a client reaches
the same agent through the same router. What is left of the old path is the deprecated
surface in `legacy_ws.py`, intercepted before the SDK — see
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
- Path constraints (`paths.py`): the absolute-path rule, and the containment boundary a session's `cwd` plus `additionalDirectories` define. Phase 4.2's `fs/*` calls are its first consumer.
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
    Router["turn_mcp_router.py<br/>McpToolRouterExecutor"]
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
    Agent --> Paths
    Agent --> Turns
    Turns -.implemented by.-> Router
    Router --> Backends
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

Every inbound WebSocket frame takes one of two paths, and which one is decided before
the SDK sees it.

```mermaid
sequenceDiagram
    participant C as WebSocket Client
    participant T as transport_ws.py
    participant L as legacy_ws.py
    participant SDK as acp.run_agent + router
    participant A as agent.py
    participant M as MCPStdioClient
    participant S as MCP Server

    C->>T: JSON frame
    T->>T: decode; -32700 / -32600 answered here
    alt deprecated surface (action, or a non-ACP method)
        T->>L: respond(message)
        L->>M: tools/*, prompts/*, resources/*
        M->>S: JSON-RPC over stdio
        alt server returns an error
            S-->>M: JSON-RPC error
            M-->>L: MCPProtocolError (code preserved)
            L-->>T: raises
            T-->>C: mapped error, MCP code forwarded
        else server returns a result
            S-->>M: JSON-RPC result
            L-->>T: reply payload
            T-->>C: success payload
        end
    else ACP request
        T-->>SDK: decoded message
        SDK->>A: routed method
        A-->>SDK: response model or RequestError
        SDK-->>C: JSON-RPC result or error
    end
```

Three things the diagram is drawn to make visible:

- **The deprecated branch never reaches the SDK.** `is_legacy` decides on the way in, so
  `tools/list` and `{"action": ...}` are answered without the agent being consulted. That
  branch is what Phase 7 deletes.
- **Framing errors are answered by the transport.** The SDK's `Transport` moves decoded
  dicts, so malformed JSON has no way to travel upward.
- **A `tools/call` result carrying `isError: true` is not an error.** It is a tool
  failure, not a request failure, and travels the success path with its content intact.
  A backend *protocol* error is the other branch, and keeps the MCP server's own code.

Under `--transport stdio` the same picture holds with the deprecated branch removed —
there is no legacy surface on stdio, and never was.

### Cancelling a turn

`session/cancel` is a *notification* that arrives while `session/prompt` is still open,
which is why the turn runs as its own task: a cancel needs something to reach. It crosses
four modules, and the ordering of the last two hops is the part worth drawing.

```mermaid
sequenceDiagram
    participant C as ACP client
    participant A as agent.py
    participant S as sessions.py
    participant X as turn_mcp_router.py
    participant M as mcp_stdio.py
    participant P as MCP server

    C->>A: session/prompt
    A->>S: attach_turn(task)
    A->>X: execute(context, prompt) as a task
    X->>M: tools/call
    M->>P: JSON-RPC request id N
    C-)A: session/cancel (notification)
    A->>S: cancel_turn()
    S->>S: set Session.cancellation, then task.cancel()
    M->>P: notifications/cancelled {requestId: N}
    M-->>X: CancelledError, re-raised
    Note over A,X: the task finishes cancelled; only then is the response built
    A-->>C: PromptResponse {stopReason: "cancelled"}
```

- **The flag is set before the task is cancelled**, so an executor's `except
  CancelledError` handler can tell `session/cancel` from the whole request dying.
- **The in-flight MCP request is un-asked, not merely dropped** — otherwise the server
  keeps computing a reply nobody will read, and a stdio server queues everything behind
  it.
- **Nothing emits after the response**, because `agent.py` builds it only once the turn
  task is done.
- A client answering `session/request_permission` with `DeniedOutcome` reaches the same
  `stopReason` with **no task cancellation at all**; the executor returns it. See
  [turns.md](src/python_acp/turns.md).

### Reading and writing files through the client

A turn's third direction. The first two are inbound requests and outbound
`session/update` notifications; this one is the agent making a **request of the client**
and waiting for the answer, on the same connection, while `session/prompt` is still open.
`fs/read_text_file` and `fs/write_text_file` are `acp.interfaces.Client` methods — the
agent calls them and never serves them — so no file is opened in this process.

```mermaid
sequenceDiagram
    participant C as ACP client
    participant A as agent.py
    participant X as turn_mcp_router.py
    participant F as paths.py
    participant M as MCP server

    C->>A: session/prompt (read: ..., write: ...)
    A->>X: execute(context, prompt)
    X->>F: require_contained(path, session.roots)
    F-->>X: resolved path, or refuse the turn
    Note over X: context.allows(Gate.READ_TEXT_FILE) — a client with no fs is refused here, before anything runs
    X->>C: session/request_permission
    C-->>X: selected option
    X->>C: fs/read_text_file {path, line, limit}
    C-->>X: content
    X->>M: tools/call (content substituted into an argument)
    M-->>X: result
    X->>C: fs/write_text_file {path, content}
    C-->>X: ok
    X-)C: session/update tool_call_update (completed)
    A-->>C: PromptResponse {stopReason: end_turn}
```

- **Containment is checked before the turn runs anything**, and the **resolved** path is
  what goes on the wire — the client is not asked to re-walk a symlink this side already
  followed. [paths.md](src/python_acp/paths.md) owns the rule.
- **The gate is read twice.** `allows` at parse time, where a client with no `fs` gets a
  `refusal`; `require` at the call, where a shut gate would mean *our* check was missing
  and `-32603` is the honest answer.
- **Permission comes first.** The client approving the call is what authorises pulling its
  files, so a denied call touches none.
- **A client that errors on `fs/*` fails that call, not the turn** — the same rule as a
  tool reporting `isError`, one layer out.

### Running a command in the client's terminal

The same direction as the file calls, with one thing the file calls do not have: a
**resource that outlives the request**. `terminal/create` answers with an id and the
process keeps running on the client's machine until `terminal/release` arrives, so every
path out of the turn has to give it back.

```mermaid
sequenceDiagram
    participant C as ACP client
    participant A as agent.py
    participant X as turn_mcp_router.py
    participant T as terminals.py
    participant M as MCP server

    C->>A: session/prompt (run: {arg: {command, args}})
    A->>X: execute(context, prompt)
    Note over X: context.allows(Gate.TERMINAL) — a client with no terminals is refused here
    X->>C: session/request_permission
    C-->>X: selected option
    X->>T: create(context, command, outputByteLimit)
    T->>C: terminal/create
    C-->>T: terminalId
    Note over T: tracked under this session, with the client that owns it
    alt the command finishes
        T->>C: terminal/wait_for_exit
        C-->>T: exitCode
        T->>C: terminal/output
        C-->>T: output, truncated
        T->>C: terminal/release
        X->>M: tools/call (output substituted into an argument)
        M-->>X: result
        A-->>C: PromptResponse {stopReason: end_turn}
    else session/cancel arrives first
        T->>C: terminal/kill
        T->>C: terminal/release
        Note over T: under asyncio.shield — the cancellation is already in flight
        A-->>C: PromptResponse {stopReason: cancelled}
    end
```

- **The terminal is released on every path**: completion, a command that failed, a tool
  that failed afterwards, cancellation, and `session/close` reaching a turn that is still
  running. [terminals.md](src/python_acp/terminals.md) has the table of which test covers
  which.
- **`outputByteLimit` is never omitted.** Unbounded output is a failure mode with no error
  message attached, and the captured bytes have to fit through the MCP request they end up
  in.
- **A disconnect releases nothing** — it *cannot*. The terminal is the client's and the
  connection that would carry the release has gone, so the handles are dropped and the
  terminals are the departed client's to reap.
- **A command that exits non-zero means the tool is never called**, the same asymmetry as
  a failed read: its argument would have to be invented.

### An MCP server asking the human a question

Every other flow starts at the ACP client. This one starts at the far end: a backend the
session opened sends **us** an `elicitation/create`, and the only person anywhere is on
the ACP connection. The bridge forwards rather than answers.

```mermaid
sequenceDiagram
    participant C as ACP client
    participant A as agent.py
    participant R as mcp_registry.py
    participant S as mcp_stdio.py
    participant E as elicitation.py
    participant M as MCP server

    C->>A: session/new
    Note over A: Gate.ELICITATION_FORM decides whether a forwarder exists at all
    A->>R: open(session, servers, roots, elicit)
    R->>S: initialize (capabilities: elicitation only if elicit is not None)
    S->>M: initialize
    M-->>S: result
    Note over C,M: later, mid-tool-call
    M->>S: elicitation/create {message, requestedSchema}
    S->>E: forwarded in a task of its own
    Note over S: the read loop keeps reading — the handler may wait on a human
    alt somebody is connected and can be asked
        E->>C: elicitation/create (form, session-scoped)
        C-->>E: accept | decline | cancel
        E-->>S: {action, content?}
    else nobody, or no elicitation.form
        E-->>S: {action: cancel}
    end
    S-->>M: result
    M-->>S: tools/call result
```

- **The promise and the answer are one decision.** A backend is told it may elicit only
  when a forwarder exists, and a forwarder exists only when the client that created the
  session advertised form-mode elicitation.
- **The read loop answers nothing itself.** Each server request is handled in its own
  task, because this one waits on a person — and a loop parked inside it could not read
  the response to the very call that provoked the question.
- **`cancel` is not an error**, and neither is a client that never advertised the
  capability. [elicitation.md](src/python_acp/elicitation.md) has the table of what is
  answered when there is no human to ask.

## Module Documentation

- [ACP agent module](src/python_acp/agent.md)
- [Capability manifest module](src/python_acp/capabilities.md)
- [Error mapping module](src/python_acp/errors.md)
- [Deprecated WebSocket surface module](src/python_acp/legacy_ws.md)
- [Path constraints module](src/python_acp/paths.md)
- [Session registry module](src/python_acp/sessions.md)
- [Client terminal registry module](src/python_acp/terminals.md)
- [Turn executor seam module](src/python_acp/turns.md)
- [MCP tool-router executor module](src/python_acp/turn_mcp_router.md)
- [CLI module](src/python_acp/cli.md)
- [MCP elicitation forwarding module](src/python_acp/elicitation.md)
- [MCP content mapping module](src/python_acp/mcp_content.md)
- [MCP backend registry module](src/python_acp/mcp_registry.md)
- [MCP stdio module](src/python_acp/mcp_stdio.md)
- [ACP stdio transport module](src/python_acp/transport_stdio.md)
- [ACP WebSocket transport module](src/python_acp/transport_ws.md)

## Conformance

`tests/test_conformance.py` is [docs/acp-compliance-matrix.md](docs/acp-compliance-matrix.md)
in executable form. Every `acp.interfaces.Agent` member has a row stating its disposition,
and the suite asserts the wire behaviour that disposition implies — including the
declines, which are asserted to return the *correct* error rather than merely to fail.

Three structural tests make a gap detectable rather than invisible:

- the table must cover every member of the `Agent` Protocol, in both directions;
- it must cover every method the SDK's router actually registers;
- the 16 names in `acp.meta.AGENT_METHODS` that the router does **not** register must stay
  unregistered, so an SDK bump that starts routing one is noticed.

A fourth binds advertisement to behaviour: every capability literal `initialize` sets is
mapped to the method it promises, and the method is called. A `true` with a broken method
behind it is the failure the capability manifest exists to prevent, and this is where the
promise meets the behaviour.

## Design Documents

- [ACP v1 plan](docs/full-apc-plan.md) — phases, decisions D1-D6, and delivery sequencing.
- [ACP v1 compliance matrix](docs/acp-compliance-matrix.md) — per-method disposition for every
  `acp.interfaces.Agent` and `Client` member, and the `initialize` capability block it dictates.
- [Interop](docs/interop.md) — the check that ACP v1 means something outside this repository, and the permission finding it produced.
- [Module boundaries](docs/module-boundaries.md) — the target module layout and the fate of `ws_bridge.py`, which `pyacp-tzd.3` carried out.

## Notes

- The WebSocket transport accepts two request styles. Only the first is ACP:
  - JSON-RPC ACP methods (`method` field) — dispatched by the SDK router to `agent.py`.
  - The deprecated surface — `{"action": ...}` messages, and the MCP passthrough
    (`tools/*`, `prompts/*`, `resources/*`, `ping`) still carried on JSON-RPC. Both live
    in [legacy_ws.py](src/python_acp/legacy_ws.md) and are removed in Phase 7.
- An ACP method with no implementation yet returns `-32601` from the SDK router.
- Error codes from the MCP backend are forwarded rather than collapsed into
  `-32603`, tagged with `data.source = "mcp"` to mark whose namespace they are
  from. See [the error mapping docs](src/python_acp/errors.md).
