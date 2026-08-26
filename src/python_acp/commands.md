# `commands.py` — the two commands a person types, in front of the JSON convention

The turn convention is one JSON object per prompt block, which is right for a program and
hostile to a person. Nobody types

```json
{"tool": "echo", "arguments": {"text": "hi"}}
```

into a chat composer to find out what a server offers. `commands.py` parses the two
commands that make the same machinery reachable by hand:

```
/tools
/invokeTool demo/echo --text "hello world" --count 3
```

## Neither one is a new protocol surface

They arrive as ordinary `session/prompt` text and answer with `agent_message_chunk` text.
Nothing here reopens `pyacp-sld.2` — the decision that a client does **not** reach through
this bridge to the MCP server — because nothing new leaves the bridge. `/tools` renders
detail about tools that `available_commands` already announces on every turn, and
`/invokeTool` calls `tools/call`, which the JSON convention has always called.

The slash is optional on input. A client that fills its composer from
`available_commands` sends the name it was given, and a person typing by hand may or may
not reach for the slash first; both arrive here.

## `/invokeTool` builds the same `Invocation` that JSON builds

This is the load-bearing choice. `_from_command` in `turn_mcp_router.py` produces the
same frozen `Invocation` dataclass the JSON path produces, so a command-line call
inherits the session mode, the permission prompt, the tool-call `kind` from the server's
annotations, and the on-tool-failure policy **without knowing any of them exist** — and
cannot drift from them, because it has no copy of them to drift.

`read`, `write` and `run` are not offered. They exist for a caller composing a file or a
shell command around a tool call, which is a JSON author's job. A person at a prompt asks
for one tool.

## Typing arguments needs the schema, and says so when it lacks one

Parsing and typing are two passes on purpose. `parse_command` reports a malformed command
without touching a server; `coerce_arguments` then applies the tool's `inputSchema`,
which the caller had to `await`.

| Declared type | `--flag value` becomes |
| --- | --- |
| `string` | the string, untouched — `--count 3` stays `"3"` |
| `integer` / `number` | `int` / `float`, refused if the text is not one |
| `boolean` | `true`/`1`/`yes` and `false`/`0`/`no`; a bare `--flag` is `true` |
| `array` | the flag repeated, or one JSON array literal |
| `object` | a JSON literal |
| undeclared | read as JSON, kept as a string when that fails |

**Only the last row guesses.** For an undeclared property `3` becomes a number and
`hello` stays a string, which is what someone typing a command line expects — but it is a
guess, and a tool wanting the *string* `"3"` for a property it never declared cannot be
reached this way. Declared properties never guess, which is why a server that publishes
schemas gets exact behaviour and one that does not gets a documented approximation.

A property the schema does not declare is refused outright when the schema declares any
properties at all, with the list of ones it does take. Silently forwarding an unknown
argument would turn a typo into an MCP-side validation error, which reaches the person as
someone else's error message about a tool they typed correctly.

## Rendering is plain text, deliberately

`render_tool_listing` returns a multi-line string with two-space indents and no Markdown.
`agent_message_chunk` carries no content type, so a client that does not render Markdown
would show the asterisks; a wide table wraps into gibberish in a chat transcript. Two
levels of indent is the most structure that survives being reflowed.

The listing ends with an example built from what the session actually has — a real server,
a real tool, its own first required parameter — rather than `<server>/<tool>`. A generic
example teaches the shape and not the vocabulary, and this one can be pasted.

An empty session gets the `session/new` shape instead of an empty list, because "no tools"
and "no servers" are different problems and only one of them is the reader's to fix.

## Errors are refusals, not wire errors

`CommandError` is a `ValueError`, so an escape maps to `-32602` via `errors.py` — the
prompt is a parameter. In practice it never escapes: `turn_mcp_router.py` catches it and
re-raises `CommandRefused`, a `PromptConventionError` whose `explains_convention` is
`False`. Someone who typed a slash command is not reaching for the JSON convention, so
appending the JSON footer to their refusal would send them looking for a mistake they did
not make.

Text that is *not* one of the two commands is not an error at all. `parse_command` returns
`None` and the prompt falls through to the JSON convention unchanged, which is what keeps
this layer additive: every prompt that worked before this module existed takes the same
path it did.

## Main symbols

- `parse_command(text)`: `ListTools`, `InvokeTool`, or `None` for "not a command".
- `coerce_arguments(command, schema)`: raw strings to JSON types, per the table above.
- `render_tool_listing(listings)`: the `/tools` answer, as plain text.
- `CommandError`: recognised, and wrong.
- `LIST_TOOLS`, `INVOKE_TOOL`, and their hints: the names and syntax strings, shared with
  the `available_commands` announcement so the palette and the parser cannot disagree.

## Related

- [turn_mcp_router.py](turn_mcp_router.md) — the only caller: dispatch, server
  resolution, and the `Invocation` both paths share
- [mcp_tools.py](mcp_tools.md) — the per-turn `ToolCatalogue` both the listing and the
  announcement read, so neither costs a second `tools/list`
- [turns.py](turns.md) — `TurnContext.emit`, and the stop reason a command ends with
