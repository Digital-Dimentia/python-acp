"""The failure surface, as one coherent table (`pyacp-6ni.3`).

Negative cases exist elsewhere in this suite — `test_agent.py` has fifteen `-32602`
assertions, `test_paths.py` owns the path rules, `test_transport_ws.py` owns framing. They
are correct and they stay. What none of them can show is whether the mapping is
**coherent**: that every way of being wrong lands on the code the `acp-protocol` skill
documents, that two different wrongnesses do not collapse into one code, and that nothing
falls through to a bare `-32603`. That is a property of the whole surface, so it is
asserted here, in one place, organised as a taxonomy.

Everything here is driven **over the wire** through `serve_websocket`, not by calling the
router in process. A client's experience of an error includes the envelope, and the layer
that builds the envelope is the layer most likely to lose a code.

## The taxonomy

| Wrongness | Code | Who refused | `data` |
|---|---|---|---|
| Method the agent does not serve | `-32601` | SDK router | `{"method": ...}` |
| Frame that is not JSON | `-32700` | `transport_ws.py` | — |
| Payload that is not a JSON object | `-32600` | `transport_ws.py` | — |
| `params` not an object, or missing | `-32602` | SDK schema | `{"errors": [...]}` |
| Required field missing | `-32602` | SDK schema | `{"errors": [{"type": "missing", ...}]}` |
| Field of the wrong type | `-32602` | SDK schema | `{"errors": [{"loc": [...], ...}]}` |
| Path that is not absolute | `-32602` | `paths.py` | `{"reason": "..."}` |
| Session id nobody issued | `-32602` | `sessions.py` | `{"reason": "..."}` |
| `authenticate` | `-32000` | `agent.py`, by decision | — |

**`data` says which layer refused**, and that turns out to be the useful discriminator:
`errors` is pydantic's own report and carries `loc`, `reason` is a sentence one of our
`ValueError` subclasses wrote, and `source: "mcp"` means the code came from the backend
(`errors.md`). Nothing sets more than one of them.

## Two holes that are the SDK's, and are deliberately pinned

`salvage_on_error` and `skip_invalid_items` mean two wrong inputs are **not** errors at
all: a junk `protocolVersion` becomes the default, and a malformed entry in `mcpServers`
is dropped before the agent ever sees it. You cannot refuse what never arrives. Both are
asserted below as *current behaviour* rather than as desirable behaviour, so an SDK bump
that changes either fails here instead of surprising a client.

## Where a bad path is not an error at all

Absoluteness is checked at the `session/new` edge and is `-32602`. **Containment** is
checked when a turn tries to use a path, and is a `stopReason: "refusal"` — the request
was well-formed ACP and the turn may already have emitted updates a client cannot un-see.
`test_a_path_outside_the_roots_refuses_the_turn_rather_than_erroring` states that boundary
so it reads as a decision rather than a gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from python_acp.transport_ws import serve_websocket
from test_transport_ws import FakeWebSocket

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"
TIMEOUT = 15


@contextlib.asynccontextmanager
async def wire() -> AsyncIterator[FakeWebSocket]:
    """A socket bound to a live agent, for driving bad input at it."""
    websocket = FakeWebSocket()
    connection = asyncio.create_task(serve_websocket(websocket))
    try:
        yield websocket
    finally:
        websocket.hang_up()
        await asyncio.wait_for(connection, timeout=TIMEOUT)


async def error_for(request: dict[str, Any]) -> dict[str, Any]:
    """Send one request and return its `error` member, failing loudly on success."""
    async with wire() as websocket:
        reply = await asyncio.wait_for(websocket.ask(request), timeout=TIMEOUT)
    assert "error" in reply, f"expected a failure, got result {reply.get('result')!r}"
    return reply["error"]


async def raw_error_for(frame: str) -> dict[str, Any]:
    """Feed one raw frame — not necessarily JSON — and return the error it draws."""
    async with wire() as websocket:
        websocket.feed(frame)
        reply = await asyncio.wait_for(websocket.next_reply(), timeout=TIMEOUT)
    assert "error" in reply, f"expected a failure, got {reply!r}"
    return reply["error"]


def request(method: str, params: Any = None, request_id: int = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


# ---------------------------------------------------------------------------
# Methods the agent does not serve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "no/such",
        # Inside a namespace the agent *does* serve, so this is not a prefix match.
        "session/nope",
        # An MCP method name that is not on the deprecated passthrough list either.
        "resources/subscribe",
    ],
)
async def test_a_method_the_agent_does_not_serve_is_method_not_found(method: str) -> None:
    error = await error_for(request(method, {}))

    assert error["code"] == -32601
    assert error["message"] == "Method not found"
    # The name is in `data` rather than interpolated into the message: a concise sentence
    # in `message`, structured detail in `data`, per `errors.md`.
    assert error["data"] == {"method": method}


async def test_an_unknown_notification_draws_no_reply_at_all() -> None:
    """There is no reply channel for a notification, so an error would go nowhere.

    Asserted by the absence of a frame rather than by a code, which is the only way to
    assert it: a notification that *did* answer would be the bug.
    """
    async with wire() as websocket:
        websocket.feed({"jsonrpc": "2.0", "method": "no/such", "params": {}})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(websocket.next_reply(), timeout=1)


# ---------------------------------------------------------------------------
# Framing — the transport's, because the SDK only ever sees decoded dicts
# ---------------------------------------------------------------------------


async def test_a_frame_that_is_not_json_is_a_parse_error() -> None:
    error = await raw_error_for("{not json")

    assert error["code"] == -32700
    assert error["message"] == "Parse error"


@pytest.mark.parametrize("payload", ["[1, 2]", '"a string"', "42", "null", "true"])
async def test_a_payload_that_is_not_an_object_is_an_invalid_request(payload: str) -> None:
    """Valid JSON, but nothing a JSON-RPC message can be. A different failure from -32700."""
    error = await raw_error_for(payload)

    assert error["code"] == -32600
    assert error["message"] == "Invalid request"


async def test_a_framing_error_answers_with_a_null_id() -> None:
    """There is no id to echo — the frame was never parsed far enough to have one.

    JSON-RPC requires the member to be present and null rather than omitted, and a client
    correlating replies by id needs it that way to tell a framing failure from a lost one.
    """
    async with wire() as websocket:
        websocket.feed("{not json")
        reply = await asyncio.wait_for(websocket.next_reply(), timeout=TIMEOUT)

    assert "id" in reply, "the id member must be present, not omitted"
    assert reply["id"] is None


# ---------------------------------------------------------------------------
# Params: absent, wrong shape, wrong type, missing fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "params"),
    [
        ("a list", [1, 2]),
        ("a string", "params"),
        ("a number", 7),
        ("omitted entirely", None),
    ],
)
async def test_params_that_are_not_an_object_are_invalid_params(
    label: str, params: Any
) -> None:
    error = await error_for(request("session/new", params))

    assert error["code"] == -32602, label
    assert error["message"] == "Invalid params"
    assert "errors" in error["data"], "the schema refused it, so pydantic's report rides along"


@pytest.mark.parametrize(
    ("method", "params", "missing"),
    [
        ("session/new", {"mcpServers": []}, "cwd"),
        ("session/prompt", {"prompt": []}, "sessionId"),
        ("initialize", {}, "protocolVersion"),
        ("session/set_mode", {"sessionId": "x"}, "modeId"),
    ],
)
async def test_a_missing_required_field_names_the_field_it_missed(
    method: str, params: dict[str, Any], missing: str
) -> None:
    """`-32602` alone would not be actionable; `loc` is what tells a client what to fix."""
    error = await error_for(request(method, params))

    assert error["code"] == -32602
    reported = [entry for entry in error["data"]["errors"] if entry["type"] == "missing"]
    assert [entry["loc"] for entry in reported] == [[missing]]


@pytest.mark.parametrize(
    ("method", "params", "field", "kind"),
    [
        ("session/new", {"cwd": 123, "mcpServers": []}, "cwd", "string_type"),
        ("session/new", {"cwd": None, "mcpServers": []}, "cwd", "string_type"),
        (
            "session/new",
            {"cwd": "/tmp", "additionalDirectories": "not-a-list", "mcpServers": []},
            "additionalDirectories",
            "list_type",
        ),
        ("session/new", {"cwd": "/tmp", "mcpServers": "not-a-list"}, "mcpServers", "list_type"),
        ("session/prompt", {"sessionId": "x", "prompt": "not-a-list"}, "prompt", "list_type"),
    ],
)
async def test_a_field_of_the_wrong_type_is_invalid_params(
    method: str, params: dict[str, Any], field: str, kind: str
) -> None:
    error = await error_for(request(method, params))

    assert error["code"] == -32602
    reported = {entry["loc"][0]: entry["type"] for entry in error["data"]["errors"]}
    assert reported.get(field) == kind


# ---------------------------------------------------------------------------
# Path constraints (Phase 2.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "names"),
    [
        ({"cwd": "relative/path", "mcpServers": []}, "cwd"),
        ({"cwd": ".", "mcpServers": []}, "cwd"),
        (
            {"cwd": "/tmp", "additionalDirectories": ["also/relative"], "mcpServers": []},
            "additionalDirectories[0]",
        ),
    ],
)
async def test_a_path_that_is_not_absolute_is_refused_and_says_which_one(
    params: dict[str, Any], names: str
) -> None:
    """ACP requires absolute paths. `reason` names the input, since a session may declare several."""
    error = await error_for(request("session/new", params))

    assert error["code"] == -32602
    assert error["message"] == "Invalid params"
    assert names in error["data"]["reason"]
    assert "absolute" in error["data"]["reason"]


async def test_a_path_outside_the_roots_refuses_the_turn_rather_than_erroring() -> None:
    """Containment is checked when a turn *uses* a path, and is not a JSON-RPC error.

    The boundary between the two path rules, stated so it reads as a decision. Absoluteness
    is a malformed request and is `-32602` at the `session/new` edge. Containment fails
    later, on a request that was well-formed ACP, and by then the turn may have emitted
    updates a client cannot un-see — so `stopReason: "refusal"` is the honest answer and an
    error object would be wrong twice over.
    """
    async with wire() as websocket:
        created = await asyncio.wait_for(
            websocket.ask(
                request(
                    "session/new",
                    {
                        "cwd": "/tmp",
                        "mcpServers": [
                            {
                                "name": "tools",
                                "command": sys.executable,
                                "args": [str(FIXTURE_SERVER)],
                                "env": [],
                            }
                        ],
                    },
                    request_id=1,
                )
            ),
            timeout=TIMEOUT,
        )
        prompted = await asyncio.wait_for(
            websocket.ask(
                request(
                    "session/prompt",
                    {
                        "sessionId": created["result"]["sessionId"],
                        "prompt": [
                            {
                                "type": "text",
                                "text": json.dumps({"tool": "echo", "read": "/etc/passwd"}),
                            }
                        ],
                    },
                    request_id=2,
                )
            ),
            timeout=TIMEOUT,
        )

    assert "error" not in prompted, "containment is a refusal, not a JSON-RPC error"
    assert prompted["result"]["stopReason"] == "refusal"


# ---------------------------------------------------------------------------
# Identifiers nobody issued
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("session/prompt", {"sessionId": "nope", "prompt": []}),
        ("session/set_mode", {"sessionId": "nope", "modeId": "x"}),
        ("session/load", {"sessionId": "nope", "cwd": "/tmp", "mcpServers": []}),
        ("session/close", {"sessionId": "nope"}),
    ],
)
async def test_a_session_id_nobody_issued_is_invalid_params(
    method: str, params: dict[str, Any]
) -> None:
    """A `ValueError` subclass, so `-32602` comes for free and nothing special-cases it."""
    error = await error_for(request(method, params))

    assert error["code"] == -32602
    assert "nope" in error["data"]["reason"]


async def test_cancelling_an_unknown_session_is_silent_not_an_error() -> None:
    """`session/cancel` is a notification: there is nowhere to put an error, so there is none."""
    async with wire() as websocket:
        websocket.feed(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "nope"}}
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(websocket.next_reply(), timeout=1)


# ---------------------------------------------------------------------------
# Declined by decision, not by failure
# ---------------------------------------------------------------------------


async def test_authenticate_is_declined_with_auth_required_not_method_not_found() -> None:
    """Declining means writing the method and returning the honest error.

    Deleting the member would answer `-32601`, which says the agent does not have the
    method — a different and untrue statement from "there is nothing to authenticate with".
    """
    error = await error_for(request("authenticate", {"methodId": "anything"}))

    assert error["code"] == -32000
    assert error["code"] != -32601


# ---------------------------------------------------------------------------
# What the SDK silently salvages — pinned as behaviour, not endorsed as design
# ---------------------------------------------------------------------------


async def test_a_junk_protocol_version_is_salvaged_instead_of_refused() -> None:
    """`salvage_on_error` on the schema field: you cannot refuse what never arrives.

    Pinned so an SDK bump that starts refusing it fails here rather than in a client.
    """
    async with wire() as websocket:
        reply = await asyncio.wait_for(
            websocket.ask(request("initialize", {"protocolVersion": "junk"})), timeout=TIMEOUT
        )

    assert "error" not in reply
    assert reply["result"]["protocolVersion"] == 1


async def test_a_malformed_mcp_server_entry_is_dropped_not_refused() -> None:
    """`skip_invalid_items` on the list: the entry never reaches the agent.

    **This is accepted behaviour, not an unfixed bug** — `pyacp-mej` settled it. ACP's
    schema annotates `mcpServers` `skip-invalid-items`, one of 35 fields it marks that way
    beside 84 more marked `default-on-error`, so dropping an unparseable entry rather than
    failing the message is what the protocol asks for. Refusing here would not be fixing a
    gap; it would be one agent unilaterally opting out of ACP's robustness rule on one
    field. `agent.md` carries the reasoning and what it costs.

    The test stays because the behaviour is invisible everywhere else: nothing in `src/`
    can even detect it, since the agent is handed the survivors and never learns what was
    sent. If an SDK bump stops salvaging, this fails and the decision gets re-opened
    deliberately rather than by surprise.
    """
    async with wire() as websocket:
        reply = await asyncio.wait_for(
            websocket.ask(
                request("session/new", {"cwd": "/tmp", "mcpServers": [{"nope": 1}]})
            ),
            timeout=TIMEOUT,
        )

    assert "error" not in reply
    assert reply["result"]["sessionId"]


async def test_an_entry_missing_only_args_and_env_is_dropped_too() -> None:
    """The shape a real client actually gets wrong, as against `{"nope": 1}`.

    All four of `name`, `command`, `args`, and `env` are required with no defaults, so the
    plausible-looking entry below is dropped exactly like nonsense is. That is why the
    refusal a client eventually hits names all four.
    """
    async with wire() as websocket:
        reply = await asyncio.wait_for(
            websocket.ask(
                request(
                    "session/new",
                    {
                        "cwd": "/tmp",
                        "mcpServers": [{"name": "tools", "command": "/bin/echo"}],
                    },
                )
            ),
            timeout=TIMEOUT,
        )

    assert "error" not in reply
    assert reply["result"]["sessionId"]


# ---------------------------------------------------------------------------
# The mapping, as one table
# ---------------------------------------------------------------------------


#: Every code this agent is allowed to answer with, and what earns it. The point of the
#: table is exhaustiveness: a code outside it means the mapping grew a case nobody
#: documented, and `errors.md` plus the `acp-protocol` skill are where it must be recorded.
DOCUMENTED_CODES = {
    -32700: "a frame that is not JSON",
    -32600: "a payload that is not a JSON-RPC object",
    -32601: "a method the agent does not serve",
    -32602: "params the agent will not accept",
    -32603: "an internal failure, or a backend error with no code of its own",
    -32000: "authenticate, declined by decision",
}


async def test_no_wrong_input_produces_a_code_outside_the_documented_set() -> None:
    """The coherence check the bead is really asking for.

    Each case here is a different *kind* of wrong. Two of them collapsing onto one code
    would be a mapping that stopped distinguishing things a client needs distinguished,
    and any of them reaching `-32603` would mean a `ValueError` escaped unmapped.
    """
    cases: list[tuple[str, dict[str, Any] | str]] = [
        ("not json", "{not json"),
        ("not an object", "[1,2]"),
        ("unknown method", request("no/such", {})),
        ("params not an object", request("session/new", "x")),
        ("missing field", request("session/new", {"mcpServers": []})),
        ("wrong type", request("session/new", {"cwd": 1, "mcpServers": []})),
        ("relative path", request("session/new", {"cwd": "rel", "mcpServers": []})),
        ("unknown session", request("session/prompt", {"sessionId": "no", "prompt": []})),
        ("authenticate", request("authenticate", {"methodId": "x"})),
    ]

    seen: dict[str, int] = {}
    for label, payload in cases:
        error = (
            await raw_error_for(payload)
            if isinstance(payload, str)
            else await error_for(payload)
        )
        seen[label] = error["code"]
        assert error["code"] in DOCUMENTED_CODES, (
            f"{label} answered {error['code']}, which is not in DOCUMENTED_CODES"
        )
        assert error["code"] != -32603, f"{label} fell through to a bare internal error"

    # The distinctions that have to survive: framing, routing, and params are three
    # different answers, and a client acts differently on each.
    assert seen["not json"] != seen["not an object"] != seen["unknown method"]
    assert seen["unknown method"] != seen["missing field"]
    assert seen["authenticate"] not in {seen["unknown method"], seen["missing field"]}


async def test_data_names_the_layer_that_refused() -> None:
    """`errors` is pydantic's, `reason` is ours, `source` is the backend's. Never two at once."""
    from_schema = await error_for(request("session/new", {"mcpServers": []}))
    from_us = await error_for(request("session/new", {"cwd": "rel", "mcpServers": []}))

    assert set(from_schema["data"]) == {"errors"}
    assert set(from_us["data"]) == {"reason"}
    assert "source" not in from_schema["data"] and "source" not in from_us["data"], (
        "an error we originate never claims to have come from MCP"
    )
