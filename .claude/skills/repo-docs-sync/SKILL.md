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
src/python_acp/cli.py        ↔ src/python_acp/cli.md
src/python_acp/mcp_stdio.py  ↔ src/python_acp/mcp_stdio.md
src/python_acp/ws_bridge.py  ↔ src/python_acp/ws_bridge.md
```

Consequences:

- **New module** → create its `.md` in the same commit, and link it from both the
  "Module Documentation" list in `ARCHITECTURE.md` and the "Architecture docs" list in
  `README.md`. Two lists, both need the entry.
- **Renamed module** → rename the `.md` to match and fix both lists.
- **Deleted module** → delete the `.md` and remove both list entries.
- **Changed public surface** → update the existing `.md`.

`__init__.py` is exempt. Files under `tests/` are exempt.

## ARCHITECTURE.md

Holds two Mermaid diagrams that describe real behavior, not intent:

- a `flowchart LR` of subsystems and their wiring
- a `sequenceDiagram` of the request lifecycle, including the `alt` branch that splits
  action-based from JSON-RPC requests

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
