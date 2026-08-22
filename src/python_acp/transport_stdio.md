# `transport_stdio.py` — the ACP stdio binding

Binds `PythonAcpAgent` to this process's own stdin and stdout. This is the transport
real ACP clients use (decision D2): an editor spawns `python-acp` as a subprocess and
speaks JSON-RPC over the pipe.

The module owns the binding and the listen/shutdown loop, and nothing else. No argument
parsing (that is [cli.py](cli.md)), no agent-shaped logic (that is [agent.py](agent.md)).

`transport_*` faces the ACP client; `mcp_*` faces the backend. Two stdio modules sit
near each other in this directory and mean opposite directions —
[mcp_stdio.py](mcp_stdio.md) drives a *server subprocess we spawn*, while this module
serves a *client that spawned us*.

## Main symbols

| Symbol | Purpose |
|---|---|
| `run_stdio(agent, *, use_unstable_protocol=True)` | Serve `agent` over stdin/stdout until the client disconnects |

## `use_unstable_protocol` defaults to True, deliberately

`session/close`, `session/fork`, and `session/resume` are registered `unstable=True` in
the SDK's agent router. With the flag off, the router answers `method_not_found`
**without ever calling the agent** — those three methods would be dead code no matter
how completely Phase 2 implements them.

The flag is protocol-visible, so it belongs with the connection rather than being
discovered later. See [docs/acp-compliance-matrix.md](../../docs/acp-compliance-matrix.md).

## stdout is reserved

Once the agent is bound, **stdout is the wire.** A single byte that is not a JSON-RPC
message corrupts the stream, and the failure surfaces at the client as a parse error far
from whatever printed it.

Discipline alone does not cover this: a `print()` anywhere in the process — ours, or a
dependency's on an unlucky path — does the damage. So `run_stdio`:

1. binds the real stdout into the SDK's writer via `stdio_streams`, **then**
2. points `sys.stdout` at stderr for the life of the connection.

A stray `print()` then lands in the log, where it can be read, instead of in the
protocol, where it cannot. Binding before swapping is what makes this safe — the writer
already holds the pipe by the time `sys.stdout` changes.

**Windows is excluded from the swap on purpose.** The SDK's Windows stdio transport
resolves `sys.stdout` at *write* time (`acp/stdio.py`, `_StdoutTransport.write`) instead
of holding the pipe, so redirecting it there would send the JSON-RPC stream to stderr
and leave the client hearing nothing at all. On POSIX the transport holds the pipe from
`connect_write_pipe`, so the swap cannot reach it.

`cli.py` keeps the complementary discipline — one logging path, on stderr, in every
mode — because this backstop should never be the only thing standing between a banner
and the wire.

## Buffer limit

`_STDIO_BUFFER_LIMIT_BYTES` is 50 MiB, mirroring the SDK's own default for `run_agent`.
`stdio_streams` on its own defaults to asyncio's 64 KiB, which a single multimodal
prompt can exceed. Because this module binds the streams itself, the limit is ours to
pass rather than the SDK's to supply.

## Shutdown

`run_agent` shields `close()`, so the connection is torn down even when the listen loop
ends on an exception. The client closing the pipe ends the loop and `run_stdio` returns;
`cli.py`'s `MCPStdioClient` context manager then stops the backend.

## Tests

`tests/test_transport_stdio.py` spawns `python -m python_acp.cli --transport stdio` as a
real subprocess and drives it with the SDK's own `ClientSideConnection`. Nothing there
touches the agent object, so what is under test is what an editor actually gets: process
startup, the binding, JSON-RPC framing over a pipe, and clean exit on disconnect. One
test asserts that **every** line on stdout parses as JSON-RPC — a banner or a traceback
would not.

## Related

- [agent.py docs](agent.md)
- [cli.py docs](cli.md)
- [mcp_stdio.py docs](mcp_stdio.md)
- [ACP v1 compliance matrix](../../docs/acp-compliance-matrix.md)
