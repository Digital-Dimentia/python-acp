"""YAML, addressed by JSON Pointer. Our scanner locates; `ruamel.yaml` parses and judges.

The division of labour is the whole design. [edit_json.py](edit_json.md) could use the
standard library as an oracle for free; there is **no YAML parser in the standard
library**, and a hand-rolled loader sitting beside this module's hand-rolled scanner
would not be independent in the sense that matters — same author, same misreading of the
spec, shared bugs — so the verifier would be agreeing with itself. That is the entire
justification for the one runtime dependency that is not protocol surface, and it is
written out again in `pyproject.toml` next to the pin.

So: **the scanner never decides what anything means.** It finds byte spans. `ruamel.yaml`
says what the document holds, and `edits.apply`'s seven steps make the two agree or
refuse.

## Addressing is RFC 6901, the same as JSON

Not a YAML-specific dotted path. Pointer escaping is published, an LLM already knows it,
and the two dialects share `edits.pointer_segments` — a dotted syntax would buy nothing
and cost an escaping bug the first time a key contains a dot.

## Refuse, do not guess

This is the module's governing rule and it is why the refusal list is long. A construct
this scanner does not fully understand is a construct where a plausible-looking span is
worse than an error, because the splice would land somewhere and the file would still
parse. Each of these is `UnsupportedConstruct`, named:

* **Flow style** (`{a: 1}`, `[1, 2]`) where an address goes *inside* one. Flow collections
  nest on a line and their span arithmetic is a different problem — a wrong offset within
  `{a: 1, b: 2}` still produces a file that parses. Replacing a *whole* flow collection is
  an ordinary span substitution and is allowed, which is the useful case anyway.
* **Anchors `&x`, aliases `*x`, and merge keys `<<`.** An alias means the addressed value
  has a *second definition site*, so a splice would change one occurrence and not the
  other, or both — and which of those a caller wanted is not knowable from the address.
* **Block scalars `|` and `>` as the target.** Fine elsewhere in the file: they are scanned
  past and left alone. As the thing being set, the replacement's indentation would have to
  be re-derived from the indicator, which is emitting rather than splicing.
* **Multi-line plain scalars.** A plain scalar continued on the following line looks
  exactly like a nested block to a line scanner, and the two are told apart only by the
  parser this module refuses to be.
* **Explicit keys (`? `), non-scalar keys, and tags (`!!str`, `!Custom`).**
* **Directives (`%YAML`, `%TAG`) and multi-document streams.** The address names a value,
  not a document, so a stream is ambiguous before the walk even begins.
* **Tabs in indentation.** YAML forbids them; a file containing them is not YAML, and the
  useful thing to say is which line rather than a parse error from somewhere else.

## Step 3, and why it is not `dump(load(src)) == src`

The plan for this module specified byte-identical round-trip idempotency as the
oracle-degradation check. **It was implemented, tried against real files, and rejected**,
because it refuses ordinary correct ones: `ruamel` has a single global sequence indent,
while real YAML mixes flush sequences (`resources:` then `- a` at the same column) with
indented ones (`images:` then `  - name: ...`) in the same file. No dumper setting
reproduces both, so the strict form answers "this file is unverifiable" to a file nothing
is wrong with.

The check that replaced it asks the question the strict form was *reaching* for — did the
oracle lose or change anything? — without demanding layout fidelity it has no business
demanding here:

1. round-trip load the source, dump it, and require the dump to **mean** what the source
   means (safe-parse both, compare);
2. require the dumper to be **stable**: dumping the reloaded dump reproduces it.

Layout is not what step 3 protects in this dialect, because **nothing is ever re-emitted**.
Step 7 — every byte outside the spliced spans is unchanged — guards formatting absolutely,
which is a far stronger guarantee than any round-trip comparison could offer. The comments,
the blank lines and the indent width of a file this module edits survive because they are
never rewritten, not because a dumper agreed to reproduce them.

## Deleting the last member of a collection is refused

Removing the only key under `metadata:` leaves `metadata:` with nothing after it, and YAML
reads that as **null**, not as an empty mapping. The structural apply in step 6 produces an
empty mapping, the reparsed file produces null, and the edit is rejected by a message about
the verifier rather than about the request. So it is refused up front, saying what to write
instead.

## What this module adds on its own

Separators, and nothing else.

* **A space after a colon that had none.** `key:` with no value has no bytes to overwrite,
  so a `set` there supplies the one byte between the key and its new value.
* **The indentation and the `- ` of an inserted member**, both *copied* from the members
  the container already has rather than chosen. A container with nothing to copy from
  cannot arise: an empty block collection is `key:` with nothing after it, which is a null.

A multi-line `value` is spliced **verbatim** and must already carry the indentation of the
place it is going, written at its absolute column in the finished file. Re-indenting a
caller's text would be an emitter, which is the thing this design exists without.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from python_acp.edits import (
    AddressNotFound,
    Confidence,
    EditError,
    Location,
    Op,
    OpKind,
    Segments,
    Span,
    UnsupportedConstruct,
    ValueSyntaxError,
    pointer_segments,
)

#: What a plain (unquoted) scalar may look like before this module will emit one. Narrow
#: on purpose: `render_scalar` verifies its own output by reparsing it, so the cost of
#: being too narrow is a pair of quotes and the cost of being too wide is a wrong file.
_PLAIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-+/= ]*$")

#: Words a **YAML 1.1** reader resolves to a boolean or a null and a YAML 1.2 reader does
#: not. They are quoted unconditionally, because the reparse check cannot catch them: our
#: oracle is 1.2, so `on` reads back as the string `on` and looks safe here — while the
#: consumer of the file (PyYAML, and anything that reaches Kubernetes through a 1.1
#: parser) reads it as `True`. This is the one disagreement between the oracle and the
#: rest of the world that the module has to know about rather than measure.
_YAML_11_WORDS = frozenset(
    "y Y yes Yes YES n N no No NO true True TRUE false False FALSE "
    "on On ON off Off OFF null Null NULL ~".split()
)

#: Indicators refused wherever they appear, and what each is. These are whole-file
#: refusals rather than path ones: an anchor defined anywhere can be aliased *onto* the
#: path, so "not on the path" is not a property this scanner can check locally.
_REFUSED_INDICATORS = {
    "&": "an anchor (&name)",
    "*": "an alias (*name)",
    "!": "a tag",
}

#: The two flow openers, and their closers. Flow is refused only where it is walked
#: *into* — see `_resolve`. A flow value that is merely present, or that is itself the
#: target of a `set`, is an ordinary span like any other.
_FLOW = {"{": "}", "[": "]"}

class _NotAMappingEntry(Exception):
    """Internal: this line does not open `key: value`.

    Its own type rather than an `EditError`, because `_mapping_ahead` swallows it to
    decide a sequence item's shape — and swallowing a *refusal* there would lose it.
    """


@dataclass(frozen=True)
class _Line:
    """One physical line, with the offsets a span is built from."""

    start: int
    """Offset of the first character of the line."""

    end: int
    """Offset just past the line's newline, or of end-of-file."""

    text: str
    """The line without its newline."""

    number: int
    """1-based, for messages. A YAML refusal that does not name a line is not actionable."""

    @property
    def indent(self) -> int:
        return len(self.text) - len(self.text.lstrip(" "))

    @property
    def content(self) -> str:
        return self.text.lstrip(" ")

    @property
    def text_end(self) -> int:
        """Offset just past the last character, excluding the newline."""
        return self.start + len(self.text)

    @property
    def is_content(self) -> bool:
        """Blank lines and whole-line comments are not structure and are never spanned."""
        return bool(self.content) and not self.content.startswith("#")


@dataclass
class _Entry:
    """One member of a block collection: its key, the whole line-range it occupies, its value."""

    key: str | int
    node: _Node
    span: Span
    """Key line start through the end of the value, **including** the trailing newline."""

    inline: bool = False
    """This entry opened on a `- ` line, so its span does not start at the line's start."""


@dataclass
class _Node:
    """A located value. `kind` is what the *scanner* saw, never what the oracle decided."""

    kind: str
    """`scalar`, `map`, `seq`, or `block` — a block scalar, which may not be a target."""

    span: Span
    """The value's own bytes. Zero-width for an implicit null (`key:` with nothing after)."""

    entries: list[_Entry] = field(default_factory=list)
    indent: int = 0
    """The column this collection's members sit at, so an insertion can copy it."""

    implicit_null: bool = False
    """`key:` with no value. A `SET` here has to bring its own separating space."""

    lifted: bool = False
    """This mapping opened on a `- ` line, so its span does not parse standalone."""


# ---------------------------------------------------------------------------
# The dialect
# ---------------------------------------------------------------------------


class YamlDialect:
    """The `edits.Dialect` implementation for YAML. The oracle is `ruamel.yaml`."""

    name = "yaml"
    confidence = Confidence.SEMANTIC

    def parse(self, source: str) -> Any:
        """The oracle: a safe load, which returns plain `dict`/`list`/scalars.

        The whole-file refusals — tabs in indentation, directives, a second document —
        run *before* the load, so the message names the construct rather than repeating
        `ruamel`'s report of where its scanner gave up. Both are true; only one is
        actionable.

        Plain rather than round-trip types on purpose. `edits._apply_to_structure` is
        written against `Mapping` and `MutableSequence`, and step 6 compares the result
        with `==`; the round-tripper's `CommentedMap` carries comment and format metadata
        that has no business influencing either.
        """
        _reject_stream(_split(source))
        try:
            return _safe().load(source)
        except YAMLError as exc:
            raise ValueSyntaxError(f"not valid YAML: {_terse(exc)}") from exc

    def parse_fragment(self, text: str) -> Any:
        """What a located span, or a caller's raw `value`, means.

        **Dedented first.** A span lifted out of a document carries the indentation of the
        place it came from, and a block mapping indented as a whole is a different document
        from the same mapping at column zero — to some parsers, an error. Removing the
        common indent is the only transformation applied, and it cannot change the
        fragment's meaning because it is applied to every line equally.
        """
        try:
            return _safe().load(_dedent(text))
        except YAMLError as exc:
            raise ValueSyntaxError(f"not a valid YAML value: {text!r} ({_terse(exc)})") from exc

    def render_scalar(self, value: Any) -> str:
        """A Python scalar as YAML source, plain when that is safe and quoted when it is not.

        **The renderer checks its own work**: it proposes the plain spelling, reparses it,
        and falls back to a double-quoted one unless the plain form reads back as exactly
        the value it was given. That is what makes `1.0`, `0755`, `- x` and a string with a
        trailing space safe to pass as `scalar=` without the caller knowing YAML's
        resolution rules.

        The reparse cannot catch everything, and `_YAML_11_WORDS` is the gap it cannot
        close: our oracle reads YAML 1.2, where `on` is the string `on`, so the check
        passes — while a 1.1 reader on the other end of the file gets `True`. Those words
        are quoted unconditionally rather than measured.
        """
        if isinstance(value, dict | list | tuple):
            raise EditError(
                "scalar= renders scalars only; pass value= with the YAML source text for a "
                "mapping or a sequence, so its indentation and style are yours"
            )
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            if isinstance(value, float) and value != value:
                return ".nan"
            if value in (float("inf"), float("-inf")):
                return ".inf" if value > 0 else "-.inf"
            return repr(value)
        if not isinstance(value, str):
            raise EditError(f"scalar= renders scalars only; {value!r} is a {type(value).__name__}")
        if _PLAIN.match(value) and value.strip() == value and value not in _YAML_11_WORDS:
            try:
                if self.parse_fragment(value) == value:
                    return value
            except ValueSyntaxError:
                pass
        # A JSON string literal *is* a YAML double-quoted scalar: YAML's double-quoted
        # escapes are a superset of JSON's, so this needs no escaping logic of its own.
        return json.dumps(value)

    def round_trip_ok(self, source: str) -> bool | None:
        """Step 3: did the oracle lose anything? See the module docstring for why this is
        not the byte-identical comparison the plan called for."""
        try:
            once = _dump(_round_trip().load(source))
            if _safe().load(once) != _safe().load(source):
                return False
            return _dump(_round_trip().load(once)) == once
        except YAMLError:
            # An unreadable file is step 1's refusal to make, with its own message. Saying
            # "cannot round-trip" about a file that does not parse would name the wrong
            # problem.
            return True

    def plan(self, source: str, parsed: Any, op: Op) -> Location:
        root = _scan(source)
        segments = pointer_segments(op.address)
        if op.kind is OpKind.APPEND:
            return _plan_append(source, root, segments, op, self)
        if op.kind is OpKind.INSERT:
            return _plan_insert(source, root, segments, op, self)
        node, resolved = _resolve(root, segments, op.address)
        if op.kind is OpKind.SET:
            return _plan_set(source, node, resolved, op, self)
        return _plan_delete(root, segments, resolved, op)


YAML_DIALECT = YamlDialect()


def _safe() -> YAML:
    """A fresh loader per call. `ruamel`'s `YAML` objects are documented as not reusable
    across a load and a dump, and a shared one would be state between two edits."""
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    return yaml


def _round_trip() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    # Wide enough that the dumper never re-wraps a long scalar. A re-wrap is a formatting
    # change, and step 3 would read it as the oracle losing something.
    yaml.width = 1 << 20
    yaml.allow_duplicate_keys = False
    return yaml


def _dump(data: Any) -> str:
    buffer = io.StringIO()
    _round_trip().dump(data, buffer)
    return buffer.getvalue()


def _terse(exc: Exception) -> str:
    """A YAML error on one line. `ruamel`'s `__str__` is a multi-line context block, which
    reads badly inside a refusal that is itself one sentence."""
    return " ".join(str(exc).split())


def _dedent(text: str) -> str:
    """Remove the indentation every non-blank line shares.

    Whole lines only, so no line's position *relative to another* moves and the fragment
    cannot come to mean something else. It is applied to a copy; the bytes spliced into
    the file are what the caller wrote.

    It deliberately does **not** try to re-align a first line that sits at a shallower
    column than the rest. That reading is right for a span lifted from a `- ` line and
    wrong for a caller's `name:\n  sub: y`, where the second line is a child of the first
    — and nothing in the text distinguishes them. The two callers are told apart at their
    own call sites instead: `_in_context` for a value being inserted, and no step-2 check
    at all for a mapping lifted off a sequence item.
    """
    lines = text.splitlines(keepends=True)
    widths = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    common = min(widths, default=0)
    return "".join(line[common:] if line.strip() else line for line in lines)


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def _scan(source: str) -> _Node:
    """Every span in the document, or a refusal naming the construct and the line."""
    lines = _split(source)
    _reject_stream(lines)
    content = [line for line in lines if line.is_content and line.text.strip() != "---"]
    if not content:
        return _Node(kind="scalar", span=Span(0, len(source)))
    node, index = _parse_node(content, 0, content[0].indent)
    if index < len(content):
        raise UnsupportedConstruct(
            f"line {content[index].number}: this scanner could not account for "
            f"{content[index].text.strip()!r}; refusing rather than editing around it"
        )
    return node


def _split(source: str) -> list[_Line]:
    lines: list[_Line] = []
    offset = 0
    for number, raw in enumerate(source.splitlines(keepends=True), start=1):
        text = raw.rstrip("\n").rstrip("\r")
        lines.append(_Line(start=offset, end=offset + len(raw), text=text, number=number))
        offset += len(raw)
    return lines


def _reject_stream(lines: list[_Line]) -> None:
    """The whole-file refusals, checked before any structure is read."""
    seen_start = False
    seen_content = False
    for line in lines:
        if "\t" in line.text[: len(line.text) - len(line.text.lstrip())]:
            raise UnsupportedConstruct(
                f"line {line.number}: a tab in the indentation. YAML forbids tabs there, "
                "so this file is not YAML and no locator can be trusted on it"
            )
        stripped = line.text.strip()
        if stripped.startswith("%"):
            raise UnsupportedConstruct(
                f"line {line.number}: a directive ({stripped.split()[0]}); this locator "
                "handles a single plain document"
            )
        if stripped == "..." or stripped.startswith("... "):
            raise UnsupportedConstruct(
                f"line {line.number}: an explicit document end; the address names a value, "
                "not a document, so a stream is ambiguous before the walk begins"
            )
        if stripped == "---" or stripped.startswith("--- "):
            if seen_start or seen_content:
                raise UnsupportedConstruct(
                    f"line {line.number}: a second document. The address names a value, not "
                    "a document, so a multi-document stream is ambiguous"
                )
            seen_start = True
            if stripped != "---":
                raise UnsupportedConstruct(
                    f"line {line.number}: content on the document marker; refusing rather "
                    "than guessing where the root begins"
                )
            continue
        if line.is_content:
            seen_content = True


def _parse_node(lines: list[_Line], index: int, indent: int) -> tuple[_Node, int]:
    """A block collection beginning at `lines[index]`, whose members sit at `indent`."""
    if _is_item(lines[index]):
        return _parse_seq(lines, index, indent)
    try:
        return _parse_map(lines, index, indent)
    except _NotAMappingEntry:
        pass
    # A document whose root is a bare scalar. Addressable only at the root, which is the
    # honest answer: there are no members to walk to.
    return _parse_value(lines, index, indent, indent=indent)


def _is_item(line: _Line) -> bool:
    return line.content == "-" or line.content.startswith("- ")


def _parse_map(
    lines: list[_Line], index: int, indent: int, *, inline_column: int | None = None
) -> tuple[_Node, int]:
    """A block mapping whose entries sit at `indent`.

    `inline_column` is set when the mapping's **first** entry shares a `- ` line with the
    sequence item that holds it, which is the one place a member's column and its line's
    indentation differ.
    """
    entries: list[_Entry] = []
    while index < len(lines):
        line = lines[index]
        inline_first = inline_column is not None and not entries
        column = inline_column if inline_first else indent
        assert column is not None
        if not inline_first and (line.indent != indent or _is_item(line)):
            break
        key, value_start = _split_key(line, column)
        node, index = _parse_value(lines, index, value_start, indent=column)
        entries.append(
            _Entry(
                key=key,
                node=node,
                # An entry that opens on a `- ` line starts *after* the dash: its span is
                # the entry, not the sequence item that carries it.
                span=Span(line.start + (column if inline_first else 0), lines[index - 1].end),
                inline=inline_first,
            )
        )
        if inline_first:
            indent = column
    return (
        _Node(
            kind="map",
            span=Span(entries[0].span.start, entries[-1].span.end - _newline(lines, index)),
            entries=entries,
            indent=indent,
            lifted=inline_column is not None,
        ),
        index,
    )


def _parse_seq(lines: list[_Line], index: int, indent: int) -> tuple[_Node, int]:
    """A block sequence. Its items sit at `indent`, which may equal the parent key's —
    both `resources:\n- a` and `resources:\n  - a` are ordinary YAML and both occur in
    the same real file, which is also why step 3 is not a byte comparison."""
    entries: list[_Entry] = []
    first = lines[index]
    while index < len(lines) and lines[index].indent == indent and _is_item(lines[index]):
        line = lines[index]
        rest = line.content[1:]
        payload = rest.strip()
        if payload.startswith("- "):
            raise UnsupportedConstruct(
                f"line {line.number}: a sequence item opening another sequence on the same "
                "line; refusing rather than guessing its indentation"
            )
        if not payload or payload.startswith("#"):
            index = _child_of(lines, index, indent, line)
            node, index = _parse_node(lines, index, lines[index].indent)
        else:
            column = indent + 1 + (len(rest) - len(rest.lstrip(" ")))
            if _mapping_ahead(line, column):
                node, index = _parse_map(lines, index, column, inline_column=column)
            else:
                node, index = _parse_value(lines, index, column, indent=indent)
        entries.append(
            _Entry(key=len(entries), node=node, span=Span(line.start, lines[index - 1].end))
        )
    if not entries:
        raise _NotAMappingEntry
    return (
        _Node(
            kind="seq",
            span=Span(first.start, entries[-1].span.end - _newline(lines, index)),
            entries=entries,
            indent=indent,
        ),
        index,
    )


def _child_of(lines: list[_Line], index: int, indent: int, line: _Line) -> int:
    """The index of the first line of a nested block, or a refusal if there is none."""
    if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
        raise UnsupportedConstruct(
            f"line {line.number}: a sequence item with nothing after it; an empty item is "
            "null, and this locator has no span to give it"
        )
    return index + 1


def _mapping_ahead(line: _Line, column: int) -> bool:
    """Does a `- ` item open a mapping rather than hold a scalar?"""
    try:
        _split_key(line, column)
    except _NotAMappingEntry:
        return False
    return True


def _parse_value(
    lines: list[_Line], index: int, column: int, *, indent: int
) -> tuple[_Node, int]:
    """The value beginning at `column` on `lines[index]`, and the index after it.

    `indent` is the column a following line must **exceed** to belong to this value. For
    a mapping entry that is the key's own column; for a sequence item's scalar it is the
    item's indentation, since the scalar has no key to be measured against.
    """
    line = lines[index]
    text = line.text[column:]
    payload, _ = _uncommented(text)
    stripped = payload.rstrip()
    if not stripped:
        if index + 1 < len(lines) and (
            lines[index + 1].indent > indent
            or (lines[index + 1].indent == indent and _is_item(lines[index + 1]))
        ):
            return _parse_node(lines, index + 1, lines[index + 1].indent)
        # `key:` with nothing after it. The value is null and occupies no bytes, so a SET
        # here brings its own separating space — see `_plan_set`.
        anchor = column
        return _Node(kind="scalar", span=Span(line.start + anchor, line.start + anchor),
                     implicit_null=True), index + 1
    indicator = stripped[0]
    if indicator in _FLOW:
        return _parse_flow(line, column, stripped, indicator), index + 1
    if indicator in _REFUSED_INDICATORS:
        raise UnsupportedConstruct(
            f"line {line.number}: {_REFUSED_INDICATORS[indicator]} on the path to the "
            "address. It is refused rather than guessed at, because a splice would still "
            "produce a file that parses"
        )
    if indicator in "|>":
        return _parse_block_scalar(lines, index, column, indent)
    if _continues(lines, index, indent):
        raise UnsupportedConstruct(
            f"line {line.number}: a multi-line plain scalar. To a line scanner it is "
            "indistinguishable from a nested block, and telling them apart is the parser's "
            "job, not this module's"
        )
    end = column + len(stripped)
    return _Node(kind="scalar", span=Span(line.start + column, line.start + end)), index + 1


def _parse_flow(line: _Line, column: int, stripped: str, opener: str) -> _Node:
    """A flow collection, recorded as one opaque span.

    Not walked into and not taken apart: `_resolve` refuses an address that goes inside
    one, because flow nesting is a different span problem and a plausible-but-wrong offset
    inside `{a: 1, b: 2}` still produces a file that parses. Replacing the whole thing is
    fine, and is the useful case anyway.
    """
    depth = 0
    for i, char in enumerate(stripped):
        if char in _FLOW:
            depth += 1
        elif char in _FLOW.values():
            depth -= 1
            if depth == 0:
                if i + 1 != len(stripped):
                    raise UnsupportedConstruct(
                        f"line {line.number}: something follows a flow collection on the "
                        "same line; refusing rather than guessing where the value ends"
                    )
                break
    if depth != 0:
        raise UnsupportedConstruct(
            f"line {line.number}: a flow collection spanning lines. Its extent is a "
            f"parser's answer, not a line scanner's"
        )
    return _Node(
        kind="flow", span=Span(line.start + column, line.start + column + len(stripped))
    )


def _continues(lines: list[_Line], index: int, indent: int) -> bool:
    """Does the scalar on this line run onto the next one?"""
    return index + 1 < len(lines) and lines[index + 1].indent > indent


def _parse_block_scalar(
    lines: list[_Line], index: int, column: int, indent: int
) -> tuple[_Node, int]:
    """Scan past a `|` or `>` block. Its lines are consumed so nothing mistakes them for
    structure; the node refuses to be a target, and `_plan_set` says why."""
    start = lines[index].start + column
    end = lines[index].text_end
    index += 1
    while index < len(lines) and lines[index].indent > indent:
        end = lines[index].text_end
        index += 1
    return _Node(kind="block", span=Span(start, end)), index


def _newline(lines: list[_Line], index: int) -> int:
    """How many bytes of trailing newline the last consumed line had."""
    last = lines[index - 1]
    return last.end - last.text_end


def _uncommented(text: str) -> tuple[str, str]:
    """A value and its trailing comment. A `#` opens one only after whitespace, and never
    inside a quoted scalar — the two rules a naive `split('#')` gets wrong."""
    if text[:1] in ("'", '"'):
        quote = text[0]
        i = 1
        while i < len(text):
            if text[i] == "\\" and quote == '"':
                i += 2
                continue
            if text[i] == quote:
                return text[: i + 1], text[i + 1 :]
            i += 1
        return text, ""
    for i, char in enumerate(text):
        if char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i], text[i:]
    return text, ""


def _split_key(line: _Line, column: int) -> tuple[str, int]:
    """The key at `column`, and the column its value starts at."""
    text = line.text[column:]
    if text.startswith("<<:") or text.startswith("<< :"):
        raise UnsupportedConstruct(
            f"line {line.number}: a merge key (<<). The merged members have a second "
            "definition site, so an address under this mapping may name a value that is "
            "not written here at all"
        )
    if text.startswith("? "):
        raise UnsupportedConstruct(
            f"line {line.number}: an explicit key (? ). Its value is a separate node and "
            "this locator addresses neither half of it"
        )
    if text[:1] in _REFUSED_INDICATORS:
        raise UnsupportedConstruct(
            f"line {line.number}: {_REFUSED_INDICATORS[text[0]]} on the path to the address"
        )
    if text[:1] in ("'", '"'):
        quoted, rest = _uncommented(text)
        key = _quoted_key(quoted, line)
        used = len(quoted)
    else:
        cut = _plain_key_end(text)
        if cut is None:
            raise _NotAMappingEntry
        key = text[:cut].rstrip()
        used = cut
        rest = text[cut:]
    if not rest.startswith(":"):
        raise _NotAMappingEntry
    value = rest[1:]
    return key, column + used + 1 + (len(value) - len(value.lstrip(" ")))


def _plain_key_end(text: str) -> int | None:
    """Where a plain key stops: at the `:` that is followed by a space or ends the line."""
    for i, char in enumerate(text):
        if char == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            return i
        if char == "#" and i and text[i - 1] == " ":
            return None
    return None


def _quoted_key(quoted: str, line: _Line) -> str:
    """A quoted key, unquoted by the oracle rather than by a slice of our own."""
    try:
        key = _safe().load(quoted)
    except YAMLError as exc:
        raise UnsupportedConstruct(f"line {line.number}: unreadable key ({_terse(exc)})") from exc
    if not isinstance(key, str):
        raise UnsupportedConstruct(
            f"line {line.number}: a non-string key ({key!r}); RFC 6901 addresses name "
            "strings, so this locator cannot say which key an address means"
        )
    return key


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _resolve(root: _Node, segments: tuple[str, ...], address: str) -> tuple[_Node, Segments]:
    node = root
    resolved: list[str | int] = []
    for depth, raw in enumerate(segments):
        prefix = "/" + "/".join(segments[: depth + 1])
        if node.kind == "map":
            match = next((e for e in node.entries if e.key == raw), None)
            if match is None:
                raise AddressNotFound(
                    f"no such address: {prefix}; {_describe(segments[:depth])} is a mapping "
                    f"with keys {sorted(str(e.key) for e in node.entries)}"
                )
            resolved.append(raw)
            node = match.node
        elif node.kind == "seq":
            resolved.append(_index(raw, len(node.entries), prefix))
            node = node.entries[resolved[-1]].node  # type: ignore[index]
        elif node.kind == "flow":
            raise UnsupportedConstruct(
                f"no such address: {prefix}; {_describe(segments[:depth])} is a flow "
                "collection, and this locator does not address inside one — a wrong offset "
                "within {a: 1, b: 2} still produces a file that parses. Set or replace the "
                "whole collection instead"
            )
        else:
            raise AddressNotFound(
                f"no such address: {prefix}; the value at {_describe(segments[:depth])} is "
                f"a {node.kind} and has no members"
            )
    return node, tuple(resolved)


def _describe(segments: tuple[str, ...]) -> str:
    return "/" + "/".join(segments) if segments else "the document"


def _index(raw: str, length: int, prefix: str) -> int:
    if not raw.isdigit():
        raise AddressNotFound(
            f"no such address: {prefix}; that container is a sequence of {length}, and "
            f"{raw!r} is not an index" + (' — use "-" to append' if raw == "-" else "")
        )
    index = int(raw)
    if index >= length:
        raise AddressNotFound(
            f"no such address: {prefix}; that container is a sequence of {length}, so index "
            f'{index} is past the end — use "-" to append'
        )
    return index


def _plan_set(
    source: str, node: _Node, resolved: Segments, op: Op, dialect: YamlDialect
) -> Location:
    if node.kind == "block":
        raise UnsupportedConstruct(
            f"set {op.address!r}: that value is a block scalar (| or >). Replacing one means "
            "re-deriving the indentation its indicator implies, which is emitting rather "
            "than splicing"
        )
    text = op.source_text(dialect)
    if node.implicit_null:
        # The key has no value at all, so there is nothing to overwrite and no space
        # between the colon and where the value goes. This is the one byte the module adds.
        return Location(
            span=node.span,
            replacement=f" {text}",
            segments=resolved,
            value_span=node.span,
            parsed_value=dialect.parse_fragment(text),
        )
    return Location(
        span=node.span,
        replacement=text,
        segments=resolved,
        # A mapping that opened on a `- ` line has a span whose first line starts at column
        # zero while its continuations start where the dash pushed them, so it does not
        # parse standalone and step 2 has nothing to compare. Skipped here rather than
        # patched up, because every rule that would repair it also breaks a caller's own
        # nested fragment — see `_dedent`. Step 6 carries the check, and step 2 is still
        # live for every scalar underneath, which is where edits actually land.
        value_span=None if node.lifted else node.span,
        # The replacement is written in context too — a lifted mapping's first line starts
        # at the column the dash pushed it to — so it is read the same way it is written.
        parsed_value=(
            _in_context(dialect, text, node.indent)
            if node.lifted
            else dialect.parse_fragment(text)
        ),
    )


def _plan_delete(
    root: _Node, segments: tuple[str, ...], resolved: Segments, op: Op
) -> Location:
    if not segments:
        raise EditError("delete needs an address naming a member, not the root")
    container, _ = _resolve(root, segments[:-1], op.address)
    if container.kind not in ("map", "seq"):
        raise EditError(f"delete {op.address!r}: a {container.kind} has no members to remove")
    if len(container.entries) == 1:
        raise UnsupportedConstruct(
            f"delete {op.address!r}: that is the only member of its collection, and removing "
            "it would leave the key with nothing after it — which YAML reads as null, not as "
            "an empty mapping or sequence. Set the collection to an empty one instead"
        )
    index = _member_index(container, resolved[-1])
    entry = container.entries[index]
    return Location(
        span=entry.span,
        replacement="",
        segments=resolved,
        # No `value_span`: an entry's span covers its key and its newline, and neither
        # parses standalone as the value the address named. Step 6 carries the check.
        value_span=None,
    )


def _member_index(container: _Node, key: str | int) -> int:
    for i, entry in enumerate(container.entries):
        if entry.key == key:
            return i
    raise AddressNotFound(f"{key!r} is not a member of that container")


def _plan_append(
    source: str, root: _Node, segments: tuple[str, ...], op: Op, dialect: YamlDialect
) -> Location:
    container, resolved = _resolve(root, segments, op.address)
    if container.kind != "seq":
        raise EditError(f"append {op.address!r}: that address is a {container.kind}, not a sequence")
    text = op.source_text(dialect)
    span, replacement = _insertion(source, container, len(container.entries), f"- {text}")
    return Location(
        span=span,
        replacement=replacement,
        segments=(*resolved, len(container.entries)),
        parsed_value=_in_context(dialect, text, container.indent + 2),
    )


def _plan_insert(
    source: str, root: _Node, segments: tuple[str, ...], op: Op, dialect: YamlDialect
) -> Location:
    if not segments:
        raise EditError("insert needs an address naming the new member, not the root")
    container, resolved = _resolve(root, segments[:-1], op.address)
    last = segments[-1]
    text = op.source_text(dialect)
    if container.kind == "map":
        if any(entry.key == last for entry in container.entries):
            raise EditError(f"insert {op.address!r}: that key already exists; use set to replace it")
        key = dialect.render_scalar(last)
        # A value that opens on its own line gets no space after the colon: the space would
        # be trailing whitespace on a line whose content is already complete.
        member = f"{key}:{text}" if text.startswith("\n") else f"{key}: {text}"
        span, replacement = _insertion(source, container, len(container.entries), member)
        return Location(
            span=span,
            replacement=replacement,
            segments=(*resolved, last),
            parsed_value=_in_context(dialect, text, container.indent + len(key) + 2),
        )
    if container.kind == "seq":
        index = int(last) if last.isdigit() else len(container.entries)
        if index > len(container.entries):
            raise AddressNotFound(
                f"insert {op.address!r}: that sequence holds {len(container.entries)}, so "
                f"{index} would leave a hole"
            )
        span, replacement = _insertion(source, container, index, f"- {text}")
        return Location(
            span=span,
            replacement=replacement,
            segments=(*resolved, index),
            parsed_value=_in_context(dialect, text, container.indent + 2),
        )
    raise EditError(f"insert {op.address!r}: cannot add a member to a {container.kind}")


def _in_context(dialect: YamlDialect, text: str, column: int) -> Any:
    """What an inserted value will mean *where it is going*, rather than in isolation.

    An inserted block sits after `- ` or after `key: `, so its first line starts at a
    column its own continuation lines are already written against — which leaves the
    fragment ragged and unparseable on its own even though the file it produces is fine.
    Prefixing the first line with the column it will occupy is what makes the two agree.
    It is a reading of the caller's text, never a rewrite of it.
    """
    return dialect.parse_fragment(" " * column + text)


def _insertion(source: str, container: _Node, index: int, member: str) -> tuple[Span, str]:
    """Where a new member goes, with the indentation the container already uses.

    A zero-width span plus a replacement, so the splice stays a substitution and step 7
    can still check that nothing outside it moved. The indent is **copied** from the
    members that are there; `_FALLBACK_INDENT` is reached only by a collection that has
    none, which this module refuses to produce in the first place.
    """
    indent = " " * container.indent
    if index >= len(container.entries):
        anchor = container.entries[-1].span.end
        if anchor > 0 and source[anchor - 1] != "\n":
            # The file ends without a newline. One is added *before* the new member rather
            # than after, so the file's own last byte is not something we changed.
            return Span(anchor, anchor), f"\n{indent}{member}"
        return Span(anchor, anchor), f"{indent}{member}\n"
    anchor = container.entries[index].span.start
    return Span(anchor, anchor), f"{indent}{member}\n"
