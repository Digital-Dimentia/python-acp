"""One mapping from our exception types onto `acp.RequestError`.

Three callers need the same translation — `agent.py`, the WebSocket transport, and the
turn executor once Phase 3 lands — and the alternative to a module is the mapping living
wherever it was first needed, which is how a backend `-32601` and a bridge `-32601`
became indistinguishable in the first place. See decision B7 in
`docs/module-boundaries.md`.

## The rule

**A message is a concise sentence; structured detail goes in `data`.** That is the ACP
schema's own instruction for `Error.message` (`acp/schema.py`), and it is what the SDK's
`RequestError` constructors already do — `invalid_params({...})` produces the message
`"Invalid params"` and puts the specifics in `data`. This module follows them rather
than inventing a second convention, so an error from the SDK-dispatched path and an
error from a hand-rolled transport read the same on the wire.

**One exception, and it is the point of the module: an error forwarded from the MCP
backend keeps the backend's code *and* its message.** The server already wrote a
concise sentence, and replacing it with ours would destroy the only description of what
actually failed. Forwarding makes the code space ambiguous — the same integer can now
come from us or from the server — so a forwarded error always carries
`data["source"] == "mcp"`. **An error we originate never sets that key**, and that is
the discriminator a client uses to tell "this agent has no such method" from "the MCP
server behind it has no such method".

## What this module does not cover

`mcp_stdio.py` also builds JSON-RPC error objects, for requests the MCP *server* sends
*us*. Those go out on the MCP wire, in the opposite direction, to a peer for whom
`source: "mcp"` would be nonsense. They are the `mcp-protocol` skill's territory and are
deliberately not routed through here.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from acp import RequestError

from python_acp.mcp_stdio import MCPProtocolError

#: `acp.schema.Error` accepts this code alongside the six JSON-RPC standard ones and the
#: two ACP codes (`-32000` auth required, `-32002` resource not found), but `RequestError`
#: supplies no constructor for it and the schema carries no description. It is LSP's
#: `RequestCancelled`, which ACP's error shape otherwise mirrors exactly. Treated here as
#: "a request was cancelled before it produced a result" on that basis; `pyacp-tzd.5`
#: owns request cancellation and should confirm the reading against the ACP docs before
#: putting it on the wire.
REQUEST_CANCELLED = -32800

#: Marks an error whose code belongs to the MCP server's namespace rather than ours.
MCP_SOURCE = "mcp"

_T = TypeVar("_T")


def to_request_error(exc: BaseException) -> RequestError:
    """Translate one of our exceptions into the error a client should receive.

    `asyncio.CancelledError` is deliberately **not** handled. It is a `BaseException`
    because swallowing it breaks task cancellation: returning a value from a cancelled
    coroutine tells asyncio the cancellation did not take, and the task keeps running.
    A caller that needs to *report* a cancellation calls `request_cancelled()` on the
    side that observed it, having already let the exception propagate.
    """
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    if isinstance(exc, RequestError):
        # Already mapped — by `agent.py`, or by the SDK. Re-wrapping would bury a
        # deliberate `-32000` under a generic `-32603`.
        return exc
    if isinstance(exc, MCPProtocolError):
        return _backend_error(exc)
    if isinstance(exc, ValueError):
        return RequestError.invalid_params({"reason": str(exc)})
    return RequestError.internal_error({"reason": str(exc)})


def request_cancelled(reason: str | None = None) -> RequestError:
    """The error for a request abandoned before it produced a result.

    Not raised by `to_request_error` — see its docstring. This exists so `REQUEST_CANCELLED`
    has one home rather than being spelled out at each site that reports a cancellation.
    """
    return RequestError(REQUEST_CANCELLED, "Request cancelled", _reason(reason))


def _backend_error(exc: MCPProtocolError) -> RequestError:
    """An MCP failure, with as much of the server's own answer as it gave us.

    A codeless failure — timeout, transport death, a malformed result — is one *we*
    invented, so it takes the generic `-32603` and must not carry `source: "mcp"`:
    claiming the backend produced a code it never sent is exactly the fidelity loss
    this mapping exists to prevent.
    """
    if exc.code is None:
        return RequestError.internal_error({"reason": str(exc)})
    data: dict[str, Any] = {"source": MCP_SOURCE, "mcpCode": exc.code}
    if exc.data is not None:
        data["mcpData"] = exc.data
    return RequestError(exc.code, str(exc), data)


def to_error_object(error: RequestError) -> dict[str, Any]:
    """Render the JSON-RPC `error` member for a transport that frames its own messages.

    Not `RequestError.to_error_obj()`: that always emits a `data` key, `null` included,
    and `data` is optional in JSON-RPC. Presence is meaningful here — `data` is where
    the `source` discriminator lives — so an error with nothing structured to say omits
    the key rather than asserting `null`.

    The SDK-dispatched path does not use this; `acp.Connection` renders its own
    envelopes. This is for `ws_bridge.py` and its successor.
    """
    rendered: dict[str, Any] = {"code": error.code, "message": str(error)}
    if error.data is not None:
        rendered["data"] = error.data
    return rendered


def _reason(reason: str | None) -> dict[str, Any] | None:
    return {"reason": reason} if reason else None


def as_request_error(
    method: Callable[..., Awaitable[_T]],
) -> Callable[..., Awaitable[_T]]:
    """Make an agent method's exceptions arrive as mapped `RequestError`s.

    **This is not belt-and-braces.** `acp.Connection._run_request` catches a
    non-`RequestError` and answers `RequestError.internal_error({"details": str(exc)})`
    — so an `MCPProtocolError` that escapes an agent method reaches the client as a
    bare `-32603`, with the backend's own code destroyed, which is precisely the defect
    this module exists to prevent. A `ValueError` fares no better: it becomes `-32603`
    instead of `-32602`.

    So the mapping has to happen on our side of that boundary, and it is applied to
    every request-serving method rather than to the ones that need it today. The bodies
    below arrive over three more phases; a decorator already in place is a requirement a
    later phase cannot forget, and it costs nothing on a method that only ever raises
    `RequestError` (which `to_request_error` returns unchanged).

    Notification handlers are **not** decorated. A notification has no reply channel, so
    raising anything at all is already the bug; see `PythonAcpAgent.cancel`.
    """

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return await method(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise to_request_error(exc) from exc

    # Marked rather than merely wrapped, so a test can tell this decorator from any
    # other one a method might pick up later.
    wrapper.maps_errors = True  # type: ignore[attr-defined]
    return wrapper
