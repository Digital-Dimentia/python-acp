"""Tests for the deterministic MCP tool-router.

Against the real `tests/fixtures/mock_mcp_server.py` subprocess, per the repo's
convention: the thing under test is a tool call actually running and its result actually
reaching a `session/update`, and a mock backend would prove neither.

The parsing tests are exhaustive on purpose. The invocation convention is invented by
this module — nothing in the ACP spec describes it — so it is the one contract a client
codes against, and every refusal it can produce is part of that contract.
"""

from __future__ import annotations

import inspect
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
    FileSystemCapabilities,
    ImageContentBlock,
    McpServerStdio,
    DeniedOutcome,
    PlanCapabilities,
    ReadTextFileResponse,
    RequestPermissionResponse,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)

from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPProtocolError
from python_acp.sessions import SessionRegistry
from python_acp.turn_mcp_router import (
    CONVENTION,
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


class FilesystemClient(RecordingClient):
    """A client whose `fs/*` methods really touch a directory.

    Real files, for the same reason the MCP backend is a real subprocess: the whole point
    of routing a read through the client is that the bytes come from somewhere this
    process never opened, and a stub returning a canned string would prove nothing about
    `line`, `limit`, or which path was actually asked for.
    """

    def __init__(
        self,
        *answers: str,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        super().__init__(*answers)
        self.read_error = read_error
        self.write_error = write_error
        self.reads: list[tuple[str, int | None, int | None]] = []
        self.writes: list[tuple[str, str]] = []

    async def read_text_file(
        self, session_id: str, path: str, line: int | None = None, limit: int | None = None, **kw
    ) -> ReadTextFileResponse:
        self.reads.append((path, line, limit))
        if self.read_error is not None:
            raise self.read_error
        lines = Path(path).read_text().splitlines(keepends=True)
        start = line - 1 if line else 0
        window = lines[start:] if limit is None else lines[start : start + limit]
        return ReadTextFileResponse(content="".join(window))

    async def write_text_file(self, session_id: str, path: str, content: str, **kw) -> None:
        self.writes.append((path, content))
        if self.write_error is not None:
            raise self.write_error
        Path(path).write_text(content)
        return None


def has_fs(*, read: bool = True, write: bool = True) -> ClientCapabilities:
    return ClientCapabilities(fs=FileSystemCapabilities(readTextFile=read, writeTextFile=write))


class Harness:
    """A session with `server_names` MCP servers open, and an executor over them."""

    def __init__(
        self,
        *server_names: str,
        capabilities: Any = None,
        client: Any = None,
        cwd: str = "/work",
    ) -> None:
        self.server_names = server_names
        self.capabilities = capabilities
        self.backends = McpBackendRegistry()
        self.client = client or RecordingClient()
        self.session = SessionRegistry().create(cwd)

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


# ---------------------------------------------------------------------------
# The client's filesystem (pyacp-8bv.2)
# ---------------------------------------------------------------------------


def fs_harness(tmp_path: Path, *, capabilities: Any = None, client: Any = None) -> Harness:
    """A session rooted at `tmp_path`, so containment has real directories to enforce."""
    return Harness(
        "tools",
        capabilities=has_fs() if capabilities is None else capabilities,
        client=client if client is not None else FilesystemClient(),
        cwd=str(tmp_path),
    )


async def test_a_file_is_read_through_the_client_into_a_tool_argument(tmp_path: Path) -> None:
    """The bytes reach the tool without this process ever opening the file."""
    source = tmp_path / "in.txt"
    source.write_text("from the client's disk")

    async with fs_harness(tmp_path) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": str(source)}}))

    assert result.stop_reason == "end_turn"
    assert harness.client.reads == [(str(source.resolve()), None, None)]
    assert harness.of("tool_call_update")[-1].content[0].content.text == "from the client's disk"


async def test_line_and_limit_ask_for_a_window_rather_than_the_whole_file(
    tmp_path: Path,
) -> None:
    """Supporting them is the difference between a window and always paying for the file."""
    source = tmp_path / "in.txt"
    source.write_text("one\ntwo\nthree\nfour\nfive\n")

    async with fs_harness(tmp_path) as harness:
        await harness.run(
            block(tool="echo", read={"text": {"path": str(source), "line": 2, "limit": 2}})
        )

    assert harness.client.reads == [(str(source.resolve()), 2, 2)]
    assert harness.of("tool_call_update")[-1].content[0].content.text == "two\nthree\n"


async def test_the_resolved_arguments_are_republished_as_raw_input(tmp_path: Path) -> None:
    """`pending` shows what the client asked for; `in_progress` shows what actually went to
    the tool, which is the only place the file's content is visible."""
    source = tmp_path / "in.txt"
    source.write_text("substituted")

    async with fs_harness(tmp_path) as harness:
        await harness.run(block(tool="echo", read={"text": {"path": str(source)}}))

    assert harness.of("tool_call")[0].raw_input == {}
    assert harness.of("tool_call_update")[0].raw_input == {"text": "substituted"}


async def test_a_tools_output_is_written_back_through_the_client(tmp_path: Path) -> None:
    destination = tmp_path / "out.txt"

    async with fs_harness(tmp_path) as harness:
        result = await harness.run(
            block(tool="echo", arguments={"text": "written"}, write={"path": str(destination)})
        )

    assert result.stop_reason == "end_turn"
    assert harness.client.writes == [(str(destination.resolve()), "written")]
    assert destination.read_text() == "written"
    assert "Wrote 7 characters" in harness.of("tool_call_update")[-1].content[-1].content.text


async def test_a_file_round_trips_through_a_tool_in_one_invocation(tmp_path: Path) -> None:
    source = tmp_path / "in.txt"
    source.write_text("round trip")
    destination = tmp_path / "out.txt"

    async with fs_harness(tmp_path) as harness:
        result = await harness.run(
            block(
                tool="echo",
                read={"text": {"path": str(source)}},
                write={"path": str(destination)},
            )
        )

    assert result.stop_reason == "end_turn"
    assert destination.read_text() == "round trip"


async def test_the_client_is_asked_for_the_resolved_path_not_the_one_it_wrote(
    tmp_path: Path,
) -> None:
    """`require_contained` hands back the resolved path deliberately: asking the client to
    re-walk a symlink would be asking it to open something this session never checked."""
    real = tmp_path / "real.txt"
    real.write_text("behind a link")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    async with fs_harness(tmp_path) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": str(link)}}))

    assert result.stop_reason == "end_turn"
    assert harness.client.reads == [(str(real.resolve()), None, None)]


async def test_a_link_that_points_outside_the_roots_is_refused(tmp_path: Path) -> None:
    """The case a lexical check passes and this one does not."""
    outside = tmp_path.parent / "outside-8bv2.txt"
    outside.write_text("secret")
    inside = tmp_path / "inside"
    inside.mkdir()
    link = inside / "link.txt"
    link.symlink_to(outside)

    async with Harness(
        "tools", capabilities=has_fs(), client=FilesystemClient(), cwd=str(inside)
    ) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": str(link)}}))

    assert result.stop_reason == "refusal"
    assert "outside this session's directories" in harness.refusal()
    assert harness.client.reads == []


async def test_a_path_outside_the_roots_refuses_the_whole_turn(tmp_path: Path) -> None:
    """Validate-then-run: a valid first block runs nothing when the second names a path
    this session may not touch."""
    async with fs_harness(tmp_path) as harness:
        result = await harness.run(
            block(tool="echo", arguments={"text": "would have run"}),
            block(tool="echo", write={"path": "/etc/python-acp-should-never-write"}),
        )

    assert result.stop_reason == "refusal"
    assert harness.of("tool_call") == []
    assert harness.client.writes == []


async def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    async with fs_harness(tmp_path) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": "notes.txt"}}))

    assert result.stop_reason == "refusal"
    assert "must be an absolute path" in harness.refusal()


# ---------------------------------------------------------------------------
# The gate: a client with no filesystem is not a bug
# ---------------------------------------------------------------------------


async def test_reading_without_the_capability_is_a_refusal_not_an_internal_error(
    tmp_path: Path,
) -> None:
    """`UngatedClientCallError` means *we* reached for something unadvertised and maps to
    `-32603`. A client that simply has no filesystem has done nothing wrong, so it gets a
    refusal — and no convention footer, because the convention was followed."""
    source = tmp_path / "in.txt"
    source.write_text("unreachable")

    async with fs_harness(tmp_path, capabilities=ClientCapabilities()) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": str(source)}}))

    assert result.stop_reason == "refusal"
    assert "fs.readTextFile" in harness.refusal()
    assert CONVENTION not in harness.refusal()
    assert harness.client.reads == []
    assert harness.of("tool_call") == []


async def test_a_read_grant_does_not_satisfy_a_write(tmp_path: Path) -> None:
    """The two `fs` booleans are independent, and this is the asymmetry that proves it."""
    async with fs_harness(tmp_path, capabilities=has_fs(read=True, write=False)) as harness:
        result = await harness.run(
            block(tool="echo", arguments={"text": "hi"}, write={"path": str(tmp_path / "o.txt")})
        )

    assert result.stop_reason == "refusal"
    assert "fs.writeTextFile" in harness.refusal()
    assert harness.client.writes == []


async def test_a_write_grant_does_not_satisfy_a_read(tmp_path: Path) -> None:
    source = tmp_path / "in.txt"
    source.write_text("nope")

    async with fs_harness(tmp_path, capabilities=has_fs(read=False, write=True)) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": str(source)}}))

    assert result.stop_reason == "refusal"
    assert "fs.readTextFile" in harness.refusal()


async def test_a_prompt_that_asks_for_no_files_is_unaffected_by_a_missing_capability(
    tmp_path: Path,
) -> None:
    """The gate is only reached by an invocation that names a file."""
    async with fs_harness(tmp_path, capabilities=ClientCapabilities()) as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"


async def test_an_empty_read_object_needs_no_capability(tmp_path: Path) -> None:
    """`"read": {}` names no file, so refusing it would refuse a request never made."""
    async with fs_harness(tmp_path, capabilities=ClientCapabilities()) as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}, read={}))

    assert result.stop_reason == "end_turn"


def test_the_gate_is_still_an_assertion_at_the_call_site() -> None:
    """Parsing refuses first, so reaching the call with a shut gate is our conformance bug.

    Asserted as a *design* fact rather than through the wire: there is no prompt that can
    produce it, which is the point.
    """
    source = inspect.getsource(McpToolRouterExecutor)

    assert "context.require(Gate.READ_TEXT_FILE)" in source
    assert "context.require(Gate.WRITE_TEXT_FILE)" in source


# ---------------------------------------------------------------------------
# A client that errors on the call does not crash the turn
# ---------------------------------------------------------------------------


async def test_a_client_that_errors_on_the_read_fails_the_call_not_the_turn(
    tmp_path: Path,
) -> None:
    source = tmp_path / "gone.txt"
    source.write_text("x")
    client = FilesystemClient(read_error=RequestError(-32603, "no such file"))

    async with fs_harness(tmp_path, client=client) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": str(source)}}))

    assert result.stop_reason == "end_turn"
    last = harness.of("tool_call_update")[-1]
    assert last.status == "failed"
    assert "-32603: no such file" in last.content[0].content.text
    # The tool was never called: its argument never arrived.
    assert [u.status for u in harness.tool_calls()] == ["pending", "failed"]


async def test_a_read_failure_does_not_stop_the_calls_after_it(tmp_path: Path) -> None:
    source = tmp_path / "gone.txt"
    source.write_text("x")
    client = FilesystemClient(read_error=RuntimeError("the connection dropped"))

    async with fs_harness(tmp_path, client=client) as harness:
        result = await harness.run(
            block(tool="echo", read={"text": {"path": str(source)}}),
            block(tool="echo", arguments={"text": "still ran"}),
        )

    assert result.stop_reason == "end_turn"
    assert [u.status for u in harness.of("tool_call_update")] == [
        "failed", "in_progress", "completed",
    ]
    assert "RuntimeError: the connection dropped" in harness.of("tool_call_update")[0].content[
        0
    ].content.text


async def test_a_read_failure_still_stops_the_turn_under_on_tool_failure_stop(
    tmp_path: Path,
) -> None:
    """A failed file call is a failed call, so the option that stops on one stops on this."""
    source = tmp_path / "gone.txt"
    source.write_text("x")

    async with fs_harness(
        tmp_path, client=FilesystemClient(read_error=RuntimeError("nope"))
    ) as harness:
        configured(harness, "on-tool-failure", "stop")
        result = await harness.run(
            block(tool="echo", read={"text": {"path": str(source)}}), block(tool="echo")
        )

    assert result.stop_reason == "end_turn"
    assert len(harness.of("tool_call")) == 1


async def test_a_client_that_errors_on_the_write_marks_the_call_failed(tmp_path: Path) -> None:
    """The tool ran and its output is still in the update; only the write did not happen."""
    client = FilesystemClient(write_error=RequestError(-32603, "read-only filesystem"))

    async with fs_harness(tmp_path, client=client) as harness:
        result = await harness.run(
            block(
                tool="echo",
                arguments={"text": "produced"},
                write={"path": str(tmp_path / "out.txt")},
            )
        )

    assert result.stop_reason == "end_turn"
    last = harness.of("tool_call_update")[-1]
    assert last.status == "failed"
    assert last.content[0].content.text == "produced"
    assert "-32603: read-only filesystem" in last.content[-1].content.text


async def test_a_client_that_answers_a_read_without_text_is_not_trusted(
    tmp_path: Path,
) -> None:
    class Nonsense(FilesystemClient):
        async def read_text_file(self, session_id, path, line=None, limit=None, **kw):
            return ReadTextFileResponse.model_construct(content=None)

    source = tmp_path / "in.txt"
    source.write_text("x")

    async with fs_harness(tmp_path, client=Nonsense()) as harness:
        result = await harness.run(block(tool="echo", read={"text": {"path": str(source)}}))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call_update")[-1].status == "failed"
    assert "without text content" in harness.of("tool_call_update")[-1].content[0].content.text


# ---------------------------------------------------------------------------
# When a write is skipped on purpose
# ---------------------------------------------------------------------------


async def test_a_failed_tool_is_not_written_to_the_file(tmp_path: Path) -> None:
    """Writing a tool's error message into the file the client asked us to fill is worse
    than not writing."""
    destination = tmp_path / "out.txt"

    async with fs_harness(tmp_path) as harness:
        result = await harness.run(block(tool="boom", write={"path": str(destination)}))

    assert result.stop_reason == "end_turn"
    assert harness.client.writes == []
    assert not destination.exists()
    assert "was not written: the tool failed" in harness.of("tool_call_update")[-1].content[
        -1
    ].content.text


async def test_a_tool_with_no_text_output_is_not_written(tmp_path: Path) -> None:
    """Truncating a file to nothing because the tool answered with a picture is a
    destructive surprise, so it is refused and said out loud."""
    destination = tmp_path / "out.txt"
    destination.write_text("do not lose me")

    async with fs_harness(tmp_path) as harness:
        result = await harness.run(block(tool="picture", write={"path": str(destination)}))

    assert result.stop_reason == "end_turn"
    assert destination.read_text() == "do not lose me"
    assert harness.of("tool_call_update")[-1].status == "failed"
    assert "no text content" in harness.of("tool_call_update")[-1].content[-1].content.text


async def test_a_denied_call_touches_no_files(tmp_path: Path) -> None:
    """Permission is asked before the read: the client approving the call is what
    authorises pulling its files."""
    source = tmp_path / "in.txt"
    source.write_text("private")

    async with fs_harness(tmp_path, client=FilesystemClient("reject")) as harness:
        result = await harness.run(
            block(
                tool="echo",
                read={"text": {"path": str(source)}},
                write={"path": str(tmp_path / "out.txt")},
            )
        )

    assert result.stop_reason == "end_turn"
    assert harness.client.reads == [] and harness.client.writes == []


async def test_a_dry_run_names_the_files_it_would_touch_and_touches_none(
    tmp_path: Path,
) -> None:
    source = tmp_path / "in.txt"
    source.write_text("preview")
    destination = tmp_path / "out.txt"

    async with fs_harness(tmp_path) as harness:
        in_mode(harness, "dry-run")
        result = await harness.run(
            block(
                tool="echo",
                read={"text": {"path": str(source)}},
                write={"path": str(destination)},
            )
        )

    assert result.stop_reason == "end_turn"
    said = harness.of("tool_call_update")[-1].content[0].content.text
    assert str(source.resolve()) in said and str(destination.resolve()) in said
    assert harness.client.reads == [] and harness.client.writes == []
    assert not destination.exists()


async def test_the_files_a_call_touches_are_reported_as_locations(tmp_path: Path) -> None:
    """`ToolCall.locations` is the schema's own field for this, and it is what lets a
    client show — or follow — which files a call is about."""
    source = tmp_path / "in.txt"
    source.write_text("x")
    destination = tmp_path / "out.txt"

    async with fs_harness(tmp_path) as harness:
        await harness.run(
            block(
                tool="echo",
                read={"text": {"path": str(source), "line": 3}},
                write={"path": str(destination)},
            )
        )

    locations = harness.of("tool_call")[0].locations
    assert [(loc.path, loc.line) for loc in locations] == [
        (str(source.resolve()), 3),
        (str(destination.resolve()), None),
    ]


async def test_a_call_that_touches_no_files_reports_no_locations() -> None:
    """`None`, not `[]`: an empty list would claim a call has locations and they are none."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo"))

    assert harness.of("tool_call")[0].locations is None


# ---------------------------------------------------------------------------
# Every way to get `read` and `write` wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "because"),
    [
        ({"tool": "echo", "read": []}, "'read' must be an object"),
        ({"tool": "echo", "read": {"text": "/abs/x"}}, "must be an object with a 'path'"),
        ({"tool": "echo", "read": {"text": {}}}, "'read.text.path' must be a non-empty string"),
        ({"tool": "echo", "read": {"text": {"path": ""}}}, "non-empty string"),
        ({"tool": "echo", "read": {"": {"path": "/abs/x"}}}, "non-empty argument name"),
        ({"tool": "echo", "write": "/abs/x"}, "'write' must be an object with a 'path'"),
        ({"tool": "echo", "write": {}}, "'write.path' must be a non-empty string"),
    ],
    ids=["read-list", "spec-string", "no-path", "empty-path", "empty-argument", "write-string", "write-no-path"],
)
async def test_every_malformed_file_clause_names_what_is_wrong(
    tmp_path: Path, payload: dict[str, Any], because: str
) -> None:
    async with fs_harness(tmp_path) as harness:
        result = await harness.run(TextContentBlock(type="text", text=json.dumps(payload)))

    assert result.stop_reason == "refusal"
    assert because in harness.refusal()


@pytest.mark.parametrize("bad", [-1, "2", True, 1.5], ids=["negative", "string", "bool", "float"])
async def test_line_must_be_a_non_negative_integer(tmp_path: Path, bad: Any) -> None:
    """`bool` is in the list because it is an `int` in Python and `{"line": true}` is not
    a line number."""
    async with fs_harness(tmp_path) as harness:
        result = await harness.run(
            block(tool="echo", read={"text": {"path": str(tmp_path / "x"), "line": bad}})
        )

    assert result.stop_reason == "refusal"
    assert "'read.text.line' must be a non-negative integer" in harness.refusal()


async def test_limit_is_bounded_the_same_way(tmp_path: Path) -> None:
    async with fs_harness(tmp_path) as harness:
        result = await harness.run(
            block(tool="echo", read={"text": {"path": str(tmp_path / "x"), "limit": -3}})
        )

    assert result.stop_reason == "refusal"
    assert "'read.text.limit' must be a non-negative integer" in harness.refusal()


async def test_an_argument_named_in_both_arguments_and_read_is_refused(tmp_path: Path) -> None:
    """Two sources for one value is exactly the guess `server` already refuses to make."""
    async with fs_harness(tmp_path) as harness:
        result = await harness.run(
            block(
                tool="echo",
                arguments={"text": "inline"},
                read={"text": {"path": str(tmp_path / "in.txt")}},
            )
        )

    assert result.stop_reason == "refusal"
    assert "named in both 'arguments' and 'read'" in harness.refusal()
