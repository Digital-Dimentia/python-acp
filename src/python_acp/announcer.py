"""Notifications that have to *follow* a response, and the one hook that can send them.

`session/new` and `session/fork` mint a session id the client learns from the response.
Anything the agent wants to say *about* that session — its command palette, above all —
is therefore unsendable from inside the handler: `acp.Connection._run_request` awaits the
handler and only then writes the reply, so a `session/update` emitted in there goes out
**first**, naming a session the client has never heard of. A correct client drops it. A
task scheduled with `create_task` from inside the handler is no better: the SDK has no
ordered outgoing queue — every sender awaits the transport directly — so it races the
reply rather than following it.

The SDK does have one hook on the far side of that write. `_run_request` is::

    payload = await self._execute_request(message)
    await self._transport.send(payload)
    self._notify_observers(StreamDirection.OUTGOING, payload)

so a **stream observer** runs strictly after the response bytes are on the wire, and a
notification it sends names an id the client already holds. Observers are public API and
reach a `run_agent` caller through `**connection_kwargs`.

This module is separate from [agent.py](agent.md) on purpose: that module's contract is
that nothing in it parses a request id or knows a transport exists, and matching raw
JSON-RPC frames is precisely both. It takes the `announce` callable rather than the
agent, so it depends on nothing here and is testable with a list.

## Matching a response to the request that caused it

A JSON-RPC response carries no method — only the id it is answering. So the observer
watches both directions: it records the id of an *incoming* `session/new` or
`session/fork`, and fires when an *outgoing* message answers that id.

**Outgoing does not mean "response".** The agent originates requests of its own —
`session/request_permission`, `fs/read_text_file` — and those carry ids from the SDK's
own counter, which is independent of the client's. A client's `session/new` with id `2`
and an agent's `session/request_permission` with id `2` are routine, and mistaking the
second for the first would announce against whatever `result` happened to be there. A
response is the message with no `method`, and that is the test used.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from acp.connection import StreamDirection, StreamEvent

logger = logging.getLogger(__name__)

#: The two methods that mint an id, and so the two whose responses are worth watching.
#: `session/load` and `session/resume` are announced inline by `agent.announce_commands`,
#: because the client named the session itself and no ordering problem exists there.
MINTING_METHODS = frozenset({"session/new", "session/fork"})


def command_announcer(
    announce: Callable[[str], Awaitable[None]],
) -> Callable[[StreamEvent], Awaitable[None]]:
    """A stream observer that announces a session's commands once its id has been sent.

    Pass the result to `run_agent(..., observers=[...])`. `announce` is
    `PythonAcpAgent.announce_commands`; nothing here needs the agent itself.

    The observer is per-connection, and so is the pending-id table it closes over: ids
    are only unique within one connection, and two sockets both numbering from 1 would
    collide in a shared one.
    """
    pending: dict[Any, str] = {}

    async def observer(event: StreamEvent) -> None:
        message = event.message
        if event.direction is StreamDirection.INCOMING:
            request_id = message.get("id")
            if request_id is not None and message.get("method") in MINTING_METHODS:
                pending[request_id] = message["method"]
            return
        # Outgoing. A response, and only a response — see the module docstring on why an
        # agent-originated request can carry a colliding id.
        if message.get("method") is not None:
            return
        method = pending.pop(message.get("id"), None)
        if method is None:
            return
        # Popped either way, so a refused `session/new` leaves nothing behind. An error
        # payload has no `result`, and there is nothing to announce about a session that
        # was never created.
        session_id = (message.get("result") or {}).get("sessionId")
        if not session_id:
            return
        try:
            await announce(session_id)
        except Exception as exc:  # noqa: BLE001 - a palette is never worth a raised error
            # This runs after the response was written, so there is no request left to
            # fail; the alternative to swallowing is a traceback from the SDK's observer
            # error path. A socket that closed in between is the ordinary case.
            logger.warning(
                "Could not announce commands for %s after %s: %s", session_id, method, exc
            )

    return observer
