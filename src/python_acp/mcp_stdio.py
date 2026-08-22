from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger("python_acp.mcp_stdio")

ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class MCPProtocolError(RuntimeError):
    """Raised when the MCP service responds with an error."""


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

    def __post_init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
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
        )
        self._stdout_task = asyncio.create_task(self._read_loop(self._proc))
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        await self._cancel_task("_stdout_task")
        await self._cancel_task("_stderr_task")
        self._fail_pending(MCPProtocolError("MCP process stopped"))
        self._proc = None

    async def __aenter__(self) -> "MCPStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "python-acp", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._list_all("tools/list", "tools")

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})

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
            raise MCPProtocolError(str(error))
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(f"Invalid response for method {method}")
        return result

    async def _write(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPProtocolError("MCP process not running")
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

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
                await self._respond_error(request_id, -32601, f"Unsupported method: {method}")
                return
        except Exception as exc:
            logger.debug("MCP server request handler failed for %s", method, exc_info=True)
            await self._respond_error(request_id, -32603, str(exc))
            return

        await self._respond(
            {"jsonrpc": "2.0", "id": request_id, "result": result}, method=method
        )

    async def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            method=message,
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
