# `legacy_ws.py` — the deprecated WebSocket surface, quarantined

Everything in this module is on its way out. Decision D4 in
[docs/full-apc-plan.md](../../docs/full-apc-plan.md) keeps the legacy API working
*through* the migration and removes it in Phase 7 (`pyacp-sld.3`). The module exists to
hold it **apart** from the ACP runtime, not to improve it.

**Add nothing here.** New capability goes on the ACP surface, which [agent.py](agent.md)
serves.

## Two shapes, one deprecation

| Request | Reply |
|---|---|
| `{"action": "list_tools", ...}` | `{"ok": true, ...}` / `{"ok": false, "error": "..."}` |
| `{"method": "tools/call", ...}` | JSON-RPC result / error |

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
| `LegacyActionHandler` | Serves both shapes for one connection; where `pyacp-sld.1` will put the deprecation warning |

## Tests

`tests/test_transport_ws.py`, under "The deprecated surface". They run through the real
transport rather than calling the handler directly, because interception order — legacy
before SDK — is half of what makes the surface work.

## Related

- [transport_ws.py docs](transport_ws.md) — what intercepts and maps for this module
- [errors.py docs](errors.md), [mcp_stdio.py docs](mcp_stdio.md)
