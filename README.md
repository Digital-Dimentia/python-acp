# python-acp

`python-acp` is a Python Agent Client Protocol (ACP) bridge that connects to an MCP server over stdio and exposes MCP capabilities through a WebSocket API for local automation and tooling.

## Features

- Serves ACP over **stdio** (how an editor spawns an agent) and over **WebSocket**, both
  binding the same agent through the `agent-client-protocol` SDK.
- Runs the MCP servers a client names in `session/new`, one set per session, over stdio.
- Routes each prompt block to a tool call and streams the result back as
  `session/update` — no LLM anywhere in the runtime.
- Asks the client's permission before every tool call, reads and writes files through the
  client, and runs commands in the client's terminals.
- Works with a repo-local virtual environment.
- Includes a `Containerfile` for containerized runs.
- Ships with a Makefile for local build, test, lint, packaging, and release-bundle generation.
- Publishes release artifacts via GitHub Actions.

## Architecture docs

- [System architecture](ARCHITECTURE.md)
- Module docs:
  - [agent.py](src/python_acp/agent.md)
  - [announcer.py](src/python_acp/announcer.md)
  - [capabilities.py](src/python_acp/capabilities.md)
  - [errors.py](src/python_acp/errors.md)
  - [paths.py](src/python_acp/paths.md)
  - [sessions.py](src/python_acp/sessions.md)
  - [terminals.py](src/python_acp/terminals.md)
  - [turns.py](src/python_acp/turns.md)
  - [turn_mcp_router.py](src/python_acp/turn_mcp_router.md)
  - [cli.py](src/python_acp/cli.md)
  - [commands.py](src/python_acp/commands.md)
  - [elicitation.py](src/python_acp/elicitation.md)
  - [mcp_catalogue.py](src/python_acp/mcp_catalogue.md)
  - [mcp_content.py](src/python_acp/mcp_content.md)
  - [mcp_registry.py](src/python_acp/mcp_registry.md)
  - [mcp_stdio.py](src/python_acp/mcp_stdio.md)
  - [mcp_tools.py](src/python_acp/mcp_tools.md)
  - [transport_stdio.py](src/python_acp/transport_stdio.md)
  - [transport_ws.py](src/python_acp/transport_ws.md)
- [ACP conformance suite](tests/test_conformance.py) — the compliance matrix, executable.
- [Changelog](CHANGELOG.md) — what changed per release, and the upgrade path off `0.1.x`.
- [Statistics](STATISTICS.md) — lines, modules, classes, functions, and tests. Generated
  by `make stats`; counted on the AST rather than with `grep`.
- Design docs — the plan is delivered, so these record decisions and their reasons,
  including what was declined:
  - [ACP v1 plan](docs/full-apc-plan.md)
  - [ACP v1 compliance matrix](docs/acp-compliance-matrix.md)
  - [Interop runbook](docs/interop.md)
  - [Module boundaries](docs/module-boundaries.md)

## Local setup

This project uses a repo-local virtual environment in `.venv`, provisioned by the
Makefile. `.venv` is the canonical directory; an older `venv/` from a previous checkout
is renamed to it on the first `make venv`.

```bash
make venv                 # create .venv and install the project with its dev extras
source .venv/bin/activate # optional; every make target uses .venv/bin/python directly
```

`make venv` stamps the environment with the interpreter it used and a hash of
`pyproject.toml`. While that stamp is current, `make lint`, `make test`, and `make build`
skip `pip` and need no network. `make sync` forces a reinstall.

Useful overrides:

```bash
make venv PYTHON=python3.12 VENV_DIR=.venv312   # a second env on another interpreter
make lint VENV_DIR=.venv312                     # ...and run against it
make venv OFFLINE=1                             # fail rather than touch the network
make venv PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org"  # TLS-intercepting proxy
```

`PIP_TRUSTED_HOST` is empty by default; the default build path never relaxes TLS
verification. Prefer `PIP_CERT=/path/to/proxy-ca.pem` when you have the proxy's CA.

## Run the bridge

```bash
python-acp --host 127.0.0.1 --port 8765
```

**There is no flag for an MCP server, and that is the design.** A client names the servers
it wants in `session/new`, and they live and die with that session — see
[Sessions bring their own MCP servers](#sessions-bring-their-own-mcp-servers) below.
`--mcp-command` used to start a process-wide one for the deprecated action surface; both
were removed in the ACP v1 migration, and passing the flag now fails at startup rather
than being ignored.

### As an ACP agent over stdio

`--transport stdio` speaks ACP on the process's own stdin and stdout, which is how an
editor spawns an agent. It is not run by hand — the client launches it:

```bash
python-acp --transport stdio
```

`--host` and `--port` are ignored in this mode, **stdout carries the JSON-RPC wire and
nothing else**, and all diagnostics go to stderr.

From a checkout, `make stdio` is the same thing through the repo venv — `DEBUG=1` adds
`--debug`, `LOG=1` tees the diagnostics to `logs/python-acp-ws.log`:

```bash
make stdio DEBUG=1
```

`run`, `debug` and `stdio` can all be started from any working directory — they chdir to
the Makefile's own directory before doing anything, so a program that launches the agent
can point at the file from wherever it happens to be:

```bash
make -f /path/to/python-acp/Makefile stdio
```

The agent serves `initialize`, the full session lifecycle (`new`, `prompt`, `cancel`,
`load`, `list`, `fork`, `resume`, `close`), and refuses `authenticate`.
**Every routed ACP method is implemented** — nothing answers `-32601` any more except a
method the SDK does not route at all.
See [agent.py](src/python_acp/agent.md) for the per-method state and
[transport_stdio.py](src/python_acp/transport_stdio.md) for the binding.

### As an ACP agent over WebSocket

`--transport ws` serves **the same agent**. A WebSocket client that sends ACP JSON-RPC
gets the same `initialize` negotiation, the same capability block, and the same error
codes a stdio client gets:

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
```

`ws` remains the default transport, though the two now carry exactly the same agent —
same negotiation, same capability block, same methods, same error codes. That sameness is
the reason the default has not moved: `v0.1.0` and `v0.1.1` shipped WebSocket-only, with
no `--transport` flag at all, so every released invocation binds a socket. Changing the
default would take that away for no capability gain. `--transport stdio` is one flag
away and fully supported. See [cli.py](src/python_acp/cli.md) for the full decision and
[transport_ws.py](src/python_acp/transport_ws.md) for the binding.

### Securing the WebSocket

**Read this before binding anything but loopback.** `session/new` takes a `command` and
`args` and spawns them, so whoever can open this socket can run programs as the user
running the bridge. On `127.0.0.1` that is the design — the client is you. Anywhere else
it is a remote shell with no password.

Set a shared key in the **environment**, and have the client present it in the URL:

```bash
PYTHON_ACP_WS_KEY=$(openssl rand -hex 32) python-acp --host 0.0.0.0
```

```
ws://your-host:8765/?key=<the same secret>
```

A client that presents no key, the wrong key, or two keys is refused with **`401` during
the opening handshake** — it never reaches `initialize`, so it never becomes an ACP
connection at all.

The variable is read from the environment and there is **no `--ws-key` flag**, because
`argv` is world-readable through `ps`: a flag would publish the secret to every other user
of the machine at the moment it is used to protect it. An empty value (`PYTHON_ACP_WS_KEY=`)
reads as unset.

**Binding a non-loopback host with no key refuses to start:**

```
$ python-acp --host 0.0.0.0
refusing to bind 0.0.0.0 without an access key: session/new runs commands named by the
client, so an unauthenticated socket off loopback is remote code execution. Set
PYTHON_ACP_WS_KEY=<secret> and connect to ws://…/?key=<secret>, or set
PYTHON_ACP_WS_ALLOW_UNAUTHENTICATED=1 to accept the risk.
$ echo $?
2
```

Loopback with no key is unchanged, so every local workflow keeps working untouched.

#### What the key does not do

It is a floor, not a security model. Know these before putting it anywhere interesting:

- **There is no TLS in this process.** The key travels in the request URL, readable by
  anything on the path and written down in proxy and server access logs. Put a reverse
  proxy or an SSH tunnel in front of it for anything beyond a trusted network.
- **One key, no identity.** Every client shares it, so there is no per-client revocation
  and nothing to attribute a session to — and `session/list` shows every client every
  session. Rotating means a restart.
- **A query parameter is the wrong carrier on principle.** `Authorization` would keep the
  secret out of the URL. It is a query parameter because that is the one thing every
  WebSocket client library can send without custom header support, and a key nobody can
  present protects nothing.

Note that this is *transport* admission control and not ACP's `authenticate`, which is why
`initialize` still advertises no auth methods: that field is about the agent presenting a
credential, and it has none. See [transport_ws.py](src/python_acp/transport_ws.md).

### Sessions bring their own MCP servers

`session/new` names the MCP servers that session should talk to. Each gets its own
subprocess, spawned and handshaked before the response returns, and torn down when the
session closes:

```json
{"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {
  "cwd": "/absolute/path",
  "mcpServers": [
    {"name": "tools", "command": "python", "args": ["my_mcp_server.py"], "env": []}
  ]
}}
```

`env` is added on top of the agent's own environment rather than replacing it.

> **All four of `name`, `command`, `args`, and `env` are required, and an entry missing
> any of them is dropped before this agent ever sees it** — including one as plausible as
> `{"name": "tools", "command": "/bin/echo"}`. You get a `sessionId` back, no error, and a
> session backed by fewer servers than you asked for.
>
> That is ACP's own rule rather than a choice made here: the schema marks `mcpServers`
> `skip-invalid-items`, one of 35 fields it marks that way, so an unparseable entry is
> dropped instead of failing the whole message. The agent is handed the survivors and
> cannot tell that anything was removed, which is why it cannot warn you at `session/new`.
> It shows up when you name the missing server in a prompt, and the refusal says so.
>
> **Send all four fields, `args` and `env` as `[]` when there is nothing to put in them.**

### Or the agent brings them, and you select

`session/new`'s `mcpServers` assumes ACP's canonical topology: a client that **spawned**
this agent, and so already holds the MCP configuration. When python-acp is a long-lived
WebSocket server instead, that is backwards — the operator configured the deployment, not
the client — and it means accepting a request to execute an arbitrary binary from anyone
past the access key.

So the operator can start the agent with a catalogue and let clients pick from it:

```bash
python-acp --mcp-config servers.toml
```

```toml
[servers.tools]
command = "python"
args = ["my_mcp_server.py"]
env = { LOG = "debug" }
description = "Local demo tools"   # shown beside the toggle
enabled = true                     # whether a new session starts with it on
```

JSON works too, including the `{"mcpServers": {...}}` shape editors already write, so an
existing config can be pasted rather than translated.

Each entry becomes one boolean `configOption` — `mcp/tools` above — on every session. No
extension method and nothing new to learn: they arrive in `NewSessionResponse.configOptions`
like any other option, `session/set_config_option` changes them, and `config_option_update`
announces a change. ACP's `select` variant is single-choice, so a set of booleans is what
a multi-select looks like here.

**The two sources are additive.** A client that knows its own servers keeps naming them in
`session/new`; a thin client selects from the catalogue; one session can have both. A name
used by both is `-32602`. Everything else is unchanged: each session still spawns its own
subprocesses, and they still die with it. See
[mcp_catalogue.py](src/python_acp/mcp_catalogue.md).

Only **stdio** servers are accepted. `initialize` advertises
`mcpCapabilities: {http: false, sse: false, acp: false}`, so an `http` or `sse` entry is
refused with `-32602` rather than accepted and quietly ignored. If any server fails to
start, the whole `session/new` fails — a session id whose tools do not exist would be
worse than an error.

### Running a tool

`session/prompt` is served by a **deterministic MCP tool-router** — there is no LLM in
this runtime, so a prompt does not get interpreted, it gets *routed*.

A prompt is either one of seven typed commands or, as it has always been, a JSON object
per text block.

### Commands for a person

All seven are announced in `available_commands`, so a client's slash palette offers them
without being taught, and each answers with plain multi-line text. The leading slash is
optional on input.

MCP servers publish three kinds of thing, and there is a command family for each.

**Tools** — what the server can *do*.

```
/tools
/invokeTool demo/echo --text "hello world" --count 3
```

`/tools` lists every tool with its parameters, types, which are required, and an example
call built from what this session actually has. It runs nothing.

`/invokeTool` calls one. Values are typed from the tool's own `inputSchema` — a `string`
parameter given `3` stays `"3"` — and a parameter the schema does not declare is refused
with the list of ones it takes, rather than forwarded to fail server-side. A typed call
builds the same invocation the JSON form builds, so it inherits the session mode, the
permission prompt, and the on-tool-failure policy identically.

**Prompts** — reusable messages the server templates for a model.

```
/listPrompts
/promptShow demo/greeting --name "Ada Lovelace"
/promptInvoke demo/greeting --name "Ada Lovelace"
```

`/listPrompts` lists them with their arguments. `/promptShow` expands one and shows the
messages that come back: the substitution is the *server's* work, so it needs no model and
works here.

`/promptInvoke` is the one command that does not work yet. Acting on an expanded prompt is
what needs a model, and this bridge has none — so it validates the arguments, refuses with
the reason, and hands back the `/promptShow` that runs. It is announced rather than
omitted so a client discovers the boundary instead of an absence.

A prompt argument is always a string: MCP types them as `{[key: string]: string}`, so
there is no `inputSchema` and no coercion. An argument the prompt does not declare, and a
missing required one, are both refused by name before the server is asked.

**Resources** — data the server can hand over.

```
/listResources
/resourceShow demo greeting://ada
```

`/listResources` shows uri, name and mime type. `/resourceShow` reads one. Text comes back
verbatim; a binary resource is reported as `[binary, about N bytes, not shown]` rather
than pasted into the transcript as base64.

The server may be omitted from any of these when the session opened exactly one. Note the
shape difference: a prompt is `demo/greeting`, one token split on the slash, while a
resource is `demo greeting://ada`, two tokens — a URI is full of slashes and cannot be
carved out of a pair.

A server that declared no `prompts` or `resources` capability in its MCP handshake is
named in the listing and not asked, rather than producing a `-32601` about a method nobody
typed.

See [commands.py](src/python_acp/commands.md).

### The JSON form

Each text content block is a JSON object naming an MCP tool:

```json
{"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
  "sessionId": "...",
  "prompt": [
    {"type": "text", "text": "{\"tool\": \"echo\", \"arguments\": {\"text\": \"hi\"}}"}
  ]
}}
```

| Field | Required | Meaning |
|---|---|---|
| `tool` | yes | The MCP tool name |
| `arguments` | no, defaults to `{}` | Passed to `tools/call` unchanged |
| `server` | only when the session opened more than one MCP server | Which server from `session/new`'s `mcpServers` |
| `read` | no | `{"<argument>": {"path": "/abs", "line": 1, "limit": 40}}` — files to read into arguments |
| `write` | no | `{"path": "/abs/out.txt"}` — where the tool's text output goes |
| `run` | no | `{"<argument>": {"command": "git", "args": ["log"]}}` — commands to run into arguments |

Each text block is one call, run in order, and returns `stopReason: "end_turn"`. The turn
streams, in this order:

1. `user_message_chunk` — the prompt, echoed, so a reloaded session shows both halves;
2. `available_commands_update` — the session's MCP tools, **including on a refusal**, so
   a client is told what it could have called;
3. `plan` — the whole plan up front, then re-emitted with statuses advanced after each
   call. Only when the client advertised `clientCapabilities.plan`;
4. `tool_call` and `tool_call_update` — real `pending` → `in_progress` →
   `completed`/`failed` transitions carrying the tool's own output.

**One `available_commands_update` arrives before any of that**, on every path that opens
a session — `session/new`, `session/fork`, `session/load` and `session/resume` — so a
client can populate a command palette without first taking a turn. On the two that *mint*
an id it follows the response rather than preceding it, because the response is where the
client learns the id; [announcer.py](src/python_acp/announcer.md) is the hook that makes
that ordering a guarantee.

`agent_thought_chunk` and `usage_update` are never sent: there is no LLM here, so there
is no reasoning trace and no token count to report. The full disposition of every
`session/update` variant is in [turns.py](src/python_acp/turns.md).

**Three `stopReason`s, and only three.** A turn ends `end_turn`, `refusal`, or
`cancelled`; `max_tokens` and `max_turn_requests` are limits on a model, and there is
none here to limit. A backend failure is not a `stopReason` at all — it is a JSON-RPC
error carrying the MCP server's own code. The table, with the reason for each, is in
[turns.py](src/python_acp/turns.md).

`session/cancel` ends the running turn with `stopReason: "cancelled"` — a response, never
a raise and never a hang — and no `session/update` for that turn arrives after it. An MCP
call still in flight is **un-asked** with `notifications/cancelled` rather than left
running, so the server stops computing a reply nobody will read.

**Only text blocks are read.** An `image`, `audio`, `resource`, or `resource_link` block
is declined by name, because each is context for a model to reason over and there is no
model here. `initialize` says so: `promptCapabilities` reports `image`, `audio`, and
`embeddedContext` all `false`, and those literals are *derived* from what the turn
executor declares it reads, so the advertisement cannot drift from the behaviour.

A prompt that is not an invocation — prose, malformed JSON, an empty prompt — is answered
with `stopReason: "refusal"` and an `agent_message_chunk` explaining the convention. It is
not an error, and **nothing runs**: the whole prompt is parsed before the first tool, so a
malformed third block does not leave two side effects behind.

### Reading and writing files

`read` and `write` go through **your** client, never through this process:
`fs/read_text_file` and `fs/write_text_file` are ACP *client* methods, and this agent
calls them so you stay in control of what is read and written.

```json
{"tool": "summarise",
 "read": {"document": {"path": "/abs/notes.md", "line": 1, "limit": 40}},
 "write": {"path": "/abs/summary.md"}}
```

- **Gated on what you advertised at `initialize`.** `clientCapabilities.fs.readTextFile`
  and `.writeTextFile` are two independent booleans — a read grant does not permit a
  write. A prompt asking for something you did not advertise is answered with
  `stopReason: "refusal"` naming the missing capability, before anything runs. There is
  **no direct-filesystem fallback**; a client that offers no `fs` gets a refusal, not a
  file opened behind its back.
- **Both paths must be absolute and inside the session's `cwd` or `additionalDirectories`**,
  with symlinks followed on both sides. Anything else is refused, and the resolved path is
  what your client is asked for.
- `line` is 1-based and `limit` is a line count; both are optional, and omitting them
  reads the whole file.
- **The write is skipped, and says so, when the tool failed or returned no text** — an
  error message is not the document you asked for, and an empty file is a truncation.
- **A client that errors on `fs/*` fails that one call, not the turn.** Its
  `tool_call_update` carries your own code and message, and the remaining calls still run.

`ToolCall.locations` carries the resolved paths, so a client can show or follow which
files a call is about.

### Running commands

`run` goes through **your** terminals, never through this process: `terminal/create`,
`/wait_for_exit`, `/output`, `/kill`, and `/release` are ACP *client* methods, and this
agent calls them so the command runs where you can see it.

```json
{"tool": "summarise",
 "run": {"log": {"command": "git", "args": ["log", "--oneline", "-5"],
                 "cwd": "/abs/repo", "env": {"TZ": "UTC"}, "outputByteLimit": 65536}}}
```

- **Gated on `clientCapabilities.terminal`**, which is one boolean covering all five
  methods — there is no per-method granularity in the protocol. A prompt asking to run a
  command without it is answered with `stopReason: "refusal"`, before anything runs.
- **Permission comes first.** Nothing is started until you approve that tool call.
- **`outputByteLimit` is always sent**, defaulting to 1 MiB. The captured output becomes
  an MCP tool argument, so it has to fit through that request; unbounded output is a
  failure mode with no error message attached. Name your own limit if 1 MiB is wrong for
  your command. When your client truncates, the tool call's content says so.
- **`cwd` defaults to the session's** and must be absolute and inside the session's roots
  when you name one.
- **The terminal is always released** — on completion, on failure, on cancellation, and on
  `session/close`. The one thing this agent cannot do is release a terminal after *you*
  disconnect: the release is a request, and the connection that would carry it is gone. It
  drops its handles and says so rather than pretending. See
  [terminals.md](src/python_acp/terminals.md).
- **A command that exits non-zero means the tool is not called**, and the failure names
  the exit status. Its output was going to *be* an argument, and inventing one is worse.

### Session modes

`session/new` advertises three modes and `session/set_mode` switches between them. Each
changes what a turn does:

| Mode | Runs tools | Asks permission |
|---|---|---|
| `execute` *(default)* | yes | yes, per call |
| `dry-run` | no — reports what *would* run, with arguments | no |
| `auto-approve` | yes | no; choosing the mode is the consent |

A change is announced with a `current_mode_update` notification, including when the
client is the one that asked — so a second client on the same session stays in step.

### Session config options

`session/set_config_option` handles both the boolean and select request shapes:

| Option | Type | Default | Effect |
|---|---|---|---|
| `announce-tools` | boolean | `true` | List the session's MCP tools each turn. Off saves the notification, not every `tools/list` — a turn still lists the servers it calls, because a tool call's `kind` comes from their annotations |
| `on-tool-failure` | select | `continue` | `continue` runs the remaining calls; `stop` ends the turn at the failed one |
| `mcp/<name>` | boolean | the catalogue's | Whether this session talks to the catalogue server `<name>` — see below. One per configured server, and only when the agent was started with `--mcp-config` |

A change is announced with `config_option_update`, carrying **every** option rather than
the changed one — which is what a client re-rendering a settings panel wants.

An `mcp/<name>` option is an **action**, not a stored flag: setting it spawns or tears
down that server for the session, and the palette is re-announced afterwards so what you
can call follows what you selected. It is refused while a turn is running — closing a
backend under a live tool call would turn it into a broken pipe — so cancel or wait. A
spawn that fails leaves the option `false` and the session otherwise untouched.

**Every tool call asks the client for permission first**, via
`session/request_permission`. A server's tool annotations set the call's `kind`, so the
prompt can say "delete" rather than "other" — but they never decide *whether* the question
is asked. A hint asserted by the party being restrained is not consent, and MCP says so
itself: annotations are not guaranteed to describe behaviour faithfully. Treating every
call as consequential is the only setting that cannot silently do damage, and the "for
session" options keep it to once per tool. See
[mcp_tools.py](src/python_acp/mcp_tools.md).
Choosing a reject option marks that call `failed` and lets the rest of the turn continue;
cancelling the prompt ends the turn with `stopReason: "cancelled"`.

A tool that *fails* is not a failed turn. MCP reports tool failure as a successful result
carrying `isError`, so the call's update says `status: "failed"` with the tool's own
output, the remaining calls still run, and the turn ends normally.

See [turn_mcp_router.py](src/python_acp/turn_mcp_router.md).

## Failure responses

Two different things can go wrong, and they are reported differently.

**The request failed** — unknown tool, bad arguments, backend unreachable. The
MCP server's own JSON-RPC error code is forwarded rather than flattened, so
`-32601` (no such tool) stays distinguishable from `-32602` (bad arguments).
`data.source` marks that the code came from the MCP backend and not from the
bridge itself:

```json
{"jsonrpc": "2.0", "id": 1, "error": {
  "code": -32601,
  "message": "MCP error -32601: Unknown tool",
  "data": {"source": "mcp", "mcpCode": -32601}
}}
```

Failures with no server-assigned code — a timeout, a dead backend — keep `-32603`.
They carry no `data.source`; that key is present only when the code is the backend's.

Errors the bridge originates put a concise sentence in `message` and the specifics in
`data.reason`, matching what the ACP SDK produces on the stdio transport:

```json
{"jsonrpc": "2.0", "id": 1, "error": {
  "code": -32602,
  "message": "Invalid params",
  "data": {"reason": "'arguments' must be an object"}
}}
```

**The tool failed** — the call ran and the tool reported an error. MCP reports
this as a *successful* result carrying `isError: true`, and the bridge passes it
through that way so the content explaining the failure is not lost:

```json
{"jsonrpc": "2.0", "id": 2, "result": {
  "content": [{"type": "text", "text": "file not found"}],
  "isError": true
}}
```

Inside a turn the same failure becomes a `tool_call_update` with `status: "failed"`
carrying that content, and the turn still ends `end_turn` — the turn finished, one tool
did not. Set the `on-tool-failure` config option to `stop` if a failed tool should end
the turn instead.

## Make targets

```bash
make venv
make sync
make install
make lint
make docs-check
make test
make transcripts
make build
make wheel
make sdist
make container-image
make package
make release-bundle
make clean
make clean-venv
make distclean
```

**`make clean` no longer removes the virtual environment.** It takes build outputs and
tool caches; `make clean-venv` takes `$VENV_DIR` and nothing else, and `make distclean`
takes both. The venv is stamped and reused, so deleting it is the one action here that
forces a full reinstall over the network — and behind a TLS-intercepting proxy that may
not be recoverable without `PIP_TRUSTED_HOST` or `PIP_CERT`. A target called `clean`
should not be able to leave a checkout unbuildable offline.

`make docs-check` enforces three documentation invariants nothing else does: relative
links resolve, every Mermaid flowchart edge names a node its own block defines, and every
module under `src/python_acp/` has a sibling `.md`. It runs in CI.

`make transcripts` re-records the golden JSON-RPC transcripts in `tests/transcripts/` and
prints the diff; read it before committing, since an unreviewed regeneration turns a wire
regression into a committed expectation.

The release bundle includes the built Python artifacts and, when available, the exported container image archive.

## Container usage

```bash
podman build -t python-acp -f Containerfile .
podman run --rm -p 8765:8765 -e PYTHON_ACP_WS_KEY python-acp --host 0.0.0.0
```

Or with Docker:

```bash
docker build -t python-acp -f Containerfile .
docker run --rm -p 8765:8765 -e PYTHON_ACP_WS_KEY python-acp --host 0.0.0.0
```

Both assume `PYTHON_ACP_WS_KEY` is set in your shell — `-e VAR` with no value passes it
through without putting the secret in the command line, where `ps` and your shell history
would both keep it.

`--host 0.0.0.0` rather than the default `127.0.0.1`, which inside a container is
reachable only from inside it. That is also why the key is not optional here: `-p
8765:8765` publishes the port on **every** host interface, and without a key the container
refuses to start rather than serve an unauthenticated agent to the network. See
[Securing the WebSocket](#securing-the-websocket) for the threat model and what the key
does not cover.

### Raspberry Pi and other arm64 hosts

Released images are **multi-arch: `linux/amd64` and `linux/arm64`**. `podman pull` or
`podman load` picks the right one for the host, so nothing above changes on a Pi.

`linux/arm64` covers **Raspberry Pi 3, 4, 5 and Zero 2 W on 64-bit Raspberry Pi OS**,
the default since 2022. That includes the Pi 5: its Cortex-A76 is ARMv8.2-A, which is a
superset of ARMv8-A and runs an `arm64` image natively — there is no separate ARMv8.2
image to look for, and no OCI platform that would describe one.

Building on the Pi itself also works and needs no flags, since a native build already
targets the host:

```bash
podman build -t python-acp -f Containerfile .
```

To cross-build from an x86 machine, ask for the platform explicitly (this needs QEMU
registered on the builder):

```bash
make container-image PLATFORMS=linux/amd64,linux/arm64
```

Not supported: **32-bit Raspberry Pi OS** (`linux/arm/v7`) is not in the released
manifest, though it would work if added — open an issue. **Pi Zero and Pi 1** (ARMv6)
cannot be supported: no `python:3.11-slim` image is published for that architecture, and
`pydantic-core` ships no armv6 wheel, so it would require compiling Rust on an ARM11.

**The image contains the agent and nothing else** — `Containerfile` copies `src/` and
installs the package. It starts no MCP server, and a client's `session/new` names servers
that must be executable *inside the container*, so a useful image is normally this one
plus your own servers layered on top. (The example here used to pass
`--mcp-command python /app/mock_mcp_server.py`; that path was never copied into the image,
so it had been broken independently of the flag's removal.)

## CI/CD

- `.github/workflows/ci.yml`: lint, tests, and build validation.
- `.github/workflows/publish-artifacts.yml`: publishes Python wheel/sdist artifacts and the multi-arch (`linux/amd64` + `linux/arm64`) container image archive on a GitHub release.

## Notes

- Build outputs such as `dist/` and `artifacts/` are intentionally ignored by Git.
- The project is designed for local development and release packaging without requiring a hosted LLM or additional orchestration layer.
