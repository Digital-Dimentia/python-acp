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
    # The first one is not part of the turn at all: `session/new` announces the session's
    # commands once its id has reached the client (`pyacp-p8v`), which is what lets a
    # palette be populated before the first prompt. The turn then announces its own.
    #
    # **This index is a real ordering guarantee, not luck** — and it was luck until
    # `pyacp-svt`. This client pipelines: it sends `session/prompt` the instant
    # `new_session` returns, so the announcement has to be enqueued before the agent can
    # even read that request. It is, because `agent._prepare_commands` builds the list
    # inside `session/new` and leaves the stream observer nothing to await but the send.
    # If this assertion starts flapping, that is what broke; see `announcer.md`.
    assert report["updates"][:6] == [
        "available_commands_update",
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


def test_a_foreign_client_serves_the_file_round_trip(report: dict) -> None:
    """`pyacp-8bv.2` over a real pipe: the agent opened neither file.

    The client is the only place that can see what actually arrived on the wire, so the
    recorded read is what proves `line` and `limit` were sent and that the path was the
    **resolved** one — a client asked to re-walk a symlink would be opening something the
    containment check never saw.
    """
    assert report["fileStopReason"] == "end_turn"
    (path, line, limit) = report["reads"][0]
    assert (line, limit) == (2, 1)
    assert path == str(Path(path).resolve()) and path.endswith("/in.txt")
    # The tool echoed only the requested window back, and the client wrote it.
    assert report["written"] == "two\n"
    assert report["writes"] == [path.replace("in.txt", "out.txt")]


def test_a_foreign_client_is_never_asked_to_open_a_path_outside_the_roots(report: dict) -> None:
    """Refused before the call, not after: the read count does not grow."""
    assert report["outsideStopReason"] == "refusal"
    assert len(report["reads"]) == 1


def test_a_foreign_client_runs_a_command_in_its_own_terminal(report: dict) -> None:
    """`pyacp-8bv.3` over a real pipe, and the one assertion only a client can make.

    `outputByteLimit` is an optional schema field the agent promises never to omit. An
    in-process test hands the client a Python keyword argument, so it would pass even if
    the field were never encoded; the number below arrived as JSON.
    """
    assert report["terminalStopReason"] == "end_turn"
    assert report["terminalLimits"] == [1024 * 1024]
    # The command's output reached the tool, and the tool echoed it back.
    assert "from a client terminal\n" in report["texts"]


def test_a_foreign_client_gets_every_terminal_back(report: dict) -> None:
    """The leak this bead is about, observed from the side that would have leaked."""
    assert report["terminalsReleased"] == ["terminal-1"]
    assert report["terminalsLeftOpen"] == []
