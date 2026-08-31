"""Tests for the JSON locator, against real files kept for their formatting.

The fixtures under `tests/data/edits/json/` are not synthesised. They keep their aligned
values, their tab indentation, their compact one-line containers and their non-ASCII
scalars, because those are the bytes under test — a locator that is correct about
structure and careless about whitespace passes every test written against a tidy file.

`test_setting_every_address_to_its_own_value_changes_nothing` is worth more than the rest
combined: it sweeps every address in every fixture, so it grows whenever a fixture does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from python_acp.edit_json import JSON_DIALECT
from python_acp.edits import (
    AddressNotFound,
    EditError,
    Op,
    OpKind,
    UnsupportedConstruct,
    ValueSyntaxError,
    apply,
)

DATA = Path(__file__).parent / "data" / "edits" / "json"
FIXTURES = sorted(DATA.glob("*.json"))


def _addresses(value: object, prefix: str = "") -> list[str]:
    """Every JSON Pointer in a document, root included."""
    found = [prefix]
    if isinstance(value, dict):
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            found += _addresses(child, f"{prefix}/{escaped}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found += _addresses(child, f"{prefix}/{index}")
    return found


def _cases() -> list[tuple[Path, str]]:
    return [(f, a) for f in FIXTURES for a in _addresses(json.loads(f.read_text())) if a]


@pytest.mark.parametrize(
    ("fixture", "address"), _cases(), ids=lambda p: p.name if isinstance(p, Path) else p
)
def test_setting_every_address_to_its_own_value_changes_nothing(
    fixture: Path, address: str
) -> None:
    """The byte-preservation property, swept over every address in every fixture.

    Setting a value to the text it already holds must produce the file back, byte for
    byte. Any locator that is off by a byte, any splice that eats a comma, and any
    accidental re-emission fails here on some address — and this test finds which one
    without anybody having thought of it in advance.
    """
    source = fixture.read_text()
    span = JSON_DIALECT.plan(source, JSON_DIALECT.parse(source), Op(OpKind.SET, address, value="0"))
    current = span.value_span.text(source)

    result = apply(source, [Op(OpKind.SET, address, value=current)], dialect=JSON_DIALECT)

    assert result.updated == source
    assert not result.changed


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_every_fixture_survives_a_delete_and_reinsert_round_trip(fixture: Path) -> None:
    """Deleting a member and putting it back must restore the document's *meaning*.

    Not its bytes: a delete that swallowed a comma and an insert that copied the
    container's separator can legitimately disagree about where a newline sits. The
    weaker claim is the honest one, and it still catches a delete that removed too much.
    """
    source = fixture.read_text()
    document = json.loads(source)
    key = next(iter(document))

    removed = apply(source, [Op(OpKind.DELETE, f"/{key}")], dialect=JSON_DIALECT)
    restored = apply(
        removed.updated,
        [Op(OpKind.INSERT, f"/{key}", value=json.dumps(document[key]))],
        dialect=JSON_DIALECT,
    )

    assert json.loads(restored.updated) == document


def test_tabs_are_copied_not_replaced_with_spaces() -> None:
    """The module copies a container's punctuation habits rather than choosing its own."""
    source = (DATA / "tabs.json").read_text()

    result = apply(source, [Op(OpKind.APPEND, "/tabbed/two", scalar="c")], dialect=JSON_DIALECT)

    assert '\t\t\t"b",\n\t\t\t"c"' in result.updated
    assert "    " not in result.updated


def test_a_compact_container_stays_compact() -> None:
    source = (DATA / "compact.json").read_text()

    result = apply(source, [Op(OpKind.APPEND, "/compact", scalar=4)], dialect=JSON_DIALECT)

    assert result.updated == '{"compact":[1,2,3,4],"flat":{"a":1,"b":2}}'


def test_a_number_keeps_its_spelling_when_a_sibling_changes() -> None:
    """`1.0`, `1e3` and `1.5e-3` all survive an edit elsewhere in the file.

    `json.dumps` would render them `1.0`, `1000.0` and `0.0015`. Nothing in a semantic
    test would notice, which is exactly why this one is textual.
    """
    source = (DATA / "package.json").read_text()

    result = apply(source, [Op(OpKind.SET, "/version", scalar="9.9.9")], dialect=JSON_DIALECT)

    assert '"numbers": [0, -1, 1.0, 1e3, 1.5e-3]' in result.updated


def test_non_ascii_is_not_escaped_by_an_edit_elsewhere() -> None:
    source = (DATA / "package.json").read_text()

    result = apply(source, [Op(OpKind.SET, "/private", scalar=False)], dialect=JSON_DIALECT)

    assert '"unicode": "café — ✓ é"' in result.updated


def test_an_empty_object_is_filled_at_the_containers_own_indent() -> None:
    source = (DATA / "package.json").read_text()

    result = apply(source, [Op(OpKind.INSERT, "/devDependencies/typescript", scalar="^5.4.0")], dialect=JSON_DIALECT)

    assert '"devDependencies": {"typescript": "^5.4.0"}' in result.updated


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_a_pointer_must_be_empty_or_start_with_a_slash() -> None:
    with pytest.raises(EditError, match="RFC 6901"):
        apply('{"a": 1}', [Op(OpKind.SET, "a", scalar=2)], dialect=JSON_DIALECT)


def test_tilde_one_is_unescaped_before_tilde_zero() -> None:
    """RFC 6901's own worked example: the other order turns `~01` into `/`."""
    source = '{"a~1b": 1}'

    result = apply(source, [Op(OpKind.SET, "/a~01b", scalar=2)], dialect=JSON_DIALECT)

    assert result.updated == '{"a~1b": 2}'


def test_a_slash_in_a_key_is_addressable() -> None:
    source = '{"a/b": 1}'

    result = apply(source, [Op(OpKind.SET, "/a~1b", scalar=2)], dialect=JSON_DIALECT)

    assert result.updated == '{"a/b": 2}'


def test_a_missing_key_names_the_prefix_that_did_resolve() -> None:
    """"Not found" alone cannot distinguish a typo from a wrong belief about the file's
    shape, and those want different fixes."""
    with pytest.raises(AddressNotFound, match=r"the document is an object with keys \['a'\]"):
        apply('{"a": 1}', [Op(OpKind.SET, "/b", scalar=2)], dialect=JSON_DIALECT)


def test_an_index_past_the_end_suggests_append() -> None:
    with pytest.raises(AddressNotFound, match='use "-" to append'):
        apply('{"a": [1]}', [Op(OpKind.SET, "/a/5", scalar=2)], dialect=JSON_DIALECT)


def test_descending_into_a_scalar_says_so() -> None:
    with pytest.raises(AddressNotFound, match="is a scalar and has no members"):
        apply('{"a": 1}', [Op(OpKind.SET, "/a/b", scalar=2)], dialect=JSON_DIALECT)


def test_inserting_over_an_existing_key_points_at_set() -> None:
    with pytest.raises(EditError, match="use set to replace it"):
        apply('{"a": 1}', [Op(OpKind.INSERT, "/a", scalar=2)], dialect=JSON_DIALECT)


# ---------------------------------------------------------------------------
# The refusal boundary
# ---------------------------------------------------------------------------


def test_duplicate_keys_are_refused_because_the_oracle_and_the_locator_disagree() -> None:
    """stdlib json keeps the last, a span scanner finds the first. An address would name
    different bytes depending on who was asked."""
    with pytest.raises(UnsupportedConstruct, match="duplicate key"):
        apply('{"a": 1, "a": 2}', [Op(OpKind.SET, "/a", scalar=3)], dialect=JSON_DIALECT)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_refused(literal: str) -> None:
    """Python's json accepts them, JSON has no such thing, and `nan != nan` would make
    step 6 fail on a file this module had edited perfectly."""
    with pytest.raises(ValueSyntaxError, match="not JSON"):
        apply(f'{{"a": {literal}}}', [Op(OpKind.SET, "/a", scalar=1)], dialect=JSON_DIALECT)


def test_a_file_that_does_not_parse_is_never_edited() -> None:
    with pytest.raises(ValueSyntaxError, match="not valid JSON"):
        apply("{oops", [Op(OpKind.SET, "/a", scalar=1)], dialect=JSON_DIALECT)


def test_a_malformed_replacement_is_rejected_with_its_parse_error() -> None:
    """The cost of `value` being raw text, and the reason that cost is acceptable."""
    with pytest.raises(ValueSyntaxError, match="not a valid JSON value"):
        apply('{"a": 1}', [Op(OpKind.SET, "/a", value="{oops")], dialect=JSON_DIALECT)


def test_scalar_refuses_a_container_rather_than_choosing_its_formatting() -> None:
    with pytest.raises(EditError, match="scalars only"):
        apply('{"a": 1}', [Op(OpKind.SET, "/a", scalar={"b": 2})], dialect=JSON_DIALECT)


def test_round_trip_is_reported_as_not_applicable_rather_than_passing() -> None:
    """`None`, not `True`. Claiming a check passed when it never ran is how a verifier
    starts reporting success by finding nothing."""
    assert JSON_DIALECT.round_trip_ok('{"a": 1}') is None
