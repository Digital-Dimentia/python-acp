"""Tests for the YAML locator, against files kept for their formatting.

`tests/data/edits/yaml/kustomization.yaml` is the fixture that matters. It keeps its
comments, its blank lines, a flush sequence and an indented one **in the same file**, a
sequence nested inside a sequence item's mapping, a block scalar, an empty flow
collection and a key with no value at all — because those are the bytes under test. A
locator that is right about structure and careless about layout passes every test written
against a tidy file.

The refusal corpus beside it is the other half. Each file isolates one construct this
module will not touch, and the tests assert the exception type **and** that the message
names the construct: the refusal boundary is an interface, and a refusal that does not say
what it refused sends the reader to the source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from python_acp.edit_yaml import YAML_DIALECT, _scan
from python_acp.edits import (
    AddressNotFound,
    EditError,
    Op,
    OpKind,
    UnsupportedConstruct,
    ValueSyntaxError,
    apply,
)

DATA = Path(__file__).parent / "data" / "edits" / "yaml"
KUSTOMIZATION = DATA / "kustomization.yaml"


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


def _sweep() -> list[str]:
    source = KUSTOMIZATION.read_text()
    return [a for a in _addresses(YAML_DIALECT.parse(source)) if a]


# ---------------------------------------------------------------------------
# The property that is worth more than the rest combined
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", _sweep())
def test_setting_every_address_to_its_own_value_changes_nothing(address: str) -> None:
    """Setting a value to the text it already holds must give the file back byte for byte.

    Swept over every address in the fixture, so it grows whenever the fixture does. Any
    locator off by a byte, any splice that eats a comment, and any accidental re-emission
    fails here on some address — and this finds which one without anyone having thought of
    it in advance.

    Two addresses answer differently and both are deliberate. `/notes` is a block scalar,
    which is refused as a target rather than located; `/emptyValue` is `key:` with nothing
    after it, which has no bytes to set to their own value — its own test is below.

    `value_span` is `None` for a mapping lifted off a `- ` line, which is step 2 being
    skipped there on purpose. The span it replaces is still the thing under test, so the
    sweep reads that instead and the property holds for those addresses too.
    """
    source = KUSTOMIZATION.read_text()
    probe = Op(OpKind.SET, address, value="0")
    try:
        located = YAML_DIALECT.plan(source, YAML_DIALECT.parse(source), probe)
    except UnsupportedConstruct as exc:
        assert "block scalar" in str(exc), address
        return
    current = (located.value_span or located.span).text(source)
    if not current:
        assert address == "/emptyValue"
        return

    result = apply(source, [Op(OpKind.SET, address, value=current)], dialect=YAML_DIALECT)

    assert result.updated == source
    assert not result.changed


def test_a_key_with_no_value_gains_the_one_space_it_needs() -> None:
    """`key:` has no bytes to replace, so the module supplies the separator. It is the only
    byte this dialect adds that the caller did not write, and it is added nowhere else."""
    source = KUSTOMIZATION.read_text()

    result = apply(source, [Op(OpKind.SET, "/emptyValue", scalar=7)], dialect=YAML_DIALECT)

    assert result.updated == source.replace("emptyValue:", "emptyValue: 7")


# ---------------------------------------------------------------------------
# Layout the module copies rather than chooses
# ---------------------------------------------------------------------------


def test_a_flush_sequence_and_an_indented_one_each_keep_their_own_shape() -> None:
    """The reason step 3 is not a byte comparison, asserted as behaviour: one file holds
    both styles, and an append copies whichever container it lands in."""
    source = KUSTOMIZATION.read_text()

    flush = apply(source, [Op(OpKind.APPEND, "/resources", scalar="extra.yaml")], dialect=YAML_DIALECT)
    indented = apply(
        source,
        [Op(OpKind.APPEND, "/images", value="name: registry.example.com/worker\n    newTag: v0.1")],
        dialect=YAML_DIALECT,
    )

    assert "\n- extra.yaml\n" in flush.updated
    assert "\n  - name: registry.example.com/worker\n    newTag: v0.1\n" in indented.updated


def test_a_sequence_nested_inside_a_sequence_items_mapping_is_addressable() -> None:
    """`configMapGenerator[0].literals` sits at a column its own `- ` line never occupied.
    It is the shape a line scanner gets wrong, so it is in the fixture."""
    source = KUSTOMIZATION.read_text()

    result = apply(
        source,
        [Op(OpKind.SET, "/configMapGenerator/0/literals/1", scalar="REGION=us-east-1")],
        dialect=YAML_DIALECT,
    )

    assert "    - REGION=us-east-1\n" in result.updated
    assert "    - LOG_LEVEL=info\n" in result.updated


def test_a_trailing_comment_survives_an_edit_to_the_value_beside_it() -> None:
    """The span stops at the comment, because a `#` after whitespace opens one — the rule a
    naive split on `#` gets wrong in both directions."""
    source = KUSTOMIZATION.read_text()

    result = apply(
        source, [Op(OpKind.SET, "/resources/1", scalar="svc.yaml")], dialect=YAML_DIALECT
    )

    assert "- svc.yaml   # the one thing this overlay adds\n" in result.updated


def test_every_comment_in_the_file_survives_an_unrelated_edit() -> None:
    """Nothing is re-emitted, so this is step 7 restated in the terms a reader cares about."""
    source = KUSTOMIZATION.read_text()
    comments = [line for line in source.splitlines() if line.lstrip().startswith("#")]

    result = apply(source, [Op(OpKind.SET, "/namespace", scalar="billing")], dialect=YAML_DIALECT)

    assert [line for line in result.updated.splitlines() if line.lstrip().startswith("#")] == comments


def test_an_insertion_copies_the_containers_indent_rather_than_choosing_one() -> None:
    source = KUSTOMIZATION.read_text()

    result = apply(source, [Op(OpKind.INSERT, "/images/0", value="name: first\n    newTag: v0")], dialect=YAML_DIALECT)

    assert result.updated.index("- name: first") < result.updated.index("- name: registry.example.com/payments")


def test_a_value_opening_on_its_own_line_gets_no_trailing_space() -> None:
    """`key: ` with nothing after the space is trailing whitespace on a finished line."""
    source = KUSTOMIZATION.read_text()

    result = apply(
        source, [Op(OpKind.INSERT, "/labels", value="\n  team: payments")], dialect=YAML_DIALECT
    )

    assert "\nlabels:\n  team: payments\n" in result.updated


def test_appending_to_a_file_that_does_not_end_in_a_newline() -> None:
    """The newline goes *before* the new member, so the file's own last byte is not one we
    changed."""
    source = "resources:\n- a.yaml\n- b.yaml"

    result = apply(source, [Op(OpKind.APPEND, "/resources", scalar="c.yaml")], dialect=YAML_DIALECT)

    assert result.updated == "resources:\n- a.yaml\n- b.yaml\n- c.yaml"


def test_a_delete_and_reinsert_restores_the_documents_meaning() -> None:
    """Not its bytes: the key comes back at the end of its mapping. The weaker claim is the
    honest one and still catches a delete that removed too much."""
    source = KUSTOMIZATION.read_text()
    document = YAML_DIALECT.parse(source)

    removed = apply(source, [Op(OpKind.DELETE, "/namespace")], dialect=YAML_DIALECT)
    restored = apply(
        removed.updated, [Op(OpKind.INSERT, "/namespace", scalar="payments")], dialect=YAML_DIALECT
    )

    assert YAML_DIALECT.parse(restored.updated) == document


# ---------------------------------------------------------------------------
# Scalars: the renderer that checks its own work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["yes", "no", "on", "off", "true", "null", "~", "N", "y"])
def test_a_string_a_yaml_11_reader_would_call_a_boolean_is_quoted(value: str) -> None:
    """The one gap the reparse check cannot close: our oracle reads YAML 1.2, where `on` is
    the string `on`, so the plain spelling looks safe here while a 1.1 reader on the other
    end of the file gets `True`."""
    assert YAML_DIALECT.render_scalar(value) == f'"{value}"'


@pytest.mark.parametrize("value", ["0755", "1.0", "- x", "#x", "a: b", "  pad ", ""])
def test_a_string_that_would_read_back_as_something_else_is_quoted(value: str) -> None:
    """These the reparse *does* catch, which is why the renderer proposes and verifies
    rather than carrying a table of YAML's resolution rules."""
    rendered = YAML_DIALECT.render_scalar(value)

    assert YAML_DIALECT.parse_fragment(rendered) == value
    assert rendered != value


@pytest.mark.parametrize("value", ["payments", "hello world", "registry.example.com/app"])
def test_an_unambiguous_string_stays_unquoted(value: str) -> None:
    assert YAML_DIALECT.render_scalar(value) == value


def test_scalar_refuses_a_container_rather_than_choosing_its_indentation() -> None:
    with pytest.raises(EditError, match="scalars only"):
        YAML_DIALECT.render_scalar({"a": 1})


# ---------------------------------------------------------------------------
# The refusal corpus — one file per construct, each naming what it refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "names"),
    [
        ("anchors.yaml", "anchor"),
        ("merge-key.yaml", "merge key"),
        ("tab-indent.yaml", "tab"),
        ("multidoc-unindexed.yaml", "second document"),
        ("tags.yaml", "tag"),
        ("multiline-plain.yaml", "multi-line plain scalar"),
    ],
)
def test_a_refused_construct_is_named_by_the_refusal(fixture: str, names: str) -> None:
    source = (DATA / fixture).read_text()

    with pytest.raises(UnsupportedConstruct, match=names):
        apply(source, [Op(OpKind.SET, "/name", scalar="x")], dialect=YAML_DIALECT)


def test_a_block_scalar_is_refused_only_as_the_target() -> None:
    """Present elsewhere it is scanned past and left alone — the fixture is full of edits
    made to a file that contains one."""
    source = (DATA / "block-scalar-target.yaml").read_text()

    with pytest.raises(UnsupportedConstruct, match="block scalar"):
        apply(source, [Op(OpKind.SET, "/script", value="x")], dialect=YAML_DIALECT)

    assert apply(source, [Op(OpKind.SET, "/name", scalar="other")], dialect=YAML_DIALECT).changed


def test_flow_style_is_refused_only_where_an_address_goes_inside_it() -> None:
    """Replacing a whole flow collection is an ordinary span substitution. Addressing a
    member of one is not: a wrong offset inside `{a: 1, b: 2}` still parses."""
    source = (DATA / "flow-style.yaml").read_text()

    with pytest.raises(UnsupportedConstruct, match="flow collection"):
        apply(source, [Op(OpKind.SET, "/limits/cpu", scalar="1")], dialect=YAML_DIALECT)

    result = apply(source, [Op(OpKind.SET, "/ports", value="[8080]")], dialect=YAML_DIALECT)
    assert "ports: [8080]\n" in result.updated


def test_deleting_the_only_member_is_refused_with_what_to_do_instead() -> None:
    """It would leave `key:` with nothing after it, which YAML reads as null rather than as
    an empty mapping — so step 6 would reject it with a message about the verifier."""
    source = "service:\n  name: payments\n"

    with pytest.raises(UnsupportedConstruct, match="only member"):
        apply(source, [Op(OpKind.DELETE, "/service/name")], dialect=YAML_DIALECT)


def test_a_file_that_does_not_parse_is_never_edited() -> None:
    with pytest.raises(ValueSyntaxError, match="not valid YAML"):
        apply("a: [1, 2\n", [Op(OpKind.SET, "/a", value="x")], dialect=YAML_DIALECT)


def test_duplicate_keys_are_refused_because_an_address_would_be_ambiguous() -> None:
    with pytest.raises(ValueSyntaxError):
        apply("a: 1\na: 2\n", [Op(OpKind.SET, "/a", scalar=3)], dialect=YAML_DIALECT)


def test_a_malformed_replacement_is_rejected_with_its_parse_error() -> None:
    source = KUSTOMIZATION.read_text()

    with pytest.raises(ValueSyntaxError):
        apply(source, [Op(OpKind.SET, "/namespace", value="[unclosed")], dialect=YAML_DIALECT)


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_a_missing_key_names_the_prefix_that_did_resolve() -> None:
    source = KUSTOMIZATION.read_text()

    with pytest.raises(AddressNotFound, match="commonLabels"):
        apply(source, [Op(OpKind.SET, "/commonLabels/nope", scalar="x")], dialect=YAML_DIALECT)


def test_an_index_past_the_end_suggests_append() -> None:
    source = KUSTOMIZATION.read_text()

    with pytest.raises(AddressNotFound, match="append"):
        apply(source, [Op(OpKind.SET, "/resources/9", scalar="x")], dialect=YAML_DIALECT)


def test_inserting_over_an_existing_key_points_at_set() -> None:
    source = KUSTOMIZATION.read_text()

    with pytest.raises(EditError, match="use set"):
        apply(source, [Op(OpKind.INSERT, "/namespace", scalar="x")], dialect=YAML_DIALECT)


def test_appending_to_a_mapping_says_it_is_not_a_sequence() -> None:
    source = KUSTOMIZATION.read_text()

    with pytest.raises(EditError, match="not a sequence"):
        apply(source, [Op(OpKind.APPEND, "/commonLabels", scalar="x")], dialect=YAML_DIALECT)


# ---------------------------------------------------------------------------
# Step 3, and the verifier it feeds
# ---------------------------------------------------------------------------


def test_the_oracle_check_passes_on_a_file_no_dumper_could_reproduce() -> None:
    """The whole reason step 3 is not `dump(load(src)) == src`: this file mixes a flush
    sequence with an indented one, `ruamel` has one global sequence indent, and no setting
    reproduces both. The strict form would call a correct file unverifiable."""
    assert YAML_DIALECT.round_trip_ok(KUSTOMIZATION.read_text()) is True


def test_a_mislocated_span_is_caught_before_the_file_is_touched() -> None:
    """Step 2 with the locator sabotaged by one byte. Without this the verifier is untested
    code that always passes."""
    from python_acp.edits import Location, Span

    source = KUSTOMIZATION.read_text()
    real = YAML_DIALECT.plan

    def off_by_one(src: str, parsed: object, op: Op) -> Location:
        located = real(src, parsed, op)
        shifted = Span(located.span.start + 1, located.span.end + 1)
        return Location(
            span=shifted,
            replacement=located.replacement,
            segments=located.segments,
            value_span=shifted,
            parsed_value=located.parsed_value,
        )

    class Sabotaged:
        name = YAML_DIALECT.name
        confidence = YAML_DIALECT.confidence
        parse = YAML_DIALECT.parse
        parse_fragment = YAML_DIALECT.parse_fragment
        render_scalar = YAML_DIALECT.render_scalar
        round_trip_ok = YAML_DIALECT.round_trip_ok
        plan = staticmethod(off_by_one)

    with pytest.raises(EditError):
        apply(source, [Op(OpKind.SET, "/namespace", scalar="x")], dialect=Sabotaged())


def test_the_scanner_accounts_for_every_line_or_refuses() -> None:
    """A locator that silently skipped a construct would leave a span nobody checked."""
    root = _scan(KUSTOMIZATION.read_text())

    assert [entry.key for entry in root.entries] == [
        "apiVersion",
        "kind",
        "namespace",
        "resources",
        "commonLabels",
        "images",
        "replicas",
        "configMapGenerator",
        "patches",
        "notes",
        "emptyValue",
    ]
