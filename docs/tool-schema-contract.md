# The tool-schema `_meta` contract

ACP gives a command exactly one argument shape: `UnstructuredCommandInput`, a single
free-text `hint`. An MCP tool, meanwhile, publishes a whole JSON Schema. `python-acp`
reads that schema to *build* the hint and then — until `pyacp-ma2` — threw the structure
away, so a client was handed a one-line summary it could not validate against and the
user learned the flag names and the legal enum values by trial and error.

`AvailableCommand` carries `_meta`, ACP's own extensibility point. This document is the
contract for what we put there: what an ACP client can rely on, and what an MCP server
author gets for writing a good schema.

Two audiences, one document, because the two halves only make sense together. **The
schema a server writes is the form a client renders, and this bridge is the pipe with no
intelligence of its own in between.**

Related: [`commands.md`](../src/python_acp/commands.md) for how a command is parsed and
coerced, [`turn_mcp_router.md`](../src/python_acp/turn_mcp_router.md) for where the
palette is built.

---

## The key

```json
{
  "name": "tools/echo",
  "description": "Echo the text back",
  "input": {"hint": "--text <string> [--times <integer>]"},
  "_meta": {
    "python-acp/tool": {
      "server": "tools",
      "tool": "echo",
      "inputSchema": {
        "type": "object",
        "properties": {
          "text": {"type": "string", "title": "Text"},
          "times": {"type": "integer", "minimum": 1, "default": 1}
        },
        "required": ["text"]
      }
    }
  }
}
```

`TOOL_META_KEY` in [`turn_mcp_router.py`](../src/python_acp/turn_mcp_router.py) is the
single definition of the string `"python-acp/tool"`.

`server` and `tool` are carried beside the schema so a client need not know that
`parse_command` splits the command name on the **first** slash — a rule that exists
because a server name may not contain one, and one no client should have to reimplement.

---

## For ACP clients

### Where it arrives

Both announcements, built by the same `_commands_for`:

| Announcement | When | Carries `_meta` |
| --- | --- | --- |
| `available_commands` on the session | Once, before the first turn | Yes |
| `session/update` → `available_commands_update` | At the start of every turn | Yes |
| The built-ins (`tools`, `listPrompts`, …) | Both, always last | **No** |

The built-ins have no MCP tool behind them and no `inputSchema` to forward. Only entries
named `<server>/<tool>` carry the key.

### What we guarantee

**Namespaced.** ACP says implementations MUST NOT make assumptions about `_meta` values,
so an unnamespaced `inputSchema` would be a land grab on a dict every extension shares. If
you want the idea without our namespace, read `_meta["python-acp/tool"].inputSchema` first
and fall back to a bare `_meta.inputSchema` — that fallback is a convention any agent may
adopt, and it is why this is an idea to copy rather than a `python-acp` feature.

**Verbatim.** What `tools/list` returned, unnormalised and unreordered. You are rendering
the *server's* vocabulary, not ours. Two consequences worth stating plainly: any weirdness
in a schema is upstream of this bridge, and cleaning it up on your side re-introduces the
disagreement we avoided by not cleaning it up on ours.

**Omitted, never null.** A tool that published no `inputSchema` said *nothing* about its
parameters. That is not the same as one that published `properties: {}` and thereby said
*it takes no parameters*, and the difference is load-bearing —
[`commands.md`](../src/python_acp/commands.md) already writes a different refusal for
each. Draw the same distinction: with no `inputSchema`, offer the text box; with an empty
property block, say the tool takes none. You will never receive `"inputSchema": null`, and
when the tool published nothing you will not receive the `python-acp/tool` key at all.

**The hint is unchanged.** `input.hint` is exactly what it was before `_meta` existed and
is always present. A client that ignores `_meta` entirely sees the session it saw before —
the property that made this shippable ahead of any client.

### What we do not guarantee

**We do not validate the schema.** It is forwarded as received. Treat a malformed or
absent `_meta` as *absent*, not as an error: the cost of a broken schema must be the form
and nothing else. The user still has a working command line.

**We do not enforce most of what a schema says.** See the table below — that is the
server's job, and yours is a convenience on top of it.

### Validation is still ours

`coerce_arguments` in [`commands.py`](../src/python_acp/commands.md) remains the authority.
A client-side form is a convenience and **must not become a trust boundary**: any client
can send any line, and a form that let this agent skip its own checks would be a regression
in exactly the direction that matters.

Said the other way, for your benefit: you are not the last line of defence, so do not build
one. Render what you can, send the line, and let the refusal come back if it must.

---

## For MCP server authors

Your `inputSchema` used to be read by this bridge and summarised into one line. It is now
forwarded whole to whatever is on the other end, which means **schema quality is suddenly
visible to a human**. A `title`, a `description`, an `enum`, a `default` and a `minimum`
are the difference between a labelled form with a dropdown and a text box.

### What a client can build from

The lists below are [acp-ui](https://claude.ai/code/artifact/c8a3af41-643c-4ccf-9f53-b643a35f2df7)'s,
not ours, and that document owns them — its rendering rules can change without anything
here changing. They are quoted because they are the first real answer to "what is worth
writing", and because a second client will likely land in a similar place.

acp-ui uses `properties`, `required`, and per-property `type`, `title`, `description`,
`default`, `enum` (with `enumNames` or `oneOf[].title` for labels), `format`,
`minimum`/`maximum`/`multipleOf`, `minLength`/`maxLength`, `pattern`, and `items` for
arrays. It also supports `dependentRequired`.

It does **not** render `if`/`then`/`else`, `dependentSchemas`, `allOf`, or discriminated
`oneOf`. Those are detected and the form steps aside to a raw line with a reason, rather
than rendering a subset of a conditional schema as though it were the whole thing. A form
that is confidently wrong is worse than the text box it should have fallen back to.

If your tools use those constructs, nothing breaks — the user gets today's text box.

### Two shapes that mean different things

| What you publish | What it says | What the user sees |
| --- | --- | --- |
| `properties` with entries | these are the parameters | a form, or a hint naming them |
| `properties: {}` | this tool takes no parameters | "takes no parameters" |
| no `inputSchema` at all | nothing | a free text box, and no claim about parameters |

The last two are deliberately not merged. Omitting `inputSchema` and reporting that as
"takes no parameters" would assert a fact nobody published. (MCP declares `inputSchema`
required, so the third row is off-spec — but servers in the wild omit it, so this bridge
handles it rather than pretending it cannot happen.)

### Composition hides your parameters from the hint

`tool_command_hint` walks top-level `properties`. A schema whose properties live inside
`allOf` or `oneOf` has none at the top level, so the hint reads `(no parameters)` even
though the tool takes two. The schema in `_meta` still carries them, which is the argument
for this whole channel in one line — but a client that declines composition, as acp-ui
does, will show your user a bare text box with no parameter names anywhere. Prefer
top-level `properties` when you can express the tool that way.

### What this agent checks, and what it does not

`coerce_arguments` turns command-line strings into JSON types. It is deliberately narrow:

| Enforced here | Not enforced here — your server's job |
| --- | --- |
| unknown parameter names are refused, **when the schema declares any** | `enum` membership |
| `required` names must be present | `minimum` / `maximum` / `multipleOf` |
| `type` for `string`, `number`, `integer`, `boolean`, `object`, `array` | `minLength` / `maxLength` / `pattern` |
| a union `type` reads as JSON, like an undeclared one | which branch of that union you meant |
| `items.type` for array elements | `format` |
| a bare `--flag` becomes `true` only for `boolean` or an undeclared type | `default` (never filled in for you) |
| | `dependentRequired`, `if`/`then`/`else`, `dependentSchemas`, `allOf`, `oneOf` |

That first row's caveat is why `zoo-all-of` accepts `--a x --b 2`: its properties live
inside `allOf`, so the top level declares none, and with nothing declared there is nothing
to check a name against. The arguments reach `tools/call` and your server decides.

An undeclared property is read as JSON and kept as a string when that fails, so `3` is a
number and `hello` is a string. That is the guess a person typing a command line expects,
and it is only a guess — declaring a `type` is what makes it a fact.

So: everything in the right-hand column reaches `tools/call` unchecked by us. **Keep
validating on arrival.** A client form and this bridge together are two conveniences, not
a guarantee.

### Trying it against something real

`MOCK_MCP_SCHEMA_ZOO=1` on
[`tests/fixtures/mock_mcp_server.py`](../tests/fixtures/mock_mcp_server.py) adds thirteen
tools, one per construct rather than one kitchen sink — every JSON type bare, string
constraints and formats, numeric bounds, the three ways to spell a choice, arrays of
scalars/enums/objects, nesting two deep, the four constructs a client is expected to
decline, and both edges above (`properties: {}` against no `inputSchema`). Every zoo tool
echoes its arguments back as JSON, so the *types* that came out the far end are visible
rather than assumed: `--count 3` arriving as `3` rather than `"3"` is a thing you can see.

[`scripts/start-zoo.sh`](../scripts/start-zoo.sh) is the whole setup: it activates the
venv, exports `MOCK_MCP_SCHEMA_ZOO=1`, and starts the agent on stdio. Because a session's
MCP servers are spawned as children of that process, they inherit the variable — so the
`session/new` entry needs no `env` of its own, and the banner prints the one to paste.

```bash
scripts/start-zoo.sh
```

Two things that are not obvious the first time:

- Every tool call asks permission. Send `session/set_mode` with `modeId: "auto-approve"`
  once, or answer each `session/request_permission`.
- Redirecting a canned file into the agent (`… < file.jsonl`) fails with
  `ValueError: Pipe transport is for pipes/sockets only`, and closing stdin at EOF kills
  the process before `session/new` — which spawns a subprocess — can answer. Pipe it, and
  hold the pipe open.

---

## The cost, measured

`available_commands_update` is re-announced every turn, so this was weighed before shipping
(`pyacp-ma2`, serialised notification, compact JSON):

| Tools announced | Without `_meta` | With | Per tool |
| --- | ---: | ---: | ---: |
| The repo fixture's three | 1,428 B | 1,797 B | ~+120 B |
| Three realistic tools — descriptions, enums, defaults, bounds | 1,702 B | 3,595 B | ~+630 B |

A schema-carrying entry is roughly **4.5× its bare self**. Shipped ungated anyway: ~630 B
per tool puts a 20-tool session near 13 KB per turn, which no transport here notices. Two
escapes exist if that ever stops being true — send `_meta` only in the once-per-session
announcement, or gate it on a client capability in `clientCapabilities._meta` — but the
first makes the per-turn list disagree with the session's, and neither is worth buying
before someone is actually paying.
