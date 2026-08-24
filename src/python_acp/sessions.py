"""Sessions: what the agent remembers between `session/new` and `session/close`.

A session is the unit every ACP method after `initialize` is addressed to. This module
owns the record and the registry that holds them, and nothing else — no JSON-RPC shapes,
no MCP subprocesses, no prompt execution. `agent.py` translates requests into calls on
`SessionRegistry`; `mcp_registry.py` (Phase 2.3) owns the backends a session's turns use.

## Why not `acp.contrib.session_state`

`pyacp-3rw.1` was told to evaluate it first. It does not fit, for reasons that are about
direction rather than completeness:

* **It is the client's side of the wire.** `SessionAccumulator.apply()` consumes
  `SessionNotification`s — the things an agent *sends* — and merges them into a UI
  snapshot of messages, tool calls, and a plan. We are the sender.
* **It holds one session, not a registry.** `session_id` is a single `str | None`, and a
  notification for a different id either resets the accumulator or raises.
* **None of the metadata is there.** No cwd, no `additionalDirectories`, no config
  options, no timestamps, no lifetime, no close.
* **It is marked experimental** in its own docstring — "APIs may change while we gather
  feedback" — which is not a thing to put under `session/new`.

It is still the right tool for a *different* job, and `pyacp-3rw.3` should reach for it:
`load_session` must replay history as `session/update` notifications, and a session that
fed an accumulator with every notification it sent would have exactly that history in a
form the SDK maintains. Recorded rather than built, because nothing emits notifications
until Phase 3.

## Lifetime

**Created** by `session/new` (or `fork`), **destroyed** by `session/close` or process
exit. There is deliberately **no idle expiry.** This process is a subprocess of the
client and its lifetime is the client's; a TTL would reap a session the user simply left
open, and the failure would look like data loss rather than a timeout.

The cost is honest: a long-lived process accumulates sessions a client never closes.
`session/close` is registered `unstable=True` in the SDK's agent router, so a client
without `use_unstable_protocol` cannot call it at all and will leak until exit. That is
the leak `docs/acp-compliance-matrix.md` warns about, and it is bounded in practice by
the process being short-lived.

## Fork copies, resume shares

The distinction is the whole reason both methods exist, and getting it wrong is silent:

| | `fork_session` | `resume_session` |
|---|---|---|
| session id | **new** | same |
| cwd, `additionalDirectories` | copied (the caller may override) | unchanged |
| mode and config option state | **deep-copied** — the fork's `set_mode` must not move the parent's | the same objects; there is only one session |
| history | copied at the fork point (`pyacp-3rw.3`, once history exists) | shared, because it *is* the session |
| MCP backends | **its own instances**, spawned from the same `mcpServers` spec | the same instances |
| in-flight turn | not inherited; a fork starts idle | whatever the session already had |

Backends are the one where cost argues the other way — forking re-spawns subprocesses
that are byte-identical to the parent's. Correctness wins: a shared backend would make
`session/close` on the fork tear down the parent's tools. A refcounted share is a valid
later optimisation *provided* closing one session cannot disturb another.
`pyacp-3rw.3` and `pyacp-db3` implement this; the semantics are fixed here so they do not
have to be rediscovered.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from acp.schema import (
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectGroup,
    SessionInfo,
    SessionMode,
    SessionModeState,
)

logger = logging.getLogger(__name__)

#: The two shapes `session/set_config_option` discriminates on `type`.
ConfigOption = SessionConfigOptionSelect | SessionConfigOptionBoolean

#: Sessions per `session/list` page. A single page is a conforming answer, so this is a
#: courtesy rather than a requirement — but a long-lived process can accumulate a lot of
#: sessions, and a client that asked for a list should not get all of them at once.
PAGE_SIZE = 100

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
CloseHook = Callable[[str], Awaitable[None]]


class UnknownSessionError(ValueError):
    """A `sessionId` that names no session we hold.

    **A `ValueError` on purpose.** `errors.to_request_error` maps that to `-32602
    Invalid params` with the reason in `data`, which is the honest answer: the parameter
    the client sent is not one we can act on. `-32603` would blame ourselves for the
    client's stale id, and `-32601` would claim the method does not exist. Nothing has to
    special-case this type for it to reach the wire correctly.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(f"No session with id {session_id!r}")
        self.session_id = session_id


class TurnAlreadyRunningError(RuntimeError):
    """A second `session/prompt` arrived while the first was still running.

    Not a `ValueError`: this is a protocol misuse the client can see coming, but it is
    also a state we must never silently allow, because two turns on one session would
    interleave `session/update` notifications with no way to tell them apart.
    """


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_session_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Session:
    """One ACP session and everything the agent remembers about it.

    Mutable by design: `set_mode` and `set_config_option` change it in place over its
    life, and `updated_at` is meant to move. Callers get the object, not a copy — with
    the single exception of `fork`, which is where the deep copy happens.
    """

    session_id: str
    cwd: str
    additional_directories: tuple[str, ...] = ()
    modes: SessionModeState | None = None
    config_options: tuple[ConfigOption, ...] = ()
    title: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    #: Every `session/update` this session has emitted, in order. `session/load` replays
    #: it. An append-only list rather than `acp.contrib.SessionAccumulator`: that helper
    #: *merges* updates into a snapshot, which is what a UI wants and the opposite of
    #: what a replay needs — order across categories, and duplicate chunks, are exactly
    #: the information it discards. See `sessions.md`.
    history: list[Any] = field(default_factory=list)
    #: Permission decisions the user asked to be remembered, keyed by qualified tool
    #: name. `allow_always` / `reject_always` are the ACP options that write here, and
    #: **session** is their scope — the SDK's own default option is literally named
    #: "Approve for session". Keeping it on the session rather than in the executor means
    #: it dies with the session instead of outliving it in a process-wide map.
    #:
    #: This module does not interpret the values; `turn_mcp_router.py` does.
    remembered_permissions: dict[str, bool] = field(default_factory=dict)
    #: The `toolCallId` of the MCP call running right now, or `None` between calls.
    #:
    #: Turn state parked on the session because it has to be readable **by session id
    #: from outside the turn**: an MCP server's `elicitation/create` arrives on that
    #: backend's read loop, in a task of its own, with no route back to the `TurnContext`
    #: of the turn that provoked it. This is how the forwarded question gets attached to
    #: the tool call it belongs to (`pyacp-owi`).
    #:
    #: Unique because a session runs **one turn at a time** (`attach_turn`) and a turn runs
    #: its invocations in order, so at most one MCP call is ever in flight per session. If
    #: either ever stops being true, this becomes a guess and must be replaced rather than
    #: patched.
    #:
    #: `compare=False, repr=False` because it is turn-scoped: two sessions that differ only
    #: in what they are doing this instant are not different sessions. A fork does not
    #: inherit it, for the same reason it does not inherit the in-flight turn.
    running_tool_call: str | None = field(default=None, repr=False, compare=False)
    _clock: Clock = field(default=_utc_now, repr=False, compare=False)
    _turn: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)
    _cancel_requested: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False, compare=False
    )

    # ------------------------------------------------------------------
    # Mutation — every one of these moves `updated_at`
    # ------------------------------------------------------------------

    def record(self, update: Any) -> None:
        """Remember one emitted `session/update` so `session/load` can replay it.

        Called by `turns.TurnContext.emit` on the way out, so the record is of what was
        *sent* rather than of what an executor intended to send.

        The list is unbounded, and that is a deliberate trade rather than an oversight:
        a cap would silently truncate the middle of a transcript a client asked to
        reload, which is worse than the memory. A session's history dies with the
        session.
        """
        self.history.append(update)
        self.touch()

    def touch(self) -> None:
        """Record activity. `SessionInfo.updatedAt` is what a client sorts a list by."""
        self.updated_at = self._clock()

    def set_mode(self, mode_id: str) -> SessionMode:
        """Switch modes, returning the one now current.

        Raises `ValueError` for an unknown mode *and* for a session that advertises no
        modes at all — `pyacp-fln.2` must emit `current_mode_update` after this, and
        emitting one for a mode the client was never offered would be worse than an error.
        """
        if self.modes is None:
            raise ValueError(f"Session {self.session_id} advertises no modes")
        available = {mode.id: mode for mode in self.modes.available_modes}
        if mode_id not in available:
            raise ValueError(
                f"Unknown mode {mode_id!r}; available: {sorted(available)}"
            )
        self.modes.current_mode_id = mode_id
        self.touch()
        return available[mode_id]

    def set_config_option(self, config_id: str, value: str | bool) -> ConfigOption:
        """Set one config option, returning it.

        The two variants validate differently and the request models are already
        discriminated on `type`, so a boolean sent to a select (or the reverse) is a
        `ValueError` here rather than a silently coerced value.
        """
        option = self.config_option(config_id)
        if isinstance(option, SessionConfigOptionBoolean):
            if not isinstance(value, bool):
                raise ValueError(f"Config option {config_id!r} is boolean, got {type(value).__name__}")
            option.current_value = value
        else:
            if not isinstance(value, str):
                raise ValueError(f"Config option {config_id!r} is a select, got {type(value).__name__}")
            allowed = set(_select_values(option))
            if value not in allowed:
                raise ValueError(
                    f"Unknown value {value!r} for config option {config_id!r}; "
                    f"allowed: {sorted(allowed)}"
                )
            option.current_value = value
        self.touch()
        return option

    def config_option(self, config_id: str) -> ConfigOption:
        for option in self.config_options:
            if option.id == config_id:
                return option
        raise ValueError(
            f"Unknown config option {config_id!r}; "
            f"available: {sorted(option.id for option in self.config_options)}"
        )

    # ------------------------------------------------------------------
    # The in-flight turn — where `session/cancel` reaches
    # ------------------------------------------------------------------

    @property
    def turn_is_running(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def attach_turn(self, turn: asyncio.Task[Any]) -> None:
        """Register the task serving `session/prompt`.

        One at a time. Two concurrent turns on one session would interleave their
        `session/update` notifications with nothing on the wire to tell them apart, so
        this refuses rather than queues — the client decides whether to cancel and retry.
        """
        if self.turn_is_running:
            raise TurnAlreadyRunningError(f"Session {self.session_id} already has a running turn")
        # A fresh event per turn. Reusing one would leave the next turn already flagged
        # as cancelled by the previous turn's `session/cancel`.
        self._cancel_requested = asyncio.Event()
        self._turn = turn
        self.touch()

    def detach_turn(self) -> None:
        self._turn = None
        self.touch()

    def cancel_turn(self) -> bool:
        """Ask the running turn to stop. Returns whether there was one.

        `False` is not an error: a client that cancels a turn which already finished is
        behaving correctly, and `session/cancel` is a notification with nowhere to report
        one anyway. The `stopReason: "cancelled"` half is `pyacp-hnk.5`'s — this only
        delivers the cancellation.
        """
        if not self.turn_is_running:
            return False
        assert self._turn is not None
        # Flagged **before** the task is cancelled, so an executor's `except
        # CancelledError` handler can already tell `session/cancel` from the whole
        # request dying. The order is the entire value of the flag.
        self._cancel_requested.set()
        self._turn.cancel()
        self.touch()
        return True

    @property
    def cancellation(self) -> asyncio.Event:
        """Set when `session/cancel` asks the running turn to stop.

        Task cancellation is still the mechanism — this is how an executor *knows*, so it
        can run async cleanup under `asyncio.shield` instead of racing the cancel, and
        can distinguish a cancelled turn from a cancelled request.
        """
        return self._cancel_requested

    # ------------------------------------------------------------------
    # Views and copies
    # ------------------------------------------------------------------

    @property
    def roots(self) -> tuple[str, ...]:
        """Everywhere this session is allowed to look: `cwd` first, then the extras.

        The containment rule itself is `paths.py`'s — this is only the declaration.
        Phase 4.2's `fs/*` calls are the first consumer:
        `paths.require_contained(path, session.roots)`.
        """
        return (self.cwd, *self.additional_directories)

    def to_info(self) -> SessionInfo:
        """The `session/list` view. `updatedAt` is a string in the schema, so ISO 8601."""
        return SessionInfo(
            sessionId=self.session_id,
            cwd=self.cwd,
            additionalDirectories=list(self.additional_directories) or None,
            title=self.title,
            updatedAt=self.updated_at.isoformat(),
        )

    def fork(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        additional_directories: Iterable[str] | None = None,
    ) -> Session:
        """A new session carrying a **deep copy** of this one's mutable state.

        `modes` and `config_options` are pydantic models that `set_mode` and
        `set_config_option` mutate in place, so a shallow copy would let the fork's
        settings move the parent's. The in-flight turn is not inherited — a fork starts
        idle, because a running task belongs to the request that started it.
        """
        return Session(
            session_id=session_id,
            cwd=self.cwd if cwd is None else cwd,
            additional_directories=(
                self.additional_directories
                if additional_directories is None
                else tuple(additional_directories)
            ),
            modes=None if self.modes is None else self.modes.model_copy(deep=True),
            config_options=tuple(
                option.model_copy(deep=True) for option in self.config_options
            ),
            title=self.title,
            # The transcript up to the fork point. A shallow copy is enough — updates
            # are never mutated after `record`, only appended — but it must be a *copy*,
            # or the child's next turn would append to the parent's transcript.
            history=list(self.history),
            # Copied, not shared: a fork answering "always allow" must not decide for its
            # parent. Same reasoning as the mode and config state above.
            remembered_permissions=dict(self.remembered_permissions),
            created_at=self._clock(),
            updated_at=self._clock(),
            _clock=self._clock,
        )


class SessionRegistry:
    """Every live session, addressable by id.

    Not thread-safe and does not need to be: one asyncio loop drives every connection,
    and `asyncio` gives us a single-threaded interleaving. It *is* shared across the
    WebSocket transport's connections, which is why `close` is the only thing that
    removes an entry — a disconnecting client must not silently delete sessions another
    connection may resume.
    """

    def __init__(
        self,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = _new_session_id,
        on_close: CloseHook | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._clock = clock
        self._id_factory = id_factory
        # The seam for `mcp_registry.py` (pyacp-3rw.3 / pyacp-db3). This module must not
        # import MCP — a session's backends are keyed by session id elsewhere — but the
        # registry is the only thing that knows when a session ends, so it has to be
        # what says so.
        self._on_close = on_close

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._sessions

    def __iter__(self) -> Iterator[Session]:
        return iter(tuple(self._sessions.values()))

    # ------------------------------------------------------------------

    def create(
        self,
        cwd: str,
        *,
        additional_directories: Iterable[str] | None = None,
        modes: SessionModeState | None = None,
        config_options: Iterable[ConfigOption] | None = None,
        title: str | None = None,
    ) -> Session:
        """Register a new session and return it.

        Path validation is **not** here. `pyacp-3rw.4` enforces the absolute-path
        constraint on `cwd` and `additionalDirectories`, at the edge where a bad value
        must become `-32602`; a registry that also validated would put the rule in two
        places and let them disagree.
        """
        now = self._clock()
        session = Session(
            session_id=self._id_factory(),
            cwd=cwd,
            additional_directories=tuple(additional_directories or ()),
            modes=modes,
            config_options=tuple(config_options or ()),
            title=title,
            created_at=now,
            updated_at=now,
            _clock=self._clock,
        )
        self._sessions[session.session_id] = session
        logger.debug("Session %s created for cwd %s", session.session_id, cwd)
        return session

    def get(self, session_id: str) -> Session:
        """The session, or `UnknownSessionError` — never `None`.

        Returning `None` would push the failure to whichever attribute access came next,
        several frames from the request that caused it.
        """
        try:
            return self._sessions[session_id]
        except KeyError:
            raise UnknownSessionError(session_id) from None

    def fork(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        additional_directories: Iterable[str] | None = None,
    ) -> Session:
        """Register a deep copy of an existing session under a new id."""
        parent = self.get(session_id)
        forked = parent.fork(
            self._id_factory(), cwd=cwd, additional_directories=additional_directories
        )
        self._sessions[forked.session_id] = forked
        logger.debug("Session %s forked from %s", forked.session_id, session_id)
        return forked

    def resume(self, session_id: str) -> Session:
        """The same session, marked active. Shares everything — see the module docstring."""
        session = self.get(session_id)
        session.touch()
        return session

    def list(self, cwd: str | None = None) -> tuple[Session, ...]:
        """Every session, most recently active first, optionally filtered by `cwd`.

        The ordering lives here rather than in the caller so that every caller sees the
        same one — which is what makes `page`'s cursor mean anything.
        """
        sessions = tuple(self._sessions.values())
        if cwd is not None:
            sessions = tuple(session for session in sessions if session.cwd == cwd)
        return tuple(sorted(sessions, key=_sort_key, reverse=True))

    def page(
        self, cwd: str | None = None, cursor: str | None = None, limit: int = PAGE_SIZE
    ) -> tuple[tuple[Session, ...], str | None]:
        """One page of `list()`, plus the cursor for the next one (`None` when done).

        A **keyset** cursor, not an offset: it names the last session on the page as
        `(updated_at, session_id)`, and the next page is everything strictly after it in
        the same ordering. An offset would skip or repeat entries whenever a session was
        created or touched between two calls, which for a live registry is most of the
        time.

        It is still not immune to `updated_at` moving — a session that becomes active
        mid-walk sorts earlier and can be seen twice. `session_id` is in the key so a
        client can dedupe, and a repeat is a far better failure than a silent omission.
        """
        sessions = self.list(cwd)
        if cursor is not None:
            after = _decode_cursor(cursor)
            sessions = tuple(session for session in sessions if _sort_key(session) < after)
        page = sessions[:limit]
        remaining = len(sessions) > limit
        return page, (_encode_cursor(page[-1]) if remaining and page else None)

    async def close(self, session_id: str) -> None:
        """Remove a session and release whatever was bound to it.

        The entry is dropped **before** the hook runs, so a hook that fails cannot leave
        a half-closed session addressable — a client retrying `session/close` on a
        session whose backend teardown threw would otherwise get `-32602` forever or
        never, depending on which side failed.
        """
        session = self.get(session_id)
        session.cancel_turn()
        del self._sessions[session_id]
        logger.debug("Session %s closed", session_id)
        if self._on_close is not None:
            await self._on_close(session_id)

    async def close_all(self) -> None:
        """Tear every session down. For process shutdown, not for a disconnect."""
        for session_id in tuple(self._sessions):
            await self.close(session_id)


def _sort_key(session: Session) -> tuple[str, str]:
    """The `list()` ordering, as something a cursor can carry across a JSON round trip."""
    return (session.updated_at.isoformat(), session.session_id)


def _encode_cursor(session: Session) -> str:
    return base64.urlsafe_b64encode(json.dumps(_sort_key(session)).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """Read a cursor back, or refuse it.

    A `ValueError`, so `errors.to_request_error` answers `-32602`: a cursor the agent did
    not issue is a bad parameter. Silently restarting from the first page would be worse
    — the client would loop forever without ever being told why.
    """
    try:
        updated_at, session_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(updated_at), str(session_id)
    except Exception:
        raise ValueError(f"Malformed session/list cursor: {cursor!r}") from None


def _select_values(option: SessionConfigOptionSelect) -> Iterator[str]:
    """Every value a select option will accept, flattening groups.

    `SessionConfigSelect.options` is `list[SessionConfigSelectOption]` **or**
    `list[SessionConfigSelectGroup]`, and only the grouped form nests. Validating against
    the top level alone would reject every value in a grouped option.
    """
    for entry in option.options:
        if isinstance(entry, SessionConfigSelectGroup):
            for nested in entry.options:
                yield nested.value
        else:
            yield entry.value
