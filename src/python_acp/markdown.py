"""Make agent text survive the Markdown renderer every real ACP client puts it through.

`session/update`'s `agent_message_chunk` carries a bare string with no content type, and
for a long time this project read that absence as licence to send preformatted plain
text: column-aligned listings, two-space indents, `<angle>` placeholders. That was wrong
about the audience. Clients render the field as **Markdown** — acp-ui through `marked`
(`ChatView.vue`), Zed likewise — and plain text put through a Markdown parser is not
merely styled differently, it is *destroyed* two ways at once:

1. **`<name>`, `<string>`, `<value>` are HTML tags.** A Markdown parser passes raw HTML
   through and the browser drops an unknown element, so the placeholder that carried the
   whole meaning of the line disappears silently. A user reading acp-ui saw
   `{"tool": "", "arguments": {...}, "server": ""}` for text that left here with
   `"<name>"` in both slots.
2. **Column alignment collapses.** `_INDENT` is two spaces, so an item name at one level
   is followed by its parameters at four with no blank line between. CommonMark forbids
   an indented code block from interrupting a paragraph, so those lines are a *lazy
   continuation* instead — every newline becomes a space and the whole listing reflows
   into one run-on line.

Both are fixed by the same move: put anything preformatted in a **fenced code block**
and anything placeholder-shaped in a **code span**. Inside either, a parser escapes
`<...>` rather than executing it, and inside a fence it preserves every column.

## Why this degrades better than plain text did

The old docstrings defended plain text on the grounds that a client which does *not*
render Markdown would show the punctuation. True, and it is still the tradeoff — but it
is not symmetric, and it was decided in the wrong direction:

| | Markdown client | Plain-text client |
|---|---|---|
| plain text (before) | placeholders **deleted**, columns **destroyed** | correct |
| fenced (after) | correct | three visible backticks per block |

Losing the `<string>` in a parameter list is data loss the reader cannot detect or
recover. A stray ``` is cosmetic and self-evidently punctuation. So the fence wins even
under the old premise, and the old premise is itself doubtful: no ACP client is known to
render this field as plain text.

## Both helpers size their own delimiters

Neither may assume its content is delimiter-free. A tool description is written by
whoever wrote the MCP server, and `render_resource_contents` embeds a resource's entire
body — arbitrary text that may itself be Markdown containing fences. So each helper
measures the longest backtick run in the content and picks a delimiter longer than it,
which is exactly what CommonMark specifies for the purpose.
"""

from __future__ import annotations

import re

#: Any run of backticks, however long. Used to size a delimiter past the longest one.
_BACKTICK_RUN = re.compile(r"`+")

#: CommonMark's minimum opening fence. A shorter run is not a fence at all.
_MIN_FENCE = 3


def _longest_backtick_run(text: str) -> int:
    """The length of the longest unbroken run of backticks in `text`, or 0 for none."""
    return max((len(match.group()) for match in _BACKTICK_RUN.finditer(text)), default=0)


def code_span(text: str) -> str:
    """Wrap `text` as an inline code span, escaping any Markdown or HTML inside it.

    For the placeholder-shaped fragments that appear in prose — `<server>/<tool>`, a JSON
    example, a `--flag <value>` pair. The delimiter is one backtick longer than the
    longest run inside, per CommonMark's rule for the case.

    A span whose content begins or ends with a backtick, or which is all spaces, is padded
    with one space at each end; a parser strips exactly that pair back off, so the padding
    is invisible in the rendered output and is what keeps the delimiters from merging with
    the content.
    """
    fence = "`" * (_longest_backtick_run(text) + 1)
    needs_padding = text.startswith("`") or text.endswith("`") or (text and not text.strip())
    body = f" {text} " if needs_padding else text
    return f"{fence}{body}{fence}"


def fenced_block(body: str) -> str:
    """Wrap `body` as a fenced code block, preserving every column and character.

    For the preformatted half of a listing — the aligned parameter tables that reflow into
    gibberish as prose. No info string is attached: this is not source code in any
    language, and tagging it would invite a highlighter to colour a tool listing as if it
    were.

    The fence is sized past the longest backtick run anywhere in `body`, so a resource
    whose text is itself Markdown containing a fenced block cannot terminate ours early.
    """
    fence = "`" * max(_MIN_FENCE, _longest_backtick_run(body) + 1)
    return f"{fence}\n{body}\n{fence}"


def fenced_lines(lines: list[str]) -> list[str]:
    """`fenced_block` for callers that are assembling a listing line by line.

    Returns the fence lines around `lines` rather than a joined string, so the result
    splices into the `list[str]` the renderers build. Empty input yields nothing at all —
    an empty fence is a visible pair of delimiters around no content, which reads as a
    rendering fault rather than as the absence it represents.
    """
    if not lines:
        return []
    fence = "`" * max(_MIN_FENCE, _longest_backtick_run("\n".join(lines)) + 1)
    return [fence, *lines, fence]
