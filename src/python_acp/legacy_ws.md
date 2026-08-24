# `legacy_ws.py` — the deprecated WebSocket surface, quarantined

Everything in this module is on its way out. Decision D4 in
[docs/full-apc-plan.md](../../docs/full-apc-plan.md) keeps the legacy API working
*through* the migration and removes it in Phase 7 (`pyacp-sld.3`). The module exists to
hold it **apart** from the ACP runtime, not to improve it.

**Add nothing here.** New capability goes on the ACP surface, which [agent.py](agent.md)
serves.

## Two shapes, one deprecation

| Request | Reply | Says so on the wire? |
|---|---|---|
| `{"action": "list_tools", ...}` | `{"ok": true, ...}` / `{"ok": false, "error": "..."}` | yes — a `deprecated` block on every reply (`pyacp-sld.1`) |
| `{"method": "tools/call", ...}` | JSON-RPC result / error | not on the wire — `pyacp-sld.3` deletes it |

The second is the one that needs explaining. `tools/*`, `prompts/*`, and `resources/*`
are **MCP methods on an ACP wire** — they are not ACP and never were, and
`PythonAcpAgent` has no members for them. Once the socket was bound to the SDK they would
have answered `-32601`, deleting a working surface in the same release that rebound it.
D4 promises the legacy API keeps working through the migration, so they are carried here
under their current names for the length of the deprecation window.

**They are not moving to `ext_method`.** See "The passthrough does not survive" below.

`ping` and `notifications/initialized` are here for the same reason. Neither is an ACP
method, and `notifications/initialized` is in fact an MCP-ism that arrived by copy.

**`initialize` is deliberately absent.** It *is* ACP, the agent serves it, and a
WebSocket client now gets the same negotiated answer a stdio client gets. That is the
point of `pyacp-tzd.3`, and
`tests/test_transport_ws.py::test_only_non_acp_methods_are_claimed_as_legacy` is the
guard: a method claimed here would shadow the agent's.

## Saying so on a channel the client can read

`pyacp-sld.1` is D4's first step: the surface keeps working, and every use of it now says
it is going away. The notice goes **in the reply**, because a log line is invisible to the
person who needs it — they are at the other end of a WebSocket and the server's stdout is
not theirs to read.

```json
{
  "ok": true,
  "tools": [...],
  "deprecated": {
    "action": "list_tools",
    "use": "tools/list",
    "removedIn": "the ACP v1 migration (Phase 7)"
  }
}
```

Four decisions worth not re-deriving:

| Decision | Why |
|---|---|
| The notice is per **call**; the log line is per **action per connection** | The envelope is the signal a client acts on, so it must never be deduped. The log is the operator's copy, and a client looping on `call_tool` would otherwise bury every other line in one repeated sentence. No action is silent; none repeats |
| `removedIn` names a **phase**, not a version | `pyacp-sld.3` is gated on Phase 8 proving parity, so no release has been promised this surface. Inventing a version here would put a commitment on the wire that nobody made |
| The **failure** envelope carries it too | A client whose call failed is no less on a surface that is going away, and is arguably reading the reply more closely. Built in `transport_ws.py`, because that is where that envelope is built |
| An unsupported action gets a notice **without** `use` | Using a surface that is going away is the thing worth saying, and getting the action name wrong does not make it less true — but there is no honest migration target for a method that never existed |

**Everything else about the envelope is untouched.** D4 promises the surface keeps
*working*, so `ok`, `tools`, `result`, and `error` mean exactly what they meant before and
`deprecated` is purely additive.

### What `use` points at, and why two-thirds of the table is empty

| Action | `use` |
|---|---|
| `list_tools` | `session/new + session/prompt` |
| `call_tool` | `session/new + session/prompt` |
| `list_prompts` | — |
| `get_prompt` | — |
| `list_resources` | — |
| `read_resource` | — |
| `ping` | — |

Each entry used to name the **JSON-RPC passthrough form of the same call** —
`list_tools` → `tools/list` — on the reasoning that those names would survive a move to
`ext_method`. `pyacp-sld.2` decided they will not, so pointing a client at one would be
pointing at a target that dies in the same release: two breaking changes where one will
do.

The ACP path is a different *shape of program*, not a method swap. Open a session with
`session/new` naming the server in `mcpServers`, then drive it with `session/prompt`; the
turn executor calls tools on the client's behalf, and the turn's first
`available_commands` update is the tool list. That covers `list_tools` and `call_tool`.

**The dashes are the finding, not a gap.** MCP prompts and resources have no ACP path at
all — ACP's model is that an agent uses them internally, not that a client reaches through
the agent to the server — and `ping` is MCP plumbing with no ACP counterpart. Naming
something anyway would invent a migration. So `ACTION_REPLACEMENTS` keeps a key for every
action, with `None` where the honest answer is nothing, and `deprecation_notice` omits
`use` rather than filling it.

## The passthrough does not survive

`pyacp-sld.2` was scoped to move `tools/*`, `prompts/*`, and `resources/*` onto
`ext_method` under a namespaced prefix, and to decide whether the passthrough survives
removal at all. The second answer settles the first: **it does not survive, so the move is
declined.**

Three reasons, in the order they matter:

1. **It addresses the wrong server.** The passthrough talks to the process-wide
   `--mcp-command` backend. ACP v1 inverted exactly that: a session's MCP servers are
   named by the *client* in `session/new` and live and die with the session. Carrying the
   passthrough forward under `_tools/call` would preserve the pre-v1 architecture behind a
   new name, and keep `--mcp-command` alive to serve it (`pyacp-sld.4`).
2. **The move would cost clients two breaking changes instead of one** — a rename now, a
   deletion later — for a surface that is being deleted either way.
3. **The stated motive is already satisfied.** The bead's reasoning was that MCP method
   names "do not belong in an ACP agent's method table once dispatch comes from
   `acp.agent.router`". They are not in it: `transport_ws.receive()` intercepts every one
   before the SDK sees the message, and `tests/test_conformance.py` asserts the router's
   table separately. The pollution the move would have fixed does not exist.

**What is lost with it, stated plainly:** `prompts/get`, `prompts/list`,
`resources/list`, and `resources/read` disappear from this bridge entirely, because no ACP
method replaces them. A client that needs MCP prompts or resources should speak MCP to
that server itself. This was a deliberate product decision, taken with no known consumers
of the surface.

## It needs `--mcp-command`, and nothing else does

`--mcp-command` became **optional** in `pyacp-db3`: ACP sessions carry their own MCP
servers in `session/new`, so an agent no longer needs a process-wide one. This surface
does — it predates sessions entirely and has nowhere else to look.

So `LegacyActionHandler` accepts `None` and says so, with a `ValueError` naming both ways
out (`--mcp-command`, or `session/new`), rather than failing later with something that
reads like a backend fault.

## `LEGACY_METHODS` only shrinks

The set is closed and never gains an entry — a new method that belongs on this surface is
a new method that should not exist. It does not drain, either: `pyacp-sld.3` empties it in
one step, because `pyacp-sld.2` declined to move anything out of it first.

## Errors

Nothing here builds an error envelope. A bad request raises `ValueError`, a backend
failure raises `MCPProtocolError`, and [transport_ws.py](transport_ws.md) maps both
through [errors.py](errors.md). The `{"action": ...}` envelope has no code field, so a
mapped error is flattened back to its message for that shape only — which is why a
backend code arrives inside the string (`"MCP error -32601: ..."`) rather than beside it.

## Tool failures are not errors

A tool that fails returns a **successful** MCP result carrying `isError: true`.

- **JSON-RPC `tools/call`** returns `result` unchanged, `isError` and all. There is no
  error member. Converting it would hide the content explaining the failure and make a
  broken tool look like an unreachable backend.
- **Legacy `call_tool`** sets `"ok": false` and adds an `"error"` string taken from the
  tool's own text content, while still returning the full `result`.

## Main symbols

| Symbol | Purpose |
|---|---|
| `is_legacy(message)` | Whether a message belongs to this surface rather than to ACP. Called on every inbound frame before the SDK sees it |
| `LEGACY_METHODS` | The closed set of JSON-RPC methods this handler answers |
| `LegacyActionHandler` | Serves both shapes for one connection, and carries the per-connection set of actions already warned about |
| `LegacyActionHandler.warn_deprecated(action)` | The operator's copy of the notice, logged once per action per connection |
| `deprecation_notice(action)` | The `deprecated` block that rides on every action reply. A fresh dict each call — it ends up inside a reply the caller owns |
| `ACTION_REPLACEMENTS` | Action → the JSON-RPC method that does the same job today |
| `REMOVED_IN` | The milestone every notice names |

## Tests

`tests/test_transport_ws.py`, under "The deprecated surface" and "Deprecation of the
action surface". They run through the real transport rather than calling the handler
directly, because interception order — legacy before SDK — is half of what makes the
surface work, and because the failure envelope is built on the transport's side.

`test_every_action_reply_names_its_replacement_and_the_removal` asserts its request table
against `ACTION_REPLACEMENTS`, so an action that grows or vanishes fails there rather than
going unnoticed.

## Related

- [transport_ws.py docs](transport_ws.md) — what intercepts and maps for this module
- [errors.py docs](errors.md), [mcp_stdio.py docs](mcp_stdio.md)
