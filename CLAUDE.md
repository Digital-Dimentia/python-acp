# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:46cd31e7 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

## Build & Test

All work goes through the Makefile, which manages a repo-local virtual environment
(`.venv` if present, otherwise `venv`). Do not invoke `pip` or `pytest` directly —
the targets bootstrap the venv first.

```bash
make venv     # create venv + pip install -e '.[dev]'
make lint     # ruff check src tests
make test     # pytest tests
make build    # python -m build → dist/*.whl, dist/*.tar.gz
```

Before handing off any code change: `make lint && make test`.

Packaging targets:

- `make container-image` — builds via podman or docker, whichever is on PATH.
  **Exits 0 without building if neither is installed**, so a green run does not prove
  an image exists. Check for `dist/python-acp-container.tar`.
- `make package` — build + container image, tarred to
  `artifacts/python-acp-artifacts.tar.gz`.
- `make release-bundle` — build only (no container build of its own; it includes the
  container tar if a previous target left one), tarred to
  `artifacts/python-acp-release-bundle.tar.gz`.
- `make run` — starts the bridge against `tests/fixtures/mock_mcp_server.py` on
  `ws://127.0.0.1:8766`.

CI (`.github/workflows/ci.yml`) runs `make venv && make lint && make test && make build`
on Python 3.11. Release publishing (`.github/workflows/publish-artifacts.yml`) fires on
a published GitHub release.

## Architecture Overview

`python-acp` is an ACP bridge with no LLM in the loop. A WebSocket client sends JSON;
the bridge translates it into JSON-RPC over stdio to an MCP server subprocess and
returns the result.

```
WebSocket client → cli.py → ACPWebSocketBridge (ws_bridge.py)
                                  ↓
                          MCPStdioClient (mcp_stdio.py)
                                  ↓
                          MCP server subprocess (stdio JSON-RPC)
```

- `cli.py` — argument parsing and async bootstrap. Owns the single `MCPStdioClient`.
- `ws_bridge.py` — `ACPWebSocketBridge`; accepts two request shapes on one socket and
  dispatches to the MCP client. This is where the wire contract lives.
- `mcp_stdio.py` — newline-delimited JSON-RPC client over the subprocess's stdio.
  Raises `MCPProtocolError` on any backend failure.

Full detail: [ARCHITECTURE.md](ARCHITECTURE.md) and the co-located module docs.
Target state for strict ACP v1: [docs/full-apc-plan.md](docs/full-apc-plan.md).

**Current shape worth knowing:** one MCP server, bound at process start via
`--mcp-command`, shared by every connected client. There are no ACP session methods
yet — `session/*` returns `-32601`.

## Conventions & Patterns

- **Wire contract** — `ws_bridge.py` serves both a legacy `{"action": ...}` surface
  returning `{"ok": bool}` and a `{"method": ...}` JSON-RPC surface. The JSON-RPC
  surface is the future; the action surface is slated for removal. Error codes are
  mapped from exception type, not built by hand. Use the **`acp-protocol` skill**
  before touching either dispatcher.
- **MCP backend** — `mcp_stdio.py` is an MCP *client* driving the server subprocess. Its
  stdio framing, `initialize` handshake, and capability block are protocol surface, not
  implementation detail, and the stderr drain and 8 MiB stream limit exist to prevent
  deadlocks. Use the **`mcp-protocol` skill** before touching it.
- **Co-located docs** — every production module in `src/python_acp/` has a sibling
  `.md` of the same basename, and both `ARCHITECTURE.md` and `README.md` link them.
  Use the **`repo-docs-sync` skill** when adding, renaming, or deleting a module.
- **Async everywhere** — the runtime is `asyncio`; `pyproject.toml` sets
  `asyncio_mode = "auto"`, so `async def test_*` needs no `@pytest.mark.asyncio`.
- **Typing** — `from __future__ import annotations` at the top of every module; PEP 604
  unions (`dict[str, Any] | None`).
- **Style** — ruff, `line-length = 100`.
- **Tests** — exercise the bridge against `tests/fixtures/mock_mcp_server.py`, not a
  mock object. New MCP methods need the fixture taught to answer them.
- **Build outputs** — `dist/`, `artifacts/`, and the venv are gitignored; never commit
  them.
