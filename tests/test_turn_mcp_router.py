"""Tests for the deterministic MCP tool-router.

Against the real `tests/fixtures/mock_mcp_server.py` subprocess, per the repo's
convention: the thing under test is a tool call actually running and its result actually
reaching a `session/update`, and a mock backend would prove neither.

The parsing tests are exhaustive on purpose. The invocation convention is invented by
this module — nothing in the ACP spec describes it — so it is the one contract a client
codes against, and every refusal it can produce is part of that contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    AudioContentBlock,
    ClientCapabilities,
    CreateTerminalResponse,
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
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    TextResourceContents,
    WaitForTerminalExitResponse,
)

from python_acp.commands import CommandError, parse_command
from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPProtocolError
from python_acp.sessions import SessionRegistry
from python_acp.terminals import DEFAULT_OUTPUT_BYTE_LIMIT, TerminalRegistry
from test_markdown import assert_markdown_safe
from python_acp.turn_mcp_router import (
    _BUILTIN_COMMANDS,
    CONVENTION,
    DECLINED_BLOCKS,
    SESSION_CONFIG_OPTIONS,
    SESSION_MODES,
    PERMISSION_OPTIONS,
    TOOL_META_KEY,
    McpToolRouterExecutor,
    _tool_meta,
)
from python_acp.turns import TurnContext

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"

#: The commands the executor answers itself, in announcement order. Taken from the
#: manifest rather than written out, so a test that asserts a whole palette is asserting
#: *where the built-ins sit* — after the session's tools — rather than re-encoding a list
#: that would then need editing every time one is added.
BUILTINS = [command.name for command in _BUILTIN_COMMANDS]


def spec(name: str, **env: str) -> McpServerStdio:
    """One fixture server. Keyword arguments become environment variables for *that*
    subprocess, which is how a session can hold a server declaring the prompts capability
    beside one that does not — `monkeypatch.setenv` would reach both."""
    return McpServerStdio(
        name=name,
        command=sys.executable,
        args=[str(FIXTURE_SERVER)],
        env=[
            EnvVariable(name="X", value="1"),
            *(EnvVariable(name=key, value=value) for key, value in env.items()),
        ],
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


def has_terminal(*, read: bool = False, write: bool = False) -> ClientCapabilities:
    """`clientCapabilities.terminal` — one boolean covering all five methods."""
    return ClientCapabilities(
        terminal=True,
        fs=FileSystemCapabilities(readTextFile=read, writeTextFile=write),
    )


class TerminalClient(RecordingClient):
    """A client whose `terminal/*` methods really run processes.

    Real subprocesses, for the same reason `FilesystemClient` uses real files and the MCP
    backend is a real server: the point of a terminal is that a command runs somewhere
    this process does not control, and a stub handing back a canned string would prove
    nothing about `outputByteLimit`, about an exit status, or about a kill actually
    reaching a running process.

    Shared with `tests/test_terminals.py`, which drives the registry directly.
    """

    def __init__(
        self,
        *answers: str,
        create_error: Exception | None = None,
        release_error: Exception | None = None,
    ) -> None:
        super().__init__(*answers)
        self.create_error = create_error
        self.release_error = release_error
        self.created: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self.released: list[str] = []
        #: Every process ever started, by terminal id, and never removed — a test asking
        #: "did the kill land" needs the handle after the terminal is gone.
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        #: Set when `wait_for_terminal_exit` is actually in flight, so a cancellation test
        #: can cancel *during* the wait rather than racing the create.
        self.waiting = asyncio.Event()
        self._live: dict[str, asyncio.subprocess.Process] = {}
        self._readers: dict[str, asyncio.Task[None]] = {}
        self._buffers: dict[str, bytearray] = {}
        self._truncated: dict[str, bool] = {}
        self._limits: dict[str, int] = {}

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kw: Any,
    ) -> CreateTerminalResponse:
        self.created.append(
            {
                "session_id": session_id,
                "command": command,
                "args": list(args or []),
                "env": {variable.name: variable.value for variable in env or []},
                "cwd": cwd,
                "output_byte_limit": output_byte_limit,
            }
        )
        if self.create_error is not None:
            raise self.create_error
        process = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=cwd,
            env={**os.environ, **{variable.name: variable.value for variable in env or []}},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        terminal_id = f"terminal-{len(self.created)}"
        self.processes[terminal_id] = process
        self._live[terminal_id] = process
        self._buffers[terminal_id] = bytearray()
        self._truncated[terminal_id] = False
        self._limits[terminal_id] = (
            DEFAULT_OUTPUT_BYTE_LIMIT if output_byte_limit is None else output_byte_limit
        )
        self._readers[terminal_id] = asyncio.create_task(self._drain(terminal_id, process))
        return CreateTerminalResponse(terminalId=terminal_id)

    async def _drain(self, terminal_id: str, process: asyncio.subprocess.Process) -> None:
        """Keep the last `outputByteLimit` bytes, truncating from the beginning.

        Which is what the schema tells a client to do, and is the half of the contract an
        agent cannot verify from its own side.
        """
        assert process.stdout is not None
        buffer = self._buffers[terminal_id]
        limit = self._limits[terminal_id]
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                return
            buffer += chunk
            if len(buffer) > limit:
                del buffer[: len(buffer) - limit]
                self._truncated[terminal_id] = True

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kw: Any
    ) -> WaitForTerminalExitResponse:
        self.waiting.set()
        code = await self._live[terminal_id].wait()
        await self._readers[terminal_id]
        return WaitForTerminalExitResponse(**_exit_fields(code))

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kw: Any
    ) -> TerminalOutputResponse:
        process = self._live[terminal_id]
        status = (
            None
            if process.returncode is None
            else TerminalExitStatus(**_exit_fields(process.returncode))
        )
        return TerminalOutputResponse(
            output=bytes(self._buffers[terminal_id]).decode(errors="replace"),
            truncated=self._truncated[terminal_id],
            exitStatus=status,
        )

    async def kill_terminal(self, session_id: str, terminal_id: str, **kw: Any) -> None:
        self.killed.append(terminal_id)
        process = self._live[terminal_id]
        if process.returncode is None:
            process.kill()
        return None

    async def release_terminal(self, session_id: str, terminal_id: str, **kw: Any) -> None:
        self.released.append(terminal_id)
        # The process is reaped **before** a simulated failure, not after. `release_error`
        # models a client that answers `terminal/release` with an error; it does not model
        # one that walks away from a process it started, which no real client does and
        # which would leave this suite with a live subprocess per such test.
        process = self._live.pop(terminal_id, None)
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        reader = self._readers.pop(terminal_id, None)
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if self.release_error is not None:
            raise self.release_error
        return None


def _exit_fields(code: int) -> dict[str, Any]:
    """A `returncode` as the schema's two fields: a signal has no exit code."""
    if code < 0:
        return {"signal": signal.Signals(-code).name}
    return {"exitCode": code}


def prints(text: str) -> dict[str, Any]:
    """A `run` spec for a command that prints `text` and exits 0."""
    return {"command": sys.executable, "args": ["-c", f"print({text!r})"]}


class Harness:
    """A session with `server_names` MCP servers open, and an executor over them."""

    def __init__(
        self,
        *server_names: str,
        capabilities: Any = None,
        client: Any = None,
        cwd: str = "/work",
        server_env: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.server_names = server_names
        self.server_env = server_env or {}
        self.capabilities = capabilities
        self.backends = McpBackendRegistry()
        # Deliberately **not** closed on exit, unlike the backends: every test here
        # asserts on what the turn itself released, and a harness that tidied up first
        # would hide exactly the leak these tests are for.
        self.terminals = TerminalRegistry()
        self.client = client or RecordingClient()
        self.session = SessionRegistry().create(cwd)

    async def __aenter__(self) -> Harness:
        await self.backends.open(
            self.session.session_id,
            [spec(n, **self.server_env.get(n, {})) for n in self.server_names],
        )
        self.context = TurnContext(self.session, self.client, self.capabilities)  # type: ignore[arg-type]
        self.executor = McpToolRouterExecutor(self.backends, self.terminals)
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

    def live_terminals(self) -> tuple[Any, ...]:
        return self.terminals.live(self.session.session_id)


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
    # The refusal is advice about a shape, and it reaches the user through a Markdown
    # renderer. Bare `<name>` is an HTML tag, so the advice arrived as `{"tool": ""}` --
    # the exact shape it was telling the reader not to send (`pyacp-nlv`).
    assert_markdown_safe(harness.refusal())
    assert "<name>" in harness.refusal()


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
    # The server's own tools first, then the ones this executor answers itself. A palette
    # is read from the top, and what the session can *do* outranks how to ask about it.
    assert [c.name for c in commands] == ["tools/echo", *BUILTINS]
    assert commands[0].description
    # ACP's only argument shape is one free-text hint, and **every** entry carries one.
    # This used to say `commands[1:]`, excusing the tool entries for having none --
    # which is how a composer came to offer `tools/echo` with nothing saying its
    # parameter was named (`pyacp-acn`).
    assert all(c.input.root.hint for c in commands)
    assert commands[0].input.root.hint == "--text <string>"


async def test_each_tools_schema_rides_along_in_meta() -> None:
    """The structure the hint had to flatten, kept for a client that can render it.

    `input` is `UnstructuredCommandInput` -- one free-text string -- so the hint is a
    summary a client cannot validate against, and the user learns the flag names and the
    legal enum values by trial and error. `_meta` is ACP's own extensibility point, and
    `AvailableCommand` already carries it (`pyacp-ma2`).
    """
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo"))

    echo = harness.of("available_commands_update")[0].available_commands[0]
    assert echo.name == "tools/echo"
    meta = echo.field_meta[TOOL_META_KEY]
    # Verbatim, unnormalised: the client renders the *server's* vocabulary, not ours, so
    # this is what `tools/list` returned and not a cleaned-up restatement of it.
    assert meta["inputSchema"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    # The name splits on the *first* slash, a rule that exists because a server name may
    # not contain one. Carrying the halves means no client has to reimplement it.
    assert (meta["server"], meta["tool"]) == ("tools", "echo")
    # The whole point of putting it in `_meta`: a client that ignores the key sees the
    # session it saw before. Additive, or it is not shippable ahead of any client.
    assert echo.input.root.hint == "--text <string>"


async def test_the_builtins_carry_no_tool_meta() -> None:
    """`_meta` here means "an MCP tool's schema". A built-in has no MCP tool behind it,
    and answering the key with something invented would make the namespace mean two
    things."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo"))

    commands = harness.of("available_commands_update")[0].available_commands
    assert all(not (c.field_meta or {}).get(TOOL_META_KEY) for c in commands[1:])


def test_a_tool_that_published_no_schema_omits_the_key() -> None:
    """Omitted, not null -- and not confused with a tool that published an empty one.

    `commands.py` already draws this distinction in its error messages: `properties: {}`
    is a statement that the tool takes no parameters, while an absent `inputSchema` says
    *nothing*, and reporting the second as the first asserts a fact nobody published. A
    client reading `_meta` has to be able to draw the same line, which it cannot do if we
    send `"inputSchema": null` for both.

    Direct rather than over the wire because the fixture's tools all publish a schema, and
    teaching it to omit one would change what every unrelated test sees.
    """
    silent = _tool_meta("srv", "quiet", {"name": "quiet"})[TOOL_META_KEY]
    assert "inputSchema" not in silent
    assert (silent["server"], silent["tool"]) == ("srv", "quiet")

    # An empty schema is a statement, so it survives.
    empty = _tool_meta("srv", "loud", {"inputSchema": {"type": "object", "properties": {}}})
    assert empty[TOOL_META_KEY]["inputSchema"] == {"type": "object", "properties": {}}

    # A schema that is not an object at all said nothing usable either.
    assert "inputSchema" not in _tool_meta("srv", "odd", {"inputSchema": "nonsense"})[TOOL_META_KEY]


ZOO = {"tools": {"MOCK_MCP_SCHEMA_ZOO": "1"}}


async def test_the_schema_zoo_is_absent_unless_asked_for() -> None:
    """Opt-in, like the annotated tools and for the same reason: every unrelated test in
    this file expects this server to offer exactly one tool."""
    async with Harness("tools") as harness:
        await harness.run(block(tool="echo"))

    names = [c.name for c in harness.of("available_commands_update")[0].available_commands]
    assert names == ["tools/echo", *BUILTINS]


async def test_the_schema_zoo_reaches_meta_intact() -> None:
    """The `pyacp-ma2` verbatim claim, checked against a real server rather than a dict.

    `every-content` serves every MCP content type so the content mapping is exercised
    against a server; this is its counterpart in the other direction (`pyacp-6kz`). A
    client author renders these, and what they render has to be what the server said —
    normalising on the way through is one more place for the form and `coerce_arguments`
    to disagree.
    """
    async with Harness("tools", server_env=ZOO) as harness:
        await harness.run(block(tool="echo"))

    commands = harness.of("available_commands_update")[0].available_commands
    zoo = {c.name: c for c in commands if c.name.startswith("tools/zoo-")}
    # Every construct on both sides of the line a client draws: the ones it renders, the
    # four conditional ones it is expected to decline, and the two edges.
    assert set(zoo) == {
        "tools/zoo-types",
        "tools/zoo-strings",
        "tools/zoo-numbers",
        "tools/zoo-choices",
        "tools/zoo-arrays",
        "tools/zoo-required",
        "tools/zoo-nested",
        "tools/zoo-if-then-else",
        "tools/zoo-dependent-schemas",
        "tools/zoo-all-of",
        "tools/zoo-one-of",
        "tools/zoo-empty",
        "tools/zoo-silent",
    }

    choices = zoo["tools/zoo-choices"].field_meta[TOOL_META_KEY]["inputSchema"]
    # Unmodified: `enumNames` is not a JSON Schema keyword and a normalising pass is
    # exactly what would drop it, taking the labels a dropdown needs with it.
    assert choices["properties"]["priority"]["enumNames"] == ["Critical", "High", "Normal", "Low"]
    # `oneOf`/`const` survives too, spelt the way a schema generator emits a choice.
    assert choices["properties"]["mode"]["oneOf"] == [
        {"const": "fast", "title": "Fast"},
        {"const": "thorough", "title": "Thorough"},
    ]
    # A conditional schema is forwarded whole. A client declines to *render* these; that
    # is its decision to make, and it cannot make it if we filter them out first.
    assert "if" in zoo["tools/zoo-if-then-else"].field_meta[TOOL_META_KEY]["inputSchema"]
    assert "allOf" in zoo["tools/zoo-all-of"].field_meta[TOOL_META_KEY]["inputSchema"]


async def test_the_two_schema_edges_are_distinguishable_over_the_wire() -> None:
    """"Said it takes none" and "said nothing" must not arrive looking the same.

    Until `zoo-silent` existed the omission branch of `_tool_meta` was reachable only from
    a unit test, because every fixture tool published a schema. `commands.py` writes a
    different error message for each of these two, and a client reading `_meta` can only
    draw the same line if the wire keeps them apart.
    """
    async with Harness("tools", server_env=ZOO) as harness:
        await harness.run(block(tool="echo"))

    zoo = {
        c.name: c.field_meta[TOOL_META_KEY]
        for c in harness.of("available_commands_update")[0].available_commands
        if c.name.startswith("tools/zoo-")
    }
    # An empty property block is a statement, and it survives as one.
    assert zoo["tools/zoo-empty"]["inputSchema"] == {"type": "object", "properties": {}}
    # A tool that published nothing gets no key at all -- not `"inputSchema": null`.
    assert "inputSchema" not in zoo["tools/zoo-silent"]
    # Both still carry a hint, because every announced command does.
    hints = {
        c.name: c.input.root.hint
        for c in harness.of("available_commands_update")[0].available_commands
    }
    assert hints["tools/zoo-empty"] == "(no parameters)"
    assert hints["tools/zoo-silent"] == "(no parameters)"


async def test_a_zoo_tool_echoes_the_json_types_it_was_given() -> None:
    """The round trip the zoo exists for: what types came out the far end.

    A form that renders correctly and then sends `"3"` where the schema says `integer` is
    broken in a way no amount of looking at the form reveals. `coerce_arguments` is what
    stands between the two, and this is the assertion that it did its job against a real
    server.
    """
    async with Harness("tools", server_env=ZOO) as harness:
        result = await harness.run(
            block(
                tool="zoo-numbers",
                arguments={"percent": 75, "ratio": 0.5, "step": 0.25},
            )
        )

    assert result.stop_reason == "end_turn"
    echoed = json.loads(
        [u for u in harness.of("tool_call_update") if u.raw_output][-1]
        .raw_output["content"][0]["text"]
    )
    assert echoed == {
        "tool": "zoo-numbers",
        "arguments": {"percent": 75, "ratio": 0.5, "step": 0.25},
    }


#: Every schema-zoo tool, as the *command line* a person would type, with the arguments
#: the server should receive. The JSON-block path above types its arguments before they
#: are sent; only this path runs them through `coerce_arguments`, which is why
#: `pyacp-708` — a `TypeError` on `zoo-types.either`'s union `type` that killed the whole
#: turn with `-32603` — survived a zoo built to contain exactly that shape.
ZOO_COMMAND_LINES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "zoo-types",
        "--a_string 123 --a_number 123 --an_integer 123 --a_boolean true "
        "--an_array '[1,2,3]' --an_object '{\"a\":\"123\"}' --either abc123",
        {
            "a_string": "123",
            "a_number": 123.0,
            "an_integer": 123,
            "a_boolean": True,
            "an_array": [1, 2, 3],
            "an_object": {"a": "123"},
            "either": "abc123",
        },
    ),
    ("zoo-strings", "--slug my-slug --short abcd", {"slug": "my-slug", "short": "abcd"}),
    (
        "zoo-numbers",
        "--percent 50 --ratio 0.5 --step 0.75 --offset -3",
        {"percent": 50, "ratio": 0.5, "step": 0.75, "offset": -3},
    ),
    (
        "zoo-choices",
        "--colour red --priority P1 --retries 3",
        {"colour": "red", "priority": "P1", "retries": 3},
    ),
    (
        "zoo-arrays",
        "--tags a --tags b --counts 1 --counts 2 --kinds code",
        {"tags": ["a", "b"], "counts": [1, 2], "kinds": ["code"]},
    ),
    ("zoo-required", "--must yes", {"must": "yes"}),
    (
        "zoo-nested",
        "--label l --server '{\"port\": 8080}'",
        {"label": "l", "server": {"port": 8080}},
    ),
    ("zoo-if-then-else", "--kind advanced --tuning fast", {"kind": "advanced", "tuning": "fast"}),
    ("zoo-dependent-schemas", "--name n --billing b", {"name": "n", "billing": "b"}),
    # Its properties live inside `allOf`, so the top level declares none and nothing can
    # be checked against a name. Both arguments reach the server untouched.
    ("zoo-all-of", "--a x --b 2", {"a": "x", "b": 2}),
    ("zoo-one-of", "--id 7", {"id": 7}),
    ("zoo-empty", "", {}),
    ("zoo-silent", "--anything goes", {"anything": "goes"}),
)


@pytest.mark.parametrize(
    ("tool", "line", "expected"),
    [pytest.param(*case, id=case[0]) for case in ZOO_COMMAND_LINES],
)
async def test_every_zoo_tool_survives_its_own_command_line(
    tool: str, line: str, expected: dict[str, Any]
) -> None:
    """Call every schema-zoo tool the way a person does, and check what the server got.

    The zoo was built as a *rendering* fixture — thirteen schemas for a client to draw a
    form from — and until this test every one of them was only ever listed. That is how a
    tool named `zoo-types`, carrying a union `type` because "most form builders do not
    expect it", went a whole commit without anyone discovering that this agent did not
    expect it either (`pyacp-708`).

    Asserted on the echo rather than on `coerce_arguments` alone, because the types are
    only real once they have crossed the wire: this is the `--counts 1 --counts 2` that
    must arrive as `[1, 2]` and not `["1", "2"]`.
    """
    async with Harness("tools", server_env=ZOO) as harness:
        text = f"/tools/{tool} {line}".rstrip()
        result = await harness.run(TextContentBlock(type="text", text=text))

    assert result.stop_reason == "end_turn", harness.of("agent_message_chunk")
    echoed = json.loads(
        [u for u in harness.of("tool_call_update") if u.raw_output][-1].raw_output["content"][0][
            "text"
        ]
    )
    assert echoed == {"tool": tool, "arguments": expected}


async def test_tools_are_announced_even_when_the_prompt_is_refused() -> None:
    """That is the point: a refusal that also says what *could* have been called is
    actionable, and one that only says "that was not an invocation" is not."""
    async with Harness("tools") as harness:
        result = await harness.run(TextContentBlock(type="text", text="prose"))

    assert result.stop_reason == "refusal"
    assert [c.name for c in harness.of("available_commands_update")[0].available_commands] == [
        "tools/echo",
        *BUILTINS,
    ]


async def test_commands_from_several_servers_are_qualified_and_ordered() -> None:
    async with Harness("beta", "alpha") as harness:
        await harness.run(block(tool="echo", server="alpha"))

    commands = harness.of("available_commands_update")[0].available_commands
    assert [c.name for c in commands] == ["alpha/echo", "beta/echo", *BUILTINS]


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
        "tools/echo",
        *BUILTINS,
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
    """The notification goes, not every `tools/list` behind it.

    Since `pyacp-eg1.3` a turn lists the servers it actually calls either way, because a
    tool call's `kind` is read from their annotations. What the option still saves is the
    notification and the servers this turn does not touch.
    """
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


# ---------------------------------------------------------------------------
# Commands through the client's terminals (pyacp-8bv.3)
# ---------------------------------------------------------------------------


def terminal_harness(tmp_path: Path, *, capabilities: Any = None, client: Any = None) -> Harness:
    """A session rooted at `tmp_path`, because a command has to start somewhere real."""
    return Harness(
        "tools",
        capabilities=has_terminal() if capabilities is None else capabilities,
        client=client if client is not None else TerminalClient(),
        cwd=str(tmp_path),
    )


async def test_a_command_runs_on_the_client_and_its_output_becomes_an_argument(
    tmp_path: Path,
) -> None:
    """The whole point: the bytes come from a process this runtime never started."""
    async with terminal_harness(tmp_path) as harness:
        result = await harness.run(block(tool="echo", run={"text": prints("from a terminal")}))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call_update")[-1].content[-1].content.text == "from a terminal\n"
    # `in_progress` re-publishes the arguments, which is the only place the substitution
    # is visible to a client.
    assert harness.of("tool_call_update")[0].raw_input == {"text": "from a terminal\n"}


async def test_all_four_ordinary_terminal_methods_are_used(tmp_path: Path) -> None:
    """`create` → `wait_for_exit` → `output` → `release`, in that order and every time."""
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        await harness.run(block(tool="echo", run={"text": prints("hi")}))

    assert [call["command"] for call in client.created] == [sys.executable]
    assert client.waiting.is_set()
    assert client.released == ["terminal-1"]
    assert client.killed == []
    assert harness.live_terminals() == ()


async def test_a_terminal_is_released_even_when_the_tool_then_fails(tmp_path: Path) -> None:
    """The command ran on someone's machine; a failing tool afterwards does not un-run it,
    and must not strand the terminal it produced."""
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        result = await harness.run(block(tool="boom", run={"text": prints("ignored")}))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call_update")[-1].status == "failed"
    assert client.released == ["terminal-1"]
    assert harness.live_terminals() == ()


async def test_the_note_says_what_ran_and_where_it_went(tmp_path: Path) -> None:
    """A command that ran on the client's machine and left no trace in the transcript
    would make the turn unreadable afterwards."""
    async with terminal_harness(tmp_path) as harness:
        await harness.run(block(tool="echo", run={"text": prints("noted")}))

    note = harness.of("tool_call_update")[-1].content[0].content.text
    assert note.startswith("Ran ")
    assert "'text'" in note


async def test_the_output_byte_limit_is_always_set(tmp_path: Path) -> None:
    """Unbounded output is the failure mode the field exists to prevent, so the request
    never omits it."""
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        await harness.run(block(tool="echo", run={"text": prints("bounded")}))

    assert client.created[0]["output_byte_limit"] == DEFAULT_OUTPUT_BYTE_LIMIT


async def test_a_client_may_ask_for_a_different_limit_and_is_told_about_truncation(
    tmp_path: Path,
) -> None:
    """The client truncates from the beginning, so the tail is what survives — and the
    note says so, because an argument silently missing its first 90 bytes is worse than
    one that admits it."""
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        await harness.run(
            block(
                tool="echo",
                run={"text": {**prints("x" * 100), "outputByteLimit": 10}},
            )
        )

    assert client.created[0]["output_byte_limit"] == 10
    note = harness.of("tool_call_update")[-1].content[0].content.text
    assert "truncated it to the last 10 bytes" in note
    assert harness.of("tool_call_update")[-1].content[-1].content.text == "x" * 9 + "\n"


async def test_cancelling_a_turn_mid_command_kills_and_releases_the_terminal(
    tmp_path: Path,
) -> None:
    """The leak path that matters most: the turn is torn out while the command is still
    running, so nobody is left to read its output. It is killed rather than left burning
    the client's machine, and released rather than left in its terminal list."""
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        turn = asyncio.create_task(
            harness.run(
                block(
                    tool="echo",
                    run={
                        "text": {
                            "command": sys.executable,
                            "args": ["-c", "import time; time.sleep(30)"],
                        }
                    },
                )
            )
        )
        await asyncio.wait_for(client.waiting.wait(), timeout=10)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    assert client.killed == ["terminal-1"]
    assert client.released == ["terminal-1"]
    assert harness.live_terminals() == ()
    # The kill really reached a process, rather than only being recorded.
    assert client.processes["terminal-1"].returncode is not None


async def test_a_command_that_exits_non_zero_fails_the_call_and_never_calls_the_tool(
    tmp_path: Path,
) -> None:
    """The same asymmetry as a failed read: an argument built from a failed command's
    output would be inventing input."""
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        result = await harness.run(
            block(
                tool="echo",
                run={"text": {"command": sys.executable, "args": ["-c", "raise SystemExit(3)"]}},
            )
        )

    assert result.stop_reason == "end_turn"
    last = harness.of("tool_call_update")[-1]
    assert last.status == "failed"
    assert "exited with status 3" in last.content[0].content.text
    assert [u.status for u in harness.tool_calls()] == ["pending", "failed"]
    # Failing is not leaking.
    assert client.released == ["terminal-1"]
    assert harness.live_terminals() == ()


async def test_a_command_killed_by_a_signal_names_the_signal(tmp_path: Path) -> None:
    """`exitCode` is null for a signalled process, so the message has to read the other
    field rather than print `None` at somebody."""
    async with terminal_harness(tmp_path) as harness:
        result = await harness.run(
            block(
                tool="echo",
                run={
                    "text": {
                        "command": sys.executable,
                        "args": ["-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
                    }
                },
            )
        )

    assert result.stop_reason == "end_turn"
    assert "was killed by SIGKILL" in harness.of("tool_call_update")[-1].content[0].content.text


async def test_a_client_that_errors_on_create_fails_the_call_not_the_turn(
    tmp_path: Path,
) -> None:
    client = TerminalClient(create_error=RequestError(-32603, "no terminals here"))

    async with terminal_harness(tmp_path, client=client) as harness:
        result = await harness.run(
            block(tool="echo", run={"text": prints("never")}),
            block(tool="echo", arguments={"text": "still ran"}),
        )

    assert result.stop_reason == "end_turn"
    first = harness.of("tool_call_update")[0]
    assert first.status == "failed"
    assert "-32603: no terminals here" in first.content[0].content.text
    assert harness.live_terminals() == ()


async def test_a_release_that_fails_does_not_fail_the_turn(tmp_path: Path) -> None:
    """A client refusing to take a terminal back is not information the turn can act on,
    and replacing the tool's result with a cleanup error would lose the answer."""
    client = TerminalClient(release_error=RequestError(-32603, "unknown terminal"))

    async with terminal_harness(tmp_path, client=client) as harness:
        result = await harness.run(block(tool="echo", run={"text": prints("kept")}))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call_update")[-1].status == "completed"
    # Tracking is dropped anyway: the handle is useless either way, and holding it would
    # be a leak of our own on top of the client's.
    assert harness.live_terminals() == ()


async def test_a_denied_call_starts_no_terminal(tmp_path: Path) -> None:
    """Permission comes first: approving the call is what authorises starting a process
    on the client's machine."""
    client = TerminalClient("reject")

    async with terminal_harness(tmp_path, client=client) as harness:
        result = await harness.run(block(tool="echo", run={"text": prints("never")}))

    assert result.stop_reason == "end_turn"
    assert client.created == []


async def test_a_dry_run_names_the_command_and_starts_nothing(tmp_path: Path) -> None:
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        in_mode(harness, "dry-run")
        result = await harness.run(block(tool="echo", run={"text": prints("previewed")}))

    assert result.stop_reason == "end_turn"
    assert "Would run" in harness.of("tool_call_update")[-1].content[0].content.text
    assert client.created == []


async def test_the_session_cwd_is_where_a_command_starts_unless_one_is_named(
    tmp_path: Path,
) -> None:
    """A command has to start somewhere, and the client's process directory is not
    something this side can see."""
    inner = tmp_path / "inner"
    inner.mkdir()
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        await harness.run(
            block(tool="echo", run={"a": prints("default")}),
            block(tool="echo", run={"b": {**prints("named"), "cwd": str(inner)}}),
        )

    assert client.created[0]["cwd"] == str(tmp_path)
    assert client.created[1]["cwd"] == str(inner.resolve())


async def test_a_cwd_outside_the_session_roots_refuses_the_turn(tmp_path: Path) -> None:
    """Containment is the same rule `read` and `write` go through — `paths.py` owns it."""
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        result = await harness.run(
            block(tool="echo", run={"a": {**prints("nope"), "cwd": "/etc"}})
        )

    assert result.stop_reason == "refusal"
    assert "outside this session's directories" in harness.refusal()
    assert client.created == []


async def test_environment_variables_reach_the_command(tmp_path: Path) -> None:
    client = TerminalClient()

    async with terminal_harness(tmp_path, client=client) as harness:
        await harness.run(
            block(
                tool="echo",
                run={
                    "text": {
                        "command": sys.executable,
                        "args": ["-c", "import os; print(os.environ['PYACP_MARKER'])"],
                        "env": {"PYACP_MARKER": "carried"},
                    }
                },
            )
        )

    assert client.created[0]["env"] == {"PYACP_MARKER": "carried"}
    assert harness.of("tool_call_update")[-1].content[-1].content.text == "carried\n"


# ---------------------------------------------------------------------------
# The terminal gate
# ---------------------------------------------------------------------------


async def test_running_a_command_without_the_capability_is_a_refusal(tmp_path: Path) -> None:
    """A client with no terminals has done nothing wrong, so it gets a `refusal` and not
    the `-32603` that `require` would produce — and no convention footer, because the
    convention was followed."""
    client = TerminalClient()

    async with terminal_harness(
        tmp_path, capabilities=ClientCapabilities(), client=client
    ) as harness:
        result = await harness.run(block(tool="echo", run={"text": prints("unreachable")}))

    assert result.stop_reason == "refusal"
    assert "clientCapabilities.terminal" in harness.refusal()
    assert CONVENTION not in harness.refusal()
    assert client.created == []
    assert harness.of("tool_call") == []


async def test_a_filesystem_grant_does_not_satisfy_a_terminal(tmp_path: Path) -> None:
    """`fs` and `terminal` are different capabilities, and one is not the other."""
    async with terminal_harness(tmp_path, capabilities=has_fs()) as harness:
        result = await harness.run(block(tool="echo", run={"text": prints("unreachable")}))

    assert result.stop_reason == "refusal"
    assert "clientCapabilities.terminal" in harness.refusal()


async def test_an_empty_run_object_needs_no_capability(tmp_path: Path) -> None:
    """`"run": {}` names no command, so refusing it would refuse a request never made."""
    async with terminal_harness(tmp_path, capabilities=ClientCapabilities()) as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}, run={}))

    assert result.stop_reason == "end_turn"


def test_the_terminal_gate_is_still_an_assertion_at_the_call_site() -> None:
    """Parsing refuses first, so reaching the call with a shut gate is our conformance
    bug. A design assertion, like its `fs` counterpart: no prompt can produce it."""
    source = inspect.getsource(McpToolRouterExecutor)

    assert "context.require(Gate.TERMINAL)" in source


# ---------------------------------------------------------------------------
# Parsing `run`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, because",
    [
        ({"run": []}, "'run' must be an object mapping an argument name to a command"),
        ({"run": {"": {"command": "x"}}}, "every key of 'run' must be a non-empty"),
        ({"run": {"a": "ls"}}, "'run.a' must be an object with a 'command'"),
        ({"run": {"a": {}}}, "'run.a.command' must be a non-empty string"),
        ({"run": {"a": {"command": ""}}}, "'run.a.command' must be a non-empty string"),
        ({"run": {"a": {"command": "ls", "args": "-la"}}}, "'run.a.args' must be a list of"),
        ({"run": {"a": {"command": "ls", "args": [3]}}}, "'run.a.args' must be a list of"),
        ({"run": {"a": {"command": "ls", "env": [["A", "1"]]}}}, "'run.a.env' must be an object"),
        ({"run": {"a": {"command": "ls", "env": {"A": 1}}}}, "'run.a.env' must be an object"),
        (
            {"run": {"a": {"command": "ls", "outputByteLimit": -1}}},
            "'run.a.outputByteLimit' must be a non-negative integer",
        ),
    ],
)
async def test_every_malformed_run_names_what_is_wrong(
    tmp_path: Path, spec: dict[str, Any], because: str
) -> None:
    async with terminal_harness(tmp_path) as harness:
        result = await harness.run(block(tool="echo", **spec))

    assert result.stop_reason == "refusal"
    assert because in harness.refusal()


async def test_an_argument_named_by_both_a_command_and_something_else_is_refused(
    tmp_path: Path,
) -> None:
    """Two sources for one value, whichever two they are."""
    async with terminal_harness(tmp_path) as harness:
        inline = await harness.run(
            block(tool="echo", arguments={"text": "inline"}, run={"text": prints("also")})
        )

    assert inline.stop_reason == "refusal"
    assert "named in 'run' and somewhere else too" in harness.refusal()


async def test_a_file_and_a_command_cannot_fill_the_same_argument(tmp_path: Path) -> None:
    source = tmp_path / "in.txt"
    source.write_text("from disk")

    async with terminal_harness(
        tmp_path, capabilities=has_terminal(read=True), client=TerminalClient()
    ) as harness:
        result = await harness.run(
            block(
                tool="echo",
                read={"text": {"path": str(source)}},
                run={"text": prints("from a command")},
            )
        )

    assert result.stop_reason == "refusal"
    assert "named in 'run' and somewhere else too" in harness.refusal()


# ---------------------------------------------------------------------------
# Tool annotations (pyacp-eg1.3)
# ---------------------------------------------------------------------------


async def test_a_read_only_tool_is_still_asked_about(monkeypatch: Any) -> None:
    """**The test this whole feature is fenced by.**

    `pyacp-eg1.3` opened by proposing the router "skip the prompt for readOnlyHint
    tools". It is refused: a server asserting `readOnlyHint: true` and thereby escaping
    the permission prompt is a privilege escalation written by the party being
    restrained. MCP says the same in its own words — never make tool-use decisions on
    annotations from an untrusted server.

    So the hint relabels the question and the question is still asked.
    """
    monkeypatch.setenv("MOCK_MCP_ANNOTATED_TOOLS", "1")
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo", arguments={"text": "hi"}))

    assert result.stop_reason == "end_turn"
    assert [c.title for c in harness.client.permission_requests] == ["tools/echo"]
    assert harness.client.permission_requests[0].kind == "read"


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        ("echo", "read"),
        ("wipe", "delete"),
        ("patch", "edit"),
        ("plain", "other"),
    ],
)
async def test_the_servers_annotations_reach_the_wire(
    monkeypatch: Any, tool: str, kind: str
) -> None:
    """On the `tool_call` a client renders *and* on the prompt a human answers.

    Both, because they are what the hint is for: an icon, and a better question.
    """
    monkeypatch.setenv("MOCK_MCP_ANNOTATED_TOOLS", "1")
    async with Harness("tools") as harness:
        await harness.run(block(tool=tool, arguments={"text": "hi"}))

    assert harness.of("tool_call")[0].kind == kind
    assert harness.client.permission_requests[0].kind == kind


async def test_the_kind_survives_a_turn_that_does_not_announce(monkeypatch: Any) -> None:
    """Turning off `announce-tools` must not quietly downgrade the permission prompt.

    The option is about a notification. A human deciding whether to run something called
    `wipe` should not get a worse question because the client already knew the tool list.
    """
    monkeypatch.setenv("MOCK_MCP_ANNOTATED_TOOLS", "1")
    async with Harness("tools") as harness:
        configured(harness, "announce-tools", False)
        await harness.run(block(tool="wipe"))

    assert harness.of("available_commands_update") == []
    assert harness.of("tool_call")[0].kind == "delete"


async def test_a_server_that_annotates_nothing_labels_nothing(monkeypatch: Any) -> None:
    """The pre-annotation behaviour, unchanged, for a server that says nothing.

    `MOCK_MCP_ANNOTATED_TOOLS` off leaves `echo` as the only tool — annotated, because a
    fixture whose one tool said nothing could not tell "we ignore hints" from "there were
    none". So this asserts the *absence* case through a tool the listing does not
    describe at all: `handshake-report` is callable and unlisted.
    """
    async with Harness("tools") as harness:
        await harness.run(block(tool="handshake-report"))

    assert harness.of("tool_call")[0].kind == "other"


async def test_an_absent_server_names_the_dropped_entry_as_a_cause() -> None:
    """The one place a `skip-invalid-items` casualty becomes visible (`pyacp-mej`).

    Nothing in `src/` can detect that an `mcpServers` entry was dropped — the agent is
    handed the survivors and never learns what was sent. What it *can* do is stop the
    eventual refusal from reading like the client's own typo, since "this session opened
    ['tools']" is equally consistent with a misspelled name and with an entry ACP threw
    away before we saw it.
    """
    async with Harness("tools") as harness:
        result = await harness.run(block(tool="echo", server="missing"))

    assert result.stop_reason == "refusal"
    refusal = harness.refusal()
    assert "'missing'" in refusal and "['tools']" in refusal
    for required in ("'name'", "'command'", "'args'", "'env'"):
        assert required in refusal, required


async def test_a_session_with_no_servers_says_the_same_thing() -> None:
    """The other half: every entry dropped leaves a session that opened nothing."""
    async with Harness() as harness:
        result = await harness.run(block(tool="echo"))

    assert result.stop_reason == "refusal"
    assert "opened no MCP servers" in harness.refusal()
    assert "'env'" in harness.refusal()


# ---------------------------------------------------------------------------
# `/tools` and `/invokeTool`: the same machinery, reachable by hand (commands.py)
# ---------------------------------------------------------------------------


def typed(text: str) -> TextContentBlock:
    """A prompt someone typed, as opposed to `block()`, which a program composed."""
    return TextContentBlock(type="text", text=text)


def said(harness: Harness) -> str:
    """Everything the agent said this turn, joined.

    Skips a chunk whose content is not text: `/promptShow` emits an expanded prompt's
    blocks through `to_content_block`, so a prompt carrying an image puts an
    `ImageContentBlock` on this stream and `.text` would raise.
    """
    return "\n".join(
        update.content.text
        for update in harness.of("agent_message_chunk")
        if getattr(update.content, "text", None) is not None
    )


def returned(harness: Harness) -> str:
    """What the tools actually sent back, which rides `tool_call_update`, not a message."""
    texts = []
    for update in harness.of("tool_call_update"):
        for item in update.content or []:
            text = getattr(getattr(item, "content", None), "text", None)
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


async def test_slash_tools_lists_the_sessions_tools_with_their_parameters() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(typed("/tools"))

    assert result.stop_reason == "end_turn"
    listing = said(harness)
    assert "tools/echo" in listing
    assert "Echoes text" in listing
    # The parameter spec is the point of the command: the announcement already carries
    # the name and description, and nothing before this carried the arguments.
    assert "--text" in listing
    assert "<string>" in listing
    assert "required" in listing
    # It answered a question; it did not call anything.
    assert harness.tool_calls() == []


async def test_tools_is_accepted_without_the_slash() -> None:
    """A client filling its composer from `available_commands` sends the bare name."""
    async with Harness("tools") as harness:
        result = await harness.run(typed("tools"))

    assert result.stop_reason == "end_turn"
    assert "tools/echo" in said(harness)


async def test_slash_tools_on_a_session_with_no_servers_says_how_to_add_one() -> None:
    """"No tools" and "no servers" are different problems, and only one is the reader's."""
    async with Harness() as harness:
        result = await harness.run(typed("/tools"))

    assert result.stop_reason == "end_turn"
    answer = said(harness)
    assert "no MCP servers" in answer
    assert "session/new" in answer


async def test_invoketool_calls_the_tool_and_reports_it_like_any_other_invocation() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(typed('/invokeTool tools/echo --text "hello world"'))

    assert result.stop_reason == "end_turn"
    calls = harness.of("tool_call")
    assert len(calls) == 1, "a typed call is reported exactly like a JSON one"
    # The result rides `tool_call_update`, exactly as it does for a JSON invocation --
    # which is the point: the command builds the same `Invocation` and nothing downstream
    # can tell the two apart.
    assert "hello world" in returned(harness)


async def test_invoketool_takes_a_bare_tool_name_when_there_is_one_server() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(typed("/invokeTool echo --text hi"))

    assert result.stop_reason == "end_turn"
    assert len(harness.of("tool_call")) == 1


async def test_invoketool_refuses_a_bare_name_when_several_servers_could_answer() -> None:
    """Picking the first server that happens to publish the name would make the same
    command mean different things as the session's servers changed."""
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(typed("/invokeTool echo --text hi"))

    assert result.stop_reason == "refusal"
    answer = said(harness)
    assert "alpha" in answer and "beta" in answer
    assert harness.tool_calls() == []


async def test_invoketool_names_the_tools_it_does_have_when_the_name_is_wrong() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(typed("/invokeTool tools/ecoh --text hi"))

    assert result.stop_reason == "refusal"
    answer = said(harness)
    assert "echo" in answer, "a refusal that lists the alternatives is actionable"
    assert harness.tool_calls() == []


async def test_invoketool_refuses_a_parameter_the_schema_does_not_declare() -> None:
    """Forwarding a typo would return someone else's validation error about a tool the
    reader named correctly."""
    async with Harness("tools") as harness:
        result = await harness.run(typed("/invokeTool tools/echo --txet hi"))

    assert result.stop_reason == "refusal"
    answer = said(harness)
    assert "--txet" in answer
    assert "--text" in answer, "the refusal names what it does take"
    assert harness.tool_calls() == []


async def test_invoketool_refuses_a_missing_required_parameter() -> None:
    async with Harness("tools") as harness:
        result = await harness.run(typed("/invokeTool tools/echo"))

    assert result.stop_reason == "refusal"
    assert "--text" in said(harness)
    assert harness.tool_calls() == []


async def test_a_declared_string_parameter_is_not_guessed_into_a_number() -> None:
    """The schema is what makes typing a fact rather than a guess: `echo.text` is a
    string, so `3` must reach the server as "3" and not 3."""
    async with Harness("tools") as harness:
        await harness.run(typed("/invokeTool tools/echo --text 3"))
        typed_call = returned(harness)

    # The fixture echoes `text` back verbatim, so a coerced `3` would come back as the
    # JSON number and fail the server's own string schema before that.
    assert typed_call == "3"


async def test_prose_is_still_refused_and_json_still_runs() -> None:
    """The command layer is additive: text that is not a command takes the path it took
    before this module existed."""
    async with Harness("tools") as harness:
        refused = await harness.run(typed("please echo something"))
    assert refused.stop_reason == "refusal"

    async with Harness("tools") as harness:
        ran = await harness.run(block(tool="echo", arguments={"text": "hi"}))
    assert ran.stop_reason == "end_turn"
    assert len(harness.of("tool_call")) == 1


# ---------------------------------------------------------------------------
# Prompts and resources (`pyacp-tc5`)
# ---------------------------------------------------------------------------


async def test_list_prompts_reports_the_servers_prompts_with_their_arguments() -> None:
    async with Harness("demo") as harness:
        result = await harness.run(typed("/listPrompts"))

    # `ended`, not `refused`: the command did exactly what it was asked to do.
    assert result.stop_reason == "end_turn"
    text = said(harness)
    assert "1 prompt on 1 server." in text
    assert "demo/greeting" in text
    assert "--name" in text and "required" in text
    assert harness.of("tool_call") == []


async def test_list_resources_reports_uri_and_mime_type() -> None:
    async with Harness("demo") as harness:
        result = await harness.run(typed("/listResources"))

    assert result.stop_reason == "end_turn"
    text = said(harness)
    assert "1 resource on 1 server." in text
    assert "greeting://ada" in text and "text/plain" in text


async def test_list_resources_shows_the_servers_uri_templates_too() -> None:
    """The bug `pyacp-as5` is about, end to end.

    A template reaches a client through `resources/templates/list` and through nothing
    else, so a listing built from `resources/list` alone reports a server whose resources
    are all templates as having none — a confident wrong picture rather than a missing
    feature. It is shown in a section of its own because it is not readable as printed.
    """
    async with Harness("demo") as harness:
        result = await harness.run(typed("/listResources"))

    assert result.stop_reason == "end_turn"
    text = said(harness)
    assert "greeting://{name}" in text
    assert "demo (1 resource, 1 template)" in text
    assert text.index("greeting://ada") < text.index("URI templates") < text.index(
        "greeting://{name}"
    )
    assert "`/resourceShow demo greeting://<name>`" in text


async def test_a_server_that_implements_no_templates_still_lists_its_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-32601` for `resources/templates/list` is a conforming answer, not a failure.

    Templates are optional *within* the `resources` capability, so absorbing the code
    into an empty section is the only reading that does not replace one wrong picture
    with another: refusing the whole listing would hide the resources the server does
    publish.
    """
    monkeypatch.setenv("MOCK_MCP_NO_TEMPLATES", "1")
    async with Harness("demo") as harness:
        result = await harness.run(typed("/listResources"))

    assert result.stop_reason == "end_turn"
    text = said(harness)
    assert "1 resource on 1 server." in text
    assert "greeting://ada" in text
    assert "URI template" not in text


async def test_a_server_that_never_declared_resources_is_not_asked_for_templates() -> None:
    """The second pass asks exactly the servers the first one did.

    A server in the undeclared bucket was never asked `resources/list`; asking it
    `resources/templates/list` would be using a capability it did not declare, which is
    the rule MCP states in both directions.
    """
    async with Harness(
        "demo", "quiet", server_env={"quiet": {"MOCK_MCP_CAPABILITIES": "tools"}}
    ) as harness:
        await harness.run(typed("/listResources"))

    text = said(harness)
    assert "quiet declares no resources capability, so it was not asked." in text
    assert "quiet (" not in text


async def test_prompt_show_expands_the_prompt_and_emits_the_messages() -> None:
    """The expansion is the *server's* work — a template substitution — which is why it
    needs no model and `/promptShow` can answer at all."""
    async with Harness("demo") as harness:
        result = await harness.run(
            typed('/promptShow demo/greeting --name "Ada Lovelace"')
        )

    assert result.stop_reason == "end_turn"
    text = said(harness)
    assert "`demo/greeting` — Greeting prompt" in text
    assert "1 message" in text
    assert "user:" in text
    assert "Hello, Ada Lovelace!" in text


async def test_prompt_show_resolves_a_bare_prompt_name_on_a_one_server_session() -> None:
    async with Harness("demo") as harness:
        await harness.run(typed("/promptShow greeting --name Ada"))

    assert "Hello, Ada!" in said(harness)


async def test_prompt_invoke_refuses_and_hands_back_a_prompt_show_that_runs() -> None:
    """Shipped rather than omitted, on the same principle that has `authenticate` answer
    `-32000`: a client should discover a boundary, not an absence."""
    async with Harness("demo") as harness:
        result = await harness.run(
            typed("/promptInvoke demo/greeting --name Ada")
        )

    assert result.stop_reason == "refusal"
    message = harness.refusal()
    assert "needs a model" in message
    assert "/promptShow demo/greeting --name 'Ada'" in message
    # The JSON convention is not what was missed, so it is not restated.
    assert CONVENTION not in message


async def test_prompt_invoke_reports_a_bad_argument_rather_than_the_missing_model() -> None:
    """Validating before refusing is what makes the refusal useful: a wrong argument is
    the error the person actually needs, whichever verb they typed."""
    async with Harness("demo") as harness:
        result = await harness.run(
            typed("/promptInvoke demo/greeting --colour red")
        )

    assert result.stop_reason == "refusal"
    assert "no argument --colour" in harness.refusal()


async def test_a_missing_required_prompt_argument_is_refused_before_the_server_is_asked() -> None:
    async with Harness("demo") as harness:
        result = await harness.run(typed("/promptShow demo/greeting"))

    assert result.stop_reason == "refusal"
    assert "missing required --name" in harness.refusal()


async def test_an_unknown_prompt_is_refused_with_the_ones_the_server_offers() -> None:
    async with Harness("demo") as harness:
        result = await harness.run(typed("/promptShow demo/nope"))

    assert result.stop_reason == "refusal"
    message = harness.refusal()
    assert "has no prompt 'nope'" in message
    assert "It offers: greeting." in message
    assert "/listPrompts" in message


async def test_resource_show_reads_the_resource() -> None:
    async with Harness("demo") as harness:
        result = await harness.run(
            typed("/resourceShow demo greeting://ada")
        )

    assert result.stop_reason == "end_turn"
    text = said(harness)
    assert "greeting://ada" in text
    assert "text/plain" in text
    assert "Hello, ada!" in text


async def test_a_prompt_command_on_a_multi_server_session_needs_a_server() -> None:
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(typed("/promptShow greeting"))

    assert result.stop_reason == "refusal"
    assert "/promptShow alpha/greeting" in harness.refusal()


async def test_resource_show_suggests_the_server_as_a_separate_word() -> None:
    """`/resourceShow alpha/greeting://ada` would be wrong, so the ambiguous case must not
    print it — hence the separator is a parameter of `_resolve_server`."""
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(
            typed("/resourceShow greeting://ada")
        )

    assert result.stop_reason == "refusal"
    assert "/resourceShow alpha greeting://ada" in harness.refusal()


async def test_a_server_that_declared_no_prompts_capability_is_named_and_not_asked() -> None:
    """MCP's rule is that a client MUST NOT use an undeclared capability. Reading the
    handshake turns a `-32601` naming a method nobody typed into a sentence about the
    server."""
    async with Harness(
        "demo", "files", server_env={"files": {"MOCK_MCP_CAPABILITIES": "tools"}}
    ) as harness:
        await harness.run(typed("/listPrompts"))

    text = said(harness)
    assert "1 prompt on 1 server." in text
    assert "demo/greeting" in text
    assert "files declares no prompts capability" in text


async def test_showing_a_prompt_on_a_server_that_declared_none_is_refused() -> None:
    async with Harness(
        "files", server_env={"files": {"MOCK_MCP_CAPABILITIES": "tools"}}
    ) as harness:
        result = await harness.run(
            typed("/promptShow files/greeting --name Ada")
        )

    assert result.stop_reason == "refusal"
    assert "declared no prompts capability" in harness.refusal()


async def test_reading_a_resource_on_a_server_that_declared_none_is_refused() -> None:
    async with Harness(
        "files", server_env={"files": {"MOCK_MCP_CAPABILITIES": "tools"}}
    ) as harness:
        result = await harness.run(
            typed("/resourceShow files greeting://ada")
        )

    assert result.stop_reason == "refusal"
    assert "declared no resources capability" in harness.refusal()


async def test_a_session_with_no_prompts_anywhere_says_so_rather_than_looking_broken() -> None:
    async with Harness(
        "files", server_env={"files": {"MOCK_MCP_CAPABILITIES": "tools"}}
    ) as harness:
        result = await harness.run(typed("/listPrompts"))

    assert result.stop_reason == "end_turn"
    assert "declares the prompts capability" in said(harness)


@pytest.mark.parametrize(
    ("command", "expected"),
    [("/listPrompts", "3 prompts on 1 server."), ("/listResources", "3 resources on 1 server.")],
)
async def test_a_paged_listing_is_walked_to_exhaustion(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: str
) -> None:
    """`_list_all` is what walks `nextCursor`, and these commands are its only ACP-facing
    callers — a listing that stopped at page one would look like a short server."""
    monkeypatch.setenv("MOCK_MCP_LIST_PAGES", "3")
    async with Harness("demo") as harness:
        await harness.run(TextContentBlock(type="text", text=command))

    assert expected in said(harness)


async def test_the_new_commands_are_announced_so_a_palette_can_offer_them() -> None:
    """Individual prompts are deliberately *not* announced: MCP keeps tools and prompts in
    separate namespaces, so per-item entries would need a rule to stop one shadowing the
    other."""
    async with Harness("demo") as harness:
        await harness.run(typed("/listPrompts"))

    names = [c.name for c in harness.of("available_commands_update")[0].available_commands]
    assert names == ["demo/echo", *BUILTINS]
    assert "listPrompts" in names and "promptShow" in names and "resourceShow" in names
    assert "demo/greeting" not in names


async def test_every_announced_command_is_one_the_parser_accepts() -> None:
    """The promise `available_commands` makes, checked against the parser that keeps it.

    `available_commands` is the field a client fills its composer from, so every name in it
    is a name the client will send back. Before `pyacp-acn` the per-tool entries were
    announced and then refused as malformed JSON, because `parse_command` knew only the
    seven built-ins — and nothing here noticed, since no test typed an advertised tool
    command. This walks the announcement itself, so a future palette entry with no parser
    behind it fails rather than shipping.
    """
    async with Harness("alpha", "beta") as harness:
        await harness.run(block(tool="echo", server="alpha"))

    commands = harness.of("available_commands_update")[0].available_commands
    assert [c.name for c in commands] == ["alpha/echo", "beta/echo", *BUILTINS]
    for command in commands:
        # `None` is the one failure. A `CommandError` means the name *was* recognised and
        # then found incomplete -- `/invokeTool` alone names no tool -- which is a real
        # answer to a real command, not the silent fall-through to the JSON convention
        # that this test exists to catch.
        try:
            recognised = parse_command(f"/{command.name}") is not None
        except CommandError:
            recognised = True
        assert recognised, (
            f"{command.name!r} is announced but parse_command does not recognise it, so a "
            f"client sending it back gets refused as malformed JSON"
        )


async def test_a_tool_is_called_by_its_own_palette_name() -> None:
    """`/alpha/echo --text hi` is sugar for `/invokeTool alpha/echo --text hi`.

    The same `InvokeTool`, so it inherits the mode, the permission prompt, the tool-call
    `kind` and the failure policy without knowing they exist.
    """
    async with Harness("alpha") as harness:
        result = await harness.run(typed("/alpha/echo --text hi"))

    assert result.stop_reason == "end_turn"
    calls = harness.of("tool_call")
    assert [c.title for c in calls] == ["alpha/echo"]
    assert calls[0].raw_input == {"text": "hi"}


async def test_a_positional_argument_is_refused_with_the_tool_s_own_parameters() -> None:
    """`pyacp-ysq`, end to end. Observed live: a user typed `/Demo/echo foo bar`.

    The parser cannot write this message — it sees a loose token and nothing else — so it
    carries the token here, where `tools/list` has been answered and the schema is in hand.
    What it must never do is what it did before: offer `--foo <value>`, naming a parameter
    after the reader's own value.
    """
    async with Harness("tools") as harness:
        result = await harness.run(typed("/tools/echo foo bar"))

    assert result.stop_reason == "refusal"
    answer = said(harness)
    assert "--foo" not in answer and "--bar" not in answer
    assert "--text <string>" in answer, "the tool's real parameter is named"
    assert '`/tools/echo --text "foo bar"`' in answer, "and the example is runnable"
    assert harness.tool_calls() == [], "nothing reached the server"


async def test_the_offered_example_is_a_command_that_then_runs() -> None:
    """The example is only worth printing if pasting it back works, so paste it back."""
    async with Harness("tools") as harness:
        await harness.run(typed("/tools/echo foo bar"))
        example = said(harness).split("Try `")[1].split("`")[0]

        result = await harness.run(typed(example))

    assert result.stop_reason == "end_turn"
    assert harness.of("tool_call")[-1].raw_input == {"text": "foo bar"}


async def test_a_tool_that_declares_no_parameters_says_so_rather_than_naming_one(
    monkeypatch: Any,
) -> None:
    """`wipe` publishes an empty `properties` block, which is a statement about itself.

    Distinct from a server that publishes no `inputSchema` at all, which has said nothing
    about its parameters — reporting *that* as "takes no parameters" would invent a fact.
    """
    monkeypatch.setenv("MOCK_MCP_ANNOTATED_TOOLS", "1")
    async with Harness("tools") as harness:
        result = await harness.run(typed("/tools/wipe now"))

    assert result.stop_reason == "refusal"
    answer = said(harness)
    assert "takes no parameters" in answer
    assert "--now" not in answer
    assert harness.tool_calls() == []


async def test_a_loose_token_is_refused_before_the_server_is_settled() -> None:
    """Two servers and a bare tool name: the ambiguity is the more basic problem, and
    answering the parameter question first would name a schema from the wrong server."""
    async with Harness("alpha", "beta") as harness:
        result = await harness.run(typed("/invokeTool echo foo"))

    assert result.stop_reason == "refusal"
    answer = said(harness)
    assert "alpha" in answer and "beta" in answer
    assert harness.tool_calls() == []


async def test_the_palette_name_and_invoke_tool_produce_the_same_call() -> None:
    """Sugar, not a second execution path — the whole reason it is safe to add."""
    async with Harness("alpha") as sugar:
        await sugar.run(typed("/alpha/echo --text hi"))
    async with Harness("alpha") as verbose:
        await verbose.run(typed("/invokeTool alpha/echo --text hi"))

    one = sugar.of("tool_call")[0]
    two = verbose.of("tool_call")[0]
    assert (one.title, one.kind, one.raw_input) == (two.title, two.kind, two.raw_input)
