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
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.stdio import spawn_agent_process

from python_acp import __version__
from python_acp.cli import build_parser, configure_logging
from python_acp.transport_stdio import _stdout_reserved

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"

AGENT_ARGV = [
    "-m",
    "python_acp.cli",
    "--transport",
    "stdio",
    "--mcp-command",
    sys.executable,
    str(FIXTURE_SERVER),
]


class _NullClient:
    """The minimum an ACP client must be to hold up its end of a connection."""

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        return None

    async def request_permission(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("The skeleton agent must not request permission yet")


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


async def test_unbuilt_methods_answer_method_not_found_over_the_wire() -> None:
    """The -32601 must survive the round trip, not just the router."""
    async with agent_process() as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

        with pytest.raises(RequestError) as excinfo:
            await asyncio.wait_for(conn.new_session(cwd="/tmp", mcp_servers=[]), timeout=30)

    assert excinfo.value.code == -32601


async def test_the_unstable_methods_are_reachable_over_stdio() -> None:
    """run_stdio passes use_unstable_protocol=True, so these reach the agent.

    They answer -32601 today because Phase 2 has not filled them in — but that error
    comes from `PythonAcpAgent`, not from the router refusing to dispatch. If the flag
    were dropped the code would be identical and this test would still pass, which is
    why the router-level direction test in test_agent.py exists alongside it.
    """
    async with agent_process() as (conn, _proc):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)

        with pytest.raises(RequestError) as excinfo:
            await asyncio.wait_for(conn.close_session(session_id="s1"), timeout=30)

    assert excinfo.value.code == -32601


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


def test_ws_stays_the_default_transport() -> None:
    args = build_parser().parse_args(["--mcp-command", "echo"])

    assert args.transport == "ws"
    assert args.host == "127.0.0.1"
    assert args.port == 8765


def test_stdio_is_selectable() -> None:
    args = build_parser().parse_args(["--mcp-command", "echo", "--transport", "stdio"])

    assert args.transport == "stdio"


def test_an_unknown_transport_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mcp-command", "echo", "--transport", "carrier-pigeon"])


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
