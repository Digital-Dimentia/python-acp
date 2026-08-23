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

It **does** serve `fs/read_text_file` and `fs/write_text_file`, and advertises them, so
`pyacp-8bv.2`'s file round trip is proved over a real pipe with SDK-built messages rather
than only in-process. The files live in a temporary directory that is also the session's
`cwd`, so containment is exercised on real paths.

Usage:

    python tests/interop/acp_client.py <mcp-server-command...>

Prints one JSON object to stdout summarising the run, so a failure is diagnosable from
the captured transcript rather than from an exit code.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, connect_to_agent, text_block
from acp.schema import (
    ClientCapabilities,
    FileSystemCapabilities,
    Implementation,
    PlanCapabilities,
    ReadTextFileResponse,
)

AGENT_ARGV = ["-m", "python_acp.cli", "--transport", "stdio"]


class RefusingClient:
    """Accepts updates and serves `fs/*`; refuses every other optional method."""

    def __init__(self) -> None:
        self.updates: list[str] = []
        self.permission_requests = 0
        self.reads: list[tuple[str, int | None, int | None]] = []
        self.writes: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update.session_update)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permission_requests += 1
        raise RequestError.method_not_found("session/request_permission")

    async def read_text_file(self, session_id, path, line=None, limit=None, **kwargs):
        """Really read the file, and record the path the agent asked for.

        Recording it is the assertion: the agent must send the *resolved* path, and a
        client is the only place that can see what actually arrived on the wire.
        """
        self.reads.append((path, line, limit))
        lines = Path(path).read_text().splitlines(keepends=True)
        start = line - 1 if line else 0
        window = lines[start:] if limit is None else lines[start : start + limit]
        return ReadTextFileResponse(content="".join(window))

    async def write_text_file(self, session_id, path, content, **kwargs):
        self.writes.append(path)
        Path(path).write_text(content)
        return None

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

    workspace_dir = tempfile.TemporaryDirectory()
    # Resolved, because macOS puts the real temporary directory under /private and the
    # agent answers with resolved paths; comparing the two otherwise fails for a reason
    # that has nothing to do with the wire.
    workspace = Path(workspace_dir.name).resolve()

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
            client_capabilities=ClientCapabilities(
                plan=PlanCapabilities(),
                fs=FileSystemCapabilities(readTextFile=True, writeTextFile=True),
            ),
            client_info=Implementation(name="python-acp-interop", version="0"),
        )
        report["protocolVersion"] = initialized.protocol_version
        report["agentInfo"] = initialized.agent_info.name

        session = await conn.new_session(
            cwd=str(workspace),
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
        report["updates"] = list(client.updates)
        report["permissionRequests"] = client.permission_requests

        # A file round trip: read through the client, into the tool, back out through
        # the client. Nothing in the agent process opens either file.
        source = workspace / "in.txt"
        source.write_text("one\ntwo\nthree\n")
        destination = workspace / "out.txt"
        filed = await conn.prompt(
            session_id=session.session_id,
            prompt=[
                text_block(
                    json.dumps(
                        {
                            "tool": "echo",
                            "read": {"text": {"path": str(source), "line": 2, "limit": 1}},
                            "write": {"path": str(destination)},
                        }
                    )
                )
            ],
        )
        report["fileStopReason"] = filed.stop_reason
        report["reads"] = client.reads
        report["writes"] = client.writes
        report["written"] = destination.read_text() if destination.exists() else None

        # A path outside the session's roots, over the real wire: the containment rule
        # must refuse it before the client is ever asked to open it, so `reads` must not
        # grow.
        outside = await conn.prompt(
            session_id=session.session_id,
            prompt=[
                text_block(
                    json.dumps({"tool": "echo", "read": {"text": {"path": "/etc/hosts"}}})
                )
            ],
        )
        report["outsideStopReason"] = outside.stop_reason

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
        workspace_dir.cleanup()

    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
