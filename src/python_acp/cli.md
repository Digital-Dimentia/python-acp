# cli.py

## Purpose

`cli.py` is the process entrypoint. It parses startup arguments, initializes logging,
builds the three process-wide registries, and hands the process to the selected
client-facing transport. It owns no protocol logic of its own.

**It starts no MCP server.** Every backend this process talks to is named by a client in
`session/new` and lives and dies with its session.

## Key Responsibilities

- Define command-line interface options, including `--transport`.
- Configure runtime logging (`INFO` or `DEBUG`) **onto stderr**.
- Construct the session, MCP-backend, and terminal registries, and wire `on_close`.
- Start and hold whichever transport was selected.

## Main Symbols

- `build_parser()`: defines `--transport`, `--host`, `--port`, `--debug`.
- `configure_logging(debug)`: installs the root handler on `sys.stderr`.
- `_run(args)`: async runtime bootstrap and service startup sequence.
- `run()`: sync wrapper that parses args and runs `_run()` with `asyncio.run`.

## Transports

`--transport` selects the client-facing wire. Both serve the **same** agent: the same
`initialize` negotiation, the same capability block, the same error codes.

| Value | What it does | Notes |
|---|---|---|
| `ws` *(default)* | Binds `WebSocketAgentServer` on `--host`/`--port` | Stays the default because it is what existing deployments bind. Since `pyacp-sld.3` it carries nothing `stdio` does not. |
| `stdio` | Serves ACP on this process's own stdin/stdout via [transport_stdio.py](transport_stdio.md) | How an editor spawns an agent (D2). `--host` and `--port` are ignored. |

The default was to flip to `stdio` when the action surface went. It has not, and the
reason is now inertia rather than capability: flipping it would break every existing
WebSocket invocation for no gain, since the two are the same agent. Filed as its own
decision rather than done silently here.

## stdout is reserved under `--transport stdio`

`cli.py` must never `print()`, in any mode. Under `--transport stdio` stdout is the
JSON-RPC wire and one stray byte corrupts it; keeping a single logging path in *every*
mode is what stops a banner from creeping back in later. `configure_logging` names
`sys.stderr` explicitly rather than relying on `basicConfig`'s default for the same
reason. `transport_stdio.py` adds a structural backstop on top of this discipline — see
its docs.

## Startup Flow

```mermaid
flowchart TD
    Start["process start"] --> Parse["build_parser + parse_args"]
    Parse --> AsyncRun["asyncio.run(_run)"]
    AsyncRun --> Log["configure_logging → stderr"]
    Log --> Registries["build sessions + backends + terminals, wire on_close"]
    Registries --> Pick{"--transport"}
    Pick -- stdio --> Stdio["run_stdio(PythonAcpAgent())"]
    Pick -- ws --> BridgeStart["start WebSocketAgentServer"]
    BridgeStart --> Serve["serve_forever"]
    Stdio --> Stop["client disconnects, or KeyboardInterrupt"]
    Serve --> Stop
```

## Error and Shutdown Behavior

- `KeyboardInterrupt` is caught in `run()` to allow clean interactive shutdown.
- MCP subprocesses belong to sessions, so they are torn down by `on_close` below and by
  `sessions.close_all()` on the way out — not by anything this module holds directly.
- A backend that cannot handshake fails its `session/new` with an error the client can
  act on, rather than failing this process at startup. See
  [mcp_registry.py docs](mcp_registry.md).
- Under `--transport stdio`, the client closing the pipe ends `run_stdio` and the
  process exits normally.

## All three registries are created here, because only here can they be connected

```python
backends = McpBackendRegistry()
terminals = TerminalRegistry()

async def release_session(session_id: str) -> None:
    await terminals.close(session_id)
    await backends.close(session_id)

sessions = SessionRegistry(on_close=release_session)
```

They live here rather than in a transport or an agent for two reasons. A session must
outlive the connection that created it — `session/resume` means nothing otherwise — and
the WebSocket transport builds one agent per socket. And `on_close` is the entire coupling
between them (decision B6a): `sessions.py` never imports MCP or terminals, so if this
wiring is missed, every closed session leaks a subprocess and a client-side process. `_run`
also calls `sessions.close_all()` on the way out, because sessions the client never closed
still own theirs.

`SessionRegistry` takes **one** hook and two registries need it, so the composition is
here too — this is the only place that constructs all three. Terminals are released first:
they are requests over a live connection with a client waiting on `session/close`, while
MCP teardown is local subprocess work nobody is watching.

Under `--transport stdio` there is deliberately **no** disconnect hook: the client going
away is this process ending, so the shutdown path above is the same event. The WebSocket
transport does have one, and it forgets rather than releases — see
[terminals.md](terminals.md).

See [sessions.py](sessions.md), [mcp_registry.py](mcp_registry.md),
[terminals.py](terminals.md), and [transport_ws.py](transport_ws.md).

## There is no `--mcp-command`, and that is the end of a two-step

It started a **process-wide** MCP server, handshaked before any listener bound. `pyacp-db3`
made it optional once ACP sessions carried their own servers in `session/new`; from then
on its only consumer was the deprecated action surface, which predated sessions and had
nowhere else to look. `pyacp-sld.3` deleted that surface and `pyacp-sld.4` deleted the
flag with it.

The flag is not merely ignored — it is **rejected**, and
`test_there_is_no_process_wide_backend_flag` asserts the rejection. A deployment that
still passes it should fail loudly at startup rather than run while quietly never using
the server it named.

## Related

- [Repository architecture](../../ARCHITECTURE.md)
- [agent.py docs](agent.md)
- [transport_stdio.py docs](transport_stdio.md)
- [mcp_stdio.py docs](mcp_stdio.md)
- [mcp_registry.py docs](mcp_registry.md)
- [transport_ws.py docs](transport_ws.md)
