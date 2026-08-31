# `agent.py` — the ACP agent runtime

`PythonAcpAgent` is this project's implementation of `acp.interfaces.Agent`. It is the
protocol edge: it validates, delegates, and serializes, and owns nothing else. Session
state (`sessions.py`), turn execution (`turns.py`), and MCP calls (`mcp_stdio.py`) all
sit below it and arrive in later phases.

**Both transports bind this class.** [transport_stdio.py](transport_stdio.md)
(`pyacp-tzd.2`) and [transport_ws.py](transport_ws.md) (`pyacp-tzd.3`) each run it
through `acp.run_agent`, one agent instance per connection, so a client gets the same
answers whichever wire it arrived on. Session and prompt methods answer `-32601` until
Phases 2 and 3 fill the bodies in.

`transport_*` faces the ACP client; `mcp_*` faces the backend. Two stdio modules sit near
each other in this directory meaning opposite directions.

## Dispatch is not ours

`acp.agent.router.build_agent_router` maps JSON-RPC method names onto this class's
attributes, and `acp.connection` turns a returned model into a result and an
`acp.RequestError` into an error object. Nothing here parses a request id, builds an
error envelope, or knows a transport exists.

Three mechanics of that arrangement are load-bearing:

| Mechanic | Consequence for this module |
|---|---|
| The router splats the request's `_meta` keys in as kwargs (`acp/router.py:104-107`) | **Every method takes `**kwargs`.** A closed signature raises `TypeError` the first time a client attaches metadata. |
| Every agent route is registered `optional=False` | A member this class does not define is already `-32601`. Declining a method means *omitting* it — never a hand-built error. |
| `session/close`, `session/fork`, `session/resume` are registered `unstable=True` | With `use_unstable_protocol` off, the router raises `method_not_found` **without calling us**. The connection must be built with the flag or those three are dead code. |

That last one is why `pyacp-tzd.2` and `pyacp-tzd.3` must pass
`use_unstable_protocol=True` to `acp.run_agent`. It is a protocol-visible choice, not a
detail of Phase 2.

## One agent per connection, one registry per process

`PythonAcpAgent` takes its `SessionRegistry` rather than making one, and the argument is
**required** on purpose. The WebSocket transport builds an agent per socket; a
per-agent registry would mean a client could not resume a session it created on a
connection that has since dropped, and `session/resume` would be meaningless. A default
would hide that. `cli.py` constructs the one registry and hands it to whichever transport
is bound.

The [`McpBackendRegistry`](mcp_registry.md) and [`TerminalRegistry`](terminals.md) are
process-wide for the same reason and are taken the same way: both are keyed by session id,
and both are torn down by the session registry's `on_close` hook rather than by anything
here. `cli.py` is the only place that constructs all three, which is why it is the only
place that can wire them together.

The `Client` facade goes the other way — `on_connect` stores *the* connection's, so it
must not be shared. `connected_client` exposes it without raising, for the one caller that
runs *after* a connection ends: `transport_ws.py` hands it to
`TerminalRegistry.forget_client` when a socket closes, where "there was never a client" is
an ordinary answer rather than the bug `client` treats it as.

## Running a turn

`session/prompt` runs the executor as its **own task**. That is what makes
`session/cancel` — a notification arriving on the same connection while the request is
still open — have something to reach; running the turn inline would leave the cancel with
nothing to cancel.

The wait is `asyncio.wait({turn})`, not `await turn`, because the two cancellations must
not be confused:

| What happened | `await turn` | `asyncio.wait({turn})` |
|---|---|---|
| `session/cancel` cancelled the turn | raises `CancelledError` here | returns; `turn.cancelled()` is `True` |
| *this request* was cancelled | raises `CancelledError` here | raises `CancelledError` |

`await turn` makes the two indistinguishable, and answering `stopReason: "cancelled"` for
a request that was itself cancelled would put a reply on a wire nobody is reading.
`wait` only raises when we are cancelled, so `turn.cancelled()` afterwards is an
unambiguous answer to "did `session/cancel` reach it".

`detach_turn` runs in a `finally`, so a cancelled session accepts the next prompt.
`TurnAlreadyRunningError` from `attach_turn` cancels the task it just created rather than
leaving one un-awaited.

The executor defaults to [`McpToolRouterExecutor`](turn_mcp_router.md) over this agent's
backend registry — decision D3's shipped default, deterministic and with no LLM in it.
`turns.IdleTurnExecutor` is still available for a caller that wants a turn to do nothing.

`detach_turn` runs in a `finally`, so a cancelled session accepts the next prompt, and
the executor's `TurnResult` supplies both `stopReason` and `usage` — `PromptResponse`
carries the second and nothing was filling it.

The client's declared capabilities are handed to the `TurnContext`, because gating is
per-connection and an executor must not have to reach back through the agent for it. The
seam itself is [turns.py](turns.md), and `turns.STOP_REASON_DISPOSITIONS` is the whole
`stopReason` contract — which three this agent returns, and why the two limit conditions
are not among them.

**The response is built only after the turn task is done.** That is what makes "no
`session/update` after the response" a property of the code rather than a rule executors
are asked to follow: an executor emitting from an `except CancelledError` cleanup block
is still inside the task, so its notification is on the wire before the answer is.

That covers every path that *has* a response. The one that does not is this request itself
being cancelled: `prompt` cancels the turn task and re-raises **without awaiting it**,
because awaiting a task inside a dead request is how a hang gets made if an executor
ignores cancellation. `context.detach()` in the `finally` closes that window — `emit`
raises `DetachedTurnError` from then on, so a shielded cleanup cannot write to a socket
nobody is reading. `pyacp-48b`; the reasoning is in [turns.py](turns.md) under "Emitting
on the way out".

`prompt` reports the executor's `stopReason` rather than deciding it, with **one
override**: if `session/cancel` was delivered to this turn and the executor still returned
something other than `cancelled`, the response says `cancelled` and a warning names the
executor. Answering `end_turn` for a turn the client explicitly stopped would be a lie,
and the flag is per turn — `attach_turn` installs a fresh event — so a previous turn's
cancellation cannot trigger it. Route two to `cancelled` needs no such rescue: a client
that answers a permission request with `DeniedOutcome` makes the executor *return*
`cancelled` with nothing cancelled anywhere.

## Load reconstitutes, resume reattaches

Both take a session id and both return the same settings, so the difference is easy to
collapse and expensive to get wrong. It is the **replay**:

| | `session/load` | `session/resume` |
|---|---|---|
| Sends the transcript again | **yes**, before the response | no |
| For a client that | lost its copy — restarted, reconnected | still has it and is picking the session back up |

Replaying on resume would duplicate every message the client already rendered.

The replay goes out **before** the response, which is the ordering the spec asks for: a
client that received the result first would have no way to tell replayed updates from
live ones on a session that is already running.

**`loadSession: true` claims the method works, not that a session outlives the agent.**
Nothing here persists across a restart, so `session/load` succeeds for a session this
process still holds and answers `-32602` for anything else.

`resume_session` receives `cwd` and `mcpServers` and applies neither. Changing either
mid-session would silently invalidate paths and tool names the transcript already refers
to; a client that wants different ones wants a fork.

## The unstable three are advertised per connection

`session/close`, `/fork`, and `/resume` are registered `unstable=True` in the SDK's
router, which answers `method_not_found` for them **without calling the agent** when the
connection lacks `use_unstable_protocol`. Advertising them there would be a promise the
SDK itself refuses to keep, so `PythonAcpAgent(unstable=...)` mirrors the connection's
flag and `capabilities.build_agent_capabilities(unstable=...)` withholds those three rows
when it is off. Both transports pass `True`.

## Every routed method has a body

`pyacp-fln.3` was the last. Nothing in this class answers `method_not_found` any more —
the only `-32601` a client can get is the router's, for a name the SDK does not route at
all, or `ext_method`'s for an unknown extension. `_not_implemented` was deleted with its
last caller.

## Mode and config changes go out through one door

`announce_mode` is the only thing that emits `current_mode_update`, and
`set_session_mode` calls it rather than emitting inline. The notification goes out even
though the client is the one who asked, for two reasons: a second client attached to the
same session needs it to stay in step, and an internally-originated change — a future
executor deciding it must drop out of `auto-approve`, say — should be indistinguishable
on the wire from a client-driven one. Nothing internal changes a mode today; the door
exists so that when something does, it does not invent a second way to say so.

`announce_config_options` is its twin, for the same reasons.

Modes come from the **executor** (`TurnExecutor.session_modes`), the only thing that can
act on them — the same arrangement as `promptCapabilities`. Config options come from the
executor *and* from the **MCP catalogue**, one boolean per configured server, appended
after the executor's. Each session gets a deep copy of both, because `set_mode` and
`set_config_option` mutate in place.

That second source is how a client **selects** MCP servers instead of supplying them.
ACP's `select` variant is single-choice, so a set of booleans is what a multi-select looks
like; no extension is involved, because the options ride `NewSessionResponse.configOptions`
and `session/set_config_option` already changes them. The ids are namespaced `mcp/<name>`
so a catalogue entry cannot shadow one of the executor's — `Session.set_config_option`
looks options up by id alone. See [mcp_catalogue.py](mcp_catalogue.md) for why an
agent-side list exists at all, and why it is not `--mcp-command` returning.

**`session/new` opens the client's servers *and* the catalogue's**, not one or the other:
an editor that knows its own keeps naming them, a thin client selects, and one session can
have both. A name in both is `-32602` naming both sources — `McpBackendRegistry.open`
would refuse the duplicate anyway, but its message says "in `session/new`", which is a
misleading thing to tell someone whose collision came from a config file they may not have
written. `session/fork` inherits the parent's actual *selection*, including entries
toggled after it was created, because that is what `mcp_registry` kept the specs for.

**An `mcp/*` option is an action, not a stored flag.** Setting one spawns or tears down
that catalogue server for the session, then re-announces the config options *and* the
palette — the session's tools just changed, so the list naming them is stale. This is the
case [announcer.py](announcer.md) exists to serve, taken inline rather than from an
observer, because the client named the session itself and already knows the id.

Three rules make that safe, and each of them is a failure someone would otherwise hit:

- **Refused while a turn is running.** Closing a backend out from under a `tools/call`
  turns a live call into a broken pipe, and the client would see a backend error for
  something it did on purpose. `session/cancel` and waiting are both available and neither
  silently loses work. It applies in *both* directions — a turn already holds its backend
  map, so a server added mid-turn would be invisible to it anyway.
- **Set first, act second, revert on failure.** The other order would mean writing
  `Session.set_config_option`'s boolean-versus-select validation a second time, in the one
  place it must not drift from.
- **A failed spawn is an error, not a closed session.** `open`'s all-or-nothing rule at
  one server's granularity: the option goes back to `false`, the session's other servers
  are untouched, and the session — which was working a moment ago — still is.

**`set_config_option` is one implementation for two request shapes.** The SDK
discriminates `SetSessionConfigOptionBooleanRequest` from its select twin on `type` and
splats either into the same parameters, so the only difference that arrives is what
`value` holds — and `Session.set_config_option` is what knows which of the two the named
option can take. Two methods would mean writing that check twice.

## Commands are announced once the client can place the session id

`available_commands_update` gives a client its command palette as soon as it holds a
session, without having to take a turn first. There are **two doors onto one write**,
and the difference between them is when the list is built, not what is in it.

**Inline, from `session/load` and `session/resume`.** Both take the session id as a
parameter, so the client already knows which session an update is about and the handler
can simply send it. `announce_commands` builds the list and sends it. On `load` it goes
out *after* the replay: the replay is what happened, and splicing a current listing into
it would rewrite the record. Every re-announcement later in a session — a catalogue
server toggled on, a turn refreshing the palette — takes this door too, and must, because
the list changes.

**From a stream observer, for `session/new` and `session/fork`.** Both mint an id the
client has never seen, and it reaches the client in the *response* — so a `session/update`
sent from inside the handler goes out first, names a session the client cannot place, and
is dropped. Nor can the handler schedule one: a `create_task` can reach the transport
while the handler is still running, so it races the reply. The hook that works is on the
far side of the write — `acp.Connection._run_request` sends the response and *then*
notifies its stream observers. [announcer.py](announcer.md) owns that observer and the
request-id matching it needs; this module stays free of both, as its own docstring
promises.

### Why the minting paths build the list before they answer

Following the response is necessary but not sufficient, and the gap is what `pyacp-svt`
turned up: the interop test failed on Python 3.11 four runs in five while passing every
time on 3.14.

An observer is a *task*, and a client may pipeline — the SDK's own client sends
`session/prompt` the moment `session/new` returns. Building the palette costs a
`tools/list` per backend, and awaiting that inside the observer parks it on real
subprocess I/O. The pipelined request is read and answered in the meantime, so the turn's
first `session/update` reaches the wire first and the palette arrives *after* the updates
it exists to precede. Which of the two sub-millisecond round trips wins is scheduling
luck.

So `session/new` and `session/fork` call `_prepare_commands` before they return, stashing
the list against the new id, and the observer is given `announce_prepared_commands` — a
door whose **first await is the send**. `acp.task.MessageSender` is an ordered queue
behind the stdio transport, so wire order is enqueue order; and the observer task is
created while `_run_request` is still on the ready queue, ahead of anything the loop's
next poll could schedule. Enqueueing without suspending therefore puts the announcement
in front of everything the pipelined request produces. The ordering stops being a race.

The stash is **one-shot and private to that door**. `announce_commands` never reads it, so
a re-announcement always rebuilds; a stash that could be consumed by the general door
would pin the palette to whatever the session started with. If nothing was prepared —
the executor has no listing, or its `tools/list` failed — `announce_prepared_commands`
falls back to building, losing the ordering guarantee rather than the palette.

Two properties make the announcement safe to lay on a working session:

- **Optional on the executor.** `TurnExecutor` declares `available_commands`, but an
  executor is swappable (D3) and one written before this existed announces nothing rather
  than raising. Read with `getattr`, like `session_modes`.
- **Never fatal.** A listing that fails is logged and swallowed — in `_prepare_commands`
  too, where it now runs *inside* `session/new` and so has a session to cost. It is a
  convenience laid on a session that is already open; turning a `tools/list` that timed
  out into a failed `session/new` would cost the client its session over a palette.

What it still cannot carry is *richer* data: `AvailableCommand` is a flat
`{name, description, hint}` with no server grouping and no MCP input schema. `pyacp-mth`
holds the extension request a client would need to ask for more than that.

## Paths are validated here and nowhere else

`cwd` and `additionalDirectories` must be absolute — `-32602` otherwise — and are stored
lexically tidied. Every method that carries them validates them: `session/new`,
`session/fork`, and also `session/resume` and `session/load`, which receive a `cwd` they
deliberately do **not** apply. Accepting a relative path there and silently ignoring it
would tell a client its path was fine when it was both invalid and unused.

Validation runs *before* `SessionRegistry.create`, so a refused request leaves nothing
behind.

The containment rule those roots define — and why it resolves symlinks on both sides —
is [paths.py](paths.md). Phase 4.2's `fs/*` calls are its first consumer.

## `session/new` refuses what `initialize` did not advertise

`mcpCapabilities.http`, `.sse`, and `.acp` are all `false` in
[capabilities.py](capabilities.md), and stdio needs no capability at all. Accepting an
`HttpMcpServer` anyway would make the advertisement a lie and hand back a session whose
tools silently do not exist, so a well-formed entry of an unadvertised transport is a
`-32602`. Spawning the stdio ones is `pyacp-db3`'s; refusing the rest could not wait for
it, because the wrong answer is silent.

The stdio entries that survive are handed to [mcp_registry.py](mcp_registry.md), which
spawns and handshakes one subprocess per server. **Opening is all-or-nothing and takes
the session with it**: if any server fails to come up, the session created a line earlier
is closed before the error propagates, because handing back an id whose tools silently do
not exist is the failure this whole path avoids.

**A session's backends are also told whether they may ask the human a question.**
`_elicit_for` reads `Gate.ELICITATION_FORM` *now*, when the subprocesses are spawned,
because that is when the MCP capability block — a promise — is written. A forwarder means
the backends are told they may send `elicitation/create`; `None` means they never are.
`session/fork` builds the child its own, because a forwarder carries the session id it
will put on the wire. What the forwarder reads *later* is whoever is connected then, and
[elicitation.md](elicitation.md) records why those can differ.

### A reloaded catalogue reaches a session at its next request

`SIGHUP` re-reads `--mcp-config` and swaps the entries into the catalogue object every
per-connection agent holds ([mcp_catalogue.md](mcp_catalogue.md)). What that *means* for a
session already open is decided here, in `_reconcile_catalogue`, and the first decision is
**when**.

**Not at the signal.** A reload has no client to notify. The WebSocket transport builds one
`PythonAcpAgent` per connection, `announce_config_options` sends through `self.client`, and
nothing maps a session to the connection that cares about it — a `Session` holds no client
handle, and it cannot, because a session outlives connections and can be resumed from a
different one. A sweep at signal time would therefore have to push
`config_option_update` down *some* connection, and every choice is wrong: telling a client
about a session it never touched is worse than telling it late.

So the reconcile runs where a client is definitely present and definitely the right one:

| Where | Why there |
|---|---|
| `session/prompt`, before the turn task exists | The turn then runs against what the operator has deployed. It is also the only ordering that works — a turn holds its backend map for its whole life, so a server added underneath it would be invisible to it |
| `session/resume` | A client picking a session back up, and it happens before the response hands over the modes and options, so what it reads is already current |

A session nobody is using has no observable state to be stale, so "late" costs nothing.
The common path is an integer comparison: `McpCatalogue.generation` against the generation
this session was last reconciled to. A session created after a reload is born current.

**A turn already running leaves the session stale on purpose**, to be retried at the next
request. Changing a session's backends under a turn is the broken pipe `_select_mcp_server`
refuses, for the same reason and with the same alternative.

#### The four cases

| Case | Answer | Why |
|---|---|---|
| An entry was **added** | The session gains the toggle, **off** — whatever `enabled` says | `enabled` means "a *new* session starts with it on": a statement about session creation. Honouring it here spawns a subprocess for a session that never asked |
| An entry was **removed** while the session had it on | Tear the server down, drop the toggle | Leaving it leaves an `mcp/*` option `entry_for_config_id` can no longer resolve, so switching it back on quietly becomes a stored flag instead of an action — a worse failure than the teardown, and a silent one |
| An entry **changed** while the session had it on | Respawn from the new recipe | A subprocess that no longer matches the catalogue is the drift "read once, at startup" existed to prevent, and waiting for the next session means a session open for a week never gets the operator's fix. Compared against `backends.specs`, because a toggle records *that* a server is on and never *what* it is |
| The file was **invalid** | Never reaches here | `cli.py` calls `replace` only once `load` has returned, so a broken file leaves the running catalogue untouched and there is nothing to reconcile |

An entry that did not change is **not** respawned — the regression that would make every
reload expensive and visible.

**A spawn that fails costs the toggle, not the session.** The option goes off, the failure
is logged for the operator, and the client's request succeeds. The client did not ask for
this; failing its prompt because a recipe it never wrote is broken bills the wrong party.

### An operator can refuse client-supplied servers entirely

`_reject_unsupported_mcp_servers` is the one funnel every client-supplied server list goes
through, on both `session/new` and `session/fork`, and it now makes **two** refusals. The
operator's comes first, because it is the operative reason: telling a client its
`HttpMcpServer` is the wrong transport, when the answer would have been no for a stdio one
too, sends it to fix the wrong thing.

By default `mcpServers` is accepted from anyone, which is right for the topology ACP was
designed around — over stdio the client *spawned* this process, and a parent configuring
its own child is the canonical arrangement. It is wrong for a long-lived socket clients
connect to afterwards, where `command` and `args` from a client past the access key is a
request to execute an arbitrary binary. `--no-client-mcp-servers` is the opt-in;
[cli.md](cli.md) carries why it is a flag rather than a default and why it is honoured on
both transports.

It **refuses** rather than dropping the list, which is the opposite of what the next
section describes ACP itself doing — deliberately. A silent drop is the protocol's own
posture for an entry it cannot parse; a *policy* refusal that behaved the same way would
hand back a session backed by fewer servers than were asked for while the operator
believed a door was shut. An empty list is not a refusal: it is exactly what a
catalogue-only client sends.

Both callers run the funnel **before** creating anything, so a refused request leaves no
session and no subprocess behind.

Note that the transport half of that method is unreachable over the wire: an `http` or
`sse` entry is dropped by `skip-invalid-items` before dispatch — see the next section —
so the agent never sees one. It stands for a direct caller, and for a schema that one day
models a transport this agent does not advertise.

### A dropped `mcpServers` entry is accepted, and that is a decision

`NewSessionRequest.mcp_servers` carries a `skip_invalid_items` wrap validator, so an entry
that fails validation is removed from the list **before the agent is called**. All four of
`name`, `command`, `args`, and `env` are required with no defaults, so
`{"name": "tools", "command": "/bin/echo"}` — which looks perfectly reasonable — is
dropped. The client gets a session backed by fewer servers than it asked for, with no
error.

`pyacp-mej` weighed refusing it and decided **not to**. The reasoning, because it is not
the obvious answer:

**This is ACP's own rule, not an SDK quirk.** `skip_invalid_items` restores the schema's
`x-deserialize-skip-invalid-items` annotation, and the schema carries it on **35 fields** —
`mcpServers` on all four session methods, plus `additionalDirectories`, `availableModes`,
`configOptions`, `content`, `locations`, `authMethods`, and more — beside 84 fields marked
`x-deserialize-default-on-error`. Taken together that is a deliberate protocol-wide
posture: *drop what you cannot parse rather than failing the message*. Refusing here would
not close a gap; it would be one agent opting out of that rule on one field, and doing it
consistently would mean opting out on all 35.

**And there is nowhere to do it anyway.** The agent is handed the survivors and never
learns what was sent — `acp.router` validates inside its own wrapper, so no ACP method can
see raw params. The only place holding the original dict is `transport_ws.receive()`,
which would have to know which method carries which list, and would fix nothing on stdio,
where the SDK owns the read loop. A bug fixed on one transport and not the other is worse
than a documented one, because the behaviour then depends on how a client connected.

**What is done instead.** The one place the client feels it — naming an absent server in a
prompt — no longer reads like the client's own typo:
[turn_mcp_router.py](turn_mcp_router.md)'s refusal names the dropped-entry possibility and
the four fields an entry needs. That is the whole of what this side can do, and it is done.

Pinned by `test_a_malformed_mcp_server_entry_is_dropped_not_refused` and
`test_an_entry_missing_only_args_and_env_is_dropped_too`, which stay so that an SDK bump
that stops salvaging re-opens the decision deliberately rather than by surprise.

## Method surface

All 15 `acp.interfaces.Agent` members are present. Nothing is declined — see
[docs/acp-compliance-matrix.md](../../docs/acp-compliance-matrix.md) for why, per
member.

| Member | Wire method | State today | Filled in by |
|---|---|---|---|
| `initialize` | `initialize` | **live** — negotiates the version, stores `clientCapabilities`, returns the capability block from [capabilities.py](capabilities.md) | — |
| `authenticate` | `authenticate` | **live** — refuses with `-32000 auth_required` | `pyacp-fln.1` |
| `cancel` | `session/cancel` | **live** — cancels the session's running turn; silent for an unknown session and for an idle one | `pyacp-hnk.5` |
| `ext_notification` | `_<name>` | **live** — silent by contract | — |
| `on_connect` | — | **live** — stores the `Client` facade | — |
| `ext_method` | `_<name>` | `-32601`, and **stays** that way — `pyacp-sld.2` declined to move the legacy MCP passthrough here | — |
| `new_session` | `session/new` | **live** — registers a session with the executor's modes, opens its MCP servers with their roots and elicitation forwarder, rejects the transports `initialize` did not advertise | — |
| `prompt` | `session/prompt` | **live** — runs a turn as a task and returns its `stopReason` | `pyacp-hnk.2` |
| `load_session` | `session/load` | **live** — replays the session's transcript, then returns its settings | — |
| `list_sessions` | `session/list` | **live** — one keyset-paginated page, most recently active first | — |
| `close_session` | `session/close` | **live** *(unstable-gated)* — cancels the turn, drops the session, releases its backends and its client terminals | — |
| `fork_session` | `session/fork` | **live** *(unstable-gated)* — deep copy under a new id, with its own MCP subprocesses | — |
| `resume_session` | `session/resume` | **live** *(unstable-gated)* — reattaches; deliberately does **not** replay | — |
| `set_session_mode` | `session/set_mode` | **live** — switches the mode and emits `current_mode_update` | — |
| `set_config_option` | `session/set_config_option` | **live** — one implementation for both request shapes; emits `config_option_update` | — |

`_not_implemented` returns exactly what the router produces for an absent attribute, so
a later phase changes a body and nothing else; the wire behaviour before it does is
already correct.

**Every request member carries `@as_request_error`**, including the ones whose bodies
are still `-32601`. That is not defensive: `acp.Connection._run_request` catches a
non-`RequestError` and answers a bare `-32603`, so an `MCPProtocolError` escaping one of
these methods would arrive at the client with the backend's code destroyed, and a
`ValueError` would arrive as `-32603` instead of `-32602`. The mapping has to happen on
our side of that boundary, and putting it there now is what keeps a later phase from
having to remember. The decorator lives on the function, so it is replaced by an
override — these bodies get filled in *in place*. See [errors.py](errors.md).

`cancel` and `ext_notification` are **not** decorated. A notification has no reply
channel, so there is nowhere to put a mapped error and raising at all is already the
bug.

**`authenticate` is a refusal, not a stub.** `initialize` advertises no auth methods, so
every `methodId` is one we never offered. `-32000 auth_required` says "the method exists,
the credentials do not"; `-32601` would say the opposite.

## `initialize` does three things

**Negotiates the version.** `capabilities.negotiate_protocol_version` echoes a version
we serve and answers with our newest when the client asked for one we do not. This is
not a rejection point — the client reads the answer and decides whether to disconnect —
so an unsupported version is logged, not raised.

**Stores `clientCapabilities`.** Whatever the client declared is kept for the life of
the connection and read back through `PythonAcpAgent.client_capabilities`. Phase 4 gates
every `fs/*`, `terminal/*`, and `elicitation/*` call on it; a call made without checking
is a conformance bug the client is entitled to answer `-32601` to. The one gate read
outside a turn is `ELICITATION_FORM`, at `session/new` — see below. `None` means the
handshake has not happened and is deliberately not collapsed with a `ClientCapabilities`
that declares nothing.

*Per-connection* means per instance: **one `PythonAcpAgent` serves one connection.**
`on_connect` stores that connection's `Client` facade on the same object, so the two
have the same lifetime by construction. A transport that binds one agent to several
connections would break both, which is why `cli.py` constructs the agent at the point it
binds it.

**Returns the capability block.** Not assembled here —
[capabilities.py](capabilities.md) owns it, so a literal cannot be flipped on without a
manifest row and a test proving the feature it advertises actually runs. Everything is
`false`/`null` today because Phase 1 implements no features; the owner of each flip is
in that module's table.

`PROTOCOL_VERSION` is the **ACP** version, an integer. It is unrelated to
`MCPStdioClient`'s MCP `protocolVersion` string. Two protocols, two version fields.

## Main symbols

| Symbol | Purpose |
|---|---|
| `PythonAcpAgent` | The `acp.interfaces.Agent` implementation |
| `PythonAcpAgent.client` | The connected `Client` facade; raises `RuntimeError` before `on_connect` |
| `PythonAcpAgent.connected_client` | The same facade or `None` — the non-raising form, for cleanup after a connection ends |
| `PythonAcpAgent.terminals` | The process-wide [`TerminalRegistry`](terminals.md) this agent's turns create through |
| `PythonAcpAgent.client_capabilities` | What the client declared at `initialize`, or `None` before it ran — Phase 4 gates every client call on this |
| `PythonAcpAgent(..., accept_client_servers=)` | Whether a client may supply MCP server recipes. `True` by default; `cli.py`'s `--no-client-mcp-servers` sets it false |
| `PythonAcpAgent._reconcile_catalogue(session)` | Brings one open session into line with a reloaded catalogue, at that session's own next request |

`client_capabilities` distinguishes `None` (no `initialize` yet) from "declared nothing";
the two are deliberately not collapsed.

## Wire-shape gotcha

`mcpServers` is **required** on `session/new` and `session/load` — `NewSessionRequest`
and `LoadSessionRequest` give it no default, even though the `Agent` Protocol's signature
does. The router always passes it, so the Python-side default never applies.

## Tests

`tests/test_agent.py` drives the agent through `build_agent_router` rather than calling
its methods directly. The contract under test is "the SDK can dispatch to this object",
and a signature the router cannot splat into is exactly the failure a direct call would
hide. The unstable-gated methods are tested in **both** directions — reachable with the
flag, `-32601` without it — because the second is what catches a connection built wrong.
