"""A standalone ACP client that knows nothing about python-acp.

Run as its own process, it spawns the agent and drives a whole session over a pipe. The
constraint that gives it value is the import list: **this file imports `acp` and the
standard library, and nothing from `python_acp`.** Every message it sends is built by the
SDK and every reply is parsed by the SDK, so a session completing here proves the wire is
sufficient — no shared objects, no shared assumptions, no in-process shortcuts.

It deliberately answers `session/request_permission` with `-32601`, copying what the
SDK's own `examples/client.py` does. That is not a hypothetical hostile client: it is the
reference client's behaviour, and an agent that becomes unusable against it has the
problem. See `docs/interop.md`.

Usage:

    python tests/interop/acp_client.py <mcp-server-command...>

Prints one JSON object to stdout summarising the run, so a failure is diagnosable from
the captured transcript rather than from an exit code.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, connect_to_agent, text_block
from acp.schema import ClientCapabilities, Implementation, PlanCapabilities

AGENT_ARGV = ["-m", "python_acp.cli", "--transport", "stdio"]


class RefusingClient:
    """Accepts updates; refuses every optional client method, and permission too."""

    def __init__(self) -> None:
        self.updates: list[str] = []
        self.permission_requests = 0

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update.session_update)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permission_requests += 1
        raise RequestError.method_not_found("session/request_permission")

    def on_connect(self, conn: Any) -> None:
        """Sync, and defined explicitly: `__getattr__` below would make it a coroutine
        the SDK never awaits."""

    def __getattr__(self, name: str):
        """Refuse anything else the agent might reach for, the way a minimal client does."""

        async def refuse(*args: Any, **kwargs: Any):
            raise RequestError.method_not_found(name)

        return refuse


async def main(argv: list[str]) -> int:
    mcp_command = argv[1:]
    if not mcp_command:
        print("usage: acp_client.py <mcp-server-command...>", file=sys.stderr)
        return 2

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *AGENT_ARGV,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None

    client = RefusingClient()
    conn = connect_to_agent(client, process.stdin, process.stdout)
    report: dict[str, Any] = {}
    try:
        initialized = await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(plan=PlanCapabilities()),
            client_info=Implementation(name="python-acp-interop", version="0"),
        )
        report["protocolVersion"] = initialized.protocol_version
        report["agentInfo"] = initialized.agent_info.name

        session = await conn.new_session(
            cwd=os.getcwd(),
            mcp_servers=[
                {
                    "name": "tools",
                    "command": mcp_command[0],
                    "args": list(mcp_command[1:]),
                    "env": [],
                }
            ],
        )
        report["sessionId"] = bool(session.session_id)

        prompted = await conn.prompt(
            session_id=session.session_id,
            prompt=[text_block(json.dumps({"tool": "echo", "arguments": {"text": "interop"}}))],
        )
        report["stopReason"] = prompted.stop_reason
        report["updates"] = client.updates
        report["permissionRequests"] = client.permission_requests

        refused = await conn.prompt(
            session_id=session.session_id, prompt=[text_block("not an invocation")]
        )
        report["refusedStopReason"] = refused.stop_reason

        listed = await conn.list_sessions()
        report["sessions"] = len(listed.sessions)
        await conn.close_session(session_id=session.session_id)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        await process.wait()
        report["agentExitCode"] = process.returncode

    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
