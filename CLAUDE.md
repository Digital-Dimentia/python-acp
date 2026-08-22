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

All work goes through the Makefile, which manages a repo-local virtual environment.
**`.venv` is canonical** — it is pinned, not discovered, so every developer and every
CI leg uses the same directory. A checkout that still has the older `venv/` is renamed
to `.venv/` on the first `make venv` (re-activate your shell afterwards). Do not invoke
`pip` or `pytest` directly — the targets bootstrap the venv first.

```bash
make venv     # create/refresh .venv; a no-op when it is already current
make sync     # force pip install -e '.[dev]' even when the venv looks current
make lint     # ruff check src tests
make test     # pytest tests
make build    # python -m build → dist/*.whl, dist/*.tar.gz
```

`make venv` writes `.venv/.python-acp-venv.json`, a stamp recording the interpreter the
venv was built from and a hash of `pyproject.toml`. While that stamp matches, `lint`,
`test`, and `build` skip `pip` entirely and need **no network**. Editing `pyproject.toml`
invalidates it and the next `make venv` reinstalls; `make sync` forces a reinstall
without editing anything.

Knobs, all overridable on the command line:

- `PYTHON` — the interpreter the venv is built *from*. Changing it rebuilds the venv
  instead of installing into the old one, so `make venv PYTHON=python3.12
  VENV_DIR=.venv312` produces a real 3.12 environment even though `.venv` already
  exists; `make lint VENV_DIR=.venv312` then runs against it. That is how a CI matrix
  leg is reproduced locally (it needs that interpreter installed on the machine).
  If `PYTHON` resolves to an interpreter *inside* a virtual environment — an activated
  shell, where `python3` is `.venv/bin/python3` — the bootstrap steps out to its base
  interpreter rather than building a venv from inside the venv it is replacing.
- `VENV_DIR` — where the environment lives. Defaults to `.venv`; `.venv*/` is gitignored.
- `OFFLINE=1` — forbid the network. Succeeds only when the venv already satisfies
  `pyproject.toml`, and otherwise fails with the list of unmet requirements.
- `PIP_TRUSTED_HOST` — empty by default. Behind a TLS-intercepting proxy, pip fails with
  `SSLCertVerificationError` because the proxy's CA is not in pip's certifi bundle. The
  clean fix is `PIP_CERT=/path/to/proxy-ca.pem`; for a *trusted local* proxy,
  `make venv PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org"` is the documented
  opt-in. Nothing in the default build path relaxes TLS verification.

The venv logic lives in [scripts/venv_bootstrap.py](scripts/venv_bootstrap.py), not in a
Makefile recipe, because the rules are conditional in ways `make` expresses badly.

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
  `ws://127.0.0.1:8766`. Both the bridge and the MCP subprocess run on
  `$(VENV_DIR)/bin/python`, so they cannot drift onto different interpreters.

CI (`.github/workflows/ci.yml`) runs `make venv && make lint && make test && make build`
across a matrix of Python 3.11, 3.12, 3.13, and 3.14 — every version
`requires-python = ">=3.11,<3.15"` claims — with `fail-fast: false` so one version's
failure does not mask the others. Build artifacts are uploaded from the 3.11 leg only.
Keep the matrix and the `classifiers` list in `pyproject.toml` in lockstep, and keep
both inside the `requires-python` window.

**That window now has a ceiling this project does not own.**
`agent-client-protocol` declares `requires-python = ">=3.10,<3.15"`, so 3.14 is the
newest interpreter we may claim or test against until the SDK itself moves. Adding a
3.15 leg to the matrix, a 3.15 classifier, or raising `requires-python` past `<3.15`
before the SDK does produces a release that cannot install. The three-way lockstep is
therefore: **matrix ↔ classifiers ↔ `requires-python`, all bounded above by the SDK.**
`tests/test_sdk_dependency.py` asserts the running interpreter is inside the SDK's
declared window, so a matrix leg that drifts out of it fails loudly rather than
silently shipping. Release publishing
(`.github/workflows/publish-artifacts.yml`) fires on a published GitHub release and
deliberately stays on 3.11, the floor, so the published wheel is installable across the
whole supported range. Each leg starts from a bare runner with no venv, so it takes the
create-and-install path; the stamp then keeps `lint`, `test`, and `build` from
reinstalling three more times.

### Dependencies

Runtime dependencies are **exact-pinned** (`==`). They are protocol surface, not
conveniences, so an upgrade is a deliberate, reviewed commit of its own rather than
whatever a resolver picks on the day.

- `websockets==12.0` — the WebSocket server transport.
- `agent-client-protocol==0.12.1` — Zed's Agent Client Protocol SDK
  ([agentclientprotocol/python-sdk](https://github.com/agentclientprotocol/python-sdk)).
  It is pre-1.0, where a *minor* bump is allowed to break, which is why the pin is exact
  rather than `~=`. It is **not** PyPI `acp-sdk` — that is IBM's Agent *Communication*
  Protocol, a different protocol with the same abbreviation. Never substitute one for
  the other. Its WebSocket/HTTP transport sits behind the SDK's `http` extra
  (`httpx[http2]>=0.27` + `websockets>=12.0`); we do not take that extra, because this
  bridge already pins `websockets` and drives its own transport.

The SDK brings `pydantic>=2.7` transitively (with `pydantic-core`, `typing-extensions`,
`typing-inspection`, `annotated-types`) — the first non-pure-Python dependency this
project has had. Cost measured on CPython 3.14 / macOS arm64 under bead `pyacp-4ns.1`:

| Measure | Before | After | Delta |
| --- | ---: | ---: | ---: |
| `dist/*.whl` | 15,086 B | 15,112 B | **+26 B** |
| `dist/*.tar.gz` (sdist) | 18,817 B | 20,539 B | +1,722 B |
| Runtime-only `site-packages` | 1,088 KiB | 11,872 KiB | **+10,784 KiB (~10.5 MiB)** |
| Linux `cp311` wheel downloads | 130,872 B | 2,852,496 B | +2,721,624 B (~2.60 MiB) |

Read that top row before panicking about the bottom two: **our own wheel is unaffected**
— a dependency only adds `Requires-Dist` lines to `METADATA`. The sdist delta is almost
entirely the new test file, not the dependency. The real cost is what gets *installed*:
`pydantic-core` (4.4 MiB) and `pydantic` (4.3 MiB) together are ~85% of it; the `acp`
package itself is ~1.0 MiB. The last row is the honest proxy for the container layer,
since `Containerfile` builds on `python:3.11-slim` and pulls manylinux wheels;
`pydantic-core` alone is 2.0 MiB of it because it ships a compiled Rust extension.

The container *image* delta was **not measured** — neither podman nor docker is
installed on the development machine, and `make container-image` exits 0 without
building in that case. Tracked in bead `pyacp-8ub`.

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
