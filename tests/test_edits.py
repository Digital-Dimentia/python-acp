"""Tests for the format-agnostic half: the op model, the splicer, and the verifier.

The verifier is the product, so the tests that matter most here are the ones that make it
*fail*. A verifier is code that always passes on correct input, which means an untested
one is indistinguishable from `return True` — `test_a_locator_off_by_one_byte_is_caught`
and its neighbours exist to keep that from being true silently.

Dialect-specific behaviour lives in `test_edit_json.py`; what is exercised here through
`JSON_DIALECT` is the machinery around it, not JSON.
"""

from __future__ import annotations

import pytest

from python_acp.edit_json import JSON_DIALECT, JsonDialect
from python_acp.edits import (
    UNSET,
    AddressNotFound,
    Confidence,
    EditError,
    Location,
    Op,
    OpKind,
    OverlappingOps,
    Span,
    VerificationFailed,
    apply,
)
from python_acp.errors import to_request_error

SIMPLE = '{\n  "a": 1,\n  "b": [1, 2],\n  "c": {"d": "x"}\n}\n'


# ---------------------------------------------------------------------------
# The op model
# ---------------------------------------------------------------------------


def test_value_and_scalar_are_mutually_exclusive() -> None:
    with pytest.raises(EditError, match="not both"):
        Op(OpKind.SET, "/a", value="1", scalar=1)


def test_a_kind_that_needs_a_value_refuses_without_one() -> None:
    with pytest.raises(EditError, match="needs a value"):
        Op(OpKind.SET, "/a")


def test_delete_refuses_a_value() -> None:
    with pytest.raises(EditError, match="takes no value"):
        Op(OpKind.DELETE, "/a", scalar=1)


def test_scalar_none_is_a_value_not_an_omission() -> None:
    """`None` is JSON `null`, so the sentinel has to be something else.

    Without `UNSET`, setting a key to null would be indistinguishable from forgetting to
    pass a value, and the resulting error would name the wrong problem.
    """
    assert Op(OpKind.SET, "/a", scalar=None).scalar is None
    assert Op(OpKind.DELETE, "/a").scalar is UNSET
    assert apply(SIMPLE, [Op(OpKind.SET, "/a", scalar=None)], dialect=JSON_DIALECT).updated == (
        '{\n  "a": null,\n  "b": [1, 2],\n  "c": {"d": "x"}\n}\n'
    )


def test_no_ops_is_a_refusal() -> None:
    with pytest.raises(EditError, match="no ops"):
        apply(SIMPLE, [], dialect=JSON_DIALECT)


# ---------------------------------------------------------------------------
# Errors reach the client as -32602 with no special-casing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        AddressNotFound("no such address: /x"),
        OverlappingOps("two ops collided"),
        VerificationFailed("the locator is wrong"),
    ],
)
def test_every_refusal_maps_to_invalid_params(exc: EditError) -> None:
    """The point of subclassing `ValueError`: `errors.py` needs no case for any of these."""
    error = to_request_error(exc)

    assert error.code == -32602
    assert str(exc) in error.data["reason"]


# ---------------------------------------------------------------------------
# The verifier, made to fail
# ---------------------------------------------------------------------------


def _sabotage(monkeypatch: pytest.MonkeyPatch, shift: int, *, value_span: bool = True) -> None:
    """Move every located span by `shift` bytes, leaving everything else correct."""
    original = JsonDialect.plan

    def shifted(self: JsonDialect, source: str, parsed: object, op: Op) -> Location:
        located = original(self, source, parsed, op)
        moved = Span(located.span.start + shift, located.span.end + shift)
        moved_value = (
            Span(located.value_span.start + shift, located.value_span.end + shift)
            if located.value_span is not None and value_span
            else located.value_span
        )
        return Location(
            span=moved,
            replacement=located.replacement,
            segments=located.segments,
            value_span=moved_value,
            parsed_value=located.parsed_value,
        )

    monkeypatch.setattr(JsonDialect, "plan", shifted)


@pytest.mark.parametrize("shift", [-1, 1, 4])
def test_a_locator_off_by_one_byte_is_caught(
    monkeypatch: pytest.MonkeyPatch, shift: int
) -> None:
    """Step 2, and the single most valuable check in the module.

    It runs before the file is touched, and it fails on exactly the bug the whole design
    defends against: an address that resolved plausibly to the wrong offsets.
    """
    _sabotage(monkeypatch, shift)

    with pytest.raises(VerificationFailed, match="locator is wrong"):
        apply(SIMPLE, [Op(OpKind.SET, "/a", scalar=5)], dialect=JSON_DIALECT)


def test_a_splice_that_lands_on_a_sibling_is_caught_by_step_six(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 6 backstops step 2, and only step 6 can catch this.

    The sabotage points the *spliced* span at a sibling holding an identical value,
    leaving `value_span` correct. So step 2 is satisfied, the result is still valid JSON,
    and steps 5 and 7 are happy — the file simply says something other than what was
    asked for. Nothing but applying the same op to the parsed document and comparing
    would notice, which is the argument for step 6 existing at all.
    """
    source = '{"a": 1, "b": 1}'
    original = JsonDialect.plan

    def redirected(self: JsonDialect, source: str, parsed: object, op: Op) -> Location:
        located = original(self, source, parsed, op)
        sibling = original(self, source, parsed, Op(OpKind.SET, "/b", value="0"))
        return Location(
            span=sibling.span,
            replacement=located.replacement,
            segments=located.segments,
            value_span=located.value_span,
            parsed_value=located.parsed_value,
        )

    monkeypatch.setattr(JsonDialect, "plan", redirected)

    with pytest.raises(VerificationFailed, match="does not mean what applying"):
        apply(source, [Op(OpKind.SET, "/a", scalar=5)], dialect=JSON_DIALECT)


def test_overlapping_ops_are_refused_before_anything_is_spliced() -> None:
    """Refused, never merged: a merge needs a rule for which op wins, and any such rule
    silently discards half of what the caller asked for."""
    with pytest.raises(OverlappingOps, match="overlapping spans"):
        apply(
            SIMPLE,
            [Op(OpKind.SET, "/b", value="[]"), Op(OpKind.SET, "/b/0", scalar=9)],
            dialect=JSON_DIALECT,
        )


def test_a_failed_edit_leaves_no_partial_result() -> None:
    """`apply` returns a result or raises. There is no third outcome to inspect."""
    with pytest.raises(EditError):
        apply(
            SIMPLE,
            [Op(OpKind.SET, "/a", scalar=1), Op(OpKind.SET, "/missing", scalar=2)],
            dialect=JSON_DIALECT,
        )


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


def test_the_result_carries_how_strongly_it_was_checked() -> None:
    """A weakness in a field is one a caller can see; one in a paragraph is not."""
    result = apply(SIMPLE, [Op(OpKind.SET, "/a", scalar=2)], dialect=JSON_DIALECT)

    assert result.confidence is Confidence.SEMANTIC


def test_setting_a_value_to_what_it_already_holds_verifies_but_does_not_change() -> None:
    result = apply(SIMPLE, [Op(OpKind.SET, "/a", scalar=1)], dialect=JSON_DIALECT)

    assert result.updated == SIMPLE
    assert not result.changed


def test_applied_ops_carry_spans_so_acp_v2_needs_no_re_derivation() -> None:
    """`pyacp-u2y`: v2 replaces oldText/newText with structured changes, which a
    whole-file before/after pair cannot be decomposed back into."""
    result = apply(
        SIMPLE,
        [Op(OpKind.SET, "/a", scalar=7), Op(OpKind.SET, "/c/d", scalar="y")],
        dialect=JSON_DIALECT,
    )

    assert [a.segments for a in result.applied] == [("a",), ("c", "d")]
    assert [(a.old_text, a.new_text) for a in result.applied] == [("1", "7"), ('"x"', '"y"')]


def test_the_unified_diff_is_a_rendering_not_the_content() -> None:
    """`Diff.new_text` is whole-file content; `unified()` is for a human to read.

    Worth pinning because handing the diff to that field is a mistake that typechecks.
    """
    result = apply(SIMPLE, [Op(OpKind.SET, "/a", scalar=2)], dialect=JSON_DIALECT)

    assert result.updated.startswith("{")
    assert result.unified().startswith("---")
    assert '-  "a": 1' in result.unified()
    assert '+  "a": 2' in result.unified()


def test_spans_touching_end_to_end_do_not_count_as_overlapping() -> None:
    assert not Span(0, 5).overlaps(Span(5, 9))
    assert Span(0, 5).overlaps(Span(4, 9))


def test_two_insertions_at_one_offset_do_overlap() -> None:
    """Both would splice at the same place, so the result would depend on ordering."""
    assert Span(3, 3).overlaps(Span(3, 3))
