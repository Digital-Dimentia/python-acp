# `commands.py` — the commands a person types, in front of the JSON convention

The turn convention is one JSON object per prompt block, which is right for a program and
hostile to a person. Nobody types

```json
{"tool": "echo", "arguments": {"text": "hi"}}
```

into a chat composer to find out what a server offers. `commands.py` parses the commands
that make the same machinery reachable by hand — one family per MCP server primitive:

```
/tools
/invokeTool demo/echo --text "hello world" --count 3
/listPrompts
/promptShow demo/greeting --name "Ada Lovelace"
/promptInvoke demo/greeting --name "Ada Lovelace"
/listResources
/resourceShow demo file:///etc/hosts
```

The slash is optional on input. A client that fills its composer from
`available_commands` sends the name it was given, and a person typing by hand may or may
not reach for the slash first; both arrive here.

## Three primitives, and the one line that divides them

MCP servers publish **tools**, **prompts**, and **resources**. Only tools were reachable
here before `pyacp-tc5`, which left the palette showing the *model's* callables — MCP
calls tools model-controlled — where the convention every other client follows surfaces
prompts, the user-controlled primitive. All three are reachable now, and what each command
can honestly do is fixed by decision D1, which puts no model in this runtime:

| Command | MCP call | Needs a model |
| --- | --- | --- |
| `/tools`, `/listPrompts`, `/listResources` | `*/list` | no — metadata |
| `/invokeTool` | `tools/call` | no — the client named the tool and its arguments |
| `/promptShow` | `prompts/get` | **no** — the *server* performs the substitution |
| `/resourceShow` | `resources/read` | no — reading is the whole operation |
| `/promptInvoke` | `prompts/get`, then act on the messages | **yes**, so it refuses |

`/promptShow` and `/promptInvoke` split on exactly that line, and it is worth being precise
about where it falls. Expanding a prompt is a **template substitution the server does**:
`prompts/get` takes `{name, arguments}` and hands back messages. Nothing in this process
reasons about anything to make that happen, which is why `/promptShow` can answer at all.
What comes back is a list of messages *addressed to a model*, and acting on them is the
half this build cannot do.

`/promptInvoke` ships anyway, refusing with the reason and handing back a `/promptShow`
that runs — the same principle that has `authenticate` answer `-32000` rather than going
missing. A client should discover a boundary, not an absence. It refuses **after**
validating the arguments and **before** calling `prompts/get`: it cannot use the
expansion, so paying for one would be a round trip and a server-side substitution
discarded, and validating first means a wrong argument gets named as a wrong argument
rather than buried under the missing model.

Resources have no such split. `resources/read` *is* the operation — there is no second
half to defer, so there is one verb and no `/resourceInvoke` beside it.

## This reopens `pyacp-sld.2`, deliberately

`pyacp-sld.2`/`sld.3` deleted the `{"action": ...}` surface and with it the MCP
passthrough, recording that "ACP's model is that the agent uses those internally, not that
a client reaches through it to the server" — so reading an MCP prompt or resource through
this bridge was decided *against*, not merely left undone.

`/promptShow` and `/resourceShow` reverse that for prompt and resource **content**, and it
is a reversal rather than a loophole. Arriving as a slash command inside a turn instead of
as a JSON-RPC method is a different door onto the same capability, and pretending otherwise
would be worse than saying so.

What still stands from `sld.2` is the part that was load-bearing: there is **no MCP method
on the ACP wire**, no process-wide server to address, and nothing here bypasses
`session/new`. A client asks in ACP; the agent is the one that speaks MCP. Do not read this
section as permission to put `prompts/get` back on the wire as a method — that decision is
unchanged.

The three listings never needed the argument. `/tools` already reports what
`available_commands` announces every turn, in more detail, and `/listPrompts` and
`/listResources` are the same kind of thing: a rendering of a server's own catalogue.

## `/invokeTool` builds the same `Invocation` that JSON builds

This is the load-bearing choice on the tool side. `_from_command` in
`turn_mcp_router.py` produces the same frozen `Invocation` dataclass the JSON path
produces, so a command-line call inherits the session mode, the permission prompt, the
tool-call `kind` from the server's annotations, and the on-tool-failure policy **without
knowing any of them exist** — and cannot drift from them, because it has no copy of them
to drift.

`read`, `write` and `run` are not offered. They exist for a caller composing a file or a
shell command around a tool call, which is a JSON author's job. A person at a prompt asks
for one tool.

## Typing a tool's arguments needs the schema, and says so when it lacks one

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

## Typing a prompt's arguments needs nothing at all

`prompt_arguments` is `coerce_arguments`' counterpart and has no table, because there is
nothing to type. **MCP types `prompts/get`'s arguments as `{[key: string]: string}`** —
there is no `inputSchema`, no declared type, and no non-string prompt argument anywhere in
the protocol.

That is the whole of the "shape problem" `pyacp-tc5` was filed over. The bead worried that
MCP's named, typed-looking prompt arguments could not survive the squeeze into ACP's
`AvailableCommandInput`, whose only variant is one free-text hint. They do not have to:
prompt arguments are **named strings**, and a command line carries named strings natively.
The hint is a display string either way.

What is left for `prompt_arguments` to do is check names against what `prompts/list`
declared for that prompt — `{name, description?, required?}` per entry:

- an argument the prompt does not declare is refused, with the ones it does take;
- a `required: true` argument that is missing is refused by name;
- `--flag` with no value is refused rather than read as `true`. A bare flag is a boolean
  only because a tool's schema can *say* it is one, and a prompt has no schema and no
  boolean to describe;
- `--flag a --flag b` is refused rather than becoming a list, for the same reason;
- a prompt whose entry declares no usable `arguments` array checks nothing and passes
  everything through — the same latitude `coerce_arguments` gives a tool with no schema.

## The server target: a slash for prompts, a space for resources

`/invokeTool`, `/promptShow` and `/promptInvoke` take `[<server>/]<name>`, split on the
last slash. A tool name and a prompt name are identifiers, so the split is safe.

**`/resourceShow` cannot use it.** A resource is addressed by URI, and `file:///etc/hosts`
is full of slashes — `rpartition("/")` would split it at `hosts`. So the server is a
separate positional token and the **count** is the discriminator:

```
/resourceShow file:///etc/hosts          # one token: the URI, server resolved
/resourceShow demo file:///etc/hosts     # two tokens: server, then URI
```

Sniffing for `://` to decide which token is which would be a guess in exactly the place
`_resolve_server` already refuses to guess, and MCP puts no constraint on a resource URI's
scheme that would make the sniff safe.

The same asymmetry reaches the *suggestion* an ambiguous session prints, which is why
`_resolve_server` takes a `separator`: with several servers open, `/promptShow` suggests
`alpha/greeting` and `/resourceShow` suggests `alpha greeting://ada`. Printing the wrong
one would be advice that does not run.

## Rendering is plain text, deliberately

The listings return a multi-line string with two-space indents and no Markdown.
`agent_message_chunk` carries no content type, so a client that does not render Markdown
would show the asterisks; a wide table wraps into gibberish in a chat transcript. Two
levels of indent is the most structure that survives being reflowed.

Each listing ends with an example built from what the session actually has — a real
server, a real tool or prompt, its own first required argument — rather than
`<server>/<name>`. A generic example teaches the shape and not the vocabulary, and this one
can be pasted. `/listResources`' example always spells the server out as a separate word,
because that is the only thing on the page that says it is one.

An empty session gets the `session/new` shape instead of an empty list, because "no
prompts" and "no servers" are different problems and only one of them is the reader's to
fix.

Three things a listing refuses to be silent about, because silence has a wrong reading:

- a server that publishes **none** of a primitive is still named, with `(this server
  publishes no prompts)`. Omitting it reads as "the server is missing";
- a server that **declared no capability** for a primitive is named separately, with why.
  Merged into the previous case, "does not do this" would be indistinguishable from "does
  this, and is empty" — and those want different reactions;
- an expanded prompt's **message count** leads `/promptShow`'s output. An expansion with no
  messages emits no chunks at all, so without the count it looks exactly like a call that
  failed.

`/promptShow` is the one command whose answer is not entirely plain text.
`prompt_message_blocks` hands back `(role, raw MCP content block)` pairs and the caller maps
each block through `mcp_content.to_content_block`, so an image in an expanded prompt stays
an image. `supported_prompt_blocks` governs what this agent *reads*; this is the outbound
direction, where the same rule that lets a tool return an image applies.

A **blob** resource is the exception that is never rendered: `render_resource_contents`
prints text verbatim and replaces base64 with `[binary, about N bytes, not shown]`. Base64
in a chat transcript is unreadable by the human it would be shown to and is bounded only by
the file's size; the size is the part a reader can act on.

## Errors are refusals, not wire errors

`CommandError` is a `ValueError`, so an escape maps to `-32602` via `errors.py` — the
prompt is a parameter. In practice it never escapes: `turn_mcp_router.py` catches it and
re-raises `CommandRefused`, a `PromptConventionError` whose `explains_convention` is
`False`. Someone who typed a slash command is not reaching for the JSON convention, so
appending the JSON footer to their refusal would send them looking for a mistake they did
not make.

Text that is *not* one of the commands is not an error at all. `parse_command` returns
`None` and the prompt falls through to the JSON convention unchanged, which is what keeps
this layer additive: every prompt that worked before this module existed takes the same
path it did.

The one place recognition happens **before** parsing is the unbalanced-quote guard: a stray
quote is only our problem when the text was aimed at us, and JSON's own parser gives a
better message about a stray quote in JSON than we can. That guard reads `COMMAND_NAMES`,
which is built from the name constants — a command added without being added to that set
would silently hand its own bad quoting to the JSON parser.

## Main symbols

- `parse_command(text)`: one of the seven command dataclasses, or `None` for "not a
  command".
- `Command`: the union `parse_command` returns, and what `turn_mcp_router` dispatches on.
- `ListTools` / `ListPrompts` / `ListResources`: the three listings. They carry nothing.
- `InvokeTool`: `<server>/<tool>` plus raw arguments and bare flags.
- `PromptCommand`, and its two subclasses `ShowPrompt` and `InvokePrompt`: `<server>/<name>`
  plus raw arguments. Two classes rather than a flag, because `execute` dispatches on which
  one it got; `verb` carries the name each error message should print.
- `ShowResource`: an optional server and a URI, never carved out of one token.
- `coerce_arguments(command, schema)`: a tool's raw strings to JSON types, per the table.
- `prompt_arguments(command, declared)`: a prompt's raw strings, checked and left as
  strings.
- `render_tool_listing` / `render_prompt_listing` / `render_resource_listing`: the three
  listings as plain text. The latter two take the servers that declared no capability.
- `render_prompt_heading(server, name, result)` and `prompt_message_blocks(result)`: the
  two halves of `/promptShow`'s answer — a text heading, then blocks for the caller to map.
- `render_resource_contents(uri, result)`: text verbatim, blobs as a sized placeholder.
- `CommandError`: recognised, and wrong.
- `COMMAND_NAMES`: every command name, for the one check that has to recognise before it
  can parse.
- `NEEDS_A_MODEL`: why `/promptInvoke` refuses, in one place because it is said twice.
- The name and hint constants, shared with the `available_commands` announcement so the
  palette and the parser cannot disagree.

## Related

- [turn_mcp_router.py](turn_mcp_router.md) — the only caller: dispatch, server
  resolution, the capability check, and the `Invocation` both tool paths share
- [mcp_tools.py](mcp_tools.md) — the per-turn `ToolCatalogue` the tool listing and the
  announcement read, so neither costs a second `tools/list`. There is deliberately no
  prompt or resource equivalent: those listings have one reader each
- [mcp_content.py](mcp_content.md) — the MCP-to-ACP content mapping `/promptShow` reuses
- [mcp_stdio.py](mcp_stdio.md) — `list_prompts`, `get_prompt`, `list_resources`,
  `read_resource`, and the `supports()` the capability check reads
- [turns.py](turns.md) — `TurnContext.emit`, and the stop reason a command ends with
