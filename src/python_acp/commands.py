"""Human-typed commands, in front of the JSON invocation convention.

The turn convention is a JSON object per prompt block (`turn_mcp_router.py`), which is
right for a program and hostile to a person: nobody types `{"tool": "echo", "arguments":
{"message": "hi"}}` into a chat box to find out what a server offers. These commands are
the human door onto the same machinery, one per MCP server primitive.

    /tools
    /invokeTool demo/echo --message "hello world" --count 3
    /listPrompts
    /promptShow demo/greeting --name "Ada Lovelace"
    /promptInvoke demo/greeting --name "Ada Lovelace"
    /listResources
    /resourceShow demo file:///etc/hosts

`/invokeTool` deliberately produces the same `Invocation` the JSON path produces, so a
command-line call is not a second execution path: it inherits the session mode, the
permission prompt, and the on-tool-failure policy without knowing they exist.

## Three primitives, three shapes, and one of them needs a model

MCP servers publish tools, prompts, and resources. Only tools were reachable here before
`pyacp-tc5`, which had the palette showing the *model's* callables — MCP calls tools
model-controlled — while the convention every other client follows surfaces prompts, the
user-controlled primitive. All three are reachable now, and what each command can honestly
do is set by decision D1, which puts no model in this runtime:

| Command | MCP call | Needs a model |
|---|---|---|
| `/tools`, `/listPrompts`, `/listResources` | `*/list` | no — metadata |
| `/invokeTool` | `tools/call` | no — the client named the tool and its arguments |
| `/promptShow` | `prompts/get` | **no** — the *server* performs the substitution |
| `/resourceShow` | `resources/read` | no — reading is the whole operation |
| `/promptInvoke` | `prompts/get`, then act on the messages | **yes**, so it refuses |

`/promptShow` and `/promptInvoke` split on exactly that line. Expanding a prompt is a
template substitution the server does; what comes back is a list of messages addressed to
a model, and acting on them is the part this build cannot do. `/promptInvoke` is shipped
anyway, refusing with the reason and pointing at `/promptShow`, on the same principle that
has `authenticate` answer `-32000` rather than going missing: a client should discover a
boundary, not an absence.

Resources have no such split. `resources/read` *is* the operation, so there is one verb.

## This reopens pyacp-sld.2, deliberately

`pyacp-sld.2`/`sld.3` deleted the `{"action": ...}` surface and with it the MCP
passthrough, recording that "ACP's model is that the agent uses those internally, not that
a client reaches through it to the server" — so reading an MCP prompt or resource through
this bridge was decided *against*, not merely left undone.

`/promptShow` and `/resourceShow` reverse that for prompt and resource **content**, and it
is a reversal rather than a loophole: arriving as a slash command inside a turn instead of
as a JSON-RPC method is a different door onto the same capability, and pretending
otherwise would be worse than saying so. What stands from `sld.2` is the part that was
actually load-bearing — there is no MCP method on the ACP wire, no process-wide server to
address, and nothing here bypasses `session/new`. A client asks in ACP, and the agent is
the one that speaks MCP.

The listings never needed that argument. `/tools` already reports what
`available_commands` announces every turn, in more detail, and `/listPrompts` and
`/listResources` are the same kind of thing: a rendering of a server's own catalogue.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field, replace
from typing import Any

from python_acp.markdown import code_span, fenced_lines

#: What a client renders as a slash command. Accepted with or without the slash: a client
#: that fills its composer from `available_commands` sends the name it was given, and a
#: person typing by hand may or may not reach for the slash first.
LIST_TOOLS = "tools"
INVOKE_TOOL = "invokeTool"
LIST_PROMPTS = "listPrompts"
PROMPT_SHOW = "promptShow"
PROMPT_INVOKE = "promptInvoke"
LIST_RESOURCES = "listResources"
RESOURCE_SHOW = "resourceShow"

#: Every name above, for the one place that has to recognise a command before it can parse
#: one: an unbalanced quote is only *our* problem when the text was aimed at us. Built from
#: the constants rather than written out again, so a new command cannot be left out of it.
COMMAND_NAMES: frozenset[str] = frozenset(
    {
        LIST_TOOLS,
        INVOKE_TOOL,
        LIST_PROMPTS,
        PROMPT_SHOW,
        PROMPT_INVOKE,
        LIST_RESOURCES,
        RESOURCE_SHOW,
    }
)

#: Shown in the `available_commands` announcement, and in each listing's own footer, so the
#: syntax is discoverable from inside the thing that needs it.
LIST_TOOLS_HINT = "list every tool on this session's MCP servers, with parameters"
INVOKE_TOOL_HINT = "<server>/<tool> --param value [--flag]"
LIST_PROMPTS_HINT = "list every prompt on this session's MCP servers, with arguments"
PROMPT_SHOW_HINT = "<server>/<prompt> --argument value"
PROMPT_INVOKE_HINT = "<server>/<prompt> --argument value  (needs a model; see /promptShow)"
LIST_RESOURCES_HINT = "list every resource on this session's MCP servers"
RESOURCE_SHOW_HINT = "[<server>] <uri>"

#: Why `/promptInvoke` cannot run, in the words the refusal uses. A constant because the
#: same sentence has to appear in the refusal and in the listing footer that offers the
#: command, and two copies would drift the moment a model arrives.
NEEDS_A_MODEL = (
    "expands a prompt and then acts on the messages it returns, which needs a model. "
    "This bridge routes tool calls and has none (decision D1)"
)


class CommandError(ValueError):
    """A command was recognised and then found to be wrong.

    `ValueError` so that the caller's existing refusal path reports it the way it reports
    every other prompt-convention failure — see `errors.py`, where `ValueError` is already
    `-32602`. A command that is *not* recognised is not an error at all: it falls through
    to the JSON convention, which owns its own diagnostics.
    """


@dataclass(frozen=True)
class ListTools:
    """`/tools`. Carries nothing: the listing is entirely derived from the session."""


@dataclass(frozen=True)
class ListPrompts:
    """`/listPrompts`. `ListTools` for the other user-facing primitive."""


@dataclass(frozen=True)
class ListResources:
    """`/listResources`. Metadata only — reading one is `/resourceShow`."""


@dataclass(frozen=True)
class ShowResource:
    """`/resourceShow [<server>] <uri>`.

    A URI, not a `<server>/<name>` pair: `file:///etc/hosts` is full of slashes and
    `rpartition` would split it somewhere meaningless. So the server is a separate
    positional token, present or absent, and never carved out of the second one.
    """

    server: str | None
    uri: str


@dataclass(frozen=True)
class PromptCommand:
    """What `/promptShow` and `/promptInvoke` share: which prompt, and its arguments.

    Values stay raw here for the same reason `InvokeTool`'s do — validating them needs the
    prompt's declared `arguments`, which needs an await — but they never become anything
    other than strings. MCP types `prompts/get`'s arguments as `{[key: string]: string}`,
    so the coercion table `InvokeTool` needs has nothing to do here. That is the whole of
    the "shape problem" this bead was filed over: prompt arguments are named strings, and a
    command line carries named strings natively.
    """

    server: str | None
    name: str
    raw_arguments: dict[str, list[str]] = field(default_factory=dict)
    #: Arguments given with no value. Always an error for a prompt — see `prompt_arguments`
    #: — but recorded rather than refused during parsing, so the message can name the
    #: prompt it belongs to.
    bare_flags: frozenset[str] = frozenset()

    #: The command that produced this, for error messages. Overridden by each subclass.
    verb: str = PROMPT_SHOW


@dataclass(frozen=True)
class ShowPrompt(PromptCommand):
    """`/promptShow <server>/<prompt> --argument value`."""

    verb: str = PROMPT_SHOW


@dataclass(frozen=True)
class InvokePrompt(PromptCommand):
    """`/promptInvoke <server>/<prompt> --argument value`. Parsed, then refused.

    Parsed rather than rejected on sight so the refusal can name the prompt and repeat the
    arguments back as a `/promptShow` the reader can run. A refusal that cannot restate
    what was asked for is a worse refusal.
    """

    verb: str = PROMPT_INVOKE


@dataclass(frozen=True)
class InvokeTool:
    """`/invokeTool <server>/<tool> --k v`, before argument types are known.

    Values are raw strings here. Typing them needs the tool's `inputSchema`, which needs
    an await, and parsing must be able to report a malformed command without touching a
    server first.
    """

    server: str | None
    tool: str
    raw_arguments: dict[str, list[str]] = field(default_factory=dict)
    #: Flags given with no value, which read as `true` unless the schema says otherwise.
    bare_flags: frozenset[str] = frozenset()
    #: Tokens typed with no `--name` in front of them, carried rather than refused here.
    #: Naming the parameters the caller *should* have used needs the tool's `inputSchema`,
    #: which needs a server round trip, so the complaint waits for
    #: `positional_argument_error` — see `pyacp-ysq`.
    positional: tuple[str, ...] = ()
    #: The command and its target exactly as typed: `/Demo/echo`, or `/invokeTool
    #: Demo/echo`. Messages quote it so the example they offer is a line the reader can
    #: paste back unchanged, rather than the other spelling of the same call.
    typed_as: str = ""
    #: `_curly_quote_note` for the text these `positional` tokens came from, carried
    #: because only `parse_command` still has that text. Deferring the refusal without
    #: this would have silently dropped the `pyacp-avg` diagnosis, which is the single
    #: most common reason a loose token appears at all.
    positional_note: str = ""


#: Everything `parse_command` can hand back. A union rather than a base class: these have
#: nothing in common but the fact that a person typed them, and the executor dispatches on
#: which one it got.
Command = (
    ListTools | InvokeTool | ListPrompts | ShowPrompt | InvokePrompt | ListResources | ShowResource
)


def parse_command(text: str) -> Command | None:
    """Recognise a command, or return `None` to leave the text to the JSON convention.

    Returning `None` rather than raising for unrecognised text is what keeps this layer
    additive: every prompt that worked before this module existed still takes the same
    path through `_parse_block`.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        # An unbalanced quote. Only *our* commands should complain about it; anything else
        # is likely JSON, whose own parser gives a better message about it than we can.
        head = stripped.split(None, 1)[0].lstrip("/")
        if head not in COMMAND_NAMES:
            return None
        raise CommandError(
            f"/{head}: the command has an unbalanced quote. Wrap a value containing "
            f"spaces in one pair of quotes: {code_span('--message \"hello world\"')}."
        ) from None
    if not tokens:
        return None

    try:
        command = _dispatch(tokens)
    except CommandError as refusal:
        # The one place raw text becomes tokens, and so the only place that still has the
        # text to diagnose. See `_curly_quote_note`.
        note = _curly_quote_note(stripped)
        if not note:
            raise
        raise CommandError(f"{refusal}{note}") from None

    if isinstance(command, InvokeTool) and command.positional:
        # A loose token no longer raises here — the refusal waits for the tool's schema
        # (`positional_argument_error`) — but the note explaining *why* one appeared can
        # still only be written where the raw text exists. `--text “hello there”` is the
        # case: the curly quotes defeat `shlex`, `there”` becomes the loose token, and
        # without carrying the note forward the reader is told about a parameter when
        # their actual mistake was a quote character.
        return replace(command, positional_note=_curly_quote_note(stripped))
    return command


#: The quotes a composer substitutes for `"` and `'`. `shlex` treats none of them as a
#: delimiter, so a value wrapped in them is split at every space instead.
_CURLY_QUOTES = "“”‘’"

#: Each curly quote to the straight one it replaced, for showing a command that would work.
_STRAIGHTEN = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def _curly_quote_note(text: str) -> str:
    """Name the curly quote as the cause, when a refusal is probably about one.

    Autocorrect turns `"` into `“` in a chat composer, and `shlex` does not know that
    character: `--text “Hello from the GUI”` tokenises to `--text`, `“Hello`, `from`,
    `the`, `GUI”`, so the refusal lands on the word `from` — four tokens past the actual
    mistake, naming something the reader did nothing wrong with. The two characters are
    nearly indistinguishable at a chat font size, so without this the message sends the
    reader looking in the wrong place (`pyacp-avg`).

    **Appended, never substituted.** The original refusal is still the accurate account of
    what the parser found; this says what probably put it there. And it is a note rather
    than a rule: a curly quote *inside* a correctly quoted value is ordinary text, so this
    is only ever reached on a command that already failed.

    Tokenisation is deliberately not changed to accept these. `’` is an apostrophe far
    more often than a quote — straightening it would corrupt ordinary prose — and treating
    only the double forms as delimiters would make the rule arbitrary.
    """
    found = [quote for quote in _CURLY_QUOTES if quote in text]
    if not found:
        return ""
    return (
        f" This command contains curly quotes ({' '.join(found)}), which are not quote "
        f"characters — the text was split at every space instead. Retype it with straight "
        f"quotes: {code_span(text.translate(_STRAIGHTEN))}"
    )


def _dispatch(tokens: list[str]) -> Command | None:
    """Route an already-tokenised command to its parser, or `None` if it is not one."""
    name = tokens[0].lstrip("/")
    rest = tokens[1:]
    if name == LIST_TOOLS:
        return _parse_bare(name, rest, ListTools())
    if name == LIST_PROMPTS:
        return _parse_bare(name, rest, ListPrompts())
    if name == LIST_RESOURCES:
        return _parse_bare(name, rest, ListResources())
    if name == INVOKE_TOOL:
        return _parse_invocation(rest)
    if name == PROMPT_SHOW:
        return _parse_prompt(ShowPrompt, PROMPT_SHOW, PROMPT_SHOW_HINT, rest)
    if name == PROMPT_INVOKE:
        return _parse_prompt(InvokePrompt, PROMPT_INVOKE, PROMPT_INVOKE_HINT, rest)
    if name == RESOURCE_SHOW:
        return _parse_resource(rest)
    if _TOOL_COMMAND.fullmatch(name):
        return _parse_tool_command(name, rest)
    return None


#: A palette entry for an MCP tool: `<server>/<tool>`, the name `_commands_for` announces.
#:
#: The server segment is deliberately narrow and the tool segment deliberately not. The
#: narrow half is what keeps this from swallowing text that merely contains a slash: a
#: JSON prompt tokenises to something like `{tool:fetch,arguments:{url:http://x/y}}`,
#: whose first segment carries `{` and `:` and so cannot match. The wide half is because
#: an MCP tool name is the server's to choose, and a tool this pattern rejected would be
#: advertised and then refused — the exact bug this whole change is about (`pyacp-acn`).
_TOOL_COMMAND = re.compile(r"[A-Za-z0-9_.-]+/\S+")


def _parse_tool_command(name: str, tokens: list[str]) -> InvokeTool:
    """`/<server>/<tool> --flag value` — a palette name typed directly.

    Sugar for `/{INVOKE_TOOL} <server>/<tool> ...`, producing the identical `InvokeTool`,
    so it inherits the session mode, the permission prompt, the tool-call `kind` and the
    on-tool-failure policy without knowing they exist — the same reasoning that makes
    `/invokeTool` share the JSON path's `Invocation`.

    **This is what makes the palette honest.** `_commands_for` announces every MCP tool
    under this name, and before `pyacp-acn` nothing accepted it: a client that filled its
    composer from `available_commands` — which is what the field is for — had its own
    advertised command refused as malformed JSON.

    Split on the **first** slash, not the last. `_commands_for` builds the name as
    `f"{server}/{tool}"` and a server name may not contain a slash, so the first one is
    the join; `rpartition` would read `Demo/a/b` as server `Demo/a`. `/invokeTool` still
    uses `rpartition` for its own free-typed target, which is a different question: there
    the whole string is the user's, not one this agent generated.
    """
    server, _, tool = name.partition("/")
    raw, bare, positional = _parse_flags(name, tokens, "parameter", collect_positional=True)
    return InvokeTool(
        server=server or None,
        tool=tool,
        raw_arguments=raw,
        bare_flags=frozenset(bare),
        positional=tuple(positional),
        typed_as=f"/{name}",
    )


def _parse_bare(name: str, tokens: list[str], command: Command) -> Command:
    """One of the three listings, which take nothing at all."""
    if tokens:
        raise CommandError(f"/{name} takes no arguments, but got: {' '.join(tokens)}")
    return command


def _parse_invocation(tokens: list[str]) -> InvokeTool:
    if not tokens:
        raise CommandError(
            f"/{INVOKE_TOOL} needs a tool to call: /{INVOKE_TOOL} {INVOKE_TOOL_HINT}"
        )
    target = tokens[0]
    if target.startswith("-"):
        raise CommandError(
            f"/{INVOKE_TOOL}: the first argument is the tool, not an option. "
            f"Try /{INVOKE_TOOL} {INVOKE_TOOL_HINT}"
        )
    server, _, tool = target.rpartition("/")
    if not tool:
        raise CommandError(f"/{INVOKE_TOOL}: {target!r} names no tool.")

    raw, bare, positional = _parse_flags(
        INVOKE_TOOL, tokens[1:], "parameter", collect_positional=True
    )
    return InvokeTool(
        server=server or None,
        tool=tool,
        raw_arguments=raw,
        bare_flags=frozenset(bare),
        positional=tuple(positional),
        typed_as=f"/{INVOKE_TOOL} {target}",
    )


def _parse_prompt(
    kind: type[PromptCommand], verb: str, hint: str, tokens: list[str]
) -> PromptCommand:
    """`/promptShow` and `/promptInvoke`, which differ only in what happens afterwards.

    Same `[<server>/]<name>` target as `/invokeTool`, because a prompt name is an
    identifier the way a tool name is — the split that `/resourceShow` cannot use is fine
    here.
    """
    if not tokens:
        raise CommandError(f"/{verb} needs a prompt: /{verb} {hint}")
    target = tokens[0]
    if target.startswith("-"):
        raise CommandError(
            f"/{verb}: the first argument is the prompt, not an option. Try /{verb} {hint}"
        )
    server, _, name = target.rpartition("/")
    if not name:
        raise CommandError(f"/{verb}: {target!r} names no prompt.")

    raw, bare, _ = _parse_flags(verb, tokens[1:], "argument")
    return kind(
        server=server or None, name=name, raw_arguments=raw, bare_flags=frozenset(bare)
    )


def _parse_resource(tokens: list[str]) -> ShowResource:
    """`/resourceShow [<server>] <uri>`, positionally.

    The count is the discriminator, not the shape of either token. Sniffing for `://` to
    decide which one is the URI would be a guess in exactly the place `_resolve_server`
    already refuses to guess, and MCP puts no constraint on a resource URI's scheme that
    would make the sniff safe.
    """
    if not tokens:
        raise CommandError(
            f"/{RESOURCE_SHOW} needs a resource: /{RESOURCE_SHOW} {RESOURCE_SHOW_HINT}. "
            f"Run /{LIST_RESOURCES} to see them."
        )
    for token in tokens:
        if token.startswith("--"):
            raise CommandError(
                f"/{RESOURCE_SHOW}: {token!r} is an option, and a resource takes none. "
                f"It is read by URI: /{RESOURCE_SHOW} {RESOURCE_SHOW_HINT}."
            )
    if len(tokens) == 1:
        return ShowResource(server=None, uri=tokens[0])
    if len(tokens) == 2:
        return ShowResource(server=tokens[0], uri=tokens[1])
    raise CommandError(
        f"/{RESOURCE_SHOW} takes a URI and an optional server before it, but got "
        f"{len(tokens)} arguments: {' '.join(tokens)}. A URI containing spaces needs "
        "quoting."
    )


def _parse_flags(
    verb: str, tokens: list[str], noun: str, *, collect_positional: bool = False
) -> tuple[dict[str, list[str]], set[str], list[str]]:
    """`--key value`, `--key=value` and `--key` into raw strings, for every command.

    Shared by `/invokeTool` and the two prompt commands so that the one syntax a person has
    to learn really is one syntax. What the values *mean* diverges afterwards — a tool's
    are typed from its `inputSchema`, a prompt's are strings — and that divergence lives in
    `coerce_arguments` and `prompt_arguments`, not here.

    `collect_positional` is what the two tool commands pass, and it changes only *where*
    an unnamed token is refused, never whether. Refused here, the message could name no
    parameter but the failing token, so it advised `--foo <value>` for a token that was the
    user's *value* — reading as though `--foo` were a real parameter of the tool
    (`pyacp-ysq`). The tool's own parameters are known one await later, in
    `positional_argument_error`, so the tokens are carried there instead. The prompt
    commands still refuse on the spot: `prompts/list` describes a prompt's arguments far
    more thinly than `inputSchema` describes a tool's, so deferring would buy them nothing.
    """
    raw: dict[str, list[str]] = {}
    bare: set[str] = set()
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            if collect_positional:
                positional.append(token)
                index += 1
                continue
            # Built in a local rather than inline: a nested f-string reusing the outer
            # quote character is PEP 701, which is 3.12+, and this project floors at 3.11.
            shape = code_span("--{} <value>".format(token.lstrip("-") or "name"))
            raise CommandError(
                f"/{verb}: unexpected argument {token!r}. Every {noun} is named: {shape}."
            )
        key, separator, inline = token[2:].partition("=")
        if not key:
            raise CommandError(f"/{verb}: {token!r} names no {noun}.")
        if separator:
            raw.setdefault(key, []).append(inline)
            index += 1
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is None or following.startswith("--"):
            # A flag with nothing after it. Recorded rather than resolved: whether it means
            # `true` or is a missing value depends on the schema, which parsing cannot see.
            bare.add(key)
            raw.setdefault(key, [])
            index += 1
            continue
        raw.setdefault(key, []).append(following)
        index += 2
    return raw, bare, positional


# ---------------------------------------------------------------------------
# Typing the arguments, which needs the tool's schema
# ---------------------------------------------------------------------------


def invocation_prefix(command: InvokeTool) -> str:
    """The command and target as the reader wrote them, for a message they can paste back.

    `/Demo/echo` and `/invokeTool Demo/echo` are the same call, and a refusal that answers
    one in the other's spelling makes the reader translate before they can retry. The
    fallback is only for an `InvokeTool` built by hand, which is to say by a test.
    """
    return command.typed_as or f"/{INVOKE_TOOL} {_target(command)}"


def _quoted(value: str) -> str:
    """`value` as it would have to be typed back, quoted only when `shlex` would split it.

    The example a refusal offers has to survive `parse_command`'s own `shlex.split`, and
    the whole reason this function exists is the observed case: `/Demo/echo foo bar`, whose
    two loose tokens are one value that wanted quoting.
    """
    try:
        if shlex.split(value) == [value]:
            return value
    except ValueError:
        pass
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def positional_argument_error(command: InvokeTool, schema: dict[str, Any] | None) -> CommandError:
    """Why `/Demo/echo foo bar` is refused, said with the tool's real parameters in hand.

    The parser cannot write this message. It sees a token with no `--name` in front of it
    and nothing else, so the best it could do was echo the token back as `--foo <value>` —
    advice to name a parameter after the reader's own *value*, which does not exist and
    fails differently when followed (`pyacp-ysq`). By here the tool has been resolved and
    its `inputSchema` fetched, so the message can name what the tool actually takes.

    Three shapes, because three things can be true and only one of them is "you meant a
    flag": the tool may declare parameters, declare none, or publish no schema at all. The
    last is not the same as the second — a server that omits `inputSchema` has said nothing
    about its parameters, and reporting that as "takes no parameters" would invent a fact.

    The `Try` line is offered only when one parameter is unambiguous — the tool has exactly
    one, or exactly one required. With several to choose from, picking one would be the
    same guess `_resolve_server` refuses to make about servers.
    """
    prefix = invocation_prefix(command)
    tokens = command.positional
    # Code spans rather than `repr`: these are the reader's own text, and a token like
    # `<b>` quoted into prose is deleted outright by a Markdown client — see
    # `tests/test_markdown.py`. The span also keeps a trailing curly quote visible, which
    # is the one character the note underneath is about.
    listed = ", ".join(code_span(token) for token in tokens)
    subject = (
        f"{listed} is a value with no parameter in front of it"
        if len(tokens) == 1
        else f"{listed} are values with no parameter in front of them"
    )

    if not isinstance(schema, dict):
        return CommandError(
            f"{prefix}: {subject}, and this tool publishes no input schema to name "
            f"them from. Every parameter is still named: "
            f"{code_span('--<name> <value>')}.{command.positional_note}"
        )

    raw_properties = schema.get("properties")
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    if not properties:
        return CommandError(
            f"{prefix}: this tool takes no parameters, but got "
            f"{listed}.{command.positional_note}"
        )

    raw_required = schema.get("required")
    required = [
        name
        for name in (raw_required if isinstance(raw_required, list) else [])
        if isinstance(name, str) and name in properties
    ]
    chosen: str | None = None
    if len(properties) == 1:
        chosen = next(iter(sorted(properties)))
    elif len(required) == 1:
        chosen = required[0]

    message = (
        f"{prefix}: {subject}. Every parameter is named: "
        f"{code_span(tool_command_hint({'inputSchema': schema}))}."
    )
    # No example when a curly quote is the likelier cause: the note below already ends
    # in the straightened command, and an example built from `there”` — the fragment the
    # quote left behind — would be a second, contradictory instruction.
    if chosen is not None and not command.positional_note:
        example = f"{prefix} --{chosen} {_quoted(' '.join(tokens))}"
        message = f"{message} Try {code_span(example)}."
    # Last, so the note reads as the aside it is — the finding above it is still the
    # accurate account of what the parser found. Same order `parse_command` uses.
    return CommandError(f"{message}{command.positional_note}")


def coerce_arguments(command: InvokeTool, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Turn raw strings into the JSON types the tool's `inputSchema` asks for.

    Without a schema — a server that does not publish one, or a property it does not
    declare — the value is read as JSON and kept as a string when that fails. So `3` is a
    number and `hello` is a string, which is the guess a person typing a command line
    expects. It is only a guess: a schema is what makes it a fact, and a tool that wants
    the *string* `"3"` for an undeclared property cannot be reached this way. Declared
    properties never guess.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    if isinstance(schema, dict):
        raw_properties = schema.get("properties")
        if isinstance(raw_properties, dict):
            properties = raw_properties
        raw_required = schema.get("required")
        if isinstance(raw_required, list):
            required = [name for name in raw_required if isinstance(name, str)]

    if properties:
        unknown = sorted(set(command.raw_arguments) - set(properties))
        if unknown:
            offered = ", ".join(f"--{name}" for name in sorted(properties)) or "none"
            raise CommandError(
                f"{invocation_prefix(command)}: no parameter "
                f"{', '.join('--' + name for name in unknown)}. It takes: {offered}."
            )

    arguments: dict[str, Any] = {}
    for key, values in command.raw_arguments.items():
        spec = properties.get(key) if isinstance(properties.get(key), dict) else {}
        declared = spec.get("type") if isinstance(spec, dict) else None
        if key in command.bare_flags and not values:
            if declared in (None, "boolean"):
                arguments[key] = True
                continue
            raise CommandError(
                f"/{INVOKE_TOOL} {_target(command)}: --{key} is {declared} and needs a "
                f"value: --{key} <{declared}>."
            )
        arguments[key] = _coerce(key, values, spec, command)

    missing = [name for name in required if name not in arguments]
    if missing:
        raise CommandError(
            f"/{INVOKE_TOOL} {_target(command)}: missing required "
            f"{', '.join('--' + name for name in missing)}."
        )
    return arguments


def _coerce(key: str, values: list[str], spec: dict[str, Any], command: InvokeTool) -> Any:
    declared = spec.get("type") if isinstance(spec, dict) else None

    if declared == "array" or len(values) > 1:
        items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        # One `--key` repeated is the list; a single JSON array literal is accepted too,
        # because a person who already knows JSON should not have to discover this rule.
        if len(values) == 1:
            decoded = _maybe_json(values[0])
            if isinstance(decoded, list):
                return decoded
        return [_scalar(key, value, items, command) for value in values]

    if not values:
        raise CommandError(f"/{INVOKE_TOOL} {_target(command)}: --{key} needs a value.")
    return _scalar(key, values[0], spec, command)


def _scalar(key: str, value: str, spec: dict[str, Any], command: InvokeTool) -> Any:
    declared = spec.get("type") if isinstance(spec, dict) else None
    if declared == "string":
        return value
    if declared in {"number", "integer"}:
        try:
            return int(value) if declared == "integer" else float(value)
        except ValueError:
            raise CommandError(
                f"/{INVOKE_TOOL} {_target(command)}: --{key} is {declared}, "
                f"and {value!r} is not one."
            ) from None
    if declared == "boolean":
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        raise CommandError(
            f"/{INVOKE_TOOL} {_target(command)}: --{key} is boolean, and {value!r} is "
            "neither true nor false."
        )
    if declared in {"object", "array"}:
        decoded = _maybe_json(value)
        if isinstance(decoded, (dict, list)):
            return decoded
        example = (
            f"--{key} '{{\"a\": 1}}'"
            if declared == "object"
            else f"--{key} one --{key} two, or --{key} '[\"one\"]'"
        )
        raise CommandError(
            f"/{INVOKE_TOOL} {_target(command)}: --{key} is {declared}, so it takes "
            f"JSON: {example}."
        )
    return _maybe_json(value)


def _maybe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def _target(command: InvokeTool) -> str:
    return f"{command.server}/{command.tool}" if command.server else command.tool


def _prompt_target(command: PromptCommand) -> str:
    return f"{command.server}/{command.name}" if command.server else command.name


# ---------------------------------------------------------------------------
# Typing a prompt's arguments, which needs no typing at all
# ---------------------------------------------------------------------------


def prompt_arguments(
    command: PromptCommand, declared: list[Any] | None
) -> dict[str, str]:
    """Check a prompt's arguments against what it declares, and hand back strings.

    `coerce_arguments`' counterpart, and much shorter, because MCP types `prompts/get`'s
    arguments as `{[key: string]: string}`. There is no `inputSchema`, no `type`, and
    nothing to coerce — a prompt argument is a string, and the only questions left are
    whether the prompt has an argument by that name and whether a required one is missing.

    `declared` is the `arguments` array from `prompts/list`: `{name, description?,
    required?}` per entry. `None` — a server that publishes no argument list for the
    prompt — means nothing can be checked and everything is passed through, the same
    latitude `coerce_arguments` gives a tool with no schema.
    """
    known: dict[str, dict[str, Any]] = {}
    if isinstance(declared, list):
        for entry in declared:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                known[entry["name"]] = entry

    if known:
        unknown = sorted(set(command.raw_arguments) - set(known))
        if unknown:
            offered = ", ".join(f"--{name}" for name in sorted(known)) or "none"
            raise CommandError(
                f"/{command.verb} {_prompt_target(command)}: no argument "
                f"{', '.join('--' + name for name in unknown)}. It takes: {offered}."
            )

    arguments: dict[str, str] = {}
    for key, values in command.raw_arguments.items():
        if not values:
            # Never a boolean the way a bare tool flag can be: there is no schema to say
            # so, and MCP has no non-string prompt argument to say it about.
            raise CommandError(
                f"/{command.verb} {_prompt_target(command)}: --{key} needs a value. "
                "A prompt argument is a string."
            )
        if len(values) > 1:
            raise CommandError(
                f"/{command.verb} {_prompt_target(command)}: --{key} was given "
                f"{len(values)} times, and a prompt argument is a single string."
            )
        arguments[key] = values[0]

    missing = [
        name
        for name, entry in sorted(known.items())
        if entry.get("required") is True and name not in arguments
    ]
    if missing:
        raise CommandError(
            f"/{command.verb} {_prompt_target(command)}: missing required "
            f"{', '.join('--' + name for name in missing)}."
        )
    return arguments


# ---------------------------------------------------------------------------
# Rendering, which is the whole point of `/tools`
# ---------------------------------------------------------------------------

#: Two spaces of indent per level. A deep indent runs out of room in a chat transcript,
#: and this is the most structure a narrow one can carry.
#:
#: The indent is only meaningful because every block that uses it is emitted inside a
#: fenced code block — see `markdown.py`. Outside a fence a Markdown renderer treats these
#: lines as a lazy paragraph continuation and reflows them into one run-on line, which is
#: what `pyacp-nlv` was filed for. Do not indent a line that is not going inside a fence.
_INDENT = "  "


def render_tool_listing(listings: dict[str, list[dict[str, Any]]]) -> str:
    """The `/tools` answer: every tool on every configured server, with its parameters.

    Three parts, and the middle one is fenced: a prose summary, the aligned body inside a
    code fence, and a prose invitation whose example is a code span. The split is what
    `pyacp-nlv` established — a client renders this string as Markdown, so the columns
    survive only inside a fence and `<string>` survives only inside a fence or a span.
    """
    if not listings:
        return _no_servers("tools")

    total = sum(len(tools) for tools in listings.values())
    body: list[str] = []
    for server in sorted(listings):
        tools = listings[server]
        if body:
            body.append("")
        body.append(f"{server} ({len(tools)} tool{'' if len(tools) == 1 else 's'})")
        if not tools:
            body.append(f"{_INDENT}(this server publishes no tools)")
            continue
        for tool in tools:
            body.extend(_render_tool(server, tool))

    lines = [
        f"{total} tool{'' if total == 1 else 's'} on "
        f"{len(listings)} server{'' if len(listings) == 1 else 's'}.",
        "",
        *fenced_lines(body),
        "",
        f"Call one with: {code_span(f'/{INVOKE_TOOL} {_example(listings)}')}",
    ]
    return "\n".join(lines)


def _render_tool(server: str, tool: dict[str, Any]) -> list[str]:
    name = tool.get("name")
    if not isinstance(name, str):
        return []
    description = tool.get("description") or ""
    lines = ["", f"{_INDENT}{server}/{name}"]
    if description:
        lines.append(f"{_INDENT * 2}{description}")

    schema = tool.get("inputSchema")
    properties: dict[str, Any] = {}
    required: set[str] = set()
    if isinstance(schema, dict):
        if isinstance(schema.get("properties"), dict):
            properties = schema["properties"]
        if isinstance(schema.get("required"), list):
            required = {name for name in schema["required"] if isinstance(name, str)}
    if not properties:
        lines.append(f"{_INDENT * 2}(no parameters)")
        return lines

    # One pass to size the columns: a ragged left edge is what makes a parameter list
    # unreadable, and the width is not knowable until every name is in hand.
    flags = {key: f"--{key}" for key in properties}
    width = max(len(flag) for flag in flags.values())
    for key in sorted(properties):
        spec = properties[key] if isinstance(properties[key], dict) else {}
        declared = spec.get("type") if isinstance(spec, dict) else None
        kind = f"<{declared}>" if isinstance(declared, str) else "<any>"
        mark = "required" if key in required else ""
        detail = spec.get("description") if isinstance(spec, dict) else None
        parts = [f"{flags[key]:<{width}}", f"{kind:<11}", f"{mark:<9}"]
        if isinstance(detail, str) and detail:
            parts.append(detail)
        lines.append(f"{_INDENT * 2}{' '.join(parts).rstrip()}")
    return lines


def tool_command_hint(tool: dict[str, Any]) -> str:
    """The `input.hint` for one MCP tool's own palette entry.

    Every built-in command carries a hint and, until `pyacp-acn`, the per-tool entries did
    not — so a client's composer offered `Demo/echo` with nothing at all about how to pass
    it anything. The observed consequence was a user typing `/Demo/echo foo bar`
    positionally, because nothing on screen said the parameter was named `--text`.

    Required parameters are shown bare and optional ones in brackets, which is the
    convention `INVOKE_TOOL_HINT` already uses and the one a reader of any command line
    expects. Types come from the schema when it declares them; `<value>` is the honest
    answer when it does not, and the whole hint says so when there is no schema at all.
    """
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return "(no parameters)"
    required: set[str] = set()
    if isinstance(schema, dict) and isinstance(schema.get("required"), list):
        required = {key for key in schema["required"] if isinstance(key, str)}
    parts: list[str] = []
    for key in sorted(properties):
        spec = properties[key] if isinstance(properties[key], dict) else {}
        declared = spec.get("type") if isinstance(spec, dict) else None
        kind = f"<{declared}>" if isinstance(declared, str) else "<value>"
        flag = f"--{key} {kind}"
        parts.append(flag if key in required else f"[{flag}]")
    # Required first, so the shortest call that works reads off the front of the hint.
    return " ".join(sorted(parts, key=lambda part: part.startswith("[")))


def _example(listings: dict[str, list[dict[str, Any]]]) -> str:
    """A call the reader could actually paste, taken from what this session really has.

    A generic `<server>/<tool>` teaches the shape and not the vocabulary; naming a real
    tool with its own first required parameter means the example runs.
    """
    for server in sorted(listings):
        for tool in listings[server]:
            name = tool.get("name")
            if not isinstance(name, str):
                continue
            schema = tool.get("inputSchema")
            required: list[str] = []
            if isinstance(schema, dict) and isinstance(schema.get("required"), list):
                required = [key for key in schema["required"] if isinstance(key, str)]
            suffix = f" --{required[0]} <value>" if required else ""
            return f"{server}/{name}{suffix}"
    return f"<server>/<tool> {INVOKE_TOOL_HINT}"


# ---------------------------------------------------------------------------
# Rendering the other two primitives
# ---------------------------------------------------------------------------

def _no_servers(noun: str) -> str:
    """What every listing says when the session opened no MCP servers at all.

    One function because the three listings are equally useless in that case and for the
    same reason, and three near-identical paragraphs would drift. "No servers" and "no
    prompts" are different problems, and only one of them is the reader's to fix.
    """
    example = '"mcpServers": [{"name": "demo", "command": "python", "args": ["server.py"]}]'
    return "\n".join(
        [
            f"This session has no MCP servers, so there are no {noun}.",
            "Servers are named when the session is created, in `session/new`:",
            "",
            *fenced_lines([example]),
        ]
    )


def _nothing_declared(undeclared: list[str], noun: str, capability: str) -> str:
    """The whole answer when *every* server on the session lacks the capability.

    Separate from `_undeclared_note`, which annotates a listing that has something in it.
    Here there is nothing to annotate, and appending that note to a sentence already saying
    the same thing would say it twice.
    """
    plural = "" if len(undeclared) == 1 else "s"
    names = ", ".join(sorted(undeclared))
    return (
        f"No {noun}: none of this session's {len(undeclared)} MCP server{plural} declares "
        f"the {capability} capability ({names})."
    )


def _undeclared_note(undeclared: list[str], capability: str) -> list[str]:
    """Name the servers that were never asked, and say why.

    A listing that silently omits a server is indistinguishable from one whose server had
    nothing to list, and the two want different reactions from the reader: one is a server
    that does not do this, the other is a server that does it and is empty. MCP's
    capability block is what separates them, so it is reported rather than absorbed.
    """
    if not undeclared:
        return []
    names = ", ".join(sorted(undeclared))
    one = len(undeclared) == 1
    return [
        "",
        f"{names} {'declares' if one else 'declare'} no {capability} capability, "
        f"so {'it was' if one else 'they were'} not asked.",
    ]


def render_prompt_listing(
    listings: dict[str, list[dict[str, Any]]], undeclared: list[str] | None = None
) -> str:
    """The `/listPrompts` answer: every prompt, with the arguments it declares.

    Same three-part shape as `render_tool_listing`, and fenced for the same reason. The
    undeclared-servers note stays **outside** the fence: it is prose about the listing
    rather than part of it, and inside the fence it would render as monospaced output.
    """
    undeclared = list(undeclared or [])
    if not listings and not undeclared:
        return _no_servers("prompts")
    if not listings:
        return _nothing_declared(undeclared, "prompts", "prompts")

    total = sum(len(prompts) for prompts in listings.values())
    body: list[str] = []
    for server in sorted(listings):
        prompts = listings[server]
        if body:
            body.append("")
        body.append(f"{server} ({len(prompts)} prompt{'' if len(prompts) == 1 else 's'})")
        if not prompts:
            body.append(f"{_INDENT}(this server publishes no prompts)")
            continue
        for prompt in prompts:
            body.extend(_render_prompt(server, prompt))

    lines = [
        f"{total} prompt{'' if total == 1 else 's'} on "
        f"{len(listings)} server{'' if len(listings) == 1 else 's'}.",
        "",
        *fenced_lines(body),
        *_undeclared_note(undeclared, "prompts"),
        "",
        f"Expand one with: {code_span(f'/{PROMPT_SHOW} {_prompt_example(listings)}')}",
    ]
    return "\n".join(lines)


def _render_prompt(server: str, prompt: dict[str, Any]) -> list[str]:
    name = prompt.get("name")
    if not isinstance(name, str):
        return []
    lines = ["", f"{_INDENT}{server}/{name}"]
    description = prompt.get("description") or ""
    if description:
        lines.append(f"{_INDENT * 2}{description}")

    arguments = [
        entry
        for entry in (prompt.get("arguments") or [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    if not arguments:
        lines.append(f"{_INDENT * 2}(no arguments)")
        return lines

    # Same column-sizing pass as `_render_tool`: a ragged left edge is what makes one of
    # these unreadable, and the width is not knowable until every name is in hand.
    flags = {str(entry["name"]): f"--{entry['name']}" for entry in arguments}
    width = max(len(flag) for flag in flags.values())
    for entry in sorted(arguments, key=lambda item: str(item["name"])):
        key = str(entry["name"])
        mark = "required" if entry.get("required") is True else ""
        parts = [f"{flags[key]:<{width}}", f"{mark:<9}"]
        detail = entry.get("description")
        if isinstance(detail, str) and detail:
            parts.append(detail)
        lines.append(f"{_INDENT * 2}{' '.join(parts).rstrip()}")
    return lines


def _prompt_example(listings: dict[str, list[dict[str, Any]]]) -> str:
    """A `/promptShow` the reader could paste, built from this session's own prompts."""
    for server in sorted(listings):
        for prompt in listings[server]:
            name = prompt.get("name")
            if not isinstance(name, str):
                continue
            required = [
                entry["name"]
                for entry in (prompt.get("arguments") or [])
                if isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and entry.get("required") is True
            ]
            suffix = f" --{required[0]} <value>" if required else ""
            return f"{server}/{name}{suffix}"
    return f"<server>/<prompt> {PROMPT_SHOW_HINT}"


def render_prompt_heading(server: str, name: str, result: dict[str, Any]) -> str:
    """The one line that precedes an expanded prompt's messages.

    Names the prompt and how many messages came back, so a reader can tell an empty
    expansion from a failed one — the difference is invisible otherwise, since an empty
    `messages` array emits no chunks at all.
    """
    messages = result.get("messages")
    count = len(messages) if isinstance(messages, list) else 0
    description = result.get("description")
    heading = code_span(f"{server}/{name}")
    if isinstance(description, str) and description:
        heading = f"{heading} — {description}"
    if count == 0:
        return f"{heading}\n\n(this prompt expanded to no messages)"
    return f"{heading}\n\n{count} message{'' if count == 1 else 's'}, addressed to a model:"


def prompt_message_blocks(result: dict[str, Any]) -> list[tuple[str, Any]]:
    """An expanded prompt as `(role, raw MCP content block)` pairs, in order.

    The blocks are handed back **unmapped**. `mcp_content.to_content_block` is what turns
    one into ACP, and calling it here would make this module depend on the ACP schema to
    do a job — parsing and rendering text — that it otherwise does without one. The caller
    already imports that mapping for tool results.

    MCP types `PromptMessage.content` as a single content block. A list is accepted anyway:
    it costs one `isinstance` and a server that sends one would otherwise have its whole
    message silently rendered as a placeholder.
    """
    pairs: list[tuple[str, Any]] = []
    for message in result.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        role = role if isinstance(role, str) and role else "unknown"
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        pairs.extend((role, block) for block in blocks if block is not None)
    return pairs


def render_resource_listing(
    listings: dict[str, list[dict[str, Any]]], undeclared: list[str] | None = None
) -> str:
    """The `/listResources` answer: uri, name, mime type. Never content.

    Fenced body, prose summary and invitation — see `render_tool_listing`.
    """
    undeclared = list(undeclared or [])
    if not listings and not undeclared:
        return _no_servers("resources")
    if not listings:
        return _nothing_declared(undeclared, "resources", "resources")

    total = sum(len(resources) for resources in listings.values())
    body: list[str] = []
    for server in sorted(listings):
        resources = listings[server]
        if body:
            body.append("")
        body.append(
            f"{server} ({len(resources)} resource{'' if len(resources) == 1 else 's'})"
        )
        if not resources:
            body.append(f"{_INDENT}(this server publishes no resources)")
            continue
        for resource in resources:
            body.extend(_render_resource(resource))

    lines = [
        f"{total} resource{'' if total == 1 else 's'} on "
        f"{len(listings)} server{'' if len(listings) == 1 else 's'}.",
        "",
        *fenced_lines(body),
        *_undeclared_note(undeclared, "resources"),
        "",
        f"Read one with: {code_span(f'/{RESOURCE_SHOW} {_resource_example(listings)}')}",
    ]
    return "\n".join(lines)


def _render_resource(resource: dict[str, Any]) -> list[str]:
    uri = resource.get("uri")
    if not isinstance(uri, str):
        return []
    lines = ["", f"{_INDENT}{uri}"]
    detail = " ".join(
        str(resource[key]) for key in ("name", "mimeType") if isinstance(resource.get(key), str)
    )
    if detail:
        lines.append(f"{_INDENT * 2}{detail}")
    description = resource.get("description")
    if isinstance(description, str) and description:
        lines.append(f"{_INDENT * 2}{description}")
    return lines


def _resource_example(listings: dict[str, list[dict[str, Any]]]) -> str:
    """A `/resourceShow` the reader could paste. Always names the server explicitly.

    Unlike `/promptShow`, where omitting the server is the common case on a one-server
    session, the two-token form is what a reader needs to see: it is the only thing that
    says the server is a separate word rather than part of the URI.
    """
    for server in sorted(listings):
        for resource in listings[server]:
            uri = resource.get("uri")
            if isinstance(uri, str):
                return f"{server} {uri}"
    return "<server> <uri>"


def render_resource_contents(uri: str, result: dict[str, Any]) -> str:
    """One `resources/read` result as text.

    A resource is text-or-blob, exactly as an embedded resource is, and a blob is **never
    printed**. Base64 in a chat transcript is not readable by the human it would be shown
    to and is bounded only by the file's size; the placeholder names the type and the
    approximate decoded size instead, which is what a reader can act on.

    The contents go inside a fence, and that matters more here than in the listings: a
    resource's text is arbitrary and frequently *is* Markdown. Rendered as prose it would
    style itself — headings, lists, and its own fences — so a reader could not tell the
    resource's text from this agent's. `fenced_lines` sizes the fence past anything the
    body contains, so a resource holding a fenced block cannot close ours early.
    """
    contents = result.get("contents")
    contents = contents if isinstance(contents, list) else []
    if not contents:
        return f"{code_span(uri)}\n\n(this resource has no contents)"

    body: list[str] = []
    for entry in contents:
        if body:
            body.append("")
        if not isinstance(entry, dict):
            body.append(f"[not a resource contents object: {type(entry).__name__}]")
            continue
        header = str(entry.get("uri") or uri)
        mime = entry.get("mimeType")
        if isinstance(mime, str) and mime:
            header = f"{header}  {mime}"
        body.append(header)
        text = entry.get("text")
        if isinstance(text, str):
            body.append(text)
            continue
        blob = entry.get("blob")
        if isinstance(blob, str):
            body.append(f"{_INDENT}[binary, about {_decoded_size(blob)} bytes, not shown]")
            continue
        body.append(f"{_INDENT}[this content carries neither text nor blob]")

    heading = (
        f"{code_span(uri)} — {len(contents)} content{'' if len(contents) == 1 else 's'}."
    )
    return "\n".join([heading, "", *fenced_lines(body)])


def _decoded_size(blob: str) -> int:
    """How many bytes a base64 string decodes to, without decoding it.

    Four base64 characters carry three bytes, less one per `=` of padding. Computed rather
    than measured because the whole point of the placeholder is not to materialise the
    blob a second time to describe it.
    """
    return max(0, (len(blob) * 3) // 4 - blob.count("="))
