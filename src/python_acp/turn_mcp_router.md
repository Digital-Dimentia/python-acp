# `turn_mcp_router.py` — the shipped default turn executor

Decision D3 says `session/prompt` runs behind a swappable executor, and D1 says there is
no LLM in this runtime. So the default cannot *interpret* a prompt — it can only **route**
one. A client says which tool to run and with what; this executes it against that
session's MCP backends, streams the call's real status transitions back as
`session/update`, and returns.

Nothing here reasons, plans, or retries. That is the point, not a limitation: an
LLM-backed executor drops into the same [seam](turns.md) without reopening it.

## The invocation convention

**Invented here.** The ACP spec says what a prompt *is* — a list of content blocks — and
nothing about how a block names a tool, because every other agent has a model to work that
out. With no model the contract has to be explicit, so it is the one thing in this module
a client codes against.

A **text** content block whose entire text is a JSON object:

```json
{"type": "text", "text": "{\"tool\": \"echo\", \"arguments\": {\"text\": \"hi\"}}"}
```

| Field | Required | Meaning |
|---|---|---|
| `tool` | yes | The MCP tool name |
| `arguments` | no, defaults to `{}` | Passed to `tools/call` unchanged |
| `server` | only when the session opened **more than one** MCP server | Which server from `session/new`'s `mcpServers` |
| `read` | no | `{argument: {path, line?, limit?}}` — files to read **through the client** into arguments |
| `write` | no | `{path}` — where the tool's text output goes, **through the client** |
| `run` | no | `{argument: {command, args?, cwd?, env?, outputByteLimit?}}` — commands to run **in the client's terminal**, into arguments |

Explicit `server`/`tool` fields rather than a single `"server/tool"` string: both names
are arbitrary and may contain a slash, so a separator would be ambiguous exactly where
being wrong is silent.

Every text block in the prompt is one invocation, run **in order**.

`server` may be omitted for a single-server session because there is nothing to guess.
With two or more it is required — picking one is the kind of help nobody wants. The
refusal names the servers that *are* open, so a client does not have to go looking.

The tool-call title is **always** qualified (`tools/echo`), even when the client omitted
`server`. The title outlives the turn — it is in the transcript `session/load` replays —
and "which server ran this" is not recoverable later from a bare name.

## Files move through the client, never through this process

`fs/read_text_file` and `fs/write_text_file` are `acp.interfaces.Client` methods: an ACP
agent **calls** them, it does not serve them. This executor never opens a file, so the
client stays in control of what is read and written — which is the whole reason the two
optional fields exist rather than a `pathlib` call.

```json
{"tool": "summarise",
 "read": {"document": {"path": "/abs/in.md", "line": 1, "limit": 40}},
 "write": {"path": "/abs/out.md"}}
```

- **`read` maps an argument name to a file.** The file's content becomes that argument's
  value immediately before the tool runs. `line` (1-based) and `limit` go straight through
  to `ReadTextFileRequest`, so a client asks for a window rather than always paying for a
  whole file; both must be non-negative integers, and `true` is rejected explicitly because
  `bool` is an `int` in Python.
- **An argument named in both `arguments` and `read` is refused.** Two sources for one
  value is exactly the guess `server` already declines to make.
- **`write` takes the tool's text content**, joined with newlines, after the call returns.

### Containment, and the lock this cannot become

Both paths must be absolute and inside the session's declared roots.
[paths.py](paths.md) owns that rule (`pyacp-3rw.4`) and this module only adapts its
refusal into a prompt refusal; the **resolved** path it returns is what goes on the wire,
because the string the client wrote may traverse a symlink the check never followed.

`paths.md` records that containment is *a check, not a lock* — a path that passes can
become a symlink out of the tree a microsecond later — and says closing that needs
`openat` / `O_NOFOLLOW` "with the code doing the opening — Phase 4.2".

**This is Phase 4.2, and the lever is not here.** It opens nothing; the client does. What
this side can do is send the resolved path, so the client is not asked to re-walk links we
already walked, and that is what it does. Closing the window properly is the *client's*
`fs/*` implementation, and no ACP agent can do it on the client's behalf.

## Commands run on the client's machine, and are always given back

`terminal/create`, `/wait_for_exit`, `/output`, `/kill`, and `/release` are
`acp.interfaces.Client` methods, the same arrangement as `fs/*`: an agent calls them and
never serves them. So the executor starts no process of its own.

```json
{"tool": "summarise",
 "run": {"log": {"command": "git", "args": ["log", "--oneline", "-5"],
                 "cwd": "/abs/repo", "env": {"TZ": "UTC"}, "outputByteLimit": 65536}}}
```

`run` maps an **argument name** to a command exactly as `read` maps one to a file: the
command's captured output becomes that argument's value immediately before the tool runs.

| Field | Required | Meaning |
|---|---|---|
| `command` | yes | The executable, as the client will run it |
| `args` | no | A list of **strings**; `["-n", 5]` is refused rather than coerced, because a number in a string field fails at the client with nothing naming the block |
| `cwd` | no, defaults to the session's `cwd` | Absolute and inside the session's roots, checked by [paths.py](paths.md) like every other path here |
| `env` | no | `{"NAME": "value"}` — an object rather than the schema's `[{name, value}]` list, because duplicate names cannot happen in one and this is the shape a client writing JSON reaches for |
| `outputByteLimit` | no, defaults to 1 MiB | Spelled as the schema field it sets. **Never absent on the wire** — see [terminals.md](terminals.md) for where the default comes from |

An argument named twice — in `arguments`, in `read`, and in `run` in any combination — is
refused, for the reason two sources for one value always are.

**Containment on `cwd` is consistency, not a sandbox.** `command` is arbitrary and the
client is the party that decides whether to run it; a command may `chdir` anywhere the
moment it starts. What the check buys is that a prompt cannot *point* the working
directory outside the session's declared roots, which is the same promise `read` and
`write` make, and it costs nothing to keep the three consistent.

### Every path releases the terminal

A terminal exists on the client until `terminal/release` arrives, so `_capture` gives it
back on every exit:

| Path | What happens |
|---|---|
| The command succeeds | `wait_for_exit` → `output` → `release`; the output becomes the argument |
| The command exits non-zero, or on a signal | released, and the **tool is not called** — its argument would have to be invented |
| The client errors on `create` or on a read | released if it exists at all; the call is `failed` with the client's own code and message |
| The tool fails afterwards | already released; a tool failing does not un-run the command |
| `session/cancel` arrives mid-command | `kill` then `release`, under `asyncio.shield`, then the `CancelledError` is re-raised |
| `session/close` reaches a turn still running | [terminals.py](terminals.md)'s registry releases it through the `on_close` hook |

The cancelled path is the one worth reading. The cleanup is shielded because the
cancellation is *already in flight*: an unshielded `await` would be cancelled at its first
suspension point and the release would never reach the wire. The `CancelledError` is
re-raised rather than swallowed, because a turn that swallowed it would report `end_turn`
for a turn the client stopped.

The command runs **after** the permission prompt, like the reads: approving the call is
what authorises starting a process on someone's machine. A dry run starts nothing and
names the command it would have run.

Each command adds a note to the tool call's content — what ran, how many characters it
produced, and which argument they went into, plus whether the client had to truncate.
A command that ran on somebody's machine and left no trace in the transcript would make
the turn unreadable afterwards.

## A client with no filesystem is not a bug — the gate is read twice

`clientCapabilities.fs` carries **two independent booleans**, and a read grant must never
satisfy a write.

| Where | Call | Because |
|---|---|---|
| **Parse time** | `context.allows(Gate.READ_TEXT_FILE)` / `WRITE_TEXT_FILE` / `TERMINAL` | A client that never advertised `fs` or `terminal` has done nothing wrong. The prompt is **refused before anything runs**, with a message naming the missing capability — and **without** the convention footer, because the convention was followed |
| **Call site** | `context.require(...)` | By then an unadvertised gate is *our* conformance bug: parsing should already have refused. That is exactly what `UngatedClientCallError` → `-32603` means |

Routing the ordinary "this client has no filesystem" case through `require` would have
answered `-32603` — telling a client that *we* were broken when the honest answer is that
it cannot do what it asked for. `stopReason: "refusal"` is the answer, for the same reason
a prompt naming an unopened server gets one.

Checking at parse time also keeps validate-then-run honest where it matters most:
discovering *after* a tool ran that its output has nowhere to go would leave the side
effect behind and lose the result.

`"read": {}` names no file, so it needs no capability — refusing it would refuse a request
never made. `"run": {}` is the same.

`fs` carries two independent booleans and `terminal` carries **one** covering all five
methods. Neither satisfies the other: a client that advertised a filesystem and no
terminals gets a refusal for `run`, and the message says which capability was missing.

## A file operation that fails is a failed call, not a failed turn

The `isError` rule, one layer out. A client that answers `-32603` to `fs/read_text_file`,
or drops the connection, or returns a response with no text in it, marks **that
invocation** `failed` with the client's own code and message in the tool call's content.
The remaining invocations still run and the turn ends `end_turn`; `on-tool-failure: stop`
still stops it, because a failed file call is a failed call.

Four cases, each decided rather than defaulted:

| Case | What happens | Why |
|---|---|---|
| The read fails | the tool is **never called**; no `in_progress` | its argument is missing, and calling it with a placeholder would be inventing input |
| The command fails to start, or exits non-zero | the tool is **never called**; the terminal is still released | same reason, and the failure names the exit status or signal |
| The tool reported `isError` | the write is **skipped**, and said so | a tool's error message is a diagnostic, not the document the client asked for |
| The tool returned no text content | the write is **skipped**, and said so | writing `""` is a truncation, not a write |
| The write fails | the call is `failed`, but the tool's own output is still in the update | the tool did run; only the disposal of its result did not |

The reads happen **after** the permission prompt and before `in_progress`: the client
approving the call is what authorises pulling its files, so a denied call touches nothing.
A dry run touches nothing either, and names the files it would have.

`ToolCall.locations` carries the resolved paths — the schema's own field for "which files
is this call about" — and is `None` rather than `[]` for a call that touches none.
`in_progress` re-publishes `rawInput` with the file content substituted in, which is the
only place a client can see what the tool was really called with.

## Only text blocks, and the other four are declined by name

A prompt may carry five block types. This executor reads **`text`** and declines the rest:

| Block | Governed by | Declined because |
|---|---|---|
| `text` | — always allowed | *read* — it carries the invocation |
| `image` | `promptCapabilities.image` | it needs a model to look at it |
| `audio` | `promptCapabilities.audio` | it needs a model to listen to it |
| `resource` | `promptCapabilities.embeddedContext` | it is context for a model to read |
| `resource_link` | **nothing** | this agent would have to fetch and reason about it |

All four share one reason, and it is worth saying out loud: **an image, a sound, or an
embedded document is context for a model to reason over, and decision D1 puts no model in
this runtime.** There is no defensible mapping from a picture to an MCP tool call, and
inventing one would be worse than refusing. This is a decision, not a gap — the bead that
made it says as much: "declining a block type is a legitimate outcome as long as
advertisement matches."

They are declined **by name**: a client debugging a rejected prompt is told which block
and why, rather than getting a crash, a silent drop, or a message about JSON. A declined
block takes the whole prompt with it, for the same validate-then-run reason below.

`resource_link` is the odd one out. `PromptCapabilities` has fields for image, audio, and
embeddedContext only, so **no capability governs a resource link** — a client may send one
however this agent answers `initialize`, which is why its refusal carries its own reason
rather than pointing at an advertisement.

`supported_prompt_blocks` is `{"text"}`, and
[capabilities.py](capabilities.md) derives the three `promptCapabilities` literals from
it. The advertisement therefore cannot drift from what this class reads, in either
direction.

## Validate everything, then run anything

A prompt is parsed completely before the first tool runs.

Tools have side effects. A turn that wrote two files and *then* refused because the third
block was malformed leaves no way to undo the first two, and no way to tell from the
outside that it stopped early. So a prompt that does not fully parse runs **nothing at
all** — `test_nothing_runs_when_a_later_block_fails_to_parse` is the guard.

## A prompt that is not an invocation is a refusal, not an error

`stopReason: "refusal"` exists for exactly this, and it comes with an
`agent_message_chunk` carrying the reason *and* the convention.

`CONVENTION` puts its JSON example in a **code span**, and that is load-bearing rather
than cosmetic. A client renders an `agent_message_chunk` as Markdown, where a bare
`<name>` is an HTML tag the browser deletes — so the refusal reached a user as
`{"tool": "", "arguments": {...}, "server": ""}`, advising the very shape it was refusing.
See [markdown.md](markdown.md) and `pyacp-nlv`.

A JSON-RPC error would be wrong twice over: the request was well-formed ACP, and by the
time a later block fails to parse the turn may already have emitted notifications a client
cannot un-see. A silent refusal would be worse than either.

An **empty prompt** refuses too. It names no tool, so it does not parse as an invocation,
and silently completing is exactly the failure `IdleTurnExecutor` warns about.

## Two kinds of failure, and only one of them fails the turn

| | What MCP sends | What the client sees | `stopReason` |
|---|---|---|---|
| **The tool failed** | a *successful* result with `isError: true` | `tool_call_update` with `status: "failed"` and the tool's own content | `end_turn` |
| **The backend failed** | a JSON-RPC error response | the error, backend code intact via [errors.py](errors.md) | — the request errors |

The first row is MCP's design, not an accident: tool-level failure is meant to be visible
to whatever is driving. Collapsing it into a `stopReason` would lose *which* tool failed
and why, so the remaining calls still run and the turn still ends normally — the turn
completed, one tool did not.

## What a turn emits, in order

| Order | Variant | Always? |
|---|---|---|
| 1 | `user_message_chunk`, one per text block | yes — the prompt, echoed |
| 2 | `available_commands_update` | yes, **including a turn about to be refused** |
| 3 | `agent_message_chunk` | only on a refusal, and then the turn ends |
| 4 | `plan`, all entries `pending` | only when `clientCapabilities.plan` is set |
| 5 | per call: `plan` (this entry `in_progress`) → `tool_call` → `tool_call_update` ×2 → `plan` (entry `completed`/`failed`) | plan lines gated as above |

**The echo is not redundant.** The transcript `session/load` replays is built from what a
turn *emitted*, so without it a reloaded session shows the agent talking to itself.

**The command list on a refusal is the point.** A refusal that also says what *could*
have been called is actionable; one that only says "that was not an invocation" is not.
It costs one `tools/list` per server per turn — sub-millisecond against a local
subprocess, and caching it would need `notifications/tools/list_changed` handling to stay
honest, which is `pyacp-eg1.1`'s neighbourhood.

**The plan is honest rather than aspirational.** Every invocation is validated before the
first tool runs, so the whole plan is known up front — this agent is in the unusual
position of never having to guess at one. It is re-emitted with statuses advanced after
each call, which is the protocol's own mechanism: `AgentPlanUpdate` carries the full
entry list and there is no per-entry patch.

`clientCapabilities.plan` gates the **variant**, never the `session/update` call. A
plan-less client still gets everything else — see [turns.md](turns.md).

The full disposition of all thirteen `session/update` variants — emitted, deferred, and
declined, each with a reason — is `turns.SESSION_UPDATE_DISPOSITIONS`.

## Session modes

Three, and each changes what a turn **does** — the bead is explicit that a mode with no
behavioural difference should not exist.

| Mode | Runs tools | Asks permission |
|---|---|---|
| `execute` *(default)* | yes | yes, per call |
| `dry-run` | **no** | no — nothing runs, so there is nothing to approve |
| `auto-approve` | yes | **no** — choosing the mode *is* the consent |

Declared on the executor (`session_modes`) for the same reason as
`supported_prompt_blocks`: `session/new` advertises them before any turn runs, and a mode
only means anything to the executor that acts on it. Each session gets a **deep copy**,
because `set_mode` mutates `current_mode_id` in place and the declaration is shared by
every session the executor serves.

A session whose executor advertises no modes has `modes = None` and behaves as
`execute` — the safe default is the one that asks.

### `dry-run` and the status ACP does not have

A dry run emits the `tool_call` with its `title` and `rawInput` — the arguments are the
point of a preview — and then a `tool_call_update` marked `completed` with content
`[dry-run] tools/echo was not executed.`

`completed` is a **choice**, not a claim: ACP's `ToolCallStatus` is
`pending | in_progress | completed | failed`, and none of them means "skipped". Leaving
the call `pending` would hang a client waiting for a terminal status; `failed` would say
something went wrong. So the status marks the tool-call *activity* as concluded and two
other signals say nothing ran: the content says so in words, and **`rawOutput` is
absent** — a real completion always carries the server's result.

## Config options

Two, one of each variant. The SDK discriminates the request on `type`, so an
implementation that only ever saw booleans would not have exercised the other branch —
and the same rule as the modes applies: only expose an option that changes what a turn
*does*.

| Option | Type | Default | Effect |
|---|---|---|---|
| `announce-tools` | boolean | `true` | Whether to list the session's MCP tools at the start of every turn. Off skips the `tools/list` behind it, which is the point |
| `on-tool-failure` | select | `continue` | `continue` runs the remaining calls; `stop` ends the turn at the failed one |

Declared on the executor and deep-copied per session, exactly as the modes are, and for
the same two reasons.

`stop` leaves the remaining plan entries `pending`, which is what says *where* the turn
stopped. ACP has no `stopReason` for "a tool failed" — `end_turn` is what a turn that
ended reports, and a `refusal` would claim nothing ran.

A session whose executor exposes no options takes every default.

## Permission

**Every tool call is consequential**, so every one is asked about — including the ones
the server calls read-only.

`pyacp-eg1.3` settled what a hint may and may not do, and the answer is that it changes
how the question **looks** and never whether it is **asked**. The tool call now carries a
`kind` read from the server's `readOnlyHint` / `destructiveHint` annotations
([mcp_tools.py](mcp_tools.md)), so the prompt a human answers says `delete` rather than
`other`. It does not skip the prompt, because a server asserting `readOnlyHint: true` and
thereby escaping it would be a privilege escalation written by the party being restrained
— which is MCP's own warning about annotations, in its own spec.

So `allow_always` is still what keeps the asking to once per tool per session, and a
missing, false, or unreadable annotation lands on `other` and on "ask", exactly as before
annotations were read at all.

*"But the client already chose the tool"* — the client that sent `session/prompt` and the
human at the ACP client are not necessarily the same party. Automation asks; the
permission prompt is how a person sees and approves it.

The request goes out **after** the `tool_call` notification and **before**
`in_progress`, which is what `pending` is for: the request carries the tool call, so the
client has something to attach its prompt to, and nothing has run yet.

`session/request_permission` has **no capability gate** — `ClientCapabilities` has no
field for it and every ACP client must accept it — so it is called with nothing to check
first.

### Denial is a selected option; the only other outcome is cancellation

This is the part worth reading twice. `RequestPermissionResponse.outcome` is:

| Model | Literal | Means |
|---|---|---|
| `AllowedOutcome` | `"selected"` + `optionId` | the user picked one of the options — which may be a **reject** one |
| `DeniedOutcome` | **`"cancelled"`** | the turn was cancelled while the prompt was open |

Despite the class name, `DeniedOutcome` does not mean denied. Reading it as a rejection
would turn a "no" into `stopReason: "cancelled"`, and reading a rejection as one would do
the reverse — which is the inversion this bead was told to get right.

| Answer | What happens |
|---|---|
| `allow_once` / `allow_always` | the call runs |
| `reject_once` / `reject_always` | the call does **not** run; its update is `failed` with a "Denied by the client" note, and the remaining calls still run |
| an option we never offered | treated as a refusal, and logged — the safe reading |
| outcome `cancelled` | the turn stops immediately with `stopReason: "cancelled"`, its plan entry left unfinished |

The `_always` variants are remembered on the `Session` for its lifetime and copied (not
shared) by a fork — a fork answering "always allow" must not decide for its parent. The
scope is the session because the SDK's own option is named *"Approve for session"*.

### A client that cannot ask a human is not a broken client

This was implemented the other way first, and [interop](../../docs/interop.md) corrected
it. `session/request_permission` is mandatory — `ClientCapabilities` has no field for it —
so a client answering `-32601` looked broken, and the turn refused. Then the SDK's own
`examples/client.py` turned out to answer exactly that, and so will any headless client
with no human to ask. An agent unusable against the reference client is the agent with the
problem.

**The turn proceeds, and says so once per session.** Not "assume consent from nowhere":
the client named this tool and these arguments in `session/prompt` itself, so the
authorization already exists. The prompt was a courtesy to a human who might be watching,
and a client that cannot reach one has already decided.

**That reasoning does not generalise.** An LLM-backed executor *chooses* the tool, so a
client's prompt authorizes nothing in particular and this fallback would be a hole. Any
executor added later must decide it again for itself.

### `acp.contrib.permissions` is used, with one addition

`PermissionBroker` builds the `RequestPermissionRequest` from the `ToolCallTracker` the
router already keeps, so the tool call in the prompt is the same object the client was
sent — used for the same reason as the tracker itself.

Its `default_permission_options()` offers `allow_once`, `allow_always`, and
`reject_once`. **A user can say "always yes" but not "always no"**, and is asked again
about a tool they have already turned down. That asymmetry looks like an oversight rather
than a design, and `reject_always` is one of the four kinds the protocol defines, so
`PERMISSION_OPTIONS` adds the fourth.

## Status transitions

`pending` → `in_progress` → `completed` / `failed`, as three notifications.

The first two are separate on purpose: a client renders the call the moment it is known,
and the move to `in_progress` is what tells it the wait has begun rather than the request
sitting behind something else.

`acp.contrib.tool_calls.ToolCallTracker` generates the ids and merges each partial update
into tracked state. **Used rather than hand-rolled**: the tracker makes "a
`tool_call_update` for a call that never started" impossible instead of merely unlikely,
which is the fiddly part of this variant, and unlike `acp.contrib.session_state` it
carries no experimental marker. The `external_id` indirection is the price; the router
keys it by position in the prompt.

## Where it gets its backends

Constructed with the `McpBackendRegistry`, not by reading one off `TurnContext`.
`docs/module-boundaries.md` has this module reach [mcp_registry.py](mcp_registry.md)
directly, so the context does not widen for one executor's dependency. Servers were opened
**and handshaked** during `session/new`, so every client here is live.

## Seven typed commands sit in front of the JSON convention

They are recognised before the JSON parse, in `execute`, and only when the prompt is **a
single text block**. A multi-block prompt is a composed request from a program; treating
its first block as a command would silently drop the rest, which is a much worse failure
than declining to recognise it.

| Command | What it does here | Stop reason |
| --- | --- | --- |
| `/tools` | `_list_tools`, from the turn's `ToolCatalogue` | `end_turn` |
| `/invokeTool` | `_from_command` → the same `Invocation` JSON builds | the turn's |
| `/listPrompts` | `_list_prompts` | `end_turn` |
| `/promptShow` | `_expand_prompt` → `prompts/get`, messages emitted | `end_turn` |
| `/promptInvoke` | `_expand_prompt`, which validates and then refuses | `refusal` |
| `/listResources` | `_list_resources`, two passes — `resources/list` then `resources/templates/list` | `end_turn` |
| `/resourceShow` | `_show_resource` → `resources/read` | `end_turn` |

`end_turn` rather than `refusal` for a listing: the turn did exactly what it was asked to
do, and `refusal` would be the wrong stop reason for a command that worked.

`/tools` answers from the turn's own `ToolCatalogue`, so it costs no `tools/list` beyond
the one the announcement already paid. **The other two listings have no catalogue behind
them, deliberately.** That cache exists because `tools/list` is paid three times in one
turn — by the announcement, by each tool call's `kind`, and by `/tools`. A prompt or
resource listing is asked for once, by the command that asked for it, and a cache with one
reader is a place for staleness to live.

`/invokeTool` builds **the same `Invocation`** the JSON path builds. That is the whole
design: the plan entry, the permission prompt, the session mode, the `kind` from the
server's annotations, and the on-tool-failure policy are all downstream of `Invocation`,
so a typed call inherits them without knowing they exist and cannot drift from them.

Server resolution is `_resolve_server`, shared by all four commands that name one. The
server given wins; a bare name is allowed only when the session has exactly one server,
because with several, picking the first that happens to publish the name would make the
same command mean different things as the session's servers changed. It takes a
`separator` so the suggestion it prints for an ambiguous session is one that *runs* —
`/promptShow alpha/greeting`, but `/resourceShow alpha greeting://ada`, whose URI cannot be
carved out of a slash-separated pair. See [commands.py](commands.md).

`_require_capability` runs before every prompt or resource call. MCP's rule is that a
client MUST NOT use a capability the server did not declare in `initialize`, and the
practical difference is the quality of the answer: asked anyway, a server without prompts
replies `-32601`, and `errors.py` faithfully forwards that as a JSON-RPC error naming a
method the person never typed. Reading the handshake block instead turns it into a refusal
naming the server and the thing it does not do. `mcp_stdio.py` keeps the block for this;
`supports()` is what reads it.

A server that *did* declare the capability and then fails is **not** absorbed — same rule
as `ToolCatalogue.listing`. A listing is the thing being asked for, so `MCPProtocolError`
propagates and `errors.py` forwards the backend's own code.

`available_commands(session_id)` builds the same list for `agent.py` to announce when a
session *opens* — created, forked, loaded or resumed — through the one `_commands_for`
both callers share — a
palette that disagreed with what a turn accepts would be worse than no palette. It does
not consult `announce-tools`: that option suppresses a notification whose cost is being
repeated every turn, and a client that suppressed the repetition still needs a first list
to show.

### Each tool's schema rides along in `_meta`

`input` is `UnstructuredCommandInput` — ACP's only argument shape, one free-text hint.
`tool_command_hint` reads the tool's `inputSchema` to *build* that hint and then throws the
structure away, so the client is handed a summary it cannot validate against and the user
learns the flag names and the legal enum values by trial and error.

`AvailableCommand` carries `_meta`, ACP's own extensibility point, so `_tool_meta` puts the
schema back:

```json
"_meta": {"python-acp/tool": {"server": "tools", "tool": "echo", "inputSchema": {…}}}
```

A client that reads it can render a real form — typed inputs, required markers, ranges,
enum values as a dropdown instead of a value the user has to spell correctly. A client that
ignores it sees the session it saw before, because **the hint is unchanged**. That is the
property that made this shippable ahead of any client (`pyacp-ma2`).

Three rules, and each is load-bearing:

- **Namespaced.** ACP says implementations MUST NOT make assumptions about `_meta` values,
  so a bare `inputSchema` would be a land grab on a dict every extension shares. A client
  wanting the idea without our namespace can fall back to `_meta.inputSchema`, which any
  agent may adopt.
- **Verbatim.** What `tools/list` returned, unnormalised and unreordered. The client renders
  the *server's* vocabulary, not ours, and a helpfully-rewritten schema is one more place
  for the form and `coerce_arguments` to disagree.
- **Omitted, never null.** A tool that published no `inputSchema` said *nothing*, which is
  not the same as one that published `properties: {}` and thereby said *it takes no
  parameters*. [commands.py](commands.md) already draws that line in its error messages; a
  client cannot draw it if both arrive as `"inputSchema": null`.

`server` and `tool` are carried beside the schema so no client has to reimplement the rule
that the name splits on the **first** slash.

The published contract — what a client may rely on, and what an MCP server author gets for
writing a schema worth rendering — is
[docs/tool-schema-contract.md](../../docs/tool-schema-contract.md). Keep it in step with
this section; it is the half of the channel neither population can read out of our source.

**Validation does not move.** `coerce_arguments` in [commands.py](commands.md) stays the
authority. A client-side form is a convenience and must not become a trust boundary: any
client can send any line, and a form that let this agent skip its own checks would be a
regression in exactly the direction that matters.

**Something to render it against.** The fixture's `echo` declares one required string,
which is not a form. `MOCK_MCP_SCHEMA_ZOO=1` adds a tool per JSON Schema construct to
[mock_mcp_server.py](../../tests/fixtures/mock_mcp_server.py) (`pyacp-6kz`) — every type
bare, string and numeric constraints, the three ways to spell a choice, arrays,
`dependentRequired`, nesting; the four conditional constructs a client is expected to
*decline* (`if`/`then`/`else`, `dependentSchemas`, `allOf`, discriminated `oneOf`), each
its own tool so each fallback can be looked at separately; and both edges — `properties:
{}` against no `inputSchema` at all. `zoo-all-of` and `zoo-one-of` are the argument for
this whole section in one line: their properties live inside the composition keyword, so
the hint can only say `(no parameters)` while the schema shows two. Every zoo tool echoes
its arguments back as JSON, so
what `coerce_arguments` produced is visible rather than assumed. `make run` prints the
demo server command; add the variable to its `env` in `session/new`.

**The cost, measured** (`pyacp-ma2`, serialised `available_commands_update`, compact JSON):

| Tools announced | Without `_meta` | With | Per tool |
| --- | ---: | ---: | ---: |
| The repo fixture's three | 1,428 B | 1,797 B | ~+120 B |
| Three realistic tools — descriptions, enums, defaults, bounds | 1,702 B | 3,595 B | ~+630 B |

A schema-carrying entry is roughly **4.5× its bare self**, and this notification is
re-announced *every turn*. It is shipped ungated anyway: ~630 B per tool puts a 20-tool
session near 13 KB per turn, which no transport here notices. The two escapes exist if that
ever stops being true — send `_meta` only in the once-per-session announcement, or gate it
on a client capability in `clientCapabilities._meta` — but the first makes the per-turn list
disagree with the session's, and neither is worth buying before someone is actually paying.

**The palette carries the verbs, and no individual prompt or resource** (`pyacp-tc5`). MCP
keeps tools, prompts and resources in three separate namespaces, so one server may legally
publish a tool and a prompt both called `greeting`; per-item entries would need a naming
rule to keep those apart, and the entry that lost the coin toss would silently shadow the
other. `/listPrompts` and `/listResources` answer the same question without inventing one.

The parsing, typing and rendering live in [commands.py](commands.md), which has the
coercion table, the reasoning behind the one row that guesses, and why a prompt's arguments
need no table at all.

## Main symbols

| Symbol | Purpose |
|---|---|
| `McpToolRouterExecutor(backends)` | The executor. `agent.py`'s default |
| `Invocation` | One parsed call: `tool`, `arguments`, `server`, `title`, `reads`, `write`, `locations` |
| `FileRead` / `FileWrite` | One file to read into an argument, and where the output goes. Both carry the **resolved** path |
| `CommandRun` | One command to run in a client terminal, into one tool argument — `FileRead`'s mirror. `output_byte_limit` is never absent |
| `Session.running_tool_call` (written here) | Set to the live `toolCallId` for the length of each `tools/call` and cleared in `finally`, so a server's `elicitation/create` can name the call it interrupted. See [elicitation.md](elicitation.md) |
| `PromptConventionError` | A prompt this executor will not run. Caught by `execute` and turned into a refusal; a `ValueError` so a future caller that let it escape gets `-32602`. `explains_convention` says whether the refusal appends `CONVENTION` |
| `UnsupportedByClientError` | The prompt correctly asked for a client method the client never advertised. A refusal, **not** an `UngatedClientCallError` |
| `CONVENTION` | The explanation appended to every refusal |
| `DECLINED_BLOCKS` | Each non-text block type and why it is refused |
| `_BUILTIN_COMMANDS` | The seven commands this executor answers itself, in announcement order. Verbs only — no per-prompt or per-resource entry |
| `_resolve_server(verb, server, target, backends, separator)` | Which server a command goes to, and the runnable suggestion when a session has several |
| `_require_capability(verb, server, backend, capability)` | Refuses a prompt or resource command the server's own handshake says it cannot answer |
| `McpToolRouterExecutor._catalogue` | Every server's `prompts/list` or `resources/list`, plus the servers that declared no such capability. Two values, because "publishes none" and "does not implement it" want different reactions |
| `McpToolRouterExecutor._resource_templates` | The second pass `/listResources` makes, over exactly the servers `_catalogue` asked. MCP publishes a URI template through `resources/templates/list` alone, so a listing without it reports a template-only server as empty (`pyacp-as5`). `-32601` becomes an empty section — templates are optional *within* the `resources` capability — and every other code still propagates |
| `TOOL_META_KEY` | `"python-acp/tool"` — the `_meta` namespace a per-tool `AvailableCommand` publishes its schema under |
| `_tool_meta(server, name, tool)` | That block: `server`, `tool`, and `inputSchema` verbatim when the tool published one |
| `PERMISSION_OPTIONS` | The four options offered before every tool call |
| `SESSION_MODES` | The three modes, and `EXECUTE` / `DRY_RUN` / `AUTO_APPROVE` for their ids |
| `SESSION_CONFIG_OPTIONS` | The two config options, and `ANNOUNCE_TOOLS` / `ON_TOOL_FAILURE` |
| `McpToolRouterExecutor.supported_prompt_blocks` | `{"text"}` — what `promptCapabilities` is derived from |
| `McpToolRouterExecutor.session_modes` / `.session_config_options` | The modes and options `session/new` advertises, declared here because only this executor acts on them |

## What later beads own

- `pyacp-eg1.1` ✔ — the MCP-result mapping now lives in [mcp_content.py](mcp_content.md)
  and covers all five MCP content types plus annotations. Unmappable content became a
  **visible placeholder** rather than a skip: once the mapping claims to be complete, a
  silent skip makes a client render "the tool did nothing" instead of "we could not show
  this".
- `pyacp-hnk.3` ✔ — content-block typing and the `promptCapabilities` gates. Settled
  above: text only, the rest declined by name, and the literals derived from
  `supported_prompt_blocks`.
- `pyacp-hnk.4` — the rest of the `session/update` variant set.
- `pyacp-8bv.2` ✔ — `fs/read_text_file` and `fs/write_text_file`, above. It inherited
  `paths.md`'s open TOCTOU question and **could not close it**: this module never opens a
  file, so `O_NOFOLLOW` has nothing to attach to here.
- `pyacp-hnk.5` ✔ — the `stopReason` contract. There is no fourth reason to add: the two
  this executor never returns are limits on a model, and it has none. See the table in
  [turns.md](turns.md).

## Tests

`tests/test_turn_mcp_router.py`, against the real `tests/fixtures/mock_mcp_server.py`
subprocess: what is under test is a tool call actually running and its result actually
reaching a `session/update`, and a mock backend would prove neither. The parsing tests are
exhaustive because the convention is invented here — every refusal it can produce is part
of the contract.

## Related

- [turns.py docs](turns.md) — the seam this implements
- [mcp_registry.py docs](mcp_registry.md) — where the backends come from
- [agent.py docs](agent.md) — what runs the turn as a cancellable task
