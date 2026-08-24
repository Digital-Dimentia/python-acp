# MCP Revisions and What a Version Bump Costs

Read this before changing the `"2025-06-18"` string in `MCPStdioClient.initialize`.
MCP revisions are dated, not semver, and the negotiated version is whatever
`initialize` settles on — so "upgrading" means being able to *speak* the newer
revision, not just asking for it.

Spec index: <https://modelcontextprotocol.io/specification/>

## The revisions that matter

| Revision | What changed | Effect on this repo |
|---|---|---|
| `2024-11-05` | The original. `initialize` handshake, tools/resources/prompts, HTTP+SSE transport. | **Still accepted**, never proposed. A server pinned here must counter with it, and hanging up on that counter would drop every server that has not moved. |
| `2025-03-26` | Streamable HTTP replaces HTTP+SSE. Tool annotations, audio content, progress messages, OAuth. | None — all stdio-side behavior is unchanged. Assumed by an HTTP server that gets no `MCP-Protocol-Version` header. Its tool annotations are the unclaimed win: `pyacp-eg1.3`. |
| `2025-06-18` | `elicitation/create` added. `structuredContent` and `outputSchema` on tools. Resource links in tool results. `title` alongside `name` on tools/prompts/resources. `MCP-Protocol-Version` header required on HTTP. JSON-RPC batching removed. | **What we propose.** Same handshake, same framing, additive result fields. |
| `2026-07-28` | Redesign — see below. | A rewrite of the client, not a version bump. |

## The move to `2025-06-18` (done — `pyacp-pb7`)

Cheap, and the transport did not move. What it cost and what it bought, recorded so the
next bump is judged the same way:

- `_MCP_PROTOCOL_VERSION` became `"2025-06-18"` and `_SUPPORTED_MCP_PROTOCOL_VERSIONS`
  became **both** revisions. Those two are not the same set on purpose: `initialize`
  checks the reply, so bumping the proposal alone would have turned every
  `2024-11-05`-only server into a hard failure.
- `elicitation` became declarable. This is the one that mattered: it is the MCP primitive
  that maps onto ACP `session/request_permission`, and `on_server_request` is the hook
  for it. It is **declarable but not yet declared** — `pyacp-8bv.4` is where forwarding
  to the ACP client lands, and declaring a capability before something answers it strands
  the server on a `-32601` it was told would not happen.
  - Request: `{message, requestedSchema}` — a flat object schema of primitives only.
  - Response: `{action: "accept" | "decline" | "cancel", content?}`. All three are
    *successful* results; declining is not an error.
- Tool results may carry `structuredContent` next to `content`, and content blocks may
  be resource links. `call_tool` passes the whole dict through, so nothing broke —
  but anything downstream that assumes `content[0].type == "text"` will.
- Batching is gone. `mcp_stdio.py` never batched, so there was nothing to remove.
- Tool annotations (from `2025-03-26`) are now reachable and still unread. That is the
  remaining piece of the version bump's value: `pyacp-eg1.3`.

## Why `2026-07-28` is a different protocol

The revision the user-facing docs now describe
(<https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture>) breaks nearly
every assumption `MCPStdioClient` is built on:

- **Stateless.** Every request carries the protocol version, client capabilities, and
  client identity in its own `_meta` block. There is no connection state to establish.
- **No `initialize` handshake.** A mandatory `server/discover` request replaces it,
  returning `supportedVersions`, `capabilities`, and `serverInfo` — and it is
  *optional* to call, cacheable (`ttlMs`, `cacheScope`), and answerable before any
  other request. `notifications/initialized` goes away with it.
- **Notifications are opt-in subscriptions.** A client opens a long-lived
  `subscriptions/listen` stream naming the notification types it wants; the server
  acknowledges with `notifications/subscriptions/acknowledged` and tags every
  subsequent notification with `io.modelcontextprotocol/subscriptionId` in `_meta`.
  Notifications are explicitly best-effort — clients are told to keep polling.
- **Version errors are typed.** A server that cannot speak the requested version
  rejects with `UnsupportedProtocolVersionError` listing what it does support, and the
  client retries.
- **Sampling and logging are deprecated.** Sampling's replacement is integrating with
  an LLM provider directly; logging's is stderr or OpenTelemetry. Elicitation survives
  as the one client primitive, delivered via the Multi Round-Trip Requests pattern.
- **Results are envelopes.** `"resultType": "complete"` plus caching hints appear on
  list and call results.

Concretely, moving there means: `initialize()` deleted, a discover-and-cache path
added, `_meta` injected on every outbound request, a subscription manager for anything
that wants notifications, and `on_notification` rewired around subscription IDs. The
read loop and stderr drain survive; almost nothing above them does.

Treat it as its own project with its own beads epic, not as part of another change.

## Checking what a server speaks

The handshake answer is authoritative. Against any stdio server:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | <the server command> 2>/dev/null | head -1
```

The `protocolVersion` in the reply is what you actually negotiated; the `capabilities`
object is the list of methods worth calling.
