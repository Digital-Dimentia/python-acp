from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Callable

import pytest

from python_acp.cli import build_parser
from python_acp.mcp_stdio import (
    _MCP_PROTOCOL_VERSION,
    MCPProtocolError,
    MCPStdioClient,
)


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def test_build_parser_accepts_debug_flag() -> None:
    args = build_parser().parse_args(["--mcp-command", "python", "script.py", "--debug"])
    assert args.debug is True


@pytest.mark.asyncio
async def test_list_tools_and_call_tool_over_stdio() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert tools[0]["name"] == "echo"

        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_list_prompts_get_prompt_and_read_resource_over_stdio() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        prompts = await client.list_prompts()
        assert prompts[0]["name"] == "greeting"

        prompt_result = await client.get_prompt("greeting", {"name": "Ava"})
        assert prompt_result["messages"][0]["content"]["text"] == "Hello, Ava!"

        resources = await client.list_resources()
        assert resources[0]["name"] == "greeting-resource"

        resource = await client.read_resource("greeting://{name}", {"name": "Ava"})
        assert resource["contents"][0]["text"] == "Hello, Ava!"


@pytest.mark.asyncio
async def test_call_tool_raises_for_unknown_tool() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        with pytest.raises(MCPProtocolError):
            await client.call_tool("missing", {})


@pytest.mark.asyncio
async def test_noisy_stderr_does_not_deadlock_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that floods stderr must still be able to answer requests.

    256 KiB comfortably exceeds the OS pipe buffer, so without a drain the
    server blocks on its own stderr write and never reads stdin.
    """
    monkeypatch.setenv("MOCK_MCP_STDERR_BYTES", str(256 * 1024))
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        tools = await asyncio.wait_for(client.list_tools(), timeout=10)

    assert tools[0]["name"] == "echo"


@pytest.mark.asyncio
async def test_stderr_lines_are_logged_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MOCK_MCP_STDERR_BYTES", "512")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    with caplog.at_level(logging.DEBUG, logger="python_acp.mcp_stdio"):
        async with MCPStdioClient(cmd) as client:
            await client.initialize()
            for _ in range(100):
                if any("MCP server stderr" in message for message in caplog.messages):
                    break
                await asyncio.sleep(0.01)

    assert any("mock-mcp noise" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_stop_is_idempotent_with_stderr_drain() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    await client.start()
    await client.initialize()
    await client.stop()
    await client.stop()

    assert client._stderr_task is None


@pytest.mark.asyncio
async def test_stop_closes_stdin_before_signalling_the_server() -> None:
    """MCP stdio shutdown starts at EOF, not at SIGTERM.

    The fixture server exits when its stdin closes, so a client that follows
    the prescribed sequence never signals it: exit status 0 and no call to
    terminate() or kill() can only happen if stdin was closed first.
    """
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    await client.start()
    await client.initialize()

    proc = client._proc
    assert proc is not None

    # Record the signals but still deliver them, so an implementation that
    # skips the stdin close fails this assertion instead of hanging on a
    # process nothing ever killed.
    signals: list[str] = []
    real_terminate, real_kill = proc.terminate, proc.kill

    def spy(name: str, real: Callable[[], None]) -> Callable[[], None]:
        def send() -> None:
            signals.append(name)
            real()

        return send

    proc.terminate = spy("SIGTERM", real_terminate)  # type: ignore[method-assign]
    proc.kill = spy("SIGKILL", real_kill)  # type: ignore[method-assign]

    await asyncio.wait_for(client.stop(), timeout=15)

    assert signals == []
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_stop_escalates_to_sigterm_when_the_server_ignores_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_MCP_IGNORE_EOF", "1")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    client._STOP_STDIN_TIMEOUT = 0.5  # type: ignore[misc]
    await client.start()
    await client.initialize()

    proc = client._proc
    assert proc is not None
    await client.stop()

    assert proc.returncode == -signal.SIGTERM


@pytest.mark.asyncio
async def test_stop_escalates_to_sigkill_when_sigterm_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_MCP_IGNORE_EOF", "1")
    monkeypatch.setenv("MOCK_MCP_IGNORE_SIGTERM", "1")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    client._STOP_STDIN_TIMEOUT = 0.5  # type: ignore[misc]
    client._STOP_TERMINATE_TIMEOUT = 0.5  # type: ignore[misc]
    await client.start()
    await client.initialize()

    proc = client._proc
    assert proc is not None
    await client.stop()

    assert proc.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_stop_on_an_already_exited_server_is_clean() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    await client.start()
    await client.initialize()

    proc = client._proc
    assert proc is not None
    proc.kill()
    await proc.wait()

    await client.stop()

    assert client._proc is None
    assert client._stdout_task is None


@pytest.mark.asyncio
async def test_server_request_is_answered_even_without_a_handler() -> None:
    """An unanswered server request strands the server; reply -32601 instead."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.call_tool("provoke", {"server_method": "roots/list"}), timeout=10
        )

    reply = json.loads(result["content"][0]["text"])
    assert reply["id"] == "srv-1"
    assert reply["error"]["code"] == -32601
    assert "roots/list" in reply["error"]["message"]


@pytest.mark.asyncio
async def test_server_ping_is_answered_without_a_handler() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.call_tool("provoke", {"server_method": "ping"}), timeout=10
        )

    reply = json.loads(result["content"][0]["text"])
    assert reply["id"] == "srv-1"
    assert reply["result"] == {}


@pytest.mark.asyncio
async def test_on_server_request_handler_supplies_the_result() -> None:
    seen: list[str] = []

    async def handler(method: str, params: dict) -> dict:
        seen.append(method)
        return {"roots": [{"uri": "file:///tmp", "name": "tmp"}]}

    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, on_server_request=handler) as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.call_tool("provoke", {"server_method": "roots/list"}), timeout=10
        )

    reply = json.loads(result["content"][0]["text"])
    assert seen == ["roots/list"]
    assert reply["result"]["roots"][0]["name"] == "tmp"


@pytest.mark.asyncio
async def test_failing_server_request_handler_still_replies() -> None:
    async def handler(method: str, params: dict) -> dict:
        raise RuntimeError("handler exploded")

    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, on_server_request=handler) as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.call_tool("provoke", {"server_method": "roots/list"}), timeout=10
        )

    reply = json.loads(result["content"][0]["text"])
    assert reply["error"]["code"] == -32603
    assert "handler exploded" in reply["error"]["message"]


@pytest.mark.asyncio
async def test_undeliverable_error_reply_logs_the_method_not_the_failure_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one thing worth knowing there is which request went unanswered."""
    client = MCPStdioClient([sys.executable, str(FIXTURE_SERVER)])

    # No start(), so the write fails and _respond takes its except branch.
    with caplog.at_level(logging.DEBUG, logger="python_acp.mcp_stdio"):
        await client._respond_error(7, -32603, "handler exploded", "sampling/createMessage")

    logged = [m for m in caplog.messages if "Could not reply" in m]
    assert logged == ["Could not reply to MCP server request sampling/createMessage"]


@pytest.mark.asyncio
async def test_server_notifications_reach_the_handler() -> None:
    received: list[tuple[str, dict]] = []

    async def on_notification(method: str, params: dict) -> None:
        received.append((method, params))

    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, on_notification=on_notification) as client:
        await client.initialize()
        await asyncio.wait_for(
            client.call_tool("provoke", {"server_method": "ping"}), timeout=10
        )

    assert ("notifications/message", {"level": "info", "data": "provoked"}) in received


@pytest.mark.asyncio
async def test_concurrent_requests_are_all_answered() -> None:
    """The write lock no longer spans the read, so requests pipeline."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        results = await asyncio.wait_for(
            asyncio.gather(*(client.call_tool("echo", {"text": str(n)}) for n in range(10))),
            timeout=10,
        )

    assert [r["content"][0]["text"] for r in results] == [str(n) for n in range(10)]


@pytest.mark.asyncio
async def test_pending_requests_fail_when_the_process_stops() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    await client.start()
    await client.initialize()
    await client.stop()

    with pytest.raises(MCPProtocolError):
        await client.request("tools/list", {})


@pytest.mark.asyncio
async def test_list_wrappers_follow_next_cursor_to_the_last_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every page is accumulated, not just the first one the server sends."""
    monkeypatch.setenv("MOCK_MCP_LIST_PAGES", "3")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        tools = await asyncio.wait_for(client.list_tools(), timeout=10)
        prompts = await asyncio.wait_for(client.list_prompts(), timeout=10)
        resources = await asyncio.wait_for(client.list_resources(), timeout=10)

    assert [t["name"] for t in tools] == ["echo", "echo-1", "echo-2"]
    assert [p["name"] for p in prompts] == ["greeting", "greeting-1", "greeting-2"]
    assert [r["name"] for r in resources] == [
        "greeting-resource",
        "greeting-resource-1",
        "greeting-resource-2",
    ]


@pytest.mark.asyncio
async def test_empty_page_is_not_a_terminator(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent nextCursor ends the walk; a page with no items does not."""
    monkeypatch.setenv("MOCK_MCP_LIST_PAGES", "2")
    monkeypatch.setenv("MOCK_MCP_LIST_EMPTY_MIDDLE", "1")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        tools = await asyncio.wait_for(client.list_tools(), timeout=10)

    assert [t["name"] for t in tools] == ["echo-1"]


@pytest.mark.asyncio
async def test_repeated_cursor_raises_instead_of_looping_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_MCP_LIST_STUCK", "1")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        with pytest.raises(MCPProtocolError, match="repeated cursor"):
            await asyncio.wait_for(client.list_tools(), timeout=10)


@pytest.mark.asyncio
async def test_unbounded_page_count_raises_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct cursors forever still terminate, via the hard page bound."""
    monkeypatch.setenv(
        "MOCK_MCP_LIST_PAGES", str(MCPStdioClient._MAX_LIST_PAGES + 5)
    )
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        with pytest.raises(MCPProtocolError, match="exceeded"):
            await asyncio.wait_for(client.list_tools(), timeout=30)


@pytest.mark.asyncio
async def test_single_page_list_sends_no_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first request must omit `cursor` entirely, not send null."""
    sent: list[dict] = []
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        original = client.request

        async def spy(method: str, params: dict | None = None) -> dict:
            sent.append({"method": method, "params": params})
            return await original(method, params)

        client.request = spy  # type: ignore[method-assign]
        tools = await asyncio.wait_for(client.list_tools(), timeout=10)

    assert [t["name"] for t in tools] == ["echo"]
    assert sent == [{"method": "tools/list", "params": {}}]


@pytest.mark.asyncio
async def test_initialize_settles_on_the_version_the_server_returns() -> None:
    """The handshake is a round trip: the server's answer is what we record."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        result = await asyncio.wait_for(client.initialize(), timeout=10)

    # The fixture echoes back whatever was proposed, so this also proves the
    # request carried the version we claim to speak.
    assert result["protocolVersion"] == _MCP_PROTOCOL_VERSION
    assert client.protocol_version == _MCP_PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_initialize_rejects_a_version_it_cannot_speak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server counter-offer we cannot speak fails loudly, not silently."""
    monkeypatch.setenv("MOCK_MCP_PROTOCOL_VERSION", "2026-07-28")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        with pytest.raises(MCPProtocolError, match="2026-07-28"):
            await asyncio.wait_for(client.initialize(), timeout=10)

        assert client.protocol_version is None


@pytest.mark.asyncio
async def test_rejected_version_disconnects_instead_of_proceeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spec says hang up; proceeding would fail later and confusingly."""
    monkeypatch.setenv("MOCK_MCP_PROTOCOL_VERSION", "2026-07-28")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    await client.start()
    with pytest.raises(MCPProtocolError):
        await asyncio.wait_for(client.initialize(), timeout=10)

    assert client._proc is None
    with pytest.raises(MCPProtocolError):
        await client.request("tools/list", {})


@pytest.mark.asyncio
async def test_initialize_rejects_an_omitted_protocol_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protocolVersion is mandatory in the result; absence is not agreement."""
    monkeypatch.setenv("MOCK_MCP_OMIT_PROTOCOL_VERSION", "1")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        with pytest.raises(MCPProtocolError, match="omitted protocolVersion"):
            await asyncio.wait_for(client.initialize(), timeout=10)

        assert client.protocol_version is None
# ---------------------------------------------------------------------------
# Failure fidelity: an MCP error code and a tool's isError are different kinds
# of failure, and neither may be flattened into the other. (pyacp-k5w)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_error_response_carries_code_and_data() -> None:
    """The server's code survives onto the exception instead of being stringified."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        with pytest.raises(MCPProtocolError) as excinfo:
            await client.call_tool(
                "rpc-error",
                {"code": -32602, "message": "bad arguments", "data": {"field": "text"}},
            )

    assert excinfo.value.code == -32602
    assert excinfo.value.data == {"field": "text"}
    assert "bad arguments" in str(excinfo.value)


@pytest.mark.asyncio
async def test_client_raised_errors_carry_no_code() -> None:
    """Failures we invent ourselves must not fake a server-assigned code."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    await client.start()
    await client.initialize()
    await client.stop()

    with pytest.raises(MCPProtocolError) as excinfo:
        await client.request("tools/list", {})

    assert excinfo.value.code is None
    assert excinfo.value.data is None


@pytest.mark.asyncio
async def test_call_tool_does_not_raise_for_a_failed_tool() -> None:
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        result = await client.call_tool("boom", {})

    assert result["isError"] is True
    assert result["content"][0]["text"] == "tool exploded"


@pytest.mark.asyncio
async def test_absent_is_error_is_normalized_to_false() -> None:
    """isError is optional on the wire; callers should not re-derive the default."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        result = await client.call_tool("no-flag", {})

    assert result["isError"] is False


@pytest.mark.asyncio
async def test_malformed_tool_result_is_a_protocol_error() -> None:
    """A non-boolean isError is a broken server, not a tool failure."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await client.initialize()
        original = client.request

        async def bad(method: str, params: dict | None = None) -> dict:
            if method == "tools/call":
                return {"content": [], "isError": "yes"}
            return await original(method, params)

        client.request = bad  # type: ignore[method-assign]
        with pytest.raises(MCPProtocolError, match="isError"):
            await client.call_tool("echo", {"text": "hi"})
