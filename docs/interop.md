# Interop: does this agent work for a client we did not write?

Decision D2 says stdio is the primary transport because that is how Zed, Neovim, and
every other ACP client connects. This document is the check that "strict full ACP v1"
means something outside this repository (`pyacp-6ni.4`).

There are two halves, and they prove different things.

| | Client | Runs in CI | Proves |
|---|---|---|---|
| **Automated** | `tests/interop/acp_client.py` | yes | The wire is sufficient: a separate process sharing no code with the agent completes a whole session |
| **Recorded** | the SDK's `examples/client.py` | no | A client written by someone else drives us successfully |

The first is stronger evidence about the *protocol*; the second is stronger evidence
about *us*. Neither replaces the other.

## Automated: `tests/test_interop.py`

`tests/interop/acp_client.py` runs as its own process, imports `acp` and the standard
library and **nothing from `python_acp`**, and talks to the agent only over a pipe. Every
message it sends is built by the SDK and every reply is parsed by the SDK.

That constraint is the whole point, so it is asserted rather than trusted:
`test_the_interop_client_shares_no_code_with_the_agent` parses the file's imports. It
checks the AST rather than the text, because the file legitimately *names*
`python_acp.cli` in the argv it spawns.

The run covers `initialize` → `session/new` with its own MCP server → a prompt turn that
really runs a tool → **a file round trip through `fs/read_text_file` and
`fs/write_text_file`** → a prompt refused for naming a path outside the session's roots →
a refused prompt → `session/list` → `session/close` → clean exit, and reports one JSON
object so a failure is diagnosable from the transcript rather than from an exit code.

**It deliberately answers `session/request_permission` with `-32601`**, copying the SDK's
own example client. See below for why that matters.

**It does serve `fs/*`, and advertises them** (`pyacp-8bv.2`). The session's `cwd` is a
temporary directory the client creates, so containment is exercised on real paths. The
client is the only vantage point from which the *resolved* path the agent sends can be
observed at all — an in-process test sees the string the agent chose, not the one that
crossed the wire — which is why `line`, `limit`, and the resolved path are asserted from
the client's own record:

```json
"reads": [["/private/var/.../in.txt", 2, 1]], "written": "two\n"
```

## The finding: refusing a permission request is normal

`pyacp-8bv.1` made the agent ask the client for permission before every tool call, and
decided that a client answering `-32601` to `session/request_permission` was **broken** —
the method is mandatory, `ClientCapabilities` has no field for it — so the turn refused.

Then this bead read the SDK's own `examples/client.py`:

```python
class ExampleClient(Client):
    async def request_permission(self, session_id, tool_call, options, **kwargs):
        raise RequestError.method_not_found("session/request_permission")
```

The reference client answers exactly that, and so will any headless client with no human
to ask. An agent that becomes unusable against the reference client is the agent with the
problem, and the automated interop run reproduced it precisely: a well-formed tool
invocation came back `stopReason: "refusal"` with the tool never run.

**Corrected: the turn proceeds, and says so once per session.** This is not "assume
consent from nowhere" — the client named this tool and these arguments in
`session/prompt` itself, so the authorization already exists. The permission prompt was
only ever a courtesy to a human who might be watching, and a client that cannot reach one
has already made the decision.

**That reasoning is specific to the deterministic tool router and does not generalise.**
An LLM-backed executor *chooses* the tool, so a client's prompt authorizes nothing in
particular and the same fallback would be a hole. Any executor added later has to decide
this for itself; `turn_mcp_router.py` says so at the fallback.

This is what interop testing is for. No amount of reading our own suite would have found
it, because our own suite's clients answer permission requests.

## Recorded: the SDK's `examples/client.py`

Not runnable in CI — it is fetched from GitHub and reads prompts from a console — so per
the bead it is a documented procedure with recorded output rather than a skipped test.

```bash
curl -sSfo /tmp/sdk_client.py \
  https://raw.githubusercontent.com/agentclientprotocol/python-sdk/main/examples/client.py

printf '%s\n' \
  'hello there' \
  '{"tool": "echo", "arguments": {"text": "hi"}}' \
| .venv/bin/python /tmp/sdk_client.py "$(pwd)/.venv/bin/python" \
    -m python_acp.cli --transport stdio
```

Recorded run — `examples/client.py` at upstream commit `f8431a9a42fc` (2026-07-05),
against this repository:

```
python-acp serving ACP over stdio
> Refusing prompt for session 286cc562bd5b489d874f5114fed6d47c: Prompt block 0 is not JSON (Expecting value).
| Agent: Prompt block 0 is not JSON (Expecting value). Each text block must be a JSON object naming an MCP tool: {"tool": "<name>", "arguments": {...}, "server": "<name>"}. "arguments" defaults to {}; "server" may be omitted only when the session opened exactly one MCP server.
> Refusing prompt for session 286cc562bd5b489d874f5114fed6d47c: Prompt block 0 names a tool, but this session opened no MCP servers to run it against.
| Agent: Prompt block 0 names a tool, but this session opened no MCP servers to run it against. Each text block must be a JSON object naming an MCP tool: {"tool": "<name>", "arguments": {...}, "server": "<name>"}. "arguments" defaults to {}; "server" may be omitted only when the session opened exactly one MCP server.
>
```

What that shows: the handshake completed, a session was created, **two prompt turns ran
with their `agent_message_chunk` updates streamed back and rendered by the client**
(`| Agent: …` is the client printing what it received), and the agent exited cleanly on
EOF.

What it does **not** show is a tool call, and the reason is worth stating rather than
glossing: `examples/client.py` hardcodes `mcp_servers=[]` in its `session/new`, so the
session has no tools for any invocation to reach. That is a limitation of the example
client, not of the agent — the automated half covers the tool path, with a session that
does bring a server.

Both refusals are the router's, and both are legible: the client's user can read what the
convention is and what went wrong.

## Trying it in a real editor

Not automatable here, and worth doing before any release that claims editor support.
Point the editor's ACP agent configuration at:

```
command: /absolute/path/to/.venv/bin/python
args:    ["-m", "python_acp.cli", "--transport", "stdio"]
```

`--mcp-command` is optional; a session brings its own MCP servers through
`session/new`'s `mcpServers`. Remember that this agent runs tools rather than reading
prose, so a prompt has to be a JSON invocation — see the convention in
[turn_mcp_router.md](../src/python_acp/turn_mcp_router.md).

## Related

- [tests/test_interop.py](../tests/test_interop.py) — the automated half
- [tests/test_conformance.py](../tests/test_conformance.py) — the compliance matrix, executable
- [turn_mcp_router.md](../src/python_acp/turn_mcp_router.md) — the permission fallback and its scope
