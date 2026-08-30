"""The contract a real ACP client codes against, pinned to the wire (`pyacp-8lp`).

`acp-ui` publishes *ACP Agent Field Notes* (`docs/agent-integration.md`, acp-ui 0.2.0): a
checklist of what an agent must put on the wire for a session to render. python-acp
already satisfies every row but one — and it satisfies each of them *incidentally*, by
whichever module happened to get it right. Nothing states them as a contract, so a
refactor that dropped one would pass the whole suite and break the client.

This module is that statement. Every assertion carries the consequence **on the client's
side** of breaking it, because that consequence is the reason the assertion exists and it
is not visible from inside this repo.

Two rules follow the bead:

* **Assert, do not re-implement.** No production change is expected here. When one of
  these fails the fix belongs in production code.
* **Assert on the wire, not on internals.** A test that read `PERMISSION_OPTIONS`
  directly would keep passing if the options stopped being *sent*, which is the failure
  that matters. So the source is a golden transcript — the whole conversation, in order,
  in both directions — or a live recorded turn for the two items no committed transcript
  covers.

The one checklist row we do **not** satisfy, `_meta` inputSchema on per-tool commands, is
`pyacp-ma2` and is deliberately absent here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from test_transcripts import (
    AGENT_TO_CLIENT,
    TRANSCRIPTS,
    _drain_new_session_announcement,
    outbound,
    recording_socket,
    updates,
)

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"

#: `ToolKind`, spelled out rather than imported. The vocabulary is closed on the *wire*:
#: acp-ui switches an icon on it and has no branch for a value the protocol never defined,
#: so a new kind invented here renders as nothing at all. Importing the enum would make
#: this test agree with whatever the enum said.
TOOL_KINDS = frozenset(
    {
        "read",
        "edit",
        "delete",
        "move",
        "search",
        "execute",
        "think",
        "fetch",
        "switch_mode",
        "other",
    }
)

#: The four `PermissionOptionKind`s, same reasoning.
PERMISSION_KINDS = ["allow_once", "allow_always", "reject_once", "reject_always"]


def golden(name: str) -> list[dict[str, Any]]:
    return json.loads((TRANSCRIPTS / f"{name}.json").read_text())


def messages(entries: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    return [e["message"] for e in entries if e["message"].get("method") == method]


def index_of(entries: list[dict[str, Any]], predicate: Any) -> int:
    for index, entry in enumerate(entries):
        if predicate(entry["message"]):
            return index
    raise AssertionError("no such message in the transcript")


def is_update(kind: str) -> Any:
    def check(message: dict[str, Any]) -> bool:
        return (
            message.get("method") == "session/update"
            and message["params"]["update"].get("sessionUpdate") == kind
        )

    return check


# ---------------------------------------------------------------------------
# From the recorded streaming turn
# ---------------------------------------------------------------------------


def test_the_permission_request_names_the_tool_call_it_gates() -> None:
    """acp-ui attaches the permission prompt to the tool call carrying that `toolCallId`.

    A request whose `toolCall.toolCallId` is not one the client has already been told
    about renders as a prompt floating next to nothing, or is dropped. The id matches here
    because `PermissionBroker.request_for()` builds the `ToolCallUpdate` from the tracker;
    a future call site that passed an explicit `tool_call=` would mint a second id and
    nothing else in this suite would notice.
    """
    entries = golden("streaming")
    announced = [u for u in updates(entries) if u["sessionUpdate"] == "tool_call"]
    requests = messages(entries, "session/request_permission")
    assert len(announced) == len(requests) == 1, "the router announces, then asks, once per call"
    assert requests[0]["params"]["toolCall"]["toolCallId"] == announced[0]["toolCallId"]

    # And the id keeps identifying the same call for the rest of the turn, which is what
    # lets the client update the row it already drew rather than adding new ones.
    progressed = [u for u in updates(entries) if u["sessionUpdate"] == "tool_call_update"]
    assert progressed, "a tool call reports its progress"
    assert {u["toolCallId"] for u in progressed} == {announced[0]["toolCallId"]}


def test_the_permission_request_offers_a_way_to_refuse_for_the_whole_session() -> None:
    """acp-ui renders one button per option and offers no refusal the agent did not send.

    Without `reject_always` a user can say "always yes" but never "always no", and is
    asked again about a tool they have already turned down. The SDK's
    `default_permission_options()` omits it; `PERMISSION_OPTIONS` adds it back, so the
    regression to watch for is the SDK changing its defaults under us — which this catches
    because it reads the options off the wire rather than out of that constant.
    """
    requests = messages(golden("streaming"), "session/request_permission")
    assert [option["kind"] for option in requests[0]["params"]["options"]] == PERMISSION_KINDS


def test_a_tool_call_is_announced_before_any_work_begins() -> None:
    """`pending` first, `in_progress` only once the wait has actually started.

    acp-ui draws the row on `tool_call` and turns on its spinner at `in_progress`. Emit
    them in the other order — or fold them into one notification — and a call that is
    waiting on a permission answer or on a client file read reads as a hung agent, because
    the client is told the work began before it was allowed to.
    """
    entries = golden("streaming")
    announced = index_of(entries, is_update("tool_call"))
    permission = index_of(entries, lambda m: m.get("method") == "session/request_permission")
    in_progress = index_of(
        entries,
        lambda m: is_update("tool_call_update")(m)
        and m["params"]["update"].get("status") == "in_progress",
    )
    assert announced < permission < in_progress

    statuses = [u["status"] for u in updates(entries) if "status" in u]
    assert statuses == ["pending", "in_progress", "completed"]


def test_every_tool_call_kind_is_one_the_protocol_defines() -> None:
    """acp-ui picks an icon from the closed `ToolKind` vocabulary.

    A kind outside it matches no branch and renders as nothing, so the risk is a future
    `ToolCatalogue.kind()` that passed an MCP annotation through instead of mapping it.
    That method returns a typed `ToolKind` today and never raises; this asserts the value
    that actually crossed the wire.
    """
    kinds = [u["kind"] for u in updates(golden("streaming")) if "kind" in u]
    permission = messages(golden("streaming"), "session/request_permission")
    kinds += [request["params"]["toolCall"]["kind"] for request in permission]
    assert kinds, "a tool call is announced with a kind"
    assert set(kinds) <= TOOL_KINDS, f"{sorted(set(kinds) - TOOL_KINDS)} is not a ToolKind"


def test_the_prompt_is_echoed_back_byte_for_byte() -> None:
    """acp-ui renders the echoed `user_message_chunk` as the user's message.

    It does not render the prompt it sent. So the echo must be the prompt *exactly*: any
    normalisation — a trim, a re-wrap, a re-serialised JSON block — shows the user
    something they did not type, and a client that also renders its own copy shows the
    prompt twice in two different forms.
    """
    entries = golden("streaming")
    sent = [
        block["text"]
        for message in messages(entries, "session/prompt")
        for block in message["params"]["prompt"]
    ]
    echoed = [
        u["content"]["text"] for u in updates(entries) if u["sessionUpdate"] == "user_message_chunk"
    ]
    assert echoed == sent


def test_a_tool_call_carries_the_raw_input_and_output_a_client_can_inspect() -> None:
    """acp-ui's tool-call detail pane shows `rawInput` and `rawOutput` verbatim.

    They are the only place a user can see what the agent actually sent the server and
    what came back; dropping them leaves the pane empty and the call unauditable.
    """
    entries = golden("streaming")
    announced = [u for u in updates(entries) if u["sessionUpdate"] == "tool_call"]
    completed = [
        u
        for u in updates(entries)
        if u["sessionUpdate"] == "tool_call_update" and u.get("status") == "completed"
    ]
    assert announced[0]["rawInput"] == {"text": "transcript"}
    assert completed[0]["rawOutput"]["content"] == [{"type": "text", "text": "transcript"}]


def test_the_agent_advertises_no_mcp_transport_it_cannot_speak() -> None:
    """acp-ui filters the user's configured MCP servers by `mcpCapabilities`.

    Both directions of a wrong answer are visible to the user: understating withholds
    servers the agent could have run, and overstating hands it a server it cannot reach,
    which surfaces as "MCP is broken". This bridge speaks stdio only, so all three are
    false — and must stay false until one of them is genuinely implemented.
    """
    handshake = next(e["message"] for e in golden("initialize") if e["dir"] == AGENT_TO_CLIENT)
    assert handshake["result"]["agentCapabilities"]["mcpCapabilities"] == {
        "http": False,
        "sse": False,
        "acp": False,
    }


# ---------------------------------------------------------------------------
# Live turns, for the two rows no committed transcript covers
# ---------------------------------------------------------------------------


def _initialize(*, fs: bool) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": fs, "writeTextFile": fs}},
        },
    }


async def _turn(socket: Any, cwd: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive one whole turn, answering whatever the agent asks along the way.

    A generic responder rather than a scripted one, because *which* requests the agent
    makes is part of what these two tests are asserting — a scripted driver would have to
    know in advance, and would deadlock rather than fail on the interesting answer.
    """
    created = await socket.ask(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {
                "cwd": cwd,
                "mcpServers": [
                    {
                        "name": "tools",
                        "command": sys.executable,
                        "args": [str(FIXTURE_SERVER)],
                        "env": [],
                    }
                ],
            },
        }
    )
    session_id = created["result"]["sessionId"]
    await _drain_new_session_announcement(socket, session_id)
    socket.feed(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": json.dumps(payload)}],
            },
        }
    )
    while True:
        message = await socket.next_message()
        if message.get("method") is None and message.get("id") == 3:
            return list(socket.log)
        if message.get("method") and message.get("id") is not None:
            socket.feed(_answer(message))


def _answer(request: dict[str, Any]) -> dict[str, Any]:
    method, params = request["method"], request["params"]
    if method == "session/request_permission":
        result: Any = {"outcome": {"outcome": "selected", "optionId": "approve"}}
    elif method == "fs/read_text_file":
        result = {"content": Path(params["path"]).read_text()}
    elif method == "fs/write_text_file":
        Path(params["path"]).write_text(params["content"])
        result = None
    else:  # pragma: no cover - a new client call belongs in this contract, not here
        raise AssertionError(
            f"the agent asked the client for {method}, which this test cannot answer"
        )
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


async def test_a_tool_call_reports_the_files_it_touches(tmp_path: Path) -> None:
    """acp-ui turns `locations` into the links that jump from a tool call to a file.

    No committed transcript exercises a call that touches files, so this records one. The
    paths are the **resolved** ones, which is the half a client cannot check for itself:
    asked to open an unresolved symlink it would be opening something the containment
    check never saw.
    """
    source = tmp_path / "in.txt"
    source.write_text("one\ntwo\n")
    destination = tmp_path / "out.txt"

    async with recording_socket() as socket:
        await socket.ask(_initialize(fs=True))
        entries = await _turn(
            socket,
            str(tmp_path.resolve()),
            {
                "tool": "echo",
                "read": {"text": {"path": str(source), "line": 2, "limit": 1}},
                "write": {"path": str(destination)},
            },
        )

    announced = [u for u in updates(entries) if u["sessionUpdate"] == "tool_call"]
    assert [(loc["path"], loc.get("line")) for loc in announced[0]["locations"]] == [
        (str(source.resolve()), 2),
        (str(destination.resolve()), None),
    ]
    assert outbound(entries)[-1]["result"]["stopReason"] == "end_turn"


async def test_the_agent_never_calls_an_fs_method_the_client_did_not_advertise(
    tmp_path: Path,
) -> None:
    """`clientCapabilities.fs` is a promise in one direction only.

    acp-ui answers `-32601` to a method it did not advertise, and the SDK's own example
    client does the same. An agent that asks anyway spends a round trip to earn an error
    it could have predicted, and the user sees a turn fail for a reason that is ours. So
    the refusal must come *before* the call: the wire must carry no `fs/*` request at all.
    """
    source = tmp_path / "in.txt"
    source.write_text("one\n")

    async with recording_socket() as socket:
        await socket.ask(_initialize(fs=False))
        entries = await _turn(
            socket,
            str(tmp_path.resolve()),
            {"tool": "echo", "read": {"text": {"path": str(source)}}},
        )

    assert outbound(entries)[-1]["result"]["stopReason"] == "refusal"
    assert not messages(entries, "fs/read_text_file")
    assert not messages(entries, "fs/write_text_file")
    # And it says which capability was missing, so the client's user can fix it.
    said = " ".join(
        u["content"]["text"]
        for u in updates(entries)
        if u["sessionUpdate"] == "agent_message_chunk"
    )
    assert "readTextFile" in said
