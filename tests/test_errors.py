"""Tests for the one exception-to-`RequestError` mapping.

Two of these check the mapping against the SDK rather than against itself:
`test_every_code_we_can_produce_is_one_acp_recognises` reads the literals out of
`acp.schema.Error`, and `test_request_cancelled_uses_a_code_the_schema_accepts` pins
`-32800` to that same union — it is the one code the schema accepts that `RequestError`
gives no constructor for.
"""

from __future__ import annotations

import asyncio
import typing

import pytest
from acp import RequestError
from acp.schema import Error

from python_acp.errors import (
    MCP_SOURCE,
    REQUEST_CANCELLED,
    request_cancelled,
    to_error_object,
    to_request_error,
)
from python_acp.mcp_stdio import MCPProtocolError


def _codes_the_schema_names() -> set[int]:
    """The codes `acp.schema.Error` enumerates, minus its bare `int` escape hatch."""
    return {
        typing.get_args(arg)[0]
        for arg in typing.get_args(Error.model_fields["code"].annotation)
        if typing.get_origin(arg) is typing.Literal
    }


# ---------------------------------------------------------------------------
# Errors we originate: a concise message, the detail in `data`
# ---------------------------------------------------------------------------


def test_a_validation_failure_is_invalid_params_with_the_reason_in_data() -> None:
    error = to_request_error(ValueError("'arguments' must be an object"))

    assert error.code == -32602
    assert str(error) == "Invalid params"
    assert error.data == {"reason": "'arguments' must be an object"}


def test_an_unexpected_exception_is_an_internal_error() -> None:
    error = to_request_error(RuntimeError("something gave way"))

    assert error.code == -32603
    assert error.data == {"reason": "something gave way"}


@pytest.mark.parametrize(
    "exc",
    [ValueError("bad"), RuntimeError("worse"), MCPProtocolError("MCP request timed out")],
)
def test_an_error_we_originate_never_claims_the_backend_produced_it(exc: Exception) -> None:
    """`data.source` is the client's only way to tell whose code it is holding."""
    data = to_request_error(exc).data or {}

    assert data.get("source") != MCP_SOURCE


def test_an_already_mapped_error_passes_through_untouched() -> None:
    """Re-wrapping would bury a deliberate -32000 under a generic -32603."""
    original = RequestError.auth_required({"methodId": "oauth"})

    assert to_request_error(original) is original


# ---------------------------------------------------------------------------
# Errors forwarded from the MCP backend: the server's code and its own message
# ---------------------------------------------------------------------------


def test_a_backend_code_is_forwarded_with_the_servers_own_message() -> None:
    error = to_request_error(
        MCPProtocolError.from_error_response(
            {"code": -32601, "message": "Unknown tool", "data": {"tool": "nope"}}
        )
    )

    assert error.code == -32601
    assert "Unknown tool" in str(error)
    assert error.data == {"source": MCP_SOURCE, "mcpCode": -32601, "mcpData": {"tool": "nope"}}


def test_a_backend_error_without_data_omits_mcp_data() -> None:
    error = to_request_error(
        MCPProtocolError.from_error_response({"code": -32602, "message": "bad args"})
    )

    assert error.data == {"source": MCP_SOURCE, "mcpCode": -32602}


def test_two_backend_codes_stay_distinguishable() -> None:
    """The defect this mapping exists to prevent: everything collapsing to -32603."""
    not_found = to_request_error(
        MCPProtocolError.from_error_response({"code": -32601, "message": "no such tool"})
    )
    bad_params = to_request_error(
        MCPProtocolError.from_error_response({"code": -32602, "message": "bad args"})
    )

    assert (not_found.code, bad_params.code) == (-32601, -32602)


def test_a_backend_failure_we_invented_takes_the_generic_code() -> None:
    """A timeout has no server-assigned code, so faking one would be a lie."""
    error = to_request_error(MCPProtocolError("MCP request timed out"))

    assert error.code == -32603
    assert error.data == {"reason": "MCP request timed out"}


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancellation_is_re_raised_rather_than_mapped() -> None:
    """Returning a value from a cancelled coroutine tells asyncio the cancel did not take."""
    with pytest.raises(asyncio.CancelledError):
        to_request_error(asyncio.CancelledError())


def test_request_cancelled_uses_a_code_the_schema_accepts() -> None:
    assert request_cancelled().code == REQUEST_CANCELLED
    assert REQUEST_CANCELLED in _codes_the_schema_names()


def test_request_cancelled_carries_a_reason_only_when_given_one() -> None:
    assert request_cancelled().data is None
    assert request_cancelled("client sent $/cancel_request").data == {
        "reason": "client sent $/cancel_request"
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_an_error_with_nothing_structured_to_say_omits_data() -> None:
    """`data` is optional in JSON-RPC, and its *presence* is meaningful here."""
    assert to_error_object(RequestError.parse_error()) == {
        "code": -32700,
        "message": "Parse error",
    }


def test_rendering_keeps_data_when_there_is_some() -> None:
    rendered = to_error_object(RequestError.method_not_found("session/new"))

    assert rendered == {
        "code": -32601,
        "message": "Method not found",
        "data": {"method": "session/new"},
    }


def test_every_code_we_can_produce_is_one_acp_recognises() -> None:
    """An unrecognised code is not illegal, but it should be a decision, not a typo."""
    produced = {
        to_request_error(ValueError("x")).code,
        to_request_error(RuntimeError("x")).code,
        to_request_error(MCPProtocolError("x")).code,
        request_cancelled().code,
        RequestError.parse_error().code,
        RequestError.invalid_request().code,
        RequestError.method_not_found("m").code,
        RequestError.auth_required().code,
        RequestError.resource_not_found("file:///x").code,
    }

    assert produced <= _codes_the_schema_names()
