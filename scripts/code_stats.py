#!/usr/bin/env python3
"""Count what is in this repository, and write `STATISTICS.md` from the answer.

**The document is generated, never hand-edited.** Numbers written by hand are wrong the
day after they are written, and a stale statistic reads exactly like a fresh one — the
same failure mode `check_docs.py` exists for. `make stats` rewrites the file; the header
it emits says so, and stamps the commit it was generated from so a reader can see the age
of what they are looking at.

## Counting is done on the AST, not with grep

A `def` inside a docstring is not a function, a `#` inside a string is not a comment, and
this repository has an unusually high ratio of prose to code — so the naive counts are not
merely imprecise here, they are wrong by a wide margin. Every structural number comes from
`ast`, and comments come from `tokenize`.

Line classification is one label per line, in priority order: blank, then comment, then
docstring, then code. A line can only be one thing, so the four always sum to the total.

## What this deliberately does not do

It does not gate CI on the numbers. A test asserting the committed line count would fail
on every commit that adds a line, which is a tax rather than a guard — the noise would
train everyone to regenerate without reading, which is how a wrong number gets committed
in the first place.

What *is* worth checking is the invariant that can rot silently: that the module table
lists every production module and no others. `tests/test_code_stats.py` asserts that, and
`--check` reports staleness without writing, so a future CI gate needs no new code here.
"""

from __future__ import annotations

import argparse
import ast
import io
import subprocess
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_docs import is_ignored  # noqa: E402  — sibling script, not an installed package

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "STATISTICS.md"

#: The groups the report is broken down by, in the order they appear.
GROUPS: tuple[tuple[str, str], ...] = (
    ("src/python_acp", "Production source"),
    ("tests", "Tests"),
    ("scripts", "Build and check scripts"),
)


@dataclass
class FileStats:
    """One Python file, counted."""

    path: Path
    total: int = 0
    blank: int = 0
    comment: int = 0
    docstring: int = 0
    code: int = 0
    classes: int = 0
    functions: int = 0
    async_functions: int = 0
    methods: int = 0
    test_functions: int = 0

    def merge(self, other: FileStats) -> None:
        for name in (
            "total", "blank", "comment", "docstring", "code",
            "classes", "functions", "async_functions", "methods", "test_functions",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class Group:
    """One reported section: a directory, its files, and their totals."""

    label: str
    directory: str
    files: list[FileStats] = field(default_factory=list)
    totals: FileStats = field(default_factory=lambda: FileStats(path=Path(".")))


def classify_lines(source: str) -> Counter[str]:
    """Label every line blank / comment / docstring / code, exactly once.

    Comments come from `tokenize` rather than a `startswith("#")` scan, so a `#` inside a
    string literal is not miscounted; only a line whose *first* token is a comment counts,
    so trailing comments stay with the code they annotate.
    """
    lines = source.splitlines()
    kind = ["code"] * len(lines)
    for index, line in enumerate(lines):
        if not line.strip():
            kind[index] = "blank"

    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                row = token.start[0] - 1
                if lines[row].lstrip().startswith("#"):
                    kind[row] = "comment"
    except (tokenize.TokenError, IndentationError):
        # A file that will not tokenize still gets blank/code counts rather than nothing.
        pass

    for node in _docstring_nodes(source):
        for row in range(node.lineno - 1, (node.end_lineno or node.lineno)):
            if 0 <= row < len(kind) and kind[row] == "code":
                kind[row] = "docstring"

    return Counter(kind)


def _docstring_nodes(source: str) -> list[ast.Expr]:
    """Every bare string expression — module, class, function docstrings, and the
    free-standing strings this repo uses as `#:`-adjacent commentary."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]


def analyse(path: Path) -> FileStats:
    """Count one file. Structure from the AST, lines from `classify_lines`."""
    source = path.read_text(encoding="utf-8")
    counts = classify_lines(source)
    stats = FileStats(
        path=path,
        total=len(source.splitlines()),
        blank=counts["blank"],
        comment=counts["comment"],
        docstring=counts["docstring"],
        code=counts["code"],
    )

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return stats

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            stats.classes += 1
            stats.methods += sum(
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) for child in node.body
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            stats.functions += 1
            if isinstance(node, ast.AsyncFunctionDef):
                stats.async_functions += 1
            if node.name.startswith("test_"):
                stats.test_functions += 1
    return stats


def python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if not is_ignored(path))


def collect(root: Path = REPO_ROOT) -> list[Group]:
    groups: list[Group] = []
    for directory, label in GROUPS:
        group = Group(label=label, directory=directory)
        for path in python_files(root / directory):
            stats = analyse(path)
            group.files.append(stats)
            group.totals.merge(stats)
        groups.append(group)
    return groups


def markdown_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every Markdown file **except the one being written**.

    `STATISTICS.md` is excluded because its own length is part of its own content: count
    it, and writing the file changes the number that goes in the file. That is a fixed
    point, not a count, and it would make `--check` report a freshly generated document as
    stale. Excluding it is stable and the report says so rather than quietly being off by
    one file.
    """
    return sorted(
        path for path in root.rglob("*.md") if not is_ignored(path) and path != OUTPUT
    )


def _line_count(path: Path) -> int:
    """Lines in a file, or 0 for one that does not exist yet — which `STATISTICS.md`
    itself does not, the first time this runs."""
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _commit() -> str:
    """The commit the numbers describe, so a reader can date them.

    Falls back to `unknown` outside a checkout rather than failing: a statistics document
    is not worth breaking a build over.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%h %cs"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


#: Prefix of the line naming the commit the numbers were counted at. Comparisons ignore
#: that line — see `without_stamp`.
STAMP_PREFIX = "Counted at commit"


def without_stamp(document: str) -> str:
    """The document with the commit stamp blanked, for comparing two generations.

    **The stamp is a fixed point and must be excluded from any staleness check.** The
    document names the commit it was generated at; committing the document *is* a new
    commit, so the file is stale against its own stamp the instant it lands. Regenerating
    does not converge — it stamps the new HEAD, which committing changes again, forever.

    The line count had the same shape and was solved by exclusion (see `markdown_files`).
    This is the same answer one level up: the stamp stays, because a reader needs to know
    how old the numbers are, and it is ignored when asking whether the *numbers* moved.
    """
    return "\n".join(
        "" if line.startswith(STAMP_PREFIX) else line for line in document.splitlines()
    )


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render(root: Path = REPO_ROOT) -> str:
    """The whole document, as a string. Pure — writing it is `main`'s job."""
    groups = collect(root)
    overall = FileStats(path=Path("."))
    for group in groups:
        overall.merge(group.totals)

    docs = markdown_files(root)
    doc_lines = sum(_line_count(path) for path in docs)
    module_docs = [path for path in docs if path.parent == root / "src/python_acp"]
    module_doc_lines = sum(_line_count(path) for path in module_docs)
    production = next(group for group in groups if group.directory == "src/python_acp")
    tests = next(group for group in groups if group.directory == "tests")

    out: list[str] = [
        "# Statistics",
        "",
        "**Generated — do not edit by hand.** `make stats` rewrites this file from",
        "[scripts/code_stats.py](scripts/code_stats.py); an edit here is lost the next time",
        "anyone runs it.",
        "",
        f"{STAMP_PREFIX} **{_commit()}**. These are a snapshot and go out of date with the",
        "next commit, which is why the commit is stamped rather than the date alone. The",
        "stamp is ignored when checking whether the numbers are current — it names the",
        "commit *before* the one that committed this file, and always will.",
        "",
        "Counting is done on the **AST**, not with `grep`: a `def` inside a docstring is not",
        "a function and a `#` inside a string is not a comment. That distinction is not",
        "pedantic here — prose outweighs code in several modules, so the naive counts are",
        "wrong by a wide margin rather than merely imprecise.",
        "",
        "Every line carries exactly one label, in priority order: blank, then comment, then",
        "docstring, then code. The four always sum to the total — and because blank wins,",
        "an empty line *inside* a docstring counts as blank, just as one inside a function",
        "does. Docstring totals below are therefore lower than a naive span count.",
        "",
        "## Totals",
        "",
        _row(["Group", "Files", "Lines", "Code", "Docstring", "Comment", "Blank",
              "Classes", "Functions", "async", "Test fns"]),
        _row(["---"] + ["---:"] * 10),
    ]

    for group in groups:
        totals = group.totals
        out.append(_row([
            f"`{group.directory}`",
            f"{len(group.files):,}", f"{totals.total:,}", f"{totals.code:,}",
            f"{totals.docstring:,}", f"{totals.comment:,}", f"{totals.blank:,}",
            f"{totals.classes:,}", f"{totals.functions:,}",
            f"{totals.async_functions:,}", f"{totals.test_functions:,}",
        ]))

    total_files = sum(len(group.files) for group in groups)
    out.append(_row([
        "**Total**",
        f"**{total_files:,}**", f"**{overall.total:,}**", f"**{overall.code:,}**",
        f"**{overall.docstring:,}**", f"**{overall.comment:,}**", f"**{overall.blank:,}**",
        f"**{overall.classes:,}**", f"**{overall.functions:,}**",
        f"**{overall.async_functions:,}**", f"**{overall.test_functions:,}**",
    ]))

    prose = production.totals.docstring + production.totals.comment
    prose_share = prose / production.totals.total * 100 if production.totals.total else 0.0
    test_ratio = tests.totals.code / production.totals.code if production.totals.code else 0.0

    out += [
        "",
        "## Ratios worth knowing",
        "",
        _row(["Measure", "Value", "What it means"]),
        _row(["---", "---:", "---"]),
        _row([
            "Test code to production code",
            f"{test_ratio:.1f} : 1",
            f"{tests.totals.code:,} lines of test code against {production.totals.code:,} "
            "of production code",
        ]),
        _row([
            "Prose share of production source",
            f"{prose_share:.0f}%",
            f"{production.totals.docstring:,} docstring + {production.totals.comment:,} "
            "comment lines. The repo documents decisions, not descriptions, and it shows "
            "up as mass",
        ]),
        _row([
            "Co-located module docs",
            f"{module_doc_lines:,} lines",
            f"{len(module_docs)} files beside the {len(production.files) - 1} modules that "
            "need one — `__init__.py` is exempt. The rule `check_docs.py` enforces",
        ]),
        _row([
            "Markdown across the repo",
            f"{doc_lines:,} lines",
            f"{len(docs)} files, module docs included and this one excluded — its own "
            "length would otherwise be part of its own content",
        ]),
        "",
        "**Test functions are not test cases.** The table counts `def test_*`; pytest",
        "collects more, because `@pytest.mark.parametrize` expands one function into many.",
        "Run `make test` for the number that matters to CI.",
        "",
        "## Production modules",
        "",
        "Every module here has a sibling `.md` — the co-located doc rule. Where the doc is",
        "longer than the module, that is usually deliberate.",
        "",
        _row(["Module", "Lines", "Code", "Classes", "Functions", "Sibling doc"]),
        _row(["---"] + ["---:"] * 5),
    ]

    for stats in sorted(production.files, key=lambda item: -item.total):
        sibling = stats.path.with_suffix(".md")
        doc = f"{_line_count(sibling):,}" if sibling.is_file() else "—"
        out.append(_row([
            f"[`{stats.path.name}`](src/python_acp/{stats.path.name})",
            f"{stats.total:,}", f"{stats.code:,}",
            f"{stats.classes:,}", f"{stats.functions:,}", doc,
        ]))

    out += [
        "",
        "## Documentation",
        "",
        "Every Markdown file in the repository, this one excepted. Prose is a deliverable",
        "here rather than a by-product, so it is counted per file and not only in total.",
        "",
        _row(["File", "Lines"]),
        _row(["---", "---:"]),
    ]
    for path in sorted(docs, key=lambda item: -_line_count(item)):
        relative = path.relative_to(root).as_posix()
        out.append(_row([f"[`{relative}`]({relative})", f"{_line_count(path):,}"]))
    out.append(_row([f"**{len(docs)} files**", f"**{doc_lines:,}**"]))

    out += [
        "",
        "## Test modules",
        "",
        _row(["Module", "Lines", "Test functions"]),
        _row(["---"] + ["---:"] * 2),
    ]
    for stats in sorted(tests.files, key=lambda item: -item.test_functions):
        if not stats.test_functions:
            continue
        out.append(_row([
            f"[`{stats.path.name}`](tests/{stats.path.name})",
            f"{stats.total:,}", f"{stats.test_functions:,}",
        ]))

    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write STATISTICS.md from the source tree.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether STATISTICS.md is current without writing it. Exits 1 if not.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        # So a test can exercise the writing path without dirtying the working tree. It
        # did: the first version of `tests/test_code_stats.py` ran this script with no
        # flag, and `make test` left STATISTICS.md modified every time it passed.
        help="Where to write. Defaults to STATISTICS.md at the repository root.",
    )
    args = parser.parse_args(argv)

    rendered = render()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if without_stamp(current) == without_stamp(rendered):
            print(f"{args.output.name} is current")
            return 0
        print(f"{args.output.name} is out of date — run `make stats`", file=sys.stderr)
        return 1

    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
