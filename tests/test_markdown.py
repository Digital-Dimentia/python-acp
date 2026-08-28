"""`markdown.py`, and the property every `agent_message_chunk` renderer must hold.

The delimiter-sizing tests are unit tests of the two helpers. `assert_markdown_safe` is
the interesting half: it is the *property* — no `<placeholder>` outside a code context, no
indented line outside a fence — and `tests/test_commands.py` applies it to real rendered
listings.

It is written here rather than there because it is about Markdown rather than about
commands, and because `turn_mcp_router`'s refusal text needs it too.

**Why a property and not a string.** The bug this was filed for (`pyacp-nlv`) shipped
under a full suite of assertions on exact rendered text. Every one of them passed, because
every one of them checked the string that leaves this process — which was correct — rather
than what a client does with it. An assertion on literal output cannot catch a rendering
fault by construction. Only a rule about the output's *shape* can.
"""

from __future__ import annotations

import re

import pytest

from python_acp.markdown import code_span, fenced_block, fenced_lines

# A run of backticks opening or closing a fence, at the start of a line.
_FENCE_LINE = re.compile(r"^(`{3,})\s*$")

# `<word>` or `<word/word>` — the placeholder shape a Markdown parser eats as an HTML tag.
# Deliberately loose: anything a browser could read as a tag name is a hazard, and a false
# positive here costs one code span while a false negative costs the reader the meaning.
_TAGLIKE = re.compile(r"<[A-Za-z][A-Za-z0-9_./-]*>")


def _spans_stripped(line: str) -> str:
    """`line` with every inline code span removed, so what is left is rendered as prose."""
    return re.sub(r"(`+)(?:(?!\1).)*\1", "", line)


def assert_markdown_safe(text: str, *, allow_indent: bool = False) -> None:
    """Fail unless `text` survives a CommonMark renderer with its meaning intact.

    Two rules, and both are about what a parser does rather than what the string says:

    1. **No tag-shaped placeholder outside a code span or fence.** `<string>` is an HTML
       tag; a parser emits it raw and the browser discards it.
    2. **No indented line outside a fence.** Two- and four-space indents are what carry
       column alignment here, and outside a fence they are a lazy paragraph continuation
       that reflows the whole block into one line.

    `allow_indent` relaxes the second rule for text that is prose by design and merely
    happens to wrap.
    """
    in_fence = False
    fence: str | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        match = _FENCE_LINE.match(line)
        if match:
            if not in_fence:
                in_fence, fence = True, match.group(1)
            elif fence is not None and len(match.group(1)) >= len(fence):
                in_fence, fence = False, None
            continue
        if in_fence:
            continue
        naked = _spans_stripped(line)
        found = _TAGLIKE.search(naked)
        assert found is None, (
            f"line {number} carries {found.group() if found else ''!r} outside a code "
            f"span or fence, so a Markdown client will delete it: {line!r}"
        )
        if not allow_indent:
            assert not line.startswith(" "), (
                f"line {number} is indented outside a fence, so a Markdown client will "
                f"reflow it into the paragraph above: {line!r}"
            )
    assert not in_fence, "text ends inside an unclosed code fence"


class TestAssertMarkdownSafe:
    """The checker has to fail on the real bug, or it is decoration."""

    def test_it_rejects_a_bare_placeholder(self) -> None:
        with pytest.raises(AssertionError, match="delete it"):
            assert_markdown_safe('{"tool": "<name>"}')

    def test_it_rejects_an_indented_line(self) -> None:
        with pytest.raises(AssertionError, match="reflow it"):
            assert_markdown_safe("Call one with:\n  /invokeTool demo/echo")

    def test_it_accepts_a_placeholder_inside_a_span(self) -> None:
        assert_markdown_safe(f"Use {code_span('<server>/<tool>')} to call one.")

    def test_it_accepts_a_placeholder_inside_a_fence(self) -> None:
        assert_markdown_safe(fenced_block("  --text  <string>  required"))

    def test_it_rejects_an_unclosed_fence(self) -> None:
        with pytest.raises(AssertionError, match="unclosed"):
            assert_markdown_safe("```\n--text <string>")


class TestCodeSpan:
    def test_it_wraps_ordinary_text_in_one_backtick(self) -> None:
        assert code_span("<name>") == "`<name>`"

    def test_it_outgrows_backticks_in_the_content(self) -> None:
        """A tool description quoting `code` must not terminate the span early."""
        assert code_span("a `b` c") == "``a `b` c``"
        assert code_span("a ``b`` c") == "```a ``b`` c```"

    def test_it_pads_content_that_starts_or_ends_with_a_backtick(self) -> None:
        """CommonMark strips one leading and trailing space, so the padding is invisible."""
        assert code_span("`x") == "`` `x ``"
        assert code_span("x`") == "`` x` ``"

    def test_it_pads_all_whitespace_content(self) -> None:
        assert code_span("  ") == "`    `"

    def test_it_leaves_empty_content_alone(self) -> None:
        """Empty is not all-spaces: there is nothing for a delimiter to merge with."""
        assert code_span("") == "``"


class TestFencedBlock:
    def test_it_uses_three_backticks_by_default(self) -> None:
        assert fenced_block("body") == "```\nbody\n```"

    def test_it_outgrows_a_fence_inside_the_body(self) -> None:
        """A resource whose text is itself Markdown must not close our fence early."""
        assert fenced_block("a\n```\nb\n```\nc").startswith("````\n")

    def test_it_attaches_no_info_string(self) -> None:
        """A tool listing is not source code, and a highlighter would colour it as if."""
        assert fenced_block("body").splitlines()[0] == "```"

    def test_lines_form_splices_into_a_renderer(self) -> None:
        assert fenced_lines(["a", "b"]) == ["```", "a", "b", "```"]

    def test_empty_lines_produce_nothing(self) -> None:
        """An empty fence renders as delimiters around nothing, which reads as a fault."""
        assert fenced_lines([]) == []
