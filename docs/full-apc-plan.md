## Plan: Full ACP v1 Runtime + Architecture Docs

Implement strict ACP v1 support in python-acp as a full agent runtime, then document the resulting subsystem architecture with root and co-located Mermaid markdown docs. This combines the original protocol implementation roadmap with the new documentation workstream so docs are produced against the real architecture.

**Steps**
1. Phase 0: Protocol Contract and Runtime Boundaries
1.1. Freeze ACP v1 method/notification compliance matrix from schema.
1.2. Select implementation substrate (recommended: official ACP Python SDK for models/JSON-RPC plumbing).
1.3. Define module boundaries: RPC transport server, session state engine, prompt-turn executor, client-call adapters, MCP backend adapter, update stream bus.
1.4. Confirm JSON-RPC-only surface (no legacy action API).

2. Phase 1: Core ACP Server Foundation (blocking)
2.1. Refactor WebSocket endpoint into strict JSON-RPC parsing/dispatch/response behavior.
2.2. Implement initialize negotiation with accurate capabilities and auth methods.
2.3. Add request registry and cooperative cancellation for $/cancel_request.
2.4. Standardize ACP/JSON-RPC error mapping and payloads.

3. Phase 2: Session Lifecycle and State
3.1. Introduce durable session registry with session metadata (cwd, additionalDirectories, mode/config state, timestamps).
3.2. Implement baseline lifecycle: session/new, session/prompt, session/cancel handling, session/update emission.
3.3. Implement optional lifecycle for strict full v1: session/load, session/resume, session/list, session/delete, session/close.
3.4. Enforce absolute-path and argument constraints.

4. Phase 3: Prompt Turn and Streaming Semantics
4.1. Build prompt-turn executor for ACP content blocks and capability gating.
4.2. Emit full session/update variants (message chunks, thought chunks, tool call/update, plan, mode/config/session-info/usage updates).
4.3. Ensure prompt responses return correct stopReason semantics, especially cancelled.

5. Phase 4: Agent-to-Client Method Surface
5.1. Implement session/request_permission (including cancellation outcomes).
5.2. Implement fs/read_text_file and fs/write_text_file requests.
5.3. Implement terminal/create, terminal/output, terminal/wait_for_exit, terminal/kill, terminal/release.
5.4. Implement elicitation/create and elicitation/complete flows.

6. Phase 5: Authentication, Modes, and Config Options
6.1. Implement authenticate and logout flows.
6.2. Implement session/set_mode and current_mode_update propagation.
6.3. Implement session/set_config_option (select + boolean variants) and config_option_update.

7. Phase 6: MCP Backend Integration
7.1. Keep MCP stdio as backend adapter, not protocol boundary.
7.2. Map MCP results to ACP tool-call/content updates.
7.3. Add backend abstraction for non-MCP executors.

8. Phase 7: Architecture Documentation Workstream (new)
8.1. Create repo-root ARCHITECTURE.md with subsystem overview and Mermaid diagrams.
8.2. Add Mermaid sequence flow for core request lifecycle.
8.3. Create co-located module docs with same basename as each production python file:
- src/python_acp/cli.md
- src/python_acp/mcp_stdio.md
- src/python_acp/ws_bridge.md
8.4. For src/python_acp/__init__.py, add __init__.md only if meaningful behavior/API warrants it.
8.5. Add README links to architecture docs for discoverability.

9. Phase 8: Validation and Conformance
9.1. Add method-by-method ACP conformance tests.
9.2. Add golden JSON-RPC transcript tests (initialize, lifecycle, streaming, cancellation).
9.3. Add negative tests for unsupported methods and invalid params/path constraints.
9.4. Validate Mermaid rendering and cross-links for all new docs.

10. Delivery Sequencing
10.1. Primary blocking chain: Phase 1 -> Phase 2 -> Phase 3.
10.2. Parallel-capable streams after Phase 1: Phase 4, 5, 6.
10.3. Documentation (Phase 7) should start once module boundaries stabilize (after Phase 2), then finalize near Phase 8.

**Relevant files**
- /Users/dave/dev/other/python-acp/src/python_acp/ws_bridge.py — ACP JSON-RPC server dispatch and notifications.
- /Users/dave/dev/other/python-acp/src/python_acp/mcp_stdio.py — MCP backend adapter behavior and cancellation hooks.
- /Users/dave/dev/other/python-acp/src/python_acp/cli.py — runtime startup and configuration.
- /Users/dave/dev/other/python-acp/src/python_acp/__init__.py — exported API surface.
- /Users/dave/dev/other/python-acp/tests/test_mcp_stdio.py — conformance + backend tests.
- /Users/dave/dev/other/python-acp/tests/fixtures/mock_mcp_server.py — fixture for streaming/cancellation scenarios.
- /Users/dave/dev/other/python-acp/pyproject.toml — runtime and test dependencies.
- /Users/dave/dev/other/python-acp/Makefile — test/conformance targets.
- /Users/dave/dev/other/python-acp/README.md — docs navigation updates.
- /Users/dave/dev/other/python-acp/ARCHITECTURE.md — new root subsystem architecture doc.
- /Users/dave/dev/other/python-acp/src/python_acp/cli.md — new module architecture doc.
- /Users/dave/dev/other/python-acp/src/python_acp/mcp_stdio.md — new module architecture doc.
- /Users/dave/dev/other/python-acp/src/python_acp/ws_bridge.md — new module architecture doc.

**Verification**
1. ACP method conformance suite green for all supported methods/notifications.
2. Cancellation behavior verified for both session/cancel and $/cancel_request.
3. Capability advertisement matches implemented optional methods.
4. Mermaid diagrams render in root and module docs; links resolve.
5. Documentation terminology and symbol references match current code.

**Decisions**
- Scope includes strict full ACP v1 runtime behavior.
- Runtime surface is JSON-RPC only.
- MCP remains a backend adapter.
- Documentation scope includes production modules in src/python_acp only.
- Docs are co-located and same-name markdown by module basename, plus root ARCHITECTURE.md.

**Further Considerations**
1. If implementation changes major module boundaries, refresh diagrams before final merge.
2. Add optional CI check for markdown link integrity and Mermaid syntax in a follow-up.
3. Extend same-name docs to tests in a later phase if desired.
