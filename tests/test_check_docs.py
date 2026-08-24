"""Tests for the documentation checker, because it is a gate that fails open.

`scripts/check_docs.py` runs in CI and reports success by finding nothing. A regex that
stopped matching would therefore go on printing "docs ok" over a broken link forever —
the failure mode is silence, which is exactly the one no reviewer notices.

So each check is exercised against a planted fault: a link that does not resolve, a
flowchart edge to a node nothing defines, a module with no sibling doc. And each is
exercised against the shapes that *look* like faults and are not, because a checker
nobody trusts gets switched off.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_docs.py"


def load():
    """Import the script by path: `scripts/` is not a package and must not become one."""
    spec = importlib.util.spec_from_file_location("check_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_docs = load()


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def mermaid(body: str) -> str:
    return f"```mermaid\n{body}\n```\n"


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_a_link_to_a_missing_file_is_caught(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "See [the other one](b.md).")

    problems = check_docs.broken_links(tmp_path)

    assert len(problems) == 1
    assert "b.md" in problems[0]


def test_a_link_that_resolves_is_not_reported(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "See [the other one](sub/b.md).")
    write(tmp_path, "sub/b.md", "here")

    assert check_docs.broken_links(tmp_path) == []


def test_a_relative_link_is_resolved_from_its_own_file(tmp_path: Path) -> None:
    """The bug this prevents: resolving every link from the repository root.

    `src/python_acp/turns.md` links to `sessions.md`, which only exists beside it.
    """
    write(tmp_path, "deep/a.md", "See [sibling](b.md) and [up](../top.md).")
    write(tmp_path, "deep/b.md", "here")
    write(tmp_path, "top.md", "here")

    assert check_docs.broken_links(tmp_path) == []


@pytest.mark.parametrize(
    "target",
    ["https://example.com/x", "http://example.com", "mailto:a@b.c", "#a-heading"],
)
def test_what_is_not_a_file_is_not_checked(tmp_path: Path, target: str) -> None:
    write(tmp_path, "a.md", f"See [it]({target}).")

    assert check_docs.broken_links(tmp_path) == []


def test_an_anchor_on_a_real_file_is_fine(tmp_path: Path) -> None:
    """Anchors are not followed — heading slugs are GitHub's business, not ours."""
    write(tmp_path, "a.md", "See [it](b.md#some-heading).")
    write(tmp_path, "b.md", "here")

    assert check_docs.broken_links(tmp_path) == []


# ---------------------------------------------------------------------------
# Flowcharts
# ---------------------------------------------------------------------------


def test_an_edge_to_an_undefined_node_is_caught(tmp_path: Path) -> None:
    """GitHub renders this as a bare node, so the failure is a plausible wrong picture."""
    write(tmp_path, "a.md", mermaid('flowchart LR\n    A["one"] --> B\n'))

    problems = check_docs.flowchart_problems(tmp_path)

    assert len(problems) == 1
    assert "'B'" in problems[0]


def test_a_node_defined_twice_is_caught(tmp_path: Path) -> None:
    """The real one this was written for: ARCHITECTURE.md had two `Router` nodes."""
    write(tmp_path, "a.md", mermaid('flowchart LR\n    A["one"]\n    A["two"]\n    A --> A\n'))

    problems = check_docs.flowchart_problems(tmp_path)

    assert any("twice" in p for p in problems)


def test_a_node_defined_inline_on_an_edge_counts_as_defined(tmp_path: Path) -> None:
    """Mermaid allows `A --> B["label"]`, and calling that undefined would be a lie."""
    write(tmp_path, "a.md", mermaid('flowchart TD\n    A["one"] --> B["two"]\n    B --> A\n'))

    assert check_docs.flowchart_problems(tmp_path) == []


@pytest.mark.parametrize(
    "edge",
    [
        'A -- yes --> B',
        'A -. "on_close" .-> B',
        'A -.label.-> B',
        'A <--> B',
        'A ==> B',
        'A -.-> B',
    ],
)
def test_every_arrow_shape_splits_into_its_two_nodes(tmp_path: Path, edge: str) -> None:
    """Edge labels are not nodes. Reading `yes` as one is the false positive that would
    have made this check untrustworthy on the repo's own diagrams."""
    write(tmp_path, "a.md", mermaid(f'flowchart LR\n    A["one"]\n    B["two"]\n    {edge}\n'))

    assert check_docs.flowchart_problems(tmp_path) == []


def test_a_sequence_diagram_is_left_alone(tmp_path: Path) -> None:
    """Its participants are not flowchart nodes, and the grammar is different."""
    body = "sequenceDiagram\n    participant A as one\n    A->>B: hi"
    write(tmp_path, "a.md", mermaid(body))

    assert check_docs.flowchart_problems(tmp_path) == []


def test_subgraphs_and_styling_are_not_read_as_edges(tmp_path: Path) -> None:
    body = (
        'flowchart LR\n'
        '    subgraph outer["A group"]\n'
        '        A["one"]\n'
        '    end\n'
        '    B["two"]\n'
        '    A --> B\n'
        '    style A fill:#eee\n'
    )
    write(tmp_path, "a.md", mermaid(body))

    assert check_docs.flowchart_problems(tmp_path) == []


# ---------------------------------------------------------------------------
# The co-located doc rule
# ---------------------------------------------------------------------------


def test_a_module_with_no_sibling_doc_is_caught(tmp_path: Path) -> None:
    write(tmp_path, "src/python_acp/thing.py", "x = 1\n")

    problems = check_docs.colocated_docs(tmp_path)

    assert len(problems) == 1
    assert "thing.md" in problems[0]


def test_a_doc_with_no_module_is_caught(tmp_path: Path) -> None:
    """The half that catches a deleted module — `legacy_ws.md` outliving `legacy_ws.py`."""
    write(tmp_path, "src/python_acp/ghost.md", "# gone\n")

    problems = check_docs.colocated_docs(tmp_path)

    assert len(problems) == 1
    assert "orphan" in problems[0]


def test_init_is_exempt(tmp_path: Path) -> None:
    write(tmp_path, "src/python_acp/__init__.py", "x = 1\n")

    assert check_docs.colocated_docs(tmp_path) == []


# ---------------------------------------------------------------------------
# The repository itself
# ---------------------------------------------------------------------------


def test_this_repository_passes_its_own_check() -> None:
    """The gate, asserted from the suite as well as from CI.

    `make docs-check` is a separate CI step, but a developer who runs only `make test`
    should still find out before pushing.
    """
    root = SCRIPT.resolve().parent.parent

    assert check_docs.broken_links(root) == []
    assert check_docs.flowchart_problems(root) == []
    assert check_docs.colocated_docs(root) == []
