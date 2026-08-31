"""Structured edits that preserve every byte they did not address, and prove it.

An LLM decides *what* to change; this module decides whether that change can be made to
this file without collateral damage, makes it, and then checks its own work. There is no
model in here and there must never be one — the value of the module is that its output is
verifiable, and a verifier that shares an author with a generator verifies nothing.

## Why not diffs

The obvious input format is a unified diff, and it is the wrong one. An LLM's `@@ -12,7`
line numbers are unreliable, so applying one means fuzzy context matching, and once the
match is fuzzy the question "did this land in the right place?" has no answer — the
matcher's own confidence is the only evidence, and it is the thing under suspicion. A
path-addressed op makes the question answerable against an independent parser, and
answering it is the entire point of the module.

## Locate-then-splice, never parse-mutate-reserialise

Every edit resolves an address to a **byte span** and substitutes text into it. Nothing is
re-emitted. The reason is not that splicing preserves formatting better — a good emitter
preserves a lot — but that it preserves it *by construction*, which is what makes step 7
of the verifier possible at all. You cannot assert "the untouched bytes are unchanged"
about a file you rebuilt from an AST; you can only hope the emitter agreed with itself.

The cost is real and is paid in the dialects: `json.dumps` on a `package-lock.json` would
be a 40,000-line diff, so `edit_json.py` carries a hand-written span-recording tokenizer
instead of calling the stdlib. That is the trade.

## `Op.value` is raw source text

Not a Python object. An object would have to be serialised into the target format, and
that serialiser is exactly the emitter this design exists to avoid — it would choose a
quoting style, an indent width, a scalar folding mode, none of which are ours to choose.
Raw text means the splice is a byte substitution and the module never has an opinion.

The cost is that a caller can hand us a syntactically broken fragment, and that cost is
absorbed entirely by step 5: the fragment fails the reparse and the whole edit is
rejected with the parse error. A loud rejection is strictly better than a silently
restyled file, which is the failure mode of every tool that round-trips through an AST.

`Op.scalar` exists for the common case (`scalar=42`) and is rendered by a **scalar-only**
mini-renderer per dialect. It never renders a container; a caller wanting one writes text.

## The verifier is the product

Seven steps, aborting at the first failure, and **any failure rejects the entire edit**.
Never a partial application, not even for ops that succeeded independently: a half-applied
structured edit is syntactically valid, semantically half-intended, and nobody diffs it.

1. **Pre-parse the original.** A file we cannot read is a file we do not edit. This is also
   what correctly refuses a Helm template that is YAML-shaped but not YAML.
2. **Span/value agreement.** Parse the located bytes *standalone* and require the result to
   equal what the oracle reports at that path in the whole file. This runs before anything
   is modified and is the check that catches a locator that landed on the wrong span.
3. **Round-trip idempotency**, where a dialect has a round-tripper at all. A failure means
   the file uses constructs the oracle does not preserve, so the oracle is degraded — that
   is a refusal, not a warning.
4. **Splice**, spans sorted descending and required pairwise non-overlapping.
5. **Re-parse** the result.
6. **Semantic equality.** Apply the same ops to the *parsed structure* independently, in
   the same order, and require the reparsed file to equal it. This is the step that
   catches a splice which landed somewhere plausible but wrong, and it handles sequence
   index shifts for free because both sides shift identically.
7. **Untouched-byte identity** outside the spliced spans.

Step 6 is worth dwelling on. The structural apply and the splice are independent
implementations of the same intent — one over values, one over bytes — so requiring them
to agree is a genuine cross-check rather than a tautology. Ordering both descending is
required for them to *be* comparable, not a weakening: an index-shifting op has to happen
in the same order on both sides or they would disagree for a reason that is not a bug.

## What this module does not know

Any format. Dialects supply parsing, locating and rendering; everything here is written
against the `Dialect` protocol. It also does not know ACP: `EditResult` deliberately does
not import `acp.schema`, because the conversion to `FileEditToolCallContent` belongs in
[mcp_content.py](mcp_content.md), which already owns "our types to ACP content". Keeping
that import out is what lets this module be exercised by a plain unit test with no
connection, and it is what keeps the neutrality seam in
`tests/test_executor_neutrality.py` honest.
"""

from __future__ import annotations

import copy
import difflib
from collections.abc import Mapping, MutableMapping, MutableSequence, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

#: A resolved address. `str` segments index a mapping, `int` segments a sequence. The
#: empty tuple is the document root.
Segments = tuple[str | int, ...]


class _Unset:
    """The default for `Op.scalar`, because `None` is a value a caller may mean.

    JSON `null` and YAML `~` are ordinary scalars, so `scalar=None` has to be
    distinguishable from "the caller did not pass scalar". A sentinel is the only way to
    tell those apart in a dataclass field, and getting this wrong would make setting a
    key to null impossible for reasons no error message would explain.
    """

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


class OpKind(StrEnum):
    """The four verbs. Every dialect implements all four or refuses explicitly."""

    SET = "set"
    """Replace the value at an address that must already exist."""

    INSERT = "insert"
    """Add a member or element at an address that must **not** already exist."""

    DELETE = "delete"
    """Remove the member or element at an address that must already exist."""

    APPEND = "append"
    """`INSERT` at the end of a sequence, where naming the index would be a race."""


#: The kinds that change a sequence's length, and therefore the indices after them. They
#: are the reason both the splice and the structural apply run in descending span order.
SHIFTING = frozenset({OpKind.INSERT, OpKind.DELETE, OpKind.APPEND})


class Confidence(StrEnum):
    """How strongly an edit was checked. Carried in `EditResult`, deliberately.

    A weakness recorded in a field is a weakness a caller and a transcript can see. The
    same weakness recorded only in a paragraph of prose is one nobody reads before
    trusting the result.
    """

    SEMANTIC = "semantic"
    """Step 6 compared parsed values against an independent parser. JSON and YAML."""

    STRUCTURAL = "structural"
    """Step 6 compared a structure derived from the text, not a parse of its meaning.

    Markdown *is* text; there is no semantic model to diff. The comparison is over the
    heading tree and section bodies, which catches a splice landing in the wrong section
    but cannot catch one that produces different-but-valid prose.
    """


# ---------------------------------------------------------------------------
# Errors. All `ValueError`, so `errors.to_request_error` answers -32602 with the
# reason in `data` and nothing has to special-case the type — the same reasoning
# that makes `paths.PathConstraintError` a `ValueError`.
# ---------------------------------------------------------------------------


class EditError(ValueError):
    """Base for every refusal. Never raised directly."""


class AddressNotFound(EditError):
    """The address resolved to nothing.

    The message names the nearest resolvable prefix and what it contains, because
    "not found" alone leaves the caller unable to tell a typo from a wrong assumption
    about the file's shape.
    """


class AddressAmbiguous(EditError):
    """The address resolved to more than one place.

    A refusal, never a "pick the first". Reachable in Markdown (duplicate sibling
    headings); unreachable in JSON, which has one value per pointer.
    """


class UnsupportedConstruct(EditError):
    """The file, or the addressed node, uses something the dialect will not touch.

    The refusal boundary. The message names the construct and the line, and must not
    suggest a workaround it cannot guarantee.
    """


class ValueSyntaxError(EditError):
    """A file, or a supplied `Op.value` fragment, that does not parse."""


class OverlappingOps(EditError):
    """Two ops in one call resolved to overlapping spans.

    Refused rather than merged. A merge would need a rule for which op wins, and any
    such rule silently discards half of what the caller asked for.
    """


class VerificationFailed(EditError):
    """The edit was applied and then failed its own check, so it was thrown away.

    Reaching this is a bug in a dialect, not in the caller's input — every caller-facing
    mistake has a more specific error above. It is raised rather than logged because a
    file we cannot vouch for is worse than no edit.
    """


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Span:
    """A half-open byte range over the source, `[start, end)`."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise VerificationFailed(f"a dialect produced a nonsensical span {self!r}")

    def text(self, source: str) -> str:
        return source[self.start : self.end]

    def overlaps(self, other: Span) -> bool:
        """Touching is not overlapping.

        Two zero-width insertion points at the same offset *do* overlap, though: they
        would both splice at one place and the result would depend on ordering.
        """
        if self.start == self.end and other.start == other.end:
            return self.start == other.start
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Location:
    """A dialect's answer to "where does this op go, and what text goes there?".

    The dialect owns the whole question, including the parts that are not the value —
    the comma before a deleted object member, the indentation of an inserted block. That
    keeps every format's punctuation in the module that understands it, and leaves this
    one splicing opaque text into opaque ranges.
    """

    span: Span
    """The bytes the splice replaces. Zero-width for `INSERT` and `APPEND`."""

    replacement: str
    """The text spliced in. Empty for `DELETE`."""

    segments: Segments
    """The resolved address, for the structural apply in step 6."""

    value_span: Span | None = None
    """The addressed **value** alone, for step 2.

    Distinct from `span`, which for a `DELETE` also covers the key and a comma and so
    would not parse standalone. `None` when the op addresses a place rather than a value,
    which is every `INSERT` and `APPEND`, and means step 2 is skipped for that op.
    """

    parsed_value: Any = None
    """What the replacement means, for the structural apply. Unused for `DELETE`."""


@dataclass(frozen=True)
class AppliedOp:
    """One op, resolved. Kept in the result so ACP v2 can be served without re-deriving.

    v2 replaces `oldText`/`newText` with structured add/delete/modify changes (see bead
    `pyacp-u2y`). A whole-file before/after pair cannot be decomposed back into those; a
    list of spans and their old and new text can. That is the one concession to a schema
    the pinned SDK does not model yet, and it costs a tuple.
    """

    op: Op
    span: Span
    segments: Segments
    old_text: str
    new_text: str


@dataclass(frozen=True)
class EditResult:
    """A verified edit. Constructing one of these is a claim that all seven steps passed."""

    path: str
    original: str
    updated: str
    applied: tuple[AppliedOp, ...]
    confidence: Confidence

    @property
    def changed(self) -> bool:
        """False for an edit that resolved and verified but altered nothing.

        Setting a value to what it already holds is legal and reaches here; a caller
        deciding whether to spend an `fs/write_text_file` round trip wants to know.
        """
        return self.original != self.updated

    def unified(self, context: int = 3) -> str:
        """The change as a unified diff, for a human.

        **Not** for `Diff.new_text`: that field is whole-file content
        (`acp/schema.py`, "The new content after modification"), which is `updated`.
        Handing a diff string to a field documented as content is a mistake that
        typechecks, so it is worth the sentence.

        A caller rendering this into an agent message must fence it —
        `markdown.fenced_lines(...)` with a `diff` info string — because a client
        rendering Markdown reads a leading `-` as a list bullet and eats the column
        that carries the entire meaning.
        """
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.updated.splitlines(keepends=True),
                fromfile=self.path,
                tofile=self.path,
                n=context,
            )
        )


@dataclass(frozen=True)
class Op:
    """One requested change.

    `value` and `scalar` are mutually exclusive and at least one is required for every
    kind but `DELETE`. The split is ergonomic, not semantic: `scalar` covers the common
    case without making a caller quote a string correctly for a format they may not know,
    and is rendered by the dialect's scalar-only renderer.
    """

    kind: OpKind
    address: str
    value: str | None = None
    scalar: Any = UNSET

    def __post_init__(self) -> None:
        if self.value is not None and self.scalar is not UNSET:
            raise EditError(
                f"{self.kind} {self.address!r}: pass value= (raw source text) or "
                "scalar= (a Python scalar), not both"
            )
        if self.kind is OpKind.DELETE:
            if self.value is not None or self.scalar is not UNSET:
                raise EditError(f"delete {self.address!r} takes no value")
        elif self.value is None and self.scalar is UNSET:
            raise EditError(f"{self.kind} {self.address!r} needs a value= or scalar=")

    def source_text(self, dialect: Dialect) -> str:
        """The text this op splices in, however it was spelled.

        `DELETE` has none and must not be asked. Everything else resolves here so a
        dialect never has to re-derive which of the two fields the caller used.
        """
        if self.value is not None:
            return self.value
        return dialect.render_scalar(self.scalar)


@runtime_checkable
class Dialect(Protocol):
    """Everything `apply` needs to know about one file format.

    Split this way so that adding a format is a locator and a renderer, and never a
    second verifier. There is one verifier and it is above.
    """

    name: str
    confidence: Confidence

    def parse(self, source: str) -> Any:
        """The oracle. Raises `ValueSyntaxError` on anything it cannot read.

        Must be an implementation **independent of** the dialect's own locator, or step 6
        degenerates into the locator agreeing with itself. For JSON that is stdlib
        `json.loads`; for YAML it is `ruamel.yaml`; for Markdown there is no independent
        implementation, which is precisely why its confidence is `STRUCTURAL`.
        """

    def parse_fragment(self, text: str) -> Any:
        """What a raw `Op.value` means, for step 6. Raises `ValueSyntaxError`."""

    def render_scalar(self, value: Any) -> str:
        """A Python scalar as source text. Never a container."""

    def round_trip_ok(self, source: str) -> bool | None:
        """Step 3. `None` when the dialect has no round-tripper and the check is moot.

        `None` is not `True`. A dialect that cannot answer must say so rather than
        assert a check it never ran.
        """

    def plan(self, source: str, parsed: Any, op: Op) -> Location:
        """Resolve one op against the file. Raises one of the address errors."""


def apply(
    source: str,
    ops: Sequence[Op],
    *,
    dialect: Dialect,
    path: str = "<memory>",
) -> EditResult:
    """Apply `ops` to `source`, or raise. Never returns a partially applied result.

    `dialect` is passed, never inferred from `path`'s extension. A `.yml` full of Go
    template directives is not YAML, a `.tf.json` is JSON, and a module that guesses is a
    module that will one day reformat a file it did not understand. The caller knows; it
    has to say.
    """
    if not ops:
        raise EditError("no ops given")

    parsed = dialect.parse(source)  # 1. pre-parse
    locations = [dialect.plan(source, parsed, op) for op in ops]
    _require_disjoint(ops, locations)

    for op, location in zip(ops, locations, strict=True):
        _check_span_agreement(source, parsed, op, location, dialect)  # 2.

    if dialect.round_trip_ok(source) is False:  # 3.
        raise UnsupportedConstruct(
            f"{path}: {dialect.name} cannot round-trip this file unchanged, so it cannot "
            "be used as an oracle for it; refusing rather than editing unverifiably"
        )

    order = sorted(range(len(ops)), key=lambda i: locations[i].span, reverse=True)
    updated = _splice(source, [locations[i] for i in order])  # 4.

    reparsed = dialect.parse(updated)  # 5.
    expected = _apply_to_structure(parsed, [(ops[i], locations[i]) for i in order])
    if reparsed != expected:  # 6.
        raise VerificationFailed(
            f"{path}: the spliced file does not mean what applying these ops to the "
            f"parsed document means. This is a locator bug, and the edit was discarded. "
            f"Addresses: {[op.address for op in ops]}"
        )

    _check_untouched(source, updated, [locations[i] for i in order], path)  # 7.

    applied = tuple(
        AppliedOp(
            op=ops[i],
            span=locations[i].span,
            segments=locations[i].segments,
            old_text=locations[i].span.text(source),
            new_text=locations[i].replacement,
        )
        for i in sorted(range(len(ops)), key=lambda i: locations[i].span)
    )
    return EditResult(
        path=path,
        original=source,
        updated=updated,
        applied=applied,
        confidence=dialect.confidence,
    )


def _require_disjoint(ops: Sequence[Op], locations: Sequence[Location]) -> None:
    """Overlapping spans in one call are a refusal.

    Checked before anything is spliced, so the caller learns about the conflict rather
    than about whichever half of it survived.
    """
    for i in range(len(locations)):
        for j in range(i + 1, len(locations)):
            if locations[i].span.overlaps(locations[j].span):
                raise OverlappingOps(
                    f"{ops[i].kind} {ops[i].address!r} and {ops[j].kind} "
                    f"{ops[j].address!r} resolve to overlapping spans "
                    f"{locations[i].span!r} and {locations[j].span!r}"
                )


def _check_span_agreement(
    source: str,
    parsed: Any,
    op: Op,
    location: Location,
    dialect: Dialect,
) -> None:
    """Step 2: the bytes we are about to replace really are the value we addressed.

    The single most valuable check in the module, because it runs *before* the file is
    touched and it fails on exactly the bug the whole design is defending against — a
    locator that resolved a plausible address to the wrong offsets. Without it, a
    mislocated `SET` is caught only in step 6, and a mislocated `DELETE` might not be
    caught at all.
    """
    if location.value_span is None:
        return
    fragment = location.value_span.text(source)
    try:
        standalone = dialect.parse_fragment(fragment)
    except ValueSyntaxError as exc:
        raise VerificationFailed(
            f"{op.kind} {op.address!r}: the located span {location.value_span!r} does not "
            f"parse standalone as {dialect.name} ({exc}); the locator is wrong"
        ) from exc
    expected = _value_at(parsed, location.segments)
    if standalone != expected:
        raise VerificationFailed(
            f"{op.kind} {op.address!r}: the located span holds {standalone!r} but the "
            f"parsed document holds {expected!r} at that address; the locator is wrong"
        )


def _splice(source: str, descending: Sequence[Location]) -> str:
    """Step 4. Spans must already be sorted descending and known disjoint.

    Descending order is what makes this a sequence of independent substitutions: every
    span's offsets still refer to the original string when its turn comes, because
    everything spliced so far lay after it.
    """
    out = source
    for location in descending:
        out = out[: location.span.start] + location.replacement + out[location.span.end :]
    return out


def _check_untouched(
    source: str,
    updated: str,
    descending: Sequence[Location],
    path: str,
) -> None:
    """Step 7: every byte outside a spliced span survived unchanged.

    Trivially true for a correct `_splice`, which is why it is here: it is a guard on the
    splice, not on the dialects, and it costs one string comparison. The gap between
    "trivially true" and "asserted" is where the regression lives.
    """
    ascending = sorted(descending, key=lambda loc: loc.span)
    rebuilt: list[str] = []
    cursor = 0
    delta = 0
    for location in ascending:
        rebuilt.append(source[cursor : location.span.start])
        before = source[cursor : location.span.start]
        after = updated[cursor + delta : location.span.start + delta]
        if before != after:
            raise VerificationFailed(
                f"{path}: bytes outside the edited spans changed; "
                f"expected {before!r} before offset {location.span.start}, got {after!r}"
            )
        rebuilt.append(location.replacement)
        delta += len(location.replacement) - (location.span.end - location.span.start)
        cursor = location.span.end
    rebuilt.append(source[cursor:])
    if "".join(rebuilt) != updated:
        raise VerificationFailed(f"{path}: the spliced file is not the sum of its parts")


# ---------------------------------------------------------------------------
# Step 6: the same intent, applied to values instead of bytes
# ---------------------------------------------------------------------------


def _apply_to_structure(parsed: Any, descending: Sequence[tuple[Op, Location]]) -> Any:
    """What the document should *mean* once these ops are applied.

    Deliberately written against `Mapping` and `MutableSequence` rather than against any
    format, so there is one of these and not one per dialect. A dialect whose `parse`
    returns something exotic must return something that behaves like a dict or a list, or
    step 6 cannot check it — which is a constraint on dialects worth stating plainly.

    Same descending order as the splice, for the same reason: an index-shifting op has to
    see the same indices on both sides or the two disagree over an ordering, not a bug.
    """
    root = copy.deepcopy(parsed)
    for op, location in descending:
        root = _apply_one(root, op.kind, location.segments, location.parsed_value)
    return root


def _apply_one(root: Any, kind: OpKind, segments: Segments, value: Any) -> Any:
    if not segments:
        if kind is not OpKind.SET:
            raise EditError(f"{kind} cannot address the document root")
        return value
    container = _value_at(root, segments[:-1])
    last = segments[-1]
    if isinstance(container, MutableSequence):
        index = int(last)
        if kind is OpKind.SET:
            container[index] = value
        elif kind is OpKind.DELETE:
            del container[index]
        else:
            container.insert(index, value)
    elif isinstance(container, MutableMapping):
        if kind is OpKind.DELETE:
            del container[last]
        else:
            container[last] = value
    else:
        raise AddressNotFound(
            f"{'/'.join(str(s) for s in segments[:-1])!r} is a "
            f"{type(container).__name__}, which has no members to edit"
        )
    return root


def _describe(segments: Segments) -> str:
    """Name a prefix the way a reader would say it aloud; the root is "the document"."""
    return "/" + "/".join(str(s) for s in segments) if segments else "the document"


def _value_at(parsed: Any, segments: Segments) -> Any:
    """Walk a resolved address. Raises `AddressNotFound` naming where it gave up.

    The message carries the prefix that *did* resolve and what it contains, because
    "no such address" on its own cannot distinguish a typo from a wrong belief about the
    file's shape, and those want different fixes.
    """
    current = parsed
    for depth, segment in enumerate(segments):
        prefix = "/" + "/".join(str(s) for s in segments[: depth + 1])
        if isinstance(current, Mapping):
            if segment not in current:
                raise AddressNotFound(
                    f"no such address: {prefix}; {_describe(segments[:depth])} is a "
                    f"mapping with keys {sorted(str(k) for k in current)}"
                )
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
            try:
                index = int(segment)
            except (TypeError, ValueError):
                raise AddressNotFound(
                    f"no such address: {prefix}; that container is a sequence of "
                    f"{len(current)}, and {segment!r} is not an index"
                ) from None
            if not -len(current) <= index < len(current):
                raise AddressNotFound(
                    f"no such address: {prefix}; that container is a sequence of "
                    f"{len(current)}, so index {index} is out of range"
                    + (' — use "-" to append' if index >= len(current) else "")
                )
            current = current[index]
        else:
            raise AddressNotFound(
                f"no such address: {prefix}; "
                f"{type(current).__name__} has no members to descend into"
            )
    return current
