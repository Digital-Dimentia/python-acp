from __future__ import annotations

import asyncio
import dataclasses
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
    MalformedServerRequest,
    MCPClientCapabilities,
    MCPProtocolError,
    MCPStdioClient,
    UnsupportedServerRequest,
    tool_result_text,
)


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def test_build_parser_accepts_debug_flag() -> None:
    args = build_parser().parse_args(["--debug"])
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
async def test_a_handler_can_answer_bad_params_without_claiming_we_broke() -> None:
    """The third answer a handler needs: not a result, not -32601, and not our fault."""

    async def handler(method: str, params: dict) -> dict:
        raise MalformedServerRequest("requestedSchema is not an object")

    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, on_server_request=handler) as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.call_tool("provoke", {"server_method": "elicitation/create"}), timeout=10
        )

    reply = json.loads(result["content"][0]["text"])
    assert reply["error"]["code"] == -32602
    assert "requestedSchema" in reply["error"]["message"]


@pytest.mark.asyncio
async def test_a_slow_handler_does_not_stop_the_read_loop() -> None:
    """A handler may wait on a human, so it must not be awaited by the reader.

    `tests/test_elicitation.py` proves the same property through the real forwarder;
    this pins it as `mcp_stdio`'s own contract, where the decision lives.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(method: str, params: dict) -> dict:
        entered.set()
        await release.wait()
        return {"roots": []}

    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, on_server_request=handler) as client:
        await client.initialize()
        sent = await asyncio.wait_for(
            client.call_tool("provoke-detached", {"server_method": "roots/list"}), timeout=10
        )
        assert sent["content"][0]["text"] == "sent"
        assert entered.is_set()
        release.set()


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


# ---------------------------------------------------------------------------
# Cancellation: a request we stop waiting for must be un-asked, not merely
# forgotten, or the server keeps working on a reply nobody will read.
# (pyacp-ua1)
# ---------------------------------------------------------------------------


async def _cancel_report(client: MCPStdioClient) -> dict:
    """Ask the fixture what it actually received on the wire."""
    result = await asyncio.wait_for(client.call_tool("cancel-report", {}), timeout=10)
    return json.loads(tool_result_text(result))


@pytest.mark.asyncio
async def test_timeout_sends_notifications_cancelled_for_that_request() -> None:
    """The abandoned request's own id is what reaches the server."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, request_timeout=0.5) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)

        with pytest.raises(MCPProtocolError, match="Timed out"):
            await client.call_tool("stall", {})

        report = await _cancel_report(client)

    assert report["stalled"], "the fixture never saw the stalled request"
    assert [c["requestId"] for c in report["cancelled"]] == [report["stalled"][-1]]
    assert "timed out" in report["cancelled"][0]["reason"]


@pytest.mark.asyncio
async def test_timeout_cancellation_does_not_disturb_later_requests() -> None:
    """A cancelled request is one request, not a broken client."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, request_timeout=0.5) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        with pytest.raises(MCPProtocolError):
            await client.call_tool("stall", {})

        result = await asyncio.wait_for(client.call_tool("echo", {"text": "hi"}), timeout=10)

    assert result["content"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_cancel_request_is_usable_outside_the_timeout_path() -> None:
    """The notification path is a method, not a branch inside the timeout.

    `pyacp-tzd.5` cancels in-flight requests for reasons that have nothing to do
    with a timeout; this asserts it can, with its own reason text.
    """
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        await client.cancel_request(4242, reason="ACP client cancelled the turn")

        report = await _cancel_report(client)

    assert report["cancelled"] == [
        {"requestId": 4242, "reason": "ACP client cancelled the turn"}
    ]


@pytest.mark.asyncio
async def test_cancel_request_omits_an_absent_reason() -> None:
    """`reason` is optional; an empty one is left off rather than sent empty."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        await client.cancel_request(7)

        report = await _cancel_report(client)

    assert report["cancelled"] == [{"requestId": 7}]


@pytest.mark.asyncio
async def test_cancel_request_never_raises_when_the_process_is_gone() -> None:
    """Cancelling is a courtesy; it must not mask the failure that prompted it."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    client = MCPStdioClient(cmd)
    await client.start()
    await asyncio.wait_for(client.initialize(), timeout=10)
    await client.stop()

    await client.cancel_request(1, reason="after shutdown")


@pytest.mark.asyncio
async def test_cancel_request_swallows_a_broken_pipe_mid_write() -> None:
    """The is_closing() guard cannot catch a subprocess that dies during drain().

    That window surfaces as BrokenPipeError, not MCPProtocolError. If it escaped,
    the timeout path would raise an OSError in place of the MCPProtocolError its
    caller is documented to raise.
    """
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)

        async def die_mid_write(payload: dict[str, object]) -> None:
            raise BrokenPipeError(32, "Broken pipe")

        client._write = die_mid_write  # type: ignore[method-assign]

        await client.cancel_request(7, reason="subprocess died mid-write")


@pytest.mark.asyncio
async def test_timeout_still_raises_mcp_error_when_cancelling_breaks() -> None:
    """A failed cancel must not replace the timeout error that prompted it."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, request_timeout=0.5) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)

        real_write = client._write
        sent_first = False

        async def die_after_the_request(payload: dict[str, object]) -> None:
            nonlocal sent_first
            if not sent_first:
                sent_first = True
                await real_write(payload)
                return
            raise ConnectionResetError(54, "Connection reset by peer")

        client._write = die_after_the_request  # type: ignore[method-assign]

        with pytest.raises(MCPProtocolError, match="Timed out"):
            await client.call_tool("stall", {})


@pytest.mark.asyncio
async def test_initialize_is_never_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP forbids cancelling the handshake, timeout or not."""
    monkeypatch.setenv("MOCK_MCP_STALL_INITIALIZE", "1")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, request_timeout=0.5) as client:
        with pytest.raises(MCPProtocolError, match="Timed out"):
            await client.initialize()

        report = await _cancel_report(client)

    assert report["stalled"], "the fixture never saw the stalled initialize"
    assert report["cancelled"] == []


# ---------------------------------------------------------------------------
# Cancellation, the other half: the *caller* is cancelled rather than timing
# out. `session/cancel` tearing down a turn mid `tools/call` is that case.
# (pyacp-hnk.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_abandoned_request_tells_the_server_to_stop() -> None:
    """A cancelled caller leaves the server in exactly the state a timeout does.

    The default `request_timeout` is 30s, so nothing here is waiting for one: the
    notification arrives because the call was abandoned, not because it expired.
    """
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)

        call = asyncio.create_task(client.call_tool("stall", {}))
        await asyncio.sleep(0.2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        report = await _cancel_report(client)

    assert report["stalled"], "the fixture never saw the stalled request"
    assert [c["requestId"] for c in report["cancelled"]] == [report["stalled"][-1]]
    assert "cancelled" in report["cancelled"][0]["reason"]


@pytest.mark.asyncio
async def test_an_abandoned_request_still_reports_cancellation_to_its_caller() -> None:
    """Returning a value from a cancelled coroutine would tell asyncio the cancel
    did not take. The notification is sent *and* the exception still propagates."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)

        call = asyncio.create_task(client.call_tool("stall", {}))
        await asyncio.sleep(0.2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=5)

        assert call.cancelled() is True


@pytest.mark.asyncio
async def test_an_abandoned_initialize_is_not_cancelled_either() -> None:
    """MCP forbids cancelling the handshake — for a caller that gave up, too."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, env={"MOCK_MCP_STALL_INITIALIZE": "1"}) as client:
        handshake = asyncio.create_task(client.initialize())
        await asyncio.sleep(0.2)
        handshake.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handshake

        report = await _cancel_report(client)

    assert report["stalled"], "the fixture never saw the stalled initialize"
    assert report["cancelled"] == []


@pytest.mark.asyncio
async def test_abandoning_one_request_does_not_disturb_the_next() -> None:
    """The client survives a cancelled call: one request ended, not the connection."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        call = asyncio.create_task(client.call_tool("stall", {}))
        await asyncio.sleep(0.2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        result = await asyncio.wait_for(client.call_tool("echo", {"text": "hi"}), timeout=10)

    assert result["content"][0]["text"] == "hi"


# ---------------------------------------------------------------------------
# Client capabilities: a capability block is a promise, and `initialize` is
# where it is made. (pyacp-pb7)
# ---------------------------------------------------------------------------


async def handshake_params(client: MCPStdioClient) -> dict:
    """The `initialize` params as the fixture actually received them.

    Read off the wire rather than off the client, because what a capability block
    promises is what arrived at the server.
    """
    result = await asyncio.wait_for(client.call_tool("handshake-report", {}), timeout=10)
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_a_client_that_can_answer_nothing_declares_nothing() -> None:
    """An empty block is the honest one for a client with no handler wired up."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        params = await handshake_params(client)

    assert params["capabilities"] == {}
    assert params["protocolVersion"] == _MCP_PROTOCOL_VERSION == "2025-06-18"


@pytest.mark.asyncio
async def test_declared_capabilities_reach_the_server_in_the_handshake() -> None:
    """Absent means unsupported, so an undeclared capability contributes no key."""

    async def handler(method: str, params: dict) -> dict:
        return {"roots": []}

    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(
        cmd,
        on_server_request=handler,
        client_capabilities=MCPClientCapabilities(roots=True),
    ) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        params = await handshake_params(client)

    assert params["capabilities"] == {"roots": {"listChanged": False}}


@pytest.mark.asyncio
async def test_declaring_a_capability_with_no_handler_is_refused() -> None:
    """Promising what nothing answers strands the server; fail before the wire."""
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(
        cmd, client_capabilities=MCPClientCapabilities(roots=True)
    ) as client:
        with pytest.raises(RuntimeError, match="no on_server_request handler"):
            await asyncio.wait_for(client.initialize(), timeout=10)

        # Nothing was negotiated, because nothing was sent.
        assert client.protocol_version is None


def test_the_capability_block_has_no_way_to_declare_sampling() -> None:
    """There is no LLM in this runtime, so the field does not exist to be set wrong."""
    names = {f.name for f in dataclasses.fields(MCPClientCapabilities)}
    assert "sampling" not in names
    assert names == {"roots", "roots_list_changed", "elicitation"}

    everything = MCPClientCapabilities(roots=True, roots_list_changed=True, elicitation=True)
    assert everything.to_wire() == {"roots": {"listChanged": True}, "elicitation": {}}


def test_roots_list_changed_without_roots_is_rejected() -> None:
    """A change notification for a list we never offered means nothing."""
    with pytest.raises(ValueError, match="no list to change"):
        MCPClientCapabilities(roots_list_changed=True)


@pytest.mark.asyncio
async def test_a_method_the_handler_does_not_serve_is_32601_not_32603() -> None:
    """`-32603` says *we broke*; the truth is *we never offered this*."""

    async def handler(method: str, params: dict) -> dict:
        raise UnsupportedServerRequest(method)

    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd, on_server_request=handler) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)
        result = await asyncio.wait_for(
            client.call_tool("provoke", {"server_method": "sampling/createMessage"}),
            timeout=10,
        )

    reply = json.loads(result["content"][0]["text"])
    assert reply["error"]["code"] == -32601
    assert "sampling/createMessage" in reply["error"]["message"]


@pytest.mark.asyncio
async def test_a_server_pinned_to_the_previous_revision_is_still_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """We propose 2025-06-18; hanging up on a 2024-11-05 counter would drop real servers."""
    monkeypatch.setenv("MOCK_MCP_PROTOCOL_VERSION", "2024-11-05")
    cmd = [sys.executable, str(FIXTURE_SERVER)]
    async with MCPStdioClient(cmd) as client:
        await asyncio.wait_for(client.initialize(), timeout=10)

        assert client.protocol_version == "2024-11-05"
        # And the connection is usable, not merely un-hung-up-on.
        assert [tool["name"] for tool in await client.list_tools()] == ["echo"]
