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
  - [cli.py](src/python_acp/cli.md)
  - [mcp_stdio.py](src/python_acp/mcp_stdio.md)
  - [transport_stdio.py](src/python_acp/transport_stdio.md)
  - [transport_ws.py](src/python_acp/transport_ws.md)
- Design docs (target state, not yet built):
  - [ACP v1 plan](docs/full-apc-plan.md)
  - [ACP v1 compliance matrix](docs/acp-compliance-matrix.md)
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

### As an ACP agent over stdio

`--transport stdio` speaks ACP on the process's own stdin and stdout, which is how an
editor spawns an agent. It is not run by hand — the client launches it:

```bash
python-acp --transport stdio --mcp-command python /path/to/your_mcp_server.py
```

`--host` and `--port` are ignored in this mode, **stdout carries the JSON-RPC wire and
nothing else**, and all diagnostics go to stderr. The agent currently answers
`initialize` and refuses `authenticate`; session and prompt methods return `-32601`
until Phases 2 and 3 land. See [agent.py](src/python_acp/agent.md) for the per-method
state and [transport_stdio.py](src/python_acp/transport_stdio.md) for the binding.

### As an ACP agent over WebSocket

`--transport ws` serves **the same agent**. A WebSocket client that sends ACP JSON-RPC
gets the same `initialize` negotiation, the same capability block, and the same error
codes a stdio client gets:

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
```

`ws` remains the default transport because it also carries the deprecated surface below,
which stdio never had. See [transport_ws.py](src/python_acp/transport_ws.md).

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
