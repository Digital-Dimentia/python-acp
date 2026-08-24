"""Tests for reading an MCP server's tool annotations as an ACP `ToolKind`.

The mapping is a pure function over a dict, so it is tested as one — exhaustively, and
including the rows where the answer is "we know nothing", because those are the rows a
future edit is most likely to get wrong by helpfully guessing.

`ToolCatalogue`'s job is smaller than it looks: fetch at most once per server per turn,
and never let a presentation hint fail a turn. Both halves are here. The end-to-end
proof — that the kind reaches the wire and that a `readOnlyHint` tool is still asked
about — lives in `test_turn_mcp_router.py`, where a real subprocess is already running.
"""

from __future__ import annotations

import pytest

from python_acp.mcp_tools import UNKNOWN_KIND, ToolCatalogue, tool_kind


class FakeBackend:
    """Answers `tools/list` with a fixed listing, or raises, and counts the calls."""

    def __init__(self, listing: object = (), error: Exception | None = None) -> None:
        self.listing = listing
        self.error = error
        self.calls = 0

    async def list_tools(self) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.listing


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        ({"readOnlyHint": True}, "read"),
        ({"readOnlyHint": True, "openWorldHint": False}, "read"),
        ({"readOnlyHint": True, "openWorldHint": True}, "fetch"),
        ({"readOnlyHint": False, "destructiveHint": False}, "edit"),
        ({"readOnlyHint": False, "destructiveHint": True}, "delete"),
        # MCP defaults `destructiveHint` to true, and this is the one place that default
        # means "assume the worst" about something a human is being asked to allow.
        ({"readOnlyHint": False}, "delete"),
        # `idempotentHint` has no ACP kind and must not disturb the ones that do.
        ({"readOnlyHint": True, "idempotentHint": True}, "read"),
        ({"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}, "edit"),
    ],
)
def test_a_stated_hint_becomes_a_kind(annotations: dict, expected: str) -> None:
    assert tool_kind(annotations) == expected


@pytest.mark.parametrize(
    "annotations",
    [
        None,
        {},
        {"title": "Echo"},
        {"destructiveHint": True},
        {"openWorldHint": True},
        {"readOnlyHint": "yes"},
        {"readOnlyHint": None},
        "not a mapping",
        [{"readOnlyHint": True}],
    ],
    ids=[
        "absent",
        "empty",
        "title-only",
        "destructive-without-readonly",
        "openworld-without-readonly",
        "readonly-not-a-bool",
        "readonly-null",
        "annotations-not-a-mapping",
        "annotations-a-list",
    ],
)
def test_saying_nothing_useful_stays_unknown(annotations: object) -> None:
    """`other` is what "the server made no claim" looked like before this existed.

    `{"title": "Echo"}` is the row that matters most. Applying MCP's `destructiveHint`
    default there would render every politely-titled tool as a deletion, and an agent
    that cries wolf on each one teaches the human to stop reading the icon.
    """
    assert tool_kind(annotations) == UNKNOWN_KIND


def test_destructive_rounds_toward_the_more_alarming_icon() -> None:
    """MCP's `destructiveHint` is wider than ACP's `delete`, and the rounding is chosen.

    It covers overwriting as well as deleting. Rounding down to `edit` would understate
    the risk in the one place the label costs something — a permission prompt.
    """
    assert tool_kind({"readOnlyHint": False, "destructiveHint": True}) == "delete"
    assert tool_kind({"readOnlyHint": False, "destructiveHint": False}) == "edit"


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


async def test_one_listing_per_server_per_turn() -> None:
    """The catalogue exists so reading a hint does not cost a call per tool call."""
    backend = FakeBackend([{"name": "echo", "annotations": {"readOnlyHint": True}}])
    catalogue = ToolCatalogue({"tools": backend})

    assert await catalogue.kind("tools", "echo") == "read"
    assert await catalogue.kind("tools", "echo") == "read"
    assert await catalogue.listing("tools")

    assert backend.calls == 1


async def test_a_broken_listing_costs_a_kind_and_nothing_else() -> None:
    """A backend whose `tools/list` fails but whose `tools/call` works still runs turns.

    That backend worked before annotations were read, and failing its turn over a
    decoration would be a regression dressed as a feature.
    """
    backend = FakeBackend(error=RuntimeError("no listing for you"))
    catalogue = ToolCatalogue({"tools": backend})

    assert await catalogue.kind("tools", "echo") == UNKNOWN_KIND


async def test_the_strict_listing_still_raises() -> None:
    """`available_commands` is the listing, so there the failure is the answer."""
    catalogue = ToolCatalogue({"tools": FakeBackend(error=RuntimeError("no listing for you"))})

    with pytest.raises(RuntimeError, match="no listing"):
        await catalogue.listing("tools")


@pytest.mark.parametrize(
    ("listing", "tool"),
    [
        ([{"name": "other-tool", "annotations": {"readOnlyHint": True}}], "echo"),
        ([{"annotations": {"readOnlyHint": True}}], "echo"),
        (["not a dict"], "echo"),
        ([], "echo"),
    ],
    ids=["tool-not-listed", "entry-has-no-name", "entry-not-a-dict", "empty-listing"],
)
async def test_a_tool_the_listing_does_not_describe_is_unknown(listing: list, tool: str) -> None:
    catalogue = ToolCatalogue({"tools": FakeBackend(listing)})

    assert await catalogue.kind("tools", tool) == UNKNOWN_KIND


async def test_an_unknown_server_is_unknown_rather_than_a_crash() -> None:
    """`_server` resolves every invocation before this is reached, so this is a belt.

    It is worth having: the failure it prevents would be a `KeyError` inside a
    notification path, which is the least debuggable place for one.
    """
    catalogue = ToolCatalogue({})

    assert await catalogue.kind("nowhere", "echo") == UNKNOWN_KIND
