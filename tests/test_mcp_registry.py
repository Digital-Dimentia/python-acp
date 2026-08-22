"""Tests for the per-session MCP backend registry.

Most of these drive a fake connector rather than spawning anything: what is under test
is *lifetime* — all-or-nothing opening, teardown ordering, one-failure-does-not-strand-
the-rest — and a real subprocess adds a handshake and two timeouts without exercising
one extra line of it. Two tests do use the real `tests/fixtures/mock_mcp_server.py`,
because "the spec actually becomes a running server" is not something a fake can prove.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from acp.schema import EnvVariable, McpServerStdio

from python_acp.mcp_registry import (
    McpBackendRegistry,
    UnknownBackendError,
    connect_stdio,
)
from python_acp.mcp_stdio import MCPProtocolError

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def spec(name: str = "tools", *, env: list[EnvVariable] | None = None) -> McpServerStdio:
    return McpServerStdio(
        name=name,
        command=sys.executable,
        args=[str(FIXTURE_SERVER)],
        env=env or [],
    )


class FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


class FakeConnector:
    """Hands back `FakeClient`s, and fails on any name in `fail_on`."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.opened: list[FakeClient] = []

    async def __call__(self, server: McpServerStdio) -> FakeClient:
        if server.name in self.fail_on:
            raise MCPProtocolError(f"{server.name} refused to start")
        client = FakeClient(server.name)
        self.opened.append(client)
        return client


def fake_registry(**kwargs) -> tuple[McpBackendRegistry, FakeConnector]:
    connector = FakeConnector(**kwargs)
    return McpBackendRegistry(connect=connector), connector


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


async def test_a_session_with_no_servers_opens_nothing() -> None:
    registry, connector = fake_registry()

    assert await registry.open("s1", []) == {}
    assert connector.opened == []


async def test_every_named_server_is_opened_and_addressable() -> None:
    registry, _ = fake_registry()

    opened = await registry.open("s1", [spec("a"), spec("b")])

    assert sorted(opened) == ["a", "b"]
    assert registry.get("s1", "a") is opened["a"]
    assert "s1" in registry


async def test_duplicate_names_are_refused() -> None:
    """`pyacp-hnk.2` routes a tool call by server name; two of one name is a coin toss."""
    registry, connector = fake_registry()

    with pytest.raises(ValueError, match="Duplicate MCP server names"):
        await registry.open("s1", [spec("a"), spec("a")])

    assert connector.opened == []
    assert "s1" not in registry


async def test_a_failure_partway_through_tears_down_what_started() -> None:
    """All-or-nothing. A half-open session leaks the subprocesses that did come up."""
    registry, connector = fake_registry(fail_on={"b"})

    with pytest.raises(MCPProtocolError):
        await registry.open("s1", [spec("a"), spec("b"), spec("c")])

    assert [client.name for client in connector.opened] == ["a"]
    assert connector.opened[0].stopped == 1
    assert "s1" not in registry


async def test_opening_a_session_twice_is_a_bug_not_a_merge() -> None:
    registry, _ = fake_registry()
    await registry.open("s1", [spec("a")])

    with pytest.raises(RuntimeError, match="already has MCP backends"):
        await registry.open("s1", [spec("b")])


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


async def test_an_unknown_server_name_reaches_the_client_as_invalid_params() -> None:
    from python_acp.errors import to_request_error

    registry, _ = fake_registry()
    await registry.open("s1", [spec("a")])

    with pytest.raises(UnknownBackendError) as excinfo:
        registry.get("s1", "nope")

    assert to_request_error(excinfo.value).code == -32602


def test_an_unknown_session_simply_has_no_backends() -> None:
    """Not an error: a session that named no servers and an unknown one look the same."""
    registry, _ = fake_registry()

    assert registry.backends("never-opened") == {}


async def test_the_returned_mapping_is_a_copy() -> None:
    registry, _ = fake_registry()
    await registry.open("s1", [spec("a")])

    registry.backends("s1").clear()

    assert list(registry.backends("s1")) == ["a"]


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


async def test_closing_a_session_stops_its_servers() -> None:
    registry, connector = fake_registry()
    await registry.open("s1", [spec("a"), spec("b")])

    await registry.close("s1")

    assert [client.stopped for client in connector.opened] == [1, 1]
    assert "s1" not in registry


async def test_closing_a_session_that_opened_nothing_is_a_no_op() -> None:
    """The `on_close` hook fires for every session, most of which named no servers."""
    registry, _ = fake_registry()

    await registry.close("never-opened")


async def test_one_server_that_will_not_stop_does_not_strand_the_rest() -> None:
    """The leak, not the failure, is what costs something."""

    class Stubborn(FakeClient):
        async def stop(self) -> None:
            raise OSError("will not die")

    connector = FakeConnector()
    registry = McpBackendRegistry(connect=connector)

    async def connect(server: McpServerStdio):
        client = Stubborn(server.name) if server.name == "bad" else FakeClient(server.name)
        connector.opened.append(client)
        return client

    registry._connect = connect  # noqa: SLF001 — the seam exists for exactly this
    await registry.open("s1", [spec("bad"), spec("good")])

    await registry.close("s1")

    assert connector.opened[1].stopped == 1
    assert "s1" not in registry


async def test_close_all_empties_the_registry() -> None:
    registry, connector = fake_registry()
    await registry.open("s1", [spec("a")])
    await registry.open("s2", [spec("b")])

    await registry.close_all()

    assert len(registry) == 0
    assert all(client.stopped == 1 for client in connector.opened)


# ---------------------------------------------------------------------------
# Against a real subprocess
# ---------------------------------------------------------------------------


async def test_a_spec_really_becomes_a_running_handshaken_server() -> None:
    registry = McpBackendRegistry()

    opened = await registry.open("s1", [spec("tools", env=[EnvVariable(name="X", value="1")])])
    try:
        assert [tool["name"] for tool in await opened["tools"].list_tools()] == ["echo"]
        assert opened["tools"].protocol_version is not None
    finally:
        await registry.close("s1")


async def test_the_handshake_happens_at_open_not_on_first_use() -> None:
    """A server that cannot negotiate is a `session/new` failure the client can act on.

    Discovering it mid-turn would surface as a broken prompt with no explanation.
    """
    with pytest.raises(Exception):  # noqa: B017 — any startup failure is the point
        await connect_stdio(
            McpServerStdio(name="silent", command=sys.executable, args=["-c", "pass"], env=[])
        )


async def test_declared_env_reaches_the_subprocess_on_top_of_our_own() -> None:
    """Overlaid, not replacing: a server command almost always needs PATH to run at all."""
    registry = McpBackendRegistry()
    opened = await registry.open(
        "s1", [spec("tools", env=[EnvVariable(name="PYTHON_ACP_TEST_MARKER", value="set")])]
    )
    try:
        assert opened["tools"].env == {"PYTHON_ACP_TEST_MARKER": "set"}
        assert await opened["tools"].list_tools()
    finally:
        await registry.close("s1")


# ---------------------------------------------------------------------------
# The seam to sessions.py (decision B6a)
# ---------------------------------------------------------------------------


async def test_closing_a_session_tears_down_its_backends_through_the_hook() -> None:
    """`cli.py` wires these two together; nothing else can, and nothing else should.

    `sessions.py` deliberately never imports MCP, so `on_close` is the entire coupling.
    A deployment that forgot to pass it would leak a subprocess per session.
    """
    from python_acp.sessions import SessionRegistry

    registry, connector = fake_registry()
    sessions = SessionRegistry(on_close=registry.close)
    session = sessions.create("/work")
    await registry.open(session.session_id, [spec("a")])

    await sessions.close(session.session_id)

    assert connector.opened[0].stopped == 1
    assert session.session_id not in registry
