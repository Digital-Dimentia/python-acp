# python-acp

`python-acp` is a Python Agent Client Protocol (ACP) bridge that connects to an MCP server over stdio and exposes MCP capabilities through a WebSocket API for local automation and tooling.

## Features

- Serves ACP over **stdio** (how an editor spawns an agent) and over **WebSocket**, both
  binding the same agent through the `agent-client-protocol` SDK.
- Connects to an MCP server over stdio.
- Initializes the server and forwards MCP messages.
- Exposes MCP tools, prompts, and resources through the deprecated WebSocket actions
  (every reply carries a `deprecated` block naming its replacement):
  - `list_tools`
  - `call_tool`
  - `list_prompts`
  - `get_prompt`
  - `list_resources`
  - `read_resource`
- Works with a repo-local virtual environment.
- Includes a `Containerfile` for containerized runs.
- Ships with a Makefile for local build, test, lint, packaging, and release-bundle generation.
- Publishes release artifacts via GitHub Actions.

## Architecture docs

- [System architecture](ARCHITECTURE.md)
- Module docs:
  - [agent.py](src/python_acp/agent.md)
  - [capabilities.py](src/python_acp/capabilities.md)
  - [errors.py](src/python_acp/errors.md)
  - [legacy_ws.py](src/python_acp/legacy_ws.md)
  - [paths.py](src/python_acp/paths.md)
  - [sessions.py](src/python_acp/sessions.md)
  - [terminals.py](src/python_acp/terminals.md)
  - [turns.py](src/python_acp/turns.md)
  - [turn_mcp_router.py](src/python_acp/turn_mcp_router.md)
  - [cli.py](src/python_acp/cli.md)
  - [elicitation.py](src/python_acp/elicitation.md)
  - [mcp_content.py](src/python_acp/mcp_content.md)
  - [mcp_registry.py](src/python_acp/mcp_registry.md)
  - [mcp_stdio.py](src/python_acp/mcp_stdio.md)
  - [mcp_tools.py](src/python_acp/mcp_tools.md)
  - [transport_stdio.py](src/python_acp/transport_stdio.md)
  - [transport_ws.py](src/python_acp/transport_ws.md)
- [ACP conformance suite](tests/test_conformance.py) — the compliance matrix, executable.
- Design docs (target state, not yet built):
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
python-acp --mcp-command python /path/to/your_mcp_server.py --host 127.0.0.1 --port 8765
```

`--mcp-command` starts a **process-wide** MCP server and is **optional**. ACP sessions
carry their own servers in `session/new`, so an agent does not need one; what still does
is the deprecated action surface below, which predates sessions.

### As an ACP agent over stdio

`--transport stdio` speaks ACP on the process's own stdin and stdout, which is how an
editor spawns an agent. It is not run by hand — the client launches it:

```bash
python-acp --transport stdio --mcp-command python /path/to/your_mcp_server.py
```

`--host` and `--port` are ignored in this mode, **stdout carries the JSON-RPC wire and
nothing else**, and all diagnostics go to stderr.

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

`ws` remains the default transport because it also carries the deprecated surface below,
which stdio never had. See [transport_ws.py](src/python_acp/transport_ws.md).

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

`name`, `command`, `args`, and `env` are all **required** by the schema — an entry
missing one is silently dropped before the agent sees it. `env` is added on top of the
agent's own environment rather than replacing it.

Only **stdio** servers are accepted. `initialize` advertises
`mcpCapabilities: {http: false, sse: false, acp: false}`, so an `http` or `sse` entry is
refused with `-32602` rather than accepted and quietly ignored. If any server fails to
start, the whole `session/new` fails — a session id whose tools do not exist would be
worse than an error.

### Running a tool

`session/prompt` is served by a **deterministic MCP tool-router** — there is no LLM in
this runtime, so a prompt does not get interpreted, it gets *routed*. Each text content
block must be a JSON object naming an MCP tool:

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

A change is announced with `config_option_update`, carrying **every** option rather than
the changed one — which is what a client re-rendering a settings panel wants.

**Every tool call asks the client for permission first**, via
`session/request_permission`. Nothing reads a tool's annotations yet, so there is no way
to tell a read from a delete — treating every call as consequential is the only setting
that cannot silently do damage, and the "for session" options keep it to once per tool.
Choosing a reject option marks that call `failed` and lets the rest of the turn continue;
cancelling the prompt ends the turn with `stopReason: "cancelled"`.

A tool that *fails* is not a failed turn. MCP reports tool failure as a successful result
carrying `isError`, so the call's update says `status: "failed"` with the tool's own
output, the remaining calls still run, and the turn ends normally.

See [turn_mcp_router.py](src/python_acp/turn_mcp_router.md).

## WebSocket actions (deprecated)

The `{"action": ...}` API and the MCP passthrough on JSON-RPC (`tools/*`, `prompts/*`,
`resources/*`, `ping`) are **deprecated**. They keep working through the ACP v1 migration
and are removed in Phase 7; the passthrough methods move to `_`-prefixed extension
methods first. New work should use the ACP surface above. See
[legacy_ws.py](src/python_acp/legacy_ws.md).

### Every action reply says so

Since the deprecation landed, every `{"action": ...}` reply — successful or not — carries
a `deprecated` block naming the replacement and the removal milestone:

```json
{
  "ok": true,
  "tools": [{"name": "echo", "description": "Echoes text", "inputSchema": {}}],
  "deprecated": {
    "action": "list_tools",
    "use": "session/new + session/prompt",
    "removedIn": "the ACP v1 migration (Phase 7)"
  }
}
```

The block is purely additive: `ok`, `tools`, `result`, and `error` mean exactly what they
meant before, so an existing client keeps working and can ignore the key. `use` is omitted
when there is nothing honest to name — an action that does not exist, or one with no ACP
replacement. The server also logs a warning, once per action per connection.

### Migration table

**The JSON-RPC `tools/*`, `prompts/*`, and `resources/*` methods are going away too.**
They are MCP method names on an ACP wire, they address the process-wide `--mcp-command`
server, and that is precisely the arrangement ACP v1 replaced. An earlier plan was to
rename them under a prefix on the SDK's `ext_method` escape hatch; that was **decided
against**, because it would cost you a rename now and a deletion later for a surface being
removed either way. Both halves go in one step.

| Deprecated call | Migrate to |
|---|---|
| `{"action": "list_tools"}` / `{"method": "tools/list"}` | `session/new` + `session/prompt` — the turn's first `available_commands` update is the tool list |
| `{"action": "call_tool"}` / `{"method": "tools/call"}` | `session/new` + `session/prompt` |
| `{"action": "list_prompts"}` / `{"method": "prompts/list"}` | **nothing** |
| `{"action": "get_prompt"}` / `{"method": "prompts/get"}` | **nothing** |
| `{"action": "list_resources"}` / `{"method": "resources/list"}` | **nothing** |
| `{"action": "read_resource"}` / `{"method": "resources/read"}` | **nothing** |
| `{"action": "ping"}` / `{"method": "ping"}` | **nothing** — ACP has no ping |

The ACP path is a different *shape of program*, not a method swap. You open a session with
`session/new` naming your `mcpServers`, then send `session/prompt`; the agent calls tools
on your behalf and streams `session/update` notifications back. See
[the invocation convention](src/python_acp/turn_mcp_router.md) for what a prompt block
looks like.

**The rows marked "nothing" are not an oversight.** ACP has no method for reading an MCP
prompt or resource — its model is that the *agent* uses them internally, rather than a
client reaching through the agent to the server. When this surface goes, so does the
ability to reach them through this bridge; a client that needs them should speak MCP to
that server directly.

Connect to `ws://127.0.0.1:8765` and send JSON messages.

### List tools

```json
{"action": "list_tools"}
```

### Call a tool

```json
{"action": "call_tool", "name": "echo", "arguments": {"text": "hello"}}
```

### List prompts

```json
{"action": "list_prompts"}
```

### Get a prompt

```json
{"action": "get_prompt", "name": "greeting", "arguments": {"name": "Alice"}}
```

### List resources

```json
{"action": "list_resources"}
```

### Read a resource

```json
{"action": "read_resource", "name": "config://settings", "arguments": {"path": "/tmp/example"}}
```

### Ping

```json
{"action": "ping"}
```

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

On the legacy `action` surface the same tool failure comes back as
`{"ok": false, "error": "file not found", "result": {...}}` — the `result` is
still included.

## Make targets

```bash
make venv
make sync
make install
make lint
make test
make transcripts
make build
make wheel
make sdist
make container-image
make package
make release-bundle
make clean
```

`make transcripts` re-records the golden JSON-RPC transcripts in `tests/transcripts/` and
prints the diff; read it before committing, since an unreviewed regeneration turns a wire
regression into a committed expectation.

The release bundle includes the built Python artifacts and, when available, the exported container image archive.

## Container usage

```bash
podman build -t python-acp -f Containerfile .
podman run --rm -p 8765:8765 python-acp --mcp-command python /app/mock_mcp_server.py
```

Or with Docker:

```bash
docker build -t python-acp -f Containerfile .
docker run --rm -p 8765:8765 python-acp --mcp-command python /app/mock_mcp_server.py
```

## CI/CD

- `.github/workflows/ci.yml`: lint, tests, and build validation.
- `.github/workflows/publish-artifacts.yml`: publishes Python wheel/sdist artifacts and the container image artifact on a GitHub release.

## Notes

- Build outputs such as `dist/` and `artifacts/` are intentionally ignored by Git.
- The project is designed for local development and release packaging without requiring a hosted LLM or additional orchestration layer.
