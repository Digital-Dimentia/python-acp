"""Terminals this agent asked a client to run, and the promise to release them.

`terminal/create`, `/output`, `/wait_for_exit`, `/kill`, and `/release` are
`acp.interfaces.Client` methods: an ACP agent **calls** them, it never serves them. So a
"terminal" here is not a process this runtime owns — it is a handle to a process running
on the *client's* machine, and the only thing this module can do about one is remember it
and ask for it back.

That is the whole reason the module exists. `create_terminal` returns an id and nothing
else; a client holds the process, the output buffer, and the file descriptors until
`terminal/release` arrives. An agent that forgets an id has leaked a process it can no
longer name, and no error is raised anywhere when that happens.

## The shape is `mcp_registry.py`'s, deliberately

Per-session tracking, `close(session_id)` wired to `SessionRegistry.on_close`,
`close_all()` for process shutdown, and **remove the entry before tearing it down**, so a
release that raises cannot leave a session addressable with a terminal that is already
gone. That module solved this lifetime once; a second shape for the same problem would
mean two places to get it wrong.

One thing is genuinely different, and it is the reason `Terminal` carries its own client:
an MCP backend is a subprocess *we* spawned, but a terminal belongs to whichever
connection created it. `session/close` may arrive on a different connection than the turn
that opened the terminal, so the release has to go back to the client that has it.

## Disconnect releases nothing, and says so

The acceptance criterion this module was written against asked for "every created
terminal released on ... disconnect". **That cannot be done, and pretending otherwise
would be the bug.** The terminal lives in the client. Once the client's connection is
gone there is nobody to send `terminal/release` to — the request would have no transport,
and the process it names is on a machine we are no longer talking to.

Sessions deliberately survive a disconnect (`sessions.py`: a disconnecting client must
not silently delete sessions another connection may resume), so the session's entry stays
and only the terminals belonging to the departed client go.

So `forget_client` **drops our tracking and releases nothing**. It frees the memory a
dead handle would otherwise hold forever; the terminals themselves are the departed
client's to reap, which is the honest description of what happens and is why it is
written here rather than left implied. A client that reconnects gets a fresh handle set;
it cannot ask us for the old ids and we cannot give them back.

## `output_byte_limit` is always set

`CreateTerminalRequest.outputByteLimit` is optional in the schema and **not optional
here**: `create` defaults it and refuses `None`. A command that runs away produces output
the client buffers until something dies, which is a failure mode with no error message
attached to it.

`DEFAULT_OUTPUT_BYTE_LIMIT` is 1 MiB, and the number comes from where the bytes end up
rather than from taste. Captured output becomes an MCP tool *argument*, so it is
JSON-escaped into a request that has to fit `MCPStdioClient`'s 8 MiB stream limit —
escaping can multiply a byte several times over, and the rest of the request has to fit
too. 1 MiB leaves that headroom while being far more than any sane command's output. The
client truncates from the **beginning** (the schema says so), which keeps the tail: where
a failing command puts its error and a finishing one puts its result.

## Gating

`clientCapabilities.terminal` is **one boolean for all five methods** — no per-method
granularity exists in the schema. Every call here starts with
`context.require(Gate.TERMINAL)`, which is an assertion of *our* invariant rather than
the ordinary capability check: an executor decides what to do about a client with no
terminals at parse time with `allows`, and reaching a call site with the gate shut means
that check was missing. See `turns.md` and `turn_mcp_router.md`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from acp.interfaces import Client
from acp.schema import (
    EnvVariable,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)

from python_acp.turns import ClientGates, Gate, TurnContext

logger = logging.getLogger(__name__)

#: The `outputByteLimit` every `terminal/create` carries unless a caller names another.
#: See the module docstring for where 1 MiB comes from — it is derived from the MCP
#: stream limit the captured output has to fit through, not chosen for roundness.
DEFAULT_OUTPUT_BYTE_LIMIT = 1024 * 1024


class Terminal:
    """One live terminal on a client, and the four things we may ask of it.

    Not constructed directly: `TerminalRegistry.create` makes one and is the only thing
    that knows the id exists. It carries its own `Client` because the connection that
    created it is the only one that can release it — see the module docstring.
    """

    def __init__(
        self,
        registry: TerminalRegistry,
        client: Client,
        gates: ClientGates,
        session_id: str,
        terminal_id: str,
    ) -> None:
        self._registry = registry
        self.client = client
        self._gates = gates
        self.session_id = session_id
        self.terminal_id = terminal_id
        self._released = False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<Terminal {self.terminal_id!r} of session {self.session_id!r}>"

    @property
    def released(self) -> bool:
        return self._released

    async def wait_for_exit(self) -> WaitForTerminalExitResponse:
        """Block until the command finishes. `terminal/wait_for_exit`."""
        self._gates.require(Gate.TERMINAL)
        return await self.client.wait_for_terminal_exit(
            session_id=self.session_id, terminal_id=self.terminal_id
        )

    async def output(self) -> TerminalOutputResponse:
        """Everything the client captured so far, and whether it had to truncate."""
        self._gates.require(Gate.TERMINAL)
        return await self.client.terminal_output(
            session_id=self.session_id, terminal_id=self.terminal_id
        )

    async def kill(self) -> None:
        """Stop the command without releasing the terminal.

        Separate from `release` because the protocol separates them: killing leaves the
        output readable, which is what makes a cancelled command's partial output
        available at all. `abandon` is the pair for the case where nobody will read it.
        """
        self._gates.require(Gate.TERMINAL)
        await self.client.kill_terminal(
            session_id=self.session_id, terminal_id=self.terminal_id
        )

    async def release(self) -> None:
        """Give the terminal back. Idempotent, and never raises.

        **Tracking is dropped first**, so a client that errors on `terminal/release`
        cannot leave this handle addressable while its terminal is gone — the same
        ordering, and the same reason, as `McpBackendRegistry.close`.

        Never raising is a decision, not laziness. A release failure is not information
        the caller can act on: the terminal is the client's and it has just refused to
        take it back, so propagating would replace a tool call's real result with a
        cleanup error. It is logged, which is the whole of what can honestly be done.

        Returning without awaiting anything when already released matters more than it
        looks: it is what makes calling this from a `finally` inside a cancelled task
        safe, because there is no suspension point for the cancellation to land on.

        The gate check is the one thing here that *does* raise, and it is checked before
        any state moves. It cannot fail for a terminal that exists — `create` required the
        same gate and `ClientGates` is frozen for the life of a connection — so a failure
        would mean this handle was built by hand, which is our bug and not a cleanup one.
        """
        if self._released:
            return
        self._gates.require(Gate.TERMINAL)
        self._released = True
        self._registry._drop(self)
        try:
            await self.client.release_terminal(
                session_id=self.session_id, terminal_id=self.terminal_id
            )
        except Exception:  # noqa: BLE001 — logged; see the docstring
            logger.warning(
                "Releasing terminal %s of session %s failed; it is the client's now",
                self.terminal_id,
                self.session_id,
                exc_info=True,
            )

    async def abandon(self) -> None:
        """Kill the command and release the terminal. Never raises.

        For a turn that is being torn out — a cancelled turn's command has nobody left to
        read its output, so leaving it running would burn the client's machine on a
        result no one will collect. The kill is best-effort: a command that already
        exited makes it fail, and that must not stop the release.
        """
        if self._released:
            return
        try:
            await self.kill()
        except Exception:  # noqa: BLE001 — the release below is the part that matters
            logger.info(
                "Killing terminal %s of session %s failed; releasing it anyway",
                self.terminal_id,
                self.session_id,
                exc_info=True,
            )
        await self.release()


class TerminalRegistry:
    """Every terminal this agent created, keyed by session and then by terminal id.

    Process-wide, like `McpBackendRegistry` and for a related reason: a session outlives
    the connection that created it, so the thing that tracks per-session resources cannot
    be per-connection either. Unlike backends, each entry remembers *which* client it
    belongs to, because that is who has to be asked for it back.
    """

    def __init__(self) -> None:
        self._live: dict[str, dict[str, Terminal]] = {}

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._live

    def __len__(self) -> int:
        """How many sessions hold at least one live terminal."""
        return len(self._live)

    def live(self, session_id: str) -> tuple[Terminal, ...]:
        """This session's live terminals, oldest first.

        Empty for a session that has none, and for one that does not exist — a caller
        holding a session id already went through `SessionRegistry.get`, and "no
        terminals" is the true answer either way.
        """
        return tuple(self._live.get(session_id, {}).values())

    async def create(
        self,
        context: TurnContext,
        *,
        command: str,
        args: Sequence[str] | None = None,
        env: Sequence[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int = DEFAULT_OUTPUT_BYTE_LIMIT,
    ) -> Terminal:
        """Start a command on the client and track the terminal it hands back.

        The gate is `require`, not `allows`: a client with no `terminal` capability is an
        ordinary thing to be and belongs to whoever parsed the request, which refuses the
        turn long before this line. Arriving here with the gate shut is our conformance
        bug, and `-32603` is the honest answer to that.

        `output_byte_limit` is refused as `None` rather than passed through. The schema
        allows omitting it; this module does not, because unbounded output is the failure
        mode the field exists to prevent.
        """
        if output_byte_limit is None:
            raise ValueError(
                "output_byte_limit must be a byte count: unbounded terminal output is "
                "the failure mode it exists to prevent"
            )
        context.require(Gate.TERMINAL)
        session_id = context.session_id
        response = await context.client.create_terminal(
            session_id=session_id,
            command=command,
            args=list(args) if args else None,
            env=list(env) if env else None,
            cwd=cwd,
            output_byte_limit=output_byte_limit,
        )
        terminal = Terminal(
            self, context.client, context.gates, session_id, response.terminal_id
        )
        self._live.setdefault(session_id, {})[terminal.terminal_id] = terminal
        logger.debug(
            "Created terminal %s for session %s (%s)", terminal.terminal_id, session_id, command
        )
        return terminal

    async def close(self, session_id: str) -> None:
        """Release every terminal a session holds. The `SessionRegistry.on_close` hook.

        Entries are removed **before** anything is released — `Terminal.release` drops
        itself first — so a client that errors on one release cannot strand the rest, and
        cannot leave a closed session holding handles.

        A session with no terminals is a no-op: the hook fires for every session, and
        most sessions never open one.
        """
        held = self._live.pop(session_id, None)
        if not held:
            return
        logger.debug("Releasing %d terminal(s) for session %s", len(held), session_id)
        for terminal in tuple(held.values()):
            await terminal.release()

    async def close_all(self) -> None:
        """Release everything. **For process shutdown, not for a disconnect.**

        The distinction is the whole point and is the same one `McpBackendRegistry` and
        `SessionRegistry` draw. A disconnect leaves the sessions alive for another
        connection to resume, and its terminals cannot be released at all — see
        `forget_client`. Only a process that is going away releases on everyone's behalf.
        """
        for session_id in tuple(self._live):
            await self.close(session_id)

    def forget_client(self, client: Client | None) -> int:
        """Drop tracking for one departed client's terminals. Releases **nothing**.

        The disconnect path, and the reason it cannot be `close`: `terminal/release` is a
        request, and the connection that would carry it is the one that just went away.
        What is left to do is stop holding handles that can never be used again, so that
        a long-lived process does not accumulate one set per connection.

        The terminals themselves are the departed client's to reap. Nothing here can, and
        no agent could.

        Returns how many were dropped, so a caller can log it — a non-zero count is worth
        seeing, because it means a client disconnected mid-command.
        """
        if client is None:
            return 0
        dropped = 0
        for session_id, held in tuple(self._live.items()):
            for terminal_id, terminal in tuple(held.items()):
                if terminal.client is client:
                    terminal._released = True
                    del held[terminal_id]
                    dropped += 1
            if not held:
                del self._live[session_id]
        if dropped:
            logger.info(
                "Dropped %d terminal handle(s) belonging to a disconnected client; the "
                "terminals themselves are that client's to reap",
                dropped,
            )
        return dropped

    def _drop(self, terminal: Terminal) -> None:
        """Stop tracking one terminal. Called by `Terminal.release` before it releases."""
        held = self._live.get(terminal.session_id)
        if held is None:
            return
        held.pop(terminal.terminal_id, None)
        if not held:
            del self._live[terminal.session_id]


__all__ = ["DEFAULT_OUTPUT_BYTE_LIMIT", "Terminal", "TerminalRegistry"]
