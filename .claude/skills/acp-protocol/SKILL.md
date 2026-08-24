---
name: acp-protocol
description: Use when changing python-acp's ACP surface — adding or modifying an Agent method, a capability literal, a session/update variant, an error response, or the deprecated action surface. Covers SDK-driven dispatch, the manifests that make advertisement match behaviour, the one error mapping, and the full checklist of files one change must touch. Trigger on work involving agent.py, capabilities.py, turns.py, errors.py, transport_ws.py, legacy_ws.py, ACP protocol compliance, or the ACP v1 plan in docs/full-apc-plan.md.
---

# python-acp Wire Contract

**Dispatch is not ours.** `acp.agent.router.build_agent_router` maps JSON-RPC method
names onto the attributes of `PythonAcpAgent`, and `acp.Connection` turns a returned
model into a result and an `acp.RequestError` into an error object. Nothing in this
repository parses a request id, builds an error envelope, or matches on a method name for
an ACP method.

That single fact reshapes everything an agent needs to know here. What is left to get
right is not plumbing but **promises**: what `initialize` advertises, what a capability
literal is allowed to mean, and which error code a failure becomes.

```
ACP client ──stdio or WebSocket──▶ python-acp ──MCP──▶ server subprocess
                (we are the agent)            (we are the client)
```

The `mcp-protocol` skill owns the right-hand arrow. This one owns the left.

## The rule that governs everything else

**An advertisement is a promise, and a promise is testable.**

Four manifests exist so that a claim cannot drift from the behaviour behind it, and each
one is enforced by a test that fails when they disagree. If you are about to hand-write a
literal that describes what this agent can do, you are almost certainly editing the wrong
file.

| Manifest | Lives in | Says |
|---|---|---|
| `AGENT_CAPABILITY_MANIFEST` | `capabilities.py` | Every leaf of `initialize`'s capability block, with the bead that owns each flip and why |
| `SESSION_UPDATE_DISPOSITIONS` | `turns.py` | Every `session/update` variant: emitted, deferred, or declined — with a reason |
| `CONFORMANCE` | `tests/test_conformance.py` | Every `acp.interfaces.Agent` member and its disposition |
| `supported_prompt_blocks`, `session_modes`, `session_config_options` | the `TurnExecutor` | What the executor reads and offers. `promptCapabilities` is **derived** from the first; `session/new` advertises the other two |

Read `src/python_acp/capabilities.md` before touching any of them.

## Adding or changing an Agent method

1. **Check the disposition first.** `docs/acp-compliance-matrix.md` states what every one
   of the 15 `acp.interfaces.Agent` members is for and why. If your change contradicts a
   row, amend the row in the same commit — the matrix is the contract, not a summary.
2. **Never delete a member to decline it.** Every route is registered `optional=False`,
   so an absent attribute is already `-32601` from the router. Declining means writing the
   method and returning the honest error, which is why `authenticate` answers `-32000`.
3. **Every method takes `**kwargs`.** The router splats the request's `_meta` keys in
   alongside real parameters, so a closed signature raises `TypeError` the first time a
   client attaches metadata.
4. **Every request-serving method carries `@as_request_error`.** Not defensive:
   `acp.Connection._run_request` catches a non-`RequestError` and answers a bare `-32603`,
   destroying a backend code and turning a `ValueError` into an internal error. The
   mapping has to happen on our side of that boundary. Notification handlers deliberately
   do **not** carry it — there is no reply channel to put an error on.
5. **Add a `CONFORMANCE` row.** `tests/test_conformance.py` walks the `Agent` Protocol and
   the router's routes in both directions, so a member with no row is a failure rather
   than a silence.
6. **Docs.** See the checklist at the bottom.

### Three unstable methods

`session/close`, `session/fork`, and `session/resume` are registered `unstable=True`. With
`use_unstable_protocol` off, the router answers `method_not_found` **without ever calling
the agent** — so a correct implementation is invisible and only a negative test can tell.
Both transports pass `True`.

Because the flag is per connection, so is the advertisement:
`build_agent_capabilities(unstable=...)` withholds those three capability rows when it is
off. Advertising them there would be a promise the SDK itself refuses to keep.

## Error-code mapping

**`errors.py` owns this. Nothing else picks a code.** The same `to_request_error` serves
`agent.py`, `transport_ws.py`, and the SDK-dispatched path, so the two client-facing
surfaces cannot answer differently. Full detail in `src/python_acp/errors.md`.

The rule: **a message is a concise sentence; structured detail goes in `data`.** That is
what `acp.schema.Error` asks for and what the SDK's own `RequestError` constructors do.

| Raised | Becomes | Message | `data` |
|---|---|---|---|
| `RequestError` | itself, unchanged | — | — |
| `MCPProtocolError` **with** a server code | that code | **the server's own** | `{source: "mcp", mcpCode, mcpData?}` |
| `MCPProtocolError` with no code | `-32603` | `Internal error` | `{reason}` |
| `ValueError` | `-32602` | `Invalid params` | `{reason}` |
| anything else | `-32603` | `Internal error` | `{reason}` |
| `asyncio.CancelledError` | **re-raised, never mapped** | — | — |

Two things follow that are easy to get wrong:

- **A forwarded backend error keeps the server's message.** It already wrote a concise
  sentence, and replacing it destroys the only account of what failed. Forwarding makes
  the code space ambiguous, so `data.source == "mcp"` is the discriminator — and **an
  error we originate never sets that key.** Not "usually": never.
- **Cancellation is not an error.** Returning a value from a cancelled coroutine tells
  asyncio the cancellation did not take. `request_cancelled()` exists for whoever
  *reports* one after letting the exception propagate. `REQUEST_CANCELLED = -32800` is an
  **unverified reading** of `acp.schema.Error`'s literal union — it is LSP's
  `RequestCancelled` and the ACP schema documents no meaning. `pyacp-tzd.5` owns
  confirming it.

Subclass `ValueError` to get `-32602` for free. `UnknownSessionError`,
`PathConstraintError`, and `UnknownBackendError` all do, and none of them needs a special
case anywhere.

**Not every `-32602` on the wire comes from `errors.py`.** Params that do not parse are
refused by the SDK's schema before an agent method is ever called, and it answers with
pydantic's own report — `data.errors`, carrying `loc` and `type` — not the `{reason}` the
table above describes. So `data`'s key names the layer that refused: `errors` is the
schema's, `reason` is ours, `source` is the backend's, and never more than one appears.
`tests/test_negative.py` asserts the whole mapping as one table — including that no
wrongness falls through to a bare `-32603` — and is where a new failure mode belongs.

## Capability advertisement

`initialize`'s block is built from `AGENT_CAPABILITY_MANIFEST` and nothing else. **Do not
hand-write one.**

To turn a capability on, four things in one commit — any three without the fourth fails
the suite:

1. build the feature;
2. write a test that exercises it;
3. change the manifest row's `advertised` value;
4. add `path -> "module:test_name"` to `CAPABILITY_EVIDENCE` in
   `tests/test_capabilities.py`.

Two families are **derived** rather than written, because a literal fixed in the table
would be a promise about a component the table cannot see:

- `promptCapabilities.image` / `.audio` / `.embeddedContext` come from the executor's
  `supported_prompt_blocks`. What a content block *means* depends on the executor, which
  D3 makes swappable.
- The three unstable `sessionCapabilities` rows come from the connection's flag.

`authMethods` is `[]` by decision, not by default, and that decision is **bound to its
premise**: a test asserts it together with the absence of any remote `mcpCapabilities`,
so flipping `http`, `sse`, or `acp` fails until the auth question is reopened.

`PROTOCOL_VERSION` is the **ACP** version, an integer. It is unrelated to the MCP
`protocolVersion` *string* in `mcp_stdio.py`. Two protocols, two version fields.

## Emitting `session/update`

`turns.SESSION_UPDATE_DISPOSITIONS` names all thirteen variants of the SDK's union and
what this project does about each. A variant we do not send is either **deferred** with
the bead that will bring it or **declined** with a structural reason; a test walks the
SDK's union so a release that grows a variant forces a decision.

- **`session/update` has no capability gate.** `ClientCapabilities` has no field for it
  and every ACP client must accept it. Neither does `session/request_permission`. **Do not
  invent gates for them.**
- `clientCapabilities.plan` gates the `agent_plan_*` **variants**, not the call. A
  plan-less client means suppressing those updates, never skipping `emit`.
- `TurnContext.emit` supplies the session id itself and records into `Session.history` for
  `session/load` replay. Do not call `client.session_update` directly from a turn.

Client-method gating lives on the seam, in method vocabulary: `context.require(Gate.X)`.
Four shapes that are not interchangeable — `fs` is **two independent booleans**,
`terminal` is **one boolean for all five** methods, `plan` is advertised by **presence**
of an empty marker model (`is not None`, never `bool(model)`), and `elicitation` is a
**container** of two independent presence markers (`form`, `url`) whose own presence
promises nothing: a client may send `elicitation: {}` and support neither mode. Reading
the outer object the way `plan` is read is the mistake `Gate.ELICITATION_FORM` and
`Gate.ELICITATION_URL` exist to prevent.

**One client method is declined structurally, and it is the only one.**
`complete_elicitation` is addressed by `elicitationId`, which exists only on the two
**URL** variants of `ElicitationMode`. Nothing here creates one — the sole source of an
elicitation is an MCP server's form-shaped `elicitation/create`, forwarded by
`elicitation.py` — so there is no id to complete. Row 11 of the client surface table in
`docs/acp-compliance-matrix.md` carries the reasoning; do not "fix" it back into a
deferred row with a bead on it.

## The deprecated surface

`legacy_ws.py` holds the `{"action": ...}` API **and** the MCP passthrough still carried
on JSON-RPC (`tools/*`, `prompts/*`, `resources/*`, `ping`,
`notifications/initialized`). `transport_ws.py` intercepts both in `receive()` before the
SDK sees them; stdio never had either.

Decision **D4** keeps it working through the migration; **Phase 7** removes it
(`pyacp-sld.3`), after Phase 8 proves parity. **Add nothing to it.** `LEGACY_METHODS` is a
closed set that only shrinks.

`initialize` is deliberately **not** in it: that is ACP, the agent serves it, and a
WebSocket client gets the same negotiated answer a stdio client gets.

Framing errors are the transport's, because the SDK's `Transport` moves already-decoded
dicts: malformed JSON is `-32700` and a non-object payload is `-32600`, both answered in
`transport_ws.py`. The `{"ok": false}` envelope has no code field, so a mapped error is
flattened back to its message for that shape only.

## Two SDK behaviours that will surprise you

- **`salvage_on_error` and `skip_invalid_items`.** A junk `protocolVersion` becomes the
  default instead of `-32602`, and a malformed entry in `session/new`'s `mcpServers` is
  **silently dropped** before the agent sees it. Both are pinned in `tests/test_negative.py`
  so they are known rather than rediscovered. You cannot refuse what never arrives.
  The second one has teeth and is tracked as `pyacp-mej`: the client asked for N servers,
  the agent opens the ones that survived, and `session/new` hands back an id whose tools
  silently do not exist — the exact failure `mcp_registry`'s all-or-nothing opening exists
  to prevent, arriving by a route that module cannot see.
- **`normalize_result`.** Five routes are registered with it — `authenticate`,
  `session/load`, `session/close`, `session/set_mode`, `session/set_config_option` — so
  their responses arrive as plain **dicts** rather than models when a test drives the
  router directly. Assert `result["configOptions"]`, not `result.config_options`.

## Adding a method: the docs checklist

Every one of these is required. See the `repo-docs-sync` skill for why.

1. `src/python_acp/agent.py` — the member, with `**kwargs` and `@as_request_error`.
2. The manifest that owns any literal you changed (`capabilities.py`, `turns.py`).
3. `tests/test_conformance.py` — the `CONFORMANCE` row.
4. `tests/test_capabilities.py` — a `CAPABILITY_EVIDENCE` entry, if a literal flipped.
5. `tests/fixtures/mock_mcp_server.py` — teach the fixture, if a new MCP call is involved.
6. The co-located `.md` beside every module you touched.
7. `ARCHITECTURE.md` — update the sequence diagram if the request path changed *shape*.
8. `README.md` — the client-facing surface, if a client would notice.
9. `docs/acp-compliance-matrix.md` — if a disposition changed.

## Verify

```bash
make lint && make test
```

`asyncio_mode = "auto"`, so `async def test_*` needs no decorator. `make build` needs
`PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org"` behind the TLS-intercepting proxy.

The interop suite (`tests/test_interop.py`) drives a client that imports nothing from
`python_acp`. It is the only thing that can prove the wire is sufficient, and it has
already caught one wrong decision — see `docs/interop.md`.
