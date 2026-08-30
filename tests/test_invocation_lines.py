"""The command line acp-ui writes, read by the parser this repo owns (`pyacp-9xd`).

acp-ui's tool-parameter form serialises a filled-in form into an invocation line —
`/demo/echo --text hello --times 7` — and *this* repo is what parses it, through
`parse_command` and then `coerce_arguments`. The contract crosses a language boundary, so
neither side can check it alone: a serialiser verified only against its own assumptions is
exactly the failure worth preventing, and it would surface as a tool called with the wrong
arguments rather than as an error anybody sees.

So the expectations are written down **once**, in acp-ui's
`fixtures/invocation-lines.json`, and asserted twice against the same file:

    acp-ui      toInvocationLine(command, parseSchema(schema), values) === line
    python-acp  coerce_arguments(parse_command(line), schema) == arguments

`tests/data/invocation-lines.json` is a copy, and its `$source` block says where from and
how to refresh it. A copy rather than a path into a sibling checkout, because the two
repos are cloned independently: a test that reached across the filesystem would pass,
fail, or skip depending on where python-acp happens to sit.

**Nothing here skips.** A missing fixture, an empty one, and a fixture whose cases changed
shape all fail, because a cross-repo contract test that reports green having asserted
nothing is worse than not having it — it is the same false confidence in the parser that
acp-ui would otherwise have in its serialiser.

Adding a case means adding it to *acp-ui's* copy and refreshing this one. A case that
exists only here asserts this repo against itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from python_acp.commands import InvokeTool, coerce_arguments, parse_command

FIXTURE = Path(__file__).parent / "data" / "invocation-lines.json"


def load() -> dict[str, Any]:
    """The fixture, or a failure naming what to do about it.

    Deliberately not a `pytest.skip`: see the module docstring.
    """
    assert FIXTURE.is_file(), (
        f"{FIXTURE} is missing. It is a copy of acp-ui's "
        "fixtures/invocation-lines.json — restore it rather than skipping, or this "
        "suite reports green having asserted nothing about the contract."
    )
    return json.loads(FIXTURE.read_text())


def cases() -> list[dict[str, Any]]:
    found = load().get("cases")
    assert isinstance(found, list) and found, "the fixture carries no cases"
    return found


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["name"])
def test_the_line_acp_ui_writes_coerces_to_the_arguments_it_recorded(case: dict[str, Any]) -> None:
    """One case, asserted the way the fixture's own header says to assert it.

    The two steps are separate on purpose. `parse_command` decides *what* was typed —
    which tool, which flags, repeated or not — and `coerce_arguments` decides what the
    strings mean once the tool's `inputSchema` is in hand. A serialiser bug shows up in
    the first, a type bug in the second.
    """
    command = parse_command(case["line"])
    assert isinstance(command, InvokeTool), (
        f"{case['line']!r} did not parse as a tool invocation but as "
        f"{type(command).__name__}"
    )
    assert coerce_arguments(command, case["schema"]) == case["arguments"]


def test_every_case_names_the_tool_the_form_was_built_for() -> None:
    """The other half of the line, which the argument assertion above cannot see.

    `coerce_arguments` never looks at the target, so a serialiser that wrote the right
    flags against the wrong tool would pass every case above and call something else.
    """
    for case in cases():
        command = parse_command(case["line"])
        assert isinstance(command, InvokeTool)
        assert f"{command.server}/{command.tool}" == case["command"]


def test_the_fixture_says_where_it_came_from() -> None:
    """A copied fixture with no provenance is a fixture nobody can refresh.

    Checked because the refresh is a file copy, which drops this block unless whoever
    ran it puts it back — and a stale copy that still *looks* authoritative is how the
    two repos drift apart while both suites stay green.
    """
    source = load().get("$source")
    assert isinstance(source, dict), "the copy must say where it came from"
    assert source.get("repo") == "acp-ui"
    assert source.get("path") == "fixtures/invocation-lines.json"
    assert isinstance(source.get("commit"), str) and len(source["commit"]) == 40
    assert isinstance(source.get("copied"), str)


def test_every_case_carries_the_five_fields_both_suites_read() -> None:
    """A refresh from a fixture that changed shape must fail here, not silently assert less.

    `values` is acp-ui's half — this suite never reads it — but its absence means the
    file is no longer the one both sides agreed on, and that is worth failing over.
    """
    for case in cases():
        assert set(case) == {"name", "command", "schema", "values", "line", "arguments"}, (
            f"case {case.get('name')!r} is not the shape both suites read"
        )
