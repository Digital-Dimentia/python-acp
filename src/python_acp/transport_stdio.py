"""Bind the ACP agent to this process's own stdin and stdout.

This is the transport real ACP clients use (decision D2): the client spawns
`python-acp` as a subprocess and speaks JSON-RPC over the pipe. The module owns the
binding and the listen/shutdown loop and nothing else — no argument parsing, no
agent-shaped logic.

`transport_*` faces the ACP client; `mcp_*` faces the backend. Two stdio modules sit
near each other in this directory and mean opposite directions.

## stdout is reserved

Once the agent is bound, **stdout is the wire**. A single stray byte that is not a
JSON-RPC message corrupts the stream, and the failure surfaces at the client as a parse
error far from whatever printed it. Discipline is not enough here: a `print()` anywhere
in this process — ours, or a dependency's on an unlucky code path — does the damage.

So `run_stdio` binds the real stdout into the SDK's writer first, then points
`sys.stdout` at stderr for the life of the connection. A stray `print()` then lands in
the log where it can be read, instead of in the protocol where it cannot.

**Windows is excluded from that swap on purpose.** The SDK's Windows stdio transport
resolves `sys.stdout` at *write* time (`acp/stdio.py:_StdoutTransport.write`) rather
than holding the pipe, so redirecting it there would send the JSON-RPC stream to stderr
and leave the client hearing nothing. On POSIX the transport holds the pipe from
`connect_write_pipe`, so the swap is safe.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import sys
from collections.abc import Iterator

from acp import run_agent, stdio_streams
from acp.interfaces import Agent

logger = logging.getLogger(__name__)

# Mirrors the SDK's own default for `run_agent`. `stdio_streams` on its own defaults to
# asyncio's 64 KiB, which a single multimodal prompt can exceed; we bind the streams
# ourselves, so the limit is ours to pass.
_STDIO_BUFFER_LIMIT_BYTES = 50 * 1024 * 1024


async def run_stdio(agent: Agent, *, use_unstable_protocol: bool = True) -> None:
    """Serve `agent` over stdin/stdout until the client closes the connection.

    `use_unstable_protocol` defaults to **True** deliberately. `session/close`,
    `session/fork`, and `session/resume` are registered `unstable=True` in the SDK's
    agent router; with the flag off the router answers `method_not_found` without ever
    calling the agent, which would make those three methods dead code no matter how
    completely they are implemented. See `docs/acp-compliance-matrix.md`.
    """
    reader, writer = await stdio_streams(limit=_STDIO_BUFFER_LIMIT_BYTES)
    logger.info("python-acp serving ACP over stdio")

    # The SDK names these from the client's point of view: `input_stream` is what the
    # agent writes to. Binding them here rather than letting `run_agent` do it is what
    # makes the stdout swap below possible — the writer already holds the real pipe.
    with _stdout_reserved():
        await run_agent(
            agent,
            input_stream=writer,
            output_stream=reader,
            use_unstable_protocol=use_unstable_protocol,
        )

    # `run_agent` returns on EOF and on nothing else, so reaching this line means the
    # client hung up. Saying so is worth a line: without it a clean shutdown is
    # indistinguishable from a crash at the far end — the process simply stops, and the
    # obvious reading is that the *agent* died mid-conversation. It is almost always the
    # parent closing the write end of the pipe (`communicate()`, `child.stdin.end()`, a
    # `stdin=DEVNULL` spawn). An exception propagates instead of arriving here, which is
    # what keeps this from claiming a graceful exit for a failed one.
    logger.info("client closed stdin; python-acp exiting")


@contextlib.contextmanager
def _stdout_reserved() -> Iterator[None]:
    """Point `sys.stdout` at stderr so nothing can write to the wire by accident.

    A no-op on Windows — see the module docstring for why undoing it there would break
    the transport rather than protect it.
    """
    if platform.system() == "Windows":
        yield
        return
    with contextlib.redirect_stdout(sys.stderr):
        yield
