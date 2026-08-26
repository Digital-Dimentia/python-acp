"""Human-typed commands, in front of the JSON invocation convention.

The turn convention is a JSON object per prompt block (`turn_mcp_router.py`), which is
right for a program and hostile to a person: nobody types `{"tool": "echo", "arguments":
{"message": "hi"}}` into a chat box to find out what a server offers. These two commands
are the human door onto the same machinery.

    /tools
    /invokeTool demo/echo --message "hello world" --count 3

**Neither is a new protocol surface.** They arrive as ordinary `session/prompt` text and
answer with `agent_message_chunk` text, so nothing here reopens `pyacp-sld.2` — the
decision that a client does not reach *through* this bridge to the MCP server. `/tools`
reports what `available_commands` already announces every turn, in more detail; the
listing is a rendering of information the client is given anyway.

`/invokeTool` deliberately produces the same `Invocation` the JSON path produces, so a
command-line call is not a second execution path: it inherits the session mode, the
permission prompt, and the on-tool-failure policy without knowing they exist.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any

#: What a client renders as a slash command. Accepted with or without the slash: a client
#: that fills its composer from `available_commands` sends the name it was given, and a
#: person typing by hand may or may not reach for the slash first.
LIST_TOOLS = "tools"
INVOKE_TOOL = "invokeTool"

#: Shown in the `available_commands` announcement, and in the listing's own footer, so the
#: syntax is discoverable from inside the thing that needs it.
LIST_TOOLS_HINT = "list every tool on this session's MCP servers, with parameters"
INVOKE_TOOL_HINT = "<server>/<tool> --param value [--flag]"


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


def parse_command(text: str) -> ListTools | InvokeTool | None:
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
        if head not in {LIST_TOOLS, INVOKE_TOOL}:
            return None
        raise CommandError(
            f"/{head}: the command has an unbalanced quote. Wrap a value containing "
            'spaces in one pair of quotes: --message "hello world".'
        ) from None
    if not tokens:
        return None

    name = tokens[0].lstrip("/")
    if name == LIST_TOOLS:
        if len(tokens) > 1:
            raise CommandError(f"/{LIST_TOOLS} takes no arguments, but got: {' '.join(tokens[1:])}")
        return ListTools()
    if name == INVOKE_TOOL:
        return _parse_invocation(tokens[1:])
    return None


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

    raw: dict[str, list[str]] = {}
    bare: set[str] = set()
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            raise CommandError(
                f"/{INVOKE_TOOL}: unexpected argument {token!r}. Every parameter is named: "
                f"--{token.lstrip('-') or 'name'} <value>."
            )
        key, separator, inline = token[2:].partition("=")
        if not key:
            raise CommandError(f"/{INVOKE_TOOL}: {token!r} names no parameter.")
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

    return InvokeTool(
        server=server or None, tool=tool, raw_arguments=raw, bare_flags=frozenset(bare)
    )


# ---------------------------------------------------------------------------
# Typing the arguments, which needs the tool's schema
# ---------------------------------------------------------------------------


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
                f"/{INVOKE_TOOL} {_target(command)}: no parameter "
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


# ---------------------------------------------------------------------------
# Rendering, which is the whole point of `/tools`
# ---------------------------------------------------------------------------

#: Two spaces of indent per level. A client renders `agent_message_chunk` as prose in a
#: chat transcript, where a wide table wraps into gibberish and a deep indent runs out of
#: room; this is the most structure that survives being reflowed.
_INDENT = "  "


def render_tool_listing(listings: dict[str, list[dict[str, Any]]]) -> str:
    """The `/tools` answer: every tool on every configured server, with its parameters.

    A plain multi-line string, because the client this serves puts it in a transcript.
    Markdown is avoided deliberately — `agent_message_chunk` has no content type, so a
    client that does not render Markdown would show the asterisks.
    """
    if not listings:
        return (
            "This session has no MCP servers, so there are no tools.\n"
            "Servers are named when the session is created, in `session/new`:\n"
            f'{_INDENT}"mcpServers": [{{"name": "demo", "command": "python", '
            '"args": ["server.py"]}]'
        )

    lines: list[str] = []
    total = sum(len(tools) for tools in listings.values())
    lines.append(
        f"{total} tool{'' if total == 1 else 's'} on "
        f"{len(listings)} server{'' if len(listings) == 1 else 's'}."
    )
    for server in sorted(listings):
        tools = listings[server]
        lines.append("")
        lines.append(f"{server} ({len(tools)} tool{'' if len(tools) == 1 else 's'})")
        if not tools:
            lines.append(f"{_INDENT}(this server publishes no tools)")
            continue
        for tool in tools:
            lines.extend(_render_tool(server, tool))

    lines.append("")
    lines.append("Call one with:")
    example = _example(listings)
    lines.append(f"{_INDENT}/{INVOKE_TOOL} {example}")
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
