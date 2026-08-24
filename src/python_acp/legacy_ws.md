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
| `{"method": "tools/call", ...}` | JSON-RPC result / error | not yet — `pyacp-sld.2` |

The second is the one that needs explaining. `tools/*`, `prompts/*`, and `resources/*`
are **MCP methods on an ACP wire** — they are not ACP and never were, and
`PythonAcpAgent` has no members for them. Once the socket was bound to the SDK they would
have answered `-32601`, deleting a working surface in the same release that rebound it.
D4 promises the legacy API keeps working through the migration, so they are carried here
under their current names until `pyacp-sld.2` moves them onto `ext_method` as
`_tools/call` and friends — a rename a client can be told about, rather than a
disappearance.

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

### What `use` points at, and why it is another deprecated thing

Every target in `ACTION_REPLACEMENTS` is on the other half of this module, which reads
oddly until you ask what an ACP-native replacement would be: **there is no ACP method that
lists tools.** The ACP path is `session/new` with `mcpServers` and then `session/prompt`,
where the turn executor calls tools on the client's behalf — a different shape of program,
not a method swap.

So the table names the like-for-like step a client can take *now*, and `pyacp-sld.2`
moves those targets under a namespaced prefix on `ext_method` as a second, smaller move.
Staging it is the whole point of D4. The prefix is not chosen yet, and that bead also owns
deciding whether the passthrough survives removal at all — so this table names only what
works today and makes no promise about the name it will have.

| Action | `use` |
|---|---|
| `list_tools` | `tools/list` |
| `call_tool` | `tools/call` |
| `list_prompts` | `prompts/list` |
| `get_prompt` | `prompts/get` |
| `list_resources` | `resources/list` |
| `read_resource` | `resources/read` |
| `ping` | `ping` |

## It needs `--mcp-command`, and nothing else does

`--mcp-command` became **optional** in `pyacp-db3`: ACP sessions carry their own MCP
servers in `session/new`, so an agent no longer needs a process-wide one. This surface
does — it predates sessions entirely and has nowhere else to look.

So `LegacyActionHandler` accepts `None` and says so, with a `ValueError` naming both ways
out (`--mcp-command`, or `session/new`), rather than failing later with something that
reads like a backend fault.

## `LEGACY_METHODS` only shrinks

The set is closed. It loses entries as `pyacp-sld.2` moves them to `ext_method`, and
never gains one — a new method that belongs on this surface is a new method that should
not exist.

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
