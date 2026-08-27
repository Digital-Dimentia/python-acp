"""Tests for the stdio transport binding and the CLI that selects it.

The end-to-end tests here spawn `python -m python_acp.cli --transport stdio` as a real
subprocess and drive it with the SDK's own `ClientSideConnection`. That is the whole
point: nothing in this file speaks to the agent object directly, so what is under test
is the thing an editor actually gets — process startup, the stdio binding, JSON-RPC
framing, and shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.stdio import spawn_agent_process

from python_acp import __version__
from python_acp.cli import _load_catalogue, build_parser, configure_logging
from python_acp.agent import PythonAcpAgent
from python_acp.sessions import SessionRegistry
from python_acp.transport_stdio import _observers, _stdout_reserved

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"

#: How an editor spawns this agent. No backend flag: since `pyacp-sld.4` there is none
#: to pass, and every MCP server arrives in `session/new`.
AGENT_ARGV = ["-m", "python_acp.cli", "--transport", "stdio"]


class _NullClient:
    """The minimum an ACP client must be to hold up its end of a connection."""

    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.permission_requests: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, session_id: str, tool_call: Any, options: Any, **kwargs: Any) -> Any:
        """Approve everything. The agent asks before every tool call (`pyacp-8bv.1`)."""
        from acp.schema import AllowedOutcome, RequestPermissionResponse

        self.permission_requests.append(tool_call)
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", optionId="approve")
        )


@contextlib.asynccontextmanager
async def agent_process():
    async with spawn_agent_process(_NullClient(), sys.executable, *AGENT_ARGV) as (conn, proc):
        yield conn, proc


async def test_a_real_acp_client_initializes_over_the_subprocess() -> None:
    async with agent_process() as (conn, _proc):
        result = await asyncio.wait_for(
            conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30
        )

    assert result.protocol_version == PROTOCOL_VERSION
    assert result.agent_info.name == "python-acp"
    assert result.agent_info.version == __version__
    assert result.auth_methods == []


async def test_an_unrouted_method_answers_method_not_found_over_the_wire() -> None:
    """The -32601 must survive the round trip, not just the router.

    Every routed method is implemented now, so the only source of one is a name the SDK
    does not route — `session/delete` has no `Agent` member in 0.12.1.
    """
    async with agent_process() as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

        with pytest.raises(RequestError) as excinfo:
            await asyncio.wait_for(
                conn._conn.send_request("session/delete", {"sessionId": "s1"}), timeout=30
            )

    assert excinfo.value.code == -32601


async def test_a_real_client_runs_the_create_prompt_cycle_over_stdio() -> None:
    """`pyacp-3rw.2`'s acceptance, end to end: a session, a turn, a stopReason.

    Over a spawned process and the SDK's own client, so what is proven is what an editor
    gets — not what the router does when called in-process.
    """
    async with agent_process() as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

        created = await asyncio.wait_for(
            conn.new_session(cwd="/tmp", mcp_servers=[]), timeout=30
        )
        result = await asyncio.wait_for(
            conn.prompt(
                session_id=created.session_id,
                prompt=[{"type": "text", "text": "hello"}],
            ),
            timeout=30,
        )

    assert created.session_id
    # The default executor is the MCP tool-router; prose is not an invocation, so it
    # refuses rather than pretending to have understood.
    assert result.stop_reason == "refusal"


def test_an_agent_without_a_command_listing_gets_no_observer() -> None:
    """`run_stdio` is typed to the SDK's `Agent`, so an embedder's agent need not have
    either announcing door. Skipping the observer is the honest answer there — there is
    nothing it could announce — and it must not be a crash on connect."""

    class Bare:
        pass

    assert _observers(Bare()) == []
    assert len(_observers(PythonAcpAgent(SessionRegistry()))) == 1


def test_an_embedders_older_agent_keeps_its_announcer() -> None:
    """`announce_prepared_commands` is the door this transport prefers (`pyacp-svt`), and
    an embedder's agent written before it existed has only `announce_commands`. Falling
    back keeps its palette; what it gives up is the ordering guarantee, which is the same
    thing the prepared door gives up when nothing was prepared."""

    class Older:
        async def announce_commands(self, session_id: str) -> None:
            """Never called here — its presence is the whole assertion."""

    assert len(_observers(Older())) == 1


async def test_a_new_session_is_told_its_commands_over_stdio() -> None:
    """`pyacp-p8v`: both transports wire the announcer, or one client sees two agents.

    Over a spawned process and the SDK's own client, so what is proven is that the
    observer reached `run_agent` on this transport too. The *ordering* it depends on is
    asserted in `test_transport_ws.py`, where the raw frames are visible — a client-side
    view cannot distinguish "sent after the response" from "dispatched after it".
    """
    client = _NullClient()
    async with spawn_agent_process(client, sys.executable, *AGENT_ARGV) as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)
        created = await asyncio.wait_for(
            conn.new_session(cwd="/tmp", mcp_servers=[]), timeout=30
        )
        announced = await asyncio.wait_for(
            _await_commands(client, created.session_id), timeout=30
        )

    assert [command.name for command in announced.available_commands]


async def _await_commands(client: _NullClient, session_id: str) -> Any:
    """The first `available_commands_update` for `session_id`.

    A poll rather than an event because `_NullClient` is a list, and the notification is
    in flight while `new_session` is already returning.
    """
    while True:
        for seen_id, update in client.updates:
            if seen_id == session_id and getattr(update, "available_commands", None) is not None:
                return update
        await asyncio.sleep(0.01)


async def test_cancelling_over_the_wire_reaches_the_session() -> None:
    """`session/cancel` is a notification, so the proof is that the next prompt still works."""
    async with agent_process() as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)
        created = await asyncio.wait_for(
            conn.new_session(cwd="/tmp", mcp_servers=[]), timeout=30
        )

        await asyncio.wait_for(conn.cancel(session_id=created.session_id), timeout=30)
        result = await asyncio.wait_for(
            conn.prompt(session_id=created.session_id, prompt=[]), timeout=30
        )

    # An empty prompt names no tool. What matters here is that the session still works.
    assert result.stop_reason == "refusal"


async def test_a_stale_session_id_is_invalid_params_over_the_wire() -> None:
    async with agent_process() as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

        with pytest.raises(RequestError) as excinfo:
            await asyncio.wait_for(
                conn.prompt(session_id="never-existed", prompt=[]), timeout=30
            )

    assert excinfo.value.code == -32602
    assert "never-existed" in excinfo.value.data["reason"]


async def test_the_unstable_methods_are_reachable_over_stdio() -> None:
    """`run_stdio` passes `use_unstable_protocol=True`, so these reach the agent.

    Now that the extended lifecycle is implemented this is unambiguous: a real
    `session/close` succeeds, which the router would never have allowed with the flag
    off — it answers `method_not_found` there without calling the agent at all.
    """
    async with agent_process() as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)
        created = await asyncio.wait_for(
            conn.new_session(cwd="/tmp", mcp_servers=[]), timeout=30
        )

        await asyncio.wait_for(conn.close_session(session_id=created.session_id), timeout=30)

        with pytest.raises(RequestError) as excinfo:
            await asyncio.wait_for(
                conn.prompt(session_id=created.session_id, prompt=[]), timeout=30
            )

    assert excinfo.value.code == -32602


async def test_the_unstable_lifecycle_is_advertised_over_stdio() -> None:
    """The capability block and the router must agree on one connection."""
    async with agent_process() as (conn, _proc):
        result = await asyncio.wait_for(
            conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30
        )

    capabilities = result.agent_capabilities.session_capabilities
    assert capabilities.fork is not None
    assert capabilities.resume is not None
    assert capabilities.close is not None


async def test_the_agent_exits_cleanly_when_the_client_disconnects() -> None:
    async with agent_process() as (conn, proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

    await asyncio.wait_for(proc.wait(), timeout=30)
    assert proc.returncode is not None


async def test_nothing_but_jsonrpc_reaches_stdout() -> None:
    """Every stdout line must parse as JSON-RPC — a banner or traceback would not."""
    import json

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        *AGENT_ARGV,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION},
    }
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(request) + "\n").encode())
    await proc.stdin.drain()
    proc.stdin.close()

    stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    lines = [line for line in stdout.decode().splitlines() if line.strip()]
    assert lines, "the agent produced no response at all"
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0"


def test_stray_prints_land_on_stderr_not_the_wire() -> None:
    captured = io.StringIO()
    real_stdout = sys.stdout

    with contextlib.redirect_stderr(captured):
        with _stdout_reserved():
            print("this would corrupt the stream")
            inside = sys.stdout

    assert sys.stdout is real_stdout
    if sys.platform != "win32":
        assert inside is not real_stdout
        assert "corrupt the stream" in captured.getvalue()


def test_there_is_no_process_wide_backend_flag() -> None:
    """`--mcp-command` is gone with the surface that was its only consumer.

    Asserted as a *rejection* rather than by reading the namespace: the flag being
    unparseable is what stops a deployment silently keeping a dead option in its command
    line and wondering why the server it named is never used.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mcp-command", "echo"])

    assert not hasattr(build_parser().parse_args([]), "mcp_command")


async def test_the_agent_serves_a_session_with_no_process_wide_backend() -> None:
    """`pyacp-db3`: the whole point is that a client no longer needs `--mcp-command`."""
    argv = ["-m", "python_acp.cli", "--transport", "stdio"]
    async with spawn_agent_process(_NullClient(), sys.executable, *argv) as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)
        created = await asyncio.wait_for(
            conn.new_session(cwd="/tmp", mcp_servers=[]), timeout=30
        )
        result = await asyncio.wait_for(
            conn.prompt(session_id=created.session_id, prompt=[]), timeout=30
        )

    assert result.stop_reason == "refusal"


async def test_a_session_can_bring_its_own_mcp_server() -> None:
    """`session/new`'s `mcpServers`, spawned per session and torn down with it."""
    argv = ["-m", "python_acp.cli", "--transport", "stdio"]
    async with spawn_agent_process(_NullClient(), sys.executable, *argv) as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

        created = await asyncio.wait_for(
            conn.new_session(
                cwd="/tmp",
                mcp_servers=[
                    {
                        "name": "tools",
                        "command": sys.executable,
                        "args": [str(FIXTURE_SERVER)],
                        "env": [],
                    }
                ],
            ),
            timeout=30,
        )

    assert created.session_id


async def test_a_session_whose_server_will_not_start_is_refused() -> None:
    argv = ["-m", "python_acp.cli", "--transport", "stdio"]
    async with spawn_agent_process(_NullClient(), sys.executable, *argv) as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

        with pytest.raises(RequestError):
            await asyncio.wait_for(
                conn.new_session(
                    cwd="/tmp",
                    mcp_servers=[
                        {"name": "broken", "command": sys.executable, "args": ["-c", "pass"], "env": []}
                    ],
                ),
                timeout=30,
            )


def test_ws_stays_the_default_transport() -> None:
    """Pins a decision, not an implementation detail (`pyacp-6z4`).

    `stdio` is how an editor spawns an agent and would be the better first
    impression, so the flip is a standing temptation. It is declined because
    `v0.1.0` and `v0.1.1` shipped WebSocket-only — no `--transport` flag existed
    — so every released invocation binds a socket, and since `pyacp-sld.3` the
    two transports serve the identical agent. A breaking change to a shipped
    default that buys no capability is not worth making. See cli.md; flip this
    only alongside a release already breaking the CLI contract.
    """
    args = build_parser().parse_args([])

    assert args.transport == "ws"
    assert args.host == "127.0.0.1"
    assert args.port == 8765


def test_stdio_is_selectable() -> None:
    args = build_parser().parse_args(["--transport", "stdio"])

    assert args.transport == "stdio"


def test_an_unknown_transport_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mcp-command", "echo", "--transport", "carrier-pigeon"])


def test_the_mcp_catalogue_is_optional_and_absent_by_default() -> None:
    """No flag means the agent offers nothing and opens exactly what each client named,
    which is what every deployment did before the catalogue existed."""
    assert build_parser().parse_args([]).mcp_config is None


def test_the_mcp_catalogue_path_is_taken_on_both_transports() -> None:
    for transport in ("ws", "stdio"):
        args = build_parser().parse_args(
            ["--transport", transport, "--mcp-config", "servers.toml"]
        )
        assert args.mcp_config == "servers.toml"


def test_a_catalogue_that_cannot_be_read_stops_the_process_before_it_serves(
    tmp_path: Path,
) -> None:
    """Exit 2, at startup — not a traceback, and not a failure at the first session/new.

    An operator who mistyped a path or a key needs the one sentence saying which, and a
    port that never binds rather than an agent advertising servers it cannot spawn.
    """
    result = subprocess.run(
        [sys.executable, "-m", "python_acp.cli", "--mcp-config", str(tmp_path / "gone.toml")],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert "gone.toml" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_catalogue_is_loaded_and_reported_at_startup(tmp_path: Path) -> None:
    """The names go to the log, because a catalogue silently loading as empty — a typo in
    the table name, a file the process cannot see — looks exactly like no flag at all."""
    catalogue = tmp_path / "servers.toml"
    catalogue.write_text('[servers.demo]\ncommand = "true"\nenabled = false\n')
    loaded = _load_catalogue(str(catalogue))

    assert loaded.names == ("demo",)
    assert loaded.specs() == ()  # enabled = false: offered, not opened


def test_a_clean_shutdown_says_what_ended_it() -> None:
    """EOF on stdin is a normal exit, and it has to look like one from the outside.

    `run_agent` returns on EOF and on nothing else, so the process simply stops — which
    from a parent reads exactly like the agent dying mid-conversation, and was reported
    as "the agent exits after initialize" when the parent was closing the pipe itself.
    The line costs nothing and turns that into a one-look diagnosis; a real failure
    propagates its exception instead of reaching it.

    Driven with a plain `subprocess.run` rather than the SDK client because what is under
    test is the *last* thing the process does, and only stderr and the exit status carry
    it.
    """
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}},
            }
        )
        + "\n"
    )

    result = subprocess.run(
        [sys.executable, *AGENT_ARGV],
        input=request,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "client closed stdin; python-acp exiting" in result.stderr
    # ...and it said so *after* serving, not instead of it.
    assert result.stderr.index("serving ACP over stdio") < result.stderr.index("closed stdin")
    assert json.loads(result.stdout.strip())["id"] == 1


def test_logging_is_configured_onto_stderr() -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    try:
        configure_logging(debug=False)
        streams = [getattr(h, "stream", None) for h in root.handlers]
    finally:
        root.handlers[:] = saved

    assert streams, "configure_logging installed no handler"
    assert all(stream is sys.stderr for stream in streams)


@pytest.mark.parametrize(
    ("debug", "expected"),
    [
        # A plain run says only what it means to say.
        (False, "server listening on 127.0.0.1:8765"),
        # --debug attributes the line, so a websockets record cannot be mistaken for
        # one of ours. The pair below is exactly what looked like a duplicated startup
        # message before: two loggers, one fact.
        (True, "websockets.server: server listening on 127.0.0.1:8765"),
    ],
)
def test_debug_logging_names_the_logger_that_emitted_each_record(
    debug: bool, expected: str
) -> None:
    """The format, not the level, is what makes --debug output readable.

    Library records reach the same handler as ours: `serve()` is called without a
    `logger=`, so websockets logs through `websockets.server`. Under `%(message)s`
    alone its startup line is indistinguishable from `cli.py`'s own.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    captured = io.StringIO()
    try:
        configure_logging(debug=debug)
        for handler in root.handlers:
            handler.setStream(captured)
        logging.getLogger("websockets.server").info(
            "server listening on %s:%s", "127.0.0.1", 8765
        )
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    assert captured.getvalue().strip() == expected
