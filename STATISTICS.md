# Statistics

**Generated — do not edit by hand.** `make stats` rewrites this file from
[scripts/code_stats.py](scripts/code_stats.py); an edit here is lost the next time
anyone runs it.

Counted at commit **dd8bc96 2026-08-25**. These are a snapshot and go out of date with the
next commit, which is why the commit is stamped rather than the date alone. The
stamp is ignored when checking whether the numbers are current — it names the
commit *before* the one that committed this file, and always will.

Counting is done on the **AST**, not with `grep`: a `def` inside a docstring is not
a function and a `#` inside a string is not a comment. That distinction is not
pedantic here — prose outweighs code in several modules, so the naive counts are
wrong by a wide margin rather than merely imprecise.

Every line carries exactly one label, in priority order: blank, then comment, then
docstring, then code. The four always sum to the total — and because blank wins,
an empty line *inside* a docstring counts as blank, just as one inside a function
does. Docstring totals below are therefore lower than a naive span count.

## Totals

| Group | Files | Lines | Code | Docstring | Comment | Blank | Classes | Functions | async | Test fns |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `src/python_acp` | 18 | 7,578 | 3,673 | 2,100 | 645 | 1,160 | 45 | 267 | 105 | 0 |
| `tests` | 29 | 14,370 | 8,604 | 1,663 | 714 | 3,389 | 45 | 951 | 551 | 702 |
| `scripts` | 3 | 956 | 627 | 145 | 37 | 147 | 4 | 31 | 0 | 0 |
| **Total** | **50** | **22,904** | **12,904** | **3,908** | **1,396** | **4,696** | **94** | **1,249** | **656** | **702** |

## Ratios worth knowing

| Measure | Value | What it means |
| --- | ---: | --- |
| Test code to production code | 2.3 : 1 | 8,604 lines of test code against 3,673 of production code |
| Prose share of production source | 36% | 2,100 docstring + 645 comment lines. The repo documents decisions, not descriptions, and it shows up as mass |
| Co-located module docs | 3,750 lines | 17 files beside the 17 modules that need one — `__init__.py` is exempt. The rule `check_docs.py` enforces |
| Markdown across the repo | 7,808 lines | 32 files, module docs included and this one excluded — its own length would otherwise be part of its own content |

**Test functions are not test cases.** The table counts `def test_*`; pytest
collects more, because `@pytest.mark.parametrize` expands one function into many.
Run `make test` for the number that matters to CI.

## Production modules

Every module here has a sibling `.md` — the co-located doc rule. Where the doc is
longer than the module, that is usually deliberate.

| Module | Lines | Code | Classes | Functions | Sibling doc |
| --- | ---: | ---: | ---: | ---: | ---: |
| [`turn_mcp_router.py`](src/python_acp/turn_mcp_router.py) | 1,613 | 879 | 10 | 42 | 536 |
| [`mcp_stdio.py`](src/python_acp/mcp_stdio.py) | 855 | 459 | 5 | 41 | 515 |
| [`agent.py`](src/python_acp/agent.py) | 795 | 365 | 1 | 31 | 381 |
| [`turns.py`](src/python_acp/turns.py) | 635 | 284 | 11 | 19 | 303 |
| [`sessions.py`](src/python_acp/sessions.py) | 571 | 260 | 4 | 32 | 217 |
| [`transport_ws.py`](src/python_acp/transport_ws.py) | 453 | 202 | 3 | 19 | 308 |
| [`terminals.py`](src/python_acp/terminals.py) | 419 | 192 | 2 | 18 | 172 |
| [`commands.py`](src/python_acp/commands.py) | 382 | 251 | 3 | 10 | 111 |
| [`mcp_registry.py`](src/python_acp/mcp_registry.py) | 353 | 145 | 2 | 16 | 162 |
| [`capabilities.py`](src/python_acp/capabilities.py) | 348 | 204 | 1 | 5 | 173 |
| [`elicitation.py`](src/python_acp/elicitation.py) | 242 | 98 | 1 | 4 | 137 |
| [`mcp_content.py`](src/python_acp/mcp_content.py) | 178 | 80 | 0 | 7 | 88 |
| [`cli.py`](src/python_acp/cli.py) | 166 | 90 | 0 | 5 | 187 |
| [`errors.py`](src/python_acp/errors.py) | 163 | 49 | 0 | 7 | 137 |
| [`mcp_tools.py`](src/python_acp/mcp_tools.py) | 159 | 37 | 1 | 4 | 123 |
| [`paths.py`](src/python_acp/paths.py) | 148 | 49 | 1 | 5 | 116 |
| [`transport_stdio.py`](src/python_acp/transport_stdio.py) | 83 | 27 | 0 | 2 | 84 |
| [`__init__.py`](src/python_acp/__init__.py) | 15 | 2 | 0 | 0 | — |

## Documentation

Every Markdown file in the repository, this one excepted. Prose is a deliverable
here rather than a by-product, so it is counted per file and not only in total.

| File | Lines |
| --- | ---: |
| [`README.md`](README.md) | 575 |
| [`src/python_acp/turn_mcp_router.md`](src/python_acp/turn_mcp_router.md) | 536 |
| [`src/python_acp/mcp_stdio.md`](src/python_acp/mcp_stdio.md) | 515 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 466 |
| [`docs/module-boundaries.md`](docs/module-boundaries.md) | 439 |
| [`AGENTS.md`](AGENTS.md) | 427 |
| [`src/python_acp/agent.md`](src/python_acp/agent.md) | 381 |
| [`CLAUDE.md`](CLAUDE.md) | 357 |
| [`src/python_acp/transport_ws.md`](src/python_acp/transport_ws.md) | 308 |
| [`src/python_acp/turns.md`](src/python_acp/turns.md) | 303 |
| [`.claude/skills/mcp-protocol/SKILL.md`](.claude/skills/mcp-protocol/SKILL.md) | 302 |
| [`docs/acp-compliance-matrix.md`](docs/acp-compliance-matrix.md) | 263 |
| [`.claude/skills/acp-protocol/SKILL.md`](.claude/skills/acp-protocol/SKILL.md) | 243 |
| [`docs/full-apc-plan.md`](docs/full-apc-plan.md) | 240 |
| [`CHANGELOG.md`](CHANGELOG.md) | 225 |
| [`src/python_acp/sessions.md`](src/python_acp/sessions.md) | 217 |
| [`src/python_acp/cli.md`](src/python_acp/cli.md) | 187 |
| [`src/python_acp/capabilities.md`](src/python_acp/capabilities.md) | 173 |
| [`src/python_acp/terminals.md`](src/python_acp/terminals.md) | 172 |
| [`src/python_acp/mcp_registry.md`](src/python_acp/mcp_registry.md) | 162 |
| [`docs/interop.md`](docs/interop.md) | 152 |
| [`src/python_acp/elicitation.md`](src/python_acp/elicitation.md) | 137 |
| [`src/python_acp/errors.md`](src/python_acp/errors.md) | 137 |
| [`src/python_acp/mcp_tools.md`](src/python_acp/mcp_tools.md) | 123 |
| [`.claude/skills/repo-docs-sync/SKILL.md`](.claude/skills/repo-docs-sync/SKILL.md) | 119 |
| [`src/python_acp/paths.md`](src/python_acp/paths.md) | 116 |
| [`src/python_acp/commands.md`](src/python_acp/commands.md) | 111 |
| [`.claude/skills/mcp-protocol/spec-versions.md`](.claude/skills/mcp-protocol/spec-versions.md) | 89 |
| [`src/python_acp/mcp_content.md`](src/python_acp/mcp_content.md) | 88 |
| [`src/python_acp/transport_stdio.md`](src/python_acp/transport_stdio.md) | 84 |
| [`.beads/README.md`](.beads/README.md) | 81 |
| [`.agents/skills/beads/SKILL.md`](.agents/skills/beads/SKILL.md) | 80 |
| **32 files** | **7,808** |

## Test modules

| Module | Lines | Test functions |
| --- | ---: | ---: |
| [`test_turn_mcp_router.py`](tests/test_turn_mcp_router.py) | 2,326 | 134 |
| [`test_agent.py`](tests/test_agent.py) | 1,723 | 87 |
| [`test_mcp_stdio.py`](tests/test_mcp_stdio.py) | 940 | 54 |
| [`test_transport_ws.py`](tests/test_transport_ws.py) | 1,040 | 42 |
| [`test_sessions.py`](tests/test_sessions.py) | 582 | 39 |
| [`test_turns.py`](tests/test_turns.py) | 467 | 31 |
| [`test_mcp_registry.py`](tests/test_mcp_registry.py) | 448 | 29 |
| [`test_container_image.py`](tests/test_container_image.py) | 413 | 25 |
| [`test_terminals.py`](tests/test_terminals.py) | 563 | 25 |
| [`test_paths.py`](tests/test_paths.py) | 213 | 24 |
| [`test_elicitation.py`](tests/test_elicitation.py) | 541 | 20 |
| [`test_commands.py`](tests/test_commands.py) | 230 | 19 |
| [`test_transport_stdio.py`](tests/test_transport_stdio.py) | 383 | 19 |
| [`test_check_docs.py`](tests/test_check_docs.py) | 264 | 18 |
| [`test_negative.py`](tests/test_negative.py) | 534 | 18 |
| [`test_capabilities.py`](tests/test_capabilities.py) | 276 | 17 |
| [`test_conformance.py`](tests/test_conformance.py) | 453 | 16 |
| [`test_mcp_content.py`](tests/test_mcp_content.py) | 195 | 16 |
| [`test_errors.py`](tests/test_errors.py) | 181 | 14 |
| [`test_code_stats.py`](tests/test_code_stats.py) | 235 | 12 |
| [`test_interop.py`](tests/test_interop.py) | 159 | 11 |
| [`test_transcripts.py`](tests/test_transcripts.py) | 652 | 10 |
| [`test_executor_neutrality.py`](tests/test_executor_neutrality.py) | 305 | 8 |
| [`test_mcp_tools.py`](tests/test_mcp_tools.py) | 166 | 8 |
| [`test_sdk_dependency.py`](tests/test_sdk_dependency.py) | 48 | 3 |
| [`test_version.py`](tests/test_version.py) | 63 | 3 |
