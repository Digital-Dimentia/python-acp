"""JSON, located by byte span so that nothing outside the address is re-emitted.

## Why there is a hand-written tokenizer here

`json.loads` is a perfect parser and a useless editor. It discards every byte that does
not carry meaning, and those bytes are the file: indentation, key order under duplicate
keys, `1.0` versus `1e0`, `\\u00e9` versus `é`, the trailing newline. Re-emitting a
`package-lock.json` through `json.dumps` produces a correct file and a 40,000-line diff,
which is not an edit — it is a rewrite that happens to contain one.

So this module scans the source a second time, recording where every value *is*. It is
about 200 lines because JSON's grammar fits on a postcard, and that cheapness is why JSON
was built first: the verifier in [edits.py](edits.md) needed somewhere to be validated
against an oracle that is genuinely independent, and stdlib `json` is exactly that. A
span-arithmetic bug here is caught by a parser that shares no code, no author and no
misunderstanding with it. The same cannot be said of any format whose only parser is ours.

## Two things this refuses, and why they are not pedantry

**Duplicate keys.** `json.loads` keeps the last; a span scanner naturally finds the first.
That divergence would make the oracle and the locator disagree about which bytes an
address names, and the disagreement would surface as a mislocated edit rather than as an
error. RFC 8259 calls the behaviour undefined, so refusing is also the honest reading.

**`NaN` and `Infinity`.** Python's `json` accepts them; JSON does not have them. Beyond
the standards argument, `nan != nan`, so step 6 of the verifier — which compares parsed
documents for equality — would fail on a file it had edited perfectly. A check that
cannot pass on valid input is worse than one that refuses it up front.

## Formatting of what gets inserted

An `INSERT` or `APPEND` has to invent bytes that were never in the file: a comma, a line
break, an indent. There is no principled answer, so the module copies rather than
chooses — it reuses the exact whitespace already separating that container's members, and
falls back to two spaces only for a container that is empty and therefore has no habit to
copy. A compact container stays compact; a container indented with tabs gets tabs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from python_acp.edits import (
    AddressNotFound,
    Confidence,
    EditError,
    Location,
    Op,
    OpKind,
    Segments,
    Span,
    pointer_segments,
    UnsupportedConstruct,
    ValueSyntaxError,
)

_WHITESPACE = " \t\n\r"
_FALLBACK_INDENT = "  "


@dataclass(frozen=True)
class _Member:
    """One `"key": value` pair, or one array element, as written.

    `span` covers the member itself and nothing around it — no comma, no whitespace. The
    punctuation between members is derived at insert or delete time from the gaps between
    these spans, which is how the module copies a file's habits instead of imposing its own.
    """

    key: str | int
    span: Span
    node: _Node


@dataclass(frozen=True)
class _Node:
    """A value, and where it sits.

    `open_end` and `close_start` are the inside faces of a container's brackets — the
    range an insertion into an empty container has to land in, and the only place that
    information exists once the members are known.
    """

    span: Span
    kind: str
    members: tuple[_Member, ...] = ()
    open_end: int = -1
    close_start: int = -1


class JsonDialect:
    """The `edits.Dialect` implementation for JSON."""

    name = "json"
    confidence = Confidence.SEMANTIC

    def parse(self, source: str) -> Any:
        """The oracle: stdlib, with the two divergences above turned into refusals."""
        try:
            value = json.loads(source, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise ValueSyntaxError(f"not valid JSON: {exc}") from exc
        _scan(source)  # duplicate-key detection lives in the scanner
        return value

    def parse_fragment(self, text: str) -> Any:
        try:
            return json.loads(text, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise ValueSyntaxError(f"not a valid JSON value: {text!r} ({exc})") from exc

    def render_scalar(self, value: Any) -> str:
        """A Python scalar as JSON source.

        Containers are refused rather than serialised: `json.dumps` on a dict would pick
        an indent and a key order, which is precisely the emitter this design exists
        without. A caller wanting a container writes `value=` and owns its formatting.
        """
        if isinstance(value, dict | list | tuple):
            raise EditError(
                "scalar= renders scalars only; pass value= with the JSON source text for "
                "a container, so its formatting is yours rather than json.dumps'"
            )
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise UnsupportedConstruct("JSON has no NaN or Infinity")
        return json.dumps(value)

    def round_trip_ok(self, source: str) -> bool | None:
        """`None`: there is no JSON round-tripper here, so step 3 does not apply.

        Not `True`. Claiming a check passed when it never ran is how a verifier starts
        reporting success by finding nothing.
        """
        return None

    def plan(self, source: str, parsed: Any, op: Op) -> Location:
        root = _scan(source)
        segments = pointer_segments(op.address)
        if op.kind is OpKind.APPEND:
            return _plan_append(source, root, segments, op, self)
        if op.kind is OpKind.INSERT:
            return _plan_insert(source, root, segments, op, self)
        node, resolved = _resolve(root, segments, op.address)
        if op.kind is OpKind.SET:
            text = op.source_text(self)
            return Location(
                span=node.span,
                replacement=text,
                segments=resolved,
                value_span=node.span,
                parsed_value=self.parse_fragment(text),
            )
        parent, _ = _resolve(root, segments[:-1], op.address)
        index = _member_index(parent, resolved[-1])
        return Location(
            span=_deletion_span(parent, index),
            replacement="",
            segments=resolved,
            value_span=node.span,
        )


JSON_DIALECT = JsonDialect()


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def _resolve(root: _Node, segments: tuple[str, ...], address: str) -> tuple[_Node, Segments]:
    """Walk the span tree, returning the node and the address with indices as `int`."""
    node = root
    resolved: list[str | int] = []
    for depth, raw in enumerate(segments):
        prefix = "/" + "/".join(segments[: depth + 1])
        if node.kind == "object":
            match = next((m for m in node.members if m.key == raw), None)
            if match is None:
                raise AddressNotFound(
                    f"no such address: {prefix}; {_describe(segments[:depth])} is an "
                    f"object with keys {sorted(str(m.key) for m in node.members)}"
                )
            resolved.append(raw)
            node = match.node
        elif node.kind == "array":
            index = _index(raw, len(node.members), prefix)
            resolved.append(index)
            node = node.members[index].node
        else:
            raise AddressNotFound(
                f"no such address: {prefix}; the value at "
                f"{_describe(segments[:depth])} is a {node.kind} and has no members"
            )
    return node, tuple(resolved)


def _describe(segments: tuple[str, ...]) -> str:
    """Name a prefix the way a reader would say it aloud.

    The root is "the document", not `/`. A pointer to the root really is the empty
    string in RFC 6901, so rendering it as a lone slash names a different address than
    the one that resolved.
    """
    return "/" + "/".join(segments) if segments else "the document"


def _index(raw: str, length: int, prefix: str) -> int:
    if not raw.isdigit():
        raise AddressNotFound(
            f"no such address: {prefix}; that container is an array of {length}, and "
            f"{raw!r} is not an index"
            + (' — use "-" to append' if raw == "-" else "")
        )
    index = int(raw)
    if index >= length:
        raise AddressNotFound(
            f"no such address: {prefix}; that container is an array of {length}, so "
            f'index {index} is past the end — use "-" to append'
        )
    return index


def _member_index(container: _Node, key: str | int) -> int:
    for i, member in enumerate(container.members):
        if member.key == key:
            return i
    raise AddressNotFound(f"{key!r} is not a member of that container")


# ---------------------------------------------------------------------------
# Insertion and deletion: copy the file's punctuation, never invent it
# ---------------------------------------------------------------------------


def _plan_append(
    source: str, root: _Node, segments: tuple[str, ...], op: Op, dialect: JsonDialect
) -> Location:
    container, resolved = _resolve(root, segments, op.address)
    if container.kind != "array":
        raise EditError(f"append {op.address!r}: that address is a {container.kind}, not an array")
    text = op.source_text(dialect)
    span, replacement = _insertion(source, container, len(container.members), text)
    return Location(
        span=span,
        replacement=replacement,
        segments=(*resolved, len(container.members)),
        parsed_value=dialect.parse_fragment(text),
    )


def _plan_insert(
    source: str, root: _Node, segments: tuple[str, ...], op: Op, dialect: JsonDialect
) -> Location:
    if not segments:
        raise EditError("insert needs an address naming the new member, not the root")
    container, resolved = _resolve(root, segments[:-1], op.address)
    last = segments[-1]
    text = op.source_text(dialect)
    if container.kind == "object":
        if any(m.key == last for m in container.members):
            raise EditError(
                f"insert {op.address!r}: that key already exists; use set to replace it"
            )
        member_text = f"{json.dumps(last)}: {text}"
        span, replacement = _insertion(source, container, len(container.members), member_text)
        return Location(
            span=span,
            replacement=replacement,
            segments=(*resolved, last),
            parsed_value=dialect.parse_fragment(text),
        )
    if container.kind == "array":
        index = int(last) if last.isdigit() else len(container.members)
        if index > len(container.members):
            raise AddressNotFound(
                f"insert {op.address!r}: that array holds {len(container.members)}, so "
                f"{index} would leave a hole"
            )
        span, replacement = _insertion(source, container, index, text)
        return Location(
            span=span,
            replacement=replacement,
            segments=(*resolved, index),
            parsed_value=dialect.parse_fragment(text),
        )
    raise EditError(f"insert {op.address!r}: cannot add a member to a {container.kind}")


def _insertion(source: str, container: _Node, index: int, text: str) -> tuple[Span, str]:
    """Where a new member goes and what punctuation it brings.

    A zero-width span plus a replacement, so the splice stays a substitution and step 7
    of the verifier can still check that nothing outside it moved.
    """
    if not container.members:
        return Span(container.open_end, container.close_start), _first_member(source, container, text)
    separator = _separator(source, container)
    if index >= len(container.members):
        anchor = container.members[-1].span.end
        return Span(anchor, anchor), f",{separator}{text}"
    anchor = container.members[index].span.start
    return Span(anchor, anchor), f"{text},{separator}"


def _separator(source: str, container: _Node) -> str:
    """The whitespace this container already puts between its members.

    Copied, never chosen. A file indented with tabs gets tabs, a compact `[1, 2]` gets
    `", "`, and the module never has to hold an opinion about indent width. With one
    member there is no gap to read, so the container's leading whitespace stands in — it
    is the same whitespace a second member would have been preceded by.
    """
    if len(container.members) >= 2:
        gap = source[container.members[0].span.end : container.members[1].span.start]
        return gap.split(",", 1)[1] if "," in gap else gap
    lead = source[container.open_end : container.members[0].span.start]
    return lead or " "


def _first_member(source: str, container: _Node, text: str) -> str:
    """Filling an empty container — the one case with no habit to copy.

    `[]` stays on its line; a container already broken across lines gets its opening
    indent plus `_FALLBACK_INDENT`. This is the only place the module guesses, and it
    guesses only about a container that gave it nothing to go on.
    """
    if "\n" not in source[container.open_end : container.close_start]:
        return text
    outer = _line_indent(source, container.span.start)
    return f"\n{outer}{_FALLBACK_INDENT}{text}\n{outer}"


def _line_indent(source: str, offset: int) -> str:
    """The leading whitespace of the line `offset` falls on."""
    line_start = source.rfind("\n", 0, offset) + 1
    stripped = source[line_start:offset].lstrip()
    return source[line_start : offset - len(stripped)] if stripped else source[line_start:offset]


def _deletion_span(container: _Node, index: int) -> Span:
    """A member plus exactly one separator, so the result is not `{"a": 1, }`.

    Which separator depends on position: a member that is not first takes the comma
    *before* it, so the line above keeps its trailing comma-free state; a first member
    takes the comma after it, which leaves the member that follows sitting at the
    indentation the deleted one occupied. A sole member takes the container's entire
    interior, producing `{}`.
    """
    members = container.members
    if len(members) == 1:
        return Span(container.open_end, container.close_start)
    if index > 0:
        return Span(members[index - 1].span.end, members[index].span.end)
    return Span(members[0].span.start, members[1].span.start)


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueSyntaxError(
        f"{name} is not JSON (RFC 8259 has no {name}), and it would also make this "
        "file impossible to verify, since NaN never compares equal to itself"
    )


def _scan(source: str) -> _Node:
    """Parse `source` into a tree of spans, or raise `ValueSyntaxError`.

    A second parse of a file stdlib `json` has already accepted, so this is deliberately
    not defensive about grammar it cannot reach — its job is offsets, and its errors are
    about the two constructs the oracle accepts and we refuse.
    """
    cursor = _skip_ws(source, 0)
    node, cursor = _scan_value(source, cursor)
    cursor = _skip_ws(source, cursor)
    if cursor != len(source):
        raise ValueSyntaxError(f"trailing content at offset {cursor}")
    return node


def _skip_ws(source: str, i: int) -> int:
    while i < len(source) and source[i] in _WHITESPACE:
        i += 1
    return i


def _scan_value(source: str, i: int) -> tuple[_Node, int]:
    if i >= len(source):
        raise ValueSyntaxError("unexpected end of input")
    char = source[i]
    if char == "{":
        return _scan_container(source, i, "object")
    if char == "[":
        return _scan_container(source, i, "array")
    start = i
    if char == '"':
        i = _scan_string_end(source, i)
    else:
        while i < len(source) and source[i] not in ",]}" and source[i] not in _WHITESPACE:
            i += 1
        literal = source[start:i]
        if literal in ("NaN", "Infinity", "-Infinity"):
            _reject_constant(literal)
    return _Node(span=Span(start, i), kind="scalar"), i


def _scan_string_end(source: str, i: int) -> int:
    i += 1
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if source[i] == '"':
            return i + 1
        i += 1
    raise ValueSyntaxError("unterminated string")


def _scan_container(source: str, start: int, kind: str) -> tuple[_Node, int]:
    closing = "}" if kind == "object" else "]"
    i = start + 1
    open_end = i
    members: list[_Member] = []
    seen: set[str] = set()
    while True:
        i = _skip_ws(source, i)
        if i < len(source) and source[i] == closing:
            return (
                _Node(
                    span=Span(start, i + 1),
                    kind=kind,
                    members=tuple(members),
                    open_end=open_end,
                    close_start=i,
                ),
                i + 1,
            )
        member_start = i
        if kind == "object":
            key_end = _scan_string_end(source, i)
            key = json.loads(source[i:key_end])
            if key in seen:
                raise UnsupportedConstruct(
                    f"duplicate key {key!r} at offset {i}: stdlib json keeps the last and "
                    "a span scanner finds the first, so an address here would name "
                    "different bytes depending on who was asked (RFC 8259 leaves it "
                    "undefined). Deduplicate the file first."
                )
            seen.add(key)
            i = _skip_ws(source, key_end)
            if i >= len(source) or source[i] != ":":
                raise ValueSyntaxError(f"expected ':' at offset {i}")
            i = _skip_ws(source, i + 1)
        else:
            key = len(members)
        node, i = _scan_value(source, i)
        members.append(_Member(key=key, span=Span(member_start, node.span.end), node=node))
        i = _skip_ws(source, i)
        if i < len(source) and source[i] == ",":
            i += 1
            continue
        if i < len(source) and source[i] == closing:
            continue
        raise ValueSyntaxError(f"expected ',' or {closing!r} at offset {i}")
