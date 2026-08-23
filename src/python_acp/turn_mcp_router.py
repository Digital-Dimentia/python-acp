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

from acp.contrib.permissions import PermissionBroker, default_permission_options
from acp.contrib.tool_calls import ToolCallTracker
from acp.exceptions import RequestError
from acp.helpers import (
    plan_entry,
    text_block,
    tool_content,
    update_agent_message_text,
    update_available_commands,
    update_plan,
    update_user_message_text,
)
from acp.schema import (
    AvailableCommand,
    PermissionOption,
    PlanEntry,
    RequestPermissionRequest,
    RequestPermissionResponse,
)

from python_acp.mcp_content import to_tool_call_content
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


class _TurnCancelled(Exception):
    """The client cancelled the turn while its permission prompt was open.

    Internal to this module: `execute` turns it into `stopReason: "cancelled"`. Not an
    `asyncio.CancelledError`, because nothing was actually cancelled — the client
    answered, and answering "cancelled" is a normal response to a normal request.
    """


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


#: What a client is offered before a tool runs.
#:
#: The SDK's `default_permission_options()` plus one. It offers `allow_once`,
#: `allow_always`, and `reject_once` — so a user can say "always yes" but has no way to
#: say "always no", and is asked again about a tool they have already turned down. The
#: asymmetry looks like an oversight rather than a design, and `reject_always` is one of
#: the four `PermissionOptionKind`s the protocol defines, so the fourth option is added
#: here rather than worked around.
PERMISSION_OPTIONS: tuple[PermissionOption, ...] = (
    *default_permission_options(),
    PermissionOption(optionId="reject_for_session", name="Reject for session", kind="reject_always"),
)

#: Which of those options mean "run it". `reject_once` / `reject_always` are the other two.
_ALLOWING_KINDS = frozenset({"allow_once", "allow_always"})

#: Sentinel key in `Session.remembered_permissions` recording that we have already told
#: this session's client we are proceeding without it. Not a tool name, and cannot collide
#: with one: every real key is `server/tool`.
_NO_HUMAN_KEY = "\x00 no permission channel"

#: Which of them mean "and do not ask again this session".
_REMEMBERING_KINDS = frozenset({"allow_always", "reject_always"})


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
        broker = PermissionBroker(
            context.session_id,
            _requester(context),
            tracker=tracker,
            default_options=PERMISSION_OPTIONS,
        )
        for index, invocation in enumerate(invocations):
            plan[index].status = "in_progress"
            await self._emit_plan(context, plan)
            try:
                failed = await self._run(context, tracker, broker, backends, invocation, index)
            except _TurnCancelled:
                # The client cancelled while its permission prompt was open. It said so in
                # the response, which is a different route to the same answer as
                # `session/cancel` cancelling our task — and the only one available when
                # the client chose not to send the notification too.
                plan[index].status = "pending"
                await self._emit_plan(context, plan)
                return TurnResult("cancelled")
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
        broker: PermissionBroker,
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
        if not await self._permitted(context, broker, invocation, key):
            await context.emit(
                tracker.progress(
                    key,
                    status="failed",
                    content=[tool_content(text_block("Denied by the client."))],
                )
            )
            tracker.forget(key)
            return True

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
                content=to_tool_call_content(result),
                raw_output=result,
            )
        )
        tracker.forget(key)
        return failed

    async def _permitted(
        self,
        context: TurnContext,
        broker: PermissionBroker,
        invocation: Invocation,
        key: str,
    ) -> bool:
        """Ask the client whether this call may run, unless it already said always.

        Asked **after** the `tool_call` notification and before `in_progress`, which is
        what `pending` is for: the request carries the tool call, so the client has
        something to attach its prompt to.
        """
        remembered = context.session.remembered_permissions.get(invocation.title)
        if remembered is not None:
            logger.debug("Permission for %s remembered: %s", invocation.title, remembered)
            return remembered

        try:
            response = await broker.request_for(
                key, description=f"Run the MCP tool {invocation.title}"
            )
        except RequestError as exc:
            return await self._without_a_human(context, exc)

        return self._decide(context, invocation, response)

    @staticmethod
    async def _without_a_human(context: TurnContext, exc: RequestError) -> bool:
        """Proceed when the client cannot take permission requests, and say so.

        **This is a correction, made under interop evidence (`pyacp-6ni.4`).** The first
        implementation refused the turn, reasoning that `session/request_permission` is
        mandatory — `ClientCapabilities` has no field for it — so a client answering
        `-32601` is broken. Then the SDK's own `examples/client.py` turned out to answer
        exactly that, and a headless client with no human to ask has nothing else it
        honestly can answer. An agent unusable against the reference client is the agent
        with the problem.

        Proceeding is not "assume consent from nowhere". **The client named this tool and
        these arguments in `session/prompt` itself**, so the authorization already exists;
        the prompt was only ever a courtesy to a human who might be watching, and a client
        that cannot reach one has already made the decision.

        That reasoning is **specific to this executor** and does not generalise. An
        LLM-backed executor *chooses* the tool, so the client's prompt authorizes nothing
        in particular and the fallback would be a hole. Any executor added later must
        decide this again for itself.

        Announced once per session rather than silently, and once rather than per call, so
        a transcript says plainly why nothing was asked.
        """
        already_said = context.session.remembered_permissions.get(_NO_HUMAN_KEY)
        if not already_said:
            context.session.remembered_permissions[_NO_HUMAN_KEY] = True
            await context.emit(
                update_agent_message_text(
                    f"This client answered {exc.code} to session/request_permission, so "
                    "there is nobody to ask. Running the tools this prompt named anyway: "
                    "the prompt is itself the authorization, because this agent only runs "
                    "what the client explicitly named."
                )
            )
        logger.warning(
            "Client refused session/request_permission (%s); proceeding on the prompt's "
            "own authority for session %s",
            exc.code,
            context.session_id,
        )
        return True

    @staticmethod
    def _decide(
        context: TurnContext, invocation: Invocation, response: RequestPermissionResponse
    ) -> bool:
        """Read one permission answer.

        **Denial is a selected option, not an outcome.** `RequestPermissionResponse.outcome`
        is `AllowedOutcome` (`"selected"`, with an `optionId`) or `DeniedOutcome` — whose
        literal is `"cancelled"`, despite the class name. So the only non-selected answer
        the protocol has is *the turn was cancelled*, and reading a rejection as one would
        turn a "no" into `stopReason: cancelled`. That inversion is exactly what this bead
        was told to get right.
        """
        outcome = response.outcome
        if getattr(outcome, "outcome", None) != "selected":
            raise _TurnCancelled
        kind = _KIND_BY_OPTION.get(getattr(outcome, "option_id", ""))
        if kind is None:
            # An option we never offered. Refusing to run is the only safe reading.
            logger.warning("Client chose unknown permission option %r", outcome)
            return False
        allowed = kind in _ALLOWING_KINDS
        if kind in _REMEMBERING_KINDS:
            context.session.remembered_permissions[invocation.title] = allowed
        return allowed

    async def _refuse(self, context: TurnContext, exc: PromptConventionError) -> TurnResult:
        """Say why, then stop. A silent refusal is worse than a wrong one."""
        logger.info("Refusing prompt for session %s: %s", context.session_id, exc)
        await context.emit(update_agent_message_text(f"{exc} {CONVENTION}"))
        return TurnResult("refusal")


#: Option id to kind, for reading an answer back. Built from `PERMISSION_OPTIONS` so the
#: two cannot disagree about what an id means.
_KIND_BY_OPTION: dict[str, str] = {option.option_id: option.kind for option in PERMISSION_OPTIONS}


def _requester(context: TurnContext):
    """Adapt the `Client` facade to the shape `PermissionBroker` calls.

    `session/request_permission` has **no capability gate** — `ClientCapabilities` has no
    field for it and every ACP client must accept it — so this is called straight off the
    client with nothing to check first.
    """

    async def request(payload: RequestPermissionRequest) -> RequestPermissionResponse:
        return await context.client.request_permission(
            session_id=payload.session_id, tool_call=payload.tool_call, options=payload.options
        )

    return request


def _plan_for(invocations: list[Invocation]) -> list[PlanEntry]:
    """The turn's plan, complete before the first tool runs.

    That completeness is what makes it an honest plan rather than a guess: the router
    validates every invocation up front, so it already knows every step it will take.
    """
    return [plan_entry(f"Run {invocation.title}", status="pending") for invocation in invocations]


def _describe(block: Any) -> str:
    kind = getattr(block, "type", None)
    return f"a {kind!r} block" if isinstance(kind, str) else f"a {type(block).__name__}"
