#!/usr/bin/env python3
"""Build and export the python-acp container image.

The Makefile calls this instead of inlining the logic in a recipe, for the same
reason ``scripts/venv_bootstrap.py`` exists: the rules are conditional in ways
``make`` expresses badly, and a recipe cannot be tested.

The condition that matters is that *finding* a container engine is not the same
as being able to *use* one. The old recipe guarded with ``command -v podman ||
command -v docker`` and treated a hit as "an engine is available". That is wrong
in two common situations:

* podman on macOS is a client for a Linux VM. The binary is on ``PATH`` and
  runs, but every build fails if the VM is stopped or its socket is
  unreachable. On the development machine the sandbox cannot dial the VM's
  loopback SSH port at all, so ``command -v`` succeeds and the build then dies
  with exit 125.
* ``docker`` is on ``PATH`` on any machine that once installed Docker Desktop,
  whether or not the daemon is running.

So this script probes the engine (``<engine> version``, which contacts the
server and is non-zero when it cannot) rather than trusting ``PATH``, and
reports three distinct outcomes instead of two: no engine installed, an engine
that is installed but unreachable, and an engine that is ready.

Both "no engine" and "unreachable" *skip* by default, so packaging works on a
machine without a usable engine. ``--require`` turns any skip into a hard
error; the release workflow passes it, because a release that silently ships
without its container image is the failure this script exists to prevent.

The other half of that failure is subtler. The old recipe joined build and save
with ``;``, so ``save`` ran even after ``build`` failed -- and a machine holding
a ``python-acp:local`` from an earlier run would save *that* image and exit 0,
packaging a stale container as though it were current. Here the output file is
deleted before the build, and the save runs only if the build succeeded, so a
failed build leaves no tar for ``package``/``release-bundle`` to pick up.

Every mode is stdlib-only and safe to run with any interpreter >= 3.11.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path

DEFAULT_ENGINES = ("podman", "docker")
DEFAULT_PROBE_TIMEOUT = 120


class EngineState(Enum):
    """Why a build can or cannot proceed.

    ``UNREACHABLE`` is the state the old two-state check could not express, and
    the reason this module exists.
    """

    NO_ENGINE = "no-engine"
    UNREACHABLE = "unreachable"
    READY = "ready"


class EngineStatus:
    """An engine probe result: the state, the binary, and why."""

    def __init__(self, state: EngineState, engine: str | None = None, detail: str = "") -> None:
        self.state = state
        self.engine = engine
        self.detail = detail

    @property
    def ready(self) -> bool:
        return self.state is EngineState.READY

    def message(self) -> str:
        """A one-line explanation aimed at whoever is reading a build log."""
        if self.state is EngineState.NO_ENGINE:
            names = " nor ".join(DEFAULT_ENGINES)
            return f"Neither {names} is installed; skipping container-image export."
        if self.state is EngineState.UNREACHABLE:
            return (
                f"{self.engine} is installed but not reachable; skipping container-image "
                f"export. It is a client for a backend that is not answering -- for podman "
                f"on macOS, `podman machine start`. Detail: {self.detail}"
            )
        return f"{self.engine} is ready."


def find_engine(candidates: tuple[str, ...] = DEFAULT_ENGINES) -> str | None:
    """Return the first candidate engine on PATH, or None."""
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


def probe_engine(engine: str, timeout: int = DEFAULT_PROBE_TIMEOUT) -> EngineStatus:
    """Ask the engine to talk to its backend.

    ``version`` is the probe because it is cheap, exists on both engines, and
    contacts the server: podman exits 125 when it cannot dial its VM, and
    docker exits non-zero when the daemon is down. Commands that read only
    local config -- ``podman system connection list`` is the trap here -- exit 0
    while nothing is reachable, so they cannot be used for this.
    """
    try:
        completed = subprocess.run(
            [engine, "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return EngineStatus(
            EngineState.UNREACHABLE, engine, f"`{engine} version` timed out after {timeout}s"
        )
    except OSError as exc:  # the binary vanished or is not executable
        return EngineStatus(EngineState.UNREACHABLE, engine, str(exc))

    if completed.returncode != 0:
        detail = _first_meaningful_line(completed.stderr) or _first_meaningful_line(
            completed.stdout
        )
        return EngineStatus(
            EngineState.UNREACHABLE,
            engine,
            detail or f"`{engine} version` exited {completed.returncode}",
        )
    return EngineStatus(EngineState.READY, engine)


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def resolve_engine(
    engine: str | None = None,
    candidates: tuple[str, ...] = DEFAULT_ENGINES,
    timeout: int = DEFAULT_PROBE_TIMEOUT,
) -> EngineStatus:
    """Find an engine and probe it, collapsing both steps into one status."""
    found = engine or find_engine(candidates)
    if not found:
        return EngineStatus(EngineState.NO_ENGINE)
    return probe_engine(found, timeout=timeout)


def build_and_save(
    engine: str,
    tag: str,
    containerfile: Path,
    context: Path,
    output: Path,
    runner: object = subprocess.call,
) -> int:
    """Build the image, then export it -- in that order, and only in that order.

    The output file is removed first. If the build fails, no tar exists, so
    ``package`` and ``release-bundle`` cannot bundle a stale one left by an
    earlier run.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    build_cmd = [engine, "build", "-t", tag, "-f", str(containerfile), str(context)]
    code = runner(build_cmd)  # type: ignore[operator]
    if code != 0:
        print(
            f"container-image: `{engine} build` failed with exit {code}; not exporting.",
            file=sys.stderr,
        )
        return int(code)

    save_cmd = [engine, "save", "-o", str(output), tag]
    code = runner(save_cmd)  # type: ignore[operator]
    if code != 0:
        print(
            f"container-image: `{engine} save` failed with exit {code}.",
            file=sys.stderr,
        )
        # A partial tar is worse than none: it would look like a real artifact.
        if output.exists():
            output.unlink()
        return int(code)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="python-acp:local")
    parser.add_argument("--containerfile", type=Path, default=Path("Containerfile"))
    parser.add_argument("--context", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("dist/python-acp-container.tar"))
    parser.add_argument(
        "--engine",
        default=None,
        help="Use this engine instead of searching PATH for podman then docker.",
    )
    parser.add_argument(
        "--require",
        action="store_true",
        help=(
            "Treat a skip as a failure. The release workflow passes this so a release "
            "cannot silently ship without its container image."
        ),
    )
    parser.add_argument("--probe-timeout", type=int, default=DEFAULT_PROBE_TIMEOUT)
    args = parser.parse_args(argv)

    status = resolve_engine(args.engine, timeout=args.probe_timeout)
    if not status.ready:
        stream = sys.stderr
        print(f"container-image: {status.message()}", file=stream)
        if args.require:
            print(
                "container-image: --require was given, so this skip is a failure.",
                file=stream,
            )
            return 1
        return 0

    assert status.engine is not None
    return build_and_save(
        status.engine, args.tag, args.containerfile, args.context, args.output
    )


if __name__ == "__main__":
    raise SystemExit(main())
