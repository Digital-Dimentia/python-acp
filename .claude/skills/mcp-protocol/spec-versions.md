# MCP Revisions and What a Version Bump Costs

Read this before changing the `"2024-11-05"` string in `MCPStdioClient.initialize`.
MCP revisions are dated, not semver, and the negotiated version is whatever
`initialize` settles on — so "upgrading" means being able to *speak* the newer
revision, not just asking for it.

Spec index: <https://modelcontextprotocol.io/specification/>

## The revisions that matter

| Revision | What changed | Effect on this repo |
|---|---|---|
| `2024-11-05` | The original. `initialize` handshake, tools/resources/prompts, HTTP+SSE transport. | **What we pin.** Everything in `mcp_stdio.py` targets it. |
| `2025-03-26` | Streamable HTTP replaces HTTP+SSE. Tool annotations, audio content, progress messages, OAuth. | None — all stdio-side behavior is unchanged. Assumed by an HTTP server that gets no `MCP-Protocol-Version` header. |
| `2025-06-18` | `elicitation/create` added. `structuredContent` and `outputSchema` on tools. Resource links in tool results. `title` alongside `name` on tools/prompts/resources. `MCP-Protocol-Version` header required on HTTP. JSON-RPC batching removed. | The realistic next target. Same handshake, same framing, additive result fields. |
| `2026-07-28` | Redesign — see below. | A rewrite of the client, not a version bump. |

## Moving to `2025-06-18`

Cheap, and the transport does not move. What it buys and costs:

- Send `"protocolVersion": "2025-06-18"` and **widen what the response may say**.
  `initialize` already checks the reply, so bumping `_MCP_PROTOCOL_VERSION` alone turns
  every `2024-11-05`-only server into a hard failure. Put both revisions in
  `_SUPPORTED_MCP_PROTOCOL_VERSIONS` so a server that counters with the older one is
  accepted rather than hung up on.
- `elicitation` becomes declarable. This is the one that matters here: it is the MCP
  primitive that maps onto ACP `session/request_permission`, and `on_server_request` is
  already the hook for it. Declaring `elicitation: {}` in the capability block is what
  makes a server actually send `elicitation/create`.
  - Request: `{message, requestedSchema}` — a flat object schema of primitives only.
  - Response: `{action: "accept" | "decline" | "cancel", content?}`. All three are
    *successful* results; declining is not an error.
- Tool results may carry `structuredContent` next to `content`, and content blocks may
  be resource links. `call_tool` passes the whole dict through, so nothing breaks —
  but anything downstream that assumes `content[0].type == "text"` will.
- Batching is gone. `mcp_stdio.py` never batched, so nothing to remove.

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
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | <the server command> 2>/dev/null | head -1
```

The `protocolVersion` in the reply is what you actually negotiated; the `capabilities`
object is the list of methods worth calling.
