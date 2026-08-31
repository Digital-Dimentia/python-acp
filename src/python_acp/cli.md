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

- `build_parser()`: defines `--transport`, `--host`, `--port`, `--mcp-config`,
  `--no-client-mcp-servers`, `--debug`.
- `_install_reload(catalogue, path)`: the `SIGHUP` handler, WebSocket transport only.
- `_load_catalogue(path)`: reads `--mcp-config`, or the empty catalogue when it was
  not given. Called before a port is bound, so a bad file fails at startup.
- `configure_logging(debug)`: installs the root handler on `sys.stderr`, with the
  bare message at INFO and `%(name)s: %(message)s` under `--debug`.
- `_run(args)`: async runtime bootstrap and service startup sequence.
- `run()`: sync wrapper that parses args and runs `_run()` with `asyncio.run`.

## Transports

`--transport` selects the client-facing wire. Both serve the **same** agent: the same
`initialize` negotiation, the same capability block, the same error codes.

| Value | What it does | Notes |
|---|---|---|
| `ws` *(default)* | Binds `WebSocketAgentServer` on `--host`/`--port` | Stays the default; see below. Since `pyacp-sld.3` it carries nothing `stdio` does not. |
| `stdio` | Serves ACP on this process's own stdin/stdout via [transport_stdio.py](transport_stdio.md) | How an editor spawns an agent (D2). `--host` and `--port` are ignored. |

### Why `ws` is still the default (`pyacp-6z4`, decided 2026-08-24)

The plan was to flip to `stdio` once the action surface went, since `stdio` is how an
editor spawns an agent (D2) and needs no port. **That flip was examined and declined.**

The reason is not inertia. It is that every *released* invocation is a WebSocket
invocation: `v0.1.0` and `v0.1.1` (both 2026-08-21) shipped a CLI of exactly
`--mcp-command`, `--host`, `--port` — **there was no `--transport` flag at all**, and no
way to speak `stdio`. Anyone running a published version binds a socket. Changing the
default would take that away from them silently, in exchange for nothing: since
`pyacp-sld.3` the two transports serve the identical agent, so the flip buys no
capability, only a different first impression.

That the two are identical is the argument *against* flipping, not for it. A breaking
change to a shipped default needs to buy something, and this one does not.

`stdio` remains fully supported and one flag away. Revisit only alongside a release that
is already breaking the CLI contract for other reasons — the cost is then already paid,
and `--transport stdio` should become the documented default in the same release note.
`tests/test_transport_stdio.py::test_ws_stays_the_default_transport` pins the current
answer so the flip cannot happen by accident.

## stdout is reserved under `--transport stdio`

`cli.py` must never `print()`, in any mode. Under `--transport stdio` stdout is the
JSON-RPC wire and one stray byte corrupts it; keeping a single logging path in *every*
mode is what stops a banner from creeping back in later. `configure_logging` names
`sys.stderr` explicitly rather than relying on `basicConfig`'s default for the same
reason. `transport_stdio.py` adds a structural backstop on top of this discipline — see
its docs.

## `--debug` names the logger, and why that is not cosmetic

The root handler is shared with everything else that logs, and the loudest of those is
`websockets`: `transport_ws.py` calls `serve()` without a `logger=`, so the library
emits through `websockets.server` — including an INFO `server listening on host:port`
at startup, which arrives whether or not `--debug` was passed.

Under the bare `%(message)s` format that line is indistinguishable from this module's
own `python-acp listening on ws://host:port`, so a normal startup reads as one program
announcing itself twice in inconsistent words. Neither line is wrong; the format simply
hid which was which. `--debug` therefore switches to `%(name)s: %(message)s`, which
matters most exactly when `--debug` is on and the library's per-connection and
handshake chatter is interleaved with our MCP and turn diagnostics.

INFO keeps the bare format: at that level every line really is this process, and a
prefix would be noise. `tests/test_transport_stdio.py::test_debug_logging_names_the_logger_that_emitted_each_record`
pins both halves.

## Startup Flow

```mermaid
flowchart TD
    Start["process start"] --> Parse["build_parser + parse_args"]
    Parse --> AsyncRun["asyncio.run(_run)"]
    AsyncRun --> Log["configure_logging → stderr"]
    Log --> Registries["build sessions + backends + terminals, wire on_close"]
    Registries --> Pick{"--transport"}
    Pick -- stdio --> Stdio["run_stdio(PythonAcpAgent())"]
    Pick -- ws --> Guard{"off loopback<br/>with no access key?"}
    Guard -- yes --> Refuse["UnauthenticatedBindError<br/>logged, exit 2"]
    Guard -- no --> BridgeStart["start WebSocketAgentServer"]
    BridgeStart --> Serve["serve_forever"]
    Stdio --> Stop["client disconnects, or KeyboardInterrupt"]
    Serve --> Stop
```

The guard raises from `WebSocketAgentServer.__init__`, so it fires before a port is
bound — and it lives in the transport rather than here, so a caller embedding the server
in its own program inherits it. `run()` turns it into **exit 2**, argparse's own code for
a usage refusal, logging the one sentence that names the fix rather than a traceback.

## The access key comes from the environment, never from a flag

`--transport ws` reads `PYTHON_ACP_WS_KEY` and `PYTHON_ACP_WS_ALLOW_UNAUTHENTICATED`
through `access_key_from_env()` and `unauthenticated_bind_allowed()`. There is deliberately
**no `--ws-key` flag**: `argv` is world-readable through `ps`, so a flag would publish the
secret to every other user of the machine at the moment it is used to protect it.

Both are ignored under `--transport stdio`, like `--host` and `--port`, because there is
no socket to admit anyone to. The full design is in
[transport_ws.py docs](transport_ws.md).

## Error and Shutdown Behavior

- `UnauthenticatedBindError` exits **2** with the fix logged to stderr — see the flowchart
  above.
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

## `--mcp-config` is not that flag returning

It names a **catalogue of recipes** a client may select from, not a server this process
runs. Nothing about the lifetime changes:

| | `--mcp-command` (removed) | `--mcp-config` |
| --- | --- | --- |
| What starts at bootstrap | one MCP server subprocess | nothing — the file is read, that is all |
| Servers per process | one, shared by every client | one subprocess set **per session**, as today |
| Torn down when | the process exits | the session closes, as today |
| Client reaches it by | a passthrough method on the socket | `session/prompt`, like any other tool |
| Client chooses | nothing | which entries its session uses |

What it changes is where a recipe may come from. That matters because on a socket,
accepting `command` and `args` from a client is accepting a request to **execute an
arbitrary binary** — unremarkable when the client is the editor that spawned you, and a
different thing entirely when it is whoever got past the access key. A catalogue lets a
deployment offer a list the operator approved.

It is optional and additive: without it a session's servers are exactly the ones its
client named, and with it they are the client's **plus** the catalogue entries that
session has switched on. The reasoning, the file format, and why its validation is loud
are in [mcp_catalogue.py](mcp_catalogue.md).

The file is read **here**, before a port is bound, so a mistyped path or key fails at
startup with exit 2 rather than at the first `session/new`.

## `SIGHUP` re-reads the catalogue, on one transport

`--mcp-config` was read once and never again, so adding a server cost a restart — which on
the WebSocket transport drops every connected client and every live session, for a
deployment whose whole point is being long-lived and shared. `_install_reload` fixes that
(`pyacp-izr`).

**Why a signal.** Reloading is an *operator* action, and both alternatives fit it worse. An
ACP extension request would need a client to send it, putting a deployment decision in the
hands of whoever connected. Watching the file — a poll or an OS watcher — is the most
convenient and the most surprising: an editor saving a half-written file is a reload nobody
asked for. `SIGHUP` is the conventional daemon answer, costs one handler, and is explicit
about when it happens.

**Why not under `--transport stdio`.** There the process is the client's **child**: the
editor spawned it, restarting it is trivial and is what the editor already does, and an
operator in a position to send it a signal is not the person configuring it. The handler is
simply not installed, and the branch that does not install it is where that decision is
recorded rather than something that fell out of where the code sits.

Nothing is installed without `--mcp-config` either — there is no file to re-read, and a
handler logging "reloaded nothing" on every `SIGHUP` would be noise on a signal a process
may be sent for other reasons. `SIGHUP` does not exist on Windows, which is one `hasattr`
and not a reason to fail a bind.

**A file that does not parse changes nothing.** `load` builds a *new* catalogue and raises
before `replace` is reached, so the running one is untouched and the error names the file,
the entry and the key exactly as it does at startup. There is no half-applied state to be
in.

What a reload *means* for a session already open — the four cases, and why the work happens
at the session's next request rather than at the signal — is
[agent.md](agent.md); this module only decides when the file is read.

## `--no-client-mcp-servers` is the other half of the catalogue

`--mcp-config` made selection **available**. It did not make supply **refusable**: without
this flag, `session/new` still accepts `mcpServers` from any client, so on a shared socket
a client past the access key can still make this process spawn an arbitrary binary. The
catalogue offered an alternative to that; this closes it.

**Why a flag and not a default.** Refusing by default would break every client that names
its own servers today — including `tests/interop/client.py` and a good part of the suite —
and it would be *wrong* under `--transport stdio`, where the client spawned this process
and supplying its own configuration is the canonical ACP arrangement. The risk is specific
to a long-lived socket with clients the operator did not start, so the refusal is an
opt-in an operator sets when binding somewhere shared.

**Honoured on both transports** even so. The flag is most useful on a socket, but an
operator wrapping this agent in a launcher may want it under stdio too, and one deployment
config should mean one thing everywhere. A flag silently ignored on one transport is worse
than either answer, and refusing the *combination* at startup would make a config
non-portable for no safety gained.

Three properties worth stating, all asserted in `tests/test_agent.py`:

- **It refuses; it does not filter.** A non-empty `mcpServers` is `-32602`. A session
  backed by fewer servers than were asked for is the failure mode the README warns about
  for ACP's `skip-invalid-items`, and a second route to it would be worse than the door
  this shuts.
- **An empty list is still accepted.** That is exactly what a catalogue-only client sends,
  and what every existing test sends.
- **The refusal names the flag and lists the catalogue.** A client told "no" without being
  told where servers *do* come from has nothing to do next. When there is no catalogue
  either, it says that instead and points at `--mcp-config`, because a deployment with the
  flag and no catalogue runs no MCP servers at all — legitimate to want, and very easy to
  do by accident. `run()` says the same thing once at startup for the operator.

Both doors go through one funnel: `agent._reject_unsupported_mcp_servers`, which
`session/new` and `session/fork` both call **before** creating anything, so a refused
request leaves nothing behind. See [agent.py](agent.md).

## Related

- [Repository architecture](../../ARCHITECTURE.md)
- [agent.py docs](agent.md)
- [transport_stdio.py docs](transport_stdio.md)
- [mcp_catalogue.py docs](mcp_catalogue.md)
- [mcp_stdio.py docs](mcp_stdio.md)
- [mcp_registry.py docs](mcp_registry.md)
- [transport_ws.py docs](transport_ws.md)
