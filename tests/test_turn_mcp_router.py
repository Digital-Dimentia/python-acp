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
from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    AudioContentBlock,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    EnvVariable,
    ImageContentBlock,
    McpServerStdio,
    DeniedOutcome,
    PlanCapabilities,
    RequestPermissionResponse,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)

from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPProtocolError
from python_acp.sessions import SessionRegistry
from python_acp.turn_mcp_router import (
    DECLINED_BLOCKS,
    SESSION_CONFIG_OPTIONS,
    SESSION_MODES,
    PERMISSION_OPTIONS,
    McpToolRouterExecutor,
)
from python_acp.turns import TurnContext

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def spec(name: str) -> McpServerStdio:
    return McpServerStdio(
        name=name, command=sys.executable, args=[str(FIXTURE_SERVER)], env=[EnvVariable(name="X", value="1")]
    )


class RecordingClient:
    """A client that records updates and answers permission requests.

    `answers` is a queue of `option_id`s; when it runs dry the client keeps giving the
    last one. `approve` (the default) means every tool runs, which is what most of these
    tests want to be about something else.
    """

    def __init__(self, *answers: str, refuses_permission: bool = False) -> None:
        self.updates: list[Any] = []
        self.permission_requests: list[Any] = []
        self.answers = list(answers) or ["approve"]
        self.refuses_permission = refuses_permission

    async def session_update(self, session_id: str, update: Any, **kwargs) -> None:
        self.updates.append(update)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permission_requests.append(tool_call)
        if self.refuses_permission:
            raise RequestError.method_not_found("session/request_permission")
        chosen = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if chosen == "cancel":
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", optionId=chosen)
        )


def block(**payload: Any) -> TextContentBlock:
    return TextContentBlock(type="text", text=json.dumps(payload))


class Harness:
    """A session with `server_names` MCP servers open, and an executor over them."""

    def __init__(self, *server_names: str, capabilities: Any = None, client: Any = None) -> None:
        self.server_names = server_names
        self.capabilities = capabilities
        self.backends = McpBackendRegistry()
        self.client = client or RecordingClient()
        self.session = SessionRegistry().create("/work")

    async def __aenter__(self) -> Harness:
        await self.backends.open(self.session.session_id, [spec(n) for n in self.server_names])
        self.context = TurnContext(self.session, self.client, self.capabilities)  # type: ignore[arg-type]
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

    def of(self, kind: str) -> list[Any]:
        """Updates of one variant. Every turn now opens with an echo and a command list,
        so selecting by kind reads better than counting from zero."""
        return [u for u in self.updates if u.session_update == kind]

    def tool_calls(self) -> list[Any]:
        return self.of("tool_call") + self.of("tool_call_update")

    def refusal(self) -> str:
        return self.of("agent_message_chunk")[0].content.text


# ---------------------------------------------------------------------------
# Running tools
# ---------------------------------------------------------------------------


async def test_a_prompt_naming_a_tool_runs_it_and_ends_the_turn() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert [u.session_update for u in harness.tool_calls()] == [
        "tool_call", "tool_call_update", "tool_call_update",
    ]


async def test_the_status_transitions_are_real() -> None:
    """`pending` and `in_progress` are separate notifications so a client can render the
    call the moment it is known, then show that the wait has begun."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "hi"}))

    start, began, finished = harness.of("tool_call") + harness.of("tool_call_update")
    assert (start.status, start.title) == ("pending", "tools/echo")
    assert began.status == "in_progress"
    assert finished.status == "completed"
    assert start.tool_call_id == began.tool_call_id == finished.tool_call_id


async def test_the_tools_own_output_reaches_the_client() -> None:
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "from the router"}))

    finished = harness.of("tool_call_update")[-1]
    assert finished.content[0].content.text == "from the router"
    assert finished.raw_output["isError"] is False


async def test_arguments_are_carried_as_raw_input() -> None:
    """A client rendering the call needs to show what it was called with."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert harness.of("tool_call")[0].raw_input == {"text": "hi"}


async def test_arguments_default_to_an_empty_object() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call")[0].raw_input == {}


async def test_several_tool_calls_run_in_order() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(
            block(tool="echo", arguments={"text": "first"}),
            block(tool="echo", arguments={"text": "second"}),
        )

    assert result.stop_reason == "end_turn"
    done = [u for u in harness.of("tool_call_update") if u.content]
    assert [u.content[0].content.text for u in done] == ["first", "second"]


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
    last = harness.of("tool_call_update")[-1]
    assert last.status == "failed"
    assert last.content[0].content.text == "it broke"


async def test_a_failed_tool_does_not_stop_the_calls_after_it() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(
            block(tool="boom"), block(tool="echo", arguments={"text": "still ran"})
        )

    assert result.stop_reason == "end_turn"
    assert [u.status for u in harness.of("tool_call_update")] == [
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
    assert harness.of("tool_call") == []
    assert '"tool"' in harness.refusal()


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
    assert because in harness.refusal()


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
    assert harness.of("tool_call") == []


# ---------------------------------------------------------------------------
# Choosing a server
# ---------------------------------------------------------------------------


async def test_server_may_be_omitted_when_the_session_opened_exactly_one() -> None:
    """The title is still qualified: it outlives the turn, in the replayed transcript."""
    async with Harness("only") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call")[0].title == "only/echo"


async def test_server_must_be_named_when_the_session_opened_several() -> None:
    """Guessing which of two servers a client meant is the kind of help nobody wants."""
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "refusal"
    assert "must name a 'server'" in harness.refusal()


async def test_a_named_server_is_used() -> None:
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(block(tool="echo", server="beta", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call")[0].title == "beta/echo"


async def test_an_unknown_server_name_is_refused_and_lists_the_real_ones() -> None:
    async with Harness("alpha") as harness:
        result = await harness.run(block(tool="echo", server="nope"))

    assert result.stop_reason == "refusal"
    assert "['alpha']" in harness.refusal()


async def test_a_session_with_no_servers_cannot_run_a_tool() -> None:
    async with Harness() as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "refusal"
    assert "opened no MCP servers" in harness.refusal()


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


async def test_every_notification_is_recorded_for_session_load() -> None:
    """`emit` records on the way out, so a replay shows the whole tool call."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert harness.session.history == harness.updates


# ---------------------------------------------------------------------------
# The rest of the variant set (pyacp-hnk.4)
# ---------------------------------------------------------------------------


def accepts_plans() -> ClientCapabilities:
    return ClientCapabilities(plan=PlanCapabilities())


async def test_the_prompt_is_echoed_back_as_user_message_chunks() -> None:
    """Without the echo, a reloaded session shows the agent talking to itself.

    The transcript `session/load` replays is built from what the turn *emitted*.
    """
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo", arguments={"text": "hi"}))

    echoed = harness.of("user_message_chunk")
    assert len(echoed) == 1
    assert json.loads(echoed[0].content.text)["tool"] == "echo"


async def test_the_sessions_tools_are_announced_every_turn() -> None:
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo"))

    commands = harness.of("available_commands_update")[0].available_commands
    assert [c.name for c in commands] == ["tools/echo"]
    assert commands[0].description


async def test_tools_are_announced_even_when_the_prompt_is_refused() -> None:
    """That is the point: a refusal that also says what *could* have been called is
    actionable, and one that only says "that was not an invocation" is not."""
    async with Harness("tools") as harness:
        result = await harness.run(TextContentBlock(type="text", text="prose"))

    assert result.stop_reason == "refusal"
    assert [c.name for c in harness.of("available_commands_update")[0].available_commands] == [
        "tools/echo"
    ]


async def test_commands_from_several_servers_are_qualified_and_ordered() -> None:
    async with Harness("beta", "alpha") as harness:
        await harness.run(block(tool="echo", server="alpha"))

    commands = harness.of("available_commands_update")[0].available_commands
    assert [c.name for c in commands] == ["alpha/echo", "beta/echo"]


async def test_a_plan_is_emitted_and_advanced_as_each_tool_runs() -> None:
    """The plan is complete before the first tool runs, which is what makes it honest."""
    async with Harness("tools", capabilities=accepts_plans()) as harness:
        await harness.run(
            block(tool="echo", arguments={"text": "one"}),
            block(tool="echo", arguments={"text": "two"}),
        )

    plans = [[(e.content, e.status) for e in u.entries] for u in harness.of("plan")]
    assert plans[0] == [("Run tools/echo", "pending"), ("Run tools/echo", "pending")]
    assert plans[-1] == [("Run tools/echo", "completed"), ("Run tools/echo", "completed")]
    assert any(status == "in_progress" for plan in plans for _content, status in plan)


async def test_a_failed_tool_shows_as_a_failed_plan_entry() -> None:
    async with Harness("tools", capabilities=accepts_plans()) as harness:
        await harness.run(block(tool="boom"))

    assert [e.status for e in harness.of("plan")[-1].entries] == ["failed"]


async def test_a_plan_less_client_gets_no_plan_and_everything_else() -> None:
    """`clientCapabilities.plan` gates the *variant*, never the `session/update` call."""
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert harness.of("plan") == []
    assert harness.of("tool_call") and harness.of("available_commands_update")


async def test_a_refused_prompt_emits_no_plan() -> None:
    """There is nothing to plan; the plan is built from parsed invocations."""
    async with Harness("tools", capabilities=accepts_plans()) as harness:
        await harness.run(TextContentBlock(type="text", text="prose"))

    assert harness.of("plan") == []


async def test_the_declined_variants_are_never_emitted() -> None:
    """Recorded in `turns.SESSION_UPDATE_DISPOSITIONS`, asserted here."""
    async with Harness("tools", capabilities=accepts_plans()) as harness:
        await harness.run(block(tool="echo"))

    assert not (
        set(harness.kinds())
        & {"agent_thought_chunk", "usage_update", "session_info_update",
           "plan_content", "plan_removed", "current_mode_update", "config_option_update"}
    )


# ---------------------------------------------------------------------------
# Content block types (pyacp-hnk.3)
# ---------------------------------------------------------------------------


def image() -> ImageContentBlock:
    return ImageContentBlock(type="image", data="aGk=", mimeType="image/png")


def audio() -> AudioContentBlock:
    return AudioContentBlock(type="audio", data="aGk=", mimeType="audio/wav")


def embedded() -> EmbeddedResourceContentBlock:
    return EmbeddedResourceContentBlock(
        type="resource",
        resource=TextResourceContents(uri="file:///notes.txt", text="context"),
    )


def link() -> ResourceContentBlock:
    return ResourceContentBlock(type="resource_link", name="notes", uri="file:///notes.txt")


@pytest.mark.parametrize(
    ("block", "because"),
    [
        (image(), "needs a model to look at it"),
        (audio(), "needs a model to listen to it"),
        (embedded(), "context for a model to read"),
        (link(), "fetch and reason about"),
    ],
    ids=["image", "audio", "resource", "resource_link"],
)
async def test_every_non_text_block_is_declined_by_name(block: Any, because: str) -> None:
    """Not a crash and not a silent drop: each type says what it is and why it is refused.

    All four share one reason — they are context for a model to reason over, and D1 puts
    no model here — but a client debugging a rejected prompt needs to see *which* block.
    """
    async with Harness("tools") as harness:
        result = await harness.run(block)

    assert result.stop_reason == "refusal"
    assert because in harness.refusal()
    assert harness.of("tool_call") == []


async def test_a_declined_block_takes_the_whole_prompt_with_it() -> None:
    """Validate-then-run again: a valid invocation beside an image runs nothing."""
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}), image())

    assert result.stop_reason == "refusal"
    assert harness.of("tool_call") == []


async def test_a_declined_block_is_still_echoed_and_still_gets_the_command_list() -> None:
    """The refusal path stays as informative for a picture as for prose."""
    async with Harness("tools") as harness:
        await harness.run(image())

    # Nothing to echo — the echo is text-only — but the client still learns what exists.
    assert harness.of("user_message_chunk") == []
    assert [c.name for c in harness.of("available_commands_update")[0].available_commands] == [
        "tools/echo"
    ]


def test_the_declined_reasons_cover_every_non_text_block_the_schema_allows() -> None:
    """A schema that grows a sixth block type must not fall through to a vague message."""
    allowed = {"text", "image", "audio", "resource", "resource_link"}

    assert McpToolRouterExecutor.supported_prompt_blocks | set(DECLINED_BLOCKS) == allowed


# ---------------------------------------------------------------------------
# Permission (pyacp-8bv.1)
# ---------------------------------------------------------------------------


async def test_permission_is_asked_before_every_tool_call() -> None:
    """The client that sent the prompt and the human at the client are not always the
    same party, which is what the prompt is for."""
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert len(harness.client.permission_requests) == 1
    assert harness.client.permission_requests[0].title == "tools/echo"


async def test_the_request_arrives_after_pending_and_before_in_progress() -> None:
    """That ordering is what `pending` is for: the client has the call to attach its
    prompt to, and nothing has run yet."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo"))

    assert [u.status for u in harness.tool_calls()] == [
        "pending", "in_progress", "completed",
    ]


async def test_denying_a_call_marks_it_failed_and_does_not_run_it() -> None:
    async with Harness("tools", client=RecordingClient("reject")) as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "never"}))

    assert result.stop_reason == "end_turn"
    last = harness.of("tool_call_update")[-1]
    assert last.status == "failed"
    assert "Denied" in last.content[0].content.text
    # No `in_progress`: the call was never made.
    assert [u.status for u in harness.tool_calls()] == ["pending", "failed"]


async def test_a_denial_does_not_stop_the_calls_after_it() -> None:
    async with Harness("tools", client=RecordingClient("reject", "approve")) as harness:
        result = await harness.run(
            block(tool="echo", arguments={"text": "denied"}),
            block(tool="echo", arguments={"text": "allowed"}),
        )

    assert result.stop_reason == "end_turn"
    assert [u.status for u in harness.of("tool_call_update")] == [
        "failed", "in_progress", "completed",
    ]


async def test_cancelling_the_prompt_ends_the_turn_as_cancelled_not_denied() -> None:
    """The inversion this bead exists to prevent.

    `RequestPermissionResponse.outcome` is `AllowedOutcome` (`"selected"`) or
    `DeniedOutcome` — whose literal is **`"cancelled"`**, despite the class name. Denial
    is a *selected* reject option; the only non-selected answer is a cancelled turn.
    """
    async with Harness("tools", client=RecordingClient("cancel")) as harness:
        result = await harness.run(block(tool="echo"), block(tool="echo"))

    assert result.stop_reason == "cancelled"
    # It stopped at the first call rather than carrying on to the second.
    assert len(harness.client.permission_requests) == 1
    assert harness.of("tool_call_update") == []


async def test_a_cancelled_prompt_leaves_its_plan_entry_unfinished() -> None:
    async with Harness("tools", capabilities=accepts_plans(), client=RecordingClient("cancel")) as harness:
        await harness.run(block(tool="echo"))

    assert [e.status for e in harness.of("plan")[-1].entries] == ["pending"]


@pytest.mark.parametrize(
    ("answer", "expected"), [("approve_for_session", True), ("reject", False)]
)
async def test_only_the_always_options_are_remembered(answer: str, expected: bool) -> None:
    """`allow_always` / `reject_always` are the two that write; the once-variants do not."""
    async with Harness("tools", client=RecordingClient(answer)) as harness:
        await harness.run(block(tool="echo"))

        remembered = harness.session.remembered_permissions
    assert remembered.get("tools/echo") is (expected if answer.endswith("session") else None)


async def test_an_always_answer_is_not_asked_again_this_session() -> None:
    """The scope is the session — the SDK's own option is named "Approve for session"."""
    async with Harness("tools", client=RecordingClient("approve_for_session")) as harness:
        await harness.run(block(tool="echo"))
        await harness.run(block(tool="echo"))

    assert len(harness.client.permission_requests) == 1
    assert harness.session.remembered_permissions == {"tools/echo": True}


async def test_a_remembered_answer_is_per_tool_not_per_session() -> None:
    async with Harness("tools", client=RecordingClient("approve_for_session")) as harness:
        await harness.run(block(tool="echo"))
        await harness.run(block(tool="boom"))

    assert [c.title for c in harness.client.permission_requests] == ["tools/echo", "tools/boom"]


async def test_a_client_that_cannot_ask_a_human_gets_the_tool_run_anyway() -> None:
    """Corrected under interop evidence (`pyacp-6ni.4`).

    The first implementation refused the turn, on the reasoning that
    `session/request_permission` is mandatory so a client answering `-32601` is broken.
    The SDK's own `examples/client.py` answers exactly that. Proceeding is not assuming
    consent from nowhere: **the client named this tool in `session/prompt` itself**, so
    the authorization already exists and the prompt was a courtesy to a human who might
    be watching.
    """
    async with Harness("tools", client=RecordingClient(refuses_permission=True)) as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert [u.status for u in harness.tool_calls()] == ["pending", "in_progress", "completed"]


async def test_proceeding_without_a_human_is_announced_once_per_session() -> None:
    """Once, not silently and not per call, so a transcript says plainly why nothing was
    asked."""
    async with Harness("tools", client=RecordingClient(refuses_permission=True)) as harness:
        await harness.run(block(tool="echo"))
        await harness.run(block(tool="echo"))

    announcements = [
        u for u in harness.of("agent_message_chunk") if "nobody to ask" in u.content.text
    ]
    assert len(announcements) == 1
    assert len(harness.client.permission_requests) == 2


async def test_an_option_we_never_offered_is_not_treated_as_consent() -> None:
    async with Harness("tools", client=RecordingClient("invented")) as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call_update")[-1].status == "failed"


def test_all_four_permission_kinds_are_offered() -> None:
    """The SDK's default set omits `reject_always`, so a user could say "always yes" but
    not "always no" and would be asked again about a tool they had turned down. All four
    kinds are in the protocol, so the fourth is added rather than worked around."""
    assert {o.kind for o in PERMISSION_OPTIONS} == {
        "allow_once", "allow_always", "reject_once", "reject_always",
    }
    assert len(PERMISSION_OPTIONS) == 4


async def test_rejecting_for_the_session_is_remembered_too() -> None:
    async with Harness("tools", client=RecordingClient("reject_for_session")) as harness:
        await harness.run(block(tool="echo"))
        await harness.run(block(tool="echo"))

    assert len(harness.client.permission_requests) == 1
    assert harness.session.remembered_permissions == {"tools/echo": False}
    assert all(u.status != "in_progress" for u in harness.of("tool_call_update"))


async def test_a_fork_does_not_inherit_a_decision_it_can_change() -> None:
    """A fork answering "always allow" must not decide for its parent, and vice versa."""
    async with Harness("tools", client=RecordingClient("approve_for_session")) as harness:
        await harness.run(block(tool="echo"))
        forked = harness.session.fork("child")

        forked.remembered_permissions["tools/echo"] = False

    assert harness.session.remembered_permissions == {"tools/echo": True}


# ---------------------------------------------------------------------------
# Result content mapping, against the real server (pyacp-eg1.1)
# ---------------------------------------------------------------------------


async def test_every_mcp_content_type_reaches_the_client_as_an_acp_block() -> None:
    """Against the fixture server rather than a hand-built dict.

    A mapping that works on dicts we wrote and not on what a server actually sends would
    pass every unit test in `tests/test_mcp_content.py`.
    """
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="every-content"))

    assert result.stop_reason == "end_turn"
    content = [c.content for c in harness.of("tool_call_update")[-1].content]
    assert [c.type for c in content] == [
        "text", "image", "audio", "resource", "resource", "resource_link", "text", "text",
    ]
    # The two trailing text blocks are the placeholders for the unmappable pair.
    assert all(c.text.startswith("[python-acp could not render") for c in content[-2:])
    assert content[0].annotations.audience == ["user"]


async def test_the_servers_original_result_is_still_there_verbatim() -> None:
    """What makes the placeholder cheap: nothing is lost, only unrendered."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="every-content"))

    raw = harness.of("tool_call_update")[-1].raw_output
    assert [b["type"] for b in raw["content"]][-2:] == ["chart", "image"]


# ---------------------------------------------------------------------------
# Session modes (pyacp-fln.2)
# ---------------------------------------------------------------------------


def in_mode(harness: Harness, mode_id: str) -> None:
    harness.session.modes = SESSION_MODES.model_copy(deep=True)
    harness.session.set_mode(mode_id)


def test_every_mode_changes_what_a_turn_does() -> None:
    """The bead is explicit: do not invent modes with no behavioural difference.

    Each of the three differs on at least one of the two axes a turn has.
    """
    assert {m.id for m in SESSION_MODES.available_modes} == {"execute", "dry-run", "auto-approve"}
    assert SESSION_MODES.current_mode_id == "execute"
    assert all(m.description for m in SESSION_MODES.available_modes)


async def test_dry_run_reports_the_call_and_runs_nothing() -> None:
    async with Harness("tools") as harness:
        in_mode(harness, "dry-run")
        result = await harness.run(block(tool="echo", arguments={"text": "would run"}))

    assert result.stop_reason == "end_turn"
    start = harness.of("tool_call")[0]
    assert start.title == "tools/echo"
    # The arguments are the point of a preview.
    assert start.raw_input == {"text": "would run"}
    final = harness.of("tool_call_update")[-1]
    assert "[dry-run]" in final.content[0].content.text
    assert harness.client.permission_requests == []


async def test_a_dry_run_completion_carries_no_raw_output() -> None:
    """ACP has no "skipped" status, so `completed` is the chosen encoding — and the
    absent `rawOutput` is the second signal that nothing actually ran. A real completion
    always carries the server's result."""
    async with Harness("tools") as harness:
        in_mode(harness, "dry-run")
        await harness.run(block(tool="echo"))

    assert harness.of("tool_call_update")[-1].raw_output is None


async def test_dry_run_never_reaches_the_backend() -> None:
    """The assertion that matters: `boom` would report a failure if it were called."""
    async with Harness("tools") as harness:
        in_mode(harness, "dry-run")
        await harness.run(block(tool="boom"))

    assert [u.status for u in harness.of("tool_call_update")] == ["completed"]


async def test_auto_approve_runs_without_asking() -> None:
    """Choosing the mode is the consent."""
    async with Harness("tools", client=RecordingClient("reject")) as harness:
        in_mode(harness, "auto-approve")
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert harness.client.permission_requests == []
    assert harness.of("tool_call_update")[-1].status == "completed"


async def test_execute_is_the_default_and_still_asks() -> None:
    async with Harness("tools") as harness:
        in_mode(harness, "execute")
        await harness.run(block(tool="echo"))

    assert len(harness.client.permission_requests) == 1


async def test_a_session_with_no_modes_behaves_as_execute() -> None:
    """A session created by an agent whose executor advertises none has `modes = None`,
    and the safe default is the one that asks."""
    async with Harness("tools") as harness:
        assert harness.session.modes is None
        await harness.run(block(tool="echo"))

    assert len(harness.client.permission_requests) == 1


async def test_switching_mid_session_takes_effect_on_the_next_turn() -> None:
    async with Harness("tools") as harness:
        in_mode(harness, "execute")
        await harness.run(block(tool="echo"))
        harness.session.set_mode("dry-run")
        await harness.run(block(tool="echo"))

    assert len(harness.client.permission_requests) == 1
    assert [u.status for u in harness.of("tool_call_update")] == [
        "in_progress", "completed", "completed",
    ]


# ---------------------------------------------------------------------------
# Config options (pyacp-fln.3)
# ---------------------------------------------------------------------------


def configured(harness: Harness, config_id: str, value: Any) -> None:
    harness.session.config_options = tuple(
        o.model_copy(deep=True) for o in SESSION_CONFIG_OPTIONS
    )
    harness.session.set_config_option(config_id, value)


def test_one_option_of_each_variant_is_exposed() -> None:
    """The SDK discriminates the request on `type`; an implementation that only ever saw
    booleans would not have exercised the other branch."""
    by_id = {o.id: o for o in SESSION_CONFIG_OPTIONS}

    assert by_id["announce-tools"].type == "boolean"
    assert by_id["on-tool-failure"].type == "select"
    assert all(o.description for o in SESSION_CONFIG_OPTIONS)


async def test_turning_off_the_announcement_skips_the_command_list() -> None:
    """And skips the `tools/list` behind it, which is the point of the option."""
    async with Harness("tools") as harness:
        configured(harness, "announce-tools", False)
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "end_turn"
    assert harness.of("available_commands_update") == []
    assert harness.of("tool_call")


async def test_the_announcement_is_on_by_default() -> None:
    async with Harness("tools") as harness:
        configured(harness, "announce-tools", True)
        await harness.run(block(tool="echo"))

    assert len(harness.of("available_commands_update")) == 1


async def test_stopping_on_failure_ends_the_turn_at_the_failed_call() -> None:
    async with Harness("tools") as harness:
        configured(harness, "on-tool-failure", "stop")
        result = await harness.run(block(tool="boom"), block(tool="echo"))

    assert result.stop_reason == "end_turn"
    assert [u.status for u in harness.of("tool_call_update")] == ["in_progress", "failed"]


async def test_stopping_leaves_the_remaining_plan_entries_pending() -> None:
    """Which is what says *where* it stopped: ACP has no stopReason for "a tool failed",
    and a refusal would claim nothing ran."""
    async with Harness("tools", capabilities=accepts_plans()) as harness:
        configured(harness, "on-tool-failure", "stop")
        await harness.run(block(tool="boom"), block(tool="echo"))

    assert [e.status for e in harness.of("plan")[-1].entries] == ["failed", "pending"]


async def test_continuing_is_the_default() -> None:
    async with Harness("tools") as harness:
        configured(harness, "on-tool-failure", "continue")
        await harness.run(block(tool="boom"), block(tool="echo"))

    assert [u.status for u in harness.of("tool_call_update")] == [
        "in_progress", "failed", "in_progress", "completed",
    ]


async def test_a_session_with_no_options_takes_the_defaults() -> None:
    async with Harness("tools") as harness:
        assert harness.session.config_options == ()
        await harness.run(block(tool="boom"), block(tool="echo"))

    assert len(harness.of("available_commands_update")) == 1
    assert [u.status for u in harness.of("tool_call_update")][-1] == "completed"
