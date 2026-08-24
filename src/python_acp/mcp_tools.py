"""What an MCP server says about its own tools, and what ACP can honestly do with it.

`tools/list` may carry an `annotations` block per tool — `readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint` — since MCP `2025-03-26`. ACP has a
`ToolCall.kind` the client uses to choose an icon and a UI treatment. This module is the
mapping between them, and the per-turn cache that makes reading it affordable.

## The rule that shapes everything here: a hint is not a permission

MCP says it plainly, and it is worth repeating at the top of the file that acts on it:

> Tool annotations are **hints**. They are not guaranteed to provide a faithful
> description of tool behavior. Clients should never make tool use decisions based on
> annotations received from untrusted servers.

`pyacp-eg1.3` opened by suggesting the router "skip the prompt for `readOnlyHint` tools".
**That is refused, and the refusal is the point of this module.** A server asserting
`readOnlyHint: true` and thereby escaping the permission prompt is a privilege escalation
written by the party being restrained. The server is a subprocess the *client* named in
`session/new`, so it is not arbitrary code — but it is also not the human, and a hint is
not consent.

So an annotation changes **how the question looks**, never **whether it is asked**.
`turn_mcp_router.py` still calls `session/request_permission` for every tool call; what it
now sends is a `tool_call` whose `kind` says "delete" rather than "other", so the human
deciding has something better than a generic prompt to decide from. That is a real
improvement and it costs nothing, because the answer still comes from a person.

## The mapping, and why unstated is not the same as false

MCP gives every hint a default — `readOnlyHint: false`, `destructiveHint: true`,
`openWorldHint: true`. Applying all of them mechanically would mean a tool whose
annotation block says only `{"title": "Echo"}` is rendered as a **deletion**, and an
agent that cries wolf on every unannotated tool teaches the human to ignore the icon.
Applying none of them would throw away the one default the spec uses to mean *assume the
worst*.

So the defaults are honoured exactly where the spec means them as caution and the answer
feeds a warning, and nowhere else:

| The server said | ACP `kind` | Why |
|---|---|---|
| no `annotations`, or no boolean `readOnlyHint` | `other` | It made no claim about the question that matters. Guessing would be inventing information |
| `readOnlyHint: true`, `openWorldHint: true` | `fetch` | Read-only and reaches outside the machine |
| `readOnlyHint: true` otherwise | `read` | An unstated `openWorldHint` is cosmetic, so the accurate generic beats the spec default |
| `readOnlyHint: false`, `destructiveHint: false` | `edit` | The server explicitly says its writes are additive |
| `readOnlyHint: false`, `destructiveHint` true or absent | `delete` | **Here** the default means "assume the worst", and here it feeds a warning |

**`delete` is wider than MCP's `destructiveHint`,** and the mapping rounds toward the more
alarming icon deliberately. `destructiveHint` means "may perform destructive updates",
which covers overwriting as well as deleting; ACP's nearest kind is `delete`. Rounding the
other way — calling it `edit` — would understate the risk in the one place a human is
being asked to decide, which is the only place the label costs anything.

`idempotentHint` maps to nothing. ACP has no kind for it, and it describes what happens on
a *retry* — a question this executor never asks, because it runs exactly the invocations
the client named and never retries one.

## `annotations.title` is deliberately not used

MCP's annotation block carries a human-readable `title`, and it would read better in the
prompt than `server/tool`. It is not adopted because `turn_mcp_router` keys
`remembered_permissions` by the invocation's title: a title that came from the server
would let two tools collide under one remembered answer, and would let a server change
what a remembered "always allow" applies to by changing a string. The display name is not
worth that.

## One `tools/list` per server per turn, and a failure costs nothing

`ToolCatalogue` memoises the listing per server for the life of one turn. That bounds the
cost at what `available_commands` already pays when announcements are on, and it means a
turn naming one server on a five-server session lists one server rather than five.

`annotations()` is **best-effort and never raises.** A backend whose `tools/list` is broken
while its `tools/call` works is a backend this executor could serve before annotations
existed, and failing a turn over a presentation hint would be absurd. `listing()` is the
strict form, for `available_commands`, where a listing that cannot be produced is the
thing being asked for.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from acp.schema import ToolKind

logger = logging.getLogger(__name__)

#: What `kind` a tool call gets when the server said nothing we can act on. Also the
#: value every tool had before this module existed.
UNKNOWN_KIND: ToolKind = "other"


def tool_kind(annotations: Any) -> ToolKind:
    """One tool's MCP annotations as an ACP `ToolKind`.

    Takes the raw value off the `tools/list` entry — anything that is not a mapping is a
    server that sent something malformed, and is treated as having said nothing. See the
    module docstring for the table and for why the spec's defaults are applied to
    `destructiveHint` and not to `openWorldHint`.
    """
    if not isinstance(annotations, Mapping):
        return UNKNOWN_KIND
    read_only = annotations.get("readOnlyHint")
    if not isinstance(read_only, bool):
        # No claim about the one question that separates a look from a change.
        return UNKNOWN_KIND
    if read_only:
        return "fetch" if annotations.get("openWorldHint") is True else "read"
    # Not read-only. MCP defaults `destructiveHint` to true precisely so that silence
    # here means "assume the worst", and this is where that default earns its keep.
    return "edit" if annotations.get("destructiveHint") is False else "delete"


class ToolCatalogue:
    """Each backend's `tools/list` for the life of one turn.

    Built per turn rather than per session: caching across turns would need
    `notifications/tools/list_changed` handling to stay honest, and a stale catalogue
    that mislabels a tool is worse than one `tools/list` against a local subprocess.
    """

    def __init__(self, backends: Mapping[str, Any]) -> None:
        self._backends = backends
        self._listings: dict[str, list[dict[str, Any]]] = {}

    async def listing(self, server: str) -> list[dict[str, Any]]:
        """This server's tools, fetched at most once. Raises what `tools/list` raises.

        The strict form. `available_commands` is a listing, so a listing that cannot be
        produced is a failure of the thing being asked for rather than of a decoration.
        """
        cached = self._listings.get(server)
        if cached is not None:
            return cached
        listing = await self._backends[server].list_tools()
        self._listings[server] = listing
        return listing

    async def kind(self, server: str, tool: str) -> ToolKind:
        """The ACP `kind` for one tool, or `other` for any reason at all.

        Never raises. An unreachable listing, a server that does not name the tool, a
        malformed entry — each means the same thing here, which is that nothing is known,
        and `other` is exactly what "nothing is known" looked like before this module.
        """
        try:
            listing = await self.listing(server)
        except Exception:  # noqa: BLE001 — a presentation hint must not fail a turn
            logger.debug("Could not list tools on %r for a tool kind", server, exc_info=True)
            return UNKNOWN_KIND
        for entry in listing:
            if isinstance(entry, dict) and entry.get("name") == tool:
                return tool_kind(entry.get("annotations"))
        return UNKNOWN_KIND


__all__ = ["UNKNOWN_KIND", "ToolCatalogue", "tool_kind"]
