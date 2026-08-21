# python-acp

`python-acp` is a Python Agent Client Protocol (ACP) app that wraps an MCP server process over `stdio/stdout` and exposes MCP tools directly over WebSockets (no LLM required).

## Features

- Starts an MCP server command using stdio transport.
- Initializes MCP and forwards:
  - `tools/list`
  - `tools/call`
- WebSocket API for direct tool usage.
- Designed for local `venv` workflows.
- Includes a `Containerfile` for Podman/Docker runs.
- GitHub Actions for validate/build and release artifact publishing.

## Runtime setup (venv)

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

## Run

```bash
python-acp --mcp-command python /path/to/your_mcp_server.py --host 127.0.0.1 --port 8765
```

## WebSocket protocol

Connect to `ws://127.0.0.1:8765` and send JSON messages.

### List tools

```json
{"action": "list_tools"}
```

### Call tool

```json
{"action": "call_tool", "name": "echo", "arguments": {"text": "hello"}}
```

### Ping

```json
{"action": "ping"}
```

## Podman

```bash
podman build -t python-acp -f Containerfile .
podman run --rm -p 8765:8765 python-acp --mcp-command python /app/mock_mcp_server.py
```

Provide your own MCP server command for production usage.

## CI/CD

- `.github/workflows/ci.yml`: lint, test, build, upload build artifacts.
- `.github/workflows/publish-artifacts.yml`: build and attach release artifacts on GitHub release publish.
