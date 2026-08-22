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
