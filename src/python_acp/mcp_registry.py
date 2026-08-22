"""Per-session MCP backends: which servers a session talks to, and for how long.

Until this module, `python-acp` bound **one** MCP server at process start from
`--mcp-command` and shared it with every client. ACP v1 inverts that: the client names
its servers in `session/new`, so the agent is an MCP *client* to N servers whose
lifetime is a session's, not the process's.

This module owns spawning, addressing, and tearing those down. It does **not** own the
stdio wire — that is `mcp_stdio.py`, and this module only ever holds `MCPStdioClient`
instances. It does not own session state either: `sessions.py` deliberately never
imports MCP, and reaches here through the `on_close` hook it is given (decision B6a in
`docs/module-boundaries.md`).

## stdio only, and that is a promise not a limitation

`capabilities.AGENT_CAPABILITY_MANIFEST` advertises `mcpCapabilities.http`, `.sse`, and
`.acp` as `false`, and `McpServerStdio` needs no capability at all. `agent.py` refuses
the three unadvertised transports before they reach here, so this module handles exactly
one shape. Adding another means flipping a capability literal, which the test suite will
not let happen without a feature test behind it.

## Every session gets its own subprocess

Two sessions naming the same server do **not** share one. It costs a process; the
alternative costs correctness, because a shared backend makes `session/close` on one
session tear down another's tools. That is the same reasoning `sessions.py` records for
`fork_session`, and the two must agree or a forked session's `close` becomes a
landmine.

A refcounted share is a valid later optimisation *provided* closing one session cannot
disturb another.

## Opening is all-or-nothing

`session/new` either gets a session with every server it asked for, or an error. A
partially-opened session would hand back an id whose tools silently do not exist — the
same failure the capability manifest exists to prevent, arriving by a different route —
and would leak the subprocesses that did start. So `open` tears down whatever it
managed to start before re-raising.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

from acp.schema import McpServerStdio

from python_acp.mcp_stdio import MCPProtocolError, MCPStdioClient

logger = logging.getLogger(__name__)

#: How a spec becomes a running, handshaken client. Injectable so tests can drive the
#: registry's lifetime and failure handling without spawning anything.
Connector = Callable[[McpServerStdio], Awaitable[MCPStdioClient]]


class UnknownBackendError(ValueError):
    """A server name that the session did not open.

    A `ValueError`, so `errors.to_request_error` maps it to `-32602` with the name in
    `data` — the same treatment `UnknownSessionError` gets, and for the same reason: the
    client named something that does not exist, which is a parameter problem.
    """

    def __init__(self, session_id: str, name: str) -> None:
        super().__init__(f"Session {session_id!r} has no MCP server named {name!r}")
        self.session_id = session_id
        self.name = name


async def connect_stdio(server: McpServerStdio) -> MCPStdioClient:
    """Spawn one MCP server and complete its handshake.

    The handshake happens **here**, not lazily on first use. A server that cannot
    negotiate is a `session/new` failure the client can act on; discovering it mid-turn
    would surface as a broken prompt with no explanation.
    """
    client = MCPStdioClient(
        [server.command, *server.args],
        env={variable.name: variable.value for variable in server.env},
    )
    await client.start()
    try:
        await client.initialize()
    except Exception:
        await client.stop()
        raise
    return client


class McpBackendRegistry:
    """The MCP servers each session opened, keyed by session id and then by name."""

    def __init__(self, *, connect: Connector = connect_stdio) -> None:
        self._backends: dict[str, dict[str, MCPStdioClient]] = {}
        # The specs each session opened, kept so `fork` can respawn them. A fork gets its
        # own subprocesses (see the module docstring), which means it needs the recipe,
        # not the running clients.
        self._specs: dict[str, tuple[McpServerStdio, ...]] = {}
        self._connect = connect

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._backends

    def __len__(self) -> int:
        return len(self._backends)

    async def open(
        self, session_id: str, servers: Sequence[McpServerStdio]
    ) -> Mapping[str, MCPStdioClient]:
        """Start every server for a session, or none of them.

        Names must be unique within one session: `pyacp-hnk.2` routes a tool call by
        server name, and two servers answering to one name would make which of them ran
        a matter of dict ordering.
        """
        if session_id in self._backends or session_id in self._specs:
            raise RuntimeError(f"Session {session_id!r} already has MCP backends open")

        names = [server.name for server in servers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate MCP server names in session/new: {duplicates}")

        opened: dict[str, MCPStdioClient] = {}
        try:
            for server in servers:
                logger.debug("Opening MCP server %r for session %s", server.name, session_id)
                opened[server.name] = await self._connect(server)
        except Exception:
            # All-or-nothing: a half-open session would hand back an id whose tools
            # silently do not exist, and leak the subprocesses that did start.
            await self._stop_all(opened.values())
            raise

        self._backends[session_id] = opened
        self._specs[session_id] = tuple(servers)
        return dict(opened)

    async def fork(
        self,
        parent_id: str,
        child_id: str,
        servers: Sequence[McpServerStdio] | None = None,
    ) -> Mapping[str, MCPStdioClient]:
        """Open a fork's servers: its own subprocesses, from the parent's specs.

        `servers` overrides them, because `session/fork` lets the client supply its own
        `mcpServers` — a fork of a session into a different working tree may well want
        different tools. `None` means "the same recipe as the parent", which is the case
        the parent's specs are kept for.

        Sharing the parent's *clients* instead would be cheaper and wrong: `session/close`
        on the fork would tear down the parent's tools. `sessions.py` records the same
        decision for the session state itself, and the two have to agree.
        """
        specs = self._specs.get(parent_id, ()) if servers is None else tuple(servers)
        return await self.open(child_id, specs)

    def backends(self, session_id: str) -> Mapping[str, MCPStdioClient]:
        """Every server this session opened. Empty for a session that opened none.

        Deliberately **not** an error for an unknown session id: a caller holding a
        session id already went through `SessionRegistry.get`, and answering "no
        backends" is the truth for a session that named no servers either way.
        """
        return dict(self._backends.get(session_id, {}))

    def get(self, session_id: str, name: str) -> MCPStdioClient:
        try:
            return self._backends[session_id][name]
        except KeyError:
            raise UnknownBackendError(session_id, name) from None

    async def close(self, session_id: str) -> None:
        """Tear down a session's servers. The `SessionRegistry.on_close` hook.

        Removes the entry **before** stopping anything, so a stop that raises cannot
        leave a session addressable with dead backends — the same ordering, and the same
        reason, as `SessionRegistry.close`.

        Unknown session ids are a no-op: `close_session` on a session that opened no
        servers is ordinary, and the hook fires for every session either way.
        """
        self._specs.pop(session_id, None)
        opened = self._backends.pop(session_id, None)
        if not opened:
            return
        logger.debug("Closing %d MCP server(s) for session %s", len(opened), session_id)
        await self._stop_all(opened.values())

    async def close_all(self) -> None:
        """Tear everything down. For process shutdown, not for a disconnect."""
        for session_id in tuple(self._backends):
            await self.close(session_id)

    @staticmethod
    async def _stop_all(clients: Iterable[MCPStdioClient]) -> None:
        """Stop every client, letting none of them prevent the rest from stopping.

        A teardown loop that propagated the first failure would strand every subprocess
        after it — and the leak, not the failure, is what actually costs something.
        """
        for client in clients:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001 — logged; one failure must not strand the rest
                logger.warning("Failed to stop an MCP server cleanly", exc_info=True)


__all__ = [
    "Connector",
    "McpBackendRegistry",
    "MCPProtocolError",
    "UnknownBackendError",
    "connect_stdio",
]
