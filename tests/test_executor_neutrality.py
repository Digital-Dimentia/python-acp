"""Proof that MCP is one executor's business and not the runtime's.

Decision D3 says `session/prompt` runs behind a swappable executor. `pyacp-eg1.2` asks
for the check that the swap is real: that nothing MCP-specific has leaked into the
session registry, the capability block, or the update-emission path, so a second backend
could be added without touching any of them.

The bead is explicit that the check must be a **running executor, not an inspection** —
so `WordCountExecutor` below is a complete, conforming turn executor that has never heard
of MCP, and the tests drive it through the SDK's own router for a whole session
lifecycle. Two structural tests stand behind it: one that walks the seam's imports, and
one that pins the capability builder's signature. An inspection alone would pass on a
runtime that could not actually host a second backend; a running turn alone would not
notice an import creeping back in.

Two things these tests deliberately do **not** call leaks, because on inspection neither
is one:

* **`session/new` opens the client's `mcpServers` whatever executor is wired in.**
  `mcpServers` is an ACP request parameter, not an implementation detail — an LLM-backed
  executor would want those servers just as much — so opening them belongs to the agent
  rather than to whatever runs the turn. What matters is that the *turn seam* carries
  nothing backend-shaped, which
  `test_the_turn_context_hands_over_nothing_backend_shaped` checks.
* **`mcpCapabilities` in the capability block.** An ACP-defined field describing which
  *client-supplied* MCP transports `session/new` will accept; every ACP agent answers
  it. What *would* be a leak is the block depending on our backend registry, and
  `test_the_capability_block_cannot_see_a_backend` forbids that.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from acp.agent.router import build_agent_router
from acp.helpers import update_agent_message_text, update_user_message_text
from acp.schema import (
    SessionConfigOptionBoolean,
    SessionMode,
    SessionModeState,
)

from python_acp.agent import PythonAcpAgent
from python_acp.capabilities import build_agent_capabilities
from python_acp.sessions import SessionRegistry
from python_acp.turns import TurnContext, TurnResult

SRC = Path(__file__).resolve().parent.parent / "src" / "python_acp"

#: The modules a second backend must be able to leave alone. `sessions.py` is the session
#: registry, `capabilities.py` is the capability block, and `turns.py` is the seam the
#: update-emission path runs through — the three named in the bead's acceptance criteria.
SEAM = ("sessions.py", "capabilities.py", "turns.py")

#: Anything here appearing in a seam module's imports means the seam knows about a
#: backend. `elicitation.py` is on the list because it is reached *from* MCP even though
#: it is ACP-shaped, and `terminals.py` deliberately is not: terminals are a **client**
#: capability every executor may use, not a backend.
BACKEND_MODULES = frozenset(
    {
        "python_acp.mcp_stdio",
        "python_acp.mcp_registry",
        "python_acp.mcp_content",
        "python_acp.turn_mcp_router",
        "python_acp.elicitation",
    }
)


class WordCountExecutor:
    """A complete turn executor with no backend at all.

    Deliberately trivial and deliberately *whole*: it declares all three of the
    `TurnExecutor` Protocol's descriptive attributes, echoes the prompt, emits a real
    `session/update`, and returns a `TurnResult`. Nothing it touches is MCP-shaped, so a
    turn it runs end to end is evidence that the runtime around it is backend-neutral.

    It counts words because it needed to do *something* a tool router does not.
    """

    supported_prompt_blocks = frozenset({"text", "image"})
    session_modes = SessionModeState(
        currentModeId="count",
        availableModes=[
            SessionMode(id="count", name="Count words"),
            SessionMode(id="shout", name="Count and shout"),
        ],
    )
    session_config_options = (
        SessionConfigOptionBoolean(
            type="boolean",
            id="include-empty",
            name="Count empty blocks",
            currentValue=False,
        ),
    )

    def __init__(self) -> None:
        self.turns = 0
        self.seen_modes: list[str] = []

    async def execute(self, context: TurnContext, prompt: list) -> TurnResult:
        self.turns += 1
        modes = context.session.modes
        mode = None if modes is None else modes.current_mode_id
        if mode is not None:
            self.seen_modes.append(mode)
        words = 0
        for block in prompt:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                words += len(text.split())
                await context.emit(update_user_message_text(text))
        answer = f"{words} words"
        if mode == "shout":
            answer = answer.upper()
        await context.emit(update_agent_message_text(answer))
        return TurnResult.ended()


class RecordingClient:
    """The `Client` facade an executor's `emit` reaches, reduced to a list."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs) -> None:
        self.updates.append((session_id, update))


def wire(executor: object) -> tuple[PythonAcpAgent, RecordingClient, object]:
    """An agent over a fresh registry, with `executor` and nothing else supplied.

    `backends` is left to default, which is the case worth testing: a caller that swaps
    the executor does **not** also swap the backend registry, and the resulting agent
    must still serve a whole session without one being used.
    """
    agent = PythonAcpAgent(SessionRegistry(), executor=executor)
    client = RecordingClient()
    agent.on_connect(client)  # type: ignore[arg-type]
    return agent, client, build_agent_router(agent, use_unstable_protocol=True)


# ---------------------------------------------------------------------------
# The structural half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", SEAM)
def test_the_seam_imports_no_backend(module: str) -> None:
    """A second backend must be addable without touching these three modules.

    An import is the thing that makes that false, and it is also the thing that creeps
    back in quietly — one convenience import of `MCPProtocolError` for an `isinstance`
    check would couple the session registry to the backend forever. Read off the AST
    rather than by running the module, so a conditional or function-local import counts
    the same as a top-level one.
    """
    tree = ast.parse((SRC / module).read_text())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert imported & BACKEND_MODULES == set()


def test_the_capability_block_cannot_see_a_backend() -> None:
    """`initialize`'s block is a promise about the agent, not about what runs its turns.

    The signature is the guarantee: the only thing the builder learns about the executor
    is `supported_prompt_blocks`, a set of ACP content-block discriminators. Hand it a
    backend registry and the advertisement could start describing one — which is exactly
    how `mcpCapabilities` would stop meaning "transports `session/new` accepts" and start
    meaning "what we happen to run".
    """
    parameters = inspect.signature(build_agent_capabilities).parameters

    assert set(parameters) == {"unstable", "prompt_blocks"}


def test_mcp_capabilities_is_acps_own_field_and_stays() -> None:
    """The one MCP word in the block, and it belongs to the protocol, not to us.

    `session/new` carries `mcpServers` for every ACP agent, so every ACP agent must say
    which transports it accepts. A backend-neutral runtime still answers this.
    """
    block = build_agent_capabilities(prompt_blocks=frozenset({"text"}))

    assert block.mcp_capabilities is not None
    assert (block.mcp_capabilities.http, block.mcp_capabilities.sse) == (False, False)


# ---------------------------------------------------------------------------
# The running half
# ---------------------------------------------------------------------------


async def test_a_non_mcp_executor_serves_a_whole_session() -> None:
    """Create, prompt, and close — with no backend registry entry ever made."""
    executor = WordCountExecutor()
    agent, client, router = wire(executor)

    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)
    result = await router(
        "session/prompt",
        {
            "sessionId": created.session_id,
            "prompt": [{"type": "text", "text": "one two three"}],
        },
        False,
    )
    await router("session/close", {"sessionId": created.session_id}, False)

    assert result.stop_reason == "end_turn"
    assert executor.turns == 1
    # The turn's own updates reached the client, addressed to its own session.
    said = [u.content.text for _, u in client.updates if hasattr(u, "content")]
    assert said == ["one two three", "3 words"]
    assert {sid for sid, _ in client.updates} == {created.session_id}
    # And no backend was ever started on its behalf. The registry *does* hold an empty
    # entry for the session — `session/new` records "this session opened these servers"
    # whether or not there were any, so `session/close` has something to remove and a
    # second `open` on the same id is still refused. An empty dict is not MCP reaching
    # into the turn; a subprocess would be.
    assert agent.backends.backends(created.session_id) == {}


async def test_the_advertised_prompt_capabilities_are_this_executors() -> None:
    """`promptCapabilities` is derived, so swapping the executor changes the promise.

    The shipped router reads text only; this one also reads images. A block that stayed
    text-only here would be a promise about a component it cannot see.
    """
    _, _, router = wire(WordCountExecutor())

    result = await router("initialize", {"protocolVersion": 1}, False)

    prompt_capabilities = result.agent_capabilities.prompt_capabilities
    assert prompt_capabilities.image is True
    assert prompt_capabilities.audio is False
    assert prompt_capabilities.embedded_context is False


async def test_session_new_advertises_this_executors_modes_and_options() -> None:
    """Both come from the executor, because only the executor can act on either."""
    _, _, router = wire(WordCountExecutor())

    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    assert created.modes.current_mode_id == "count"
    assert [mode.id for mode in created.modes.available_modes] == ["count", "shout"]
    assert [option.id for option in created.config_options] == ["include-empty"]


async def test_a_mode_the_executor_declared_changes_what_a_turn_does() -> None:
    """The whole point of a mode: `session/set_mode` has to reach the turn.

    Asserting the response would only prove the registry stored it. Asserting the turn's
    *output* proves the executor read it back.
    """
    executor = WordCountExecutor()
    _, client, router = wire(executor)
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)

    await router(
        "session/set_mode", {"sessionId": created.session_id, "modeId": "shout"}, False
    )
    await router(
        "session/prompt",
        {"sessionId": created.session_id, "prompt": [{"type": "text", "text": "hi there"}]},
        False,
    )

    assert executor.seen_modes == ["shout"]
    assert [u.content.text for _, u in client.updates if hasattr(u, "content")][-1] == "2 WORDS"


async def test_the_turn_context_hands_over_nothing_backend_shaped() -> None:
    """What an executor is given is a session, a client, and gates. That is the seam.

    A backend arriving here would make every executor's signature depend on which one
    the runtime happened to be built with — the coupling `TurnExecutor` exists to avoid.
    """
    seen: list[TurnContext] = []

    class Inspecting(WordCountExecutor):
        async def execute(self, context: TurnContext, prompt: list) -> TurnResult:
            seen.append(context)
            return TurnResult.ended()

    _, _, router = wire(Inspecting())
    created = await router("session/new", {"cwd": "/work", "mcpServers": []}, False)
    await router("session/prompt", {"sessionId": created.session_id, "prompt": []}, False)

    context = seen[0]
    public = {name for name in vars(context) if not name.startswith("_")}
    assert public == {"session", "client", "gates"}
    assert type(context.client) is RecordingClient
