"""Forwarding an MCP server's `elicitation/create` to the ACP client. The seam between
two ways of asking a human a question.

Both protocols have "stop and ask the user something structured", and this process sits
between them: an MCP server we launched asks *us*, and the only human anywhere is on the
far side of the ACP connection. So the answer is not to invent one — it is to pass the
question along and hand the reply back.

```
MCP server ──elicitation/create──▶ python-acp ──elicitation/create──▶ ACP client
           ◀──── {action, content} ──────────  ◀──── accept|decline|cancel|…
```

That forwarding is the whole reason `mcp_stdio.py` proposes `2025-06-18` and the whole
reason `MCPClientCapabilities.elicitation` exists. Declaring the capability and answering
it are one change (`pyacp-pb7` wrote the first half and left the second to this module),
so `mcp_registry.connect_stdio` declares `elicitation` **only** when it is handed a
forwarder — and `agent.py` only builds one when the connected client advertised
form-mode elicitation.

## MCP asks a form, so ACP is asked a form

MCP's `elicitation/create` is `{message, requestedSchema}` — one shape, always a form.
ACP's is a union of four: form or URL, scoped to a session or to a request. Only one of
the four can carry an MCP question, and it is `ElicitationFormSessionMode`: form, because
that is what MCP sends, and session-scoped, because the backend belongs to a session and
a session id is the thing we have.

**The URL modes have no source here and are not a gap.** A URL elicitation sends the user
somewhere to complete a flow out of band; nothing in this runtime has such a flow, and an
MCP `requestedSchema` cannot become one. That is also what settles
`Client.complete_elicitation`: `elicitationId` exists **only** on the two URL variants, so
a form elicitation has no id to complete and the notification has nothing here to
announce. It is declined for a structural reason, not deferred — see the client surface
table in `docs/acp-compliance-matrix.md`.

## Three answers that are not the client's

The reply is the client's whenever there is a client to ask. When there is not, something
still has to be said, because an MCP server that sent a request is blocked until it is
answered.

| Situation | Reply | Why not an error |
|---|---|---|
| Nobody is connected | `cancel` | A session outlives the connection that made it (`sessions.py`), so a backend may ask after its client has gone. "The prompt was dismissed without a choice" is precisely what happened |
| The connected client has no `elicitation.form` | `cancel` | Only reachable when a *different* client picked the session up — see the window below. A client lacking a capability is not an error, here or anywhere |
| The client answered with an action ACP added later | `cancel` | MCP has exactly three actions. An extension action has no MCP spelling, and "no explicit choice" is the truthful reduction of one we cannot read |

`cancel` rather than `decline` in all three: `decline` says a human refused, which would
be a fiction. None of them is an error response, because none of them is a failure — the
server asked a legitimate question and got a legitimate "no answer".

**A client that raises is different**, and is left alone: the exception travels back to
`mcp_stdio._handle_server_request`, which answers the server `-32603`. Something really
did break, and flattening that into `cancel` would tell the server a human dismissed a
prompt nobody ever saw.

## The window this does not close

The MCP capability block is a promise made once, when the backend is spawned; the ACP
client's capabilities are a fact about one connection. A session created by a form-capable
client, disconnected, and then resumed by a client without elicitation leaves a backend
holding a promise the current connection cannot keep. It is answered with `cancel` and a
warning rather than pretended away, and it cannot be closed from this side: MCP has no way
to withdraw a declared capability, short of restarting the subprocess.

The same shape as the terminal that cannot be released after a disconnect
(`terminals.md`), and recorded for the same reason.

## What is *not* translated

`requestedSchema` goes through the SDK's `ElicitationSchema` rather than being passed as
an opaque dict, because `Client.create_elicitation` takes a model and there is no raw
path. That parse is the validation: a schema the SDK cannot read raises
`MalformedServerRequest`, and the server gets `-32602` — the server sent bad params,
which is neither "we broke" nor "we never offered this".

`content` on an accepted answer is **not** checked against that schema. The client
rendered the form and the server declared the schema; a bridge that second-guessed either
would only add a third opinion. It is passed through exactly as it arrived, and omitted
entirely when the client sent none.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from acp.interfaces import Client
from acp.schema import (
    AcceptElicitationResponse,
    CancelElicitationResponse,
    DeclineElicitationResponse,
    ElicitationFormSessionMode,
    ElicitationSchema,
)
from pydantic import ValidationError

from python_acp.mcp_stdio import MalformedServerRequest
from python_acp.turns import ClientGates, Gate

logger = logging.getLogger(__name__)

#: The MCP method this module answers. The ACP method it calls has the same name on the
#: wire, which is a coincidence of two specs agreeing — do not collapse the two.
MCP_ELICITATION_CREATE = "elicitation/create"

#: MCP's three answers. ACP's union is wider, and `_action` narrows it.
_ACCEPT = "accept"
_DECLINE = "decline"
_CANCEL = "cancel"


@dataclass(frozen=True)
class ConnectedClient:
    """The live ACP connection, and what it said it could do.

    Looked up per elicitation rather than captured once: a backend outlives the connection
    that created it, so "who is connected" is a question with a different answer at the
    end of a session than at its start.
    """

    client: Client
    gates: ClientGates


#: How the forwarder finds the client to ask. `None` means nobody is connected.
Connected = Callable[[], ConnectedClient | None]

#: The `toolCallId` of the MCP call in flight for this session, or `None` between calls.
#: Looked up per elicitation for the same reason the client is: it changes constantly, and
#: a value captured when the backend was spawned would name a call that finished long ago.
RunningToolCall = Callable[[], str | None]

#: Answers one MCP `elicitation/create`: its params in, its result out.
Forwarder = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def forwarder(
    session_id: str,
    connected: Connected,
    running_tool_call: RunningToolCall | None = None,
) -> Forwarder:
    """Build the handler for one session's `elicitation/create` requests.

    Bound to a session id because that is what the ACP request must carry, and every
    backend a session opened shares it.

    `running_tool_call` supplies the optional `toolCallId`, so a client can attach the
    question to the tool call that provoked it rather than floating it beside the
    transcript. Optional, and `None` from it is ordinary: a server may elicit outside any
    `tools/call`, and then there is genuinely nothing to attach to.
    """

    async def forward(params: dict[str, Any]) -> dict[str, Any]:
        message, schema = _question(params)
        current = connected()
        if current is None:
            logger.warning(
                "elicitation/create for session %s arrived with no ACP client connected; "
                "answering cancel",
                session_id,
            )
            return {"action": _CANCEL}
        if not current.gates.allows(Gate.ELICITATION_FORM):
            # `allows`, never `require`: a client without the capability is conforming,
            # and `UngatedClientCallError` would report our bug for their absence. This is
            # only reachable across a reconnect — see "The window this does not close".
            logger.warning(
                "elicitation/create for session %s cannot be forwarded: the connected "
                "client does not support form elicitation; answering cancel",
                session_id,
            )
            return {"action": _CANCEL}

        response = await current.client.create_elicitation(
            message=message,
            mode=ElicitationFormSessionMode(
                sessionId=session_id,
                toolCallId=None if running_tool_call is None else running_tool_call(),
                requestedSchema=schema,
            ),
        )
        return _result(response)

    return forward


def _question(params: dict[str, Any]) -> tuple[str, ElicitationSchema]:
    """Read an MCP `elicitation/create` params object, or refuse it as `-32602`.

    Both members are required by MCP. A server that omits or mistypes either has sent bad
    params, and saying so is more useful than asking a human a question with no text.
    """
    message = params.get("message")
    if not isinstance(message, str):
        raise MalformedServerRequest("elicitation/create requires a string 'message'")
    requested = params.get("requestedSchema")
    if not isinstance(requested, dict):
        raise MalformedServerRequest(
            "elicitation/create requires an object 'requestedSchema'"
        )
    try:
        return message, ElicitationSchema.model_validate(requested)
    except ValidationError as exc:
        raise MalformedServerRequest(
            f"elicitation/create carried an unreadable 'requestedSchema': {exc}"
        ) from exc


def _result(response: Any) -> dict[str, Any]:
    """Narrow ACP's four answers onto MCP's three."""
    if isinstance(response, AcceptElicitationResponse):
        result: dict[str, Any] = {"action": _ACCEPT}
        if response.content is not None:
            # Passed through untouched: the client filled the server's own schema, and a
            # third opinion on the shape would only be a new way to be wrong.
            result["content"] = response.content
        return result
    if isinstance(response, DeclineElicitationResponse):
        return {"action": _DECLINE}
    if isinstance(response, CancelElicitationResponse):
        return {"action": _CANCEL}
    # An `OtherElicitationResponse`, or whatever a later SDK adds. MCP has three actions
    # and no room for a fourth, so the honest reduction is "no explicit choice".
    logger.info(
        "The client answered elicitation/create with %s, which MCP cannot express; "
        "answering cancel",
        getattr(response, "action", type(response).__name__),
    )
    return {"action": _CANCEL}


__all__ = [
    "MCP_ELICITATION_CREATE",
    "Connected",
    "ConnectedClient",
    "Forwarder",
    "forwarder",
]
