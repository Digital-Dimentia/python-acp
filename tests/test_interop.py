"""Interop: a client that shares no code with the agent completes a whole session.

`tests/interop/acp_client.py` runs as **its own process**, imports `acp` and the standard
library and nothing from `python_acp`, and talks to the agent only over a pipe. Every
message it sends is built by the SDK and every reply is parsed by the SDK, so a session
completing there proves the wire is sufficient — which is the one thing our own suite,
importing both halves, structurally cannot prove.

The client that is genuinely *not ours* — the SDK's `examples/client.py` — cannot run in
CI: it fetches from GitHub and reads from a console. `docs/interop.md` is its runbook,
with the transcript of an actual run, per the bead's instruction to record rather than
skip.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

INTEROP_CLIENT = Path(__file__).parent / "interop" / "acp_client.py"
FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"
TIMEOUT = 120


@pytest.fixture(scope="module")
def report() -> dict:
    """One interop run, shared by every assertion below.

    Module-scoped because the run spawns two processes and an MCP subprocess; asserting
    against one recorded transcript is both faster and closer to what the bead asks for —
    a failure diagnosable from the transcript rather than from an exit code.
    """
    return asyncio.run(_run())


async def _run() -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(INTEROP_CLIENT),
        sys.executable,
        str(FIXTURE_SERVER),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT)
    assert process.returncode == 0, f"interop client failed:\n{stderr.decode()}"
    return json.loads(stdout.decode().strip().splitlines()[-1])


def test_the_interop_client_shares_no_code_with_the_agent() -> None:
    """The constraint that gives the whole file its value, asserted rather than trusted.

    Checked on the parsed imports rather than the text: the file names `python_acp.cli`
    in the argv it spawns and in its own docstring, and neither is sharing code.
    """
    import ast

    tree = ast.parse(INTEROP_CLIENT.read_text())
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert "python_acp" not in imported
    assert "acp" in imported


def test_a_foreign_client_completes_the_handshake(report: dict) -> None:
    assert report["protocolVersion"] == 1
    assert report["agentInfo"] == "python-acp"


def test_a_foreign_client_creates_a_session_with_its_own_mcp_server(report: dict) -> None:
    assert report["sessionId"] is True
    assert report["sessions"] == 1


def test_a_foreign_client_runs_a_tool_and_sees_the_whole_update_stream(report: dict) -> None:
    """The payoff for decision D2: strict ACP v1 means something outside this repo."""
    assert report["stopReason"] == "end_turn"
    assert report["updates"][:5] == [
        "user_message_chunk",
        "available_commands_update",
        "plan",
        "plan",
        "tool_call",
    ]
    assert "tool_call_update" in report["updates"]


def test_a_client_that_refuses_permission_still_gets_its_tool_run(report: dict) -> None:
    """The interop finding this suite was written to catch.

    The client answers `-32601` to `session/request_permission`, copying the SDK's own
    example. An earlier implementation refused the turn, which made python-acp unusable
    against the reference client; see `docs/interop.md`.
    """
    assert report["permissionRequests"] == 1
    assert report["stopReason"] == "end_turn"


def test_a_foreign_client_gets_a_refusal_it_can_read(report: dict) -> None:
    assert report["refusedStopReason"] == "refusal"


def test_the_agent_exits_cleanly_when_the_foreign_client_hangs_up(report: dict) -> None:
    assert report["agentExitCode"] == 0
