# Changelog

Notable changes to `python-acp`, newest first.

This project is pre-1.0 and follows semantic versioning's pre-1.0 rule: a **minor** bump
is allowed to break the client-facing wire. `0.2.0` does. Read its Breaking section before
upgrading — every `0.1.x` client stops working, and every `0.1.x` invocation fails at
startup. `0.3.0` leaves that wire alone; its Breaking section is short, and the item that
will stop a deployment is the Python floor moving to 3.12.

## 0.3.0 — 2026-08-31

**`0.2.0` made this a strict ACP v1 agent. `0.3.0` makes it usable by a person, deployable
by an operator, and — for the first time — willing to author bytes of its own.**
62 commits since `0.2.0`.

There is still **no LLM anywhere in the runtime**. Nothing here interprets a prompt; the
three lines of work below are about who can reach the machinery, where a server recipe
comes from, and what this process is allowed to write.

- **A command surface a person can type.** The turn convention is one JSON object per
  prompt block, which is right for a program and hostile to a human. All three MCP
  primitives — tools, prompts, resources — are now reachable from a composer.
- **A catalogue an operator configures.** ACP assumes the client spawns the agent and
  hands it `mcpServers`. A long-lived WebSocket bridge inverts that, and a client naming
  `command` and `args` on a shared socket is asking this process to execute an arbitrary
  binary. An operator writes a catalogue; a client selects from it.
- **Structured edits that prove they did nothing else.** Path-addressed splices into JSON,
  YAML, and Markdown, verified against an independent parser before they are written.

### Breaking

- **Python 3.11 is dropped; the floor is 3.12.** The whole lockstep moved together — the
  CI matrix, the `classifiers` list, `requires-python`, and `Containerfile` (now
  `python:3.12-slim`, because a 3.11 base can no longer install this wheel at all). The
  SDK's `<3.15` remains the ceiling, so the supported window is 3.12–3.14.

  One signal was lost on purpose: **CPython 3.11 was the only interpreter that ever
  reported a leaked subprocess transport**, as an unraisable-exception warning that fired
  only when the GC happened to run and named whichever test was executing rather than the
  one that leaked. The deterministic guard in `tests/conftest.py` replaced it rather than
  supplementing it, so no version CI runs emits it any more and nothing is weaker for it.
- **`/invokeTool` is no longer announced**, though it still parses. Every MCP tool is now
  advertised under its own name — `/demo/echo --text hi` — so the built-in that existed to
  reach tools the long way round no longer earns a palette slot. It stays in the parser
  because the sugar cannot express two things: a tool whose name contains a slash, and a
  server this session has not selected. A client that renders `available_commands` will
  see the entry disappear and the tools appear.
- **Agent text is Markdown, not preformatted plain text.** `agent_message_chunk` names no
  content type, which this project had read as licence to send plain text. Every real ACP
  client parses it as Markdown, and plain text put through a Markdown parser is not styled
  differently, it is destroyed: `<name>` is an HTML tag the browser deletes, and an
  indented listing reflows into one line. Listings and refusals are now fenced and
  code-spanned by [`markdown.py`](src/python_acp/markdown.py). A client that displayed
  these chunks verbatim will now see the delimiters.

### Added

#### Commands, for a person at a prompt

- **One command family per MCP primitive** — `/tools`, `/invokeTool`, `/listPrompts`,
  `/promptShow`, `/promptInvoke`, `/listResources`, `/resourceShow`. The leading slash is
  optional on input, because a client filling its composer from `available_commands` sends
  the name it was given and a person typing by hand may not reach for the slash first.

  Only tools were reachable before, which left the palette showing the *model's* callables
  — MCP calls tools model-controlled — where every other client surfaces prompts, the
  user-controlled primitive. What each command can honestly do is still fixed by having no
  model: `/promptShow` works because the **server** performs the substitution.
- **Every MCP tool is callable by its announced name.** `<server>/<tool>` produces the
  identical `InvokeTool` that `/invokeTool` produces, so it inherits the session mode, the
  permission prompt, the tool-call `kind`, and the failure policy without knowing they
  exist.
- **Each tool's `inputSchema` rides on `AvailableCommand._meta`**, so a client can render
  a form instead of making the user learn flag names and legal enum values by trial and
  error. ACP gives a command exactly one argument shape — a single free-text `hint` — and
  this bridge was building that hint from a whole JSON Schema and then throwing the
  structure away. The contract for what goes in `_meta` is published as
  [docs/tool-schema-contract.md](docs/tool-schema-contract.md), for both audiences at
  once: what a client may rely on, and what an MCP server author gets for writing a good
  schema.
- **`announcer.py`** — a session's commands are announced on `new`, `load`, and `resume`,
  after the response rather than inside it.

#### An operator-configured MCP catalogue

- **`--mcp-config PATH`** — a catalogue of MCP servers this agent offers. TOML is primary
  (`tomllib` is stdlib at the floor, and a file an operator maintains wants comments);
  JSON is accepted by suffix in the `{"mcpServers": {...}}` shape every editor already
  writes, so an operator can paste the config they have rather than translate it.
- **Selection is native ACP, not an extension.** One `SessionConfigOptionBoolean` per
  entry, advertised in `NewSessionResponse.configOptions` and `LoadSessionResponse`,
  changed with `session/set_config_option`, announced with `config_option_update` — a set
  of booleans is what a multi-select looks like here, and it is not a workaround: a server
  is independently on or off. Ids are namespaced `mcp/<name>` so a catalogue entry called
  `announce-tools` cannot shadow the executor's own toggle. Servers can be switched on and
  off **mid-session**.
- **This is not `--mcp-command` returning.** A catalogue holds *recipes*. Servers are
  still one subprocess set **per session**, owned by the session, torn down when it
  closes, and reachable only through `session/prompt`. The only thing that moved is where
  a recipe comes from. `session/new`'s `mcpServers` is untouched, and one session can have
  both.
- **`--no-client-mcp-servers`** refuses client-supplied command lines with `-32602`
  naming the flag and listing the catalogue, rather than by ignoring them. Off by default,
  because refusing is wrong for the transport ACP was designed around; honoured on both
  transports, so one deployment config means one thing everywhere.
- **SIGHUP reloads the catalogue without a restart** (WebSocket only, and only with
  `--mcp-config`). A restart drops every connected client and every live session, for a
  deployment whose whole point is being long-lived and shared.

  The hard question was *when a session sees it*. A reload has no client to notify — a
  `Session` holds no client handle and cannot, since it outlives connections — so a sweep
  at signal time would have to push `config_option_update` down *some* connection, and
  telling a client about a session it never touched is worse than telling it late. The
  reconcile therefore runs at the session's own next `session/prompt` or `session/resume`,
  where a client is definitely present and definitely the right one. A session nobody is
  using has no observable state to be stale. Added entries arrive **off** whatever
  `enabled` says; removed entries are torn down; a changed recipe is respawned; an invalid
  file never reaches the reconciler, so there is no half-applied state.

#### Structured edits

- **[`edits.py`](src/python_acp/edits.md)** and three dialects —
  [`edit_json.py`](src/python_acp/edit_json.md),
  [`edit_yaml.py`](src/python_acp/edit_yaml.md), and
  [`edit_docs.py`](src/python_acp/edit_docs.md). An address resolves to a byte span and
  text is substituted into it; nothing is re-emitted. Not because splicing preserves
  formatting better, but because it preserves it *by construction* — you cannot assert
  "the untouched bytes are unchanged" about a file you rebuilt from an AST.

  The obvious input, a unified diff, is the wrong one: an LLM's `@@ -12,7` line numbers
  are unreliable, so applying one means fuzzy context matching, and once the match is
  fuzzy "did this land in the right place?" has no answer but the matcher's own confidence
  — which is the thing under suspicion.
- **An `edit` directive on a turn block**, taking `path`, `format`, and `ops`. `format` is
  **named, never sniffed from the extension**, because an extension is not a promise about
  a file's contents: a `.yml` full of Go template directives is not YAML. `address` is an
  RFC 6901 pointer; for Markdown it is a heading path with the `#` markers included
  (`/# Install/## macOS`), because without them `## Errors` and `### Errors` are the same
  address in a document that has both.
- **`"fromOutput": true`** is what makes `edit` the precise sibling of `write` rather than
  an unrelated second feature: the same bytes, spliced at an address instead of over the
  whole file. `write` and `edit` in one block is refused — two destinations for one tool's
  output is a guess this executor already declines to make.
- **This is the one place the agent authors bytes of its own**, and it was decided rather
  than refactored into. The alternative on offer was not "no transformation" but "the same
  transformation, done by a model emitting a whole file into `write`" — the same posture
  with none of the evidence. It is paid for with a proof: seven steps, any failure
  rejecting the whole edit, ending in *every byte outside the addressed spans is
  unchanged*. The whole file is read, with no `line`/`limit` window, because a window
  would make that assertion about a fragment while the write replaces a file. An edit
  refusal is a **failed tool call** with the reason as a text block — never a JSON-RPC
  error and never a failed turn. See ARCHITECTURE.md, "Structured edits, and the
  neutrality this deliberately trades away".

#### Operating and inspecting

- **[`scripts/start-ws.sh`](scripts/start-ws.sh)**, and `make run` / `make debug` /
  `make stdio` running through it. The script *activates* the venv, which only shows one
  level down: an MCP server a client names as a bare `python` inherits this process's
  PATH. `LOG=1` tees diagnostics to a file, capturing **stderr only** so stdout stays a
  clean protocol wire — which is what makes the same script reusable for `--transport
  stdio`. `run` and `debug` mint a fresh access key per start and print the whole connect
  string, so the banner is a working example rather than a template. All three are
  launchable from any working directory, because another program starts them.
- **[`scripts/start-zoo.sh`](scripts/start-zoo.sh)** and a **schema zoo** in the mock MCP
  server, so a client's form rendering has something non-trivial to render.
- **[`STATISTICS.md`](STATISTICS.md)**, generated by `make stats` and checked for
  staleness by the suite.
- **`--debug` names the emitting logger.** Without it, `websockets`' own "server listening
  on host:port" renders identically to one of ours.
- The WebSocket transport says at startup **whether an access key was configured**.

### Fixed

- **A union `type` no longer kills the turn with `-32603`.** JSON Schema lets `type` be a
  list; `x in {"number", "integer"}` hashes `x`, so an unhashable list raised `TypeError`
  where every other bad-argument case is a readable `CommandError`. A union declares no
  single type to coerce to, so it now takes the same path an undeclared property takes.
- **A loose token is refused by naming the tool's own parameters.** `/demo/echo foo` used
  to offer the user's *value* back as a parameter name and never mention the real ones.
- **A curly quote is reported at its cause.** `shlex` does not know `“`, so an
  autocorrected `--text “Hello there”` split at every space and the refusal landed four
  tokens away. Tokenisation is deliberately unchanged: `’` is an apostrophe far more often
  than a quote.
- **`/listResources` shows URI templates.** MCP publishes them through
  `resources/templates/list` and nothing else, so a filesystem server publishing
  `file:///{path}` was reported as "0 resources on 1 server" — a confident wrong picture,
  not a missing feature, since such a server *does* declare the resources capability. The
  second pass inherits the cursor walk, the repeated-cursor guard, and the page ceiling.
- **`read_resource` sends a `uri` only**, as `resources/read` defines. It had also
  forwarded an `arguments` member that no real server reads; it survived because the mock
  fixture honoured it, so the divergence looked like a working feature. Template expansion
  is client-side (RFC 6570) and never crosses the wire, so there is no param for it to
  travel in.
- **The command palette is built before the `session/new` response, not inside the
  observer.** The observer is a task, and building the palette awaits a `tools/list` round
  trip per backend; the SDK's own client pipelines, so a `session/prompt` sent the instant
  `new_session` returned could overtake the announcement on the wire. A genuine
  sub-millisecond race — 4 failures in 5 runs on 3.11, 0 in 8 on 3.14.
- **`start-ws.sh` drops empty arguments and lets pre-launch failures reach the log.** A
  caller that builds its command line by joining a list turns a blank row in a config form
  into `unrecognized arguments:` with nothing after the colon, and a bare exit 2 that
  names no cause.
- **`make stats` counts the project's documentation**, not every `.md` in the checkout —
  agent tooling, fixture documents, and scratch notes were moving the numbers and failing
  the staleness check with a diff about a file that is not documentation. A staleness alarm
  that fires for reasons the reader cannot connect to the report is one people learn to
  silence by regenerating without reading, which is the failure the document exists to
  prevent. Two related fixes: the commit stamp is excluded from the check, and the suite
  no longer rewrites the document it is checking.
- **`docs-check` skips matrix-leg venvs** (`.venv312`, …), which the exact-name match
  never did.
- **`ping_interval` / `ping_timeout` are named explicitly** on the WebSocket server rather
  than left to the library default.

### Changed — dependencies, CI, and tests

- **`ruamel.yaml==0.19.1` is a new exact-pinned runtime dependency**, and the one pin that
  is not protocol surface. There is no YAML parser in the standard library, and without an
  *independent* parser the verifier — the entire product of the `edits.py` family — cannot
  exist for YAML: a hand-rolled loader beside the hand-rolled scanner would share an author
  and therefore its bugs. Cost is a 118 KB pure-Python wheel with **zero hard
  dependencies**. **Never take the `libyaml` or `oldlibyaml` extras** — `ruamel.yaml.clib`
  is a C extension that would reintroduce per-architecture wheels for a speed-up nothing
  here needs.
- **A client-contract suite.** [tests/test_client_contract.py](tests/test_client_contract.py)
  and [tests/test_invocation_lines.py](tests/test_invocation_lines.py) pin a real ACP
  client's integration checklist and its invocation lines against this repo's own parser,
  so a wire regression breaks a test here rather than a downstream client. Three of this
  release's fixes were found by driving that client against the stdio transport, and all
  three had shipped under a full suite of assertions — because every one of those
  assertions checked what leaves this process rather than what a client does with it.
  `test_every_announced_command_is_one_the_parser_accepts` walks the announcement and
  feeds each name back through the parser.
- **CI runs every action on the Node 24 runtime**, and the matrix is 3.12, 3.13, 3.14.
  Releases publish from 3.12, the floor.
- **All four golden transcripts were re-recorded and the diff read.**
- The suite is **1843 tests**, against 41 test modules and a 1.9 : 1 test-to-production
  line ratio.
- `docs/mcp-gateway-ideals.md` records a gateway structure sketched for a future direction;
  nothing in this release implements it.

## 0.2.0 — 2026-08-24

**`python-acp` stops being an MCP passthrough and becomes a strict ACP v1 agent runtime.**
196 commits since `0.1.1`.

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
- **Binding a non-loopback host now requires an access key, or an explicit opt-out.**
  `python-acp --host 0.0.0.0` with no `PYTHON_ACP_WS_KEY` exits `2` instead of serving.
  This breaks the `0.1.x` container recipe on purpose — see Added, below, for what to set.
  `PYTHON_ACP_WS_ALLOW_UNAUTHENTICATED=1` restores the old behaviour for anyone who wants
  it. **Loopback with no key is unchanged**, so no local workflow is affected.

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
- **An access key for the WebSocket transport.** Set `PYTHON_ACP_WS_KEY` in the server's
  environment; a client presents it as `ws://host:8765/?key=<secret>`. A client without it
  is refused `401` **during the opening handshake**, so it never reaches `initialize`.
  Before this, anyone who could open the socket was a client — and since `session/new`
  spawns a command the client names, an exposed socket was remote code execution as the
  bridge's user.

  It is read from the environment and there is no `--ws-key` flag, because `argv` is
  world-readable through `ps`. It is transport admission control rather than ACP's
  `authenticate`, so `initialize` still advertises no auth methods — accurately: that
  field is the agent presenting a credential, and it has none.

  Know its limits before relying on it: no TLS in this process, so the key rides in the
  URL and lands in access logs; one shared key with no identity, so no per-client
  revocation and no rotation without a restart. The README's
  [Securing the WebSocket](README.md#securing-the-websocket) section states the threat
  model, and `pyacp-smj` holds the real design.
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
- **The test suite went from one file to 24 files and 901 tests**, including a method-by-method
  conformance suite, golden JSON-RPC transcripts for four flows, a negative-surface table,
  an interop run against a client that imports nothing from this package, and a
  session-wide guard that fails the run if any test leaves a subprocess behind.

## 0.1.1 — 2026-08-21

- Publish the container image as a separate release artifact.

## 0.1.0 — 2026-08-21

- First tagged release: an MCP-passthrough WebSocket bridge with the `{"action": ...}`
  surface, a process-wide MCP server named by `--mcp-command`, and the Makefile,
  `Containerfile`, and GitHub Actions release workflow around it.
