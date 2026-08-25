# Changelog

Notable changes to `python-acp`, newest first.

This project is pre-1.0 and follows semantic versioning's pre-1.0 rule: a **minor** bump
is allowed to break the client-facing wire. `0.2.0` does. Read its Breaking section before
upgrading — every `0.1.x` client stops working, and every `0.1.x` invocation fails at
startup.

## 0.2.0 — 2026-08-24

**`python-acp` stops being an MCP passthrough and becomes a strict ACP v1 agent runtime.**
192 commits since `0.1.1`.

A client no longer reaches through this process to an MCP server it did not choose. It
opens an ACP session, names the MCP servers that session should talk to, and sends
prompts. There is still **no LLM anywhere in the runtime** — a prompt is not interpreted,
it is *routed* to tool calls deterministically, which is what makes an agent runtime with
no model in it a coherent thing to ship.

The whole design, including what was declined and why, is
[docs/full-apc-plan.md](docs/full-apc-plan.md). The method-by-method state is
[docs/acp-compliance-matrix.md](docs/acp-compliance-matrix.md), and it is executable as
[tests/test_conformance.py](tests/test_conformance.py) rather than a claim in prose.

### Breaking

- **The `{"action": ...}` WebSocket surface is removed**, along with its `{"ok": bool}`
  reply envelope. It was deprecated for the whole development cycle and removed only once
  the conformance suite and an interop run against a foreign client proved the JSON-RPC
  surface at parity. Every well-formed frame on the socket now reaches the ACP router.
- **The MCP passthrough is removed** — `tools/*`, `prompts/*`, `resources/*`, `ping`, and
  `notifications/initialized` no longer ride the client socket. It addressed the
  process-wide server, which is the arrangement ACP v1 inverts, so it was deleted rather
  than renamed onto `ext_method`. There are no extensions: every `ext_method` call is a
  genuine `-32601`.
- **`--mcp-command` is removed, and rejected rather than ignored.** A deployment still
  passing it fails at startup, instead of running while quietly never using the server it
  named. Every MCP server this process talks to is now named by a client in `session/new`,
  gets its own subprocess spawned and handshaked before the response returns, and is torn
  down when the session closes.
- **Reading an MCP prompt or resource through this bridge has no replacement.** ACP has no
  method for either — its model is that an agent uses them internally, not that a client
  reaches through the agent to the server. This is recorded as a decision, not left as a
  gap: see the do-not-reintroduce section of the `acp-protocol` skill.
- **WebSocket is served through the SDK router.** `ws_bridge.py` had its own dispatcher,
  its own error codes, and a hand-built capability block, so a WebSocket client and a
  stdio client were talking to two different agents. One agent now answers both wires, with
  one capability block and one error mapping. The module is now `transport_ws.py`;
  "bridge" named the thing this project stopped being.
- `--transport ws` **remains the default**, so the socket a `0.1.x` deployment binds is
  still bound on the same host and port. Only what you may send over it changed.

#### Upgrading

Before — the server is named on the command line, and the client sends actions:

```bash
python-acp --mcp-command python /path/to/your_mcp_server.py --port 8765
```

```json
{"action": "list_tools"}
{"action": "call_tool", "name": "echo", "arguments": {"text": "hi"}}
```

After — the process names no server, and the client opens a session that does:

```bash
python-acp --port 8765
```

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
{"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {
  "cwd": "/absolute/path",
  "mcpServers": [
    {"name": "tools", "command": "python", "args": ["/path/to/your_mcp_server.py"], "env": []}
  ]}}
{"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
  "sessionId": "...",
  "prompt": [{"type": "text", "text": "{\"tool\": \"echo\", \"arguments\": {\"text\": \"hi\"}}"}]}}
```

Three things to know while porting:

- **`list_tools` needs no equivalent.** Every turn emits an `available_commands_update`
  carrying the session's tools — *including on a refusal*, so a client that got the
  invocation wrong is still told what it could have called.
- **All four of `name`, `command`, `args`, and `env` are required** on an `mcpServers`
  entry. ACP's schema marks the field `skip-invalid-items`, so an entry missing one is
  dropped before this agent sees it: you get a `sessionId` back, no error, and a session
  backed by fewer servers than you asked for. Send `args` and `env` as `[]` when empty.
- **A client is now answering questions, not just asking them.** The agent calls
  `session/request_permission` before every tool call, and a script that never answers it
  will hang rather than fail. `auto-approve` mode exists for exactly that case.

### Added

- **The full ACP v1 agent surface**, built on the `agent-client-protocol` SDK. Every
  routed ACP method is implemented — nothing answers `-32601` any more except a method
  the SDK does not route at all.
- **stdio transport** (`--transport stdio`), which is how an editor spawns an agent.
  stdout carries the JSON-RPC wire and nothing else; all diagnostics go to stderr.
- **Session lifecycle** — `new`, `prompt`, `cancel`, `load`, `list`, `close`, `fork`,
  `resume`. The last three are unstable-gated, matching the SDK, and are advertised only
  while the connection carries `use_unstable_protocol`, so the capability block cannot
  promise a method the router will refuse.
- **Per-session MCP servers** from `session/new`'s `mcpServers`, replacing the single
  process-wide client. Only stdio servers are accepted; an `http` or `sse` entry is
  refused with `-32602` rather than accepted and ignored.
- **A pluggable `TurnExecutor` seam**, with the deterministic MCP tool-router shipped as
  the default. The seam is proven rather than asserted:
  [tests/test_executor_neutrality.py](tests/test_executor_neutrality.py) runs a non-MCP
  executor through a whole session and guards the seam's imports.
- **The full `session/update` variant set** — message chunks, tool call start and
  progress, plan updates, mode, config, session-info. `agent_thought_chunk` and
  `usage_update` are deliberately never sent: no model, so no reasoning trace and no token
  count.
- **Permission before every tool call**, via `session/request_permission`. Rejecting one
  call marks it `failed` and lets the turn continue; cancelling ends the turn `cancelled`.
- **File access through the client** — `fs/read_text_file` and `fs/write_text_file`,
  gated on client capability, so reads and writes stay under the client's control rather
  than this process's.
- **The `terminal/*` family**, with every terminal given back — including one created by a
  turn that was cancelled mid-call.
- **Elicitation forwarding** — an MCP server's question reaches the client as a form-mode
  `elicitation/create`, carrying the tool call it interrupted so a client can attach the
  question to the right place in its UI.
- **Session modes** (`execute`, `dry-run`, `auto-approve`) via `session/set_mode`, and
  **config options** (`announce-tools`, `on-tool-failure`) via `session/set_config_option`
  in both boolean and select shapes. Both announce changes to every client on the session,
  including the one that asked.
- **MCP tool annotations as `ToolCall.kind`**, so a permission prompt can say "delete"
  rather than "other". An annotation changes how the question looks, **never whether it is
  asked** — a hint from the party being restrained is not consent.
- **`authenticate` as a typed refusal.** `initialize` advertises no auth methods, so the
  method exists to answer "authenticate with a method I never offered" with an auth error
  rather than `-32601`.
- **MCP client capabilities and `roots/list`**, and a mapping from every MCP content type
  onto ACP content blocks.
- **Absolute-path enforcement** and a settled containment boundary for `cwd` and
  `additionalDirectories`.
- **A multi-arch container image** — `linux/amd64` and `linux/arm64`, so it runs on a
  Raspberry Pi 3, 4, 5, or Zero 2 W under 64-bit Raspberry Pi OS.

### Fixed

- The MCP protocol version is **negotiated** rather than requested and ignored.
- A timed-out MCP request is **un-asked** with `notifications/cancelled` instead of
  abandoned, and a failed cancel no longer replaces the timeout error that caused it.
- The MCP subprocess is shut down via stdin EOF **before** it is signalled.
- The subprocess's stderr is drained, which was a deadlock waiting on a chatty server.
- `nextCursor` is walked to exhaustion in the list wrappers, so a paginated `tools/list`
  no longer silently returns its first page.
- Server-initiated MCP requests and notifications are routed rather than dropped.
- An MCP server's own JSON-RPC error code and a tool's `isError` survive to the client,
  tagged `data.source` so a backend `-32601` stays distinguishable from ours.
- A turn can no longer emit updates after the request it belongs to is gone.
- `terminal/create` is shielded, so a turn cancelled mid-call still gives the terminal
  back rather than leaking one whose id nobody learned.
- An undeliverable reply logs the method it failed on, not the error text in the method
  field.

### Changed — build, CI, and tests

- **Dependencies are exact-pinned** (`==`): `agent-client-protocol==0.12.1` and
  `websockets==17.0.1`. They are protocol surface, so an upgrade is a reviewed commit
  rather than whatever a resolver picks that day. The SDK brings `pydantic` transitively —
  the first non-pure-Python dependency here. Cost is measured, not guessed: **+26 B** on
  our own wheel, **+~9.9 MiB** on the container image. The table is in
  [CLAUDE.md](CLAUDE.md).
- **The SDK's `requires-python` is now this project's ceiling.** It declares `<3.15`, so
  the CI matrix, the classifiers, and `requires-python` move together and none may pass
  it until the SDK does. A matrix leg outside the SDK's window fails loudly.
- **CI runs the full 3.11–3.14 matrix** — every version `requires-python` claims — with
  `fail-fast: false`. Releases publish from 3.11, the floor, so the wheel installs across
  the whole range.
- **The venv is pinned to `.venv` and stamped**, so `lint`, `test`, and `build` skip `pip`
  and need no network while the stamp is current. `make clean` no longer deletes it;
  `clean-venv` and `distclean` ask for that explicitly.
- **`make container-image` probes the engine instead of trusting `PATH`.** podman on macOS
  and docker are both clients for something that may not be answering, so being installed
  and being able to build are different facts. A failed build now deletes any tar an
  earlier run left, so `release-bundle` cannot ship a stale image, and
  `REQUIRE_CONTAINER=1` — which the release workflow sets — turns any skip into a failure.
- **`make docs-check`** enforces the three documentation invariants nothing else did:
  relative links resolve, every Mermaid flowchart edge names a node its own block defines,
  and every production module has a sibling `.md` with no orphans.
- **The test suite went from one file to 23 files and 866 tests**, including a method-by-method
  conformance suite, golden JSON-RPC transcripts for four flows, a negative-surface table,
  an interop run against a client that imports nothing from this package, and a
  session-wide guard that fails the run if any test leaves a subprocess behind.

## 0.1.1 — 2026-08-21

- Publish the container image as a separate release artifact.

## 0.1.0 — 2026-08-21

- First tagged release: an MCP-passthrough WebSocket bridge with the `{"action": ...}`
  surface, a process-wide MCP server named by `--mcp-command`, and the Makefile,
  `Containerfile`, and GitHub Actions release workflow around it.
