"""Markdown, addressed by heading path.

Named `edit_docs` and not `edit_markdown` because [markdown.py](markdown.md) already
exists and means something else entirely — it makes *outbound agent text* survive the
Markdown renderer every ACP client puts it through. An editor sitting next to it under a
near-identical name would be misread by every future reader, and one of the two would
eventually be changed for the other's reasons.

## Why the confidence is `STRUCTURAL` and not `SEMANTIC`

Every other dialect verifies against a parser that shares nothing with its locator. There
is no such parser here — not because none was available, but because **Markdown has no
semantic model to diff.** It *is* text. `markdown-it-py` would give an AST, but comparing
two ASTs proves the two files render alike, which is a weaker claim than it appears and
would still be produced by the same reading of CommonMark that the locator uses.

So step 6 compares the **heading tree and its section bodies**, which is a real check with
a real limit worth stating plainly:

- it catches a splice that landed in the wrong section, deleted a heading, or absorbed the
  following section into a body — the failures that actually happen;
- it cannot catch a splice that produced different-but-valid prose inside the right
  section, because at that point there is nothing left to compare against.

`Confidence.STRUCTURAL` is carried in the result so a caller sees that rather than having
to find this paragraph.

## The scanner, and the bug every naive one has

A section's body runs from the end of its heading line to the start of the next heading of
level less than or equal to its own. That is the entire locator, and it is correct only if
"heading" is decided correctly — which means tracking:

- **ATX** (`## Title`), with the optional closing sequence (`## Title ##`) stripped, and
  the CommonMark rule that up to three leading spaces are allowed but four make it an
  indented code block instead;
- **setext** (`Title` over `=====` or `-----`), which is a heading spread across two lines
  and therefore has a start offset on the *previous* line — miss that and a `SET` on the
  section eats its own title;
- **fenced code blocks**, matched by the *opening run length* per CommonMark, so a fence
  opened with four backticks is not closed by three.

**A `#` inside a fenced block is not a heading.** That is the one bug every naive
implementation has, and it is not academic here: this repository's own documentation is
full of fenced Markdown examples containing headings, and `ARCHITECTURE.md` is a fixture.

## Addressing

Heading paths, RFC 6901-escaped, sharing `edits.pointer_segments` with the JSON dialect:
`/# Install/## macOS`. A heading containing a slash is reached with `~1`.

**The `#` markers are part of the key.** Without them `/# API/## Errors` and
`/# API/### Errors` are the same address in a document that has both, and picking either
would be a guess. With them the address says which one it means.

The **empty** pointer addresses the preamble — everything before the first heading. It is
a real place that real edits target (a badge block, a lede paragraph) and it has no
heading to name, so the one address with no segments is the natural fit.

Duplicate sibling headings are `AddressAmbiguous`, listing every match's line number. The
module never picks the first.

## Why an inserted section gets no blank line before it

It would read better, and it would be wrong. The body of a section runs up to the start of
the next heading, so a blank line inserted *above* a new heading lands inside the previous
section's body and changes it — an edit the caller never asked for, in a place they were
not looking. Step 6 catches it, which is how this was found; the fix is not to do it.

In practice the seam is invisible anyway, because a section body that ends at a heading
almost always ends with a blank line already.

## The one byte this module adds on its own

A `SET` whose replacement does not end in a newline gets one, when the body it replaces
was not at end of file. Without it the next heading is glued onto the last line of the
body, stops being a heading, and the edit is refused by step 6 — technically correct and
useless. This is the only place `edit_docs` writes a byte the caller did not, and it is
recorded here because a reader is entitled to know where the module stopped being literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from python_acp.edits import (
    AddressAmbiguous,
    AddressNotFound,
    Confidence,
    EditError,
    Location,
    Op,
    OpKind,
    Segments,
    Span,
    UnsupportedConstruct,
    pointer_segments,
)

#: Up to three leading spaces; four would make it an indented code block.
_ATX = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<text>.*?))?[ \t]*$")

#: A closing sequence — `## Title ##` — is decoration, not part of the title.
_CLOSING = re.compile(r"[ \t]+#+[ \t]*$")

#: Setext underlines. Ambiguous with a thematic break, which is why the preceding line
#: has to be a paragraph; see `_scan`.
_SETEXT = re.compile(r"^ {0,3}(?P<rule>=+|-+)[ \t]*$")

#: CommonMark fences: three or more backticks or tildes, and the run length matters.
_FENCE = re.compile(r"^ {0,3}(?P<char>`|~)(?P=char){2,}(?P<info>.*)$")

#: The body slot inside a section's subtree. Not a legal heading key, so it cannot collide.
BODY = ""


@dataclass
class _Section:
    key: str
    level: int
    start: int
    body_start: int
    line: int
    end: int = -1
    children: list[_Section] = field(default_factory=list)

    @property
    def content_end(self) -> int:
        """Where this section's own body stops — at its first child, not at its end."""
        return self.children[0].start if self.children else self.end


class DocsDialect:
    """The `edits.Dialect` implementation for Markdown."""

    name = "markdown"
    confidence = Confidence.STRUCTURAL

    def parse(self, source: str) -> Any:
        """The heading tree, with each section's body under the `BODY` key.

        Shaped as nested dicts on purpose: `edits._apply_to_structure` is written against
        `Mapping` and `MutableSequence` so there is one structural applier and not one per
        dialect, and a dialect that returned something exotic could not be checked by it.
        """
        preamble_end, sections = _scan(source)
        return {BODY: source[:preamble_end], **{s.key: _subtree(source, s) for s in sections}}

    def parse_fragment(self, text: str) -> Any:
        """A body is text. There is nothing to parse and nothing to normalise."""
        return text

    def render_scalar(self, value: Any) -> str:
        if not isinstance(value, str):
            raise EditError(
                f"markdown bodies are text; scalar={value!r} is a {type(value).__name__}"
            )
        return value

    def round_trip_ok(self, source: str) -> bool | None:
        """`None`: there is no Markdown round-tripper here, so step 3 does not apply."""
        return None

    def plan(self, source: str, parsed: Any, op: Op) -> Location:
        if op.kind is OpKind.APPEND:
            raise UnsupportedConstruct(
                "markdown has no sequences to append to; use insert to add a section, or "
                "set to replace a section's body"
            )
        preamble_end, sections = _scan(source)
        segments = pointer_segments(op.address)
        if op.kind is OpKind.INSERT:
            return _plan_insert(source, sections, preamble_end, segments, op, self)
        if not segments:
            if op.kind is OpKind.DELETE:
                raise EditError("delete needs a heading to delete; the preamble is not one")
            return _body_location(source, Span(0, preamble_end), (BODY,), op, self)
        section, keys = _resolve(sections, segments, op.address)
        if op.kind is OpKind.SET:
            span = Span(section.body_start, section.content_end)
            return _body_location(source, span, (*keys, BODY), op, self)
        return Location(
            span=Span(section.start, section.end),
            replacement="",
            segments=keys,
            # No `value_span`: a section lifted out of its document does not parse
            # standalone, because its heading levels are relative to the parent it left.
            # Step 2 is skipped here and step 6 carries the whole check.
            value_span=None,
        )


DOCS_DIALECT = DocsDialect()


def _body_location(
    source: str, span: Span, segments: Segments, op: Op, dialect: DocsDialect
) -> Location:
    text = _terminated(op.source_text(dialect), span, source)
    return Location(
        span=span,
        replacement=text,
        segments=segments,
        value_span=span,
        parsed_value=text,
    )


def _terminated(text: str, span: Span, source: str) -> str:
    """The one byte this module adds on its own; see the module docstring.

    Only when the body is followed by something. A replacement at end of file keeps
    whatever the caller wrote, because there is no heading below it to protect.
    """
    if not text or text.endswith("\n") or span.end >= len(source):
        return text
    return text + "\n"


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def _resolve(
    sections: list[_Section], segments: tuple[str, ...], address: str
) -> tuple[_Section, Segments]:
    current = sections
    section: _Section | None = None
    for depth, key in enumerate(segments):
        matches = [s for s in current if s.key == key]
        if not matches:
            raise AddressNotFound(
                f"no such heading: {key!r} under "
                f"{'/' + '/'.join(segments[:depth]) if depth else 'the document'}; "
                f"it holds {[s.key for s in current] or 'no headings'}"
            )
        if len(matches) > 1:
            raise AddressAmbiguous(
                f"{key!r} appears {len(matches)} times at that level, on lines "
                f"{[s.line for s in matches]}; this module will not pick one for you"
            )
        section = matches[0]
        current = section.children
    assert section is not None
    return section, tuple(segments)


def _plan_insert(
    source: str,
    sections: list[_Section],
    preamble_end: int,
    segments: tuple[str, ...],
    op: Op,
    dialect: DocsDialect,
) -> Location:
    if not segments:
        raise EditError("insert needs an address naming the new heading")
    key = segments[-1]
    level = _level_of(key)
    if len(segments) == 1:
        siblings, anchor, parent_level = sections, _end_of(sections, preamble_end, source), 0
    else:
        parent, _ = _resolve(sections, segments[:-1], op.address)
        siblings, anchor, parent_level = parent.children, parent.end, parent.level
    if level <= parent_level:
        raise EditError(
            f"insert {op.address!r}: {key!r} is level {level}, which is not deeper than "
            f"its parent at level {parent_level}, so it would not be a child of it"
        )
    if any(s.key == key for s in siblings):
        raise EditError(f"insert {op.address!r}: that heading already exists; use set")
    if anchor > 0 and not source[:anchor].endswith("\n"):
        raise UnsupportedConstruct(
            f"insert {op.address!r}: the text before the insertion point does not end in a "
            "newline, so a heading cannot start there without first altering the section "
            "above it. Add a trailing newline to the file and retry."
        )
    body = _terminated(op.source_text(dialect), Span(anchor, anchor), source + "\n")
    return Location(
        span=Span(anchor, anchor),
        replacement=f"{key}\n{body}",
        segments=(*segments,),
        parsed_value={BODY: body},
    )


def _level_of(key: str) -> int:
    match = _ATX.match(key)
    if match is None:
        raise EditError(
            f"{key!r} is not a heading; an address segment must carry its own markers, "
            'such as "## Errors", so that levels stay distinguishable'
        )
    return len(match.group("hashes"))


def _end_of(sections: list[_Section], preamble_end: int, source: str) -> int:
    return sections[-1].end if sections else preamble_end


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def _subtree(source: str, section: _Section) -> dict[str, Any]:
    return {
        BODY: source[section.body_start : section.content_end],
        **{child.key: _subtree(source, child) for child in section.children},
    }


def _scan(source: str) -> tuple[int, list[_Section]]:
    """Every heading in the document, nested, plus where the preamble ends."""
    flat = _headings(source)
    for index, section in enumerate(flat):
        section.end = next(
            (later.start for later in flat[index + 1 :] if later.level <= section.level),
            len(source),
        )
    roots: list[_Section] = []
    stack: list[_Section] = []
    for section in flat:
        while stack and stack[-1].level >= section.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(section)
        stack.append(section)
    return (flat[0].start if flat else len(source)), roots


def _headings(source: str) -> list[_Section]:
    """Scan lines, honouring fences, and return ATX and setext headings in order."""
    found: list[_Section] = []
    fence: tuple[str, int] | None = None
    offset = 0
    previous: tuple[int, str, int] | None = None  # start, text, line number
    for number, line in enumerate(source.splitlines(keepends=True), start=1):
        stripped = line.rstrip("\n")
        fence_match = _FENCE.match(stripped)
        if fence is not None:
            if fence_match is not None and _closes(fence, fence_match):
                fence = None
            previous = None
            offset += len(line)
            continue
        if fence_match is not None:
            char = fence_match.group("char")
            fence = (char, len(stripped.strip()) - len(fence_match.group("info")))
            previous = None
            offset += len(line)
            continue
        atx = _ATX.match(stripped)
        setext = _SETEXT.match(stripped)
        if atx is not None:
            text = _CLOSING.sub("", atx.group("text") or "").strip()
            found.append(
                _Section(
                    key=f"{atx.group('hashes')} {text}".rstrip(),
                    level=len(atx.group("hashes")),
                    start=offset,
                    body_start=offset + len(line),
                    line=number,
                )
            )
            previous = None
        elif setext is not None and previous is not None:
            # A setext heading starts on the line *above* its underline. Miss that and a
            # `set` on the section eats its own title.
            level = 1 if setext.group("rule").startswith("=") else 2
            found.append(
                _Section(
                    key=f"{'#' * level} {previous[1]}",
                    level=level,
                    start=previous[0],
                    body_start=offset + len(line),
                    line=previous[2],
                )
            )
            previous = None
        elif stripped.strip():
            previous = (offset, stripped.strip(), number)
        else:
            previous = None
        offset += len(line)
    return found


def _closes(fence: tuple[str, int], match: re.Match[str]) -> bool:
    """CommonMark: a closing fence uses the same character, is at least as long, and
    carries no info string. A fence opened with four backticks is not closed by three."""
    char, length = fence
    run = match.group(0).strip()
    return (
        match.group("char") == char
        and len(run) - len(match.group("info")) >= length
        and not match.group("info").strip()
    )
