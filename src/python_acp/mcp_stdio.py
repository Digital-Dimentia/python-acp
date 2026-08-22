from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

logger = logging.getLogger("python_acp.mcp_stdio")

# The MCP revision this client proposes at `initialize`. It has nothing to do
# with `capabilities.SUPPORTED_PROTOCOL_VERSIONS` — that one is the ACP
# version, an int, on the other side of the bridge. Two protocols, two fields.
_MCP_PROTOCOL_VERSION = "2024-11-05"
# The revisions we can actually speak. The server's answer is authoritative and
# may name a revision we never proposed; anything outside this set means we hang
# up rather than guess. Read .claude/skills/mcp-protocol/spec-versions.md before
# widening it — a newer revision is a capability claim, not a string swap.
_SUPPORTED_MCP_PROTOCOL_VERSIONS: frozenset[str] = frozenset({_MCP_PROTOCOL_VERSION})

ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class MCPProtocolError(RuntimeError):
    """Raised when the MCP service responds with an error.

    Carries the originating JSON-RPC error `code` and `data` when the failure
    came from the server as an error *response*. Both are `None` for failures
    this client raises itself — timeouts, transport death, malformed results —
    because those have no server-assigned code. Callers use that difference to
    decide whether a code can be forwarded or must fall back to a generic one.

    A failed tool call is **not** one of these. `tools/call` reports tool-level
    failure through `isError` on a successful result; see `call_tool`.
    """

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    @classmethod
    def from_error_response(cls, error: Any) -> "MCPProtocolError":
        """Build from the `error` member of a JSON-RPC error response.

        A conforming server sends `{"code": int, "message": str}` with optional
        `data`. Anything else is kept as an unstructured message with no code,
        so a malformed server degrades to the generic path instead of putting
        junk on the client-facing wire.
        """
        if not isinstance(error, dict):
            return cls(f"MCP error: {error}")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, int):
            return cls(f"MCP error: {error}")
        text = message if isinstance(message, str) and message else "Unknown MCP error"
        return cls(f"MCP error {code}: {text}", code=code, data=error.get("data"))


def tool_result_text(result: dict[str, Any]) -> str:
    """Flatten the text blocks of a `tools/call` result into one string.

    Used to give the legacy `{"ok": false}` envelope, which has no room for
    structured content, something human-readable to report. The JSON-RPC
    surface forwards the full content array instead and does not need this.
    """
    parts: list[str] = []
    for block in result.get("content", []):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


@dataclass
class MCPStdioClient:
    """JSON-RPC client speaking to an MCP server over the subprocess's stdio.

    MCP is bidirectional: the server may send requests and notifications of its
    own at any time, not only responses to ours. A background read loop consumes
    every stdout message and routes it by shape, so nothing is dropped and the
    server never blocks waiting on a request we ignored.
    """

    command: Sequence[str]
    request_timeout: float = 30.0
    #: Environment variables to add for the subprocess, **overlaid on this process's
    #: own** rather than replacing it. A server command almost always needs `PATH` and
    #: `HOME` to run at all, and withholding them would make every client-supplied
    #: server fail for a reason that looks nothing like the cause. It is not a sandbox
    #: boundary either way: whoever supplies `env` already supplies `command`.
    env: Mapping[str, str] | None = None
    on_server_request: ServerRequestHandler | None = field(default=None)
    on_notification: NotificationHandler | None = field(default=None)

    # Bare assignments (no annotation) stay class attributes, not fields.
    _STDERR_CHUNK = 4096
    # asyncio caps stream readers at 64 KiB by default, which a large
    # resources/read response can exceed. Raise it so whole messages fit.
    _STREAM_LIMIT = 8 * 1024 * 1024
    # Cursor pagination is driven entirely by the server, so a broken or hostile
    # one can keep handing out cursors forever. Bound the walk and fail loudly.
    _MAX_LIST_PAGES = 100
    # Shutdown budget, per the MCP stdio shutdown sequence: how long a server
    # gets to exit on EOF before SIGTERM, and after SIGTERM before SIGKILL.
    _STOP_STDIN_TIMEOUT = 2.0
    _STOP_TERMINATE_TIMEOUT = 2.0

    def __post_init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        # The version both sides settled on, set by initialize() and None until
        # then. A handshake that fails negotiation leaves it None.
        self.protocol_version: str | None = None
        self._id = 0
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._STREAM_LIMIT,
            env=None if self.env is None else {**os.environ, **self.env},
        )
        self._stdout_task = asyncio.create_task(self._read_loop(self._proc))
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            await self._shutdown_process(proc)
        finally:
            # The read loop is cancelled only after the process is gone, so the
            # server's final stdout output is still consumed on the way out.
            await self._cancel_task("_stdout_task")
            await self._cancel_task("_stderr_task")
            self._fail_pending(MCPProtocolError("MCP process stopped"))
            self._proc = None

    async def _shutdown_process(self, proc: asyncio.subprocess.Process) -> None:
        """Shut the server down the way the MCP stdio transport prescribes.

        Close the server's stdin, wait for it to exit on EOF, escalate to
        SIGTERM, wait again, then SIGKILL. Starting at SIGTERM would signal
        every server that shuts down cleanly on EOF -- which is most of them,
        and is the documented contract they are written against.
        """
        if proc.returncode is not None:
            return

        await self._close_stdin(proc)
        if await self._wait_for_exit(proc, self._STOP_STDIN_TIMEOUT):
            return

        if self._signal(proc, "terminate") and await self._wait_for_exit(
            proc, self._STOP_TERMINATE_TIMEOUT
        ):
            return

        self._signal(proc, "kill")
        await proc.wait()

    async def _close_stdin(self, proc: asyncio.subprocess.Process) -> None:
        """Close the server's stdin and wait for the pipe to actually shut."""
        stdin = proc.stdin
        if stdin is None:
            return
        try:
            if not stdin.is_closing():
                stdin.close()
            await asyncio.wait_for(stdin.wait_closed(), timeout=self._STOP_STDIN_TIMEOUT)
        except Exception:
            # Best-effort: a broken pipe here just means the server is already
            # gone, and nothing about it should stop the rest of the shutdown.
            logger.debug("Closing MCP stdin did not complete cleanly", exc_info=True)

    @staticmethod
    async def _wait_for_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, action: str) -> bool:
        """Send SIGTERM/SIGKILL, tolerating a process reaped in the meantime."""
        try:
            getattr(proc, action)()
        except ProcessLookupError:
            return False
        return True

    async def __aenter__(self) -> "MCPStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def initialize(self) -> dict[str, Any]:
        """Run the MCP handshake and settle on a protocol version.

        Negotiation is a real round trip, not a formality: we propose
        `_MCP_PROTOCOL_VERSION`, and the server replies with the revision it
        will actually use — which need not be the one we asked for. A server
        that cannot speak our proposal MUST counter with one it supports, and a
        client that cannot speak the counter MUST hang up instead of carrying
        on. So a rejected version stops the subprocess before the error escapes,
        and `notifications/initialized` is never sent on that path: half a
        handshake is worse than none, because the mismatch would otherwise
        resurface later as unrelated-looking failures.
        """
        result = await self.request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "python-acp", "version": "0.1.0"},
            },
        )
        try:
            self.protocol_version = self._agreed_protocol_version(result)
        except MCPProtocolError:
            await self.stop()
            raise
        await self.notify("notifications/initialized")
        return result

    @staticmethod
    def _agreed_protocol_version(result: dict[str, Any]) -> str:
        """Validate the server's `initialize` answer, or raise.

        The result MUST carry `protocolVersion`; a server that omits it is not
        speaking the lifecycle we asked for, so an absent field is as fatal as
        an unusable one.
        """
        supported = ", ".join(sorted(_SUPPORTED_MCP_PROTOCOL_VERSIONS))
        version = result.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise MCPProtocolError(
                "MCP initialize result omitted protocolVersion; "
                f"python-acp proposed {_MCP_PROTOCOL_VERSION} and supports {supported}"
            )
        if version not in _SUPPORTED_MCP_PROTOCOL_VERSIONS:
            raise MCPProtocolError(
                f"Unsupported MCP protocol version {version} from server; "
                f"python-acp proposed {_MCP_PROTOCOL_VERSION} and supports {supported}"
            )
        if version != _MCP_PROTOCOL_VERSION:
            logger.info(
                "MCP server countered protocol version %s with %s",
                _MCP_PROTOCOL_VERSION,
                version,
            )
        return version

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._list_all("tools/list", "tools")

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke an MCP tool.

        **A tool that fails is not a protocol error.** MCP reports tool-level
        failure as a *successful* JSON-RPC result carrying `isError: true` and
        content explaining the failure — deliberately, so the caller can read
        what went wrong. Only a JSON-RPC error response (the tool is unknown,
        the arguments are invalid) raises `MCPProtocolError`.

        `isError` is optional on the wire and defaults to false. It is filled in
        here so callers can read the flag unconditionally rather than each
        re-deriving the default.
        """
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})

        content = result.get("content", [])
        if not isinstance(content, list):
            raise MCPProtocolError("Invalid tools/call response: 'content' must be an array")

        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise MCPProtocolError("Invalid tools/call response: 'isError' must be a boolean")

        result["content"] = content
        result["isError"] = is_error
        return result

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._list_all("prompts/list", "prompts")

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request("prompts/get", {"name": name, "arguments": arguments or {}})

    async def list_resources(self) -> list[dict[str, Any]]:
        return await self._list_all("resources/list", "resources")

    async def _list_all(self, method: str, key: str) -> list[dict[str, Any]]:
        """Walk an MCP cursor-paginated list method to exhaustion.

        MCP list results carry `nextCursor` when more pages exist; the client
        re-issues the request with `cursor` set until the field is absent.
        **An absent `nextCursor` is the only terminator** — an empty page in the
        middle of a walk is legal and does not mean the end.

        The loop is entirely server-driven, so it is bounded twice: a cursor the
        server has already handed out, and a hard page ceiling. Either one raises
        `MCPProtocolError` rather than hanging the bridge forever.
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for _ in range(self._MAX_LIST_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            response = await self.request(method, params)

            page = response.get(key, [])
            if not isinstance(page, list):
                raise MCPProtocolError(f"Invalid {method} response")
            items.extend(page)

            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                return items
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MCPProtocolError(f"Invalid nextCursor in {method} response")
            if next_cursor in seen_cursors:
                raise MCPProtocolError(f"{method} repeated cursor {next_cursor!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise MCPProtocolError(f"{method} exceeded {self._MAX_LIST_PAGES} pages")

    async def read_resource(
        self, resource_id: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"uri": resource_id}
        if arguments:
            params["arguments"] = arguments
        return await self.request("resources/read", params)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        async with self._write_lock:
            await self._write(payload)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()

        # The lock covers id allocation and the write only. Waiting for the reply
        # happens outside it, so concurrent requests pipeline instead of queueing.
        async with self._write_lock:
            self._id += 1
            request_id = self._id
            self._pending[request_id] = future
            try:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            except BaseException:
                self._pending.pop(request_id, None)
                raise

        try:
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise MCPProtocolError("Timed out waiting for MCP response") from exc
        finally:
            self._pending.pop(request_id, None)

        error = response.get("error")
        if error:
            raise MCPProtocolError.from_error_response(error)
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(f"Invalid response for method {method}")
        return result

    async def _write(self, payload: dict[str, Any]) -> None:
        stdin = self._proc.stdin if self._proc is not None else None
        # is_closing() covers the window inside stop() where stdin has been
        # closed but the process has not been reaped yet.
        if stdin is None or stdin.is_closing():
            raise MCPProtocolError("MCP process not running")
        stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await stdin.drain()

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        """Consume every stdout message for the life of the subprocess."""
        stream = proc.stdout
        if stream is None:
            return
        reason = "MCP process closed stdout"
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    logger.debug("Skipping non-JSON MCP stdout line")
                    continue
                if not isinstance(message, dict):
                    continue
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("MCP read loop failed", exc_info=True)
            reason = f"MCP read loop failed: {exc}"
        self._fail_pending(MCPProtocolError(reason))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        if method is None:
            self._resolve_response(message_id, message)
            return

        if not isinstance(method, str) or not method:
            return

        if message_id is None:
            await self._handle_notification(method, params)
            return

        await self._handle_server_request(message_id, method, params)

    def _resolve_response(self, message_id: Any, message: dict[str, Any]) -> None:
        future = self._pending.pop(message_id, None) if message_id is not None else None
        if future is None:
            logger.debug("Discarding MCP response with unknown id %r", message_id)
            return
        if not future.done():
            future.set_result(message)

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.debug("MCP server notification: %s", method)
        if self.on_notification is None:
            return
        try:
            await self.on_notification(method, params)
        except Exception:
            # A misbehaving handler must not kill the read loop.
            logger.debug("MCP notification handler failed for %s", method, exc_info=True)

    async def _handle_server_request(
        self, request_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        """Answer a request the server sent us.

        Every server request gets a reply, even an error one — leaving it
        unanswered strands the server waiting on us.
        """
        logger.debug("MCP server request: %s", method)
        try:
            if method == "ping":
                # Protocol plumbing, not application logic: always answered here.
                result: dict[str, Any] = {}
            elif self.on_server_request is not None:
                result = await self.on_server_request(method, params)
            else:
                await self._respond_error(
                    request_id, -32601, f"Unsupported method: {method}", method
                )
                return
        except Exception as exc:
            logger.debug("MCP server request handler failed for %s", method, exc_info=True)
            await self._respond_error(request_id, -32603, str(exc), method)
            return

        await self._respond(
            {"jsonrpc": "2.0", "id": request_id, "result": result}, method=method
        )

    async def _respond_error(self, request_id: Any, code: int, message: str, method: str) -> None:
        """Reply to a server request with a JSON-RPC error.

        `method` is the method being answered, not the failure text. It exists
        only for `_respond`'s log line: when the reply itself cannot be written,
        the useful fact is which request went unanswered.
        """
        await self._respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            method=method,
        )

    async def _respond(self, payload: dict[str, Any], method: str) -> None:
        try:
            async with self._write_lock:
                await self._write(payload)
        except MCPProtocolError:
            # The process is already gone; nothing useful to do with the reply.
            logger.debug("Could not reply to MCP server request %s", method, exc_info=True)

    def _fail_pending(self, exc: MCPProtocolError) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Continuously read the server's stderr so its pipe buffer cannot fill.

        stderr is piped, so nothing consuming it means the OS buffer fills and the
        server blocks mid-write — deadlocking every request on this client. Lines
        are logged at debug, which surfaces them under the CLI's --debug flag.
        """
        stream = proc.stderr
        if stream is None:
            return
        buffer = b""
        try:
            while True:
                chunk = await stream.read(self._STDERR_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    self._log_stderr_line(line)
                if len(buffer) > self._STDERR_CHUNK:
                    # One line longer than a chunk; flush it rather than letting
                    # the buffer grow without bound.
                    self._log_stderr_line(buffer)
                    buffer = b""
        except asyncio.CancelledError:
            raise
        except Exception:
            # Draining is best-effort; it must never take down the client.
            logger.debug("MCP stderr drain stopped early", exc_info=True)
        finally:
            if buffer:
                self._log_stderr_line(buffer)

    @staticmethod
    def _log_stderr_line(line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            logger.debug("MCP server stderr: %s", text)

    async def _cancel_task(self, attribute: str) -> None:
        task: asyncio.Task[None] | None = getattr(self, attribute)
        setattr(self, attribute, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
