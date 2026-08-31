# Statistics

**Generated — do not edit by hand.** `make stats` rewrites this file from
[scripts/code_stats.py](scripts/code_stats.py); an edit here is lost the next time
anyone runs it.

Counted at commit **d79d3f1 2026-08-31**. These are a snapshot and go out of date with the
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
| `src/python_acp` | 25 | 12,886 | 6,433 | 3,515 | 941 | 1,997 | 82 | 449 | 120 | 0 |
| `tests` | 41 | 20,729 | 12,392 | 2,565 | 1,027 | 4,745 | 55 | 1,356 | 683 | 1,044 |
| `scripts` | 3 | 956 | 627 | 145 | 37 | 147 | 4 | 31 | 0 | 0 |
| **Total** | **69** | **34,571** | **19,452** | **6,225** | **2,005** | **6,889** | **141** | **1,836** | **803** | **1,044** |

## Ratios worth knowing

| Measure | Value | What it means |
| --- | ---: | --- |
| Test code to production code | 1.9 : 1 | 12,392 lines of test code against 6,433 of production code |
| Prose share of production source | 35% | 3,515 docstring + 941 comment lines. The repo documents decisions, not descriptions, and it shows up as mass |
| Co-located module docs | 5,570 lines | 24 files beside the 24 modules that need one — `__init__.py` is exempt. The rule `check_docs.py` enforces |
| Markdown across the repo | 10,354 lines | 42 files, module docs included and this one excluded — its own length would otherwise be part of its own content |

**Test functions are not test cases.** The table counts `def test_*`; pytest
collects more, because `@pytest.mark.parametrize` expands one function into many.
Run `make test` for the number that matters to CI.

## Production modules

Every module here has a sibling `.md` — the co-located doc rule. Where the doc is
longer than the module, that is usually deliberate.

| Module | Lines | Code | Classes | Functions | Sibling doc |
| --- | ---: | ---: | ---: | ---: | ---: |
| [`turn_mcp_router.py`](src/python_acp/turn_mcp_router.py) | 2,299 | 1,274 | 12 | 55 | 748 |
| [`commands.py`](src/python_acp/commands.py) | 1,376 | 759 | 9 | 40 | 424 |
| [`agent.py`](src/python_acp/agent.py) | 1,068 | 481 | 1 | 37 | 489 |
| [`edit_yaml.py`](src/python_acp/edit_yaml.py) | 973 | 591 | 5 | 42 | 207 |
| [`mcp_stdio.py`](src/python_acp/mcp_stdio.py) | 911 | 463 | 5 | 43 | 562 |
| [`edits.py`](src/python_acp/edits.py) | 675 | 290 | 16 | 23 | 178 |
| [`turns.py`](src/python_acp/turns.py) | 636 | 284 | 11 | 19 | 303 |
| [`sessions.py`](src/python_acp/sessions.py) | 571 | 260 | 4 | 32 | 217 |
| [`transport_ws.py`](src/python_acp/transport_ws.py) | 475 | 215 | 3 | 19 | 308 |
| [`edit_json.py`](src/python_acp/edit_json.py) | 470 | 299 | 3 | 22 | 117 |
| [`terminals.py`](src/python_acp/terminals.py) | 419 | 192 | 2 | 18 | 172 |
| [`mcp_registry.py`](src/python_acp/mcp_registry.py) | 404 | 168 | 2 | 18 | 193 |
| [`edit_docs.py`](src/python_acp/edit_docs.py) | 400 | 239 | 2 | 16 | 136 |
| [`capabilities.py`](src/python_acp/capabilities.py) | 348 | 204 | 1 | 5 | 173 |
| [`mcp_catalogue.py`](src/python_acp/mcp_catalogue.py) | 329 | 157 | 3 | 17 | 120 |
| [`cli.py`](src/python_acp/cli.py) | 249 | 146 | 0 | 6 | 256 |
| [`elicitation.py`](src/python_acp/elicitation.py) | 242 | 98 | 1 | 4 | 137 |
| [`mcp_content.py`](src/python_acp/mcp_content.py) | 209 | 88 | 0 | 8 | 117 |
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
| [`README.md`](README.md) | 767 |
| [`src/python_acp/turn_mcp_router.md`](src/python_acp/turn_mcp_router.md) | 748 |
| [`src/python_acp/mcp_stdio.md`](src/python_acp/mcp_stdio.md) | 562 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 558 |
| [`src/python_acp/agent.md`](src/python_acp/agent.md) | 489 |
| [`AGENTS.md`](AGENTS.md) | 455 |
| [`docs/module-boundaries.md`](docs/module-boundaries.md) | 439 |
| [`src/python_acp/commands.md`](src/python_acp/commands.md) | 424 |
| [`CLAUDE.md`](CLAUDE.md) | 385 |
| [`src/python_acp/transport_ws.md`](src/python_acp/transport_ws.md) | 308 |
| [`.claude/skills/mcp-protocol/SKILL.md`](.claude/skills/mcp-protocol/SKILL.md) | 307 |
| [`src/python_acp/turns.md`](src/python_acp/turns.md) | 303 |
| [`docs/acp-compliance-matrix.md`](docs/acp-compliance-matrix.md) | 263 |
| [`src/python_acp/cli.md`](src/python_acp/cli.md) | 256 |
| [`.claude/skills/acp-protocol/SKILL.md`](.claude/skills/acp-protocol/SKILL.md) | 247 |
| [`docs/full-apc-plan.md`](docs/full-apc-plan.md) | 240 |
| [`docs/tool-schema-contract.md`](docs/tool-schema-contract.md) | 237 |
| [`CHANGELOG.md`](CHANGELOG.md) | 225 |
| [`src/python_acp/sessions.md`](src/python_acp/sessions.md) | 217 |
| [`src/python_acp/edit_yaml.md`](src/python_acp/edit_yaml.md) | 207 |
| [`.claude/skills/repo-docs-sync/SKILL.md`](.claude/skills/repo-docs-sync/SKILL.md) | 201 |
| [`src/python_acp/mcp_registry.md`](src/python_acp/mcp_registry.md) | 193 |
| [`src/python_acp/edits.md`](src/python_acp/edits.md) | 178 |
| [`src/python_acp/capabilities.md`](src/python_acp/capabilities.md) | 173 |
| [`src/python_acp/terminals.md`](src/python_acp/terminals.md) | 172 |
| [`docs/interop.md`](docs/interop.md) | 152 |
| [`src/python_acp/elicitation.md`](src/python_acp/elicitation.md) | 137 |
| [`src/python_acp/errors.md`](src/python_acp/errors.md) | 137 |
| [`src/python_acp/edit_docs.md`](src/python_acp/edit_docs.md) | 136 |
| [`src/python_acp/mcp_tools.md`](src/python_acp/mcp_tools.md) | 123 |
| [`src/python_acp/mcp_catalogue.md`](src/python_acp/mcp_catalogue.md) | 120 |
| [`src/python_acp/announcer.md`](src/python_acp/announcer.md) | 119 |
| [`src/python_acp/edit_json.md`](src/python_acp/edit_json.md) | 117 |
| [`src/python_acp/mcp_content.md`](src/python_acp/mcp_content.md) | 117 |
| [`src/python_acp/paths.md`](src/python_acp/paths.md) | 116 |
| [`src/python_acp/markdown.md`](src/python_acp/markdown.md) | 113 |
| [`src/python_acp/transport_stdio.md`](src/python_acp/transport_stdio.md) | 105 |
| [`.claude/skills/mcp-protocol/spec-versions.md`](.claude/skills/mcp-protocol/spec-versions.md) | 89 |
| [`.beads/README.md`](.beads/README.md) | 81 |
| [`.agents/skills/beads/SKILL.md`](.agents/skills/beads/SKILL.md) | 80 |
| [`tests/data/edits/markdown/guide.md`](tests/data/edits/markdown/guide.md) | 49 |
| [`tests/data/edits/markdown/duplicates.md`](tests/data/edits/markdown/duplicates.md) | 9 |
| **42 files** | **10,354** |

## Test modules

| Module | Lines | Test functions |
| --- | ---: | ---: |
| [`test_turn_mcp_router.py`](tests/test_turn_mcp_router.py) | 3,370 | 188 |
| [`test_agent.py`](tests/test_agent.py) | 2,438 | 116 |
| [`test_commands.py`](tests/test_commands.py) | 900 | 73 |
| [`test_mcp_stdio.py`](tests/test_mcp_stdio.py) | 1,033 | 60 |
| [`test_transport_ws.py`](tests/test_transport_ws.py) | 1,258 | 49 |
| [`test_mcp_registry.py`](tests/test_mcp_registry.py) | 568 | 39 |
| [`test_sessions.py`](tests/test_sessions.py) | 582 | 39 |
| [`test_turns.py`](tests/test_turns.py) | 467 | 31 |
| [`test_transport_stdio.py`](tests/test_transport_stdio.py) | 547 | 29 |
| [`test_edit_yaml.py`](tests/test_edit_yaml.py) | 404 | 28 |
| [`test_container_image.py`](tests/test_container_image.py) | 413 | 25 |
| [`test_terminals.py`](tests/test_terminals.py) | 563 | 25 |
| [`test_paths.py`](tests/test_paths.py) | 213 | 24 |
| [`test_edit_docs.py`](tests/test_edit_docs.py) | 286 | 22 |
| [`test_mcp_catalogue.py`](tests/test_mcp_catalogue.py) | 276 | 22 |
| [`test_edit_json.py`](tests/test_edit_json.py) | 231 | 20 |
| [`test_elicitation.py`](tests/test_elicitation.py) | 541 | 20 |
| [`test_check_docs.py`](tests/test_check_docs.py) | 264 | 18 |
| [`test_mcp_content.py`](tests/test_mcp_content.py) | 238 | 18 |
| [`test_negative.py`](tests/test_negative.py) | 534 | 18 |
| [`test_capabilities.py`](tests/test_capabilities.py) | 276 | 17 |
| [`test_conformance.py`](tests/test_conformance.py) | 453 | 16 |
| [`test_edits.py`](tests/test_edits.py) | 241 | 16 |
| [`test_markdown.py`](tests/test_markdown.py) | 141 | 15 |
| [`test_errors.py`](tests/test_errors.py) | 181 | 14 |
| [`test_code_stats.py`](tests/test_code_stats.py) | 235 | 12 |
| [`test_makefile_targets.py`](tests/test_makefile_targets.py) | 265 | 12 |
| [`test_interop.py`](tests/test_interop.py) | 170 | 11 |
| [`test_transcripts.py`](tests/test_transcripts.py) | 691 | 10 |
| [`test_client_contract.py`](tests/test_client_contract.py) | 364 | 9 |
| [`test_announcer.py`](tests/test_announcer.py) | 163 | 8 |
| [`test_executor_neutrality.py`](tests/test_executor_neutrality.py) | 305 | 8 |
| [`test_mcp_tools.py`](tests/test_mcp_tools.py) | 166 | 8 |
| [`test_start_zoo.py`](tests/test_start_zoo.py) | 151 | 8 |
| [`test_start_ws.py`](tests/test_start_ws.py) | 187 | 6 |
| [`test_invocation_lines.py`](tests/test_invocation_lines.py) | 115 | 4 |
| [`test_sdk_dependency.py`](tests/test_sdk_dependency.py) | 48 | 3 |
| [`test_version.py`](tests/test_version.py) | 63 | 3 |
