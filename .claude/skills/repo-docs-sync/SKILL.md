---
name: repo-docs-sync
description: Use when adding, renaming, deleting, or materially changing any file under src/python_acp/, or when touching ARCHITECTURE.md or the README's client-facing surface. This repo requires a co-located Markdown doc with the same basename beside every production Python module, plus Mermaid diagrams in ARCHITECTURE.md that track the real request path, and `make docs-check` enforces the mechanical half. Trigger before finishing any change to production source or architecture docs.
---

# Documentation Invariants

This repo carries a documentation contract that nothing in the code enforces and no
linter checks. It is easy to violate silently.

## The co-located doc rule

**Every production `.py` under `src/python_acp/` has a sibling `.md` with the same
basename.** Established by `docs/full-apc-plan.md` step 8.3.

```
src/python_acp/cli.py          ↔ src/python_acp/cli.md
src/python_acp/mcp_stdio.py    ↔ src/python_acp/mcp_stdio.md
src/python_acp/transport_ws.py ↔ src/python_acp/transport_ws.md
```

Consequences:

- **New module** → create its `.md` in the same commit, link it from both the
  "Module Documentation" list in `ARCHITECTURE.md` and the "Architecture docs" list in
  `README.md`, and run `make stats`. Two lists and one generated file.
- **Renamed module** → rename the `.md` to match, fix both lists, `make stats`.
- **Deleted module** → delete the `.md`, remove both list entries, `make stats`.
- **Changed public surface** → update the existing `.md`.

`__init__.py` is exempt. Files under `tests/` are exempt.

## ARCHITECTURE.md

Holds two Mermaid diagrams that describe real behavior, not intent:

- a `flowchart LR` of subsystems and their wiring
- a `sequenceDiagram` of the request lifecycle, including the `alt` branch that splits an
  unusable frame from a well-formed one. (That branch used to split *action-based from
  JSON-RPC* requests; `pyacp-sld.3` deleted the action surface, so the only thing decided
  before the SDK sees a frame is whether it is usable at all.)

Update the sequence diagram whenever the dispatch path changes shape — a new
participant, a new branch, a changed hop. Adding one more method to an existing branch
does not need a diagram edit; adding a new *kind* of request does.

Mermaid blocks are rendered by GitHub. Check fences are ```` ```mermaid ```` and that
node labels with parentheses or slashes are quoted.

## README.md

The "Features" list and the worked request examples under "Run the bridge" both drift the
moment the client-facing surface changes. See the `acp-protocol` skill for the full
add-a-method checklist.

(This section used to describe a "WebSocket actions" section with one payload example per
deprecated action. `pyacp-sld.3` deleted both the surface and the section.)

## STATISTICS.md is generated — never hand-edit it

[STATISTICS.md](../../../STATISTICS.md) at the repo root records lines, modules, classes,
functions, and tests. **`make stats` writes it** from
[scripts/code_stats.py](../../../scripts/code_stats.py); anything typed into it by hand is
lost the next time anyone runs that.

Counting is done on the **AST**, so a `def` inside a docstring is not a function and a `#`
inside a string is not a comment. That is not fussiness in this repo — prose outweighs code
in several modules, so grep-based counts are wrong by a wide margin rather than merely
imprecise.

**Run `make stats` whenever the module list changes** — a module added, renamed, or
deleted. That is the case the document cannot survive, and `tests/test_code_stats.py`
asserts the module table is complete, so forgetting fails the suite rather than shipping a
table that reads as authoritative.

`make stats-check` reports staleness without writing. It is deliberately **not** in
`make lint && make docs-check && make test`: gating a build on a line count would fail
every commit that adds a line, and a check that cries wolf is one people learn to satisfy
without reading. The line numbers are allowed to lag; the module list is not.

The document stamps the commit it was generated from, so a reader can date what they are
looking at rather than assuming it is current.

## Checking before handoff

```bash
make docs-check
```

`pyacp-6ni.5` turned the hand-rolled loop that used to live here into
[scripts/check_docs.py](../../../scripts/check_docs.py), which runs in CI and in
`tests/test_check_docs.py`. It enforces three things:

1. **Every relative Markdown link resolves.** Links are resolved from the linking file,
   not the repo root, so `turns.md` → `sessions.md` works.
2. **Every Mermaid *flowchart* edge names a node its own block defines**, and no node is
   defined twice. This is the one worth having: GitHub renders a dangling edge by
   inventing a bare node, so a drifted diagram looks *plausible* instead of broken.
3. **The co-located doc rule** — a sibling `.md` for every module, no orphans.

**It does not check everything, and the gaps are deliberate.** It does not render Mermaid
(that needs node and a headless browser), it does not follow `#anchors` (heading slugs are
GitHub's business and pinning them would fail on every rename), and it does not verify
that a doc's prose is *true* — including that the symbols it names still exist. A green
`docs-check` means the wiring is intact, not that the writing is right. Read the doc.

## What a green check still will not tell you

The audits worth running by hand when a module's surface changes, because no script does
them:

- Does the sibling `.md`'s **Main symbols** table still name what the module exports, and
  only that? A renamed class leaves a table entry that reads as authoritative.
- Does a doc cite a test that has been renamed? Beware: some citations are deliberately
  historical (`capabilities.md` names a test that "died with the first flip, as
  intended"), so this cannot be automated without false positives.
- Do `ARCHITECTURE.md`'s diagrams still describe the *delivered* system rather than a
  planned one? Two diagrams of the same thing is how one goes stale — `pyacp-6ni.5` merged
  a "today" and a "target" section for exactly that reason.
