"""Tests for the post-response command announcer.

The observer is deliberately free of the agent, so these drive it with plain JSON-RPC
dicts and a list. What they cannot show here is the *ordering* that motivates the whole
module — that an observer fires after the response is written — because that is the SDK's
behaviour rather than ours. `test_transport_ws.py` asserts it on the wire, over a real
`run_agent`; if the SDK ever stops guaranteeing it, that test fails and this file still
passes, which is the right division.
"""

from __future__ import annotations

import logging

import pytest
from acp.connection import StreamDirection, StreamEvent

from python_acp.announcer import MINTING_METHODS, command_announcer


def incoming(message: dict) -> StreamEvent:
    return StreamEvent(direction=StreamDirection.INCOMING, message=message)


def outgoing(message: dict) -> StreamEvent:
    return StreamEvent(direction=StreamDirection.OUTGOING, message=message)


def recorder() -> tuple[list[str], object]:
    """An `announce` callable that records the ids it was asked to announce."""
    announced: list[str] = []

    async def announce(session_id: str) -> None:
        announced.append(session_id)

    return announced, announce


async def test_a_new_session_is_announced_when_its_response_goes_out() -> None:
    announced, announce = recorder()
    observer = command_announcer(announce)

    await observer(incoming({"jsonrpc": "2.0", "id": 1, "method": "session/new"}))
    assert announced == []  # nothing yet: the client has not been told the id

    await observer(outgoing({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "abc"}}))
    assert announced == ["abc"]


async def test_a_fork_is_announced_too() -> None:
    """`session/fork` mints an id exactly as `session/new` does, and has the same gap."""
    announced, announce = recorder()
    observer = command_announcer(announce)

    await observer(incoming({"jsonrpc": "2.0", "id": 7, "method": "session/fork"}))
    await observer(outgoing({"jsonrpc": "2.0", "id": 7, "result": {"sessionId": "fork-1"}}))
    assert announced == ["fork-1"]


async def test_load_and_resume_are_not_watched() -> None:
    """They announce inline from the handler, where the client already knows the id.

    Watching them here would double the notification, not fix anything.
    """
    assert MINTING_METHODS == {"session/new", "session/fork"}
    announced, announce = recorder()
    observer = command_announcer(announce)

    for method in ("session/load", "session/resume", "session/prompt", "initialize"):
        await observer(incoming({"jsonrpc": "2.0", "id": 2, "method": method}))
        await observer(outgoing({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "x"}}))
    assert announced == []


async def test_an_agent_request_is_not_mistaken_for_the_response() -> None:
    """The collision this module exists to get right.

    The agent originates requests, and the SDK numbers them from its *own* counter — so
    an outgoing `session/request_permission` can carry the same id as the client's
    still-open `session/new`. Both are outgoing and both have that id. Only one is a
    response, and the difference is that a response has no `method`.
    """
    announced, announce = recorder()
    observer = command_announcer(announce)

    await observer(incoming({"jsonrpc": "2.0", "id": 2, "method": "session/new"}))
    await observer(
        outgoing(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/request_permission",
                "params": {"sessionId": "some-other-session"},
            }
        )
    )
    assert announced == []

    # Still pending, and still announced when the real response arrives.
    await observer(outgoing({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "mine"}}))
    assert announced == ["mine"]


async def test_a_refused_session_announces_nothing_and_leaves_nothing_pending() -> None:
    announced, announce = recorder()
    observer = command_announcer(announce)

    await observer(incoming({"jsonrpc": "2.0", "id": 3, "method": "session/new"}))
    await observer(
        outgoing({"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "bad cwd"}})
    )
    assert announced == []

    # The id was dropped, so a later message reusing it cannot resurrect the entry.
    await observer(outgoing({"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "late"}}))
    assert announced == []


async def test_an_unrelated_response_is_ignored() -> None:
    """A response to a client-side request the agent made, arriving as an outgoing id we
    never recorded — most of the traffic on a busy connection."""
    announced, announce = recorder()
    observer = command_announcer(announce)

    await observer(outgoing({"jsonrpc": "2.0", "id": 99, "result": {"sessionId": "nope"}}))
    await observer(outgoing({"jsonrpc": "2.0", "method": "session/update", "params": {}}))
    assert announced == []


async def test_a_failing_announcement_is_logged_and_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It runs after the response is written, so there is no request left to fail.

    Letting it raise would only reach the SDK's observer error path and produce a
    traceback for a socket that closed a moment early.
    """
    async def announce(session_id: str) -> None:
        raise RuntimeError("socket closed")

    observer = command_announcer(announce)
    await observer(incoming({"jsonrpc": "2.0", "id": 4, "method": "session/new"}))
    with caplog.at_level(logging.WARNING, logger="python_acp.announcer"):
        await observer(outgoing({"jsonrpc": "2.0", "id": 4, "result": {"sessionId": "boom"}}))
    assert "boom" in caplog.text


async def test_each_connection_gets_its_own_pending_table() -> None:
    """Ids are unique per connection, not per process: two sockets both numbering from 1
    would collide in a shared table, and one client's `session/new` would be announced
    against the other's response."""
    first_announced, first_announce = recorder()
    second_announced, second_announce = recorder()
    first = command_announcer(first_announce)
    second = command_announcer(second_announce)

    await first(incoming({"jsonrpc": "2.0", "id": 1, "method": "session/new"}))
    await second(outgoing({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "other"}}))
    assert second_announced == []
    assert first_announced == []

    await first(outgoing({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "ours"}}))
    assert first_announced == ["ours"]
