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
import uuid
from dataclasses import dataclass
from typing import Any

from acp.schema import (
    AgentMessageChunk,
    ContentToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from python_acp.mcp_registry import McpBackendRegistry
from python_acp.mcp_stdio import MCPStdioClient
from python_acp.turns import TurnContext, TurnResult

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


class McpToolRouterExecutor:
    """Runs the tool calls a prompt names, against that session's MCP backends.

    Constructed with the backend registry rather than reading it off `TurnContext`:
    `docs/module-boundaries.md` has this module reach `mcp_registry.py` directly, so the
    context does not have to widen for one executor's dependency.
    """

    def __init__(self, backends: McpBackendRegistry) -> None:
        self._backends = backends

    async def execute(self, context: TurnContext, prompt: list[Any]) -> TurnResult:
        backends = self._backends.backends(context.session_id)
        try:
            invocations = self._parse(prompt, backends)
        except PromptConventionError as exc:
            return await self._refuse(context, exc)

        for invocation in invocations:
            await self._run(context, backends, invocation)
        return TurnResult.ended()

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
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            raise PromptConventionError(
                f"Prompt block {index} is {_describe(block)}, and this agent runs tools "
                "rather than reading prose, so only text blocks are understood."
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

    async def _run(self, context: TurnContext, backends: Any, invocation: Invocation) -> None:
        """One tool call, announced before it starts and updated when it ends.

        `pending` → `in_progress` → `completed`/`failed`. The first two are separate
        notifications on purpose: a client renders the call the moment it is known, and
        the transition to `in_progress` is what tells it the wait has begun rather than
        the request being queued behind something else.
        """
        tool_call_id = uuid.uuid4().hex
        await context.emit(
            ToolCallStart(
                sessionUpdate="tool_call",
                toolCallId=tool_call_id,
                title=invocation.title,
                kind="other",
                status="pending",
                rawInput=invocation.arguments,
            )
        )
        await context.emit(
            ToolCallProgress(
                sessionUpdate="tool_call_update", toolCallId=tool_call_id, status="in_progress"
            )
        )

        client = backends[invocation.server]
        logger.debug("Calling %s for session %s", invocation.title, context.session_id)
        result = await client.call_tool(invocation.tool, invocation.arguments)

        await context.emit(
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId=tool_call_id,
                # `isError` is the MCP-sanctioned way for a tool to report its own
                # failure on an otherwise successful call. It becomes a status, never an
                # exception — see the module docstring.
                status="failed" if result["isError"] else "completed",
                content=_as_tool_content(result),
                rawOutput=result,
            )
        )

    async def _refuse(self, context: TurnContext, exc: PromptConventionError) -> TurnResult:
        """Say why, then stop. A silent refusal is worse than a wrong one."""
        logger.info("Refusing prompt for session %s: %s", context.session_id, exc)
        await context.emit(
            AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                content=TextContentBlock(type="text", text=f"{exc} {CONVENTION}"),
            )
        )
        return TurnResult("refusal")


def _as_tool_content(result: dict[str, Any]) -> list[ContentToolCallContent] | None:
    """Carry the tool's own output through as ACP tool-call content.

    Text only for now: `pyacp-eg1.1` owns the richer mapping (images, embedded resources,
    annotations). A block this does not understand is **skipped rather than guessed at**,
    because a wrong `type` on the wire is harder to notice than a missing block.
    """
    blocks = [
        ContentToolCallContent(
            type="content", content=TextContentBlock(type="text", text=block["text"])
        )
        for block in result.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return blocks or None


def _describe(block: Any) -> str:
    kind = getattr(block, "type", None)
    return f"a {kind!r} block" if isinstance(kind, str) else f"a {type(block).__name__}"
