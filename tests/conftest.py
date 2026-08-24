"""Suite-wide guard: no test may leave a subprocess behind.

This suite starts real processes on purpose — MCP servers, and the commands a
`TerminalClient` runs — because a mock proves nothing about a subprocess's lifetime. The
cost is that forgetting to reap one is **completely silent**. Nothing raises, no timeout
fires, and the tests pass.

CPython 3.11 does mention it, once, as a `PytestUnraisableExceptionWarning` from
`BaseSubprocessTransport.__del__` running after its event loop closed. `pyacp-6k5` was
filed against that warning and found three unrelated leaks behind it, so the warning is an
error now (see `filterwarnings` in `pyproject.toml`). But it is **not a reliable
detector**, and this file exists because of that:

* It fires only when the garbage collector happens to finalize the transport during a
  later test, so a real leak can pass a whole run unreported.
* When it does fire it names **whichever test was running at the time**, which is almost
  never the test that leaked. Chasing that name is chasing noise.
* Only 3.11 emits it at all. On 3.14 the same leak is entirely invisible.

So the check here is deterministic instead: wrap `asyncio.create_subprocess_exec`, keep a
**strong** reference to every process it returns, and at the end of the session fail if
any of their transports were never closed — naming the test that created each one.

The reference has to be strong. A weak one is collected exactly in the case that leaks,
which is how the first attempt at this reported "0 unclosed" while the warning was still
firing. Holding them also means nothing is finalized mid-run, so this guard *replaces* the
warning as the detector rather than supplementing it — if this file is ever removed, the
leaks come back silently.

## Finding the leak once this fails

The failure names the creating test. From there the cause is usually one of the three
`pyacp-6k5` found, in rough order of likelihood:

1. **A `SessionRegistry` built without `on_close=backends.close`.** That hook is the
   entire coupling between sessions and their MCP subprocesses (decision B6a in
   `docs/module-boundaries.md`), so a harness that builds both registries and forgets it
   closes sessions while their servers keep running. Three separate harnesses had this.
2. **A harness that hangs up without closing its sessions.** A disconnect deliberately
   does *not* close sessions, so `sessions.close_all()` is what `cli.py` does on the way
   out and what a harness has to do too.
3. **A test double that walks away from a process it started** — one simulating a failed
   `terminal/release`, or one whose `create_terminal` is cancelled after the process is
   already running.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

#: Every process this suite started, held **strongly** — see the module docstring for why
#: a weak reference hides the one case that matters. Each entry is
#: `(process, command, node id of the test that created it)`.
_STARTED: list[tuple[Any, str, str]] = []

#: The test currently running, so a leak can be attributed to whoever created it rather
#: than to whoever happened to be running when it surfaced.
_CURRENT = {"node_id": "<collection or module import>"}

_REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec


async def _recording_create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
    process = await _REAL_CREATE_SUBPROCESS_EXEC(*args, **kwargs)
    _STARTED.append((process, " ".join(str(part) for part in args), _CURRENT["node_id"]))
    return process


asyncio.create_subprocess_exec = _recording_create_subprocess_exec  # type: ignore[assignment]


def pytest_runtest_setup(item: pytest.Item) -> None:
    _CURRENT["node_id"] = item.nodeid


def _leaked() -> list[str]:
    """Every process whose transport was never closed, with the test that started it.

    `_transport._closed` is private, and the `getattr` guard is deliberate: if a future
    CPython renames it this guard goes quiet rather than failing every run on a detail it
    has no business asserting. A silent guard is a bug to notice; a suite that cannot run
    is worse.
    """
    leaks = []
    for process, command, node_id in _STARTED:
        transport = getattr(process, "_transport", None)
        if transport is None or not hasattr(transport, "_closed"):
            continue
        if not transport._closed:
            leaks.append(f"  {node_id}\n    left running: {command} (returncode={process.returncode})")
    return leaks


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # Only when the run was otherwise clean. A suite that already failed has enough to
    # read, and a failing test very reasonably leaves its subprocess behind.
    if exitstatus != 0:
        return
    leaks = _leaked()
    if not leaks:
        return
    session.exitstatus = 1
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    detail = "\n".join(leaks)
    message = (
        f"{len(leaks)} subprocess(es) were started and never reaped. Nothing else in this "
        f"suite reports that, and on Python 3.14 it is invisible — see "
        f"tests/conftest.py for the three causes this usually is.\n{detail}"
    )
    if reporter is not None:
        reporter.write_sep("=", "leaked subprocesses", red=True)
        reporter.write_line(message)
    else:  # pragma: no cover - only when the terminal plugin is disabled
        print(message)
