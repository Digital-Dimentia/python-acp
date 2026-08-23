# python-acp

`python-acp` is a Python Agent Client Protocol (ACP) bridge that connects to an MCP server over stdio and exposes MCP capabilities through a WebSocket API for local automation and tooling.

## Features

- Serves ACP over **stdio** (how an editor spawns an agent) and over **WebSocket**, both
  binding the same agent through the `agent-client-protocol` SDK.
- Connects to an MCP server over stdio.
- Initializes the server and forwards MCP messages.
- Exposes MCP tools, prompts, and resources through the deprecated WebSocket actions:
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
  - [turns.py](src/python_acp/turns.md)
  - [turn_mcp_router.py](src/python_acp/turn_mcp_router.md)
  - [cli.py](src/python_acp/cli.md)
  - [mcp_content.py](src/python_acp/mcp_content.md)
  - [mcp_registry.py](src/python_acp/mcp_registry.md)
  - [mcp_stdio.py](src/python_acp/mcp_stdio.md)
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
`session/set_config_option` returns `-32601` until Phase 5.
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

**Only text blocks are read.** An `image`, `audio`, `resource`, or `resource_link` block
is declined by name, because each is context for a model to reason over and there is no
model here. `initialize` says so: `promptCapabilities` reports `image`, `audio`, and
`embeddedContext` all `false`, and those literals are *derived* from what the turn
executor declares it reads, so the advertisement cannot drift from the behaviour.

A prompt that is not an invocation — prose, malformed JSON, an empty prompt — is answered
with `stopReason: "refusal"` and an `agent_message_chunk` explaining the convention. It is
not an error, and **nothing runs**: the whole prompt is parsed before the first tool, so a
malformed third block does not leave two side effects behind.

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

**Every tool call asks the client for permission first**, via
`session/request_permission`. MCP `2024-11-05` has no tool annotations, so there is no way
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
make build
make wheel
make sdist
make container-image
make package
make release-bundle
make clean
```

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
