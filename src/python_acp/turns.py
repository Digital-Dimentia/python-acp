"""The seam a prompt turn runs behind.

`session/prompt` is where an agent does its work, and decision D3 says *what* that work
is must be swappable — the shipped default is a deterministic MCP tool-router with no LLM
in the loop, and an LLM-backed executor has to be droppable in later without reopening
the interface.

> **This is the minimum `pyacp-3rw.2` needed to wire `session/prompt`, not the finished
> interface.** `pyacp-hnk.1` owns that and depends on this bead. What is here — an async
> `execute(context, prompt)` returning a `stopReason`, and a context carrying the session,
> the client handle, and the update channel — is deliberately the smallest shape that does
> not foreclose hnk.1's requirements: the call is `async` and single-method, so a turn may
> take as many steps and client round-trips as it likes. What hnk.1 must still add is
> named in `turns.md`.

## Why the context is an object

An executor needs three things and they arrive from three places: the `Session`
(`sessions.py`), the `Client` facade (`agent.py`, from `on_connect`), and a way to push
`session/update` (the two combined). Passing them separately would make every executor
signature grow when a later phase adds a fourth; passing a context means `TurnContext`
grows and executors do not.

## Emission is not optional

`session/update` is the only way a client sees anything before the turn ends, and
`ClientCapabilities` has **no gate** for it — every ACP client must accept it. So `emit`
is unconditional, and `pyacp-hnk.4`'s per-variant gating (`clientCapabilities.plan`
governs the `agent_plan_*` variants) belongs on the *variant*, never on the call.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from acp.interfaces import Client
from acp.schema import StopReason

from python_acp.sessions import Session

logger = logging.getLogger(__name__)


class TurnContext:
    """Everything a turn is allowed to reach, and nothing else.

    Deliberately not a dataclass of public attributes: `emit` is the point, and a context
    that merely exposed `client` would invite an executor to call `session_update`
    with the wrong session id.
    """

    def __init__(self, session: Session, client: Client) -> None:
        self.session = session
        self.client = client

    @property
    def session_id(self) -> str:
        return self.session.session_id

    async def emit(self, update: Any) -> None:
        """Push one `session/update` notification for this turn's session.

        The session id comes from the context, never from the caller, so an executor
        cannot address someone else's session by accident.
        """
        logger.debug("session/update for %s: %s", self.session_id, type(update).__name__)
        # Recorded before the send, not after: a notification that failed on the wire
        # still happened as far as this session is concerned, and `session/load` replaying
        # it is the client's chance to see what it missed.
        self.session.record(update)
        await self.client.session_update(session_id=self.session_id, update=update)


class TurnExecutor(Protocol):
    """Serves one `session/prompt`.

    Returns the `stopReason` the response carries. Raising is allowed and becomes a
    JSON-RPC error through `errors.py`; being cancelled is **not** an error — `agent.py`
    turns a cancelled turn into `stopReason: "cancelled"`, which is why an executor should
    let `asyncio.CancelledError` propagate rather than catching it to return early.
    """

    async def execute(self, context: TurnContext, prompt: list[Any]) -> StopReason: ...


class IdleTurnExecutor:
    """The default: complete the turn immediately, having done nothing.

    Not a placeholder that raises, and not one that invents content. A conforming turn
    that ends straight away is the honest answer while there is no executor: the client's
    `session/prompt` gets a well-formed `PromptResponse`, the create-prompt-cancel cycle
    works end to end, and nothing pretends to have run a tool.

    `pyacp-hnk.2` replaces this with the deterministic MCP tool-router. The warning fires
    once per turn on purpose — a silent no-op turn is exactly the failure someone would
    otherwise spend an afternoon on.
    """

    async def execute(self, context: TurnContext, prompt: list[Any]) -> StopReason:
        logger.warning(
            "session/prompt for %s completed without running anything: no turn executor "
            "is configured (pyacp-hnk.2 ships the default)",
            context.session_id,
        )
        return "end_turn"
