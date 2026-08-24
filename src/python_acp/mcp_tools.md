# `mcp_tools.py` — what a server says about its own tools, and what ACP may do with it

MCP `2025-03-26` added an `annotations` block to each entry in `tools/list`:
`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`. ACP has a
`ToolCall.kind` that a client uses to pick an icon and a UI treatment. This module is the
mapping between the two, plus the per-turn cache that makes reading it affordable.

The pin has carried these since `pyacp-pb7` moved `_MCP_PROTOCOL_VERSION` to
`2025-06-18` for elicitation. `pyacp-eg1.3` is the bead that finally read them.

## A hint is not a permission

MCP says it in its own spec, and it is worth repeating beside the code that acts on it:

> Tool annotations are **hints**. They are not guaranteed to provide a faithful
> description of tool behavior. Clients should never make tool use decisions based on
> annotations received from untrusted servers.

`pyacp-eg1.3` opened by proposing that the router "skip the prompt for `readOnlyHint`
tools". **That is refused, and the refusal is the point of this module.** A server
asserting `readOnlyHint: true` and thereby escaping the permission prompt is a privilege
escalation written by the party being restrained. The server is a subprocess the *client*
named in `session/new`, so it is not arbitrary code — but it is not the human either, and
a hint is not consent.

So an annotation changes **how the question looks**, never **whether it is asked**.
[turn_mcp_router.py](turn_mcp_router.md) still sends `session/request_permission` for
every tool call; what it now carries is a `tool_call` whose `kind` says `delete` instead
of `other`. `tests/test_turn_mcp_router.py::test_a_read_only_tool_is_still_asked_about` is
the fence, and `tests/transcripts/streaming.json` shows both halves on the wire: the kind
changed, the permission request did not go away.

## The mapping

| The server said | ACP `kind` | Why |
|---|---|---|
| no `annotations`, or no boolean `readOnlyHint` | `other` | It made no claim about the question that matters, and guessing would invent information |
| `readOnlyHint: true`, `openWorldHint: true` | `fetch` | Read-only and reaches outside the machine |
| `readOnlyHint: true` otherwise | `read` | |
| `readOnlyHint: false`, `destructiveHint: false` | `edit` | The server explicitly says its writes are additive |
| `readOnlyHint: false`, `destructiveHint` true or absent | `delete` | MCP defaults it to true precisely to mean "assume the worst" |

Two decisions inside that table are worth stating, because both look arbitrary until the
alternative is written out.

**Unstated is not the same as false, except where the spec means it to be.** MCP gives
every hint a default — `readOnlyHint: false`, `destructiveHint: true`, `openWorldHint:
true`. Applying all of them mechanically would render a tool whose annotation block says
only `{"title": "Echo"}` as a **deletion**, and an agent that cries wolf on every politely
titled tool teaches the human to stop reading the icon. Applying none of them would throw
away the one default the spec uses to mean *assume the worst*. So the defaults are
honoured where the spec means caution and the answer feeds a warning —
`destructiveHint` — and nowhere else. An unstated `openWorldHint` is cosmetic, so `read`
wins over the spec's `fetch`.

**`delete` is wider than `destructiveHint`, and the mapping rounds toward the more
alarming icon deliberately.** `destructiveHint` means "may perform destructive updates",
which covers overwriting as well as deleting; ACP's nearest kind is `delete`. Rounding the
other way — calling it `edit` — would understate the risk in the one place the label costs
anything, which is a prompt a human is answering.

`idempotentHint` maps to nothing. ACP has no kind for it, and it describes what happens on
a **retry** — a question this executor never asks, because it runs exactly the invocations
the client named and never repeats one.

## `annotations.title` is deliberately unused

The block also carries a human-readable `title`, and it would read better in a prompt than
`server/tool`. It is not adopted because `turn_mcp_router` keys `remembered_permissions`
by the invocation's title: a server-supplied title would let two tools collide under one
remembered answer, and would let a server change what a remembered "always allow" applies
to by changing a string. A nicer display name is not worth that.

## One listing per server per turn

`ToolCatalogue` memoises `tools/list` per server for the life of a turn. Per turn rather
than per session, because caching across turns would need
`notifications/tools/list_changed` handling to stay honest, and a stale catalogue that
mislabels a tool is worse than one round trip to a local subprocess.

It is also **lazy per server**: a turn naming one server on a five-server session lists
one. That is what stops annotations from making a multi-server session pay for backends it
never touches.

**This changed what the `announce-tools` option saves**, and the option's own description
was corrected to match. Before, turning announcements off meant a turn issued no
`tools/list` at all. Now a turn still lists the servers it *calls*, because the permission
prompt's `kind` comes from there — the option trades the notification and the untouched
servers, not every listing. Making the kind conditional on that option was considered and
rejected: it is a setting about a notification, and a human deciding whether to run
something called `wipe` should not get a worse question because the client already knew
the tool list.

## Two entry points, because failure means different things

| | Raises? | For |
|---|---|---|
| `listing(server)` | yes | `available_commands`, where the listing **is** the answer being asked for |
| `kind(server, tool)` | never | a tool call's label, where nothing is known is a fine answer |

A backend whose `tools/list` is broken while its `tools/call` works is a backend this
executor could serve before annotations existed. Failing its turn over a presentation hint
would be a regression dressed as a feature, so every reason to fail — an unreachable
listing, a tool the listing does not name, a malformed entry — produces `other`, which is
exactly what every tool call looked like before this module.

## Main symbols

| Symbol | What it is |
|---|---|
| `tool_kind(annotations)` | The mapping table above, as a pure function over the raw block |
| `ToolCatalogue(backends)` | One turn's `tools/list` per server, fetched at most once |
| `ToolCatalogue.listing(server)` | The strict form; raises what `tools/list` raises |
| `ToolCatalogue.kind(server, tool)` | The best-effort form; never raises |
| `UNKNOWN_KIND` | `"other"` — what nothing-is-known looks like, and what every tool call was before |

## Related

- [turn_mcp_router.py](turn_mcp_router.md) — the only caller, and where the permission
  prompt this feeds is sent
- [mcp_content.py](mcp_content.md) — the other MCP→ACP mapping table, for result content
- [mcp_stdio.py](mcp_stdio.md) — `list_tools`, and the protocol revision that carries
  annotations at all
