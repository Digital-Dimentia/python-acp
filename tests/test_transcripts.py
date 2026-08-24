"""Golden JSON-RPC transcripts: the whole conversation, in order (`pyacp-6ni.2`).

Every other test in this suite asserts a *fact* — this reply has that code, that update
carries this text. A transcript asserts the **shape of the conversation**: which messages
crossed the wire, in which direction, and in what order. Those are different failures.
A unit test that checks `stopReason == "cancelled"` still passes when a `session/update`
starts arriving *after* the response it belongs to, and a client that renders updates into
a transcript pane is broken by exactly that.

## What is recorded

`RecordingSocket` sits where the WebSocket frames do and appends one entry per message in
both directions, so the golden file is the interleaving as it actually happened rather
than two lists stitched together afterwards. The flows are driven strictly sequentially —
the client never has two requests in flight — so the interleaving is deterministic and a
diff means a real change.

Recording is at the **message** level, not the byte level. Framing differs between the two
transports (WebSocket text frames against newline-delimited JSON on stdio) but the
messages do not, and `test_transport_stdio.py` already drives the stdio binding with the
SDK's own client. What is unique to stdio framing — one complete JSON object per line,
never an embedded newline — is asserted here as its own transcript rather than duplicating
all four flows over a second transport.

## Regenerating

Golden files are **recorded**, not hand-written. When a change to the wire is intentional:

    make transcripts        # or: PYTHON_ACP_RECORD_TRANSCRIPTS=1 pytest tests/test_transcripts.py

then read the diff before committing it. That review is the point of the whole file: a
regeneration that nobody looked at is worse than no transcript, because it launders a
regression into a golden file.

## Non-determinism

Session ids, tool-call ids, and terminal ids are minted per run, so `canonicalize()`
replaces each with a stable placeholder (`<session-1>`) the first time it is seen. That is
deliberately positional: it preserves *which* id appeared where — a swap of two session ids
still fails — while dropping only the randomness.

**Environment paths are the other half, and they are easier to miss.** A recorded
`session/new` carries the interpreter that spawned the MCP server and the absolute path of
the fixture, so a transcript recorded in `.venv` fails in `.venv311`, and one recorded in
your checkout fails in CI's. Neither is a wire change. `_ENVIRONMENT` maps each to a
placeholder; anything else absolute that reaches a golden file belongs there too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION

from python_acp.mcp_registry import McpBackendRegistry
from python_acp.sessions import SessionRegistry
from python_acp.transport_ws import serve_websocket

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"
TRANSCRIPTS = Path(__file__).parent / "transcripts"
TIMEOUT = 20

#: Set by `make transcripts` to rewrite the golden files instead of asserting against them.
RECORDING = os.environ.get("PYTHON_ACP_RECORD_TRANSCRIPTS") == "1"

CLIENT_TO_AGENT = "client->agent"
AGENT_TO_CLIENT = "agent->client"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class RecordingSocket:
    """A `FakeWebSocket` that also keeps an ordered, directional log.

    Inbound frames are recorded when the transport **reads** them rather than when the
    driver queues them, so the log is the order the agent actually saw — which is the
    order that matters when the question is whether an update escaped its turn.
    """

    remote_address = ("transcript", 0)

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._replies: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.log: list[dict[str, Any]] = []
        self.closed = False

    def __aiter__(self) -> RecordingSocket:
        return self

    async def __anext__(self) -> str:
        frame = await self._inbox.get()
        if frame is None:
            raise StopAsyncIteration
        self.log.append({"dir": CLIENT_TO_AGENT, "message": json.loads(frame)})
        return frame

    async def send(self, data: str) -> None:
        message = json.loads(data)
        self.log.append({"dir": AGENT_TO_CLIENT, "message": message})
        self._replies.put_nowait(message)

    async def close(self) -> None:
        self.closed = True

    # -- driver side ----------------------------------------------------

    def feed(self, message: dict[str, Any]) -> None:
        self._inbox.put_nowait(json.dumps(message))

    def hang_up(self) -> None:
        self._inbox.put_nowait(None)

    async def next_message(self) -> dict[str, Any]:
        return await asyncio.wait_for(self._replies.get(), timeout=TIMEOUT)

    async def ask(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one request and return its response, letting notifications pass."""
        self.feed(message)
        wanted = message.get("id")
        while True:
            reply = await self.next_message()
            if reply.get("id") == wanted:
                return reply

    async def answer(self, respond: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """Wait for an agent-initiated *request* and reply to it.

        `session/request_permission` is the one the tool router always sends, and a
        transcript that skipped it would be missing the round trip that gates every tool
        call this project makes.
        """
        while True:
            message = await self.next_message()
            if message.get("method") and message.get("id") is not None:
                self.feed(respond(message))
                return message


@contextlib.asynccontextmanager
async def recording_socket() -> AsyncIterator[RecordingSocket]:
    """One socket bound to a live agent, with the registries wired as `cli.py` wires them.

    Explicit rather than left to `serve_websocket`'s defaults because a session's MCP
    subprocesses are torn down by the `on_close` hook and by nothing else, and a
    disconnect deliberately does not close sessions. See `wire()` in
    `tests/test_negative.py` — same harness, same reason (`pyacp-6k5`).
    """
    backends = McpBackendRegistry()
    sessions = SessionRegistry(on_close=backends.close)
    socket = RecordingSocket()
    connection = asyncio.create_task(
        serve_websocket(socket, sessions=sessions, backends=backends)
    )
    try:
        yield socket
    finally:
        socket.hang_up()
        await asyncio.wait_for(connection, timeout=TIMEOUT)
        await sessions.close_all()


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------

_MINTED = (
    # (json key, placeholder prefix) — every value minted fresh per run.
    ("sessionId", "session"),
    ("toolCallId", "tool-call"),
    ("terminalId", "terminal"),
)
_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$")

#: Values that differ per machine, per checkout, and per `VENV_DIR`, and are therefore not
#: wire content. Longest first, so a path that contains another is replaced whole.
_ENVIRONMENT: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (sys.executable, "<python>"),
            (str(FIXTURE_SERVER), "<fixture-server>"),
            (str(Path(__file__).parent.parent), "<repo>"),
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def _despecialize(value: str) -> str:
    for actual, placeholder in _ENVIRONMENT:
        value = value.replace(actual, placeholder)
    return value


def canonicalize(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace per-run values with stable placeholders, preserving identity.

    Positional rather than blanket: the *first* session id becomes `<session-1>` and every
    later occurrence of that same id becomes `<session-1>` too, so a transcript that
    swapped two sessions still fails. Blanking them to a constant would hide that.
    """
    seen: dict[str, str] = {}

    def placeholder(prefix: str, value: str) -> str:
        if value not in seen:
            seen[value] = f"<{prefix}-{sum(1 for v in seen.values() if v.startswith(f'<{prefix}-')) + 1}>"
        return seen[value]

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if isinstance(value, str):
                    minted = next((p for k, p in _MINTED if k == key), None)
                    if minted is not None:
                        out[key] = placeholder(minted, value)
                        continue
                    if _ISO_TIMESTAMP.match(value):
                        out[key] = "<timestamp>"
                        continue
                out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            if node in seen:
                # An id echoed in a position that is not its own key — a refusal message
                # naming the session, say.
                return seen[node]
            return _despecialize(node)
        return node

    return [walk(entry) for entry in entries]


def assert_matches_golden(name: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare a recorded transcript to its golden file, or rewrite it when recording."""
    recorded = canonicalize(entries)
    path = TRANSCRIPTS / f"{name}.json"
    if RECORDING:
        TRANSCRIPTS.mkdir(exist_ok=True)
        path.write_text(json.dumps(recorded, indent=2) + "\n")
        return recorded

    assert path.exists(), (
        f"No golden transcript at {path}. Record one with `make transcripts`, "
        "then read the diff before committing it."
    )
    expected = json.loads(path.read_text())
    assert recorded == expected, (
        f"{name} transcript changed.\n"
        f"If the change is intentional run `make transcripts` and review the diff.\n"
        f"{_first_difference(expected, recorded)}"
    )
    return recorded


def _first_difference(expected: list[Any], recorded: list[Any]) -> str:
    """Point at the first entry that differs; a whole-list diff buries the lede."""
    for index, (want, got) in enumerate(zip(expected, recorded)):
        if want != got:
            return (
                f"First difference at entry {index}:\n"
                f"  expected: {json.dumps(want)}\n"
                f"  recorded: {json.dumps(got)}"
            )
    if len(expected) != len(recorded):
        return f"Length differs: expected {len(expected)} entries, recorded {len(recorded)}"
    return ""


# ---------------------------------------------------------------------------
# Reading a transcript
# ---------------------------------------------------------------------------


def outbound(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["message"] for e in entries if e["dir"] == AGENT_TO_CLIENT]


def updates(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m["params"]["update"] for m in outbound(entries) if m.get("method") == "session/update"]


def index_of_response(entries: list[dict[str, Any]], request_id: int) -> int:
    for index, entry in enumerate(entries):
        message = entry["message"]
        if entry["dir"] == AGENT_TO_CLIENT and message.get("id") == request_id and (
            "result" in message or "error" in message
        ):
            return index
    raise AssertionError(f"no response to request {request_id} in the transcript")


def indices_of_updates(entries: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, entry in enumerate(entries)
        if entry["dir"] == AGENT_TO_CLIENT and entry["message"].get("method") == "session/update"
    ]


# ---------------------------------------------------------------------------
# The flows
# ---------------------------------------------------------------------------


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
    },
}


def _mcp_server() -> dict[str, Any]:
    return {
        "name": "tools",
        "command": sys.executable,
        "args": [str(FIXTURE_SERVER)],
        "env": [],
    }


#: The option ids `turn_mcp_router.PERMISSION_OPTIONS` puts on the wire. Spelled out here
#: rather than imported, because a transcript test that borrowed the agent's own constant
#: would still pass if the ids changed — and the ids are the wire contract a client codes
#: against. `test_the_permission_request_offers_the_documented_options` pins them.
PERMISSION_OPTION_IDS = ["approve", "approve_for_session", "reject", "reject_for_session"]


def _approve(request: dict[str, Any]) -> dict[str, Any]:
    """Answer `session/request_permission` with `approve` — `allow_once`, one call.

    Note this is an option **id**, not a `PermissionOptionKind`. Sending a kind here is
    accepted by the schema and then matches no option, which the router treats as a
    rejection: the tool is skipped and the turn still ends `end_turn`. Cheap to get wrong
    and silent when you do, which is half of why this file exists.
    """
    return {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"outcome": {"outcome": "selected", "optionId": "approve"}},
    }


async def test_initialize_transcript() -> None:
    """The handshake, whole. What an editor sees before it decides what it may ask for."""
    async with recording_socket() as socket:
        await socket.ask(INITIALIZE)
        entries = list(socket.log)

    assert_matches_golden("initialize", entries)
    assert len(entries) == 2, "a handshake is one request and one response"


async def test_session_lifecycle_transcript() -> None:
    """new -> prompt -> close, and the refusal a prompt that names no tool earns."""
    async with recording_socket() as socket:
        await socket.ask(INITIALIZE)
        created = await socket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new",
             "params": {"cwd": "/tmp", "mcpServers": []}}
        )
        session_id = created["result"]["sessionId"]
        await socket.ask(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": session_id, "prompt": []}}
        )
        await socket.ask(
            {"jsonrpc": "2.0", "id": 4, "method": "session/close",
             "params": {"sessionId": session_id}}
        )
        entries = list(socket.log)

    recorded = assert_matches_golden("session_lifecycle", entries)

    # An empty prompt refuses rather than silently completing, and says why first.
    prompt_response = recorded[index_of_response(recorded, 3)]["message"]
    assert prompt_response["result"]["stopReason"] == "refusal"
    assert indices_of_updates(recorded), "a refusal still explains itself in an update"
    assert max(indices_of_updates(recorded)) < index_of_response(recorded, 3)


async def test_streaming_transcript() -> None:
    """A real tool call: the notification storm, the permission round trip, the response.

    This is the flow where ordering carries meaning. Every `session/update` for a turn
    must land **before** the response to the `session/prompt` that started it — a client
    rendering updates into a pane has no way to place one that arrives afterwards.
    """
    async with recording_socket() as socket:
        await socket.ask(INITIALIZE)
        created = await socket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new",
             "params": {"cwd": "/tmp", "mcpServers": [_mcp_server()]}}
        )
        session_id = created["result"]["sessionId"]

        socket.feed(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {
                 "sessionId": session_id,
                 "prompt": [{"type": "text",
                             "text": json.dumps({"tool": "echo",
                                                 "arguments": {"text": "transcript"}})}],
             }}
        )
        # The router asks before every tool call; nothing proceeds until this is answered.
        await socket.answer(_approve)
        while True:
            message = await socket.next_message()
            if message.get("id") == 3:
                break
        entries = list(socket.log)

    recorded = assert_matches_golden("streaming", entries)

    prompt_response = index_of_response(recorded, 3)
    assert recorded[prompt_response]["message"]["result"]["stopReason"] == "end_turn"

    # The ordering invariant, stated rather than left implicit in the golden file.
    assert indices_of_updates(recorded), "a tool call streams its progress"
    assert max(indices_of_updates(recorded)) < prompt_response, (
        "a session/update arrived after the prompt response it belongs to"
    )

    kinds = [update["sessionUpdate"] for update in updates(recorded)]
    assert kinds.index("tool_call") < kinds.index("tool_call_update"), (
        "a tool call must be announced before it is updated"
    )


async def test_the_permission_request_offers_the_documented_options() -> None:
    """The four option ids are what a client codes against, so they are pinned on the wire.

    Read off the recorded streaming transcript rather than from `PERMISSION_OPTIONS`: a
    test that imported the agent's own constant would agree with it no matter what it said.
    """
    golden = json.loads((TRANSCRIPTS / "streaming.json").read_text())
    requests = [
        entry["message"]
        for entry in golden
        if entry["message"].get("method") == "session/request_permission"
    ]
    assert len(requests) == 1, "the router asks once per tool call"
    offered = [option["optionId"] for option in requests[0]["params"]["options"]]
    assert offered == PERMISSION_OPTION_IDS


async def test_cancellation_transcript() -> None:
    """A turn torn down mid-tool-call. The sequence is the contract, not just the stopReason."""
    async with recording_socket() as socket:
        await socket.ask(INITIALIZE)
        created = await socket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new",
             "params": {"cwd": "/tmp", "mcpServers": [_mcp_server()]}}
        )
        session_id = created["result"]["sessionId"]

        # `stall` is read by the fixture and never answered, so the turn is still inside
        # the tool call when the cancellation lands — which is the case worth recording.
        socket.feed(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {
                 "sessionId": session_id,
                 "prompt": [{"type": "text", "text": json.dumps({"tool": "stall"})}],
             }}
        )
        await socket.answer(_approve)
        socket.feed(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}}
        )
        while True:
            message = await socket.next_message()
            if message.get("id") == 3:
                break
        entries = list(socket.log)

    recorded = assert_matches_golden("cancellation", entries)

    prompt_response = index_of_response(recorded, 3)
    assert recorded[prompt_response]["message"]["result"]["stopReason"] == "cancelled"
    assert prompt_response == len(recorded) - 1, (
        "the prompt response is the last thing a cancelled turn sends"
    )

    cancel = next(
        index
        for index, entry in enumerate(recorded)
        if entry["message"].get("method") == "session/cancel"
    )
    assert cancel < prompt_response, "the cancellation must precede the response it causes"


# ---------------------------------------------------------------------------
# The guard on the guard
# ---------------------------------------------------------------------------


async def test_a_reordering_of_notifications_fails_the_suite() -> None:
    """The acceptance criterion, asserted directly.

    A golden comparison is only worth having if it actually catches a reordering, and
    "it obviously would" is how suites end up not catching things. So: record a real
    transcript, swap two adjacent notifications, and prove the comparison rejects it.
    """
    async with recording_socket() as socket:
        await socket.ask(INITIALIZE)
        created = await socket.ask(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new",
             "params": {"cwd": "/tmp", "mcpServers": []}}
        )
        await socket.ask(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": created["result"]["sessionId"], "prompt": []}}
        )
        entries = list(socket.log)

    positions = indices_of_updates(entries)
    assert len(positions) >= 2, "this flow needs two updates to swap"
    scrambled = list(entries)
    first, second = positions[0], positions[1]
    scrambled[first], scrambled[second] = scrambled[second], scrambled[first]

    # The same comparison the flow tests run, against a transcript only reordering broke.
    golden = json.loads((TRANSCRIPTS / "session_lifecycle.json").read_text())
    assert canonicalize(entries) != canonicalize(scrambled)
    assert canonicalize(scrambled)[: len(golden)] != golden


#: Absolute paths a golden transcript is allowed to contain. Every one is a literal the
#: flows choose on purpose and that means the same thing on every machine — not something
#: the recording environment supplied.
ALLOWED_ABSOLUTE_PATHS = {"/tmp"}

_ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def test_no_golden_transcript_carries_a_path_from_the_machine_that_recorded_it() -> None:
    """The guard on `canonicalize`, and the one that would have caught this in review.

    A transcript that bakes in `sys.executable` or a checkout path passes for whoever
    recorded it and fails for everyone else — in CI, in a second venv, in another clone.
    It shipped once exactly that way: recorded under `.venv` on macOS, green locally, and
    broken the moment it ran anywhere else. The failure did not look like a wire change,
    which is what made it easy to miss.

    Asserting `canonicalize` handled *this* run's paths would be circular — it is the
    thing under test. So this reads the committed files and refuses any absolute path that
    is not a declared literal, which fails for a machine-specific path no matter which
    machine recorded it.
    """
    offenders: list[str] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]")
        elif isinstance(node, str) and _ABSOLUTE_PATH.match(node):
            if node not in ALLOWED_ABSOLUTE_PATHS:
                offenders.append(f"{where}: {node!r}")

    for path in sorted(TRANSCRIPTS.glob("*.json")):
        walk(json.loads(path.read_text()), path.name)

    assert not offenders, (
        "Golden transcripts contain absolute paths that came from the recording machine.\n"
        "Add a placeholder to `_ENVIRONMENT` and re-record, or declare the literal in\n"
        "`ALLOWED_ABSOLUTE_PATHS` if it really is the same everywhere:\n  "
        + "\n  ".join(offenders)
    )


def test_every_golden_transcript_is_reachable_from_a_test() -> None:
    """A golden file nothing asserts against is a file that can rot unnoticed."""
    recorded = {path.stem for path in TRANSCRIPTS.glob("*.json")}
    asserted = {"initialize", "session_lifecycle", "streaming", "cancellation"}
    assert recorded == asserted, "a golden transcript grew or vanished without a test"


@pytest.mark.skipif(RECORDING, reason="recording rewrites the files this test reads")
def test_golden_transcripts_are_canonical_on_disk() -> None:
    """Committed files must be what recording produces, or every diff is noise."""
    for path in TRANSCRIPTS.glob("*.json"):
        content = path.read_text()
        assert content == json.dumps(json.loads(content), indent=2) + "\n", (
            f"{path.name} is not formatted the way `make transcripts` writes it"
        )


# ---------------------------------------------------------------------------
# The other transport, at the only level where it differs
# ---------------------------------------------------------------------------


async def test_stdio_frames_the_same_messages_one_json_object_per_line() -> None:
    """The bead asks for both transports *if the framing differs*. It does.

    WebSocket carries one message per text frame; stdio carries newline-delimited JSON,
    where an embedded newline in a payload would split one message into two and desync the
    stream for good. So this drives the real subprocess over a raw pipe — no SDK client,
    because the SDK would hide the bytes that are the whole question — and asserts the
    framing *and* that the message on the other side is the one the WebSocket transcript
    recorded. Same messages, different envelope.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "python_acp.cli", "--transport", "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(INITIALIZE).encode() + b"\n")
        await process.stdin.drain()
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=TIMEOUT)
    finally:
        process.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=TIMEOUT)

    # Framing: exactly one line, terminated, and nothing but the object inside it.
    assert raw.endswith(b"\n"), "a stdio message is newline-terminated"
    assert raw.count(b"\n") == 1, "an embedded newline would split one message into two"
    response = json.loads(raw)

    # And the message itself is the one the WebSocket transport sent for the same request.
    golden = json.loads((TRANSCRIPTS / "initialize.json").read_text())
    expected = next(e["message"] for e in golden if e["dir"] == AGENT_TO_CLIENT)
    assert canonicalize([{"dir": AGENT_TO_CLIENT, "message": response}])[0]["message"] == expected
