"""Tests for forwarding an MCP server's `elicitation/create` to the ACP client.

Three layers, because the failure modes live in different places:

* the translation itself — a fake ACP client, no subprocess;
* the wiring — `mcp_registry` deciding what a backend is promised, and `agent.py`
  deciding whether to promise anything at all;
* the wire — the real `tests/fixtures/mock_mcp_server.py` sending a real
  `elicitation/create` and reading the reply it actually received.

The last is the only one that can prove the round trip, and it is also the only one
that can catch a read loop parked inside its own handler.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from acp.schema import (
    AcceptElicitationResponse,
    CancelElicitationResponse,
    ClientCapabilities,
    DeclineElicitationResponse,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    ElicitationFormSessionMode,
    ElicitationUrlCapabilities,
    McpServerStdio,
    OtherElicitationResponse,
)

from python_acp.elicitation import ConnectedClient, forwarder
from python_acp.mcp_registry import backend_responder, connect_stdio
from python_acp.mcp_stdio import MalformedServerRequest, UnsupportedServerRequest
from python_acp.turns import ClientGates

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"

SCHEMA = {
    "type": "object",
    "title": "Credentials",
    "properties": {
        "user": {"type": "string", "title": "User", "minLength": 2},
        "keep": {"type": "boolean", "default": True},
    },
    "required": ["user"],
}

FORM_CLIENT = ClientCapabilities(
    elicitation=ElicitationCapabilities(form=ElicitationFormCapabilities())
)


def spec(name: str = "tools") -> McpServerStdio:
    return McpServerStdio(
        name=name, command=sys.executable, args=[str(FIXTURE_SERVER)], env=[]
    )


class FakeClient:
    """An ACP client that answers `elicitation/create` with whatever it was given."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls: list[tuple[str, object]] = []

    async def create_elicitation(self, message: str, mode: object, **kwargs) -> object:
        self.calls.append((message, mode))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def connected(client: object, capabilities: ClientCapabilities | None = FORM_CLIENT):
    def lookup() -> ConnectedClient | None:
        return ConnectedClient(client, ClientGates.of(capabilities))

    return lookup


def question(**overrides) -> dict:
    params = {"message": "Who are you?", "requestedSchema": SCHEMA}
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# The translation
# ---------------------------------------------------------------------------


async def test_an_mcp_question_becomes_a_session_scoped_form() -> None:
    """One of ACP's four modes can carry an MCP question, and this is the one."""
    client = FakeClient(AcceptElicitationResponse(action="accept", content={"user": "ada"}))

    result = await forwarder("sess-1", connected(client))(question())

    message, mode = client.calls[0]
    assert message == "Who are you?"
    assert isinstance(mode, ElicitationFormSessionMode)
    assert mode.session_id == "sess-1"
    assert result == {"action": "accept", "content": {"user": "ada"}}


async def test_the_servers_schema_survives_the_trip() -> None:
    """The constraints are the question. A form that loses them asks something else."""
    client = FakeClient(CancelElicitationResponse(action="cancel"))

    await forwarder("sess-1", connected(client))(question())

    _, mode = client.calls[0]
    schema = mode.requested_schema.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert schema["title"] == "Credentials"
    assert schema["required"] == ["user"]
    assert schema["properties"]["user"] == {"title": "User", "minLength": 2, "type": "string"}
    assert schema["properties"]["keep"] == {"default": True, "type": "boolean"}


async def test_an_accepted_answer_with_no_content_carries_no_content_key() -> None:
    """`content` is optional in MCP, and inventing an empty object would be a claim."""
    client = FakeClient(AcceptElicitationResponse(action="accept"))

    assert await forwarder("s", connected(client))(question()) == {"action": "accept"}


@pytest.mark.parametrize(
    ("answer", "action"),
    [
        (DeclineElicitationResponse(action="decline"), "decline"),
        (CancelElicitationResponse(action="cancel"), "cancel"),
        # MCP has exactly three actions, so an ACP extension has no spelling there.
        (OtherElicitationResponse(action="_deferred"), "cancel"),
    ],
)
async def test_acps_four_answers_narrow_onto_mcps_three(answer: object, action: str) -> None:
    client = FakeClient(answer)

    assert await forwarder("s", connected(client))(question()) == {"action": action}


async def test_nobody_connected_is_cancel_not_an_error() -> None:
    """A session outlives its connection, so a backend may ask after the client left.

    `cancel` — the prompt went away without a choice — is what actually happened.
    `decline` would claim a human refused.
    """
    forward = forwarder("s", lambda: None)

    assert await forward(question()) == {"action": "cancel"}


async def test_a_client_without_form_elicitation_is_cancel_not_our_bug() -> None:
    """Reachable only across a reconnect, and still not `UngatedClientCallError`.

    Routing this through `require` would answer -32603 — *we* reached for something
    unadvertised — for a client that is merely conforming.
    """
    client = FakeClient(AcceptElicitationResponse(action="accept"))
    url_only = ClientCapabilities(
        elicitation=ElicitationCapabilities(url=ElicitationUrlCapabilities())
    )

    result = await forwarder("s", connected(client, url_only))(question())

    assert result == {"action": "cancel"}
    assert client.calls == []


async def test_a_client_that_raises_is_left_to_raise() -> None:
    """`mcp_stdio` answers -32603. Flattening it to `cancel` would invent a human."""
    client = FakeClient(RuntimeError("the client exploded"))

    with pytest.raises(RuntimeError, match="exploded"):
        await forwarder("s", connected(client))(question())


@pytest.mark.parametrize(
    "params",
    [
        {"requestedSchema": SCHEMA},
        {"message": 7, "requestedSchema": SCHEMA},
        {"message": "hi"},
        {"message": "hi", "requestedSchema": "not an object"},
        {"message": "hi", "requestedSchema": {"properties": {"a": {"type": 3}}}},
    ],
    ids=["no-message", "message-not-a-string", "no-schema", "schema-not-an-object", "junk-schema"],
)
async def test_unreadable_params_are_the_servers_mistake(params: dict) -> None:
    """-32602, not -32603: the server used a capability we really do declare, badly."""
    client = FakeClient(AcceptElicitationResponse(action="accept"))

    with pytest.raises(MalformedServerRequest):
        await forwarder("s", connected(client))(params)

    assert client.calls == []


# ---------------------------------------------------------------------------
# What a backend is allowed to ask
# ---------------------------------------------------------------------------


async def test_the_responder_serves_both_primitives_and_nothing_else() -> None:
    async def elicit(params: dict) -> dict:
        return {"action": "decline"}

    respond = backend_responder(["/work"], elicit)

    assert await respond("roots/list", {}) == {"roots": [{"uri": "file:///work", "name": "work"}]}
    assert await respond("elicitation/create", question()) == {"action": "decline"}
    with pytest.raises(UnsupportedServerRequest):
        # Never declared, so "we never offered this" rather than "we broke".
        await respond("sampling/createMessage", {})


async def test_a_responder_with_nothing_to_answer_is_no_responder() -> None:
    """A handler that serves nothing would still make `initialize` refuse an empty block."""
    assert backend_responder([], None) is None


async def test_a_backend_promises_elicitation_only_when_it_can_be_forwarded() -> None:
    """Declaring the capability and answering it are one decision, taken here."""

    async def elicit(params: dict) -> dict:
        return {"action": "cancel"}

    with_forwarder = await connect_stdio(spec(), (), elicit)
    try:
        promised = json.loads(
            (await with_forwarder.call_tool("handshake-report", {}))["content"][0]["text"]
        )
    finally:
        await with_forwarder.stop()

    without = await connect_stdio(spec(), ())
    try:
        silent = json.loads(
            (await without.call_tool("handshake-report", {}))["content"][0]["text"]
        )
    finally:
        await without.stop()

    assert promised["capabilities"] == {"elicitation": {}}
    assert silent["capabilities"] == {}


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


async def test_a_real_server_gets_a_real_answer() -> None:
    """The whole round trip: MCP request in, ACP round trip, MCP result out."""
    client = FakeClient(AcceptElicitationResponse(action="accept", content={"user": "ada"}))
    backend = await connect_stdio(spec(), (), forwarder("sess-1", connected(client)))
    try:
        provoked = await asyncio.wait_for(
            backend.call_tool(
                "provoke",
                {
                    "server_method": "elicitation/create",
                    "server_params": question(),
                },
            ),
            timeout=10,
        )
    finally:
        await backend.stop()

    reply = json.loads(provoked["content"][0]["text"])
    assert reply["result"] == {"action": "accept", "content": {"user": "ada"}}
    assert client.calls[0][0] == "Who are you?"


async def test_a_real_server_sending_junk_gets_invalid_params() -> None:
    client = FakeClient(AcceptElicitationResponse(action="accept"))
    backend = await connect_stdio(spec(), (), forwarder("sess-1", connected(client)))
    try:
        provoked = await asyncio.wait_for(
            backend.call_tool(
                "provoke",
                {"server_method": "elicitation/create", "server_params": {"message": "hi"}},
            ),
            timeout=10,
        )
    finally:
        await backend.stop()

    assert json.loads(provoked["content"][0]["text"])["error"]["code"] == -32602


async def test_asking_a_human_does_not_stall_the_read_loop() -> None:
    """The reason a server request is answered in a task of its own.

    An elicitation waits on a person. If the read loop awaited the handler, nothing
    else on this connection could be read meanwhile — including the response to the
    call that provoked the question, which is the deadlock this shape avoids.
    """
    released = asyncio.Event()

    class SlowClient(FakeClient):
        async def create_elicitation(self, message: str, mode: object, **kwargs) -> object:
            self.calls.append((message, mode))
            await released.wait()
            return AcceptElicitationResponse(action="accept", content={"user": "ada"})

    client = SlowClient(None)
    backend = await connect_stdio(spec(), (), forwarder("sess-1", connected(client)))
    try:
        # The server sends `elicitation/create` and answers this call immediately,
        # without waiting for the reply. Receiving it proves the loop is still reading.
        sent = await asyncio.wait_for(
            backend.call_tool(
                "provoke-detached",
                {"server_method": "elicitation/create", "server_params": question()},
            ),
            timeout=10,
        )
        assert sent["content"][0]["text"] == "sent"
        assert client.calls, "the handler should already be waiting on the human"

        released.set()
        for _ in range(200):
            report = await asyncio.wait_for(backend.call_tool("provoke-report", {}), timeout=10)
            replies = json.loads(report["content"][0]["text"])
            if replies:
                break
            await asyncio.sleep(0.01)
    finally:
        await backend.stop()

    assert replies[0]["id"] == "srv-detached"
    assert replies[0]["result"] == {"action": "accept", "content": {"user": "ada"}}


async def test_tearing_the_backend_down_abandons_an_unanswered_question() -> None:
    """`session/close` must not wait on a human who may never answer."""
    waiting = asyncio.Event()

    class NeverAnswers(FakeClient):
        async def create_elicitation(self, message: str, mode: object, **kwargs) -> object:
            waiting.set()
            await asyncio.Event().wait()

    backend = await connect_stdio(
        spec(), (), forwarder("sess-1", connected(NeverAnswers(None)))
    )
    await backend.call_tool(
        "provoke-detached",
        {"server_method": "elicitation/create", "server_params": question()},
    )
    await asyncio.wait_for(waiting.wait(), timeout=10)

    await asyncio.wait_for(backend.stop(), timeout=10)

    assert backend._server_requests == set()


# ---------------------------------------------------------------------------
# Who decides whether anything is promised at all
# ---------------------------------------------------------------------------


def _agent():
    from python_acp.agent import PythonAcpAgent
    from python_acp.sessions import SessionRegistry

    return PythonAcpAgent(SessionRegistry())


async def _promised(capabilities: ClientCapabilities | None) -> dict:
    """What `session/new` really told a backend, read off the handshake it sent."""
    from acp.agent.router import build_agent_router

    agent = _agent()
    router = build_agent_router(agent, use_unstable_protocol=True)
    await router(
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": (
                {} if capabilities is None else capabilities.model_dump(by_alias=True)
            ),
        },
        False,
    )
    # `env` is not optional on the wire: the SDK drops an entry that omits it, silently
    # (`pyacp-mej`), and the session would come back with no backends at all.
    servers = [
        {
            "name": "tools",
            "command": sys.executable,
            "args": [str(FIXTURE_SERVER)],
            "env": [],
        }
    ]
    result = await router("session/new", {"cwd": "/work", "mcpServers": servers}, False)
    session_id = result.session_id
    backend = agent.backends.get(session_id, "tools")
    try:
        report = await asyncio.wait_for(backend.call_tool("handshake-report", {}), timeout=10)
    finally:
        await agent.backends.close(session_id)
    return json.loads(report["content"][0]["text"])["capabilities"]


async def test_session_new_promises_elicitation_to_a_form_capable_clients_backends() -> None:
    """The promise is made when the backends are spawned, from the gate as it is then."""
    assert (await _promised(FORM_CLIENT)).get("elicitation") == {}


async def test_session_new_promises_nothing_to_a_client_that_cannot_be_asked() -> None:
    """A server told it may elicit, with nobody to elicit from, strands itself."""
    promised = await _promised(None)

    assert "elicitation" not in promised
