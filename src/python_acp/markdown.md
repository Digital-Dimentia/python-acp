# `markdown.py` — making agent text survive the client's renderer

`session/update`'s `agent_message_chunk` carries a bare string and names no content type.
This project read that absence as licence to send preformatted plain text — column-aligned
listings, two-space indents, `<angle>` placeholders. That was wrong about the audience.

**Every real ACP client renders the field as Markdown.** acp-ui parses it with `marked`
and injects the result with `v-html` (`ChatView.vue`); Zed renders agent messages the same
way. `pyacp-nlv` was filed after a live acp-ui session showed what that does to us.

## Two failures, one cause

**Placeholders are deleted.** `<name>`, `<string>`, `<integer>`, `<value>`, `<server>`,
`<tool>` are parsed as HTML tags. A Markdown parser passes raw HTML through untouched and
the browser drops an unknown element, so the placeholder vanishes with no error anywhere.
The refusal message reached a user as:

```
{"tool": "", "arguments": {...}, "server": ""}
```

for text that left this process with `"<name>"` in both slots — advice about a shape that
is not the shape.

**Columns collapse.** [`commands.py`](commands.md)'s `_INDENT` is two spaces, so an item
name at one level is followed by its parameters at four with no blank line between them.
CommonMark forbids an indented code block from *interrupting a paragraph*, so those lines
are a lazy continuation instead: every newline becomes a space. A one-tool listing that
reads as

```
  mock/echo
    --text  <string>    required  What to echo
    --times <integer>
```

arrives as `mock/echo --text required What to echo --times`.

## The fix, and why it degrades better than what it replaced

Preformatted content goes in a **fenced code block**; placeholder-shaped fragments in
prose go in a **code span**. A parser escapes `<...>` inside either, and preserves every
column inside a fence.

The docstrings this replaced defended plain text on the grounds that a client which does
*not* render Markdown would show the punctuation. That is still the tradeoff — but it is
not symmetric, and it had been decided the wrong way round:

| | Markdown client | Plain-text client |
|---|---|---|
| plain text (before) | placeholders **deleted**, columns **destroyed** | correct |
| fenced (after) | correct | three visible backticks per block |

Losing the `<string>` from a parameter list is data loss the reader cannot detect, let
alone recover. A stray ``` is cosmetic and self-evidently punctuation. The fence wins even
granting the old premise — and the premise is itself doubtful, since no ACP client is
known to render this field as plain text.

## Main symbols

| Symbol | Purpose |
|---|---|
| `code_span(text)` | Inline code span — for a placeholder or example inside a sentence |
| `fenced_block(body)` | Fenced code block from a string |
| `fenced_lines(lines)` | Fenced code block from a `list[str]`, splicing into a renderer |

## Both helpers size their own delimiters

Neither may assume its content is delimiter-free. A tool description is written by whoever
wrote the MCP server, and `render_resource_contents` embeds a resource's **entire body** —
arbitrary text that is frequently Markdown and may contain fenced blocks of its own. So
each helper measures the longest backtick run in the content and picks a delimiter one
longer, which is what CommonMark specifies for exactly this. A resource holding a ```` ```
```` fence therefore cannot close ours early.

`code_span` additionally pads with a space at each end when the content begins or ends
with a backtick, or is all spaces. A parser strips exactly that pair back off, so the
padding never shows.

`fenced_lines([])` returns **nothing**, not an empty fence: a visible pair of delimiters
around no content reads as a rendering fault rather than as the absence it represents.

## What is deliberately *not* wrapped

`AvailableCommand.input.hint` and `AvailableCommand.description` keep their bare
`<server>/<tool>` text. Those are not rendered as Markdown — acp-ui interpolates them with
`{{ }}` (`CommandPalette.vue`), which escapes HTML — so backticks there would appear
literally. The rule is per **field**, not per string: only what lands in an
`agent_message_chunk` goes through these helpers.

The blocks emitted by `prompt_message_blocks` are also left alone. Those are an MCP
server's own content forwarded verbatim, and rewriting someone else's message is a
different decision from formatting our own.

## Not a sanitizer

These helpers make our text render *correctly*. They are not a security boundary and must
not be relied on as one. Separately and upstream: acp-ui's `ChatView.vue` runs
`v-html="marked.parse(content)"` with no sanitizer, so it executes whatever HTML an agent
sends it. That is acp-ui's hole to close, not ours to work around.

## Tests

`tests/test_markdown.py` covers the delimiter sizing directly. `tests/test_commands.py`
asserts the *property* on real rendered output rather than on literal strings — that every
`<...>` sits inside a span or a fence, and that no indented line sits outside one — because
an assertion on the exact text is what let this ship in the first place.

## Related

- [commands.py docs](commands.md) — every listing renderer, and `_INDENT`
- [turn_mcp_router.py docs](turn_mcp_router.md) — `CONVENTION`, the refusal footer
- [turns.py docs](turns.md) — `agent_message_chunk` and the rest of the update surface
