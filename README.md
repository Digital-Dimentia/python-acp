# python-acp

`python-acp` is a Python Agent Client Protocol (ACP) bridge that connects to an MCP server over stdio and exposes MCP capabilities through a WebSocket API for local automation and tooling.

## Features

- Connects to an MCP server over stdio.
- Initializes the server and forwards MCP messages.
- Exposes MCP tools, prompts, and resources through WebSocket actions.
- Supports:
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
  - [cli.py](src/python_acp/cli.md)
  - [mcp_stdio.py](src/python_acp/mcp_stdio.md)
  - [ws_bridge.py](src/python_acp/ws_bridge.md)
- Design docs (target state, not yet built):
  - [ACP v1 plan](docs/full-apc-plan.md)
  - [Module boundaries](docs/module-boundaries.md)

## Local setup

This project prefers a repo-local virtual environment.

```bash
python -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Run the bridge

```bash
python-acp --mcp-command python /path/to/your_mcp_server.py --host 127.0.0.1 --port 8765
```

## WebSocket actions

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

## Make targets

```bash
make venv
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
