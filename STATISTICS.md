# Statistics

**Generated — do not edit by hand.** `make stats` rewrites this file from
[scripts/code_stats.py](scripts/code_stats.py); an edit here is lost the next time
anyone runs it.

Counted at commit **093a24e 2026-08-25**. These are a snapshot and go out of date with the
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
| `src/python_acp` | 17 | 6,857 | 3,268 | 1,962 | 567 | 1,060 | 41 | 247 | 98 | 0 |
| `tests` | 28 | 13,674 | 8,188 | 1,576 | 680 | 3,230 | 43 | 899 | 529 | 658 |
| `scripts` | 4 | 1,344 | 902 | 172 | 42 | 228 | 5 | 51 | 0 | 0 |
| **Total** | **49** | **21,875** | **12,358** | **3,710** | **1,289** | **4,518** | **89** | **1,197** | **627** | **658** |

## Ratios worth knowing

| Measure | Value | What it means |
| --- | ---: | --- |
| Test code to production code | 2.5 : 1 | 8,188 lines of test code against 3,268 of production code |
| Prose share of production source | 37% | 1,962 docstring + 567 comment lines. The repo documents decisions, not descriptions, and it shows up as mass |
| Co-located module docs | 3,499 lines | 16 files beside the 16 modules that need one — `__init__.py` is exempt. The rule `check_docs.py` enforces |
| Markdown across the repo | 7,547 lines | 34 files, module docs included and this one excluded — its own length would otherwise be part of its own content |

**Test functions are not test cases.** The table counts `def test_*`; pytest
collects more, because `@pytest.mark.parametrize` expands one function into many.
Run `make test` for the number that matters to CI.

## Production modules

Every module here has a sibling `.md` — the co-located doc rule. Where the doc is
longer than the module, that is usually deliberate.

| Module | Lines | Code | Classes | Functions | Sibling doc |
| --- | ---: | ---: | ---: | ---: | ---: |
| [`turn_mcp_router.py`](src/python_acp/turn_mcp_router.py) | 1,432 | 772 | 9 | 36 | 505 |
| [`mcp_stdio.py`](src/python_acp/mcp_stdio.py) | 855 | 459 | 5 | 41 | 515 |
| [`agent.py`](src/python_acp/agent.py) | 736 | 346 | 1 | 30 | 354 |
| [`turns.py`](src/python_acp/turns.py) | 618 | 280 | 11 | 17 | 303 |
| [`sessions.py`](src/python_acp/sessions.py) | 571 | 260 | 4 | 32 | 217 |
| [`terminals.py`](src/python_acp/terminals.py) | 419 | 192 | 2 | 18 | 172 |
| [`transport_ws.py`](src/python_acp/transport_ws.py) | 389 | 180 | 3 | 18 | 245 |
| [`mcp_registry.py`](src/python_acp/mcp_registry.py) | 353 | 145 | 2 | 16 | 162 |
| [`capabilities.py`](src/python_acp/capabilities.py) | 348 | 204 | 1 | 5 | 173 |
| [`elicitation.py`](src/python_acp/elicitation.py) | 242 | 98 | 1 | 4 | 137 |
| [`mcp_content.py`](src/python_acp/mcp_content.py) | 178 | 80 | 0 | 7 | 88 |
| [`errors.py`](src/python_acp/errors.py) | 163 | 49 | 0 | 7 | 137 |
| [`mcp_tools.py`](src/python_acp/mcp_tools.py) | 159 | 37 | 1 | 4 | 123 |
| [`cli.py`](src/python_acp/cli.py) | 148 | 88 | 0 | 5 | 168 |
| [`paths.py`](src/python_acp/paths.py) | 148 | 49 | 1 | 5 | 116 |
| [`transport_stdio.py`](src/python_acp/transport_stdio.py) | 83 | 27 | 0 | 2 | 84 |
| [`__init__.py`](src/python_acp/__init__.py) | 15 | 2 | 0 | 0 | — |

## Test modules

| Module | Lines | Test functions |
| --- | ---: | ---: |
| [`test_turn_mcp_router.py`](tests/test_turn_mcp_router.py) | 2,166 | 123 |
| [`test_agent.py`](tests/test_agent.py) | 1,625 | 82 |
| [`test_mcp_stdio.py`](tests/test_mcp_stdio.py) | 940 | 54 |
| [`test_sessions.py`](tests/test_sessions.py) | 582 | 39 |
| [`test_transport_ws.py`](tests/test_transport_ws.py) | 951 | 38 |
| [`test_turns.py`](tests/test_turns.py) | 467 | 31 |
| [`test_mcp_registry.py`](tests/test_mcp_registry.py) | 448 | 29 |
| [`test_container_image.py`](tests/test_container_image.py) | 413 | 25 |
| [`test_terminals.py`](tests/test_terminals.py) | 563 | 25 |
| [`test_paths.py`](tests/test_paths.py) | 213 | 24 |
| [`test_elicitation.py`](tests/test_elicitation.py) | 541 | 20 |
| [`test_negative.py`](tests/test_negative.py) | 534 | 18 |
| [`test_transport_stdio.py`](tests/test_transport_stdio.py) | 344 | 18 |
| [`test_capabilities.py`](tests/test_capabilities.py) | 276 | 17 |
| [`test_conformance.py`](tests/test_conformance.py) | 453 | 16 |
| [`test_mcp_content.py`](tests/test_mcp_content.py) | 195 | 16 |
| [`test_check_docs.py`](tests/test_check_docs.py) | 215 | 15 |
| [`test_errors.py`](tests/test_errors.py) | 181 | 14 |
| [`test_code_stats.py`](tests/test_code_stats.py) | 204 | 11 |
| [`test_interop.py`](tests/test_interop.py) | 159 | 11 |
| [`test_transcripts.py`](tests/test_transcripts.py) | 652 | 10 |
| [`test_executor_neutrality.py`](tests/test_executor_neutrality.py) | 305 | 8 |
| [`test_mcp_tools.py`](tests/test_mcp_tools.py) | 166 | 8 |
| [`test_sdk_dependency.py`](tests/test_sdk_dependency.py) | 48 | 3 |
| [`test_version.py`](tests/test_version.py) | 63 | 3 |
