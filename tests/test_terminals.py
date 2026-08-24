"""Tests for the terminal registry: the lifetime, not the plumbing.

The plumbing — a command running and its output becoming a tool argument — is
`test_turn_mcp_router.py`'s. What is here is the part with no visible symptom when it is
wrong: a terminal lives in the **client**, so a handle we forget is a process we can no
longer name and nothing anywhere raises. Every test below is about a path out of that.

`TerminalClient` is imported rather than re-written. It runs real subprocesses, and the
question "did the release land" is only worth asking about something that was really
running.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from acp.exceptions import RequestError
from acp.schema import ClientCapabilities

from python_acp.errors import to_request_error
from python_acp.sessions import SessionRegistry
from python_acp.terminals import DEFAULT_OUTPUT_BYTE_LIMIT, Terminal, TerminalRegistry
from python_acp.turns import ClientGates, TurnContext, UngatedClientCallError

from test_turn_mcp_router import TerminalClient

SLEEP = ["-c", "import time; time.sleep(30)"]
PRINT = ["-c", "print('done')"]


def context_for(
    session_id_holder: SessionRegistry, client: Any, cwd: str, *, terminal: bool = True
) -> TurnContext:
    session = session_id_holder.create(cwd)
    return TurnContext(session, client, ClientCapabilities(terminal=terminal))  # type: ignore[arg-type]


async def started(
    registry: TerminalRegistry, context: TurnContext, args: list[str] | None = None, **kwargs: Any
) -> Terminal:
    return await registry.create(
        context, command=sys.executable, args=args or PRINT, cwd=context.session.cwd, **kwargs
    )


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


async def test_a_created_terminal_is_tracked_for_its_session(tmp_path: Path) -> None:
    registry = TerminalRegistry()
    context = context_for(SessionRegistry(), TerminalClient(), str(tmp_path))

    terminal = await started(registry, context)

    assert registry.live(context.session_id) == (terminal,)
    assert context.session_id in registry
    assert len(registry) == 1

    await terminal.release()


async def test_two_sessions_do_not_see_each_others_terminals(tmp_path: Path) -> None:
    """Keyed by session, like every other per-session lifetime in this codebase."""
    registry = TerminalRegistry()
    client = TerminalClient()
    sessions = SessionRegistry()
    one = context_for(sessions, client, str(tmp_path))
    two = context_for(sessions, client, str(tmp_path))

    first = await started(registry, one)
    second = await started(registry, two)

    assert registry.live(one.session_id) == (first,)
    assert registry.live(two.session_id) == (second,)
    assert registry.live("no-such-session") == ()

    await registry.close_all()


async def test_the_client_is_asked_for_the_terminal_it_created(tmp_path: Path) -> None:
    """The id is the client's, and every later call carries it back unchanged."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))

    terminal = await started(registry, context)
    exit_status = await terminal.wait_for_exit()
    captured = await terminal.output()
    await terminal.release()

    assert terminal.terminal_id == "terminal-1"
    assert exit_status.exit_code == 0
    assert captured.output == "done\n"
    assert captured.truncated is False
    assert client.released == ["terminal-1"]


# ---------------------------------------------------------------------------
# Releasing
# ---------------------------------------------------------------------------


async def test_release_drops_tracking_before_it_asks_the_client(tmp_path: Path) -> None:
    """`mcp_registry`'s ordering, for `mcp_registry`'s reason: a teardown that raises must
    not leave a session addressable with a resource that is already gone."""
    registry = TerminalRegistry()
    seen: list[tuple[Any, ...]] = []

    class Watching(TerminalClient):
        async def release_terminal(self, session_id: str, terminal_id: str, **kw: Any) -> None:
            seen.append(registry.live(session_id))
            return await super().release_terminal(session_id, terminal_id, **kw)

    context = context_for(SessionRegistry(), Watching(), str(tmp_path))
    terminal = await started(registry, context)

    await terminal.release()

    assert seen == [()]


async def test_a_release_that_raises_still_drops_tracking(tmp_path: Path) -> None:
    """And does not propagate: the terminal is the client's, it has just refused to take
    it back, and there is nothing a caller could do with the exception."""
    registry = TerminalRegistry()
    client = TerminalClient(release_error=RequestError(-32603, "unknown terminal"))
    context = context_for(SessionRegistry(), client, str(tmp_path))
    terminal = await started(registry, context)

    await terminal.release()

    assert terminal.released is True
    assert registry.live(context.session_id) == ()
    assert len(registry) == 0


async def test_release_is_idempotent(tmp_path: Path) -> None:
    """Which is what makes calling it from a `finally` safe after `abandon` already did."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    terminal = await started(registry, context)

    await terminal.release()
    await terminal.release()

    assert client.released == ["terminal-1"]


async def test_abandon_kills_before_it_releases(tmp_path: Path) -> None:
    """For a turn that is being torn out: nobody is left to read the output, so leaving
    the command running would burn the client's machine for nothing."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    terminal = await started(registry, context, SLEEP)

    await terminal.abandon()

    assert client.killed == ["terminal-1"]
    assert client.released == ["terminal-1"]
    assert client.processes["terminal-1"].returncode is not None
    assert registry.live(context.session_id) == ()


async def test_a_kill_that_fails_does_not_stop_the_release(tmp_path: Path) -> None:
    """A command that already exited makes the kill fail, and the release is the part
    that actually matters."""
    registry = TerminalRegistry()

    class Stubborn(TerminalClient):
        async def kill_terminal(self, session_id: str, terminal_id: str, **kw: Any) -> None:
            self.killed.append(terminal_id)
            raise RequestError(-32603, "already exited")

    client = Stubborn()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    terminal = await started(registry, context)

    await terminal.abandon()

    assert client.killed == ["terminal-1"]
    assert client.released == ["terminal-1"]


# ---------------------------------------------------------------------------
# Session close — the hook `SessionRegistry` fires
# ---------------------------------------------------------------------------


async def test_close_releases_every_terminal_a_session_holds(tmp_path: Path) -> None:
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    await started(registry, context, SLEEP)
    await started(registry, context, SLEEP)

    await registry.close(context.session_id)

    assert client.released == ["terminal-1", "terminal-2"]
    assert registry.live(context.session_id) == ()
    assert context.session_id not in registry


async def test_close_of_a_session_with_no_terminals_is_a_no_op() -> None:
    """The hook fires for every session, and most never open one."""
    registry = TerminalRegistry()

    await registry.close("never-heard-of-it")

    assert len(registry) == 0


async def test_one_failing_release_does_not_strand_the_rest(tmp_path: Path) -> None:
    registry = TerminalRegistry()

    class Awkward(TerminalClient):
        async def release_terminal(self, session_id: str, terminal_id: str, **kw: Any) -> None:
            # Through `super()` first, so the process this double started is still
            # reaped: what is being simulated is a client that answers `-32603`, not one
            # that walks away from its own subprocess.
            await super().release_terminal(session_id, terminal_id, **kw)
            if terminal_id == "terminal-1":
                raise RequestError(-32603, "no")

    client = Awkward()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    await started(registry, context, SLEEP)
    await started(registry, context, SLEEP)

    await registry.close(context.session_id)

    assert client.released == ["terminal-1", "terminal-2"]
    assert registry.live(context.session_id) == ()


async def test_close_all_releases_every_session(tmp_path: Path) -> None:
    """For process shutdown. **Not** for a disconnect — see `forget_client`."""
    registry = TerminalRegistry()
    client = TerminalClient()
    sessions = SessionRegistry()
    one = context_for(sessions, client, str(tmp_path))
    two = context_for(sessions, client, str(tmp_path))
    await started(registry, one, SLEEP)
    await started(registry, two, SLEEP)

    await registry.close_all()

    assert client.released == ["terminal-1", "terminal-2"]
    assert len(registry) == 0


async def test_closing_a_session_releases_the_terminal_a_turn_left_running(
    tmp_path: Path,
) -> None:
    """The leak path this module exists for, through the seam that really carries it.

    `SessionRegistry.on_close` is what `cli.py` wires to this registry, so a session
    closed while a command is still running releases the terminal rather than leaving a
    process nobody can name.
    """
    registry = TerminalRegistry()
    client = TerminalClient()
    sessions = SessionRegistry(on_close=registry.close)
    session = sessions.create(str(tmp_path))
    context = TurnContext(session, client, ClientCapabilities(terminal=True))  # type: ignore[arg-type]
    terminal = await registry.create(
        context, command=sys.executable, args=SLEEP, cwd=str(tmp_path)
    )

    await sessions.close(session.session_id)

    assert client.released == [terminal.terminal_id]
    assert registry.live(session.session_id) == ()
    assert client.processes[terminal.terminal_id].returncode is not None


# ---------------------------------------------------------------------------
# Disconnect — tracking is dropped, and nothing is released
# ---------------------------------------------------------------------------


async def test_a_disconnect_drops_tracking_and_releases_nothing(tmp_path: Path) -> None:
    """Because it cannot release anything. `terminal/release` is a request and the
    connection that would carry it is the one that just went away. The memory is freed;
    the terminals are the departed client's to reap.
    """
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    terminal = await started(registry, context, SLEEP)

    dropped = registry.forget_client(client)

    assert dropped == 1
    assert registry.live(context.session_id) == ()
    assert len(registry) == 0
    assert client.released == []
    assert client.killed == []
    # Still running, on the other side of a connection that no longer exists.
    assert client.processes[terminal.terminal_id].returncode is None

    client.processes[terminal.terminal_id].kill()


async def test_a_disconnect_leaves_another_clients_terminals_alone(tmp_path: Path) -> None:
    """A registry is process-wide and several connections share it, so forgetting has to
    be by client rather than wholesale."""
    registry = TerminalRegistry()
    sessions = SessionRegistry()
    leaving = TerminalClient()
    staying = TerminalClient()
    gone = context_for(sessions, leaving, str(tmp_path))
    kept = context_for(sessions, staying, str(tmp_path))
    await started(registry, gone, SLEEP)
    survivor = await started(registry, kept, SLEEP)

    assert registry.forget_client(leaving) == 1

    assert registry.live(kept.session_id) == (survivor,)
    assert registry.live(gone.session_id) == ()

    leaving.processes["terminal-1"].kill()
    await registry.close_all()


def test_forgetting_a_client_that_never_connected_is_nothing() -> None:
    """`on_connect` may never have run — a socket that closed before `initialize`."""
    assert TerminalRegistry().forget_client(None) == 0


async def test_a_forgotten_terminal_is_not_released_later(tmp_path: Path) -> None:
    """Dropping tracking marks the handle spent, so a stray `release` on it does not send
    a request down a connection that is gone."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    terminal = await started(registry, context, SLEEP)
    registry.forget_client(client)

    await terminal.release()

    assert client.released == []

    client.processes[terminal.terminal_id].kill()


# ---------------------------------------------------------------------------
# `outputByteLimit`
# ---------------------------------------------------------------------------


async def test_the_default_limit_is_sent_on_every_create(tmp_path: Path) -> None:
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))

    terminal = await started(registry, context)

    assert client.created[0]["output_byte_limit"] == DEFAULT_OUTPUT_BYTE_LIMIT
    assert DEFAULT_OUTPUT_BYTE_LIMIT == 1024 * 1024

    await terminal.release()


async def test_an_unbounded_terminal_is_refused(tmp_path: Path) -> None:
    """The schema allows omitting `outputByteLimit`; this registry does not. Unbounded
    output is the failure mode the field exists to prevent, and it has no error message
    of its own — the client just buffers until something dies."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))

    with pytest.raises(ValueError, match="unbounded terminal output"):
        await registry.create(
            context, command=sys.executable, args=PRINT, output_byte_limit=None  # type: ignore[arg-type]
        )

    assert client.created == []


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def test_creating_a_terminal_ungated_is_our_conformance_bug(tmp_path: Path) -> None:
    """`require`, not `allows`: whoever parsed the request refuses a client with no
    `terminal` long before this line, so arriving here with the gate shut is ours."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path), terminal=False)

    with pytest.raises(UngatedClientCallError) as raised:
        await started(registry, context)

    assert to_request_error(raised.value).code == -32603
    assert client.created == []


@pytest.mark.parametrize(
    "method", ["wait_for_exit", "output", "kill", "release"]
)
async def test_every_terminal_method_is_gated(method: str) -> None:
    """One boolean covers all five, so all five check it — including `release`, which is
    the only one that would otherwise be reachable from a cleanup path.

    Built by hand, because there is no way to reach this through `create`: the gate was
    open when the terminal was made and `ClientGates` is frozen for the connection.
    """
    registry = TerminalRegistry()
    client = TerminalClient()
    terminal = Terminal(registry, client, ClientGates(), "session", "terminal-1")  # type: ignore[arg-type]

    with pytest.raises(UngatedClientCallError):
        await getattr(terminal, method)()

    assert client.released == []
    assert client.killed == []


async def test_a_cancellation_racing_a_release_still_completes(tmp_path: Path) -> None:
    """`Terminal.release` on an already-released handle awaits nothing, which is what
    makes it safe to call from a `finally` inside a task that is being cancelled: there is
    no suspension point for the cancellation to land on."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))
    terminal = await started(registry, context, SLEEP)
    await terminal.abandon()

    async def cleanup() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            await terminal.release()

    task = asyncio.create_task(cleanup())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.released == ["terminal-1"]


# ---------------------------------------------------------------------------
# Cancelled mid-create (pyacp-9hd)
# ---------------------------------------------------------------------------


class SlowCreateClient(TerminalClient):
    """A client whose `terminal/create` starts the process, then stalls before replying.

    The real race, reproduced deliberately: the terminal **exists** on the client and its
    id is on its way back when the turn is cancelled. A client that failed to create
    would not exercise anything.
    """

    def __init__(self) -> None:
        super().__init__()
        self.creating = asyncio.Event()
        self.reply = asyncio.Event()

    async def create_terminal(self, **kwargs: Any):
        response = await super().create_terminal(**kwargs)
        self.creating.set()
        try:
            await self.reply.wait()
        except asyncio.CancelledError:
            # The process is already running; if the reply is never let through, this
            # double is the only thing that still knows about it. A real client would
            # own it either way — a *test* double that leaked it would leave a 30-second
            # sleeper behind and, on 3.11, a `PytestUnraisableExceptionWarning` from a
            # transport finalized after its loop (`pyacp-6k5`).
            await self.reap(response.terminal_id)
            raise
        return response

    async def reap(self, terminal_id: str) -> None:
        """Kill and wait a process this double started, without pretending to release."""
        process = self._live.pop(terminal_id, None)
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        reader = self._readers.pop(terminal_id, None)
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def test_a_terminal_created_after_the_cancel_is_still_given_back(
    tmp_path: Path,
) -> None:
    """The window `pyacp-9hd` was filed for, and it closes.

    Without the shield the reply lands on a future nobody awaits: the process runs on
    the client's machine under an id this process never learned, so it can be neither
    released nor even named. With it, the id still arrives and the terminal is killed and
    released like any other cancelled command's.
    """
    registry = TerminalRegistry()
    client = SlowCreateClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))

    turn = asyncio.create_task(started(registry, context, SLEEP))
    await asyncio.wait_for(client.creating.wait(), timeout=10)
    turn.cancel()
    # The reply lands *after* the cancellation, which is the whole point.
    client.reply.set()

    with pytest.raises(asyncio.CancelledError):
        await turn

    assert len(client.created) == 1, "the client really did start a terminal"
    assert client.released == ["terminal-1"], "and it was given back"
    assert client.killed == ["terminal-1"], "killed too — nobody was left to read it"
    assert registry.live(context.session_id) == ()


async def test_a_create_that_never_answers_does_not_hang_the_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The residual window, bounded rather than closed.

    A reply that never arrives is a connection dying underneath us, and the id existed
    only in that reply — nothing on this side can recover it. What this asserts is the
    part that *is* ours: the cancellation completes instead of hanging on a wait that
    will never finish.
    """
    monkeypatch.setattr(TerminalRegistry, "_UNCLAIMED_CREATE_TIMEOUT", 0.05)
    registry = TerminalRegistry()
    client = SlowCreateClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))

    turn = asyncio.create_task(started(registry, context, SLEEP))
    await asyncio.wait_for(client.creating.wait(), timeout=10)
    turn.cancel()
    # `client.reply` is never set: the answer is gone for good.

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=10)

    assert registry.live(context.session_id) == ()


async def test_an_ordinary_create_is_unaffected_by_the_shield(tmp_path: Path) -> None:
    """The shield must not change the uncancelled path, which is every other turn."""
    registry = TerminalRegistry()
    client = TerminalClient()
    context = context_for(SessionRegistry(), client, str(tmp_path))

    terminal = await started(registry, context)

    assert registry.live(context.session_id) == (terminal,)
    assert client.killed == []
    await terminal.release()
