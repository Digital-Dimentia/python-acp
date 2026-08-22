---
name: repo-docs-sync
description: Use when adding, renaming, deleting, or materially changing any file under src/python_acp/, or when touching ARCHITECTURE.md or the README's WebSocket action list. This repo requires a co-located Markdown doc with the same basename beside every production Python module, plus Mermaid diagrams in ARCHITECTURE.md that track the real request path. Trigger before finishing any change to production source or architecture docs.
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

The "WebSocket actions" section lists an example JSON payload per supported action, and
the "Features" section lists the supported action names. Both drift the moment a method
is added. See the `acp-protocol` skill for the full add-a-method checklist.

## Checking before handoff

```bash
# every production module has a sibling doc
for f in src/python_acp/*.py; do
  case "$f" in */__init__.py) continue;; esac
  [ -f "${f%.py}.md" ] || echo "MISSING DOC: ${f%.py}.md"
done

# every doc still has a module
for f in src/python_acp/*.md; do
  [ -f "${f%.md}.py" ] || echo "ORPHAN DOC: $f"
done
```

## Note on git state

`ARCHITECTURE.md`, `docs/`, and the three module `.md` files were untracked as of the
last check. If a doc edit seems to vanish from a diff, confirm the file is actually
tracked before assuming the edit failed.
