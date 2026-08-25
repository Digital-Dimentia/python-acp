#!/usr/bin/env python3
"""Check the documentation invariants nothing else enforces.

Three of them, and none is caught by ruff, pytest, or a human skimming a diff:

1. **Every relative Markdown link resolves.** A doc that links to a module doc that was
   renamed or deleted reads as authoritative and is wrong, which is worse than silence.
2. **Every Mermaid flowchart edge names a node the same block defines**, and no node is
   defined twice. GitHub renders a diagram with a dangling edge by inventing a bare node,
   so the failure is a *plausible-looking picture*, not an error — the worst kind.
3. **Every production module under `src/python_acp/` has a sibling `.md`, and every
   sibling `.md` has a module.** The repo's co-located doc rule (`docs/full-apc-plan.md`
   step 8.3), which until now lived only in the `repo-docs-sync` skill.

Exits non-zero with a list. Run it from the repository root: `make docs-check`.

**What it deliberately does not do.** It does not render Mermaid — that needs node and a
headless browser, which is a large dependency for a small check — and it does not follow
`#anchors`, because heading slugs are a GitHub implementation detail and pinning them
would produce false failures on every heading rename. See `pyacp-6ni.5`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Directory names that are never ours, matched exactly against a path component.
SKIP_PARTS = (
    ".git", "node_modules", "dist", "artifacts",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
)

#: Directory name *suffixes* that are never ours.
#:
#: `egg-info` used to sit in `SKIP_PARTS`, where it could never match anything: build
#: metadata is always named `<project>.egg-info`, so the exact comparison was against a
#: string no directory is ever called. The same mistake as the `.venv` one below, in the
#: same tuple, found by the test that was written to pin the first.
SKIP_SUFFIXES = (".egg-info",)

#: Directory name *prefixes* that are never ours.
#:
#: **A prefix, because an exact match on `.venv` was a bug** (`pyacp-bdc`). `.gitignore`
#: line 155 and the Makefile's `VENV_DIR` override document matrix-leg environments named
#: `.venv311` and `.venv312`, and none of those equal `.venv` — so this walker used to
#: descend into an entire installed site-packages tree. Cosmetic for `code_stats.py`,
#: which merely counted vendored Markdown as ours; not cosmetic here, where it meant
#: validating relative links inside a *dependency's* files and failing `make docs-check`
#: for a file nobody in this repository wrote.
SKIP_PREFIXES = (".venv", "venv")


def is_ignored(path: Path) -> bool:
    """Whether `path` lies inside something that is not this project's source.

    Shared with `code_stats.py`, which imports it rather than keeping a second copy —
    two lists of directories to skip is exactly how one of them ends up missing an entry.
    """
    return any(
        part in SKIP_PARTS or part.startswith(SKIP_PREFIXES) or part.endswith(SKIP_SUFFIXES)
        for part in path.parts
    )

MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)
LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
#: An arrow, with or without an `-- label -->` or `-. label .->` in the middle.
ARROW = re.compile(
    r"\s(?:"
    r"-{2,3}\s[^->]*?\s-{2,3}>"      # -- label -->
    r"|-\.\s[^.]*?\s\.->"            # -. label .->
    r"|<?-\.->|<?-{2,3}>|-\.-"       # -.-> --> <--> -.-
    r"|<?={2,3}>"                    # ==>
    r")\s"
)
#: A node reference: an id, optionally carrying a shape and label.
NODE = re.compile(r"^([A-Za-z_][\w]*)(?:[\[\(\{].*)?$")
STRUCTURAL = ("subgraph", "end", "style", "classDef", "class ", "click", "linkStyle", "direction")


def markdown_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if not is_ignored(p))


def broken_links(root: Path) -> list[str]:
    problems = []
    for path in markdown_files(root):
        for text, target in LINK.findall(path.read_text()):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative, _, _anchor = target.partition("#")
            if not relative:
                continue
            if not (path.parent / relative).exists():
                problems.append(f"{path}: broken link [{text}]({target})")
    return problems


def flowchart_problems(root: Path) -> list[str]:
    problems = []
    for path in markdown_files(root):
        for index, source in enumerate(MERMAID_BLOCK.findall(path.read_text()), 1):
            lines = source.strip().split("\n")
            if not lines[0].startswith("flowchart"):
                continue
            defined: set[str] = set()
            referenced: set[str] = set()
            duplicated: list[str] = []
            for raw in lines[1:]:
                line = raw.split("%%")[0].strip()
                if not line or line.startswith(STRUCTURAL):
                    continue
                for part in (p.strip() for p in ARROW.split(line) if p and p.strip()):
                    part = re.sub(r"^\|.*?\|\s*", "", part).strip()
                    match = NODE.match(part)
                    if match is None:
                        continue
                    node = match.group(1)
                    if re.match(r"^[A-Za-z_][\w]*[\[\(\{]", part):
                        if node in defined:
                            duplicated.append(node)
                        defined.add(node)
                    else:
                        referenced.add(node)
            for node in sorted(referenced - defined):
                problems.append(f"{path}: flowchart {index} has an edge to undefined {node!r}")
            for node in sorted(set(duplicated)):
                problems.append(f"{path}: flowchart {index} defines {node!r} twice")
    return problems


def colocated_docs(root: Path) -> list[str]:
    package = root / "src" / "python_acp"
    problems = []
    for module in sorted(package.glob("*.py")):
        if module.name == "__init__.py":
            continue
        if not module.with_suffix(".md").exists():
            problems.append(f"{module}: no sibling {module.with_suffix('.md').name}")
    for doc in sorted(package.glob("*.md")):
        if not doc.with_suffix(".py").exists():
            problems.append(f"{doc}: orphan doc, no sibling module")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    problems = broken_links(root) + flowchart_problems(root) + colocated_docs(root)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print(f"\n{len(problems)} documentation problem(s)", file=sys.stderr)
        return 1
    print(f"docs ok: {len(markdown_files(root))} markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
