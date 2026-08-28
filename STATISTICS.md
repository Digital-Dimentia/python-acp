# Statistics

**Generated — do not edit by hand.** `make stats` rewrites this file from
[scripts/code_stats.py](scripts/code_stats.py); an edit here is lost the next time
anyone runs it.

Counted at commit **7824ed9 2026-08-27**. These are a snapshot and go out of date with the
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
| `src/python_acp` | 21 | 9,535 | 4,601 | 2,687 | 783 | 1,464 | 54 | 330 | 117 | 0 |
| `tests` | 33 | 17,038 | 10,223 | 2,011 | 833 | 3,971 | 54 | 1,145 | 636 | 864 |
| `scripts` | 3 | 956 | 627 | 145 | 37 | 147 | 4 | 31 | 0 | 0 |
| **Total** | **57** | **27,529** | **15,451** | **4,843** | **1,653** | **5,582** | **112** | **1,506** | **753** | **864** |

## Ratios worth knowing

| Measure | Value | What it means |
| --- | ---: | --- |
| Test code to production code | 2.2 : 1 | 10,223 lines of test code against 4,601 of production code |
| Prose share of production source | 36% | 2,687 docstring + 783 comment lines. The repo documents decisions, not descriptions, and it shows up as mass |
| Co-located module docs | 4,563 lines | 20 files beside the 20 modules that need one — `__init__.py` is exempt. The rule `check_docs.py` enforces |
| Markdown across the repo | 8,776 lines | 35 files, module docs included and this one excluded — its own length would otherwise be part of its own content |

**Test functions are not test cases.** The table counts `def test_*`; pytest
collects more, because `@pytest.mark.parametrize` expands one function into many.
Run `make test` for the number that matters to CI.

## Production modules

Every module here has a sibling `.md` — the co-located doc rule. Where the doc is
longer than the module, that is usually deliberate.

| Module | Lines | Code | Classes | Functions | Sibling doc |
| --- | ---: | ---: | ---: | ---: | ---: |
| [`turn_mcp_router.py`](src/python_acp/turn_mcp_router.py) | 1,852 | 1,029 | 10 | 48 | 585 |
| [`commands.py`](src/python_acp/commands.py) | 1,129 | 646 | 9 | 33 | 333 |
| [`agent.py`](src/python_acp/agent.py) | 1,023 | 466 | 1 | 37 | 457 |
| [`mcp_stdio.py`](src/python_acp/mcp_stdio.py) | 890 | 461 | 5 | 42 | 554 |
| [`turns.py`](src/python_acp/turns.py) | 636 | 284 | 11 | 19 | 303 |
| [`sessions.py`](src/python_acp/sessions.py) | 571 | 260 | 4 | 32 | 217 |
| [`transport_ws.py`](src/python_acp/transport_ws.py) | 467 | 210 | 3 | 19 | 308 |
| [`terminals.py`](src/python_acp/terminals.py) | 419 | 192 | 2 | 18 | 172 |
| [`mcp_registry.py`](src/python_acp/mcp_registry.py) | 404 | 168 | 2 | 18 | 193 |
| [`capabilities.py`](src/python_acp/capabilities.py) | 348 | 204 | 1 | 5 | 173 |
| [`mcp_catalogue.py`](src/python_acp/mcp_catalogue.py) | 324 | 157 | 3 | 17 | 113 |
| [`elicitation.py`](src/python_acp/elicitation.py) | 242 | 98 | 1 | 4 | 137 |
| [`cli.py`](src/python_acp/cli.py) | 220 | 121 | 0 | 6 | 217 |
| [`mcp_content.py`](src/python_acp/mcp_content.py) | 178 | 80 | 0 | 7 | 88 |
| [`errors.py`](src/python_acp/errors.py) | 163 | 49 | 0 | 7 | 137 |
| [`mcp_tools.py`](src/python_acp/mcp_tools.py) | 159 | 37 | 1 | 4 | 123 |
| [`paths.py`](src/python_acp/paths.py) | 148 | 49 | 1 | 5 | 116 |
| [`transport_stdio.py`](src/python_acp/transport_stdio.py) | 119 | 36 | 0 | 3 | 105 |
| [`announcer.py`](src/python_acp/announcer.py) | 118 | 33 | 0 | 2 | 119 |
| [`markdown.py`](src/python_acp/markdown.py) | 110 | 19 | 0 | 4 | 113 |
| [`__init__.py`](src/python_acp/__init__.py) | 15 | 2 | 0 | 0 | — |

## Documentation

Every Markdown file in the repository, this one excepted. Prose is a deliverable
here rather than a by-product, so it is counted per file and not only in total.

| File | Lines |
| --- | ---: |
| [`README.md`](README.md) | 686 |
| [`src/python_acp/turn_mcp_router.md`](src/python_acp/turn_mcp_router.md) | 585 |
| [`src/python_acp/mcp_stdio.md`](src/python_acp/mcp_stdio.md) | 554 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 469 |
| [`src/python_acp/agent.md`](src/python_acp/agent.md) | 457 |
| [`AGENTS.md`](AGENTS.md) | 444 |
| [`docs/module-boundaries.md`](docs/module-boundaries.md) | 439 |
| [`CLAUDE.md`](CLAUDE.md) | 374 |
| [`src/python_acp/commands.md`](src/python_acp/commands.md) | 333 |
| [`src/python_acp/transport_ws.md`](src/python_acp/transport_ws.md) | 308 |
| [`.claude/skills/mcp-protocol/SKILL.md`](.claude/skills/mcp-protocol/SKILL.md) | 305 |
| [`src/python_acp/turns.md`](src/python_acp/turns.md) | 303 |
| [`docs/acp-compliance-matrix.md`](docs/acp-compliance-matrix.md) | 263 |
| [`.claude/skills/acp-protocol/SKILL.md`](.claude/skills/acp-protocol/SKILL.md) | 247 |
| [`docs/full-apc-plan.md`](docs/full-apc-plan.md) | 240 |
| [`CHANGELOG.md`](CHANGELOG.md) | 225 |
| [`src/python_acp/cli.md`](src/python_acp/cli.md) | 217 |
| [`src/python_acp/sessions.md`](src/python_acp/sessions.md) | 217 |
| [`src/python_acp/mcp_registry.md`](src/python_acp/mcp_registry.md) | 193 |
| [`src/python_acp/capabilities.md`](src/python_acp/capabilities.md) | 173 |
| [`src/python_acp/terminals.md`](src/python_acp/terminals.md) | 172 |
| [`docs/interop.md`](docs/interop.md) | 152 |
| [`src/python_acp/elicitation.md`](src/python_acp/elicitation.md) | 137 |
| [`src/python_acp/errors.md`](src/python_acp/errors.md) | 137 |
| [`src/python_acp/mcp_tools.md`](src/python_acp/mcp_tools.md) | 123 |
| [`.claude/skills/repo-docs-sync/SKILL.md`](.claude/skills/repo-docs-sync/SKILL.md) | 119 |
| [`src/python_acp/announcer.md`](src/python_acp/announcer.md) | 119 |
| [`src/python_acp/paths.md`](src/python_acp/paths.md) | 116 |
| [`src/python_acp/markdown.md`](src/python_acp/markdown.md) | 113 |
| [`src/python_acp/mcp_catalogue.md`](src/python_acp/mcp_catalogue.md) | 113 |
| [`src/python_acp/transport_stdio.md`](src/python_acp/transport_stdio.md) | 105 |
| [`.claude/skills/mcp-protocol/spec-versions.md`](.claude/skills/mcp-protocol/spec-versions.md) | 89 |
| [`src/python_acp/mcp_content.md`](src/python_acp/mcp_content.md) | 88 |
| [`.beads/README.md`](.beads/README.md) | 81 |
| [`.agents/skills/beads/SKILL.md`](.agents/skills/beads/SKILL.md) | 80 |
| **35 files** | **8,776** |

## Test modules

| Module | Lines | Test functions |
| --- | ---: | ---: |
| [`test_turn_mcp_router.py`](tests/test_turn_mcp_router.py) | 2,632 | 154 |
| [`test_agent.py`](tests/test_agent.py) | 2,276 | 108 |
| [`test_mcp_stdio.py`](tests/test_mcp_stdio.py) | 979 | 57 |
| [`test_commands.py`](tests/test_commands.py) | 652 | 56 |
| [`test_transport_ws.py`](tests/test_transport_ws.py) | 1,212 | 48 |
| [`test_mcp_registry.py`](tests/test_mcp_registry.py) | 568 | 39 |
| [`test_sessions.py`](tests/test_sessions.py) | 582 | 39 |
| [`test_turns.py`](tests/test_turns.py) | 467 | 31 |
| [`test_transport_stdio.py`](tests/test_transport_stdio.py) | 531 | 27 |
| [`test_container_image.py`](tests/test_container_image.py) | 413 | 25 |
| [`test_terminals.py`](tests/test_terminals.py) | 563 | 25 |
| [`test_paths.py`](tests/test_paths.py) | 213 | 24 |
| [`test_mcp_catalogue.py`](tests/test_mcp_catalogue.py) | 276 | 22 |
| [`test_elicitation.py`](tests/test_elicitation.py) | 541 | 20 |
| [`test_check_docs.py`](tests/test_check_docs.py) | 264 | 18 |
| [`test_negative.py`](tests/test_negative.py) | 534 | 18 |
| [`test_capabilities.py`](tests/test_capabilities.py) | 276 | 17 |
| [`test_conformance.py`](tests/test_conformance.py) | 453 | 16 |
| [`test_mcp_content.py`](tests/test_mcp_content.py) | 195 | 16 |
| [`test_markdown.py`](tests/test_markdown.py) | 141 | 15 |
| [`test_errors.py`](tests/test_errors.py) | 181 | 14 |
| [`test_code_stats.py`](tests/test_code_stats.py) | 235 | 12 |
| [`test_makefile_targets.py`](tests/test_makefile_targets.py) | 265 | 12 |
| [`test_interop.py`](tests/test_interop.py) | 170 | 11 |
| [`test_transcripts.py`](tests/test_transcripts.py) | 691 | 10 |
| [`test_announcer.py`](tests/test_announcer.py) | 163 | 8 |
| [`test_executor_neutrality.py`](tests/test_executor_neutrality.py) | 305 | 8 |
| [`test_mcp_tools.py`](tests/test_mcp_tools.py) | 166 | 8 |
| [`test_sdk_dependency.py`](tests/test_sdk_dependency.py) | 48 | 3 |
| [`test_version.py`](tests/test_version.py) | 63 | 3 |
