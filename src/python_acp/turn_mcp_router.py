"""The shipped default turn executor: prompt blocks in, MCP tool calls out.

Decision D3 says `session/prompt` runs behind a swappable executor, and D1 says there is
no LLM in this runtime. So the default cannot *interpret* a prompt — it can only route
one. A client says which tool to run and with what; this executes it against the
session's MCP backends, streams the tool call's real status transitions back as
`session/update`, and returns.

Nothing here reasons, plans, or retries.

## The invocation convention

**Invented here.** The ACP spec says what a prompt *is* — a list of content blocks — and
nothing about how a block names a tool, because every other agent has a model to work
that out. With no model, the contract has to be explicit, so it is the one thing in this
module a client codes against:

```json
{"type": "text", "text": "{\\"tool\\": \\"echo\\", \\"arguments\\": {\\"text\\": \\"hi\\"}}"}
```

* A **text** content block whose entire text is a JSON object.
* `tool` — required, the MCP tool name.
* `arguments` — optional object, defaults to `{}`.
* `server` — the MCP server name from `session/new`'s `mcpServers`. Optional **only**
  when the session opened exactly one server; required when it opened several, because
  guessing which of two servers a client meant is the kind of help nobody wants.
* `read` and `write` — optional, and both go through the *client's* filesystem methods.
  See "Files move through the client" below.
* `edit` — optional, a structured path-addressed change to one file, verified before it
  is written. See "An edit is the one place this agent authors bytes" below.
* `run` — optional, and goes through the *client's* terminals. See "Commands run on the
  client's machine" below.

Explicit `server`/`tool` fields rather than a single `"server/tool"` string: both names
are arbitrary and may contain a slash, so a separator would be ambiguous exactly where
being wrong is silent.

Every text block in the prompt is one invocation, run **in order**.

## Validate everything, then run anything

A prompt is parsed completely before the first tool runs. Tools have side effects — a
turn that wrote two files and *then* refused because the third block was malformed leaves
no way to undo the first two, and no way to tell from the outside that it stopped early.
So a prompt that does not fully parse runs nothing at all.

## A prompt that is not an invocation is a refusal, not an error

`stopReason: "refusal"` exists for exactly this, and it comes with an
`agent_message_chunk` explaining the convention. A JSON-RPC error would be wrong twice
over: the request was well-formed ACP, and by the time a later block fails to parse the
turn may already have emitted notifications a client cannot un-see.

An empty prompt refuses too. It names no tool, so it does not parse as an invocation, and
silently completing is the failure `IdleTurnExecutor` warns about.

## A failed tool does not fail the turn

MCP reports tool-level failure as a **successful** result carrying `isError: true`. The
tool call's `session/update` says `status: "failed"` and carries the tool's own content,
the remaining calls still run, and the turn returns `end_turn` — the turn completed, one
tool did not. Collapsing that into a `stopReason` would lose which tool failed and why.

A *protocol* failure is different: `MCPProtocolError` propagates and `errors.py` maps it,
backend code intact.

## Files move through the client, never through this process

`fs/read_text_file` and `fs/write_text_file` are **client** methods — an ACP agent calls
them, it does not serve them. This executor never opens a file itself, so the client stays
in control of what is read and written. Two optional keys extend the invocation:

```json
{"tool": "echo",
 "read": {"text": {"path": "/abs/in.md", "line": 1, "limit": 40}},
 "write": {"path": "/abs/out.md"}}
```

* `read` maps an **argument name** to a file. The file's content becomes that argument's
  value before the tool runs. `line` (1-based) and `limit` are passed straight through, so
  a client asks for a window rather than always paying for a whole file. An argument named
  in both `read` and `arguments` is refused rather than silently overridden — two sources
  for one value is exactly the kind of guess `server` already refuses to make.
* `write` names where the tool's **text** output goes, after it runs.

Both paths must be absolute and inside the session's declared roots. `paths.py` owns that
rule (`pyacp-3rw.4`), and the **resolved** path it returns is what goes on the wire: the
string the client wrote may traverse a symlink the check never followed.

**That containment check is still a check and not a lock, and here it cannot become one.**
`paths.md` records the TOCTOU window and says closing it needs `openat`/`O_NOFOLLOW` "with
the code doing the opening — Phase 4.2". This *is* Phase 4.2, and it does not open
anything: the client does. The lever does not exist on this side of the wire. Sending the
resolved path narrows the window — the client is not asked to re-walk the links we already
walked — and that is the whole of what this module can do about it.

## An edit is the one place this agent authors bytes

Every other directive is neutral carriage: `read` puts a client-named file into a tool
argument, `write` puts the *tool's own* output into a file. If those bytes are wrong it is
the tool's fault and the transcript proves it. `edit` is different — the agent reads the
file, computes a splice, and writes back a result no tool produced. **The bytes are ours.**

That trade is decided in `ARCHITECTURE.md` ("Structured edits, and the neutrality this
deliberately trades away") and paid for with a proof: [edits.py](edits.md) refuses to
return a result it cannot verify, ending in *every byte outside the addressed spans is
unchanged*.

```json
{"tool": "render-changelog",
 "arguments": {"since": "v1.2"},
 "edit": {"path": "/abs/CHANGELOG.md",
          "format": "markdown",
          "ops": [{"kind": "set", "address": "/# Changes/## Unreleased",
                   "fromOutput": true}]}}
```

* `path` — absolute and inside the session's roots, like every other path here.
* `format` — `json`, `markdown`, or `yaml`, **named and never sniffed from the
  extension**. A `.yml` full of Go template directives is not YAML and a `.tf.json` is
  JSON; `edits.apply` refuses to guess and so does this.
* `ops` — a non-empty list of `{"kind": "set"|"insert"|"delete"|"append", "address": ...}`.
  The address is an RFC 6901 pointer, which for Markdown is a heading path
  (`/# Install/## macOS`, `#` markers included) and for JSON an ordinary pointer.

Every op but `delete` names **exactly one** value source, and `delete` names none:

* `"value"` — raw source text in the target format. Not a JSON value: an object would need
  a serialiser per format, which is the emitter this design exists without.
* `"scalar"` — a JSON scalar, rendered by the format's scalar-only renderer, for the
  common case where quoting a string correctly is the only hard part.
* `"fromOutput": true` — the tool's own text output. **This is what makes `edit` the
  precise sibling of `write`**: the same bytes, spliced at an address instead of over the
  whole file.

`write` and `edit` in one block is refused. That is two destinations for one tool's
output, which is the same guess `server` already declines to make.

Three rules that are not obvious and are load-bearing:

* **The whole file is read** — no `line`/`limit` window. The verifier asserts something
  about every untouched byte, and a window would make that assertion about a fragment
  while the write replaces a file.
* **An edit refusal is a failed tool call**, not a JSON-RPC error, and not a failed turn.
  The turn is fine; one operation in it was refused.
* **`_write_file`'s empty-content guard is not inherited, and no shrink heuristic replaces
  it.** That guard exists because tool output is unverified. An edit result carries a
  proof, so emptying one addressed value is an ordinary edit — and a heuristic on top of a
  proof adds no information while eventually blocking a correct delete.

## Commands run on the client's machine, and are always given back

`terminal/*` is the same arrangement as `fs/*`: five **client** methods an agent calls and
never serves. A third optional key uses them:

```json
{"tool": "summarise",
 "run": {"log": {"command": "git", "args": ["log", "--oneline", "-5"]}}}
```

`run` maps an **argument name** to a command, exactly as `read` maps one to a file: the
command's captured output becomes that argument's value before the tool runs. An argument
named twice — in `arguments`, in `read`, or in another `run` — is refused, for the same
reason two sources for one value always are.

Four of the five methods are used on the ordinary path (`create` → `wait_for_exit` →
`output` → `release`) and `kill` is used on the cancelled one. **A created terminal is
released on every path out of `_capture`**, including the one where the turn is torn out
mid-command: that release runs under `asyncio.shield`, because a cancellation is already
in flight and unshielded cleanup would be cancelled before it could send anything.

A command that exits non-zero, or dies on a signal, means the tool is **not called** —
the same asymmetry as a failed read, and for the same reason: its argument would have to
be invented. `terminals.py` owns the tracking, the `outputByteLimit` default, and what
happens to a terminal when the client disconnects.

## Gates are read twice, deliberately

A client that never advertised `fs.readTextFile` is not a bug, ours or theirs. So the
gate is checked at **parse** time with `context.allows(...)`, and a prompt asking for a
file operation the client cannot do is **refused before anything runs** — the same answer,
and for the same reason, as a prompt naming a server the session never opened.

`context.require(...)` is then used at the call site as an assertion, because by that point
an unadvertised gate really would be our conformance bug: parsing should have refused the
turn. That is exactly what `UngatedClientCallError` -> `-32603` means, and routing the
ordinary "this client has no filesystem" case through it would have told the client that
*we* were broken.

Checking at parse time rather than at the call also keeps validate-then-run honest in the
one place it matters most: discovering after a tool ran that its output has nowhere to go
would leave the side effect and lose the result.

## A file operation that fails is a failed call, not a failed turn

Same rule as `isError`, one layer out. A client that answers `-32603` to
`fs/read_text_file` — or raises anything else — marks that invocation `failed` with the
client's own words in the tool call's content, and the remaining invocations still run.
The turn ends `end_turn`; `on-tool-failure: stop` still stops it.

Three asymmetries worth stating:

* **A read failure means the tool never runs.** Its argument is missing, and calling it
  with a placeholder would be inventing input.
* **A command that fails means the tool never runs**, for the same reason. Its terminal is
  still released, and the failure names the exit status.
* **A write is skipped when the tool failed**, and when the result carries no text
  content. Writing a tool's error message — or truncating a file to nothing because the
  tool answered with an image — is worse than not writing, and the update says which
  happened.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from acp.contrib.permissions import PermissionBroker, default_permission_options
from acp.contrib.tool_calls import ToolCallTracker
from acp.exceptions import RequestError
from acp.helpers import (
    ToolCallLocation,
    plan_entry,
    text_block,
    tool_content,
    update_agent_message,
    update_agent_message_text,
    update_available_commands,
    update_plan,
    update_user_message_text,
)
from acp.schema import (
    AvailableCommand,
    AvailableCommandInput,
    UnstructuredCommandInput,
    EnvVariable,
    PermissionOption,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionMode,
    SessionModeState,
    PlanEntry,
    RequestPermissionRequest,
    RequestPermissionResponse,
)

from python_acp.edit_docs import DOCS_DIALECT
from python_acp.edit_json import JSON_DIALECT
from python_acp.edit_yaml import YAML_DIALECT
from python_acp.edits import UNSET, Dialect, EditError, Op, OpKind
from python_acp.edits import apply as apply_edits
from python_acp.mcp_content import to_content_block, to_edit_content, to_tool_call_content
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPStdioClient
from python_acp.mcp_tools import ToolCatalogue
from python_acp.commands import (
    INVOKE_TOOL,
    LIST_PROMPTS,
    LIST_PROMPTS_HINT,
    LIST_RESOURCES,
    LIST_RESOURCES_HINT,
    LIST_TOOLS,
    LIST_TOOLS_HINT,
    NEEDS_A_MODEL,
    PROMPT_INVOKE,
    PROMPT_INVOKE_HINT,
    PROMPT_SHOW,
    PROMPT_SHOW_HINT,
    RESOURCE_SHOW,
    RESOURCE_SHOW_HINT,
    Command,
    CommandError,
    InvokePrompt,
    InvokeTool,
    ListPrompts,
    ListResources,
    ListTools,
    PromptCommand,
    ShowResource,
    coerce_arguments,
    invocation_prefix,
    parse_command,
    positional_argument_error,
    prompt_arguments,
    prompt_message_blocks,
    render_prompt_heading,
    render_prompt_listing,
    render_resource_contents,
    render_resource_listing,
    render_tool_listing,
    tool_command_hint,
)
from python_acp.markdown import code_span
from python_acp.mcp_stdio import MCPProtocolError
from python_acp.paths import PathConstraintError, require_contained
from python_acp.terminals import DEFAULT_OUTPUT_BYTE_LIMIT, TerminalRegistry
from python_acp.turns import Gate, TurnContext, TurnResult

logger = logging.getLogger(__name__)

#: JSON-RPC's "no such method". Named because it is read as a *value* here — the one
#: backend failure `/listResources` absorbs rather than forwards.
_METHOD_NOT_FOUND = -32601

#: The shape a prompt block must have, said once so every refusal says it the same way.
#:
#: The JSON example is a **code span**, not bare text. A client renders this message as
#: Markdown, and bare `<name>` is an HTML tag it discards silently — the refusal reached a
#: user as `{"tool": "", "arguments": {...}, "server": ""}`, advising a shape that is not
#: the shape. See `markdown.py` and `pyacp-nlv`.
CONVENTION = (
    "Each text block must be a JSON object naming an MCP tool: "
    + code_span('{"tool": "<name>", "arguments": {...}, "server": "<name>"}')
    + '. "arguments" defaults to {}; "server" may be omitted only when the session '
    "opened exactly one MCP server."
)


class _TurnCancelled(Exception):
    """The client cancelled the turn while its permission prompt was open.

    Internal to this module: `execute` turns it into `stopReason: "cancelled"`. Not an
    `asyncio.CancelledError`, because nothing was actually cancelled — the client
    answered, and answering "cancelled" is a normal response to a normal request.
    """


class _ClientCallFailed(Exception):
    """A client method this invocation depended on did not answer usefully.

    Covers both directions the client is asked to work in: an `fs/*` call that failed,
    and a `terminal/*` command that could not be started or exited non-zero.

    Internal to this module, like `_TurnCancelled`: `_run` turns it into a failed tool
    call rather than letting it end the turn. Deliberately **not** a `RequestError` and
    deliberately not raised past `_run` — a client that cannot read one file, or a command
    that failed, has not made the turn unanswerable, and the remaining invocations still
    have work to do.
    """


class PromptConventionError(ValueError):
    """A prompt this executor will not run, refused before anything runs.

    Usually a block that is not an invocation. Never reaches the wire as an error:
    `execute` catches it and refuses the turn with an explanation instead. It is a
    `ValueError` anyway so that a future caller which lets it escape gets `-32602` rather
    than `-32603` — the prompt is a parameter.
    """

    #: Whether the refusal message should append `CONVENTION`. True for a prompt that got
    #: the convention wrong; false when the prompt was written correctly and the answer is
    #: still no, where restating the convention would only misdirect.
    explains_convention: bool = True


class CommandRefused(PromptConventionError):
    """A `/tools` or `/invokeTool` that was recognised and then found to be wrong.

    Refused on the same path as any other bad prompt, but with the JSON convention footer
    suppressed: someone who typed a slash command is not reaching for the JSON convention,
    and `commands.py` has already said what the right syntax is.
    """

    explains_convention = False


class UnsupportedByClientError(PromptConventionError):
    """The prompt asks for a client method this client never advertised.

    A refusal rather than an `UngatedClientCallError`: that exception maps to `-32603` and
    means *we* reached for something we never checked, which would be the wrong story to
    tell a client whose only sin is having no filesystem. The prompt was well-formed; this
    agent simply cannot carry it out here.

    It is a `PromptConventionError` so `execute` refuses it on the same path, and it
    suppresses the convention footer because the convention was followed.
    """

    explains_convention = False


@dataclass(frozen=True)
class FileRead:
    """One file to read through the client, into one tool argument.

    `path` is the **resolved** path `require_contained` handed back, not the string the
    client wrote: re-deriving from the original would hand the client a path this session
    never checked.
    """

    argument: str
    path: str
    line: int | None = None
    limit: int | None = None


@dataclass(frozen=True)
class FileWrite:
    """Where a tool's text output goes, written through the client. Resolved, as above."""

    path: str


#: The formats an `edit` directive may name, and the only ones. Named rather than
#: sniffed from the path's extension: a `.yml` full of Go template directives is not
#: YAML and a `.tf.json` is JSON, so `edits.apply` refuses to guess and so does this.
#: See `edits.py`'s `apply` docstring — the caller knows; it has to say.
DIALECTS: dict[str, Dialect] = {
    JSON_DIALECT.name: JSON_DIALECT,
    DOCS_DIALECT.name: DOCS_DIALECT,
    YAML_DIALECT.name: YAML_DIALECT,
}

#: An op's value comes from exactly one of these. Kept as a tuple so the refusal can
#: name all three in the order the doc introduces them.
VALUE_SOURCES = ("value", "scalar", "fromOutput")


@dataclass(frozen=True)
class EditOp:
    """One op from the prompt, before the tool has run.

    Not an `edits.Op` yet, because an op that takes its value from the tool's output has
    no value until the tool answers and `Op.__post_init__` requires one. So the prompt's
    three value spellings survive to `resolve`, which builds the real op.
    """

    kind: OpKind
    address: str
    value: str | None = None
    scalar: Any = UNSET
    from_output: bool = False

    def resolve(self, output: str) -> Op:
        """The `edits.Op` this becomes once the tool's text output is known."""
        if self.from_output:
            return Op(kind=self.kind, address=self.address, value=output)
        return Op(kind=self.kind, address=self.address, value=self.value, scalar=self.scalar)


@dataclass(frozen=True)
class FileEdit:
    """A structured edit to make to one file after the tool runs. Resolved path, as above.

    `dialect` is the object, not its name: the name was validated at parse time and
    re-looking it up at the call would be a second chance to get it wrong.
    """

    path: str
    dialect: Dialect
    ops: tuple[EditOp, ...]


@dataclass(frozen=True)
class CommandRun:
    """One command to run in a client terminal, into one tool argument.

    The mirror of `FileRead`: a source for an argument that this process cannot produce
    itself. `env` is a tuple of pairs rather than a dict so the record stays hashable and
    frozen like every other parsed thing here.

    `output_byte_limit` is not optional. It defaults to `terminals.DEFAULT_OUTPUT_BYTE_LIMIT`
    and a client may name a smaller or larger one, but never *no* limit — see
    `terminals.md` for what unbounded output does to the request this output ends up in.
    """

    argument: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    output_byte_limit: int = DEFAULT_OUTPUT_BYTE_LIMIT

    @property
    def display(self) -> str:
        """The command as a client would read it back, for notes and refusals."""
        return " ".join((self.command, *self.args))


@dataclass(frozen=True)
class Invocation:
    """One parsed tool call, before anything has run."""

    tool: str
    arguments: dict[str, Any]
    server: str | None = None
    reads: tuple[FileRead, ...] = ()
    write: FileWrite | None = None
    edit: FileEdit | None = None
    runs: tuple[CommandRun, ...] = ()

    @property
    def title(self) -> str:
        """What a client shows for this call.

        Always qualified by server, even when the client omitted it because the session
        had only one. The title outlives the turn — it is in the transcript
        `session/load` replays — and "which server ran this" is not recoverable later
        from an unqualified name.
        """
        return f"{self.server}/{self.tool}" if self.server else self.tool

    @property
    def locations(self) -> list[ToolCallLocation] | None:
        """The files this call touches, for `ToolCall.locations`.

        The schema has a field for exactly this and it is what lets a client show — or
        follow — which files a tool call is about. `None` rather than `[]` when there are
        none, so a call that touches nothing does not claim an empty list of locations.
        """
        found = [ToolCallLocation(path=read.path, line=read.line) for read in self.reads]
        if self.write is not None:
            found.append(ToolCallLocation(path=self.write.path))
        if self.edit is not None:
            found.append(ToolCallLocation(path=self.edit.path))
        return found or None


#: Mode ids, so a branch reads as a name rather than a string literal.
EXECUTE = "execute"
DRY_RUN = "dry-run"
AUTO_APPROVE = "auto-approve"

#: The modes this executor offers. Each one changes what a turn *does*; the bead is
#: explicit that a mode with no behavioural difference should not exist.
#:
#: | Mode | Runs tools | Asks permission |
#: |---|---|---|
#: | `execute` (default) | yes | yes, per call |
#: | `dry-run` | **no** | no — nothing runs, so there is nothing to approve |
#: | `auto-approve` | yes | **no** — choosing the mode *is* the consent |
SESSION_MODES = SessionModeState(
    currentModeId=EXECUTE,
    availableModes=[
        SessionMode(id=EXECUTE, name="Execute", description="Run each tool, asking first."),
        SessionMode(
            id=DRY_RUN,
            name="Dry run",
            description="Report which tools would run, with their arguments, and run none.",
        ),
        SessionMode(
            id=AUTO_APPROVE,
            name="Auto-approve",
            description="Run each tool without asking. Choosing this mode is the consent.",
        ),
    ],
)


#: Config option ids.
#: The commands this executor answers itself, announced beside the session's MCP tools so
#: a client's palette shows them without being taught. `input` is ACP's only argument
#: shape -- `UnstructuredCommandInput`, a single free-text hint -- so the syntax is a
#: display string rather than anything the client can validate. See `commands.py`.
#:
#: **The verbs are here; individual prompts and resources are not** (`pyacp-tc5`). MCP
#: keeps tools, prompts and resources in three separate namespaces, so one server may
#: legally publish a tool and a prompt both called `greeting`; per-item palette entries
#: would need a naming rule to keep those apart, and the entry that lost the coin toss
#: would silently shadow the other. `/listPrompts` and `/listResources` answer the same
#: question without inventing one.
#:
#: **`/invokeTool` is recognised and not announced** (`pyacp-b50`). It existed to reach a
#: tool the long way round; since `pyacp-acn` every tool is announced under its own name,
#: so `/alpha/echo --text hi` is the ordinary way to call one and a palette entry for the
#: verb teaches a detour past the thing a client already shows. It is not *removed* — it
#: is the escape hatch the sugar cannot cover, for a tool whose name contains a slash and
#: for the bare `/invokeTool echo --text x` a single-server session allows — and a prompt
#: is free text, so it still works when typed. `commands.UNANNOUNCED_COMMANDS` names it,
#: and a test asserts these two sets partition `COMMAND_NAMES`.
#:
#: `/tools` stays in both. It prints every parameter with its type, required flag and
#: description, where a palette entry carries a name and one hint line; they answer
#: different questions, and every listing footer points at it.
_BUILTIN_COMMANDS: tuple[AvailableCommand, ...] = (
    AvailableCommand(
        name=LIST_TOOLS,
        description="List this session's MCP tools with their parameters",
        input=AvailableCommandInput(root=UnstructuredCommandInput(hint=LIST_TOOLS_HINT)),
    ),
    AvailableCommand(
        name=LIST_PROMPTS,
        description="List this session's MCP prompts with their arguments",
        input=AvailableCommandInput(root=UnstructuredCommandInput(hint=LIST_PROMPTS_HINT)),
    ),
    AvailableCommand(
        name=PROMPT_SHOW,
        description="Expand one prompt and show the messages it returns",
        input=AvailableCommandInput(root=UnstructuredCommandInput(hint=PROMPT_SHOW_HINT)),
    ),
    AvailableCommand(
        name=PROMPT_INVOKE,
        description="Expand one prompt and act on it — needs a model, so it refuses here",
        input=AvailableCommandInput(root=UnstructuredCommandInput(hint=PROMPT_INVOKE_HINT)),
    ),
    AvailableCommand(
        name=LIST_RESOURCES,
        description="List this session's MCP resources",
        input=AvailableCommandInput(root=UnstructuredCommandInput(hint=LIST_RESOURCES_HINT)),
    ),
    AvailableCommand(
        name=RESOURCE_SHOW,
        description="Read one resource and show its contents",
        input=AvailableCommandInput(root=UnstructuredCommandInput(hint=RESOURCE_SHOW_HINT)),
    ),
)

ANNOUNCE_TOOLS = "announce-tools"
ON_TOOL_FAILURE = "on-tool-failure"

#: What a client may change about how a turn runs. Same rule as the modes: only expose an
#: option that changes what a turn *does*.
#:
#: One of each variant, deliberately — the SDK discriminates the request on `type`, and an
#: implementation that only ever saw booleans would not have exercised the other branch.
SESSION_CONFIG_OPTIONS: tuple[Any, ...] = (
    SessionConfigOptionBoolean(
        type="boolean",
        id=ANNOUNCE_TOOLS,
        name="Announce available tools",
        description=(
            "List the session's MCP tools at the start of every turn. Turning it off "
            "saves the notification, not every tools/list: a turn still lists the "
            "servers it calls, because a tool call's kind comes from their annotations."
        ),
        currentValue=True,
    ),
    SessionConfigOptionSelect(
        type="select",
        id=ON_TOOL_FAILURE,
        name="On tool failure",
        description="What to do when a tool reports isError.",
        currentValue="continue",
        options=[
            SessionConfigSelectOption(
                value="continue", name="Continue", description="Run the remaining calls."
            ),
            SessionConfigSelectOption(
                value="stop", name="Stop", description="End the turn at the failed call."
            ),
        ],
    ),
)


#: What a client is offered before a tool runs.
#:
#: The SDK's `default_permission_options()` plus one. It offers `allow_once`,
#: `allow_always`, and `reject_once` — so a user can say "always yes" but has no way to
#: say "always no", and is asked again about a tool they have already turned down. The
#: asymmetry looks like an oversight rather than a design, and `reject_always` is one of
#: the four `PermissionOptionKind`s the protocol defines, so the fourth option is added
#: here rather than worked around.
PERMISSION_OPTIONS: tuple[PermissionOption, ...] = (
    *default_permission_options(),
    PermissionOption(optionId="reject_for_session", name="Reject for session", kind="reject_always"),
)

#: Which of those options mean "run it". `reject_once` / `reject_always` are the other two.
_ALLOWING_KINDS = frozenset({"allow_once", "allow_always"})

#: Sentinel key in `Session.remembered_permissions` recording that we have already told
#: this session's client we are proceeding without it. Not a tool name, and cannot collide
#: with one: every real key is `server/tool`.
_NO_HUMAN_KEY = "\x00 no permission channel"

#: Which of them mean "and do not ask again this session".
_REMEMBERING_KINDS = frozenset({"allow_always", "reject_always"})


#: Why each non-text prompt block is declined, keyed by its `type` discriminator.
#:
#: Every one is declined for the **same** reason and it is worth saying out loud: an
#: image, a sound, or an embedded document is context for a model to reason over, and
#: decision D1 puts no model in this runtime. There is no defensible mapping from a
#: picture to an MCP tool call, and inventing one would be worse than refusing.
#:
#: `resource_link` is the odd one: `PromptCapabilities` has fields for image, audio, and
#: embeddedContext only, so **no capability governs a resource link** and a client may
#: send one however this agent answers `initialize`. Refusing it needs its own reason
#: rather than an advertisement, which is why it is here.
DECLINED_BLOCKS: dict[str, str] = {
    "image": "an image, which needs a model to look at it",
    "audio": "audio, which needs a model to listen to it",
    "resource": "an embedded resource, which is context for a model to read",
    "resource_link": (
        "a link to a resource, which this agent would have to fetch and reason about"
    ),
}


class McpToolRouterExecutor:
    """Runs the tool calls a prompt names, against that session's MCP backends.

    Constructed with the backend registry rather than reading it off `TurnContext`:
    `docs/module-boundaries.md` has this module reach `mcp_registry.py` directly, so the
    context does not have to widen for one executor's dependency.
    """

    #: Text only, and that is what `initialize` therefore advertises — `capabilities.py`
    #: derives `promptCapabilities.image`, `.audio`, and `.embeddedContext` from this set,
    #: so the advertisement cannot drift from what this class actually reads.
    supported_prompt_blocks: frozenset[str] = frozenset({"text"})
    session_modes: SessionModeState | None = SESSION_MODES
    session_config_options: tuple[Any, ...] = SESSION_CONFIG_OPTIONS

    def __init__(
        self, backends: McpBackendRegistry, terminals: TerminalRegistry | None = None
    ) -> None:
        self._backends = backends
        # Defaulted rather than required, unlike `backends`. A private registry still
        # tracks and releases everything a turn creates; what it cannot do is let
        # `session/close` reach a terminal from outside the turn, which is why `agent.py`
        # passes the process-wide one. See `terminals.md`.
        self._terminals = terminals if terminals is not None else TerminalRegistry()

    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult:
        backends = self._backends.backends(context.session_id)
        # One `tools/list` per server per turn at most, shared by the announcement and by
        # the tool-call `kind` each invocation is labelled with. See `mcp_tools.py`.
        catalogue = ToolCatalogue(backends)
        await self._echo_prompt(context, prompt)
        await self._announce_tools(context, backends, catalogue)

        try:
            command = _command_in(prompt)
            if isinstance(command, ListTools):
                return await self._list_tools(context, backends, catalogue)
            if isinstance(command, ListPrompts):
                return await self._list_prompts(context, backends)
            if isinstance(command, ListResources):
                return await self._list_resources(context, backends)
            if isinstance(command, ShowResource):
                return await self._show_resource(context, backends, command)
            if isinstance(command, PromptCommand):
                return await self._expand_prompt(context, backends, command)
            if isinstance(command, InvokeTool):
                invocations = [await self._from_command(command, backends, catalogue)]
            else:
                invocations = self._parse(context, prompt, backends)
        except PromptConventionError as exc:
            return await self._refuse(context, exc)

        plan = _plan_for(invocations)
        await self._emit_plan(context, plan)
        tracker = ToolCallTracker()
        broker = PermissionBroker(
            context.session_id,
            _requester(context),
            tracker=tracker,
            default_options=PERMISSION_OPTIONS,
        )
        for index, invocation in enumerate(invocations):
            plan[index].status = "in_progress"
            await self._emit_plan(context, plan)
            try:
                failed = await self._run(
                    context, tracker, broker, backends, catalogue, invocation, index
                )
            except _TurnCancelled:
                # The client cancelled while its permission prompt was open. It said so in
                # the response, which is a different route to the same answer as
                # `session/cancel` cancelling our task — and the only one available when
                # the client chose not to send the notification too.
                plan[index].status = "pending"
                await self._emit_plan(context, plan)
                return TurnResult.cancelled()
            plan[index].status = "failed" if failed else "completed"
            await self._emit_plan(context, plan)
            if failed and _option(context, ON_TOOL_FAILURE, "continue") == "stop":
                # The turn ends here. The remaining plan entries stay `pending`, which is
                # what says *where* it stopped — ACP has no stopReason for "a tool failed",
                # and inventing a refusal would claim nothing ran.
                logger.info(
                    "Stopping turn for %s after a failed tool: on-tool-failure=stop",
                    context.session_id,
                )
                return TurnResult.ended()
        return TurnResult.ended()

    # ------------------------------------------------------------------
    # What happened, before and around the tools
    # ------------------------------------------------------------------

    async def _echo_prompt(self, context: TurnContext, prompt: list[Any]) -> None:
        """Send the prompt back as `user_message_chunk`s.

        Not redundant: the transcript `session/load` replays is built from what this turn
        *emitted*, so without the echo a reloaded session shows the agent talking to
        itself. Ungated, like every `session/update`.
        """
        for block in prompt:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                await context.emit(update_user_message_text(text))

    async def _announce_tools(
        self, context: TurnContext, backends: Any, catalogue: ToolCatalogue
    ) -> None:
        """List the session's MCP tools as `available_commands`.

        Emitted at the start of **every** turn, including one about to be refused — that
        is the point. A refusal that also says what *could* have been called is
        actionable; one that only says "that was not an invocation" is not.

        Costs one `tools/list` per server per turn. Against a local subprocess that is
        sub-millisecond, and caching it *across* turns would need
        `notifications/tools/list_changed` handling to stay honest.

        The listing goes through the turn's `ToolCatalogue`, so turning announcements off
        does not make the tool-call `kind` cost a second call — and turning them on does
        not make it cost a first one.
        """
        if not _option(context, ANNOUNCE_TOOLS, True):
            return
        await context.emit(update_available_commands(await _commands_for(backends, catalogue)))

    async def available_commands(self, session_id: str) -> list[AvailableCommand]:
        """The session's commands, for the announcement that precedes the first turn.

        Same list the turn announces, built by the same function, because a palette that
        disagreed with what a turn accepts would be worse than no palette. It costs one
        `tools/list` per server, paid once when the session becomes usable.

        `announce-tools` is deliberately **not** consulted here. That option turns off a
        per-turn notification whose cost is being repeated on every turn; this fires once,
        and a client that suppressed the repetition still needs the first list to have
        something to show.
        """
        backends = self._backends.backends(session_id)
        return await _commands_for(backends, ToolCatalogue(backends))

    async def _list_tools(
        self, context: TurnContext, backends: Any, catalogue: ToolCatalogue
    ) -> TurnResult:
        """Answer `/tools` with the listing, and end the turn without running anything.

        `TurnResult.ended()` rather than `refused()`: the turn did exactly what it was
        asked to do. Refusing would be the wrong stop reason for a command that worked.

        The listing comes from the same per-turn `ToolCatalogue` the announcement uses, so
        asking for it costs no extra `tools/list` beyond the one the turn already paid.
        """
        listings: dict[str, list[dict[str, Any]]] = {}
        for server in sorted(backends):
            listings[server] = list(await catalogue.listing(server))
        await context.emit(update_agent_message_text(render_tool_listing(listings)))
        return TurnResult.ended()

    # ------------------------------------------------------------------
    # Prompts and resources, MCP's other two primitives (`pyacp-tc5`)
    # ------------------------------------------------------------------

    async def _list_prompts(self, context: TurnContext, backends: Any) -> TurnResult:
        """Answer `/listPrompts` from every server that declared the capability.

        No `ToolCatalogue` equivalent behind it, deliberately. That cache exists because
        `tools/list` is paid three times in one turn — by the announcement, by each tool
        call's `kind`, and by `/tools`. A prompt listing is asked for once by the command
        that asked for it, and a cache with one reader is a place for staleness to live.
        """
        listings, undeclared = await self._catalogue(backends, "prompts", "list_prompts")
        await context.emit(update_agent_message_text(render_prompt_listing(listings, undeclared)))
        return TurnResult.ended()

    async def _list_resources(self, context: TurnContext, backends: Any) -> TurnResult:
        """Answer `/listResources`. Metadata only — reading one is `/resourceShow`.

        **Two passes, because MCP publishes resources through two methods.** A URI
        template like `greeting://{name}` reaches a client via
        `resources/templates/list` and via nothing else, so a listing built from
        `resources/list` alone reports a server whose resources are all templates as
        having none — a confident wrong picture, and the bug `pyacp-as5` is about.
        """
        listings, undeclared = await self._catalogue(backends, "resources", "list_resources")
        templates = await self._resource_templates(backends, listings)
        await context.emit(
            update_agent_message_text(render_resource_listing(listings, undeclared, templates))
        )
        return TurnResult.ended()

    @staticmethod
    async def _resource_templates(
        backends: Any, listings: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Every declared server's URI templates, asked for only where it makes sense.

        Only the servers already in `listings` are asked: they are exactly the ones that
        declared `resources`, which is the capability gating both methods. The servers
        `_catalogue` put in `undeclared` were never asked the first question and must not
        be asked this one either.

        `-32601` is absorbed into an empty section rather than failing the command.
        Templates are *optional within* the capability, so a server that declares
        `resources` and implements no templates is conforming — refusing to list its
        concrete resources over that would be the same wrong picture in the other
        direction. Every other code still propagates, matching `_catalogue`'s rule that a
        server which declared a capability and then fails is not absorbed.

        A server with no templates contributes no entry at all, so the common case — a
        server publishing only concrete resources — renders exactly as it did before.
        """
        found: dict[str, list[dict[str, Any]]] = {}
        for server in sorted(listings):
            try:
                templates = list(await backends[server].list_resource_templates())
            except MCPProtocolError as exc:
                if exc.code != _METHOD_NOT_FOUND:
                    raise
                logger.debug("%s implements no resources/templates/list", server)
                continue
            if templates:
                found[server] = templates
        return found

    @staticmethod
    async def _catalogue(
        backends: Any, capability: str, method: str
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """Every server's listing for one primitive, and the servers that have none.

        Two return values rather than one, because "not listed" has two causes a reader
        needs to tell apart: a server that publishes none of this primitive, and a server
        that does not implement it at all. Silently merging them would make an empty
        listing unactionable.

        A server that declared the capability and then fails is **not** absorbed. That is
        `ToolCatalogue.listing`'s rule too: a listing is the thing being asked for here, so
        `MCPProtocolError` propagates and `errors.py` forwards the backend's own code.
        """
        listings: dict[str, list[dict[str, Any]]] = {}
        undeclared: list[str] = []
        for server in sorted(backends):
            backend = backends[server]
            supports = getattr(backend, "supports", None)
            if supports is not None and not supports(capability):
                undeclared.append(server)
                continue
            listings[server] = list(await getattr(backend, method)())
        return listings, undeclared

    async def _expand_prompt(
        self, context: TurnContext, backends: Any, command: PromptCommand
    ) -> TurnResult:
        """`/promptShow` and `/promptInvoke`, which agree right up to the last step.

        Both resolve the server, check the capability, and validate the arguments against
        what the prompt declares. `/promptInvoke` then refuses, **before** `prompts/get`
        is called: it cannot use the expansion, and issuing a request whose answer is
        discarded would make the refusal cost a round trip and a server-side expansion for
        nothing.

        Validating first is what makes that refusal useful rather than a wall. It has the
        prompt's real name and the arguments in hand, so it can hand back a `/promptShow`
        that runs — and when the arguments are wrong it says *that* instead, which is the
        error the person actually needs either way.
        """
        server = _resolve_server(command.verb, command.server, command.name, backends)
        backend = backends[server]
        _require_capability(command.verb, server, backend, "prompts")

        listing = await backend.list_prompts()
        declared: list[Any] | None = None
        for entry in listing:
            if isinstance(entry, dict) and entry.get("name") == command.name:
                declared = entry.get("arguments")
                break
        else:
            offered = ", ".join(
                sorted(
                    str(entry.get("name"))
                    for entry in listing
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                )
            )
            raise CommandRefused(
                f"/{command.verb}: {server!r} has no prompt {command.name!r}. "
                + (f"It offers: {offered}." if offered else "It publishes no prompts.")
                + f" Run /{LIST_PROMPTS} to see them with their arguments."
            )

        try:
            arguments = prompt_arguments(command, declared)
        except CommandError as exc:
            raise CommandRefused(str(exc)) from None

        if isinstance(command, InvokePrompt):
            spelled = " ".join(f"--{key} {value!r}" for key, value in sorted(arguments.items()))
            raise CommandRefused(
                f"/{PROMPT_INVOKE} {NEEDS_A_MODEL}. The expansion itself needs none, so "
                f"/{PROMPT_SHOW} {server}/{command.name}"
                + (f" {spelled}" if spelled else "")
                + " returns the messages this prompt would have produced."
            )

        result = await backend.get_prompt(command.name, arguments)
        await context.emit(
            update_agent_message_text(render_prompt_heading(server, command.name, result))
        )
        for role, block in prompt_message_blocks(result):
            # A text chunk for the role, then the content mapped the way a tool result's
            # content is mapped. Reusing `to_content_block` rather than flattening
            # everything to text keeps an image in an expanded prompt an image: the
            # `supported_prompt_blocks` gate governs what this agent *reads*, and this is
            # the outbound direction. See `mcp_content.py`.
            await context.emit(update_agent_message_text(f"{role}:"))
            await context.emit(update_agent_message(to_content_block(block)))
        return TurnResult.ended()

    async def _show_resource(
        self, context: TurnContext, backends: Any, command: ShowResource
    ) -> TurnResult:
        """`/resourceShow`. One verb, because `resources/read` *is* the operation.

        There is no `/resourceInvoke` beside it the way `/promptInvoke` sits beside
        `/promptShow`. That pair splits on a model: expanding is the server's work and
        acting on the expansion is a model's. Reading a resource has no second half to
        defer — the bytes are the answer.
        """
        server = _resolve_server(
            RESOURCE_SHOW, command.server, command.uri, backends, separator=" "
        )
        backend = backends[server]
        _require_capability(RESOURCE_SHOW, server, backend, "resources")
        result = await backend.read_resource(command.uri)
        await context.emit(
            update_agent_message_text(render_resource_contents(command.uri, result))
        )
        return TurnResult.ended()

    async def _from_command(
        self, command: InvokeTool, backends: Any, catalogue: ToolCatalogue
    ) -> Invocation:
        """Turn `/invokeTool server/tool --k v` into the same `Invocation` JSON produces.

        Deliberately the same dataclass: everything after this point — the plan entry, the
        permission prompt, the session mode, the `kind` from the tool's annotations, the
        on-tool-failure policy — is machinery this command never has to know about, and
        cannot diverge from because it does not have its own copy.

        `read`, `write` and `run` are not offered here. They exist for a caller composing a
        file or command around a tool call, which is a JSON author's job; a person at a
        prompt asks for one tool.
        """
        server = _resolve_server(INVOKE_TOOL, command.server, command.tool, backends)
        schema: dict[str, Any] | None = None
        for tool in await catalogue.listing(server):
            if tool.get("name") == command.tool:
                raw = tool.get("inputSchema")
                schema = raw if isinstance(raw, dict) else None
                break
        else:
            offered = ", ".join(
                sorted(
                    str(tool.get("name"))
                    for tool in await catalogue.listing(server)
                    if isinstance(tool.get("name"), str)
                )
            )
            raise CommandRefused(
                f"{invocation_prefix(command)}: {server!r} has no tool {command.tool!r}. "
                + (f"It offers: {offered}." if offered else "It publishes no tools.")
                + f" Run /{LIST_TOOLS} to see them with their parameters."
            )
        # Before `coerce_arguments`, because a loose token is the more basic mistake: a
        # command that has both would otherwise be answered about its flags while the
        # reader's real error was never mentioned. The parser carried these here rather
        # than refusing them itself precisely so this message could name `schema`'s
        # parameters instead of the failing token — see `pyacp-ysq`.
        if command.positional:
            raise CommandRefused(str(positional_argument_error(command, schema))) from None
        try:
            arguments = coerce_arguments(command, schema)
        except CommandError as exc:
            raise CommandRefused(str(exc)) from None
        return Invocation(tool=command.tool, arguments=arguments, server=server)

    async def _emit_plan(self, context: TurnContext, plan: list[PlanEntry]) -> None:
        """Send the plan, if the client accepts plans and there is one.

        `clientCapabilities.plan` gates the *variant*, never the `session/update` call —
        see `turns.md`. So this suppresses the notification rather than skipping `emit`
        for everything else in the turn.

        The whole entry list goes every time, which is what `AgentPlanUpdate` carries;
        the protocol has no per-entry patch.
        """
        if plan and context.allows(Gate.PLAN_UPDATES):
            await context.emit(update_plan(entry.model_copy(deep=True) for entry in plan))

    # ------------------------------------------------------------------
    # Parsing — all of it, before any of it runs
    # ------------------------------------------------------------------

    def _parse(
        self,
        context: TurnContext,
        prompt: list[Any],
        backends: dict[str, MCPStdioClient] | Any,
    ) -> list[Invocation]:
        if not prompt:
            raise PromptConventionError("The prompt is empty, so it names no tool to run.")
        return [
            self._parse_block(context, index, block, backends)
            for index, block in enumerate(prompt)
        ]

    def _parse_block(
        self, context: TurnContext, index: int, block: Any, backends: Any
    ) -> Invocation:
        kind = getattr(block, "type", None)
        if kind in DECLINED_BLOCKS:
            raise PromptConventionError(
                f"Prompt block {index} is {DECLINED_BLOCKS[kind]}, and this agent runs "
                "tools rather than reasoning, so it is declined."
            )
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            raise PromptConventionError(
                f"Prompt block {index} is {_describe(block)}, and only text blocks carry "
                "an invocation."
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PromptConventionError(
                f"Prompt block {index} is not JSON ({exc.msg})."
            ) from None
        if not isinstance(payload, dict):
            raise PromptConventionError(
                f"Prompt block {index} is a JSON {type(payload).__name__}, not an object."
            )

        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            raise PromptConventionError(
                f"Prompt block {index} has no non-empty string 'tool'."
            )
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise PromptConventionError(
                f"Prompt block {index}: 'arguments' must be an object."
            )
        reads = self._reads(context, index, payload, arguments)
        write = self._write(context, index, payload)
        edit = self._edit(context, index, payload)
        if write is not None and edit is not None:
            raise PromptConventionError(
                f"Prompt block {index} names both 'write' and 'edit', which gives this "
                "call two destinations for one tool's output; name one of them."
            )
        return Invocation(
            tool=tool,
            arguments=arguments,
            server=self._server(index, payload, backends),
            reads=reads,
            write=write,
            edit=edit,
            runs=self._runs(context, index, payload, arguments, reads),
        )

    @staticmethod
    def _reads(
        context: TurnContext, index: int, payload: dict[str, Any], arguments: dict[str, Any]
    ) -> tuple[FileRead, ...]:
        """Parse `read`, refusing before anything runs rather than at the call.

        The gate is read with `allows` here — a client with no `fs.readTextFile` is not a
        bug, so it gets a refusal, not the `-32603` that `require` would produce. See the
        module docstring.
        """
        declared = payload.get("read")
        if declared is None:
            return ()
        if not isinstance(declared, dict):
            raise PromptConventionError(
                f"Prompt block {index}: 'read' must be an object mapping an argument name "
                "to a file."
            )
        if declared and not context.allows(Gate.READ_TEXT_FILE):
            raise UnsupportedByClientError(
                f"Prompt block {index} asks to read a file, but this client did not "
                "advertise clientCapabilities.fs.readTextFile, and this agent reads files "
                "only through the client."
            )
        reads: list[FileRead] = []
        for argument, spec in declared.items():
            if not isinstance(argument, str) or not argument:
                raise PromptConventionError(
                    f"Prompt block {index}: every key of 'read' must be a non-empty "
                    "argument name."
                )
            if argument in arguments:
                raise PromptConventionError(
                    f"Prompt block {index}: {argument!r} is named in both 'arguments' and "
                    "'read', which gives it two values; name it in one of them."
                )
            if not isinstance(spec, dict):
                raise PromptConventionError(
                    f"Prompt block {index}: 'read.{argument}' must be an object with a "
                    "'path'."
                )
            reads.append(
                FileRead(
                    argument=argument,
                    path=_contained(context, index, spec.get("path"), f"read.{argument}"),
                    line=_bounded(index, spec.get("line"), f"read.{argument}.line"),
                    limit=_bounded(index, spec.get("limit"), f"read.{argument}.limit"),
                )
            )
        return tuple(reads)

    @staticmethod
    def _runs(
        context: TurnContext,
        index: int,
        payload: dict[str, Any],
        arguments: dict[str, Any],
        reads: tuple[FileRead, ...],
    ) -> tuple[CommandRun, ...]:
        """Parse `run`. Same gate treatment as `_reads`, and the same reason.

        `terminal` is **one** capability covering all five methods, so one `allows` here
        decides the whole family — there is no per-method granularity to check.

        The argument names already claimed by `arguments` and `read` are passed in rather
        than re-derived, because "two sources for one value" has to mean *any* two
        sources: a command and a file competing for one argument is the same refusal as a
        file and a literal.
        """
        declared = payload.get("run")
        if declared is None:
            return ()
        if not isinstance(declared, dict):
            raise PromptConventionError(
                f"Prompt block {index}: 'run' must be an object mapping an argument name "
                "to a command."
            )
        if declared and not context.allows(Gate.TERMINAL):
            raise UnsupportedByClientError(
                f"Prompt block {index} asks to run a command, but this client did not "
                "advertise clientCapabilities.terminal, and this agent runs commands only "
                "through the client."
            )
        taken = set(arguments) | {read.argument for read in reads}
        runs: list[CommandRun] = []
        for argument, spec in declared.items():
            if not isinstance(argument, str) or not argument:
                raise PromptConventionError(
                    f"Prompt block {index}: every key of 'run' must be a non-empty "
                    "argument name."
                )
            if argument in taken:
                raise PromptConventionError(
                    f"Prompt block {index}: {argument!r} is named in 'run' and somewhere "
                    "else too, which gives it two values; name it once."
                )
            taken.add(argument)
            if not isinstance(spec, dict):
                raise PromptConventionError(
                    f"Prompt block {index}: 'run.{argument}' must be an object with a "
                    "'command'."
                )
            command = spec.get("command")
            if not isinstance(command, str) or not command:
                raise PromptConventionError(
                    f"Prompt block {index}: 'run.{argument}.command' must be a non-empty "
                    "string."
                )
            limit = _bounded(index, spec.get("outputByteLimit"), f"run.{argument}.outputByteLimit")
            runs.append(
                CommandRun(
                    argument=argument,
                    command=command,
                    args=_strings(index, spec.get("args"), f"run.{argument}.args"),
                    env=_environment(index, spec.get("env"), f"run.{argument}.env"),
                    # The session's own cwd when none is named: a command has to start
                    # somewhere, and the client's process directory is not something this
                    # side can see. A declared one is contained like every other path.
                    cwd=(
                        context.session.cwd
                        if spec.get("cwd") is None
                        else _contained(context, index, spec.get("cwd"), f"run.{argument}", "cwd")
                    ),
                    output_byte_limit=DEFAULT_OUTPUT_BYTE_LIMIT if limit is None else limit,
                )
            )
        return tuple(runs)

    @staticmethod
    def _write(context: TurnContext, index: int, payload: dict[str, Any]) -> FileWrite | None:
        """Parse `write`. Same gate treatment as `_reads`, and the same reason."""
        declared = payload.get("write")
        if declared is None:
            return None
        if not isinstance(declared, dict):
            raise PromptConventionError(
                f"Prompt block {index}: 'write' must be an object with a 'path'."
            )
        if not context.allows(Gate.WRITE_TEXT_FILE):
            raise UnsupportedByClientError(
                f"Prompt block {index} asks to write a file, but this client did not "
                "advertise clientCapabilities.fs.writeTextFile, and this agent writes "
                "files only through the client."
            )
        return FileWrite(path=_contained(context, index, declared.get("path"), "write"))

    @staticmethod
    def _edit(context: TurnContext, index: int, payload: dict[str, Any]) -> FileEdit | None:
        """Parse `edit`. Gated on **both** filesystem capabilities, and here is why.

        An edit reads the file, splices, and writes it back, so a client that granted only
        one half cannot be served: with no read there is nothing to verify against, and
        with no write the verified result has nowhere to go. Asking for both up front is
        also what keeps the parse-time refusal honest — discovering after the tool ran
        that the result cannot be written is the failure "validate everything, then run
        anything" exists to prevent.

        Same `allows` treatment as `_reads` and `_write`, and the same reason: a client
        without a filesystem is not a bug.
        """
        declared = payload.get("edit")
        if declared is None:
            return None
        if not isinstance(declared, dict):
            raise PromptConventionError(
                f"Prompt block {index}: 'edit' must be an object with a 'path', a "
                "'format', and an 'ops' list."
            )
        missing = [
            gate.value
            for gate in (Gate.READ_TEXT_FILE, Gate.WRITE_TEXT_FILE)
            if not context.allows(gate)
        ]
        if missing:
            raise UnsupportedByClientError(
                f"Prompt block {index} asks to edit a file, which needs both "
                "clientCapabilities.fs.readTextFile and "
                "clientCapabilities.fs.writeTextFile — an edit reads the file it is "
                f"verified against and writes the result back. This client did not "
                f"advertise {' or '.join(missing)}."
            )
        fmt = declared.get("format")
        if fmt not in DIALECTS:
            raise PromptConventionError(
                f"Prompt block {index}: 'edit.format' must be one of "
                f"{sorted(DIALECTS)}; the format is named rather than guessed from the "
                "path, because an extension is not a promise about a file's contents."
            )
        declared_ops = declared.get("ops")
        if not isinstance(declared_ops, list) or not declared_ops:
            raise PromptConventionError(
                f"Prompt block {index}: 'edit.ops' must be a non-empty list of ops."
            )
        return FileEdit(
            path=_contained(context, index, declared.get("path"), "edit"),
            dialect=DIALECTS[fmt],
            ops=tuple(
                _edit_op(index, position, spec) for position, spec in enumerate(declared_ops)
            ),
        )

    @staticmethod
    def _server(index: int, payload: dict[str, Any], backends: Any) -> str | None:
        """Which backend runs this call, refusing to guess when guessing could be wrong.

        **This is where a dropped `mcpServers` entry surfaces**, and the only place it
        can. ACP's schema marks that field `skip-invalid-items`, so an entry that does not
        validate is removed before `session/new` is ever called and the agent cannot know
        it was asked for — see `agent.md`. The client's first sign is a server it named
        being absent here, which reads like its own typo. Saying both possibilities out
        loud is the whole of what this side can do about it (`pyacp-mej`).
        """
        named = payload.get("server")
        if named is not None:
            if not isinstance(named, str) or named not in backends:
                raise PromptConventionError(
                    f"Prompt block {index} names server {named!r}; this session opened "
                    f"{sorted(backends) or 'none'}. If you did name it in session/new, "
                    "the entry was dropped before this agent saw it: ACP skips "
                    "mcpServers entries that do not validate, and every entry needs all "
                    "four of 'name', 'command', 'args', and 'env'."
                )
            return named
        if not backends:
            raise PromptConventionError(
                f"Prompt block {index} names a tool, but this session opened no MCP "
                "servers to run it against. If session/new did name one, check that its "
                "entry carried all four of 'name', 'command', 'args', and 'env' — ACP "
                "skips mcpServers entries that do not validate."
            )
        if len(backends) > 1:
            raise PromptConventionError(
                f"Prompt block {index} must name a 'server': this session opened "
                f"{sorted(backends)}."
            )
        return next(iter(backends))

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    async def _run(
        self,
        context: TurnContext,
        tracker: ToolCallTracker,
        broker: PermissionBroker,
        backends: Any,
        catalogue: ToolCatalogue,
        invocation: Invocation,
        index: int,
    ) -> bool:
        """One tool call, announced before it starts and updated when it ends.

        `pending` → `in_progress` → `completed`/`failed`. The first two are separate
        notifications on purpose: a client renders the call the moment it is known, and
        the transition to `in_progress` is what tells it the wait has begun rather than
        the request being queued behind something else.

        Returns whether the *tool* failed — not whether the call did. See the module
        docstring for why those are different.

        `kind` comes from the server's own tool annotations, and is the **only** thing
        they are allowed to change: the permission request below is sent either way. See
        `mcp_tools.py` for why a hint may relabel a question but never withdraw it.
        """
        key = str(index)
        started = tracker.start(
            key,
            title=invocation.title,
            kind=await catalogue.kind(invocation.server, invocation.tool),
            status="pending",
            raw_input=invocation.arguments,
            locations=invocation.locations,
        )
        await context.emit(started)
        if _mode(context) == DRY_RUN:
            await context.emit(
                tracker.progress(
                    key,
                    status="completed",
                    content=[
                        tool_content(
                            text_block(
                                f"[dry-run] {invocation.title} was not executed."
                                f"{_files_note(invocation)}"
                            )
                        )
                    ],
                )
            )
            tracker.forget(key)
            return False

        if not await self._permitted(context, broker, invocation, key):
            await context.emit(
                tracker.progress(
                    key,
                    status="failed",
                    content=[tool_content(text_block("Denied by the client."))],
                )
            )
            tracker.forget(key)
            return True

        # Reading and running are asked for *after* permission and before `in_progress`:
        # the client approving the call is what authorises pulling its files and starting
        # its processes, and a failure here means the tool never runs, so `in_progress`
        # would have been a lie.
        notes: list[str] = []
        try:
            arguments = await self._read_files(context, invocation)
            arguments = await self._run_commands(context, invocation, arguments, notes)
        except _ClientCallFailed as exc:
            await context.emit(
                tracker.progress(key, status="failed", content=[tool_content(text_block(str(exc)))])
            )
            tracker.forget(key)
            return True

        await context.emit(tracker.progress(key, status="in_progress", raw_input=arguments))

        client = backends[invocation.server]
        logger.debug("Calling %s for session %s", invocation.title, context.session_id)
        # Parked on the session for the length of the call, and only that long. A server
        # that asks a question mid-`tools/call` sends `elicitation/create` on its own read
        # loop, which has no route back to this turn — so this is how the forwarded
        # question learns which tool call it belongs to (`pyacp-owi`, `elicitation.md`).
        # Cleared in `finally` because a call that raised is no longer in flight either,
        # and a stale id would attach the *next* question to a call that already ended.
        context.session.running_tool_call = started.tool_call_id
        try:
            result = await client.call_tool(invocation.tool, arguments)
        finally:
            context.session.running_tool_call = None

        # `isError` is the MCP-sanctioned way for a tool to report its own failure on an
        # otherwise successful call. It becomes a status, never an exception.
        failed = bool(result["isError"])
        # The command notes come first because the commands did: a transcript that reads
        # in the order things happened is the whole reason they are content and not a log
        # line.
        content = [tool_content(text_block(note)) for note in notes]
        content.extend(to_tool_call_content(result) or ())
        if invocation.write is not None:
            note, wrote = await self._write_file(context, invocation.write, result, failed)
            failed = failed or not wrote
            content.append(tool_content(text_block(note)))
        if invocation.edit is not None:
            note, blocks, edited = await self._edit_file(context, invocation.edit, result, failed)
            failed = failed or not edited
            content.extend(blocks)
            content.append(tool_content(text_block(note)))
        await context.emit(
            tracker.progress(
                key,
                status="failed" if failed else "completed",
                content=content or None,
                raw_output=result,
            )
        )
        tracker.forget(key)
        return failed

    # ------------------------------------------------------------------
    # The client's filesystem
    # ------------------------------------------------------------------

    @staticmethod
    async def _read_files(context: TurnContext, invocation: Invocation) -> dict[str, Any]:
        """Fill this call's file-backed arguments from the client.

        `require` rather than `allows` here on purpose: `_reads` already refused the turn
        for a client without the capability, so reaching this line with the gate shut is
        *our* bug and `-32603` is the honest answer. See the module docstring.
        """
        if not invocation.reads:
            return invocation.arguments
        context.require(Gate.READ_TEXT_FILE)
        arguments = dict(invocation.arguments)
        for read in invocation.reads:
            try:
                response = await context.client.read_text_file(
                    session_id=context.session_id,
                    path=read.path,
                    line=read.line,
                    limit=read.limit,
                )
            except Exception as exc:  # noqa: BLE001 - see the module docstring
                logger.info("fs/read_text_file for %s failed: %s", read.path, exc)
                raise _ClientCallFailed(
                    f"Reading {read.path} through the client failed ({_why(exc)}), so "
                    f"{invocation.title} was not called."
                ) from None
            content = getattr(response, "content", None)
            if not isinstance(content, str):
                raise _ClientCallFailed(
                    f"The client answered fs/read_text_file for {read.path} without text "
                    f"content, so {invocation.title} was not called."
                )
            arguments[read.argument] = content
        return arguments

    @staticmethod
    async def _write_file(
        context: TurnContext, write: FileWrite, result: dict[str, Any], failed: bool
    ) -> tuple[str, bool]:
        """Send the tool's text output to the client. Returns the note and whether it went.

        Skipped rather than attempted in two cases, both of which would otherwise damage a
        file the client asked us to fill: a tool that reported `isError` (its output is a
        diagnostic, not a document) and a result with no text content at all (writing "" is
        a truncation, not a write).
        """
        context.require(Gate.WRITE_TEXT_FILE)
        if failed:
            return f"{write.path} was not written: the tool failed.", False
        text = _text_output(result)
        if not text:
            return (
                f"{write.path} was not written: the tool returned no text content, and "
                "writing an empty file would be a truncation rather than a result.",
                False,
            )
        try:
            await context.client.write_text_file(
                session_id=context.session_id, path=write.path, content=text
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            logger.info("fs/write_text_file for %s failed: %s", write.path, exc)
            return f"Writing {write.path} through the client failed ({_why(exc)}).", False
        return f"Wrote {len(text)} characters to {write.path}.", True

    @staticmethod
    async def _edit_file(
        context: TurnContext, edit: FileEdit, result: dict[str, Any], failed: bool
    ) -> tuple[str, list[Any], bool]:
        """Apply this call's structured edit. Returns a note, its content, and whether it went.

        The whole file is read — no `line`/`limit` window. The verifier's last step is
        "every byte outside the spliced spans is unchanged", and a window makes that
        assertion about a fragment while the write replaces a file, which is the one way
        this could quietly truncate.

        **The empty-content guard `_write_file` carries is deliberately not inherited.**
        There, an empty result is written over a whole file and the only evidence it is
        the right thing to write is that a tool said so. Here the result is a splice at an
        address, and `edits.apply` has already proved that nothing outside that address
        moved — so an op that sets a value to the empty string is an ordinary edit and
        refusing it would refuse a legitimate one. For the same reason there is no shrink
        heuristic and there must not be: a heuristic layered on top of a proof adds no
        information and will eventually block a correct delete.

        Every failure here is a **failed tool call with a note**, never a raised error —
        the same rule as a client that cannot write. The turn is fine; one operation in it
        was refused.
        """
        context.require(Gate.READ_TEXT_FILE)
        context.require(Gate.WRITE_TEXT_FILE)
        if failed:
            return f"{edit.path} was not edited: the tool failed.", [], False
        try:
            response = await context.client.read_text_file(
                session_id=context.session_id, path=edit.path
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            logger.info("fs/read_text_file for %s failed: %s", edit.path, exc)
            return (
                f"{edit.path} was not edited: reading it through the client failed "
                f"({_why(exc)}), and an edit is verified against the file it changes.",
                [],
                False,
            )
        original = getattr(response, "content", None)
        if not isinstance(original, str):
            return (
                f"{edit.path} was not edited: the client answered fs/read_text_file "
                "without text content.",
                [],
                False,
            )
        output = _text_output(result)
        try:
            edited = apply_edits(
                original,
                [op.resolve(output) for op in edit.ops],
                dialect=edit.dialect,
                path=edit.path,
            )
        except EditError as exc:
            logger.info("Edit of %s refused: %s", edit.path, exc)
            return f"{edit.path} was not edited: {exc}", [], False
        if not edited.changed:
            return (
                f"{edit.path} already holds these values, so nothing was written.",
                [],
                True,
            )
        try:
            await context.client.write_text_file(
                session_id=context.session_id, path=edit.path, content=edited.updated
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            logger.info("fs/write_text_file for %s failed: %s", edit.path, exc)
            return (
                f"Writing the edited {edit.path} through the client failed ({_why(exc)}).",
                [],
                False,
            )
        return (
            f"Edited {edit.path}: {len(edited.applied)} "
            f"{'op' if len(edited.applied) == 1 else 'ops'} applied and verified "
            f"({edited.confidence.value}).",
            list(to_edit_content(edited)),
            True,
        )

    # ------------------------------------------------------------------
    # The client's terminals
    # ------------------------------------------------------------------

    async def _run_commands(
        self,
        context: TurnContext,
        invocation: Invocation,
        arguments: dict[str, Any],
        notes: list[str],
    ) -> dict[str, Any]:
        """Fill this call's command-backed arguments by running them on the client.

        `require` rather than `allows`, exactly as `_read_files` does and for the same
        reason: `_runs` already refused the turn for a client with no `terminal`, so a
        shut gate here is our bug. One gate covers all five methods.

        `notes` is filled rather than returned so that what ran is reported even when the
        *tool* then fails — the command really did run on someone's machine, and a
        transcript that omitted it would be describing a different turn.
        """
        if not invocation.runs:
            return arguments
        context.require(Gate.TERMINAL)
        arguments = dict(arguments)
        for run in invocation.runs:
            output, note = await self._capture(context, invocation, run)
            arguments[run.argument] = output
            notes.append(note)
        return arguments

    async def _capture(
        self, context: TurnContext, invocation: Invocation, run: CommandRun
    ) -> tuple[str, str]:
        """Run one command to completion and return its output and a note about it.

        The lifetime is the point of this method. A terminal exists on the *client* until
        `terminal/release` arrives, so every exit from here releases: the ordinary one,
        the failed one, and the cancelled one.

        Cancellation is the case worth reading. `session/cancel` cancels the turn task
        while `wait_for_terminal_exit` is pending, so the command is still running and
        nobody is left to read its output — it is killed and released rather than left
        burning the client's machine. That cleanup runs under `asyncio.shield` because the
        cancellation is already in flight: an unshielded `await` here would be cancelled
        at its first suspension point and the release would never reach the wire. The
        `CancelledError` is re-raised, because a turn that swallowed it would report
        `end_turn` for a turn the client stopped.
        """
        try:
            terminal = await self._terminals.create(
                context,
                command=run.command,
                args=run.args,
                env=[EnvVariable(name=name, value=value) for name, value in run.env],
                cwd=run.cwd,
                output_byte_limit=run.output_byte_limit,
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            logger.info("terminal/create for %s failed: %s", run.display, exc)
            raise _ClientCallFailed(
                f"Starting {run.display} through the client failed ({_why(exc)}), so "
                f"{invocation.title} was not called."
            ) from None

        try:
            try:
                exit_status = await terminal.wait_for_exit()
                captured = await terminal.output()
            except asyncio.CancelledError:
                await asyncio.shield(terminal.abandon())
                raise
            except Exception as exc:  # noqa: BLE001 - see the module docstring
                logger.info("Reading terminal %s failed: %s", terminal.terminal_id, exc)
                raise _ClientCallFailed(
                    f"Running {run.display} through the client failed ({_why(exc)}), so "
                    f"{invocation.title} was not called."
                ) from None
        finally:
            # A no-op after `abandon`, and it never raises — see `Terminal.release`.
            await terminal.release()

        if exit_status.signal is not None or (exit_status.exit_code or 0) != 0:
            # An argument built from a failed command's output would be inventing input,
            # which is the same call the failed-read path makes.
            raise _ClientCallFailed(
                f"{run.display} {_exit_note(exit_status)}, so {invocation.title} was not "
                "called."
            )
        note = (
            f"Ran {run.display} in a client terminal: {len(captured.output)} characters "
            f"into {run.argument!r}."
        )
        if captured.truncated:
            note += (
                f" The client truncated it to the last {run.output_byte_limit} bytes, so "
                "the beginning of the output is not in that argument."
            )
        return captured.output, note

    async def _permitted(
        self,
        context: TurnContext,
        broker: PermissionBroker,
        invocation: Invocation,
        key: str,
    ) -> bool:
        """Ask the client whether this call may run, unless it already said always.

        Asked **after** the `tool_call` notification and before `in_progress`, which is
        what `pending` is for: the request carries the tool call, so the client has
        something to attach its prompt to.
        """
        if _mode(context) == AUTO_APPROVE:
            logger.debug("Mode %s: not asking about %s", AUTO_APPROVE, invocation.title)
            return True

        remembered = context.session.remembered_permissions.get(invocation.title)
        if remembered is not None:
            logger.debug("Permission for %s remembered: %s", invocation.title, remembered)
            return remembered

        try:
            response = await broker.request_for(
                key, description=f"Run the MCP tool {invocation.title}"
            )
        except RequestError as exc:
            return await self._without_a_human(context, exc)

        return self._decide(context, invocation, response)

    @staticmethod
    async def _without_a_human(context: TurnContext, exc: RequestError) -> bool:
        """Proceed when the client cannot take permission requests, and say so.

        **This is a correction, made under interop evidence (`pyacp-6ni.4`).** The first
        implementation refused the turn, reasoning that `session/request_permission` is
        mandatory — `ClientCapabilities` has no field for it — so a client answering
        `-32601` is broken. Then the SDK's own `examples/client.py` turned out to answer
        exactly that, and a headless client with no human to ask has nothing else it
        honestly can answer. An agent unusable against the reference client is the agent
        with the problem.

        Proceeding is not "assume consent from nowhere". **The client named this tool and
        these arguments in `session/prompt` itself**, so the authorization already exists;
        the prompt was only ever a courtesy to a human who might be watching, and a client
        that cannot reach one has already made the decision.

        That reasoning is **specific to this executor** and does not generalise. An
        LLM-backed executor *chooses* the tool, so the client's prompt authorizes nothing
        in particular and the fallback would be a hole. Any executor added later must
        decide this again for itself.

        Announced once per session rather than silently, and once rather than per call, so
        a transcript says plainly why nothing was asked.
        """
        already_said = context.session.remembered_permissions.get(_NO_HUMAN_KEY)
        if not already_said:
            context.session.remembered_permissions[_NO_HUMAN_KEY] = True
            await context.emit(
                update_agent_message_text(
                    f"This client answered {exc.code} to session/request_permission, so "
                    "there is nobody to ask. Running the tools this prompt named anyway: "
                    "the prompt is itself the authorization, because this agent only runs "
                    "what the client explicitly named."
                )
            )
        logger.warning(
            "Client refused session/request_permission (%s); proceeding on the prompt's "
            "own authority for session %s",
            exc.code,
            context.session_id,
        )
        return True

    @staticmethod
    def _decide(
        context: TurnContext, invocation: Invocation, response: RequestPermissionResponse
    ) -> bool:
        """Read one permission answer.

        **Denial is a selected option, not an outcome.** `RequestPermissionResponse.outcome`
        is `AllowedOutcome` (`"selected"`, with an `optionId`) or `DeniedOutcome` — whose
        literal is `"cancelled"`, despite the class name. So the only non-selected answer
        the protocol has is *the turn was cancelled*, and reading a rejection as one would
        turn a "no" into `stopReason: cancelled`. That inversion is exactly what this bead
        was told to get right.
        """
        outcome = response.outcome
        if getattr(outcome, "outcome", None) != "selected":
            raise _TurnCancelled
        kind = _KIND_BY_OPTION.get(getattr(outcome, "option_id", ""))
        if kind is None:
            # An option we never offered. Refusing to run is the only safe reading.
            logger.warning("Client chose unknown permission option %r", outcome)
            return False
        allowed = kind in _ALLOWING_KINDS
        if kind in _REMEMBERING_KINDS:
            context.session.remembered_permissions[invocation.title] = allowed
        return allowed

    async def _refuse(self, context: TurnContext, exc: PromptConventionError) -> TurnResult:
        """Say why, then stop. A silent refusal is worse than a wrong one.

        The convention footer is appended only when the convention is what was missed.
        A prompt refused because the *client* cannot do what it correctly asked for gets
        the reason alone — restating the convention there would send a client looking for
        a mistake it did not make.
        """
        logger.info("Refusing prompt for session %s: %s", context.session_id, exc)
        message = f"{exc} {CONVENTION}" if exc.explains_convention else str(exc)
        await context.emit(update_agent_message_text(message))
        return TurnResult.refused()


#: Option id to kind, for reading an answer back. Built from `PERMISSION_OPTIONS` so the
#: two cannot disagree about what an id means.
_KIND_BY_OPTION: dict[str, str] = {option.option_id: option.kind for option in PERMISSION_OPTIONS}


#: The `_meta` namespace this agent publishes on a per-tool `AvailableCommand`.
#:
#: Namespaced because ACP says implementations MUST NOT make assumptions about `_meta`
#: values: an unnamespaced `inputSchema` would be a land grab on a dict every extension
#: shares. A client that wants the idea without our namespace can fall back to a bare
#: `_meta.inputSchema`, which is a convention any agent may adopt.
TOOL_META_KEY = "python-acp/tool"


def _tool_meta(server: str, name: str, tool: dict[str, Any]) -> dict[str, Any]:
    """The `_meta` block for one tool's palette entry: what the hint had to throw away.

    `input` is `UnstructuredCommandInput`, ACP's only argument shape — one free-text
    string. `tool_command_hint` reads the tool's `inputSchema` to *build* that string and
    then discards the structure, so a client is handed a summary it cannot validate
    against, and the user learns the flag names and the legal enum values by trial and
    error. `AvailableCommand` carries `_meta`, ACP's own extensibility point, and putting
    the schema there lets a client render a real form — typed inputs, required markers,
    enum dropdowns — while the hint stays exactly as it was for one that does not look.

    Additive by construction: a client that ignores `_meta` sees no change at all, which
    is the property that makes this safe to ship ahead of any client (`pyacp-ma2`).

    **Passed through verbatim**, unnormalised and unreordered. The client is rendering the
    *server's* vocabulary, not ours, and a helpfully-rewritten schema is one more place
    for the form and `coerce_arguments` to disagree.

    `server` and `tool` are carried alongside so a client need not know that
    `parse_command` splits the name on the **first** slash — a rule that exists because a
    server name may not contain one, and one no client should have to reimplement.
    """
    meta: dict[str, Any] = {"server": server, "tool": name}
    schema = tool.get("inputSchema")
    # Omitted rather than null when the tool published nothing. The distinction is
    # load-bearing and `commands.py` already draws it: `properties: {}` is a statement
    # that the tool takes no parameters, while an absent `inputSchema` says *nothing*, and
    # `"inputSchema": null` is noise every reader has to defend against.
    if isinstance(schema, dict):
        meta["inputSchema"] = schema
    return {TOOL_META_KEY: meta}


async def _commands_for(backends: Any, catalogue: ToolCatalogue) -> list[AvailableCommand]:
    """Every MCP tool on the session, then the two built-ins.

    One function with two callers — the per-turn announcement and the one the agent sends
    when a session opens — so the palette a client is given before its first prompt cannot
    differ from the one a turn announces.
    """
    commands: list[AvailableCommand] = []
    for server in sorted(backends):
        for tool in await catalogue.listing(server):
            name = tool.get("name")
            if not isinstance(name, str):
                continue
            commands.append(
                AvailableCommand(
                    name=f"{server}/{name}",
                    description=tool.get("description") or f"MCP tool {name!r}",
                    # A hint, like every built-in has. Without one a composer offers the
                    # name and nothing about its parameters, and the user guesses
                    # positionally (`pyacp-acn`). `commands.parse_command` accepts this
                    # name directly, so the hint describes a command that really runs.
                    input=AvailableCommandInput(
                        root=UnstructuredCommandInput(hint=tool_command_hint(tool))
                    ),
                    field_meta=_tool_meta(server, name, tool),
                )
            )
    # The built-ins go last, after the server's own tools: a palette is read from the top,
    # and what the session can *do* is more interesting than how to ask about it.
    commands.extend(_BUILTIN_COMMANDS)
    return commands


def _command_in(prompt: list[Any]) -> Command | None:
    """Recognise a slash command, or return `None` and leave the prompt to JSON.

    Only a single text block can be one. A multi-block prompt is a composed request from a
    program, and treating the first block of one as a command would silently drop the
    rest — a much worse failure than declining to recognise it.
    """
    if len(prompt) != 1:
        return None
    text = getattr(prompt[0], "text", None)
    if not isinstance(text, str):
        return None
    try:
        return parse_command(text)
    except CommandError as exc:
        raise CommandRefused(str(exc)) from None


def _resolve_server(
    verb: str, server: str | None, target: str, backends: Any, separator: str = "/"
) -> str:
    """Which server a command goes to, refusing an ambiguous name rather than guessing.

    The named server wins. A bare name is allowed only when the session has exactly one
    server, where there is nothing to guess; with several, picking the first that happens
    to publish it would make the same command mean different things as the session's
    servers changed.

    `separator` is what joins a server to `target` in the suggestion the ambiguous case
    prints -- `/` for the three `<server>/<name>` commands, a space for `/resourceShow`,
    whose URI cannot be carved out of a slash-separated pair. It is a parameter rather than
    something derived, because those two shapes are the whole reason this is shared.
    """
    names = sorted(backends)
    if server is not None:
        if server not in names:
            offered = ", ".join(names) if names else "none"
            raise CommandRefused(
                f"/{verb}: this session has no MCP server {server!r}. It has: {offered}."
            )
        return server
    if not names:
        raise CommandRefused(
            f"/{verb}: this session has no MCP servers, so there is nothing to call. "
            "Servers are named in `session/new`."
        )
    if len(names) > 1:
        raise CommandRefused(
            f"/{verb}: this session has several MCP servers ({', '.join(names)}), so it "
            f"needs one: /{verb} {names[0]}{separator}{target} ..."
        )
    return names[0]


def _require_capability(verb: str, server: str, backend: Any, capability: str) -> None:
    """Refuse a command the server's own handshake says it cannot answer.

    MCP's rule is that a client MUST NOT use a capability the server did not declare, and
    the practical difference is the quality of the answer. Asked anyway, the server replies
    `-32601`, which `errors.py` faithfully forwards as a JSON-RPC error naming a method the
    person never typed. Reading the `initialize` block instead turns that into a refusal
    that names the server and the thing it does not do.
    """
    supports = getattr(backend, "supports", None)
    if supports is not None and not supports(capability):
        raise CommandRefused(
            f"/{verb}: MCP server {server!r} declared no {capability} capability in its "
            "handshake, so it has none to offer."
        )


def _requester(context: TurnContext):
    """Adapt the `Client` facade to the shape `PermissionBroker` calls.

    `session/request_permission` has **no capability gate** — `ClientCapabilities` has no
    field for it and every ACP client must accept it — so this is called straight off the
    client with nothing to check first.
    """

    async def request(payload: RequestPermissionRequest) -> RequestPermissionResponse:
        return await context.client.request_permission(
            session_id=payload.session_id, tool_call=payload.tool_call, options=payload.options
        )

    return request


def _option(context: TurnContext, config_id: str, default: Any) -> Any:
    """One config option's current value, or `default` when the session has no such option.

    A session created by an agent whose executor exposes none has an empty tuple, and a
    missing option means the behaviour it would have changed stays at its default.
    """
    for option in context.session.config_options:
        if option.id == config_id:
            return option.current_value
    return default


def _mode(context: TurnContext) -> str:
    """This session's mode id, defaulting to `execute`.

    A session created by an agent whose executor advertises no modes has `modes` of
    `None`, and the safe default is the one that asks.
    """
    modes = context.session.modes
    return modes.current_mode_id if modes is not None else EXECUTE


def _plan_for(invocations: list[Invocation]) -> list[PlanEntry]:
    """The turn's plan, complete before the first tool runs.

    That completeness is what makes it an honest plan rather than a guess: the router
    validates every invocation up front, so it already knows every step it will take.
    """
    return [plan_entry(f"Run {invocation.title}", status="pending") for invocation in invocations]


def _why(exc: Exception) -> str:
    """One phrase naming a failed client call, for a tool call's content.

    A `RequestError` carries a JSON-RPC code the client chose and a message it wrote;
    both belong in the transcript, because "the client said -32603 file not found" is a
    different problem from "the connection dropped".
    """
    if isinstance(exc, RequestError):
        # `RequestError` has no `.message`; the message is the exception's own args.
        return f"{exc.code}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _exit_note(status: Any) -> str:
    """How a command ended, in words, for a refusal a person has to read.

    `exitCode` and `signal` are both optional in the schema — a process killed by a
    signal has no exit code — so the three cases are named rather than formatted into one
    template that would print `None` at somebody.
    """
    if status.signal is not None:
        return f"was killed by {status.signal}"
    if status.exit_code is None:
        return "ended without an exit status"
    return f"exited with status {status.exit_code}"


def _files_note(invocation: Invocation) -> str:
    """What a dry run says about the files and commands it did not touch.

    A preview that named the tool and its arguments but stayed silent about the file it
    was about to overwrite — or the command it was about to run — would be a preview of
    the wrong thing.
    """
    parts = [f"{read.path} -> {read.argument!r}" for read in invocation.reads]
    note = f" Would read {', '.join(parts)}." if parts else ""
    commands = [f"{run.display} -> {run.argument!r}" for run in invocation.runs]
    if commands:
        # A preview that stayed quiet about the command it was about to run on the
        # client's machine would be a preview of the wrong thing, exactly as with files.
        note += f" Would run {', '.join(commands)}."
    if invocation.write is not None:
        note += f" Would write its output to {invocation.write.path}."
    if invocation.edit is not None:
        addresses = ", ".join(
            f"{op.kind.value} {op.address or '(root)'}" for op in invocation.edit.ops
        )
        note += (
            f" Would edit {invocation.edit.path} as {invocation.edit.dialect.name}: "
            f"{addresses}."
        )
    return note


def _contained(
    context: TurnContext, index: int, path: Any, label: str, field: str = "path"
) -> str:
    """One declared path, checked against the session's roots, resolved.

    `paths.py` owns the rule (`pyacp-3rw.4`); this only adapts its refusal into a prompt
    refusal. `PathConstraintError` is a `ValueError` that `errors.py` would answer with
    `-32602`, but it arrives here from inside a text block of an otherwise well-formed
    `session/prompt`, and this executor answers a prompt it will not run with a refusal —
    the same reasoning as every other parse failure. It is re-raised, not swallowed, so
    the reason still reaches the client verbatim.
    """
    if not isinstance(path, str) or not path:
        raise PromptConventionError(
            f"Prompt block {index}: '{label}.{field}' must be a non-empty string."
        )
    try:
        return str(require_contained(path, context.session.roots, f"{label}.{field}"))
    except PathConstraintError as exc:
        raise PromptConventionError(f"Prompt block {index}: {exc}") from None


def _text_output(result: dict[str, Any]) -> str:
    """A `tools/call` result's text blocks, joined. The tool's output as a document.

    Non-text content is skipped rather than described: this feeds a file, and a sentence
    about an image is not what the caller asked to be written. `rawOutput` carries the
    whole result for anyone who wants it.
    """
    return "\n".join(
        block["text"]
        for block in result.get("content") or ()
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _edit_op(index: int, position: int, spec: Any) -> EditOp:
    """One declared op, validated before the turn runs anything.

    The value rules are `edits.Op`'s own, restated here rather than deferred to it,
    because `Op` cannot be constructed yet for an op that takes the tool's output — and a
    prompt whose ops are wrong must be refused before the tool runs, not after.
    """
    label = f"edit.ops[{position}]"
    if not isinstance(spec, dict):
        raise PromptConventionError(
            f"Prompt block {index}: {label!r} must be an object with a 'kind' and an "
            "'address'."
        )
    raw_kind = spec.get("kind")
    kinds = [kind.value for kind in OpKind]
    if raw_kind not in kinds:
        raise PromptConventionError(
            f"Prompt block {index}: '{label}.kind' must be one of {kinds}."
        )
    kind = OpKind(raw_kind)
    address = spec.get("address")
    if not isinstance(address, str):
        raise PromptConventionError(
            f"Prompt block {index}: '{label}.address' must be a string. The empty string "
            "is the document root, so it is valid and is not the same as leaving it out."
        )
    named = [source for source in VALUE_SOURCES if source in spec]
    if kind is OpKind.DELETE:
        if named:
            raise PromptConventionError(
                f"Prompt block {index}: '{label}' is a delete and takes no "
                f"{' or '.join(named)}."
            )
        return EditOp(kind=kind, address=address)
    if len(named) != 1:
        raise PromptConventionError(
            f"Prompt block {index}: '{label}' needs exactly one of "
            f"{', '.join(VALUE_SOURCES)} — 'value' is raw source text in the target "
            "format, 'scalar' is a JSON scalar rendered by the format, and "
            "'fromOutput': true takes the tool's own text output. It named "
            f"{len(named)}."
        )
    source = named[0]
    if source == "fromOutput":
        if spec["fromOutput"] is not True:
            raise PromptConventionError(
                f"Prompt block {index}: '{label}.fromOutput' is true or absent; there is "
                "no other value it could mean."
            )
        return EditOp(kind=kind, address=address, from_output=True)
    if source == "value":
        if not isinstance(spec["value"], str):
            raise PromptConventionError(
                f"Prompt block {index}: '{label}.value' must be a string — it is raw "
                "source text in the target format, not a JSON value. Use 'scalar' for a "
                "JSON scalar."
            )
        return EditOp(kind=kind, address=address, value=spec["value"])
    return EditOp(kind=kind, address=address, scalar=spec["scalar"])


def _strings(index: int, value: Any, label: str) -> tuple[str, ...]:
    """A command's arguments: absent, or a list of strings.

    Not coerced. `["-n", 5]` would reach the client as a number in a field the schema
    types as a string, and refusing here says which block and which field instead of
    letting pydantic say neither.
    """
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PromptConventionError(
            f"Prompt block {index}: '{label}' must be a list of strings."
        )
    return tuple(value)


def _environment(index: int, value: Any, label: str) -> tuple[tuple[str, str], ...]:
    """A command's environment: absent, or an object of string to string.

    An object rather than the schema's `[{name, value}]` list, because that is the shape a
    client writing JSON by hand reaches for and duplicate names cannot happen in it.
    `terminals.py` turns it back into `EnvVariable`s at the call.
    """
    if value is None:
        return ()
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and name and isinstance(item, str) for name, item in value.items()
    ):
        raise PromptConventionError(
            f"Prompt block {index}: '{label}' must be an object mapping variable names to "
            "string values."
        )
    return tuple(value.items())


def _bounded(index: int, value: Any, label: str) -> int | None:
    """`line` or `limit`: absent, or a non-negative integer.

    The schema constrains both to `ge=0` and would reject anything else with a pydantic
    error mid-turn; refusing here says which block and which field instead. `bool` is
    excluded explicitly because it is an `int` in Python and `{"line": true}` is not a
    line number.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PromptConventionError(
            f"Prompt block {index}: '{label}' must be a non-negative integer."
        )
    return value


def _describe(block: Any) -> str:
    kind = getattr(block, "type", None)
    return f"a {kind!r} block" if isinstance(kind, str) else f"a {type(block).__name__}"
