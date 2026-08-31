"""Tests for the Markdown locator.

The fixture is written to contain the things that break naive scanners — setext headings,
a fenced block full of `#` lines, a nested four-backtick fence, a closing sequence, and
the same `### Errors` heading under two different parents. `ARCHITECTURE.md` is used as a
second fixture precisely because nobody wrote it to be convenient.

Confidence here is `STRUCTURAL`, not `SEMANTIC`, and
`test_the_result_says_it_was_checked_structurally` exists so that stays visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from python_acp.edit_docs import BODY, DOCS_DIALECT
from python_acp.edits import (
    AddressAmbiguous,
    AddressNotFound,
    Confidence,
    EditError,
    Op,
    OpKind,
    UnsupportedConstruct,
    apply,
)

DATA = Path(__file__).parent / "data" / "edits" / "markdown"
GUIDE = DATA / "guide.md"
ROOT = "/# Project Guide"
REPO_DOCS = sorted((Path(__file__).parent.parent / "src" / "python_acp").glob("*.md"))


def _addresses(tree: dict, prefix: str = "") -> list[str]:
    found = []
    for key, child in tree.items():
        if key == BODY:
            continue
        escaped = key.replace("~", "~0").replace("/", "~1")
        found.append(f"{prefix}/{escaped}")
        found += _addresses(child, f"{prefix}/{escaped}")
    return found


def _unique_cases() -> list[tuple[Path, str]]:
    """Every unambiguously addressable heading in every doc this repo ships.

    Duplicated sibling headings are skipped, not because they are uninteresting but
    because they are covered by their own refusal test.
    """
    cases = []
    for doc in [GUIDE, *REPO_DOCS]:
        tree = DOCS_DIALECT.parse(doc.read_text())
        addresses = _addresses(tree)
        cases += [(doc, a) for a in addresses if addresses.count(a) == 1]
    return cases


@pytest.mark.parametrize(
    ("fixture", "address"),
    _unique_cases(),
    ids=lambda v: v.name if isinstance(v, Path) else v,
)
def test_setting_every_section_body_to_its_own_text_changes_nothing(
    fixture: Path, address: str
) -> None:
    """The byte-preservation property, swept over every heading in every doc in the repo.

    A locator that is off by a line eats a heading somewhere in this corpus, and this
    finds which one without anybody having thought of it. It grows with the repo's docs.
    """
    source = fixture.read_text()
    located = DOCS_DIALECT.plan(source, None, Op(OpKind.SET, address, value="x"))
    current = located.value_span.text(source)

    result = apply(source, [Op(OpKind.SET, address, value=current)], dialect=DOCS_DIALECT)

    assert result.updated == source
    assert not result.changed


# ---------------------------------------------------------------------------
# The scanner's hard cases
# ---------------------------------------------------------------------------


def test_a_hash_inside_a_fenced_block_is_not_a_heading() -> None:
    """The one bug every naive implementation has, and this repo's docs are full of
    fenced Markdown examples containing headings."""
    tree = DOCS_DIALECT.parse(GUIDE.read_text())

    assert "## nor this" not in tree["# Project Guide"]
    assert "# a heading inside a nested fence, opened with four backticks" not in str(
        list(tree["# Project Guide"]["## Usage"])
    )


def test_a_four_backtick_fence_is_not_closed_by_three() -> None:
    """CommonMark matches the opening run length. Getting this wrong would end the fence
    early and turn the rest of the example into headings."""
    tree = DOCS_DIALECT.parse(GUIDE.read_text())

    assert list(tree["# Project Guide"]["## Usage"]) == [BODY, "### Flags", "### Errors"]


def test_a_setext_heading_is_found_and_keyed_by_its_level() -> None:
    """`Title` over `-----` is level 2, and is written into the address as `##` so a
    heading path never depends on which spelling the author used."""
    tree = DOCS_DIALECT.parse(GUIDE.read_text())

    assert "## macOS" in tree["# Project Guide"]
    assert "## Linux" in tree["# Project Guide"]


def test_a_setext_section_does_not_eat_its_own_title() -> None:
    """A setext heading starts on the line *above* its underline. Miss that and a `set`
    on the section swallows the title, which is the whole reason this case is tested."""
    source = GUIDE.read_text()

    result = apply(
        source, [Op(OpKind.SET, f"{ROOT}/## macOS", value="Replaced.\n\n")], dialect=DOCS_DIALECT
    )

    assert "macOS\n-----\n" in result.updated
    assert "brew install thing" not in result.updated


def test_a_closing_sequence_is_decoration_not_part_of_the_title() -> None:
    tree = DOCS_DIALECT.parse(GUIDE.read_text())

    assert "## Notes" in tree["# Project Guide"]


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_the_markers_are_part_of_the_key_so_levels_stay_distinguishable() -> None:
    """Without them `/# API/## Errors` and `/# API/### Errors` are the same address."""
    source = GUIDE.read_text()

    result = apply(
        source,
        [Op(OpKind.SET, f"{ROOT}/## API/### Errors", value="Rewritten.\n")],
        dialect=DOCS_DIALECT,
    )

    assert "Rewritten.\n" in result.updated
    assert "See below." in result.updated  # the *other* ### Errors is untouched


def test_the_empty_pointer_addresses_the_preamble() -> None:
    """A real place real edits target — a badge block, a lede — with no heading to name."""
    source = GUIDE.read_text()

    result = apply(source, [Op(OpKind.SET, "", value="> A note.\n\n")], dialect=DOCS_DIALECT)

    assert result.updated.startswith("> A note.\n\n# Project Guide")


def test_duplicate_sibling_headings_are_refused_with_their_line_numbers() -> None:
    """Refused, never resolved to the first. The message has to be actionable."""
    source = (DATA / "duplicates.md").read_text()

    with pytest.raises(AddressAmbiguous, match=r"appears 2 times at that level, on lines"):
        apply(source, [Op(OpKind.SET, "/# Top/## Repeated", value="x\n")], dialect=DOCS_DIALECT)


def test_a_missing_heading_lists_what_that_level_does_hold() -> None:
    with pytest.raises(AddressNotFound, match=r"it holds \['## Install'"):
        apply(
            GUIDE.read_text(),
            [Op(OpKind.SET, f"{ROOT}/## Nope", value="x\n")],
            dialect=DOCS_DIALECT,
        )


def test_an_address_segment_must_carry_its_own_markers() -> None:
    with pytest.raises(EditError, match="must carry its own markers"):
        apply(
            GUIDE.read_text(),
            [Op(OpKind.INSERT, f"{ROOT}/Plain", value="x\n")],
            dialect=DOCS_DIALECT,
        )


# ---------------------------------------------------------------------------
# Insert and delete
# ---------------------------------------------------------------------------


def test_a_new_section_is_inserted_at_the_end_of_its_parent() -> None:
    source = GUIDE.read_text()

    result = apply(
        source,
        [Op(OpKind.INSERT, f"{ROOT}/## Usage/### Timeouts", value="They are configurable.\n")],
        dialect=DOCS_DIALECT,
    )

    assert "### Timeouts\nThey are configurable.\n" in result.updated
    assert "## API" in result.updated


def test_an_inserted_section_gets_no_blank_line_because_that_would_edit_its_neighbour() -> None:
    """A blank line above a new heading lands inside the *previous* section's body.

    Step 6 caught this during implementation, which is the reason it is pinned here: the
    cosmetic improvement is an unrequested edit in a place the caller was not looking.
    """
    source = "# Top\n\n## A\n\nBody of A.\n"

    result = apply(
        source, [Op(OpKind.INSERT, "/# Top/## B", value="Body of B.\n")], dialect=DOCS_DIALECT
    )

    assert result.updated == "# Top\n\n## A\n\nBody of A.\n## B\nBody of B.\n"


def test_a_heading_no_deeper_than_its_parent_is_refused() -> None:
    with pytest.raises(EditError, match="not deeper than"):
        apply(
            GUIDE.read_text(),
            [Op(OpKind.INSERT, f"{ROOT}/## Usage/## Sibling", value="x\n")],
            dialect=DOCS_DIALECT,
        )


def test_inserting_into_a_file_without_a_trailing_newline_is_refused() -> None:
    """Adding the newline would alter the section above, which the caller did not ask for."""
    with pytest.raises(UnsupportedConstruct, match="does not end in a newline"):
        apply("# Top\n\n## A\n\nBody", [Op(OpKind.INSERT, "/# Top/## B", value="x\n")], dialect=DOCS_DIALECT)


def test_deleting_a_section_takes_its_children_with_it() -> None:
    source = GUIDE.read_text()

    result = apply(source, [Op(OpKind.DELETE, f"{ROOT}/## Usage")], dialect=DOCS_DIALECT)

    assert "## Usage" not in result.updated
    assert "### Flags" not in result.updated
    assert "## API" in result.updated
    assert "### Errors" in result.updated  # the one under ## API survives


def test_the_preamble_cannot_be_deleted() -> None:
    with pytest.raises(EditError, match="the preamble is not one"):
        apply(GUIDE.read_text(), [Op(OpKind.DELETE, "")], dialect=DOCS_DIALECT)


# ---------------------------------------------------------------------------
# The refusal boundary
# ---------------------------------------------------------------------------


def test_append_is_refused_because_markdown_has_no_sequences() -> None:
    with pytest.raises(UnsupportedConstruct, match="no sequences to append to"):
        apply(GUIDE.read_text(), [Op(OpKind.APPEND, ROOT, value="x")], dialect=DOCS_DIALECT)


def test_a_non_text_scalar_is_refused() -> None:
    with pytest.raises(EditError, match="markdown bodies are text"):
        apply(GUIDE.read_text(), [Op(OpKind.SET, ROOT, scalar=42)], dialect=DOCS_DIALECT)


def test_a_replacement_gains_the_newline_that_keeps_the_next_heading_a_heading() -> None:
    """The one byte this dialect adds on its own, and why."""
    source = "# Top\n\n## A\n\nBody.\n\n## B\n\nOther.\n"

    result = apply(source, [Op(OpKind.SET, "/# Top/## A", value="New")], dialect=DOCS_DIALECT)

    assert result.updated == "# Top\n\n## A\nNew\n## B\n\nOther.\n"


def test_the_result_says_it_was_checked_structurally() -> None:
    """Markdown has no semantic model to diff, and the caller is told so in a field."""
    result = apply(GUIDE.read_text(), [Op(OpKind.SET, ROOT, value="x\n")], dialect=DOCS_DIALECT)

    assert result.confidence is Confidence.STRUCTURAL


def test_round_trip_is_reported_as_not_applicable() -> None:
    assert DOCS_DIALECT.round_trip_ok("# x\n") is None
