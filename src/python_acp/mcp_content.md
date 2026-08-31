# `mcp_content.py` — the seam between two content models

MCP and ACP both have a notion of "a piece of content in a message", and the notions
overlap without matching. This module is the mapping, written out rather than inferred at
each call site — because the failure mode of getting it wrong is **silent**: a client
renders what it is given, and a block dropped or mislabelled looks like a tool that
produced nothing rather than like a bug here.

## Direction matters, and this is the outbound one

[turn_mcp_router.py](turn_mcp_router.md) **declines** `image`, `audio`, and `resource`
blocks arriving *in a prompt*, because reading them would need a model. Sending them
*out* is a different question with a different answer: they are a tool's output, the
client asked for the tool, and `promptCapabilities` governs only what the agent **reads**.

Nothing here needs a capability flip, and the apparent contradiction is only apparent.

## The mapping

| MCP content | ACP block | Notes |
|---|---|---|
| `text` | `TextContentBlock` | |
| `image` | `ImageContentBlock` | `data` + `mimeType`, required on both sides; optional `uri` survives |
| `audio` | `AudioContentBlock` | MCP added audio in `2025-03-26`, after this project's pinned revision. Mapped anyway — a server that sends it costs nothing to honour |
| `resource` carrying `text` | `EmbeddedResourceContentBlock` → `TextResourceContents` | |
| `resource` carrying `blob` | `EmbeddedResourceContentBlock` → `BlobResourceContents` | |
| `resource_link` | `ResourceContentBlock` | |
| anything else | a marked placeholder | see below |

Two details that are easy to get backwards:

- **MCP's embedded resource has no `type` tag inside it.** `text` and `blob` *are* the
  discriminator, so the mapping branches on which field is present.
- **ACP calls the link type `resource_link` and the embedded one `resource`.** MCP uses
  the same two words for the same two things — a coincidence worth not relying on, since
  nothing keeps them aligned if either spec moves.

`annotations` (`audience`, `priority`, `lastModified`) carry across where present. Absent
is the one case where silence is right: nothing was said, so nothing is lost. Malformed
annotations are dropped without costing the block — they are decoration, and losing
content over them would be the wrong trade.

## Unmappable content becomes a visible placeholder

A block this module cannot map is replaced by a text block saying so, not skipped.

Skipping is what `pyacp-hnk.2` did, and it was right **while the mapping was known to be
narrow**. Once the mapping claims to be complete, a silent skip is worse: a client
rendering `content` sees a tool call that produced nothing, which reads as *"the tool did
nothing"* — a wrong answer — rather than *"there was something here we could not show"*.

The placeholder names what was there and points at the escape hatch:

```
[python-acp could not render MCP content of type 'chart'; see rawOutput]
```

Nothing is lost either way: `ToolCallProgress.rawOutput` already carries the server's
result verbatim, so a client that wants the original always has it. The placeholder is for
the humans, and is bracketed and prefixed so it cannot be mistaken for a tool's own words.

The same treatment covers a block of a *known* type missing what that type needs — a
half-built `ImageContentBlock` is worse than a sentence saying the image was malformed.

## One thing here is not MCP content at all

`to_edit_content` converts an [edits.py](edits.md) `EditResult` — ours, not a server's —
into tool-call content. It lands in this module because this is where "one of our value
types becomes ACP content" already lives, and because `edits.py` **must not import
`acp.schema`**: keeping that import out is what lets it be a plain library a unit test can
drive with no connection, and what keeps the seam in `tests/test_executor_neutrality.py`
honest. A second module holding one function would be a worse home than the one that
already owns the direction.

It returns two blocks:

| Block | Contents |
|---|---|
| `FileEditToolCallContent` (`type: "diff"`) | `oldText` = `result.original`, `newText` = `result.updated` |
| A text block | `result.unified()`, **fenced** with a `diff` info string |

Both halves are worth stating because both are easy to get wrong:

- **`Diff.oldText`/`newText` are whole-file contents, not a patch.** The schema says "The
  new content after modification". Handing `unified()` to `newText` typechecks and would
  make a client replace the file with the diff.
- **The unified diff is fenced.** A client renders tool-call text as Markdown, where a
  leading `-` is a list bullet — unfenced, the column carrying the entire meaning of the
  diff is eaten. [markdown.py](markdown.md)'s `fenced_lines` sizes the fence.

## Main symbols

| Symbol | Purpose |
|---|---|
| `to_content_block(block)` | One MCP content block as an ACP one. **Never `None`** — unmappable becomes a placeholder |
| `to_tool_call_content(result)` | A whole `tools/call` result's content, wrapped as ACP tool-call content |
| `to_edit_content(result)` | A verified `edits.EditResult` as a whole-file diff block plus a fenced human-readable one |
| `MAPPED_TYPES` | The five MCP `type` values with a real mapping |

`to_tool_call_content` returns `None` rather than `[]` for a result with no content: the
schema makes the field optional, and an empty list would claim the tool answered with
nothing when it answered with no content field at all.

## Tests

`tests/test_mcp_content.py` for the mapping, and
`tests/test_turn_mcp_router.py::test_every_mcp_content_type_reaches_the_client_as_an_acp_block`
end to end against the fixture server's `every-content` tool. That second one matters: a
mapping that works on dicts we wrote and not on what a server actually sends would pass
the whole unit-test file.

## Related

- [turn_mcp_router.py docs](turn_mcp_router.md) — the caller, and the inbound direction
- [mcp_stdio.py docs](mcp_stdio.md) — where a `tools/call` result comes from
- [edits.py docs](edits.md) — where an `EditResult` comes from, and why it cannot build
  its own ACP content
