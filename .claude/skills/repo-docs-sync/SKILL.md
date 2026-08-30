---
name: repo-docs-sync
description: Use when adding, renaming, deleting, or materially changing any file under src/python_acp/, or when touching ARCHITECTURE.md or the README's client-facing surface. This repo requires a co-located Markdown doc with the same basename beside every production Python module, plus Mermaid diagrams — architecture, data flow, protocol ordering, code logic — that track real behaviour in ARCHITECTURE.md and in the module docs, and `make docs-check` enforces only the mechanical half. Trigger before finishing any change to production source or architecture docs.
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

## Mermaid diagrams

A module doc that is only prose forces the reader to rebuild the shape of the thing in
their head. Where a module *has* a shape — a path data takes, a wiring between
collaborators, a state machine, an ordering across processes — draw it. The diagram is
part of the doc contract, not decoration.

### Which diagram for which subject

Pick by what is actually being explained; the wrong form is worse than no picture.

| The thing you are explaining | Form | Live example |
| --- | --- | --- |
| **Architecture / wiring** — which module holds which, and what is a callback rather than a call | `flowchart LR` | the subsystem map in [ARCHITECTURE.md](../../../ARCHITECTURE.md) |
| **Data flow** — how one payload is classified or transformed hop by hop | `flowchart TD`, one node per transform, edges labelled with the *shape* in flight | [transport_ws.md](../../../src/python_acp/transport_ws.md): frame → decode → dispatch, with each error code as its own edge |
| **Protocol / ordering across processes** — who speaks when, and what overlaps | `sequenceDiagram` with `alt` / `opt` / `par` | [mcp_stdio.md](../../../src/python_acp/mcp_stdio.md) request/reply loop; the six in `ARCHITECTURE.md` |
| **Code logic** — a decision tree, a startup path, an escalation ladder | `flowchart TD` with `{"…?"}` decision nodes and labelled edges | [cli.md](../../../src/python_acp/cli.md) bootstrap; the `mcp_stdio.md` shutdown ladder (stdin → SIGTERM → SIGKILL) |
| **Lifecycle** — an object with named states and legal transitions | `stateDiagram-v2` | none yet; the shape a session or terminal registry doc wants |
| **Ownership / cardinality** — one-to-many between long-lived objects | `erDiagram`, sparingly | none yet |

Direction is part of the choice: **`LR` for wiring and pipelines** (wide, few branches),
**`TD` for decision trees** (a `{"…?"}` node with labelled outcomes reads top-down).

Rules of thumb:

- **One diagram answers one question.** Two diagrams of the same subject is how one goes
  stale — `pyacp-6ni.5` merged a "today" and a "target" section for exactly that reason.
- **A diagram that restates the prose earns nothing.** Draw what prose is bad at:
  branching, concurrency, ordering, fan-out.
- **Fewer than ~4 nodes is a sentence.** More than ~20 is two diagrams, or a `subgraph`.
- Diagrams describe **delivered behaviour**, never intent. Label a planned edge, or leave
  it out.

### House style

Follow what is already in `ARCHITECTURE.md` and `mcp_stdio.md`:

- Node id in PascalCase, label carrying the **filename and the role**:
  `Turns["turns.py<br/>TurnExecutor"]`. A reader should be able to jump from a box to a
  file without guessing.
- `<br/>` for a second line; `&rarr;` for an arrow inside a label (a literal `-->` in a
  label breaks the parse).
- **Solid edge = a direct call. Dotted edge = a callback, hook, or deferred path**, and
  label it: `Sessions -. "on_close" .-> Backends`.
- `[(…)]` for a process or external system (`MCPProc[("MCP server subprocess")]`).
- Quote any label containing parentheses, slashes, commas, or `#`.
- In a `sequenceDiagram`, name participants after the module (`participant Client as
  MCPStdioClient`), and use `Note over` for the invariant a reader cannot infer from the
  arrows ("read loop runs for the life of the subprocess").

### What `make docs-check` will and will not catch

`scripts/check_docs.py` validates **flowchart edges only** — every edge must name a node
its own block defines, and no node may be defined twice. That check exists because GitHub
renders a dangling edge as an invented bare node, so a drifted flowchart looks *plausible*
rather than broken.

Two traps follow from how the check is written:

- **Write `flowchart`, never `graph`.** The checker skips any block whose first line is
  not `flowchart…`, so `graph TD` renders identically on GitHub while silently opting out
  of validation.
- **Put spaces around every arrow.** The edge regex requires whitespace on both sides, so
  `A-->B` is invisible to the checker; write `A --> B`.

Nothing validates `sequenceDiagram`, `stateDiagram-v2`, or `erDiagram` — no node check, no
render. For those, and for label syntax in any block, paste the block into
<https://mermaid.live> before committing. `make docs-check` does not render Mermaid (that
needs node and a headless browser), so a green check means the wiring is intact, not that
the picture draws.

### When you change code

A diagram is as much a drift risk as a docstring. Redraw when the **shape** changes — a
new participant, a new branch, a changed hop, a new kind of request. Adding one more
method to an existing branch does not need a diagram edit.

## ARCHITECTURE.md

Holds one `flowchart LR` of subsystems and their wiring, plus a family of
`sequenceDiagram`s — the handshake, the request lifecycle, and one per hard case
(cancelling a turn, client-side `fs/*`, `terminal/*`, an MCP server asking the human a
question). They describe real behavior, not intent:

- the flowchart is the only diagram in the repo that names every module; it is the one
  that goes stale when a module is added or rewired
- the request-lifecycle `sequenceDiagram` carries the `alt` branch that splits an
  unusable frame from a well-formed one. (That branch used to split *action-based from
  JSON-RPC* requests; `pyacp-sld.3` deleted the action surface, so the only thing decided
  before the SDK sees a frame is whether it is usable at all.)

Update the sequence diagram whenever the dispatch path changes shape — a new
participant, a new branch, a changed hop. Adding one more method to an existing branch
does not need a diagram edit; adding a new *kind* of request does.

Mermaid blocks are rendered by GitHub. Check fences are ```` ```mermaid ```` and that
node labels with parentheses or slashes are quoted. See **Mermaid diagrams** above for
form, house style, and what `docs-check` does not validate.

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
