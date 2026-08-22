"""The shipped default turn executor: prompt blocks in, MCP tool calls out.

Decision D3 says `session/prompt` runs behind a swappable executor, and D1 says there is
no LLM in this runtime. So the default cannot *interpret* a prompt — it can only route
one. A client says which tool to run and with what; this executes it against the
session's MCP backends, streams the tool call's real status transitions back as
`session/update`, and returns.

Nothing here reasons, plans, or retries.

## The invocation convention

**Invented here.** The ACP spec says what a prompt *is* — a list of content blocks — and
nothing about how a block names a tool, because every other agent has a model to work
that out. With no model, the contract has to be explicit, so it is the one thing in this
module a client codes against:

```json
{"type": "text", "text": "{\\"tool\\": \\"echo\\", \\"arguments\\": {\\"text\\": \\"hi\\"}}"}
```

* A **text** content block whose entire text is a JSON object.
* `tool` — required, the MCP tool name.
* `arguments` — optional object, defaults to `{}`.
* `server` — the MCP server name from `session/new`'s `mcpServers`. Optional **only**
  when the session opened exactly one server; required when it opened several, because
  guessing which of two servers a client meant is the kind of help nobody wants.

Explicit `server`/`tool` fields rather than a single `"server/tool"` string: both names
are arbitrary and may contain a slash, so a separator would be ambiguous exactly where
being wrong is silent.

Every text block in the prompt is one invocation, run **in order**.

## Validate everything, then run anything

A prompt is parsed completely before the first tool runs. Tools have side effects — a
turn that wrote two files and *then* refused because the third block was malformed leaves
no way to undo the first two, and no way to tell from the outside that it stopped early.
So a prompt that does not fully parse runs nothing at all.

## A prompt that is not an invocation is a refusal, not an error

`stopReason: "refusal"` exists for exactly this, and it comes with an
`agent_message_chunk` explaining the convention. A JSON-RPC error would be wrong twice
over: the request was well-formed ACP, and by the time a later block fails to parse the
turn may already have emitted notifications a client cannot un-see.

An empty prompt refuses too. It names no tool, so it does not parse as an invocation, and
silently completing is the failure `IdleTurnExecutor` warns about.

## A failed tool does not fail the turn

MCP reports tool-level failure as a **successful** result carrying `isError: true`. The
tool call's `session/update` says `status: "failed"` and carries the tool's own content,
the remaining calls still run, and the turn returns `end_turn` — the turn completed, one
tool did not. Collapsing that into a `stopReason` would lose which tool failed and why.

A *protocol* failure is different: `MCPProtocolError` propagates and `errors.py` maps it,
backend code intact.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from acp.contrib.tool_calls import ToolCallTracker
from acp.helpers import (
    plan_entry,
    text_block,
    tool_content,
    update_agent_message_text,
    update_available_commands,
    update_plan,
    update_user_message_text,
)
from acp.schema import AvailableCommand, ContentToolCallContent, PlanEntry

from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPStdioClient
from python_acp.turns import Gate, TurnContext, TurnResult

logger = logging.getLogger(__name__)

CONVENTION = (
    'Each text block must be a JSON object naming an MCP tool: '
    '{"tool": "<name>", "arguments": {...}, "server": "<name>"}. '
    '"arguments" defaults to {}; "server" may be omitted only when the session opened '
    "exactly one MCP server."
)


class PromptConventionError(ValueError):
    """A prompt block that is not an invocation.

    Never reaches the wire as an error: `execute` catches it and refuses the turn with an
    explanation instead. It is a `ValueError` anyway so that a future caller which lets it
    escape gets `-32602` rather than `-32603` — the prompt is a parameter.
    """


@dataclass(frozen=True)
class Invocation:
    """One parsed tool call, before anything has run."""

    tool: str
    arguments: dict[str, Any]
    server: str | None = None

    @property
    def title(self) -> str:
        """What a client shows for this call.

        Always qualified by server, even when the client omitted it because the session
        had only one. The title outlives the turn — it is in the transcript
        `session/load` replays — and "which server ran this" is not recoverable later
        from an unqualified name.
        """
        return f"{self.server}/{self.tool}" if self.server else self.tool


#: Why each non-text prompt block is declined, keyed by its `type` discriminator.
#:
#: Every one is declined for the **same** reason and it is worth saying out loud: an
#: image, a sound, or an embedded document is context for a model to reason over, and
#: decision D1 puts no model in this runtime. There is no defensible mapping from a
#: picture to an MCP tool call, and inventing one would be worse than refusing.
#:
#: `resource_link` is the odd one: `PromptCapabilities` has fields for image, audio, and
#: embeddedContext only, so **no capability governs a resource link** and a client may
#: send one however this agent answers `initialize`. Refusing it needs its own reason
#: rather than an advertisement, which is why it is here.
DECLINED_BLOCKS: dict[str, str] = {
    "image": "an image, which needs a model to look at it",
    "audio": "audio, which needs a model to listen to it",
    "resource": "an embedded resource, which is context for a model to read",
    "resource_link": (
        "a link to a resource, which this agent would have to fetch and reason about"
    ),
}


class McpToolRouterExecutor:
    """Runs the tool calls a prompt names, against that session's MCP backends.

    Constructed with the backend registry rather than reading it off `TurnContext`:
    `docs/module-boundaries.md` has this module reach `mcp_registry.py` directly, so the
    context does not have to widen for one executor's dependency.
    """

    #: Text only, and that is what `initialize` therefore advertises — `capabilities.py`
    #: derives `promptCapabilities.image`, `.audio`, and `.embeddedContext` from this set,
    #: so the advertisement cannot drift from what this class actually reads.
    supported_prompt_blocks: frozenset[str] = frozenset({"text"})

    def __init__(self, backends: McpBackendRegistry) -> None:
        self._backends = backends

    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult:
        backends = self._backends.backends(context.session_id)
        await self._echo_prompt(context, prompt)
        await self._announce_tools(context, backends)

        try:
            invocations = self._parse(prompt, backends)
        except PromptConventionError as exc:
            return await self._refuse(context, exc)

        plan = _plan_for(invocations)
        await self._emit_plan(context, plan)
        tracker = ToolCallTracker()
        for index, invocation in enumerate(invocations):
            plan[index].status = "in_progress"
            await self._emit_plan(context, plan)
            failed = await self._run(context, tracker, backends, invocation, index)
            plan[index].status = "failed" if failed else "completed"
            await self._emit_plan(context, plan)
        return TurnResult.ended()

    # ------------------------------------------------------------------
    # What happened, before and around the tools
    # ------------------------------------------------------------------

    async def _echo_prompt(self, context: TurnContext, prompt: list[Any]) -> None:
        """Send the prompt back as `user_message_chunk`s.

        Not redundant: the transcript `session/load` replays is built from what this turn
        *emitted*, so without the echo a reloaded session shows the agent talking to
        itself. Ungated, like every `session/update`.
        """
        for block in prompt:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                await context.emit(update_user_message_text(text))

    async def _announce_tools(self, context: TurnContext, backends: Any) -> None:
        """List the session's MCP tools as `available_commands`.

        Emitted at the start of **every** turn, including one about to be refused — that
        is the point. A refusal that also says what *could* have been called is
        actionable; one that only says "that was not an invocation" is not.

        Costs one `tools/list` per server per turn. Against a local subprocess that is
        sub-millisecond, and caching it would need `notifications/tools/list_changed`
        handling to stay honest, which is `pyacp-eg1.1`'s neighbourhood.
        """
        commands: list[AvailableCommand] = []
        for server, client in sorted(backends.items()):
            for tool in await client.list_tools():
                name = tool.get("name")
                if not isinstance(name, str):
                    continue
                commands.append(
                    AvailableCommand(
                        name=f"{server}/{name}",
                        description=tool.get("description") or f"MCP tool {name!r}",
                    )
                )
        await context.emit(update_available_commands(commands))

    async def _emit_plan(self, context: TurnContext, plan: list[PlanEntry]) -> None:
        """Send the plan, if the client accepts plans and there is one.

        `clientCapabilities.plan` gates the *variant*, never the `session/update` call —
        see `turns.md`. So this suppresses the notification rather than skipping `emit`
        for everything else in the turn.

        The whole entry list goes every time, which is what `AgentPlanUpdate` carries;
        the protocol has no per-entry patch.
        """
        if plan and context.allows(Gate.PLAN_UPDATES):
            await context.emit(update_plan(entry.model_copy(deep=True) for entry in plan))

    # ------------------------------------------------------------------
    # Parsing — all of it, before any of it runs
    # ------------------------------------------------------------------

    def _parse(
        self, prompt: list[Any], backends: dict[str, MCPStdioClient] | Any
    ) -> list[Invocation]:
        if not prompt:
            raise PromptConventionError("The prompt is empty, so it names no tool to run.")
        return [
            self._parse_block(index, block, backends) for index, block in enumerate(prompt)
        ]

    def _parse_block(self, index: int, block: Any, backends: Any) -> Invocation:
        kind = getattr(block, "type", None)
        if kind in DECLINED_BLOCKS:
            raise PromptConventionError(
                f"Prompt block {index} is {DECLINED_BLOCKS[kind]}, and this agent runs "
                "tools rather than reasoning, so it is declined."
            )
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            raise PromptConventionError(
                f"Prompt block {index} is {_describe(block)}, and only text blocks carry "
                "an invocation."
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PromptConventionError(
                f"Prompt block {index} is not JSON ({exc.msg})."
            ) from None
        if not isinstance(payload, dict):
            raise PromptConventionError(
                f"Prompt block {index} is a JSON {type(payload).__name__}, not an object."
            )

        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            raise PromptConventionError(
                f"Prompt block {index} has no non-empty string 'tool'."
            )
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise PromptConventionError(
                f"Prompt block {index}: 'arguments' must be an object."
            )
        return Invocation(tool=tool, arguments=arguments, server=self._server(index, payload, backends))

    @staticmethod
    def _server(index: int, payload: dict[str, Any], backends: Any) -> str | None:
        """Which backend runs this call, refusing to guess when guessing could be wrong."""
        named = payload.get("server")
        if named is not None:
            if not isinstance(named, str) or named not in backends:
                raise PromptConventionError(
                    f"Prompt block {index} names server {named!r}; this session opened "
                    f"{sorted(backends) or 'none'}."
                )
            return named
        if not backends:
            raise PromptConventionError(
                f"Prompt block {index} names a tool, but this session opened no MCP "
                "servers to run it against."
            )
        if len(backends) > 1:
            raise PromptConventionError(
                f"Prompt block {index} must name a 'server': this session opened "
                f"{sorted(backends)}."
            )
        return next(iter(backends))

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    async def _run(
        self,
        context: TurnContext,
        tracker: ToolCallTracker,
        backends: Any,
        invocation: Invocation,
        index: int,
    ) -> bool:
        """One tool call, announced before it starts and updated when it ends.

        `pending` → `in_progress` → `completed`/`failed`. The first two are separate
        notifications on purpose: a client renders the call the moment it is known, and
        the transition to `in_progress` is what tells it the wait has begun rather than
        the request being queued behind something else.

        Returns whether the *tool* failed — not whether the call did. See the module
        docstring for why those are different.
        """
        key = str(index)
        await context.emit(
            tracker.start(
                key,
                title=invocation.title,
                kind="other",
                status="pending",
                raw_input=invocation.arguments,
            )
        )
        await context.emit(tracker.progress(key, status="in_progress"))

        client = backends[invocation.server]
        logger.debug("Calling %s for session %s", invocation.title, context.session_id)
        result = await client.call_tool(invocation.tool, invocation.arguments)

        # `isError` is the MCP-sanctioned way for a tool to report its own failure on an
        # otherwise successful call. It becomes a status, never an exception.
        failed = bool(result["isError"])
        await context.emit(
            tracker.progress(
                key,
                status="failed" if failed else "completed",
                content=_as_tool_content(result),
                raw_output=result,
            )
        )
        tracker.forget(key)
        return failed

    async def _refuse(self, context: TurnContext, exc: PromptConventionError) -> TurnResult:
        """Say why, then stop. A silent refusal is worse than a wrong one."""
        logger.info("Refusing prompt for session %s: %s", context.session_id, exc)
        await context.emit(update_agent_message_text(f"{exc} {CONVENTION}"))
        return TurnResult("refusal")


def _as_tool_content(result: dict[str, Any]) -> list[ContentToolCallContent] | None:
    """Carry the tool's own output through as ACP tool-call content.

    Text only for now: `pyacp-eg1.1` owns the richer mapping (images, embedded resources,
    annotations). A block this does not understand is **skipped rather than guessed at**,
    because a wrong `type` on the wire is harder to notice than a missing block.
    """
    blocks = [
        tool_content(text_block(block["text"]))
        for block in result.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return blocks or None


def _plan_for(invocations: list[Invocation]) -> list[PlanEntry]:
    """The turn's plan, complete before the first tool runs.

    That completeness is what makes it an honest plan rather than a guess: the router
    validates every invocation up front, so it already knows every step it will take.
    """
    return [plan_entry(f"Run {invocation.title}", status="pending") for invocation in invocations]


def _describe(block: Any) -> str:
    kind = getattr(block, "type", None)
    return f"a {kind!r} block" if isinstance(kind, str) else f"a {type(block).__name__}"
