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

Multi-platform builds take a different path per engine, which is why the engine
*kind* matters and not just its path: ``docker`` needs ``buildx`` and emits an
OCI archive directly, while ``podman`` builds into a manifest list and then
pushes that to an archive. Plain ``docker build`` cannot produce a multi-platform
image at all, so choosing the wrong verb fails rather than silently producing a
single-arch image -- which would be the worse outcome, since a single-arch image
looks entirely correct until someone runs it on a Pi.

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

# linux/arm64 is what a Raspberry Pi 3, 4, 5 or Zero 2 W runs under 64-bit
# Raspberry Pi OS. There is deliberately no armv8.2 entry here: ARMv8.2-A (the
# Pi 5's Cortex-A76) is a superset of ARMv8-A, it runs an arm64/v8 image
# natively, and neither OCI nor Docker Hub defines an arm64/v8.2 platform. See
# the "Raspberry Pi" note in CLAUDE.md before adding one.
RASPBERRY_PI_PLATFORM = "linux/arm64"


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


def engine_kind(engine: str) -> str:
    """``podman`` or ``docker``, from the binary name.

    The two need different verbs for a multi-platform build, so this is load
    bearing rather than cosmetic.
    """
    name = Path(engine).name.lower()
    if name.startswith("docker"):
        return "docker"
    return "podman"


def normalize_platforms(platforms: str | None) -> list[str]:
    """Split a comma-separated platform list, dropping blanks."""
    if not platforms:
        return []
    return [p.strip() for p in platforms.split(",") if p.strip()]


def plan_commands(
    engine: str,
    tag: str,
    containerfile: Path,
    context: Path,
    output: Path,
    platforms: list[str],
) -> list[list[str]]:
    """The exact commands a build will run, in order.

    Split out from execution so the command *shape* can be tested. Multi-arch
    end-to-end cannot be verified without an engine, but "did we emit a
    single-arch build when we were asked for two platforms" can be, and that is
    the failure worth catching: a single-arch image is not obviously wrong until
    it will not start on a Pi.
    """
    if len(platforms) <= 1:
        build = [engine, "build"]
        if platforms:
            build += ["--platform", platforms[0]]
        build += ["-t", tag, "-f", str(containerfile), str(context)]
        return [build, [engine, "save", "-o", str(output), tag]]

    joined = ",".join(platforms)
    if engine_kind(engine) == "docker":
        # Plain `docker build` cannot do this; buildx writes the OCI archive itself.
        return [
            [
                engine,
                "buildx",
                "build",
                "--platform",
                joined,
                "-t",
                tag,
                "-f",
                str(containerfile),
                f"--output=type=oci,dest={output}",
                str(context),
            ]
        ]
    # podman builds into a manifest list, then exports it. --all keeps every
    # per-platform image; without it the archive carries only one.
    return [
        [
            engine,
            "build",
            "--platform",
            joined,
            "--manifest",
            tag,
            "-f",
            str(containerfile),
            str(context),
        ],
        [engine, "manifest", "push", "--all", tag, f"oci-archive:{output}"],
    ]


def build_and_save(
    engine: str,
    tag: str,
    containerfile: Path,
    context: Path,
    output: Path,
    runner: object = subprocess.call,
    platforms: list[str] | None = None,
) -> int:
    """Build the image, then export it -- in that order, and only in that order.

    The output file is removed first. If any step fails, no tar is left behind,
    so ``package`` and ``release-bundle`` cannot bundle a stale one from an
    earlier run.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    commands = plan_commands(engine, tag, containerfile, context, output, platforms or [])
    for command in commands:
        code = runner(command)  # type: ignore[operator]
        if code != 0:
            verb = " ".join(command[1:3])
            print(
                f"container-image: `{engine} {verb}` failed with exit {code}; not exporting.",
                file=sys.stderr,
            )
            # A partial or stale tar is worse than none: it looks like a real artifact.
            if output.exists():
                output.unlink()
            return int(code)

    if not output.exists():
        print(
            f"container-image: {engine} reported success but produced no {output}.",
            file=sys.stderr,
        )
        return 1

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
    parser.add_argument(
        "--platform",
        default=None,
        help=(
            "Comma-separated target platforms, e.g. "
            f"'linux/amd64,{RASPBERRY_PI_PLATFORM}'. One platform builds normally; two or "
            "more produce a manifest list, which needs docker buildx or podman and QEMU "
            "for any platform that is not the host's. Default: build for the host."
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
    platforms = normalize_platforms(args.platform)
    if len(platforms) > 1:
        print(
            f"container-image: building a manifest list for {', '.join(platforms)}.",
            file=sys.stderr,
        )
    return build_and_save(
        status.engine,
        args.tag,
        args.containerfile,
        args.context,
        args.output,
        platforms=platforms,
    )


if __name__ == "__main__":
    raise SystemExit(main())
