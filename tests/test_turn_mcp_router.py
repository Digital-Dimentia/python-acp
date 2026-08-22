"""Tests for the deterministic MCP tool-router.

Against the real `tests/fixtures/mock_mcp_server.py` subprocess, per the repo's
convention: the thing under test is a tool call actually running and its result actually
reaching a `session/update`, and a mock backend would prove neither.

The parsing tests are exhaustive on purpose. The invocation convention is invented by
this module — nothing in the ACP spec describes it — so it is the one contract a client
codes against, and every refusal it can produce is part of that contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from acp.schema import EnvVariable, McpServerStdio, TextContentBlock

from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPProtocolError
from python_acp.sessions import SessionRegistry
from python_acp.turn_mcp_router import McpToolRouterExecutor
from python_acp.turns import TurnContext

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def spec(name: str) -> McpServerStdio:
    return McpServerStdio(
        name=name, command=sys.executable, args=[str(FIXTURE_SERVER)], env=[EnvVariable(name="X", value="1")]
    )


class RecordingClient:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs) -> None:
        self.updates.append(update)


def block(**payload: Any) -> TextContentBlock:
    return TextContentBlock(type="text", text=json.dumps(payload))


class Harness:
    """A session with `server_names` MCP servers open, and an executor over them."""

    def __init__(self, *server_names: str) -> None:
        self.server_names = server_names
        self.backends = McpBackendRegistry()
        self.client = RecordingClient()
        self.session = SessionRegistry().create("/work")

    async def __aenter__(self) -> Harness:
        await self.backends.open(self.session.session_id, [spec(n) for n in self.server_names])
        self.context = TurnContext(self.session, self.client)  # type: ignore[arg-type]
        self.executor = McpToolRouterExecutor(self.backends)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.backends.close_all()

    async def run(self, *blocks: Any):
        return await self.executor.execute(self.context, list(blocks))

    @property
    def updates(self) -> list[Any]:
        return self.client.updates

    def kinds(self) -> list[str]:
        return [update.session_update for update in self.updates]


# ---------------------------------------------------------------------------
# Running tools
# ---------------------------------------------------------------------------


async def test_a_prompt_naming_a_tool_runs_it_and_ends_the_turn() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert harness.kinds() == ["tool_call", "tool_call_update", "tool_call_update"]


async def test_the_status_transitions_are_real() -> None:
    """`pending` and `in_progress` are separate notifications so a client can render the
    call the moment it is known, then show that the wait has begun."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "hi"}))

    start, began, finished = harness.updates
    assert (start.status, start.title) == ("pending", "tools/echo")
    assert began.status == "in_progress"
    assert finished.status == "completed"
    assert start.tool_call_id == began.tool_call_id == finished.tool_call_id


async def test_the_tools_own_output_reaches_the_client() -> None:
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "from the router"}))

    finished = harness.updates[-1]
    assert finished.content[0].content.text == "from the router"
    assert finished.raw_output["isError"] is False


async def test_arguments_are_carried_as_raw_input() -> None:
    """A client rendering the call needs to show what it was called with."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert harness.updates[0].raw_input == {"text": "hi"}


async def test_arguments_default_to_an_empty_object() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "end_turn"
    assert harness.updates[0].raw_input == {}


async def test_several_tool_calls_run_in_order() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(
            block(tool="echo", arguments={"text": "first"}),
            block(tool="echo", arguments={"text": "second"}),
        )

    assert result.stop_reason == "end_turn"
    finished = [u for u in harness.updates if getattr(u, "content", None)]
    assert [u.content[0].content.text for u in finished] == ["first", "second"]


# ---------------------------------------------------------------------------
# Failure, of two different kinds
# ---------------------------------------------------------------------------


async def test_a_failed_tool_becomes_a_failed_status_not_a_failed_turn() -> None:
    """MCP reports tool failure as a *successful* result carrying isError.

    Collapsing that into a stopReason would lose which tool failed and why.
    """
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="boom", arguments={"detail": "it broke"}))

    assert result.stop_reason == "end_turn"
    assert harness.updates[-1].status == "failed"
    assert harness.updates[-1].content[0].content.text == "it broke"


async def test_a_failed_tool_does_not_stop_the_calls_after_it() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(
            block(tool="boom"), block(tool="echo", arguments={"text": "still ran"})
        )

    assert result.stop_reason == "end_turn"
    assert [u.status for u in harness.updates if u.session_update == "tool_call_update"] == [
        "in_progress", "failed", "in_progress", "completed",
    ]


async def test_a_protocol_failure_propagates_with_its_backend_code() -> None:
    """A server-level JSON-RPC error is not a tool failure; `errors.py` maps it."""
    async with Harness("tools") as harness:
        with pytest.raises(MCPProtocolError) as excinfo:
            await harness.run(
                block(tool="rpc-error", arguments={"code": -32601, "message": "no such tool"})
            )

    assert excinfo.value.code == -32601


# ---------------------------------------------------------------------------
# The invocation convention, and every way to miss it
# ---------------------------------------------------------------------------


async def test_a_prompt_that_is_not_an_invocation_is_refused_with_an_explanation() -> None:
    """A silent refusal is worse than a wrong one, and an error would be wrong twice:
    the request was well-formed ACP, and the turn may already have emitted updates."""
    async with Harness("tools") as harness:
        result = await harness.run(TextContentBlock(type="text", text="please run the tool"))

    assert result.stop_reason == "refusal"
    assert harness.kinds() == ["agent_message_chunk"]
    assert '"tool"' in harness.updates[0].content.text


async def test_an_empty_prompt_is_refused() -> None:
    """It names no tool. Silently completing is the failure IdleTurnExecutor warns about."""
    async with Harness("tools") as harness:
        result = await harness.run()

    assert result.stop_reason == "refusal"


@pytest.mark.parametrize(
    ("payload", "because"),
    [
        ('{"arguments": {}}', "no non-empty string 'tool'"),
        ('{"tool": ""}', "no non-empty string 'tool'"),
        ('{"tool": 3}', "no non-empty string 'tool'"),
        ('{"tool": "echo", "arguments": []}', "must be an object"),
        ('["echo"]', "not an object"),
        ("not json at all", "not JSON"),
    ],
)
async def test_every_malformed_invocation_names_what_is_wrong(payload: str, because: str) -> None:
    async with Harness("tools") as harness:
        result = await harness.run(TextContentBlock(type="text", text=payload))

    assert result.stop_reason == "refusal"
    assert because in harness.updates[0].content.text


async def test_nothing_runs_when_a_later_block_fails_to_parse() -> None:
    """Validate everything, then run anything.

    Tools have side effects; a turn that ran two and then refused leaves no way to undo
    them and no way to tell from outside that it stopped early.
    """
    async with Harness("tools") as harness:
        result = await harness.run(
            block(tool="echo", arguments={"text": "would have run"}),
            TextContentBlock(type="text", text="junk"),
        )

    assert result.stop_reason == "refusal"
    assert harness.kinds() == ["agent_message_chunk"]


# ---------------------------------------------------------------------------
# Choosing a server
# ---------------------------------------------------------------------------


async def test_server_may_be_omitted_when_the_session_opened_exactly_one() -> None:
    """The title is still qualified: it outlives the turn, in the replayed transcript."""
    async with Harness("only") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert harness.updates[0].title == "only/echo"


async def test_server_must_be_named_when_the_session_opened_several() -> None:
    """Guessing which of two servers a client meant is the kind of help nobody wants."""
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "refusal"
    assert "must name a 'server'" in harness.updates[0].content.text


async def test_a_named_server_is_used() -> None:
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(block(tool="echo", server="beta", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert harness.updates[0].title == "beta/echo"


async def test_an_unknown_server_name_is_refused_and_lists_the_real_ones() -> None:
    async with Harness("alpha") as harness:
        result = await harness.run(block(tool="echo", server="nope"))

    assert result.stop_reason == "refusal"
    assert "['alpha']" in harness.updates[0].content.text


async def test_a_session_with_no_servers_cannot_run_a_tool() -> None:
    async with Harness() as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "refusal"
    assert "opened no MCP servers" in harness.updates[0].content.text


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


async def test_every_notification_is_recorded_for_session_load() -> None:
    """`emit` records on the way out, so a replay shows the whole tool call."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert len(harness.session.history) == 3
