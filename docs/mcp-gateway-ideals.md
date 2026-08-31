With all integrations as local stdio subprocesses, the structure simplifies significantly. Here's a practical layout:

```
mcp-gateway/
├── gateway/                    # The parent process (your MCP client)
│   ├── main.py                 # Entry point: starts gateway, exposes unified MCP interface
│   ├── registry.py             # Server registry: maps integration name → spawn config
│   ├── session_manager.py      # Spawns/tears down subprocesses, manages stdio pipes
│   ├── router.py               # Routes incoming tool calls to the right subprocess
│   ├── auth/                   # Gateway-level auth (if any)
│   │   └── ...
│   └── config/
│       └── servers.yaml        # Declarative config for all registered servers
│
├── integrations/               # One directory per integration (each is a standalone MCP server)
│   ├── github/
│   │   ├── server.py           # MCP server entry point (stdio transport)
│   │   ├── auth.py             # Integration-specific auth (tokens, OAuth, etc.)
│   │   ├── tools.py            # Tool definitions
│   │   └── requirements.txt    # Isolated deps
│   ├── slack/
│   │   ├── server.py
│   │   ├── auth.py
│   │   ├── tools.py
│   │   └── requirements.txt
│   ├── postgres/
│   │   ├── server.py
│   │   ├── auth.py
│   │   ├── tools.py
│   │   └── requirements.txt
│   └── ...
│
├── shared/                     # Code shared across integrations (imported, not inherited)
│   ├── base_server.py          # Common stdio server bootstrap
│   ├── base_auth.py            # Auth patterns (token refresh, credential storage)
│   ├── logging.py              # Structured logging that works over stdio
│   └── errors.py               # Standard error formatting for MCP responses
│
├── tests/
│   ├── test_gateway.py
│   └── integrations/
│       ├── test_github.py
│       └── test_slack.py
│
├── pyproject.toml              # Gateway-level deps
├── Makefile                    # `make run-gateway`, `make test-github`, etc.
└── README.md
```

### Key design decisions

**Registry-driven spawning.** The gateway never hardcodes which integrations exist. `servers.yaml` declares each one:

```yaml
servers:
  github:
    command: "python"
    args: ["integrations/github/server.py"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"
    tools_prefix: "gh_"       # optional: prefix tool names to avoid collisions
  slack:
    command: "python"
    args: ["integrations/slack/server.py"]
    env:
      SLACK_BOT_TOKEN: "${SLACK_BOT_TOKEN}"
```

The `session_manager` reads this, spawns each subprocess, and wires `stdin`/`stdout` to its MCP client connection. Adding a new integration = new directory + one YAML entry, zero gateway code changes.

**Per-integration venvs or containers.** Since each integration has its own `requirements.txt` (or `package.json`), you can either:
- Give each its own virtualenv (simplest), or
- Run each in a Docker container with `command: "docker", args: ["run", "--rm", "mcp-github"]` — the gateway code doesn't change, only the spawn config does.

**Auth lives in the integration, not the gateway.** Each `auth.py` handles its own credential resolution (env vars, keychain, file). The gateway only passes through environment variables declared in the registry. This keeps the gateway integration-agnostic.

**Shared is a library, not a package.** Integrations `import` from `shared/` (via `sys.path` or a local editable install) but never from each other. This preserves isolation while avoiding duplication of boilerplate.

### Process lifecycle tips

- **Lazy spawn:** Don't start all subprocesses at gateway boot. Spawn on first tool call for that integration, keep alive with a TTL (e.g., 5 min idle), then kill. This matters when you have 20+ integrations.
- **Health check on spawn:** After `initialize`, do a quick `list_tools()` call to confirm the server is responsive before registering it.
- **Stderr forwarding:** Pipe subprocess `stderr` to your structured logger (tagged with the integration name) so you can debug crashes without losing the stdio channel (which is reserved for JSON-RPC on stdout).
- **Graceful shutdown:** On gateway exit, send SIGTERM to all children, wait a grace period, then SIGKILL.

