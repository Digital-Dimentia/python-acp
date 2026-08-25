"""Tests for the `STATISTICS.md` generator.

Two different things need proving, and only one of them is about this repository.

**The counter** is tested against a fixture with hand-known contents, written inline. A
test that counted the real tree would assert a number that changes with every commit,
which is a test that has to be edited to stay green — the kind everyone learns to update
without reading. The fixture cannot drift, so an assertion about it stays meaningful.

**The document** is checked for the one property that can rot silently: that its module
table names every production module and no others. Line counts are deliberately *not*
gated — see `scripts/code_stats.py` for why a build that fails over a changed line count
is a tax rather than a guard.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from code_stats import (  # noqa: E402
    OUTPUT,
    REPO_ROOT,
    analyse,
    classify_lines,
    markdown_files,
    render,
)

SAMPLE = '''"""Module docstring.

Second line of it.
"""

# A real comment.
import os  # a trailing comment, which stays code

VALUE = os.sep


class Widget:
    """Class docstring."""

    def method(self) -> str:
        return "# not a comment"

    async def async_method(self) -> None:
        """Doc."""


def free_function() -> None:
    pass


async def test_something() -> None:
    pass
'''


def test_every_line_carries_exactly_one_label() -> None:
    """The four labels partition the file, which is what lets the report's columns sum
    to its total. If they ever stop summing, a column is double-counting."""
    counts = classify_lines(SAMPLE)
    assert sum(counts.values()) == len(SAMPLE.splitlines())


def test_a_docstring_is_not_code_and_a_string_hash_is_not_a_comment(tmp_path: Path) -> None:
    """The distinction the AST buys over `grep`.

    `return "# not a comment"` is code, and the four-line module docstring is not.
    """
    path = tmp_path / "sample.py"
    path.write_text(SAMPLE, encoding="utf-8")
    stats = analyse(path)

    # Five, not seven. The module docstring spans four lines but one of them is empty,
    # and **blank wins over docstring** in the priority order — an empty line inside a
    # docstring is counted as blank, exactly like an empty line inside a function. Add
    # the class docstring and `async_method`'s, one line each, and it comes to five.
    assert stats.docstring == 5
    assert stats.blank == SAMPLE.splitlines().count("")

    # Only the standalone `#` line. The trailing comment stays with its code, and the
    # `#` inside the returned string is not a comment at all.
    assert stats.comment == 1


def test_a_def_inside_a_docstring_is_not_a_function(tmp_path: Path) -> None:
    """The failure that motivated using the AST: this file has one function by grep's
    reckoning and none by Python's."""
    path = tmp_path / "prose.py"
    path.write_text('"""Docs.\n\ndef looks_like_a_function() -> None: ...\n"""\n', "utf-8")
    stats = analyse(path)
    assert stats.functions == 0
    assert stats.classes == 0


def test_structure_is_counted_from_the_tree(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text(SAMPLE, encoding="utf-8")
    stats = analyse(path)
    assert stats.classes == 1
    # method, async_method, free_function, test_something — methods included.
    assert stats.functions == 4
    assert stats.async_functions == 2
    assert stats.methods == 2
    assert stats.test_functions == 1


def test_a_file_that_will_not_parse_still_reports_lines(tmp_path: Path) -> None:
    """A syntax error must not take the whole report down — the line counts are still
    true, and the structural ones are honestly zero."""
    path = tmp_path / "broken.py"
    path.write_text("def (:\n    oops\n", encoding="utf-8")
    stats = analyse(path)
    assert stats.total == 2
    assert stats.functions == 0


# ----------------------------------------------------------------------
# The document
# ----------------------------------------------------------------------


def test_the_document_is_current() -> None:
    """`make stats` was run after the last change that moved a number.

    This is the one staleness assertion, and it is cheap to satisfy: the message says the
    command. It is here rather than in `check_docs.py` because it is about a generated
    file, not about the documentation invariants that script owns.
    """
    assert OUTPUT.is_file(), "STATISTICS.md is missing — run `make stats`"
    assert OUTPUT.read_text(encoding="utf-8") == render(), (
        "STATISTICS.md is out of date — run `make stats` and commit the result"
    )


def test_the_module_table_names_every_production_module() -> None:
    """The invariant that can rot silently.

    A module added without regenerating leaves a table that reads as complete and is not
    — the same failure the co-located doc rule exists to prevent, which is why this is
    asserted rather than left to whoever remembers.
    """
    document = OUTPUT.read_text(encoding="utf-8")
    modules = {
        path.name
        for path in (REPO_ROOT / "src/python_acp").glob("*.py")
    }
    listed = {name for name in modules if f"[`{name}`]" in document}
    assert listed == modules, f"not in STATISTICS.md: {sorted(modules - listed)}"


def test_the_document_excludes_itself_from_the_markdown_count() -> None:
    """Its own length is part of its own content, so counting it is a fixed point rather
    than a count — and `--check` would call a freshly written file stale."""
    assert OUTPUT not in markdown_files()


def test_generating_twice_produces_the_same_bytes() -> None:
    """Idempotence, which `--check` depends on entirely."""
    assert render() == render()


@pytest.mark.parametrize("flag", [[], ["--check"]])
def test_the_script_runs_as_a_command(flag: list[str]) -> None:
    """It is invoked by `make`, not imported, so the entry point is exercised too."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "code_stats.py"), *flag],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
